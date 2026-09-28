"""Computational communication MCP, built on openags/paper-search-mcp."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import logging
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

from mcp.server.fastmcp import FastMCP

from .academic_platforms.arxiv import ArxivSearcher
from .academic_platforms.crossref import CrossRefSearcher
from .academic_platforms.dblp import DBLPSearcher
from .academic_platforms.openalex import OpenAlexSearcher
from .academic_platforms.semantic import SemanticSearcher
from .comm_download import data_dir, download_selected as download_one, local_file, read_pdf, safe_error
from .comm_ranking import canonical, doi_key, load_profile, rank_papers
from .config import get_env
from . import comm_auth, comm_manifest, comm_workflow
from .comm_zotero import safe_failure
from . import comm_bilingual, comm_cnki


@asynccontextmanager
async def lifespan(_):
    try:
        yield {}
    finally:
        if comm_cnki._worker:
            await asyncio.to_thread(comm_cnki._worker.close)

mcp = FastMCP("Find_comm_papers", lifespan=lifespan, instructions="Bilingual computational communication literature. Use comm_search_bilingual for joint Chinese/English discovery: generate one explicit Chinese keyword/concept per call; search additional Chinese concepts in separate calls, never combine them into one CNKI query and complementary English queries from the research question, disclosing them to the user. CNKI handles Chinese; comm_search/comm_search_many handle English. No cross-language deduplication: retain both lanes, rank within each, interleave equally placed results. English internal deduplication and Zotero duplicate checks remain. Rankings are heuristic, never research quality. Save reviewed order with comm_save_selection and import by manifest_id; status/retry retain checkpoints. CNKI search and downloads run as integrated Python modules in this same service and share its browser session; no other CNKI MCP or Python environment is required. CAJ is retained but needs conversion/identity verification before PDF ingestion. Ask before comm_zotero_authorize; import never opens a prompt automatically. Read full text before summarizing. English use_scihub=false means OA/public sources only.")
PROVIDERS = {"openalex": OpenAlexSearcher, "crossref": CrossRefSearcher, "arxiv": ArxivSearcher,
             "semantic": SemanticSearcher, "dblp": DBLPSearcher, "openalex_communication": OpenAlexSearcher}
POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="comm-search")
SLOTS = threading.BoundedSemaphore(8)


def search_source(source: str, query: str, limit: int, start: int | None, end: int | None) -> dict:
    searcher = PROVIDERS[source]()
    kwargs = {}
    if source in {"openalex", "openalex_communication"} and (start or end):
        kwargs["filter"] = f"publication_year:{start or 1800}-{end or 2100}"
    elif source == "crossref" and (start or end):
        kwargs["filter"] = f"from-pub-date:{start or 1800}-01-01,until-pub-date:{end or 2100}-12-31"
    elif source == "semantic" and (start or end):
        kwargs["year"] = f"{start or ''}:{end or ''}".replace(":", "-")
    if source == "openalex_communication":
        journal_filter = "primary_location.source.issn:" + "|".join(load_profile()["communication_issns"])
        kwargs["filter"] = ",".join(v for v in (kwargs.get("filter", ""), journal_filter) if v)
    try:
        actual_query = query
        if source == "arxiv" and not any(marker in query for marker in (":", '"', " AND ", " OR ", " ANDNOT ")):
            # Upstream quotes plain multiword queries as one exact phrase.
            # Use an explicit conjunction for cross-source keyword discovery.
            actual_query = " AND ".join(f"all:{word}" for word in query.split())
        records = searcher.search(actual_query, max_results=limit, **kwargs)
        papers = []
        for rank, record in enumerate(records, 1):
            paper = canonical(record)
            paper["provider_rank"] = rank
            paper["discovery_channel"] = source
            papers.append(paper)
        error = getattr(searcher, "last_error", "")
        return {"status": "error" if error else ("ok" if papers else "empty_or_unreported_error"),
                "count": len(papers), "error": error, "query_sent": actual_query, "papers": papers}
    finally:
        session = getattr(searcher, "session", None)
        if session is not None:
            session.close()


async def bounded_search(source, query, limit, start, end, timeout=40):
    if not SLOTS.acquire(blocking=False):
        return {"status": "busy", "count": 0, "papers": []}
    try:
        future = POOL.submit(search_source, source, query, limit, start, end)
    except BaseException:
        SLOTS.release()
        raise
    future.add_done_callback(lambda _: SLOTS.release())
    try:
        return await asyncio.wait_for(asyncio.wrap_future(future), timeout)
    except asyncio.TimeoutError:
        return {"status": "timeout", "count": 0, "papers": []}
    except Exception as exc:
        return {"status": "error", "count": 0, "error": safe_error(exc), "papers": []}


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
def comm_get_profile() -> dict:
    """Show ranking weights, venue preferences, source choices and fallback policy. No secrets."""
    return {"profile": load_profile(), "available_sources": list(PROVIDERS),
            "optional_credentials_present": {key: bool(get_env(key)) for key in ("OPENALEX_API_KEY", "SEMANTIC_SCHOLAR_API_KEY", "UNPAYWALL_EMAIL")}}


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True})
async def comm_search_bilingual(query: str, chinese_query: str | None = None,
                                english_queries: list[str] | None = None,
                                languages: list[str] | None = None, max_results: int = 20,
                                per_language: int = 30, year_start: int | None = None,
                                year_end: int | None = None, english_sources: list[str] | None = None,
                                db_code: str = "CJFD", weights: dict[str, float] | None = None) -> dict:
    """Joint CNKI Chinese and multi-source English discovery; no cross-language deduplication.

    The agent must supply one Chinese keyword/concept (e.g. 算法推荐; search 政治极化 separately) and 1..6 English
    query variants for the requested languages (default both zh/en). These are not translated
    by a hidden model/API. Rank each lane, then alternate equal ranks; raw bilingual relevance
    and citation scores are not compared. Return source diagnostics even if one side fails.
    Chinese search uses a separate persistent browser profile and existing CNKI login cookies;
    it can open a browser and retains session files, but never imports or downloads papers here.
    """
    return await comm_bilingual.search(query, chinese_query, english_queries, languages,
        max_results, per_language, year_start, year_end, english_sources, db_code, weights,
        cnki_search=comm_cnki.search, english_search=comm_search_many)


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def comm_search(query: str, max_results: int = 20, per_source: int = 30,
                      sources: list[str] | None = None, year_start: int | None = None,
                      year_end: int | None = None, weights: dict[str, float] | None = None,
                      boost_communication_recall: bool = True) -> dict:
    """Search broadly, deduplicate and rank communication first without excluding other disciplines.

    query: explicit English keywords (agent translates Chinese questions).
    sources: openalex/crossref/arxiv/semantic/dblp. Null uses profile defaults.
    weights: optional per-call overrides for relevance/discipline/recency/citations.
    boost_communication_recall: add one journal-targeted OpenAlex query alongside
    broad discovery so core communication journals are not crowded out.
    Date constraints are applied to every source after retrieval; missing dates
    are excluded when a date range is requested. Result set is a candidate sample,
    not an exhaustive systematic review. Citation counts remain source-dependent.
    """
    if not query.strip() or not 1 <= max_results <= 100 or not 1 <= per_source <= 100:
        raise ValueError("Nonempty query; max_results and per_source must be 1..100")
    if year_start and year_end and year_start > year_end:
        raise ValueError("year_start must not exceed year_end")
    profile = load_profile()
    chosen = list(dict.fromkeys(sources if sources is not None else profile["default_sources"]))
    if not chosen or set(chosen) - set(PROVIDERS):
        raise ValueError(f"Choose one or more of {list(PROVIDERS)}")
    if boost_communication_recall and "openalex" in chosen and "openalex_communication" not in chosen:
        chosen.append("openalex_communication")
    # Validate weights/query before spending API calls.
    rank_papers([], query, weights, profile)
    outputs = await asyncio.gather(*(bounded_search(s, query, per_source, year_start, year_end) for s in chosen))
    records, excluded = [], {"date": 0, "retracted": 0}
    diagnostics = {}
    retracted_dois = {doi_key(p.get("doi")) for out in outputs for p in out["papers"]
                      if p["extra"].get("is_retracted") and p.get("doi")}
    for source, output in zip(chosen, outputs):
        diagnostics[source] = {k: v for k, v in output.items() if k != "papers"}
        for paper in output["papers"]:
            if paper["extra"].get("is_retracted") or (paper.get("doi") and doi_key(paper["doi"]) in retracted_dois):
                excluded["retracted"] += 1
                continue
            match = re.match(r"^(\d{4})", paper["published_date"])
            year = int(match[1]) if match else None
            if (year_start or year_end) and (not year or (year_start and year < year_start) or (year_end and year > year_end)):
                excluded["date"] += 1
                continue
            records.append(paper)
    ranked = rank_papers(records, query, weights, profile)
    return {"query": query, "profile": profile["name"], "sources": diagnostics, "excluded": excluded,
            "candidates_before_dedup": len(records), "unique_candidates": len(ranked),
            "count": min(max_results, len(ranked)), "papers": ranked[:max_results],
            "note": "Heuristic ranking of retrieved candidates. Missing abstracts affect lexical scores. No discipline is excluded."}


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
def comm_rank(papers: list[dict], query: str, weights: dict[str, float] | None = None) -> dict:
    """Rerank/merge supplied records without a network search. Partial weight overrides accepted."""
    if len(papers) > 1000:
        raise ValueError("At most 1000 records")
    ranked = comm_bilingual.rank_mixed(papers, query, weights)
    return {"query": query, "count": len(ranked), "papers": ranked}


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def comm_search_many(queries: list[str], ranking_query: str, max_results: int = 20,
                           per_source: int = 30, sources: list[str] | None = None,
                           year_start: int | None = None, year_end: int | None = None,
                           weights: dict[str, float] | None = None) -> dict:
    """Search 1..6 explicit query variants sequentially, merge, and rank against one research question.

    Use complementary phrases such as algorithmic recommendation political polarization,
    news feed affective polarization, and recommender systems ideological segregation.
    The original query and source diagnostics are retained. Does not save a selection.
    """
    if not 1 <= len(queries) <= 6 or any(not q.strip() for q in queries):
        raise ValueError("Provide 1..6 nonempty queries")
    if not 1 <= max_results <= 100:
        raise ValueError("max_results must be 1..100")
    rank_papers([], ranking_query, weights)
    records, diagnostics = [], []
    for query in dict.fromkeys(queries):
        result = await comm_search(query, 100, per_source, sources, year_start, year_end, weights)
        for paper in result["papers"]:
            paper["extra"]["retrieval_queries"] = [query]
            for evidence in paper.get("provenance", []):
                evidence["retrieval_query"] = query
        records.extend(result["papers"])
        diagnostics.append({k: v for k, v in result.items() if k != "papers"})
    ranked = rank_papers(records, ranking_query, weights)
    return {"query": ranking_query, "searches": diagnostics, "unique_candidates": len(ranked),
            "count": min(max_results, len(ranked)), "papers": ranked[:max_results],
            "note": "Candidate retrieval, not exhaustive coverage. Review relevance and versions before selection."}


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False})
def comm_save_selection(papers: list[dict], query: str, title: str = "") -> dict:
    """Save 1..100 reviewed papers or download receipts in their supplied order, without reranking.

    Optional per-record selection_reason and zotero_item_key are retained. Same-DOI
    duplicates within one language collapse to the first entry; Chinese and English
    remain separate. Returns manifest_id for import/status/retry.
    """
    return comm_manifest.create(papers, query, title)


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
def comm_status(manifest_id: str, include_papers: bool = False) -> dict:
    """Read saved selection, bibliographic and attachment checkpoints; no network or writes."""
    return comm_manifest.report(comm_manifest.load(manifest_id), include_papers)


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
async def comm_zotero_probe() -> dict:
    """Check Zotero Desktop and whether a remembered local key exists; does not request authorization."""
    return await asyncio.to_thread(comm_auth.probe)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False})
async def comm_zotero_authorize(user_approved: bool) -> dict:
    """Open Zotero's native authorization dialog ONLY after explicit user approval.

    Explain that Always Allow grants ongoing local write access (revocable in Zotero).
    Multi-step ingestion needs a reusable key. Windows DPAPI protects the saved key;
    no secret appears in tool output. A denial is never retried automatically.
    """
    if not user_approved:
        return {"status": "authorization_required", "reason": "Ask the user before requesting Zotero authorization"}
    try:
        return await asyncio.to_thread(comm_auth.authorize)
    except Exception as exc:
        return {"status": "authorization_failed", "error": safe_failure(exc)}


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True})
async def comm_import_to_zotero(manifest_id: str, collection_keys: list[str] | None = None,
                                 tags: list[str] | None = None, download_pdfs: bool = True,
                                 limit: int = 5, transport: str = "auto",
                                 use_scihub: bool | None = None) -> dict:
    """Import up to limit (1..10) unfinished selected entries; preserve the reviewed order.

    Exact DOI matching reuses Zotero items. PDFs must pass identity validation;
    identical attachment hashes are reused. Existing bibliography is preserved.
    Target/options freeze on the first call. Local authorization must already exist.
    Always inspect metadata_verified versus attachments_verified; they are separate.
    """
    try:
        return await asyncio.to_thread(comm_workflow.execute, manifest_id, collection_keys,
                                       tags, download_pdfs, limit, transport, use_scihub)
    except Exception as exc:
        return {"status": "blocked", "manifest_id": manifest_id, "error": str(exc) if isinstance(exc, ValueError) else safe_failure(exc)}


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True})
async def comm_retry(manifest_id: str, limit: int = 5) -> dict:
    """Retry failed/interrupted entries using saved targets/options, reconciling prior writes first.

    Unattempted pending and completed entries are skipped. Use comm_import_to_zotero
    with the original options to continue the next batch of unattempted entries.
    Does not repeat successful metadata creation when only the PDF failed.
    """
    try:
        return await asyncio.to_thread(comm_workflow.execute, manifest_id, limit=limit, retry_only=True)
    except Exception as exc:
        return {"status": "blocked", "manifest_id": manifest_id, "error": str(exc) if isinstance(exc, ValueError) else safe_failure(exc)}


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True})
async def comm_download(paper: dict, use_scihub: bool | None = None) -> dict:
    """Retrieve one selected paper into the configured library with PDF identity validation.

    CNKI records use the existing Chinese access permissions and retained-response bridge.
    English records try record/publisher/OA locations, OpenAlex and optional Unpaywall, then the
    user-enabled Sci-Hub fallback. Null uses profile policy; false means OA/public
    sources only. Existing files are never overwritten or deleted.
    """
    return await asyncio.to_thread(download_one, paper, use_scihub)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True})
async def comm_download_batch(papers: list[dict], query: str, limit: int = 5,
                            weights: dict[str, float] | None = None, use_scihub: bool | None = None) -> dict:
    """Rerank selected candidates and attempt downloads in score order, with a per-paper result.

    limit counts attempted papers, not guaranteed successful downloads. Continues
    after individual failures. Retains all receipts; no files are deleted.
    """
    profile = load_profile()
    if not 1 <= limit <= profile["download"]["batch_limit"]:
        raise ValueError("limit exceeds profile batch_limit")
    ranked = comm_bilingual.rank_mixed(papers, query, weights, profile)[:limit]
    results = []
    for paper in ranked:
        try:
            result = await asyncio.to_thread(download_one, paper, use_scihub)
        except Exception as exc:
            result = {"status": "not_downloaded", "paper": paper, "error": safe_error(exc)}
        results.append(result)
    report = {"query": query, "attempted": len(results),
              "downloaded": sum(r["status"] == "downloaded" for r in results), "results": results}
    path = data_dir() / f"batch_{uuid.uuid4().hex}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["report_path"] = str(path)
    return report


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
def comm_read_pdf(path: str, start_page: int = 1, page_count: int = 3) -> dict:
    """Read bounded PDF pages from this MCP's library, retaining PDF page numbers."""
    return read_pdf(path, start_page, page_count)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False})
