"""Offline regressions for one shared BFSU login and separate resource checks.

Run with ``-p no:tmpdir -p no:cacheprovider -p tests.retained_tmp`` and ``-B``.
The finite fake browser never launches Chromium, accesses a network, reads form
values, or deletes files. Session directories use tests.retained_tmp only.
"""
import asyncio
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import pytest

from paper_search_mcp.cnki.browser import BrowserSession
from paper_search_mcp.cnki.webvpn import BfsuGateway, BfsuWebVPN, LOGIN_URL, ORIGIN, proxy_url
from paper_search_mcp.comm_cnki_host import CNKIBackend
from paper_search_mcp import comm_wos_host
from paper_search_mcp.comm_wos_host import WOSBackend, is_wos, resource_url


CNKI_HOME = proxy_url("https://www.cnki.net/")
WOS_HOME = resource_url("https://webofscience.clarivate.cn/wos/woscc/smart-search")
LIBRARY_HOME = resource_url("https://lib.bfsu.edu.cn/info/71321.jspx")


class FakeLocator:
    """A small set of real portal, password-form, library and badge elements."""

    def __init__(self, page, selector=None, role=None, text=None, index=None):
        self.page, self.selector, self.role = page, selector, role
        self.text, self.index = text, index

    def _elements(self):
        elements = self.page.elements()
        if self.role is not None:
            elements = [item for item in elements if item.get("role") == self.role]
        if self.selector is not None:
            selector = self.selector
            if selector == "body":
                elements = [{"text": self.page.body_text(), "visible": True}]
            elif "input" in selector and "password" in selector:
                elements = [item for item in elements if item.get("kind") == "password"]
            elif selector == "#ecpHeaderContainer":
                elements = [item for item in elements if item.get("kind") == "cnki_badge"]
            elif selector.startswith("a[href*="):
                match = re.fullmatch(r"a\[href\*=['\"]([^'\"]+)['\"]\]", selector)
                assert match, f"Unsupported fake selector: {selector}"
                elements = [item for item in elements
                            if item.get("role") == "link" and match[1] in item.get("href", "")]
            elif selector in {"[aria-label=\"Close this tour\"]", "#onetrust-reject-all-handler",
                              ".onetrust-close-btn-handler"}:
                elements = []
            else:
                raise AssertionError(f"Unsupported fake selector: {selector}")
        if self.text is not None:
            if hasattr(self.text, "search"):
                elements = [item for item in elements if self.text.search(item.get("text", ""))]
            else:
                elements = [item for item in elements if str(self.text) in item.get("text", "")]
        if self.index is not None:
            elements = elements[self.index:self.index + 1]
        return elements

    @property
    def first(self):
        return FakeLocator(self.page, self.selector, self.role, self.text, 0)

    def nth(self, index):
        return FakeLocator(self.page, self.selector, self.role, self.text, index)

    def filter(self, has_text=None, **kwargs):
        assert not kwargs, kwargs
        return FakeLocator(self.page, self.selector, self.role, has_text, self.index)

    async def count(self):
        return len(self._elements())

    async def is_visible(self):
        return bool(self._elements() and self._elements()[0].get("visible", True))

    async def inner_text(self, **kwargs):
        elements = self._elements()
        assert elements, "No matching element"
        return elements[0].get("text", "")

    async def wait_for(self, state="visible", **kwargs):
        assert state == "visible"
        if not await self.is_visible():
            raise TimeoutError("fake element is not visible")

    async def click(self, **kwargs):
        elements = self._elements()
        assert elements and elements[0].get("visible", True)
        assert elements[0].get("role") == "link", "Only normal resource links are clickable"
        await self.page.context.open_resource_link(elements[0]["href"])

    async def get_attribute(self, name, **kwargs):
        return self._elements()[0].get(name)

    async def input_value(self, **kwargs):
        raise AssertionError("Authentication must never read credentials")

    async def fill(self, *args, **kwargs):
        raise AssertionError("Authentication must never fill credentials")


