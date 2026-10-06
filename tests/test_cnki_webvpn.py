"""Offline WebVPN regressions; use tests.retained_tmp, never pytest tmpdir."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from paper_search_mcp.cnki.webvpn import (
    BfsuWebVPN, LOGIN_URL, ORIGIN, canonical_url, proxy_url, resolve_cnki_url,
)
from paper_search_mcp.cnki.browser import BrowserSession
from paper_search_mcp.comm_cnki_host import CNKIBackend, RetainedResponses


@pytest.mark.parametrize("host,token", [
    ("www.cnki.net", "77726476706e69737468656265737421e7e056d2243e635930068cb8"),
    ("kns.cnki.net", "77726476706e69737468656265737421fbf952d2243e635930068cb8"),
])
def test_codec_matches_live_bfsu_links(host, token):
    original = "https://" + host + "/starter/advanced?x=1&y=%E4%B8%AD#tab"
    expected = ORIGIN + "/https/" + token + "/starter/advanced?x=1&y=%E4%B8%AD#tab"
    assert proxy_url(original) == expected
    assert canonical_url(expected) == original
    assert proxy_url(expected) == expected


@pytest.mark.parametrize("url", [
    "https://example.com/", "https://cnki.net.evil.example/", "https://evilcnki.net/",
    "https://webvpn.bfsu.edu.cn/login", "https://webvpn.bfsu.edu.cn/https/abcdef/",
    "https://user:pass@kns.cnki.net/", "https://kns.cnki.net:9443/",
    "javascript:alert(1)", "https://kns.cnki.net\\@example.com/",
    "https://webvpn.bfsu.edu.cn.evil.example/https/abc/",
])
def test_unrelated_and_malformed_urls_rejected(url):
    with pytest.raises(ValueError):
        canonical_url(url)


def test_gateway_is_not_a_blanket_allowlist():
    from paper_search_mcp.cnki.webvpn import PUBLIC_ADDRESS_KEY, _cipher
    token = PUBLIC_ADDRESS_KEY.hex() + _cipher().encryptor().update(b"my.bfsu.edu.cn").hex()
    with pytest.raises(ValueError):
        canonical_url(ORIGIN + "/https/" + token + "/")


@pytest.mark.parametrize("href,expected", [
    ("/kcms/detail?id=1", "https://kns.cnki.net/kcms/detail?id=1"),
    ("detail?id=1", "https://kns.cnki.net/starter/detail?id=1"),
    ("//kns.cnki.net/kcms/detail?id=1", "https://kns.cnki.net/kcms/detail?id=1"),
    ("https://kns.cnki.net/kcms/detail?id=1", "https://kns.cnki.net/kcms/detail?id=1"),
])
def test_relative_results_keep_upstream_host(href, expected):
    assert resolve_cnki_url(href, proxy_url("https://kns.cnki.net/starter/search")) == expected


def test_already_rewritten_relative_result():
    original = "https://kns.cnki.net/kcms/detail?id=2"
    assert resolve_cnki_url(proxy_url(original)[len(ORIGIN):], proxy_url("https://www.cnki.net/")) == original


class FakePage:
    def __init__(self, context, url, institution=False):
        self.context, self.url, self.institution = context, url, institution
        self.gotos = []
        self.fronts = 0
        self.next_url = None

    def is_closed(self):
        return False

    async def goto(self, url, **kwargs):
        self.gotos.append(url)
        self.url = ((ORIGIN + "/" if self.context.authenticated else LOGIN_URL)
                    if url == ORIGIN + "/" else self.next_url or url)

    async def bring_to_front(self):
        self.fronts += 1

    async def wait_for_timeout(self, _):
        pass

    async def wait_for_load_state(self, *args, **kwargs):
        pass

    def locator(self, selector):
        assert selector in {"#ecpHeaderContainer", "input[type='password']"}
        return SimpleNamespace(count=AsyncMock(return_value=0 if selector == "input[type='password']" else 1),
                               is_visible=AsyncMock(return_value=True),
                               inner_text=AsyncMock(return_value="北京外国语大学图书馆" if self.institution else "个人登录"))

    def get_by_role(self, role):
        page = self
        class Card:
            def filter(self, **kwargs):
                return self
            @property
            def first(self):
                return self
            async def count(self):
                return 1
            async def is_visible(self):
                return True
            async def click(self, **kwargs):
                page.context.pages.append(FakePage(page.context, proxy_url("https://www.cnki.net/"), True))
        return Card()


class FakeContext:
    def __init__(self, authenticated=False):
        self.pages = []
        self.authenticated = authenticated

    async def new_page(self):
        page = FakePage(self, "about:blank")
        self.pages.append(page)
        return page


def test_manual_login_resumes_and_clicks_cnki_popup():
    async def scenario():
        ctx, vpn = FakeContext(), BfsuWebVPN()
        first = await vpn.ensure(ctx)
        assert first["authentication_required"]
        assert vpn.page.gotos == [ORIGIN + "/"]
        second = await vpn.ensure(ctx)
        assert second["authentication_required"]
        assert vpn.page.gotos == [ORIGIN + "/"]  # no reloading an in-progress form
        assert vpn.page.fronts == 1
        ctx.authenticated = True
        vpn.page.url = ORIGIN + "/"
        done = await vpn.ensure(ctx)
        assert done["authenticated"] and vpn.ready
        assert len(ctx.pages) == 3  # preserved login, reusable probe, CNKI
    asyncio.run(scenario())


def test_old_institution_badge_does_not_survive_expired_gateway():
    async def scenario():
        ctx, vpn = FakeContext(), BfsuWebVPN()
        page = FakePage(ctx, vpn.home_url, True)
        page.next_url = LOGIN_URL
        ctx.pages.append(page)
        vpn.page, vpn.ready = page, True
        result = await vpn.ensure(ctx)
        assert result["authentication_required"] and not vpn.ready
        assert page.gotos == []  # rejected by the shared gateway before CNKI
        assert vpn.gateway.page.gotos == [ORIGIN + "/"]
    asyncio.run(scenario())


def test_proxied_cnki_without_school_badge_is_not_authenticated():
    async def scenario():
        ctx, vpn = FakeContext(authenticated=True), BfsuWebVPN()
        vpn.page = FakePage(ctx, vpn.home_url)
        ctx.pages.append(vpn.page)
        result = await vpn.ensure(ctx)
        assert result["authentication_stage"] == "cnki_institution"
        assert not vpn.ready
    asyncio.run(scenario())


def test_gateway_cookie_cache_is_encrypted_and_scoped(monkeypatch, tmp_path):
    from paper_search_mcp import comm_auth
    # Use a deterministic test envelope; Windows DPAPI itself is exercised separately.
    def envelope(data, decrypt=False):
        return data[8:] if decrypt else b"ENCRYPT:" + data
    monkeypatch.setattr(comm_auth, "protect", envelope)
    cookies = [
        {"domain": ".webvpn.bfsu.edu.cn", "name": "gateway", "value": "test-only"},
        {"domain": ".cnki.net", "name": "cnki", "value": "test-only"},
        {"domain": "my.bfsu.edu.cn", "name": "sso", "value": "not-retained"},
        {"domain": "webvpn.bfsu.edu.cn.evil.example", "name": "other", "value": "not-retained"},
    ]
    async def scenario():
        browser = BrowserSession(tmp_path / "session", webvpn_enabled=True)
        browser._context = SimpleNamespace(cookies=AsyncMock(return_value=cookies))
        browser.webvpn_gateway.ready = True
        await browser.save_cookies(school_verified=True)
        assert b"gateway" not in browser.cookie_file.read_bytes()
        assert browser.webvpn_cookie_file.read_bytes().startswith(b"ENCRYPT:")
        retained = json.loads(envelope(browser.webvpn_cookie_file.read_bytes(), True))
        assert [c["name"] for c in retained] == ["gateway"]
        new_context = SimpleNamespace(add_cookies=AsyncMock(), cookies=AsyncMock(return_value=[]))
        await browser.load_cookies(new_context)
        loaded = [cookie for call in new_context.add_cookies.call_args_list for cookie in call.args[0]]
        assert {c["name"] for c in loaded} == {"gateway", "cnki"}
    asyncio.run(scenario())


def test_authentication_required_blocks_direct_download(monkeypatch, tmp_path):
    monkeypatch.setenv("COMM_CNKI_ACCESS_MODE", "bfsu_webvpn")
    async def scenario():
        backend = CNKIBackend(tmp_path / "session")
        context = FakeContext()
        backend._context = AsyncMock(return_value=context)
        backend.browser.save_cookies = AsyncMock()
        result = await backend.download("https://kns.cnki.net/kcms/detail?id=1", "test")
        assert result["authentication_required"]
        assert len(context.pages) == 1 and context.pages[0].gotos == [ORIGIN + "/"]
    asyncio.run(scenario())


def test_direct_mode_does_not_construct_webvpn(monkeypatch, tmp_path):
    monkeypatch.setenv("COMM_CNKI_ACCESS_MODE", "direct")
    backend = CNKIBackend(tmp_path / "session")
    assert backend.webvpn is None and not backend.browser.webvpn_enabled
    assert backend._access_url("https://kns.cnki.net/a") == "https://kns.cnki.net/a"


def test_school_login_requests_never_enter_response_capture(tmp_path):
    route = SimpleNamespace(request=SimpleNamespace(url=LOGIN_URL, resource_type="document"),
                            continue_=AsyncMock(), fetch=AsyncMock())
    asyncio.run(RetainedResponses(tmp_path).route(route))
    route.continue_.assert_awaited_once()
    route.fetch.assert_not_awaited()


class NativeSession:
    def __init__(self, fail_on=None):
        self.handlers = {}
        self.calls = []
        self.fail_on = fail_on
        self.detached = False

    def on(self, event, handler):
        self.handlers[event] = handler

    async def send(self, method, params=None):
        import base64
        self.calls.append(method)
        if method == self.fail_on:
            raise RuntimeError("test-only protocol failure")
        if method == "Fetch.getResponseBody":
            return {"base64Encoded": True, "body": base64.b64encode(b"%PDF-1.7\ntest-only").decode()}
        return {}

    async def detach(self):
        self.detached = True


def native_event(url=None, mime="application/pdf", size="20"):
    return {"requestId": "test-only", "request": {"url": url or proxy_url("https://docdown.cnki.net/fulltext")},
            "responseHeaders": [{"name": "Content-Type", "value": mime}, {"name": "Content-Length", "value": size}]}


def test_native_capture_attaches_only_target_and_retains_pdf(tmp_path):
    async def scenario():
        page = FakePage(FakeContext(), "about:blank")
        session = NativeSession()
        ctx = SimpleNamespace(new_cdp_session=AsyncMock(return_value=session))
        retained = RetainedResponses(tmp_path, webvpn_enabled=True)
        await retained.arm_native(ctx, page)
        ctx.new_cdp_session.assert_awaited_once_with(page)
        await session.handlers["Fetch.requestPaused"](native_event())
        assert retained.arrived.is_set() and retained.files[0]["success"]
        from pathlib import Path
        assert Path(retained.files[0]["file_path"]).read_bytes() == b"%PDF-1.7\ntest-only"
        assert "Fetch.failRequest" in session.calls and "Fetch.continueRequest" not in session.calls
        await retained.disarm_native()
        assert session.detached and not retained.capture_enabled
    asyncio.run(scenario())


@pytest.mark.parametrize("event", [native_event(mime="text/html"), native_event(url="https://example.com/other.pdf"), native_event(url="https://docdown.cnki.net/fulltext")])
def test_native_capture_does_not_read_html_or_unrelated_response_bodies(tmp_path, event):
    async def scenario():
        session = NativeSession()
        retained = RetainedResponses(tmp_path, webvpn_enabled=True)
        await retained.arm_native(SimpleNamespace(new_cdp_session=AsyncMock(return_value=session)), FakePage(FakeContext(), "about:blank"))
        await session.handlers["Fetch.requestPaused"](event)
        assert "Fetch.getResponseBody" not in session.calls
        assert "Fetch.continueRequest" in session.calls and not retained.files
        await retained.disarm_native()
    asyncio.run(scenario())


def test_native_disk_failure_returns_immediate_failure_and_aborts(monkeypatch, tmp_path):
    async def scenario():
        session = NativeSession()
        retained = RetainedResponses(tmp_path, webvpn_enabled=True)
        def disk_full(*args, **kwargs):
            raise OSError("test-only disk full")
        monkeypatch.setattr(retained, "_retain_body", disk_full)
        await retained.arm_native(SimpleNamespace(new_cdp_session=AsyncMock(return_value=session)), FakePage(FakeContext(), "about:blank"))
        await session.handlers["Fetch.requestPaused"](native_event())
        assert retained.arrived.is_set() and not retained.files[0]["success"]
        assert "Fetch.failRequest" in session.calls and "Fetch.continueRequest" not in session.calls
        await retained.disarm_native()
    asyncio.run(scenario())


def test_native_attach_fails_before_a_download_can_be_clicked(tmp_path):
    from paper_search_mcp.cnki.browser import BrowserConfigurationError
    async def scenario():
        retained = RetainedResponses(tmp_path, webvpn_enabled=True)
        session = NativeSession(fail_on="Fetch.enable")
        with pytest.raises(BrowserConfigurationError):
            await retained.arm_native(SimpleNamespace(new_cdp_session=AsyncMock(return_value=session)), FakePage(FakeContext(), "about:blank"))
        await retained.disarm_native()
        assert session.detached
    asyncio.run(scenario())


def test_native_cleanup_detaches_even_if_disable_fails(tmp_path):
    async def scenario():
        retained = RetainedResponses(tmp_path, webvpn_enabled=True)
        session = NativeSession(fail_on="Fetch.disable")
        await retained.arm_native(SimpleNamespace(new_cdp_session=AsyncMock(return_value=session)), FakePage(FakeContext(), "about:blank"))
        await retained.disarm_native()
        assert session.detached and not retained._cdp_sessions
    asyncio.run(scenario())


def test_login_and_search_use_native_network_until_download(tmp_path):
    route = SimpleNamespace(request=SimpleNamespace(url=proxy_url("https://www.cnki.net/"), resource_type="document"),
                            continue_=AsyncMock(), fetch=AsyncMock())
    asyncio.run(RetainedResponses(tmp_path, webvpn_enabled=True).route(route))
    route.continue_.assert_awaited_once()
    route.fetch.assert_not_awaited()


def test_direct_mode_still_captures_redirected_cdn_files(tmp_path):
    from tests.test_comm_bilingual import FakeRoute, FakeResponse
    route = FakeRoute(FakeResponse({"content-type": "application/pdf"}, b"%PDF-1.7\ntest-only"))
    route.request.url = "https://publisher-cdn.example/authorized.pdf"
    retained = RetainedResponses(tmp_path)
    asyncio.run(retained.route(route))
    assert retained.files[0]["success"] and route.aborted


def test_visible_password_form_is_not_saved_as_diagnostic(monkeypatch, tmp_path):
    monkeypatch.setenv("COMM_CNKI_ACCESS_MODE", "bfsu_webvpn")
    async def scenario():
        backend = CNKIBackend(tmp_path / "session")
        field = SimpleNamespace(is_visible=AsyncMock(return_value=True))
        fields = SimpleNamespace(count=AsyncMock(return_value=1), nth=lambda _: field)
        page = SimpleNamespace(url=proxy_url("https://kns.cnki.net/search"),
                               locator=lambda _: fields, bring_to_front=AsyncMock(),
                               content=AsyncMock(), screenshot=AsyncMock())
        backend._context = AsyncMock(return_value=FakeContext(authenticated=True))
        result = await backend._verification_result(page, "search")
        assert result["authentication_required"]
        assert result["authentication_stage"] == "cnki_institution"
        page.content.assert_not_awaited()
        page.screenshot.assert_not_awaited()
    asyncio.run(scenario())


def test_school_mention_in_body_is_not_an_institution_badge():
    async def scenario():
        ctx, vpn = FakeContext(authenticated=True), BfsuWebVPN()
        page = FakePage(ctx, vpn.home_url, False)
        original_locator = page.locator
        page.locator = lambda selector: (SimpleNamespace(inner_text=AsyncMock(return_value="北京外国语大学研究"))
                                         if selector == "body" else original_locator(selector))
        vpn.page = page
        ctx.pages.append(page)
        result = await vpn.ensure(ctx)
        assert not result.get("authenticated")
    asyncio.run(scenario())
