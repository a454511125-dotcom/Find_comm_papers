"""Mount upstream tools in one server with retained files and bounded execution.

Upstream IEEE/ACM connectors are placeholders, including when credentials exist.
The original tool signatures remain available, without starting its MCP server.
"""
from __future__ import annotations

import asyncio
import functools
import inspect
import json
import logging
import re
import threading
import uuid
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import unquote

from pypdf import PdfReader

from . import server as upstream
from .academic_platforms.retained import retained_scope, safe_filename, SafeDiagnosticFilter, redact_diagnostic
from .comm_download import data_dir, safe_error

_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="upstream-tools")
_SLOTS = threading.BoundedSemaphore(8)
_SEARCH_SOURCES = (
    "arxiv pubmed biorxiv medrxiv google_scholar iacr semantic crossref openalex "
    "pmc core europepmc dblp openaire citeseerx doaj base zenodo hal ssrn unpaywall"
).split()
_FILE_SOURCES = (
    "arxiv pubmed biorxiv medrxiv iacr semantic crossref dblp openaire "
    "citeseerx doaj base zenodo hal ssrn openalex"
).split()
TOOL_NAMES = (
    ["search_papers", "get_crossref_paper_by_doi", "download_scihub", "download_with_fallback"]
    + ["search_" + source for source in _SEARCH_SOURCES]
    + ["download_" + source for source in _FILE_SOURCES]
    + ["read_" + source + "_paper" for source in _FILE_SOURCES]
    + [prefix + source + suffix for source in ("ieee", "acm")
       for prefix, suffix in (("search_", ""), ("download_", ""), ("read_", "_paper"))]
)


def _optional_search(query: str, max_results: int = 10) -> list[dict]:
    """Compatibility entry point; the upstream provider is not implemented."""


def _optional_file(paper_id: str, save_path: str = "./downloads") -> str:
    """Compatibility entry point; the upstream provider is not implemented."""


def _root(save_path):
    return (data_dir() / "upstream" if save_path in ("", "./downloads")
            else Path(save_path).expanduser().resolve())


def _validate_identifier(identifier):
    value = unquote(str(identifier))
    if not value.strip() or any(ord(c) < 32 for c in value) or "\\" in value:
        raise ValueError("Invalid paper identifier")
    if ".." in value.split("/") or re.match(r"^[A-Za-z]:[/\\]", value):
        raise ValueError("Invalid paper identifier")


def _read_local(root, paper_id, source):
    """Explicit files, legacy names, and previous successful retained receipts."""
    candidates = []
    supplied = Path(paper_id).expanduser()
    if supplied.suffix.lower() == ".pdf":
        candidates.append(supplied if supplied.is_absolute() else root / supplied)
    stem = safe_filename(paper_id)
    candidates.extend([root / (stem + ".pdf"), root / (source + "_" + stem + ".pdf")])
    if root.is_dir():
        for receipt_path in sorted(root.glob("*/result.json"), reverse=True):
            try:
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                if receipt.get("paper_id") == paper_id and receipt.get("source") == source and receipt.get("pdf_path"):
                    candidates.append(Path(receipt["pdf_path"]))
            except (OSError, ValueError):
                continue
    for path in candidates:
        path = path.resolve()
        if path.is_relative_to(root.resolve()) and path.is_file():
            return "\n\n".join((page.extract_text() or "") for page in PdfReader(str(path)).pages)
    return None