class FakePage:
    def __init__(self, context, url="about:blank", kind="blank", badge=False):
        self.context, self.url, self.kind, self.badge = context, url, kind, badge
        self.frames = [self]
        self.gotos, self.fronts, self.closed = [], 0, False

    def is_closed(self):
        return self.closed or self.context.closed

    async def goto(self, url, **kwargs):
        assert not self.is_closed(), "Cannot navigate a closed fake context/page"
        self.gotos.append(url)
        if url == ORIGIN + "/":
            self.context.portal_checks += 1
            self.kind = "portal" if self.context.school_logged_in else "login"
            self.url = url if self.context.school_logged_in else LOGIN_URL
        elif url == LOGIN_URL:
            # A stale /login tab can retain its form after another tab logs in.
            self.kind, self.url = "login", LOGIN_URL
        elif url == LIBRARY_HOME:
            self.kind, self.url = (("library", url) if self.context.school_logged_in
                                   else ("login", LOGIN_URL))
        elif url == CNKI_HOME or url.startswith(proxy_url("https://kns.cnki.net/")):
            if self.context.school_logged_in:
                self.kind, self.url, self.badge = "cnki", url, self.context.cnki_badge
            else:
                self.kind, self.url, self.badge = "login", LOGIN_URL, False
        elif is_wos(url):
            if self.context.school_logged_in:
                self.kind, self.url, self.badge = "wos", WOS_HOME, self.context.wos_badge
            else:
                self.kind, self.url, self.badge = "login", LOGIN_URL, False
        else:
            raise AssertionError(f"Unexpected fake navigation: {url}")

    def elements(self):
        if self.kind == "login":
            return [{"kind": "password", "visible": True}]
        if self.kind == "portal":
            return [{"role": "link", "text": "中国知网", "href": CNKI_HOME,
                     "visible": self.context.portal_links_visible},
                    {"role": "link", "text": "图书馆资源", "href": LIBRARY_HOME,
                     "visible": self.context.portal_links_visible}]
        if self.kind == "library":
            return [{"role": "link", "text": "Web of Science",
                     "href": resource_url("http://www.webofscience.com/"), "visible": True}]
        if self.kind == "cnki":
            return [{"kind": "cnki_badge", "visible": True,
                     "text": "北京外国语大学图书馆" if self.badge else "个人登录"}]
        return []

    def body_text(self):
        if self.kind == "wos":
            return "Web of Science · Beijing Foreign Studies University" if self.badge else "Web of Science · Sign in"
        if self.kind == "portal":
            return "北京外国语大学资源访问控制系统 中国知网 图书馆资源"
        if self.kind == "cnki":
            return "CNKI 北京外国语大学图书馆" if self.badge else "CNKI 个人登录"
        return "统一身份认证" if self.kind == "login" else ""

    def locator(self, selector):
        return FakeLocator(self, selector=selector)

    def get_by_role(self, role, name=None, **kwargs):
        assert role == "link", role
        return FakeLocator(self, role=role, text=name)

    async def bring_to_front(self):
        assert not self.is_closed()
        self.fronts += 1

    async def wait_for_load_state(self, *args, **kwargs):
        return None

    async def wait_for_timeout(self, *args, **kwargs):
        return None

    async def close(self):
        self.closed = True


class FakeContext:
    """The boolean ticket models a current school session, never cookie values."""

    def __init__(self, school_logged_in=False, cnki_badge=True, wos_badge=True,
                 portal_links_visible=True):
        self.school_logged_in = school_logged_in
        self.cnki_badge, self.wos_badge = cnki_badge, wos_badge
        self.portal_links_visible = portal_links_visible
        self.pages, self.handlers = [], {}
        self.portal_checks, self.closed = 0, False

    async def new_page(self):
        assert not self.closed
        page = FakePage(self)
        self.pages.append(page)
        return page

    async def open_resource_link(self, href):
        page = await self.new_page()
        await page.goto(href)
        return page

    def on(self, event, handler):
        assert event == "close", event
        self.handlers.setdefault(event, []).append(handler)

    async def route(self, *args, **kwargs):
        raise AssertionError("No network interception is needed for fake authentication")

    async def cookies(self, *args, **kwargs):
        raise AssertionError("Offline session reuse must not inspect real cookies")

    async def close(self):
        self.closed = True
        for page in self.pages:
            page.closed = True
        for handler in self.handlers.get("close", []):
            handler(self)


