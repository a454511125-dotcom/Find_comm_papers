"""Inspectable, configurable ranking for computational communication research.

The score is a heuristic, not an estimate of scientific quality or a neural
semantic similarity. Original provider ranks and each contribution are retained.
"""
from __future__ import annotations

import ast
import copy
import html
import json
import math
import os
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

STOPWORDS = set("a an the of for to in on and or not with by from as is are be this that study research using how what about".split())


def normalize(text) -> str:
    text = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return " ".join(re.findall(r"[^\W_]+", text, re.UNICODE))


def tokens(text) -> set[str]:
    return {w for w in normalize(text).split() if len(w) > 1 and w not in STOPWORDS}


def concept_tokens(text, profile):
    aliases = {word: base for base, words in profile.get("term_variants", {}).items() for word in [base, *words]}
    return {aliases.get(word, word) for word in tokens(text)}


def doi_key(value) -> str:
    value = unquote(str(value or "")).strip().casefold()
    return re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", value).rstrip(".,;")


def validate_weights(weights: dict) -> dict:
    expected = {"relevance", "discipline", "recency", "citations"}
    if set(weights) != expected:
        raise ValueError(f"weights must contain exactly {sorted(expected)}")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in weights.values()):
        raise ValueError("weights must be finite nonnegative numbers")
    total = sum(weights.values())
    if total <= 0:
        raise ValueError("at least one weight must be positive")
    return {k: v / total for k, v in weights.items()}


def load_profile() -> dict:
    default = Path(__file__).with_name("comm_profile.json")
    path = Path(os.environ.get("COMM_MCP_PROFILE", str(default)))
    profile = json.loads(path.read_text(encoding="utf-8-sig"))
    profile["weights"] = validate_weights(profile["weights"])
    scores = profile["discipline_scores"]
    if "other" not in scores or any(not isinstance(v, (float, int)) or not math.isfinite(v) or not 0 <= v <= 1 for v in scores.values()):
        raise ValueError("discipline_scores must be between 0 and 1 and include other")
    for group in set(profile["venues"]) | set(profile["signals"]):
        if group not in scores:
            raise ValueError(f"Missing discipline score: {group}")
    return profile


def as_list(value) -> list:
    if isinstance(value, list):
        return value
    return [v.strip() for v in str(value or "").split(";") if v.strip()]


def canonical(record) -> dict:
    if hasattr(record, "__dataclass_fields__"):
        from dataclasses import asdict
        paper = asdict(record)
    else:
        paper = copy.deepcopy(record)
    extra = paper.get("extra") or {}
    if isinstance(extra, str):
        try:
            extra = ast.literal_eval(extra)
        except (ValueError, SyntaxError):
            extra = {}
    paper["extra"] = extra if isinstance(extra, dict) else {}
    for key in ("authors", "categories", "keywords", "references"):
        paper[key] = as_list(paper.get(key))
    paper["doi"] = doi_key(paper.get("doi"))
    for key in ("published_date", "updated_date"):
        value = paper.get(key)
        paper[key] = value.isoformat() if hasattr(value, "isoformat") else str(value or "")
    paper["venue"] = str(paper.get("venue") or paper["extra"].get("venue") or paper["extra"].get("container_title") or "")
    paper["keywords"] = list(dict.fromkeys(paper["keywords"] + as_list(paper["extra"].get("topics"))))
    paper["abstract"] = html.unescape(re.sub(r"<[^>]+>", " ", paper.get("abstract") or ""))
    paper["title"] = html.unescape(str(paper.get("title") or ""))
    paper["source"] = str(paper.get("source") or "unknown")
    paper["language"] = paper.get("language") or ("zh" if paper["source"] == "cnki" else "en")
    paper["citations"] = max(0, int(paper.get("citations") or 0))
    return paper


def merge_papers(records: list[dict]) -> list[dict]:
    """Merge exact DOI duplicates; match DOI-less versions conservatively.

    Distinct nonempty DOIs never collapse solely because titles match.
    All source identifiers and URLs survive merging for later retrieval.
    """
    merged = []
    for raw in records:
        paper = canonical(raw)
        if not paper["title"]:
            continue
        title_key = normalize(paper["title"])
        first_author = normalize((paper["authors"] or [""])[0])
        match = None
        for current in merged:
            same_doi = paper["doi"] and paper["doi"] == current["doi"]
            author_match = first_author and first_author == normalize((current["authors"] or [""])[0])
            same_title = title_key == normalize(current["title"]) and author_match
            if same_doi or (same_title and not (paper["doi"] and current["doi"])):
                match = current
                break
        if match is None:
            paper["provenance"] = paper.get("provenance") or []
            merged.append(paper)
            match = paper
        else:
            for field in ("doi", "venue", "pdf_url", "url", "published_date"):
                if not match.get(field) and paper.get(field):
                    match[field] = paper[field]
            if len(paper["abstract"]) > len(match["abstract"]):
                match["abstract"] = paper["abstract"]
            match["citations"] = max(match["citations"], paper["citations"])
            for field in ("categories", "keywords"):
                match[field] = list(dict.fromkeys(match[field] + paper[field]))
            for key, value in paper["extra"].items():
                if not match["extra"].get(key):
                    match["extra"][key] = value
        evidence = {k: paper.get(k) for k in ("source", "paper_id", "pdf_url", "url", "provider_rank", "citations", "venue", "discovery_channel")}
        for prior in paper.get("provenance", []):
            if prior not in match["provenance"]:
                match["provenance"].append(copy.deepcopy(prior))
        if evidence not in match["provenance"]:
            match["provenance"].append(evidence)
    return merged