def comm_export_ris(papers: list[dict]) -> dict:
    """Export selected records to a new UTF-8 RIS file for Zotero, including known local PDFs.

    Accepts paper records or successful comm_download receipts. This creates a
    file only; it does not modify the Zotero library.
    """
    if not 1 <= len(papers) <= 1000:
        raise ValueError("Provide 1..1000 records")
    lines = []
    def field(tag, value):
        if value:
            lines.append(f"{tag}  - {str(value).replace(chr(10), ' ').replace(chr(13), ' ')}")
    for record in papers:
        paper = canonical(record.get("paper", record))
        kind = str(paper["extra"].get("crossref_type") or paper["extra"].get("work_type") or "")
        field("TY", "THES" if "thesis" in kind.casefold() else "CPAPER" if "proceeding" in kind.casefold() or "conference" in kind.casefold() else "JOUR")
        field("TI", paper["title"])
        for author in paper["authors"]:
            field("AU", author)
        field("JO", paper["venue"])
        field("PY", paper["published_date"][:4])
        field("DA", paper["published_date"][:10])
        field("DO", paper["doi"])
        field("UR", paper.get("url"))
        field("AB", paper["abstract"])
        field("LA", paper["language"])
        for tag, key in (("VL", "volume"), ("IS", "issue"), ("SP", "page")):
            field(tag, paper["extra"].get(key))
        for keyword in paper["keywords"]:
            field("KW", keyword)
        pdf = record.get("pdf_path") or paper.get("pdf_path")
        if pdf:
            field("L1", local_file(pdf).as_uri())
        field("N1", "Retrieved with comm-paper-mcp; bibliographic metadata retains source provenance")
        lines.extend(["ER  -", ""])
    path = data_dir() / f"references_{uuid.uuid4().hex}.ris"
    path.write_text("\n".join(lines), encoding="utf-8")
    return {"path": str(path), "count": len(papers), "status": "exported", "zotero_imported": False}


def main():
    logging.basicConfig(level=logging.WARNING)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