@pytest.fixture(autouse=True)
def no_real_authentication_wait(monkeypatch):
    async def no_wait(_):
        return None
    # Authentication loops stay bounded but run without 40 seconds of polling.
    monkeypatch.setattr(comm_wos_host, "asyncio", SimpleNamespace(sleep=no_wait))


def backends(monkeypatch, tmp_path, context):
    monkeypatch.setenv("COMM_CNKI_ACCESS_MODE", "bfsu_webvpn")
    cnki = CNKIBackend(tmp_path / "session")
    cnki.browser.get_context = AsyncMock(return_value=context)
    cnki.browser.save_cookies = AsyncMock()
    wos = WOSBackend(cnki.browser)
    return cnki, wos


def authentication_call(engine, cnki, wos):
    return cnki.authenticate() if engine == "cnki" else wos.authenticate()


def test_backends_share_one_gateway_without_global_cross_session_state(monkeypatch, tmp_path):
    cnki, wos = backends(monkeypatch, tmp_path, FakeContext())
    gateway = cnki.browser.webvpn_gateway
    assert isinstance(gateway, BfsuGateway)
    assert cnki.webvpn.gateway is gateway and wos.gateway is gateway
    assert BfsuWebVPN(gateway=gateway).gateway is gateway
    other = BrowserSession(tmp_path / "other-session", webvpn_enabled=True)
    assert isinstance(other.webvpn_gateway, BfsuGateway)
    assert other.webvpn_gateway is not gateway


@pytest.mark.parametrize("order", [("cnki", "wos"), ("wos", "cnki")])
def test_current_school_session_is_reused_in_both_language_orders(monkeypatch, tmp_path, order):
    async def scenario():
        context = FakeContext(school_logged_in=True)
        cnki, wos = backends(monkeypatch, tmp_path, context)
        gateway = cnki.browser.webvpn_gateway
        for position, engine in enumerate(order, 1):
            result = await authentication_call(engine, cnki, wos)
            assert result["success"] and result["authenticated"]
            assert gateway.ready and context.portal_checks >= position
        assert cnki.webvpn.gateway is wos.gateway is gateway
        assert not any(page.kind == "login" for page in context.pages)
    asyncio.run(scenario())


@pytest.mark.parametrize("first_engine", ["cnki", "wos"])
def test_old_login_tabs_do_not_block_new_valid_school_session(monkeypatch, tmp_path, first_engine):
    async def scenario():
        context = FakeContext()
        cnki, wos = backends(monkeypatch, tmp_path, context)
        first = await authentication_call(first_engine, cnki, wos)
        assert first["authentication_stage"] == "webvpn_login"
        gateway = cnki.browser.webvpn_gateway
        login = gateway.login_page
        assert login is not None and login.kind == "login"
        before_gotos, before_fronts = list(login.gotos), login.fronts
        cnki.webvpn.page = wos.page = login
        # Completion in a different tab updates the context's current ticket.
        context.school_logged_in = True
        other_engine = "wos" if first_engine == "cnki" else "cnki"
        resumed = await authentication_call(other_engine, cnki, wos)
        original = await authentication_call(first_engine, cnki, wos)
        assert resumed["authenticated"] and original["authenticated"] and gateway.ready
        assert login.kind == "login" and login.gotos == before_gotos
        assert login.fronts == before_fronts
    asyncio.run(scenario())