def classify(paper: dict, profile: dict) -> tuple[str, float, list[str]]:
    matches = []
    venue = normalize(paper["venue"])
    text = " " + normalize(" ".join([paper["title"], paper["abstract"], *paper["categories"], *paper["keywords"]])) + " "
    for group, aliases in profile["venues"].items():
        # Exact venue names prevent "Nature" matching "Nature Materials".
        if venue and venue in {normalize(v) for v in aliases}:
            matches.append((profile["discipline_scores"][group], group, f"venue: {paper['venue']}"))
    for group, signals in profile["signals"].items():
        for signal in signals:
            if " " + normalize(signal) + " " in text:
                matches.append((profile["discipline_scores"][group] * profile.get("topic_prior_multiplier", 0.85), group, f"topic: {signal}"))
    if not matches:
        return "other", profile["discipline_scores"]["other"], ["no configured discipline signal; retained"]
    matches.sort(reverse=True)
    score, group, _ = matches[0]
    return group, score, list(dict.fromkeys(m[2] for m in matches))[:8]


def rank_papers(records: list[dict], query: str, weights: dict | None = None, profile: dict | None = None) -> list[dict]:
    profile = profile or load_profile()
    weights = validate_weights({**profile["weights"], **(weights or {})})
    query_words = concept_tokens(query, profile)
    if not query_words:
        raise ValueError("Provide a nonempty research query, preferably English keywords")
    now = datetime.now(timezone.utc).year
    results = merge_papers(records)
    for paper in results:
        title_words = concept_tokens(paper["title"], profile)
        all_words = title_words | concept_tokens(paper["abstract"], profile)
        coverage = len(query_words & all_words) / len(query_words)
        title_coverage = len(query_words & title_words) / len(query_words)
        ranks = [p.get("provider_rank") for p in paper["provenance"] if isinstance(p.get("provider_rank"), int) and p["provider_rank"] > 0]
        provider_relevance = max((1 / math.sqrt(r) for r in ranks), default=0)
        relevance = 0.6 * coverage + 0.25 * title_coverage + 0.15 * provider_relevance
        review_flags = []
        political_query = "polarization" in query_words and bool(query_words & {"political", "affective", "ideological", "partisan"})
        context_words = all_words | concept_tokens(" ".join(paper["keywords"]), profile)
        political_evidence = context_words & {"political", "affective", "ideological", "partisan", "democracy", "election", "voter"}
        if political_query and not political_evidence:
            review_flags.append("political_context_not_found_in_available_metadata")
            # Polarization also describes cells, light, and materials. Keep uncertain
            # records visible; reduce their priority only when an abstract is available.
            if paper["abstract"].strip():
                relevance *= 0.25
        group, discipline, reasons = classify(paper, profile)
        venue_groups = [g for g, names in profile["venues"].items()
                        if normalize(paper["venue"]) in {normalize(n) for n in names}]
        # Topic/venue preference is gated by query evidence; prestige cannot
        # make a completely unrelated paper win solely through the prior.
        discipline *= min(1.0, relevance / 0.35)
        year_match = re.match(r"^(\d{4})", paper["published_date"])
        year = int(year_match[1]) if year_match else None
        recency = math.exp(-max(0, now - year) / 5) if year and 1800 <= year <= now + 1 else 0
        citations = min(1.0, math.log1p(paper["citations"]) / math.log1p(1000))
        components = {"relevance": relevance, "discipline": discipline, "recency": recency, "citations": citations}
        paper["ranking"] = {
            "score": round(100 * sum(components[k] * weights[k] for k in weights), 3),
            "discipline_group": group,
            "venue_groups": venue_groups,
            "components": {k: round(v, 4) for k, v in components.items()},
            "weights": weights,
            "reasons": reasons,
            "query_terms_matched": sorted(query_words & all_words),
            "review_flags": review_flags,
            "method": "configured term variants + lexical coverage + provider rank; discipline prior; not neural semantic similarity"
        }
    return sorted(results, key=lambda p: (-p["ranking"]["score"], normalize(p["title"])))
