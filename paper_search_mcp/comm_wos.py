"""WoS lane and verified PDF receipts for the simplified literature service."""
from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

from . import comm_cnki
from .comm_download import data_dir, verify_pdf
from .comm_ranking import canonical, rank_papers


async def authenticate():
    return await comm_cnki.call("wos_authenticate", {})


async def access_status():
    return await comm_cnki.call("wos_access_status", {})


async def search_many(queries, ranking_query, max_results=20, per_source=30, sources=None,
                      year_start=None, year_end=None, weights=None):
    if sources and set(sources) != {"wos"}:
        raise ValueError("English discovery now uses WoS only; omit sources or pass ['wos']")
    if not 1 <= len(queries) <= 6 or any(not isinstance(q, str) or not q.strip() for q in queries):
        raise ValueError("Supply 1..6 explicit WoS topic expressions")
    if not 1 <= max_results <= 100 or not 1 <= per_source <= 100:
        raise ValueError("limits must be 1..100")
    rank_papers([], ranking_query, weights)
    papers, searches = [], []
    for query in dict.fromkeys(queries):
        raw = await comm_cnki.call("wos_search", {"query": query, "max_results": per_source,
            "year_start": year_start, "year_end": year_end})
        papers.extend(raw.get("papers", []))
        diagnostic = {k:v for k,v in raw.items() if k != "papers"}
        if raw.get("success"):
            diagnostic.setdefault('status', 'ok' if raw.get('papers') else 'empty')
        else:
            diagnostic["status"] = "needs_attention" if raw.get("authentication_required") or raw.get("captcha") else raw.get("status", "error")
        searches.append({"query": query, "sources": {"wos": diagnostic}})
        if diagnostic.get("status") == "needs_attention":
            break
    ranked = rank_papers(papers, ranking_query, weights)
    statuses = [r["sources"]["wos"].get("status") for r in searches]
    failed = any(s not in {"ok", "empty"} for s in statuses)
    status = "partial" if failed and ranked else "needs_attention" if "needs_attention" in statuses else "unavailable" if failed else "ok" if ranked else "empty"
    return {"query": ranking_query, "status": status, "searches": searches, "papers": ranked[:max_results],
            "count": min(len(ranked), max_results), "unique_candidates": len(ranked),
            "note": "WoS Core Collection candidate sample, limited by the school subscription; ranking is heuristic, not research quality."}


async def download(paper):
    paper = canonical(paper)
    result = await comm_cnki.call("wos_download", {"paper": paper})
    receipt = {"status": "not_downloaded", "source": "wos", "paper": paper,
               "stage": result.get("status", "received_pdf" if result.get("file_path") else "error"),
               "message": result.get("message", "")}
    for key in ("authentication_required", "authentication_stage", "login_url", "captcha"):
        if key in result:
            receipt[key] = result[key]
    if result.get("file_path"):
        path = Path(result["file_path"]).resolve(strict=True)
        if not path.is_relative_to(data_dir()):
            raise ValueError("WoS response path must be inside the library")
        valid, evidence = verify_pdf(path.read_bytes(), paper)
        receipt.update(status="downloaded" if valid else "identity_unverified", identity_check=evidence, retained_path=str(path))
        if valid:
            receipt["pdf_path"] = str(path)
    path = data_dir() / ("wos_receipt_" + uuid.uuid4().hex + ".json")
    with path.open("x", encoding="utf-8") as stream:
        json.dump(receipt, stream, ensure_ascii=False, indent=2)
    return {**receipt, "receipt_path": str(path)}


def download_sync(paper, use_scihub=None):
    return asyncio.run(download(paper))