@pytest.mark.parametrize("resource_home", [CNKI_HOME, WOS_HOME], ids=["cnki", "wos"])
def test_manual_resource_navigation_does_not_reclaim_gateway_tab(resource_home):
    async def scenario():
        context = FakeContext(school_logged_in=True)
        gateway = BfsuGateway()
        assert (await gateway.ensure(context))["authenticated"]
        resource = gateway.page
        # The user chooses a database in the gateway's tab outside the adapter.
        await resource.goto(resource_home)
        resource_url_before, resource_gotos_before = resource.url, list(resource.gotos)
        checks_before = context.portal_checks

        result = await gateway.ensure(context)

        assert result["authenticated"] and gateway.ready
        assert gateway.page is not resource and gateway.page.kind == "portal"
        assert context.portal_checks > checks_before
        assert resource.url == resource_url_before
        assert resource.gotos == resource_gotos_before, "The user's resource tab must stay untouched"
    asyncio.run(scenario())


@pytest.mark.parametrize("order", [("cnki", "wos"), ("wos", "cnki")])
def test_same_tab_resource_links_preserve_previous_database_page(monkeypatch, tmp_path, order):
    async def scenario():
        context = FakeContext(school_logged_in=True)
        cnki, wos = backends(monkeypatch, tmp_path, context)
        clicks = []

        async def open_in_same_tab(href):
            sources = [page for page in context.pages
                       if not page.is_closed() and any(
                           element.get("href") == href and element.get("visible", True)
                           for element in page.elements())]
            assert len(sources) == 1, "The resource link must have one current source tab"
            entry = sources[0]
            clicks.append((entry, href))
            await entry.goto(href)
            return entry

        # Keep the default popup behavior intact for all the other tests.
        monkeypatch.setattr(context, "open_resource_link", open_in_same_tab)
        first_engine, second_engine = order
        assert (await authentication_call(first_engine, cnki, wos))["authenticated"]
        first_resource = cnki.webvpn.page if first_engine == "cnki" else wos.page
        first_url, first_gotos = first_resource.url, list(first_resource.gotos)
        assert len(context.pages) == 1, "All first-database links navigated the same tab"

        result = await authentication_call(second_engine, cnki, wos)

        assert result["authenticated"] and cnki.browser.webvpn_gateway.ready
        second_resource = cnki.webvpn.page if second_engine == "cnki" else wos.page
        assert second_resource is not first_resource
        assert first_resource.url == first_url
        assert first_resource.gotos == first_gotos, "The next school probe must preserve the first database"
        assert len(context.pages) == 2, "One resource tab per database, without fake popup tabs"
        assert len(clicks) == 3, "Exercise both portal links and the library's WoS link"
    asyncio.run(scenario())


@pytest.mark.parametrize("first_engine", ["cnki", "wos"])
def test_waiting_login_form_is_retained_and_prompted_once(monkeypatch, tmp_path, first_engine):
    async def scenario():
        context = FakeContext()
        cnki, wos = backends(monkeypatch, tmp_path, context)
        first = await authentication_call(first_engine, cnki, wos)
        assert first["authentication_required"]
        gateway = cnki.browser.webvpn_gateway
        login = gateway.login_page
        assert login is not None
        initial_gotos = list(login.gotos)
        for engine in ["cnki", "wos"] * 4:
            result = await authentication_call(engine, cnki, wos)
            assert result["authentication_stage"] == "webvpn_login"
            assert not gateway.ready
        assert login.gotos == initial_gotos, "The user may still be filling this form"
        assert len(context.pages) <= 2, "One login form and one reusable probe are sufficient"
        assert sum(page.fronts for page in context.pages) == 1
        assert context.portal_checks >= 9, "Each operation must check the current school ticket"
    asyncio.run(scenario())


@pytest.mark.parametrize("engine", ["cnki", "wos"])
def test_expired_school_session_blocks_stale_resource_badge(monkeypatch, tmp_path, engine):
    async def scenario():
        context = FakeContext(school_logged_in=True)
        cnki, wos = backends(monkeypatch, tmp_path, context)
        assert (await authentication_call(engine, cnki, wos))["authenticated"]
        resource = cnki.webvpn.page if engine == "cnki" else wos.page
        assert resource.badge
        resource_gotos = list(resource.gotos)
        checks = context.portal_checks
        context.school_logged_in = False
        # A loaded resource still displays yesterday's institution indicator.
        assert resource.badge
        if engine == "wos":
            assert "University" in resource.body_text()
        expired = await authentication_call(engine, cnki, wos)
        assert expired["authentication_required"]
        assert expired["authentication_stage"] == "webvpn_login"
        assert not cnki.browser.webvpn_gateway.ready
        assert context.portal_checks > checks
        assert resource.gotos == resource_gotos, "Expired gateway must block before resource navigation"
    asyncio.run(scenario())


