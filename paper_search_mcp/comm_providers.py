"""Direct adapters for the upstream provider classes in the unified service.

Only network sessions are closed here. Downloads, file removal, and the legacy
upstream MCP server are never invoked. Registration is not a claim that an
upstream endpoint is currently reachable or that every source supports keywords.
"""
from __future__ import annotations

import re

from .academic_platforms.acm import ACMSearcher
from .academic_platforms.arxiv import ArxivSearcher
from .academic_platforms.base_search import BASESearcher
from .academic_platforms.biorxiv import BioRxivSearcher
from .academic_platforms.citeseerx import CiteSeerXSearcher
from .academic_platforms.core import CORESearcher
from .academic_platforms.crossref import CrossRefSearcher
from .academic_platforms.dblp import DBLPSearcher
from .academic_platforms.doaj import DOAJSearcher
from .academic_platforms.europepmc import EuropePMCSearcher
from .academic_platforms.google_scholar import GoogleScholarSearcher
from .academic_platforms.hal import HALSearcher
from .academic_platforms.iacr import IACRSearcher
from .academic_platforms.ieee import IEEESearcher
from .academic_platforms.medrxiv import MedRxivSearcher
from .academic_platforms.openaire import OpenAiresearcher
from .academic_platforms.openalex import OpenAlexSearcher
from .academic_platforms.pmc import PMCSearcher
from .academic_platforms.pubmed import PubMedSearcher
from .academic_platforms.semantic import SemanticSearcher
from .academic_platforms.ssrn import SSRNSearcher
from .academic_platforms.unpaywall import UnpaywallSearcher
from .academic_platforms.zenodo import ZenodoSearcher
from .comm_download import safe_error
from .comm_ranking import canonical, load_profile
from .config import get_env
from .utils import extract_doi


PROVIDERS = {
    "openalex": OpenAlexSearcher, "crossref": CrossRefSearcher,
    "arxiv": ArxivSearcher, "semantic": SemanticSearcher, "dblp": DBLPSearcher,
    "openalex_communication": OpenAlexSearcher,
    "google_scholar": GoogleScholarSearcher, "pubmed": PubMedSearcher,
    "pmc": PMCSearcher, "core": CORESearcher, "europepmc": EuropePMCSearcher,
    "openaire": OpenAiresearcher, "base": BASESearcher, "hal": HALSearcher,
    "zenodo": ZenodoSearcher, "doaj": DOAJSearcher, "ssrn": SSRNSearcher,
    "iacr": IACRSearcher, "citeseerx": CiteSeerXSearcher,
    "biorxiv": BioRxivSearcher, "medrxiv": MedRxivSearcher,
    "unpaywall": UnpaywallSearcher, "ieee": IEEESearcher, "acm": ACMSearcher,
}

SOURCE_NOTES = {
    "unpaywall": "DOI lookup only; requires UNPAYWALL_EMAIL. Not a keyword discovery source.",
    "biorxiv": "DOI, YYYY-MM-DD/YYYY-MM-DD interval, or explicit category:<name>; categories use the upstream recent 30-day window.",
    "medrxiv": "Explicit category:<name> only, within the upstream recent 30-day window. No DOI or arbitrary keyword lookup in this connector.",
    "ieee": "The bundled upstream connector is a placeholder; search is not implemented even with an API key.",
    "acm": "The bundled upstream connector is a placeholder; search is not implemented even with an API key.",
    "base": "The upstream OAI-PMH adapter filters harvested records; publication years are filtered after retrieval, not by harvest datestamps.",
    "google_scholar": "HTML retrieval can be blocked or rate-limited; a bounded search is not exhaustive coverage.",
    "ssrn": "HTML retrieval can be blocked or rate-limited; upstream retrieval has a limited result window.",
}


def _result(status, query, error="", papers=None, **extra):
    papers = papers or []
    return {"status": status, "count": len(papers), "error": error,
            "query_sent": query, "papers": papers, **extra}


def _reported_error(value):
    """Keep provider error codes, but never return arbitrary request URLs/keys."""
    if not value:
        return ""
    if isinstance(value, Exception):
        return safe_error(value)
    value = str(value).strip()
    http = re.search(r"\bHTTP\s+(\d{3})\b", value)
    if http:
        return "HTTP " + http[1]
    return value if re.fullmatch(r"[A-Za-z_]\w{0,63}", value) else "provider_reported_error"


