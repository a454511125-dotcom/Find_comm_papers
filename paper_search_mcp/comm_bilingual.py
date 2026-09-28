"""Two-language discovery: independent ranking, no cross-language deduplication."""
from __future__ import annotations

import asyncio
import math
import re
from datetime import datetime, timezone

from .comm_ranking import canonical, load_profile, normalize, validate_weights
from .comm_cnki_host import single_keyword


def cnki_record(raw, rank=1, db_code="CJFD"):
    authors = raw.get("authors", [])
    if isinstance(authors, str):
        authors = [a.strip() for a in re.split(r"[;；,，、\r\n]+", authors) if a.strip()]
    date = str(raw.get("published_date") or raw.get("year") or "")
    year = re.search(r"(?:19|20)\d{2}", date)
    citation_text = str(raw.get("citations") or "").replace(",", "")
    citations = int(citation_text) if citation_text.isdigit() else 0
    return canonical({**raw, "authors": authors, "language": "zh", "source": "cnki",
                      "published_date": year[0] if year else "", "venue": raw.get("journal") or raw.get("venue", ""),
                      "citations": citations,
                      "provider_rank": rank, "extra": {**(raw.get("extra") or {}), "db_code": db_code,
                      "retrieval_language": "zh", "cnki_url": raw.get("url", ""),
                      "work_type": "thesis" if db_code in {"CDFD", "CMFD"} else "journal-article"}})


def zh_terms(query, profile):
    """Explicit phrases first; dictionary segmentation and bigrams for a continuous query."""
    groups = profile.get("chinese", {}).get("term_groups", {})
    aliases = {term: base for base, variants in groups.items() for term in [base, *variants]}
    query = re.sub(r"(?:19|20)\d{2}年(?:以来|之后|至今)?", " ", query)
    chunks = re.findall(r"[\u3400-\u9fff]+|[a-zA-Z0-9]+", query)
    result = []
    for chunk in chunks:
        if chunk in aliases:
            result.append(aliases[chunk])
        elif len(chunk) <= 4 or chunk.isascii():
            result.append(chunk)
        else:
            remainder = chunk
            for term in sorted(aliases, key=len, reverse=True):
                if term in remainder:
                    result.append(aliases[term])
                    remainder = remainder.replace(term, " ")
            for fragment in remainder.split():
                result.extend(fragment[i:i + 2] for i in range(len(fragment) - 1))
    return list(dict.fromkeys(t for t in result if t.strip()))


def rank_chinese(records, query, weights=None, profile=None):
    profile = profile or load_profile()
    weights = validate_weights({**profile["weights"], **(weights or {})})
    terms = zh_terms(query, profile)
    if not terms:
        raise ValueError("A nonempty Chinese query is required")
    settings = profile.get("chinese", {})
    aliases = settings.get("term_groups", {})
    year_now = datetime.now(timezone.utc).year
    result = []
    for record in records:  # Do not collapse the CNKI records supplied by the source.
        paper = canonical(record)
        title = normalize(paper["title"])
        full = normalize(paper["title"] + " " + paper["abstract"])
        def found(term, text):
            return any(normalize(v) in text for v in [term, *aliases.get(term, [])])
        matched = [t for t in terms if found(t, full)]
        relevance = .6 * len(matched) / len(terms) + .25 * sum(found(t, title) for t in terms) / len(terms)
        relevance += .15 / math.sqrt(max(1, paper.get("provider_rank", 1)))
        venue_groups = [g for g, venues in settings.get("venues", {}).items() if normalize(paper["venue"]) in {normalize(v) for v in venues}]
        priors = [(profile["discipline_scores"][g], g, "期刊匹配：" + paper["venue"]) for g in venue_groups]
        for group, signals in settings.get("signals", {}).items():
            priors.extend((profile["discipline_scores"][group] * profile.get("topic_prior_multiplier", .85), group, "主题匹配：" + s)
                          for s in signals if normalize(s) in full)
        prior, group, reason = max(priors, default=(profile["discipline_scores"]["other"], "other", "未命中配置的学科信号，保留候选"))
        discipline = prior * min(1, relevance / .35)
        year = int(paper["published_date"][:4]) if re.match(r"^\d{4}", paper["published_date"]) else None
        recency = math.exp(-max(0, year_now - year) / 5) if year and 1800 <= year <= year_now + 1 else 0
        components = {"relevance": relevance, "discipline": discipline, "recency": recency,
                      "citations": min(1, math.log1p(paper["citations"]) / math.log1p(1000))}
        paper["ranking"] = {"score": round(100 * sum(weights[k] * components[k] for k in weights), 3),
                            "weights": weights, "components": components, "discipline_group": group,
                            "venue_groups": venue_groups, "query_terms_matched": matched, "reasons": [reason],
                            "review_flags": [] if matched else ["query_terms_not_found_in_available_metadata"],
                            "method": "Chinese phrase variants/dictionary segmentation/bigram fallback; within-language heuristic"}
        result.append(paper)
    return sorted(result, key=lambda p: (-p["ranking"]["score"], normalize(p["title"])))