@pytest.mark.parametrize("engine", ["cnki", "wos"])
def test_resource_badge_failure_preserves_valid_school_session(monkeypatch, tmp_path, engine):
    async def scenario():
        context = FakeContext(school_logged_in=True, cnki_badge=engine != "cnki", wos_badge=engine != "wos")
        cnki, wos = backends(monkeypatch, tmp_path, context)
        result = await authentication_call(engine, cnki, wos)
        assert result["authentication_required"]
        assert result["authentication_stage"] == engine + "_institution"
        assert cnki.browser.webvpn_gateway.ready
        other_engine = "wos" if engine == "cnki" else "cnki"
        assert (await authentication_call(other_engine, cnki, wos))["authenticated"]
        assert cnki.browser.webvpn_gateway.ready
    asyncio.run(scenario())


@pytest.mark.parametrize("engine", ["cnki", "wos"])
def test_replacement_context_cannot_inherit_previous_gateway_authorization(monkeypatch, tmp_path, engine):
    async def scenario():
        old = FakeContext(school_logged_in=True)
        cnki, wos = backends(monkeypatch, tmp_path, old)
        assert (await authentication_call(engine, cnki, wos))["authenticated"]
        new = FakeContext(school_logged_in=False)
        cnki.browser.get_context = AsyncMock(return_value=new)
        result = await authentication_call(engine, cnki, wos)
        assert result["authentication_stage"] == "webvpn_login"
        assert not cnki.browser.webvpn_gateway.ready and new.portal_checks >= 1
        assert cnki.browser.webvpn_gateway.login_page.context is new
    asyncio.run(scenario())


@pytest.mark.parametrize("engine", ["cnki", "wos"])
def test_closed_context_revalidates_even_when_next_school_session_is_valid(monkeypatch, tmp_path, engine):
    async def scenario():
        old = FakeContext(school_logged_in=True)
        cnki, wos = backends(monkeypatch, tmp_path, old)
        assert (await authentication_call(engine, cnki, wos))["authenticated"]
        await old.close()
        new = FakeContext(school_logged_in=True)
        cnki.browser.get_context = AsyncMock(return_value=new)
        result = await authentication_call(engine, cnki, wos)
        assert result["authenticated"] and new.portal_checks >= 1
        assert cnki.browser.webvpn_gateway.page.context is new
        assert (cnki.webvpn.page if engine == "cnki" else wos.page).context is new
    asyncio.run(scenario())


def test_root_without_visible_resource_entries_is_not_school_session_proof():
    async def scenario():
        context = FakeContext(school_logged_in=True, portal_links_visible=False)
        gateway = BfsuGateway()
        result = await gateway.ensure(context)
        assert not result["success"] and not gateway.ready
        assert result["authentication_required"] is False
        assert result["school_authenticated"] is None
        assert result["authentication_stage"] == "webvpn_portal"
    asyncio.run(scenario())


def test_direct_cnki_mode_does_not_open_or_check_webvpn(monkeypatch, tmp_path):
    monkeypatch.setenv("COMM_CNKI_ACCESS_MODE", "direct")
    cnki = CNKIBackend(tmp_path / "direct-session")
    cnki.browser.get_context = AsyncMock(side_effect=AssertionError("Direct status needs no gateway/browser"))
    assert cnki.webvpn is None and not cnki.browser.webvpn_enabled
    assert getattr(cnki.browser, "webvpn_gateway", None) is None
    assert cnki._access_url("https://kns.cnki.net/kns8s/defaultresult/index") == "https://kns.cnki.net/kns8s/defaultresult/index"
    result = asyncio.run(cnki.authenticate())
    assert result["access_mode"] == "direct" and result["authentication_required"] is False
    cnki.browser.get_context.assert_not_awaited()