def _arguments(source, query, start, end):
    kwargs = {}
    if source in {"openalex", "openalex_communication"}:
        if start or end:
            kwargs["filter"] = f"publication_year:{start or 1800}-{end or 2100}"
        if source == "openalex_communication":
            issns = load_profile()["communication_issns"]
            if not issns:
                raise ValueError("Communication ISSN list must not be empty")
            targeted = "primary_location.source.issn:" + "|".join(issns)
            kwargs["filter"] = ",".join(part for part in (kwargs.get("filter"), targeted) if part)
    elif source == "crossref" and (start or end):
        kwargs["filter"] = f"from-pub-date:{start or 1800}-01-01,until-pub-date:{end or 2100}-12-31"
    elif source in {"semantic", "citeseerx"} and (start or end):
        kwargs["year"] = f"{start or ''}-{end or ''}"
    elif source == "core":
        if start:
            kwargs["publishedAfter"] = f"{start}-01-01"
        if end:
            kwargs["publishedBefore"] = f"{end}-12-31"
    elif source == "openaire":
        if start:
            kwargs["from_date"] = f"{start}-01-01"
        if end:
            kwargs["to_date"] = f"{end}-12-31"
    elif source in {"hal", "zenodo", "doaj", "europepmc"} and start and start == end:
        kwargs["year"] = start
    if source == "iacr":
        kwargs["fetch_details"] = False
    elif source == "google_scholar":
        kwargs["timeout_seconds"] = 20.0
    elif source == "arxiv" and not any(marker in query for marker in (":", '"', " AND ", " OR ", " ANDNOT ")):
        query = " AND ".join(f"all:{word}" for word in query.split())
    return query, kwargs


def _close_network_sessions(searcher):
    """A resolver may own the only session (Unpaywall); never call file cleanup."""
    sessions = []
    for owner in (searcher, getattr(searcher, "resolver", None)):
        session = getattr(owner, "session", None)
        if session is not None and all(session is not other for other in sessions):
            sessions.append(session)
    for session in sessions:
        try:
            session.close()
        except Exception:
            # Closing an HTTP pool must not hide the actual provider result.
            pass


def search_records(source: str, query: str, limit: int, start: int | None, end: int | None) -> dict:
    """Return canonical candidates and honest diagnostics from one direct provider.

    The caller applies publication-year exclusion to every candidate after
    retrieval, including connectors lacking a native year-range parameter.
    """
    if source not in PROVIDERS:
        raise ValueError("Unknown literature source")
    if not query.strip() or limit < 1 or (start and end and start > end):
        raise ValueError("Provide a nonempty query, positive limit, and valid year range")
    note = SOURCE_NOTES.get(source, "")
    metadata = {"note": note} if note else {}
    if source in {"ieee", "acm"}:
        return _result("unsupported", query, "upstream_search_not_implemented", **metadata)
    actual_query = query.strip()
    if source == "unpaywall":
        doi = extract_doi(actual_query)
        if not doi:
            return _result("unsupported_query", query, "doi_required", **metadata)
        if not get_env("UNPAYWALL_EMAIL", "").strip():
            return _result("credentials_required", query, "UNPAYWALL_EMAIL_required", **metadata)
        actual_query = doi
    if source in {"biorxiv", "medrxiv"}:
        category = re.fullmatch(r"category\s*:\s*(.+)", actual_query, re.I)
        bio_lookup = source == "biorxiv" and (
            extract_doi(actual_query) or BioRxivSearcher.DATE_RANGE_PATTERN.fullmatch(actual_query))
        if category:
            actual_query = category[1].strip()
        elif not bio_lookup:
            return _result("unsupported_query", query, "explicit_category_or_supported_lookup_required", **metadata)
    searcher = None
    try:
        actual_query, kwargs = _arguments(source, actual_query, start, end)
        searcher = PROVIDERS[source]()
        records = searcher.search(actual_query, max_results=limit, **kwargs)
        papers = []
        for rank, record in enumerate(records, 1):
            if rank > limit:
                break
            paper = canonical(record)
            if paper["source"] == "unknown":
                paper["source"] = "openalex" if source == "openalex_communication" else source
            paper["provider_rank"] = rank
            paper["discovery_channel"] = source
            papers.append(paper)
        error = _reported_error(getattr(searcher, "last_error", ""))
        return _result("error" if error else ("ok" if papers else "empty_or_unreported_error"),
                       actual_query, error, papers, **metadata)
    except Exception as exc:
        return _result("error", actual_query, safe_error(exc), **metadata)
    finally:
        if searcher is not None:
            _close_network_sessions(searcher)
