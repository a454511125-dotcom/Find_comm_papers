"""The headless fallback must pass the same identity and file-preservation checks."""
from pathlib import Path
from unittest.mock import Mock

import pytest

from paper_search_mcp import comm_browser as browser, comm_download as dl
from tests.test_comm_workflow import pdf_bytes


@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.setenv("COMM_MCP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMM_BROWSER_FALLBACK", "1")
    monkeypatch.setattr(dl, "fetch_bytes", Mock(return_value=(b"<html>JS required</html>", "https://example.org/paper", "text/html")))
    monkeypatch.setattr(dl, "fetch_json", Mock(return_value={}))
    monkeypatch.setattr(dl, "get_env", Mock(return_value=""))
    return {"title": "Algorithmic recommendation political polarization", "url": "https://example.org/paper"}


def test_browser_pdf_verified_saved_and_never_overwritten(setup, monkeypatch):
    content = pdf_bytes(setup["title"])
    call = Mock(return_value={"status": "downloaded", "content": content, "final_url": "https://example.org/file.pdf"})
    monkeypatch.setattr(browser, "fetch_browser_pdf", call)
    first = dl.download_one(setup, False)
    second = dl.download_one(setup, False)
    assert first["status"] == second["status"] == "downloaded"
    assert first["source"] == "headless_browser"
    assert first["pdf_path"] != second["pdf_path"]
    assert Path(first["pdf_path"]).read_bytes() == content
    assert Path(second["pdf_path"]).read_bytes() == content
    assert call.call_args.kwargs["timeout"] <= 30


def test_wrong_browser_pdf_is_not_importable(setup, monkeypatch):
    monkeypatch.setattr(browser, "fetch_browser_pdf", Mock(return_value={"status": "downloaded", "content": pdf_bytes("Unrelated geological record"), "final_url": "https://example.org/wrong.pdf"}))
    result = dl.download_one(setup, False)
    assert result["status"] == "not_downloaded"
    assert any(a["status"] == "identity_unverified" for a in result["attempts"])
    assert not list(dl.data_dir().glob("*.pdf"))


def test_manual_verification_returns_diagnostic(setup, monkeypatch):
    monkeypatch.setattr(browser, "fetch_browser_pdf", Mock(return_value={"status": "needs_manual", "reason": "verification_required", "screenshot": "retained.png"}))
    result = dl.download_one(setup, False)
    assert result["status"] == "not_downloaded"
    assert result["attempts"][-1]["screenshot"] == "retained.png"
    assert result["attempts"][-1]["status"] == "needs_manual"


def test_explicit_browser_disable(setup, monkeypatch):
    monkeypatch.setenv("COMM_BROWSER_FALLBACK", "0")
    call = Mock(side_effect=AssertionError("browser disabled"))
    monkeypatch.setattr(browser, "fetch_browser_pdf", call)
    assert dl.download_one(setup, False)["status"] == "not_downloaded"
    call.assert_not_called()


def test_direct_pdf_skips_browser(setup, monkeypatch):
    monkeypatch.setattr(dl, "fetch_bytes", Mock(return_value=(pdf_bytes(setup["title"]), setup["url"], "application/pdf")))
    call = Mock(side_effect=AssertionError("unnecessary browser"))
    monkeypatch.setattr(browser, "fetch_browser_pdf", call)
    assert dl.download_one(setup, False)["status"] == "downloaded"
    call.assert_not_called()


def test_credentials_never_passed_to_browser(setup, monkeypatch):
    setup["url"] = ""
    monkeypatch.setattr(dl, "oa_candidates", Mock(return_value=[{"url": "https://example.org/api.pdf", "source": "openalex_content", "headers": {"Authorization": "Bearer TEST"}}]))
    call = Mock(side_effect=AssertionError("private API candidate"))
    monkeypatch.setattr(browser, "fetch_browser_pdf", call)
    assert dl.download_one(setup, False)["status"] == "not_downloaded"
    call.assert_not_called()