def combine(chinese, english, limit):
    """Equal-source reciprocal-rank fusion; raw cross-language scores are not compared."""
    result = []
    for language, papers in (("zh", chinese), ("en", english)):
        for rank, raw in enumerate(papers, 1):
            paper = canonical(raw)
            paper["language"] = language
            paper["joint_ranking"] = {"language_rank": rank, "score": round(1 / (60 + rank), 8),
                                       "method": "equal-language reciprocal rank; tied ranks alternate zh/en; not a cross-language relevance probability"}
            result.append(paper)
    result.sort(key=lambda p: (p["joint_ranking"]["language_rank"], 0 if p["language"] == "zh" else 1))
    return result[:limit]


def rank_mixed(records, query, weights=None, profile=None):
    from .comm_ranking import rank_papers
    papers = [canonical(p) for p in records]
    chinese = [p for p in papers if p["language"].startswith("zh")]
    english = [p for p in papers if not p["language"].startswith("zh")]
    if not chinese:
        return rank_papers(english, query, weights, profile)
    zh = rank_chinese(chinese, query, weights, profile)
    if not english:
        return zh
    return combine(zh, rank_papers(english, query, weights, profile), len(papers))


async def search(query, chinese_query, english_queries, languages, max_results, per_language,
                 year_start, year_end, english_sources, db_code, weights,
                 cnki_search, english_search):
    languages = list(dict.fromkeys(languages or ["zh", "en"]))
    if not languages or set(languages) - {"zh", "en"}:
        raise ValueError("languages must contain zh and/or en")
    if not query.strip() or not 1 <= max_results <= 100 or not 1 <= per_language <= 100:
        raise ValueError("Provide a research query; limits must be 1..100")
    end = year_end or datetime.now(timezone.utc).year
    start = year_start or 1800
    if not 1800 <= start <= end <= datetime.now(timezone.utc).year + 1:
        raise ValueError("Invalid year range")
    if db_code not in {"CJFD", "CDFD", "CMFD"}:
        raise ValueError("db_code must be CJFD, CDFD or CMFD")
    if "zh" in languages and not (chinese_query or "").strip():
        raise ValueError("Supply explicit chinese_query before searching zh")
    if "zh" in languages:
        chinese_query = single_keyword(chinese_query)
    if "en" in languages and (not english_queries or len(english_queries) > 6 or any(not q.strip() for q in english_queries)):
        raise ValueError("Supply 1..6 explicit english_queries before searching en")
    validate_weights({**load_profile()["weights"], **(weights or {})})
    async def zh():
        raw = await cnki_search(query=chinese_query, year_start=start, year_end=end,
                                max_results=per_language, db_code=db_code)
        if not raw.get("success"):
            return [], {"status": "needs_attention" if raw.get("captcha") else "unavailable",
                        "message": str(raw.get("message", "CNKI search unavailable"))[:500],
                        "network_diagnostics": raw.get("network_diagnostics", []),
                        "page_title": raw.get("page_title", ""),
                        "diagnostic_snapshot": raw.get("diagnostic_snapshot", "")}
        papers, excluded = [], 0
        for rank, record in enumerate(raw.get("papers", []), 1):
            paper = cnki_record(record, rank, db_code)
            year = int(paper["published_date"][:4]) if paper["published_date"] else None
            if not year or not start <= year <= end:
                excluded += 1
                continue
            if paper["title"]:
                papers.append(paper)
        ranked = rank_chinese(papers, query, weights)
        return ranked, {"status": "ok" if ranked else "empty", "count": len(ranked), "excluded_date": excluded,
                        "message": str(raw.get("message", ""))[:500], "ranking_query": query,
                        "retrieval_diagnostics":raw.get("diagnostics", {})}
    async def en():
        raw = await english_search(queries=english_queries, ranking_query=" ".join(english_queries),
                                   max_results=per_language, per_source=per_language, sources=english_sources,
                                   year_start=start, year_end=end, weights=weights)
        diagnostics = raw.get("searches", [])
        errors = any(s.get("status") != "ok" for run in diagnostics for s in run.get("sources", {}).values())
        papers = raw.get("papers", [])
        return papers, {"status": "partial" if errors and papers else "unavailable" if errors else "ok" if papers else "empty",
                        "count": len(papers), "searches": diagnostics}
    handlers = {"zh": zh, "en": en}
    outputs = await asyncio.gather(*(handlers[lang]() for lang in languages), return_exceptions=True)
    lanes, statuses = {}, {}
    for lang, output in zip(languages, outputs):
        if isinstance(output, BaseException):
            lanes[lang], statuses[lang] = [], {"status": "error", "error": type(output).__name__}
        else:
            lanes[lang], statuses[lang] = output
    papers = combine(lanes.get("zh", []), lanes.get("en", []), max_results)
    return {"query": query, "queries": {"zh": chinese_query if "zh" in languages else None,
                                       "en": english_queries if "en" in languages else []},
            "years": {"start": start, "end": end}, "sources": statuses, "count": len(papers),
            "counts_by_language": {lang: sum(p["language"] == lang for p in papers) for lang in languages},
            "papers": papers, "cross_language_deduplication": False,
            "ordering": "Each language is ranked for relevance and discipline; equal within-language ranks alternate, without comparing raw bilingual scores/citation counts.",
            "note": "English multi-provider deduplication and Zotero-library duplicate checking remain enabled. Source failures do not mean no literature exists."}