def _invoke(fn, name, arguments):
    if name.endswith(("ieee", "acm", "ieee_paper", "acm_paper")):
        # Clear MCP error rather than silently returning an empty result set.
        raise NotImplementedError("Upstream IEEE/ACM connector is not implemented; configuring a key does not enable it")
    if not (name.startswith("download_") or name.startswith("read_")):
        try:
            result = asyncio.run(fn(**arguments))
            if isinstance(result, dict) and isinstance(result.get("errors"), dict):
                result["errors"] = {key: redact_diagnostic(value) for key, value in result["errors"].items()}
            return result
        except Exception as exc:
            raise RuntimeError(safe_error(exc)) from None

    root = _root(arguments.get("save_path", "./downloads"))
    identifier = arguments.get("paper_id", arguments.get("identifier", ""))
    source = arguments.get("source") or name.removeprefix("download_").removeprefix("read_").removesuffix("_paper")
    if name.startswith("read_"):
        local = _read_local(root, identifier, source)
        if local is not None:
            return local
    _validate_identifier(identifier)
    run_dir = root / uuid.uuid4().hex
    run_dir.mkdir(parents=True, exist_ok=False)
    arguments["save_path"] = str(run_dir)
    receipt = {"tool": name, "source": source, "paper_id": identifier, "status": "started"}
    try:
        with retained_scope(run_dir):
            result = asyncio.run(fn(**arguments))
        files = [str(path.resolve()) for path in run_dir.rglob("*") if path.is_file()]
        receipt["files"] = files
        if name.startswith("download_"):
            try:
                output = Path(result).resolve() if isinstance(result, str) else None
                is_file = bool(output and output.is_relative_to(run_dir) and output.is_file())
            except (OSError, ValueError):
                is_file = False
            if is_file:
                try:
                    if not PdfReader(str(output)).pages:
                        raise ValueError("Empty PDF")
                except Exception:
                    receipt["status"] = "invalid_pdf_retained"
                    return "Download did not produce a readable PDF; received files retained in " + str(run_dir)
                receipt.update(status="downloaded", pdf_path=str(output), identity_check="not_performed_by_native_tool")
                return str(output)
            receipt["status"] = "not_downloaded"
        else:
            receipt["status"] = "read_returned"
        if isinstance(result, str) and re.match(r"(?i)^(error|failed|download failed)", result):
            return redact_diagnostic(result)
        return result
    except Exception as exc:
        receipt.update(status="error", error=safe_error(exc))
        if isinstance(exc, NotImplementedError):
            return "Source-native operation is not implemented; use comm_download with paper metadata and an OA URL/DOI."
        raise RuntimeError(safe_error(exc) + "; artifacts retained in " + str(run_dir)) from None
    finally:
        with (run_dir / "result.json").open("x", encoding="utf-8") as stream:
            json.dump(receipt, stream, ensure_ascii=False, indent=2)


def _wrapper(fn, name):
    signature = inspect.signature(fn)

    @functools.wraps(fn)
    async def wrapped(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        if not _SLOTS.acquire(blocking=False):
            raise RuntimeError("Upstream tool capacity is busy; retry after running calls finish")
        try:
            future = _POOL.submit(_invoke, fn, name, dict(bound.arguments))
        except BaseException:
            _SLOTS.release()
            raise
        future.add_done_callback(lambda _: _SLOTS.release())
        return await asyncio.wait_for(asyncio.wrap_future(future), timeout=180)

    wrapped.__name__ = name
    wrapped.__signature__ = signature
    return wrapped


def register_upstream_tools(mcp):
    """Register 57 original tools and six explicit unavailable compatibility tools."""
    for module_name in tuple(sys.modules):
        if module_name == upstream.__name__ or module_name.startswith("paper_search_mcp.academic_platforms."):
            logger = logging.getLogger(module_name)
            if not any(isinstance(item, SafeDiagnosticFilter) for item in logger.filters):
                logger.addFilter(SafeDiagnosticFilter())
    for name in TOOL_NAMES:
        fn = getattr(upstream, name, None)
        if fn is None:
            fn = _optional_search if name.startswith("search_") else _optional_file
        annotation = {"readOnlyHint": not (name.startswith("download_") or name.startswith("read_")),
                      "destructiveHint": False, "openWorldHint": True}
        description = fn.__doc__ or name
        if "ieee" in name or "acm" in name:
            description = "Unavailable: upstream connector is not implemented, even with an API key. " + description
        elif name.startswith("download_"):
            description += " Files are retained in unique directories. Native PDF readability is checked; use comm_download for DOI/title identity verification."
        mcp.add_tool(_wrapper(fn, name), name=name, description=description, annotations=annotation)
