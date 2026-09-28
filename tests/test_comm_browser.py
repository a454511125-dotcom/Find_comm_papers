"""Offline acceptance of headless fallback boundaries; no real browser or network."""
import asyncio
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from paper_search_mcp import comm_browser as browser
from paper_search_mcp import comm_download


class Response:
    def __init__(self, url, body=b"<html></html>", status=200, **headers):
        self.url, self.payload, self.status = url, body, status
        self.headers = headers or {"content-type": "text/html"}
        self.body_reads = 0

    async def body(self):
        self.body_reads += 1
        return self.payload


class Route:
    def __init__(self, url, responses, resource_type="document"):
        self.request = SimpleNamespace(url=url, method="GET", resource_type=resource_type)
        self.responses, self.fetches, self.aborted, self.fulfilled = responses, [], False, None

    async def fetch(self, **kwargs):
        self.fetches.append(kwargs)
        return self.responses[kwargs["url"]]

    async def abort(self):
        self.aborted = True

    async def fulfill(self, **kwargs):
        self.fulfilled = kwargs


@pytest.fixture
def public_only(monkeypatch):
    checked = []

    def validate(url):
        checked.append(url)
        if not url.startswith("https://public.example/"):
            raise ValueError("private or unsupported URL")
        return url

    monkeypatch.setattr(comm_download, "validate_public_url", validate)
    return checked


def capture(max_bytes=1000):
    return browser._Capture(time.monotonic() + 10, max_bytes)


def test_all_redirect_hops_are_checked_before_request(public_only):
    start = "https://public.example/start"
    private = "http://127.0.0.1/private"
    route = Route(start, {start: Response(start, status=302, location=private)})
    result = capture()
    asyncio.run(result.route(route))
    assert len(route.fetches) == 1
    assert route.fetches[0]["max_redirects"] == 0
    assert private in public_only
    assert result.reason == "blocked_url" and route.aborted


def test_subresources_cannot_request_private_network(public_only):
    route = Route("http://192.168.1.1/secret", {}, resource_type="xhr")
    result = capture()
    asyncio.run(result.route(route))
    assert route.fetches == [] and route.aborted


def test_network_request_budget_includes_redirect_fetches(public_only):
    start, final = "https://public.example/start", "https://public.example/final"
    route = Route(start, {start: Response(start, status=302, location=final)})
    result = capture()
    result.request_count = browser._MAX_REQUESTS - 1
    asyncio.run(result.route(route))
    assert len(route.fetches) == 1 and route.aborted
    assert result.request_count == browser._MAX_REQUESTS
    assert result.reason == "request_limit"


def test_redirected_pdf_is_retained_as_bytes_without_native_download(public_only):
    start, final = "https://public.example/start", "https://public.example/paper.pdf"
    pdf = b"%PDF-1.7\nmock"
    route = Route(start, {
        start: Response(start, status=302, location=final),
        final: Response(final, body=pdf, **{"content-type": "application/pdf"}),
    })
    result = capture()
    asyncio.run(result.route(route))
    assert [request["url"] for request in route.fetches] == [start, final]
    assert all(request["max_redirects"] == 0 for request in route.fetches)
    assert result.content == pdf and result.final_url == final
    assert route.aborted and not route.fulfilled


@pytest.mark.parametrize("declared,body_read", [(True, 0), (False, 1)])
def test_size_limit_checked_before_and_after_body(public_only, declared, body_read):
    url = "https://public.example/paper.pdf"
    headers = {"content-type": "application/pdf"}
    if declared:
        headers["content-length"] = "100"
    response = Response(url, body=b"%PDF-" + b"x" * 95, **headers)
    route = Route(url, {url: response})
    result = capture(max_bytes=50)
    asyncio.run(result.route(route))
    assert result.reason == "size_limit" and result.content is None
    assert response.body_reads == body_read


def test_scihub_cannot_become_browser_oa(public_only):
    with pytest.raises(ValueError):
        browser._validate("https://sci-hub.st/paper")
    assert public_only == []


def test_explicit_binary_headless_and_retained_artifacts(monkeypatch, tmp_path):
    binary = tmp_path / "browser.exe"
    binary.write_bytes(b"mock")
    observed = {}
    sentinel = object()

    async def launch(profile, **kwargs):
        observed.update(kwargs, profile=profile, binary=os.environ["CLOAKBROWSER_BINARY_PATH"],
                        updates=os.environ["CLOAKBROWSER_AUTO_UPDATE"])
        return sentinel

    monkeypatch.setitem(sys.modules, "cloakbrowser", SimpleNamespace(launch_persistent_context_async=launch))
    from paper_search_mcp import comm_cnki_host
    monkeypatch.setattr(comm_cnki_host, "retain_license_signals", lambda path: None)
    monkeypatch.setenv("CLOAKBROWSER_BINARY_PATH", "existing-setting")
    monkeypatch.setenv("OPENALEX_API_KEY", "must-not-forward")
    result = asyncio.run(browser._launch(tmp_path / "session", binary, time.monotonic() + 10))
    assert result is sentinel
    assert observed["headless"] is True and observed["accept_downloads"] is False
    assert observed["service_workers"] == "block"
    assert observed["binary"] == str(binary) and observed["updates"] == "false"
    assert "OPENALEX_API_KEY" not in observed["env"]
    assert Path(observed["profile"]).is_dir() and Path(observed["artifacts_dir"]).is_dir()
    assert os.environ["CLOAKBROWSER_BINARY_PATH"] == "existing-setting"


