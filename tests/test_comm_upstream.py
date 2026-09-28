import asyncio
import inspect
import io
import json
import threading
from pathlib import Path

import pytest
from mcp.server.fastmcp import FastMCP
from pypdf import PdfWriter

from paper_search_mcp import comm_upstream as compat
from paper_search_mcp import comm_download
from paper_search_mcp.academic_platforms.retained import retained_open, retained_scope, redact_diagnostic


def pdf():
    writer = PdfWriter()
    writer.add_blank_page(100, 100)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def test_all_upstream_names_and_original_schemas():
    mcp = FastMCP("audit")
    compat.register_upstream_tools(mcp)
    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
    originals = {tool.name: tool for tool in asyncio.run(compat.upstream.mcp.list_tools())}
    assert len(tools) == 63 == len(set(compat.TOOL_NAMES))
    assert set(originals).issubset(tools)
    for name, tool in originals.items():
        assert tools[name].inputSchema == tool.inputSchema
    assert "not implemented" in tools["search_ieee"].description
    assert tools["read_arxiv_paper"].annotations.readOnlyHint is False


@pytest.mark.parametrize("source", ["ieee", "acm"])
def test_optional_placeholders_do_not_claim_working_with_key(monkeypatch, source):
    monkeypatch.setenv("PAPER_SEARCH_MCP_" + source.upper() + "_API_KEY", "test-key")
    with pytest.raises(NotImplementedError, match="not implemented"):
        compat._invoke(compat._optional_search, "search_" + source, {"query": "test", "max_results": 1})


def test_native_repeat_download_keeps_both_files(tmp_path):
    async def native(paper_id: str, save_path: str = "./downloads") -> str:
        path = Path(save_path) / "same.pdf"
        with retained_open(path, "wb") as stream:
            stream.write(pdf())
        return str(path)
    args = {"paper_id": "123", "save_path": str(tmp_path)}
    first = compat._invoke(native, "download_arxiv", dict(args))
    second = compat._invoke(native, "download_arxiv", dict(args))
    assert first != second
    assert Path(first).read_bytes() == Path(second).read_bytes() == pdf()
    assert len(list(tmp_path.glob("*/result.json"))) == 2


def test_partial_write_is_retained_on_error(tmp_path):
    async def native(paper_id: str, save_path: str = "./downloads") -> str:
        with retained_open(Path(save_path) / "partial.pdf", "wb") as stream:
            stream.write(b"partial")
        raise RuntimeError("failure")
    with pytest.raises(RuntimeError, match="artifacts retained"):
        compat._invoke(native, "download_arxiv", {"paper_id": "1", "save_path": str(tmp_path)})
    assert next(tmp_path.glob("*/partial.pdf")).read_bytes() == b"partial"


def test_bad_pdf_is_retained_but_not_success(tmp_path):
    async def native(paper_id: str, save_path: str = "./downloads") -> str:
        path = Path(save_path) / "html.pdf"
        with retained_open(path, "wb") as stream:
            stream.write(b"<html>no pdf</html>")
        return str(path)
    result = compat._invoke(native, "download_arxiv", {"paper_id": "1", "save_path": str(tmp_path)})
    assert "did not produce" in result
    assert next(tmp_path.glob("*/html.pdf")).exists()


def test_write_scope_and_existing_files_are_protected(tmp_path):
    root = tmp_path / "scope"
    root.mkdir()
    path = root / "kept.pdf"
    path.write_bytes(b"keep")
    with retained_scope(root):
        with pytest.raises(FileExistsError):
            retained_open(path, "wb")
        with pytest.raises(ValueError, match="escapes"):
            retained_open(tmp_path / "outside.pdf", "wb")
    assert path.read_bytes() == b"keep"


def test_write_scope_survives_upstream_executor_and_async_thread(tmp_path):
    scope = tmp_path / "scope"
    scope.mkdir()
    outside = tmp_path / "outside.pdf"
    def write_outside():
        with retained_open(outside, "wb") as stream:
            stream.write(b"bad")
    async def exercise_async_thread():
        with pytest.raises(ValueError, match="escapes"):
            await asyncio.to_thread(write_outside)
    with retained_scope(scope):
        with pytest.raises(ValueError, match="escapes"):
            compat.upstream._SEARCH_EXECUTOR.submit(write_outside).result(timeout=5)
        asyncio.run(exercise_async_thread())
    assert not outside.exists()


@pytest.mark.parametrize("identifier", ["../paper", "%2e%2e/paper", "C:\\private", "paper\x00"])
def test_unsafe_identifiers_rejected_before_native_call(tmp_path, identifier):
    async def never(**kwargs):
        raise AssertionError("must not run")
    with pytest.raises(ValueError):
        compat._invoke(never, "download_arxiv", {"paper_id": identifier, "save_path": str(tmp_path)})
    assert not list(tmp_path.iterdir())


def test_read_existing_local_file_does_not_download(tmp_path):
    (tmp_path / "123.pdf").write_bytes(pdf())
    async def never(**kwargs):
        raise AssertionError("must not download")
    assert compat._invoke(never, "read_arxiv_paper", {"paper_id": "123", "save_path": str(tmp_path)}) == ""
    assert len(list(tmp_path.iterdir())) == 1


def test_rejected_fallback_pdf_stays_on_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(comm_download, "fetch_bytes", lambda *args: (pdf(), "https://example.org/p.pdf", "application/pdf"))
    monkeypatch.setattr(compat.upstream, "_pdf_matches_expected", lambda *args: False)
    assert asyncio.run(compat.upstream._download_from_url("https://example.org/p.pdf", str(tmp_path), expected_title="wrong")) is None
    assert len(list(tmp_path.glob("*.pdf"))) == 1


def test_rejected_scihub_file_stays_on_disk(tmp_path, monkeypatch):
    path = tmp_path / "rejected.pdf"
    path.write_bytes(pdf())
    async def no_repository(*args, **kwargs):
        return None, "no candidate"
    class Fetcher:
        def __init__(self, **kwargs):
            pass
        def download_pdf(self, identifier):
            return str(path)
    monkeypatch.setattr(compat.upstream, "_try_repository_fallback", no_repository)
    monkeypatch.setattr(compat.upstream, "SciHubFetcher", Fetcher)
    monkeypatch.setattr(compat.upstream, "_pdf_matches_expected", lambda *args: False)
    result = asyncio.run(compat.upstream.download_with_fallback(
        "not-a-provider", "identifier", title="wrong title", save_path=str(tmp_path), use_scihub=True))
    assert "did not match" in result
    assert path.read_bytes() == pdf()


def test_worker_does_not_run_blocking_body_on_event_loop():
    main_thread = threading.get_ident()
    async def native(query: str, max_results: int = 10) -> list[dict]:
        return [{"thread": threading.get_ident()}]
    wrapped = compat._wrapper(native, "search_arxiv")
    assert inspect.signature(wrapped) == inspect.signature(native)
    assert asyncio.run(wrapped("test"))[0]["thread"] != main_thread


def test_log_diagnostics_redact_credentials():
    redacted = redact_diagnostic("failed https://user:pass@example.org/path?api_key=SECRET Authorization: SECRET2")
    assert "SECRET" not in redacted and "user:pass" not in redacted