class Locator:
    async def count(self):
        return 0

    async def all(self):
        return []


class Page:
    def __init__(self, context, title="Article", links=(), delay=0):
        self.context, self.page_title, self.links, self.delay = context, title, list(links), delay
        self.url = "about:blank"

    async def goto(self, url, **kwargs):
        self.url = url
        if self.delay:
            await asyncio.sleep(self.delay)
        route = Route(url, self.context.responses)
        await self.context.handler(route)
        if route.aborted:
            raise RuntimeError("aborted")

    async def title(self):
        return self.page_title

    async def wait_for_timeout(self, value):
        pass

    def locator(self, selector):
        return Locator()

    async def evaluate(self, script):
        return self.links

    async def content(self):
        return "<html><title>Failure evidence</title></html>"

    async def screenshot(self, path, **kwargs):
        Path(path).write_bytes(b"mock image")

    def is_closed(self):
        return False


class Context:
    def __init__(self, responses, **page_options):
        self.responses, self.closed = responses, False
        self.page = Page(self, **page_options)

    async def route(self, pattern, handler):
        assert pattern == "**/*"
        self.handler = handler

    async def new_page(self):
        return self.page

    async def close(self):
        self.closed = True


def mock_context(monkeypatch, context):
    async def launch(*args):
        return context
    monkeypatch.setattr(browser, "_launch", launch)


def test_metadata_pdf_retrieval_keeps_parent_verification_responsibility(public_only, monkeypatch, tmp_path):
    start, pdf = "https://public.example/article", "https://public.example/paper.pdf"
    payload = b"%PDF-not-yet-verified"
    context = Context({start: Response(start), pdf: Response(pdf, body=payload, **{"content-type": "application/pdf"})}, links=["paper.pdf"])
    mock_context(monkeypatch, context)
    result = asyncio.run(browser._fetch(start, tmp_path / "browser", tmp_path, 2, 1000))
    assert result["status"] == "downloaded" and result["content"] == payload
    assert result["final_url"] == pdf and context.closed
    assert "identity_verified" not in result
    assert not (tmp_path / "failure.html").exists()


def test_redirected_html_renders_at_final_origin(public_only, monkeypatch, tmp_path):
    start, final = "https://public.example/doi", "https://public.example/journal/article"
    pdf = "https://public.example/journal/paper.pdf"
    context = Context({
        start: Response(start, status=302, location=final), final: Response(final),
        pdf: Response(pdf, body=b"%PDF-mock", **{"content-type": "application/pdf"}),
    }, links=["paper.pdf"])
    visited = []
    original = context.page.goto

    async def navigate(url, **kwargs):
        visited.append(url)
        await original(url, **kwargs)

    context.page.goto = navigate
    mock_context(monkeypatch, context)
    result = asyncio.run(browser._fetch(start, tmp_path / "browser", tmp_path, 2, 1000))
    assert result["status"] == "downloaded"
    assert visited == [start, final, pdf]


@pytest.mark.parametrize("title", ["Verify you are human", "Making sure you're not a bot!"])
def test_captcha_returns_manual_and_keeps_failure_artifacts(public_only, monkeypatch, tmp_path, title):
    start = "https://public.example/article"
    context = Context({start: Response(start)}, title=title)
    mock_context(monkeypatch, context)
    result = asyncio.run(browser._fetch(start, tmp_path / "browser", tmp_path, 2, 1000))
    assert result["status"] == "needs_manual" and result["reason"] == "verification_required"
    assert Path(result["diagnostic_snapshot"]).exists() and Path(result["screenshot"]).exists()
    assert context.closed


def test_timeout_closes_context_and_keeps_evidence(public_only, monkeypatch, tmp_path):
    start = "https://public.example/article"
    context = Context({start: Response(start)}, delay=1)
    mock_context(monkeypatch, context)
    result = asyncio.run(browser._fetch(start, tmp_path / "browser", tmp_path, .02, 1000))
    assert result["status"] == "failed" and result["reason"] == "timeout"
    assert context.closed and Path(result["diagnostic_snapshot"]).exists()


def test_missing_binary_never_imports_or_launches_browser(public_only, monkeypatch, tmp_path):
    monkeypatch.setenv("COMM_BROWSER_PATH", str(tmp_path / "missing.exe"))
    monkeypatch.setattr(browser, "_launch", lambda *args: pytest.fail("must not launch"))
    result = browser.fetch_browser_pdf("https://public.example/article", {}, timeout=2, max_bytes=1000)
    assert result == {"status": "failed", "reason": "browser_not_configured"}


def test_concurrency_is_bounded_without_queue(public_only, monkeypatch, tmp_path):
    binary = tmp_path / "browser.exe"
    binary.write_bytes(b"mock")
    monkeypatch.setenv("COMM_BROWSER_PATH", str(binary))
    browser._BROWSER_SLOT.acquire()
    try:
        result = browser.fetch_browser_pdf("https://public.example/article", {}, timeout=2, max_bytes=1000)
    finally:
        browser._BROWSER_SLOT.release()
    assert result == {"status": "failed", "reason": "browser_busy"}
