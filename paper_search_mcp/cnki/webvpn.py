"""BFSU WebVPN access through the user's ordinary institutional login.

The public Wengine address codec only rewrites hostnames; it grants no access.
Its output is checked against the www/kns links observed on BFSU's CNKI page.
All authentication remains in the visible browser. No password is collected.
"""
from __future__ import annotations

import contextlib
import re
from urllib.parse import urljoin, urlsplit, urlunsplit

ORIGIN = "https://webvpn.bfsu.edu.cn"
LOGIN_URL = ORIGIN + "/login"
PUBLIC_ADDRESS_KEY = b"wrdvpnisthebest!"
INSTITUTION = "北京外国语大学"


def cnki_host(host):
    host = (host or "").lower()
    return any(host == root or host.endswith("." + root)
               for root in ("cnki.net", "cnki.com.cn"))


def _cipher():
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms
    try:
        from cryptography.hazmat.decrepit.ciphers.modes import CFB
    except ImportError:
        from cryptography.hazmat.primitives.ciphers.modes import CFB
    return Cipher(algorithms.AES(PUBLIC_ADDRESS_KEY), CFB(PUBLIC_ADDRESS_KEY))


def canonical_url(url):
    """Accept only CNKI URLs, or BFSU proxy URLs that decode to a CNKI host."""
    if not isinstance(url, str) or any(c in url for c in "\\\r\n\t"):
        raise ValueError("Expected a CNKI URL")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        raise ValueError("Expected an HTTP(S) CNKI URL without credentials")
    if parsed.port not in {None, 80 if parsed.scheme == "http" else 443}:
        raise ValueError("Unexpected CNKI URL port")
    if cnki_host(parsed.hostname):
        return urlunsplit((parsed.scheme, parsed.hostname, parsed.path or "/", parsed.query, parsed.fragment))
    if parsed.scheme != "https" or parsed.hostname != "webvpn.bfsu.edu.cn":
        raise ValueError("Expected CNKI or BFSU's configured WebVPN")
    match = re.fullmatch(r"/(https?)/([0-9a-f]+)(/.*)?", parsed.path)
    prefix = PUBLIC_ADDRESS_KEY.hex()
    if not match or not match[2].startswith(prefix):
        raise ValueError("Not a BFSU CNKI resource URL")
    try:
        encrypted = bytes.fromhex(match[2][len(prefix):])
        host = _cipher().decryptor().update(encrypted).decode("ascii")
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Invalid BFSU resource hostname") from exc
    if not re.fullmatch(r"[a-z0-9.-]+", host) or not cnki_host(host):
        raise ValueError("BFSU resource must point to CNKI")
    return urlunsplit((match[1], host, match[3] or "/", parsed.query, parsed.fragment))


def proxy_url(url):
    parsed = urlsplit(canonical_url(url))
    token = PUBLIC_ADDRESS_KEY.hex() + _cipher().encryptor().update(parsed.hostname.encode("ascii")).hex()
    return urlunsplit(("https", "webvpn.bfsu.edu.cn",
                      "/" + parsed.scheme + "/" + token + (parsed.path or "/"),
                      parsed.query, parsed.fragment))


def resolve_cnki_url(href, page_url):
    """Resolve relative upstream and already-rewritten links without losing the host."""
    if href.startswith(("http://", "https://", "//")):
        return canonical_url(urljoin(page_url, href))
    if href.startswith(("/http/", "/https/")) and urlsplit(page_url).hostname == "webvpn.bfsu.edu.cn":
        return canonical_url(ORIGIN + href)
    return canonical_url(urljoin(canonical_url(page_url), href))


def is_proxy_cnki(url):
    try:
        return urlsplit(url).hostname == "webvpn.bfsu.edu.cn" and bool(canonical_url(url))
    except (ValueError, TypeError):
        return False


async def contains_login_form(page):
    """Check only form structure/visibility; never read entered credentials."""
    for frame in getattr(page, "frames", [page]):
        fields = frame.locator("input[type='password']")
        for index in range(await fields.count()):
            if await fields.nth(index).is_visible():
                return True
    return False


def is_portal(url):
    parsed = urlsplit(url)
    return parsed.scheme == "https" and parsed.hostname == "webvpn.bfsu.edu.cn" and parsed.path == "/"


def is_school_login(url):
    parsed = urlsplit(url)
    return parsed.hostname == "webvpn.bfsu.edu.cn" and parsed.path.startswith("/login")


class BfsuGateway:
    """One school login per BrowserSession, independent of database entitlement.

    Keep a pending human login untouched. A second, reusable portal probe can
    detect a ticket obtained in another tab without reloading that form.
    """

    def __init__(self, on_authenticated=None):
        self.on_authenticated = on_authenticated
        self.reset()

    def reset(self):
        self.context = None
        self.page = None
        self.login_page = None
        self.ready = False
        self._prompted_page = None

    @staticmethod
    def _alive(page):
        return page is not None and not page.is_closed()

    def status(self):
        return {"last_check_authenticated": self.ready,
                "browser_open": self._alive(self.page) or self._alive(self.login_page)}

    def release_page(self, page):
        """A same-tab resource link hands this page to a database adapter."""
        if page is self.page and not is_portal(page.url):
            self.page = None

    async def ensure(self, context):
        if context is not self.context:
            self.reset()
            self.context = context
        self.ready = False
        if not self._alive(self.login_page):
            self.login_page = None

        if self._alive(self.page) and self.page is not self.login_page:
            current_url = urlsplit(self.page.url)
            if (current_url.hostname == "webvpn.bfsu.edu.cn"
                    and current_url.path.startswith(("/http/", "/https/"))):
                # The user can also click a resource manually in this tab.
                # Hand it off without replacing their document with the portal.
                self.page = None

        probe = self.page if self._alive(self.page) and self.page is not self.login_page else None
        if probe is None:
            # A new portal tab may be the result of login in either database.
            for candidate in reversed(context.pages):
                if (not candidate.is_closed() and is_portal(candidate.url)
                        and not await contains_login_form(candidate)):
                    probe = candidate
                    break
        if probe is None and self.login_page is None:
            for candidate in reversed(context.pages):
                if not candidate.is_closed() and is_school_login(candidate.url):
                    self.login_page = candidate
                    break
        if probe is None:
            probe = await context.new_page()
        self.page = probe
        # Always make a fresh request: old resource badges and saved cookies
        # alone do not establish that the gateway ticket is still valid.
        await probe.goto(ORIGIN + "/", wait_until="domcontentloaded", timeout=30000)
        login_form = await contains_login_form(probe)
        if is_portal(probe.url) and not login_form:
            card = probe.get_by_role("link").filter(has_text=re.compile("中国知网|图书馆资源")).first
            if not await card.count():
                with contextlib.suppress(Exception):
                    await card.wait_for(state="visible", timeout=8000)
            if (await card.count() and await card.is_visible()
                    and not await contains_login_form(probe)):
                self.ready = True
                self.login_page = None
                self._prompted_page = None
                result = {"success": True, "authenticated": True,
                          "school_authenticated": True, "shared_school_login": True,
                          "access_mode": "bfsu_webvpn"}
                if self.on_authenticated:
                    try:
                        result["session_persistence"] = await self.on_authenticated()
                    except Exception:
                        result["session_persistence"] = {"gateway_save": "failed"}
                    if result["session_persistence"].get("gateway_save") in {"failed", "skipped_empty"}:
                        result["persistence_warning"] = "学校登录有效，但会话缓存尚未保存；下次启动可能需要重新登录。"
                return result

        needs_login = is_school_login(probe.url) or await contains_login_form(probe)
        if needs_login and self.login_page is None:
            self.login_page = probe
        prompt = self.login_page if needs_login else probe
        if prompt is not self._prompted_page:
            await prompt.bring_to_front()
            self._prompted_page = prompt
        return {"success": False, "status": "needs_attention",
                "authentication_required": needs_login,
                "authentication_stage": "webvpn_login" if needs_login else "webvpn_portal",
                "school_authenticated": False if needs_login else None,
                "shared_school_login": True, "access_mode": "bfsu_webvpn",
                "login_url": LOGIN_URL,
                "message": ("请在已打开的统一文献浏览器完成一次北外登录；CNKI 和 WoS 共用此会话，无需分别登录。完成后重试原操作。"
                            if needs_login else
                            "尚未确认北外资源选择页已加载，请查看已打开的页面后重试；此状态不代表需要重新登录。")}


class BfsuWebVPN:
    """CNKI institution access, preceded by the shared school login check."""

    def __init__(self, gateway=None):
        self.gateway = gateway if gateway is not None else BfsuGateway()
        self.context = None
        self.page = None
        self.ready = False
        self.stage = "not_checked"
        self.home_url = proxy_url("https://www.cnki.net/")
        self._prompted_page = None

    async def _show_once(self):
        if self.page is not self._prompted_page:
            await self.page.bring_to_front()
            self._prompted_page = self.page

    def status(self):
        return {"access_mode": "bfsu_webvpn", "login_url": LOGIN_URL,
                "last_check_authenticated": self.ready,
                "school_login": self.gateway.status(),
                "browser_open": self.page is not None and not self.page.is_closed()}

    def required(self, stage="webvpn_login"):
        self.ready, self.stage = False, stage
        return {"success": False, "authentication_required": True,
                "access_mode": "bfsu_webvpn", "authentication_stage": stage,
                "school_authenticated": self.gateway.ready, "shared_school_login": True,
                "login_url": LOGIN_URL,
                "message": ("请在统一文献浏览器完成一次北外登录；CNKI 和 WoS 共用学校会话。"
                            if stage == "webvpn_login" else
                            "已打开 WebVPN 知网页面，尚未确认北外机构身份；请完成页面上的登录或验证后重试。")}

    async def ensure(self, context):
        if self.context is not context:
            self.context = context
            self.ready = False
            self.stage = "not_checked"
            if self.page not in context.pages:
                self.page = None
        access = await self.gateway.ensure(context)
        if not access.get("success"):
            self.ready = False
            self.stage = access["authentication_stage"]
            if self.page is None or self.page.is_closed():
                self.page = self.gateway.login_page or self.gateway.page
            return {**access, "source": "cnki"}

        current = self.page is not None and not self.page.is_closed() and is_proxy_cnki(self.page.url)
        if not current:
            self.page = None
            for candidate in reversed(context.pages):
                if not candidate.is_closed() and is_proxy_cnki(candidate.url):
                    self.page = candidate
                    break
        if self.page is not None:
            # Preserve an unfinished CNKI verification, but revalidate a prior
            # success or an adopted old tab with a fresh resource request.
            if self.ready or not current or self.stage != "cnki_institution":
                await self.page.goto(self.home_url, wait_until="domcontentloaded", timeout=30000)
        else:
            self.page = self.gateway.page
        self.ready = False
        if is_portal(self.page.url):
            card = self.page.get_by_role("link").filter(has_text="中国知网").first
            if not await card.count():
                # The portal is an SPA; DOMContentLoaded precedes its cards.
                with contextlib.suppress(Exception):
                    await card.wait_for(state="visible", timeout=8000)
            if await card.count() and await card.is_visible():
                before = list(context.pages)
                entry = self.page
                await card.click(timeout=15000)
                for _ in range(20):
                    opened = [p for p in context.pages if p not in before and not p.is_closed()]
                    if opened:
                        self.page = opened[-1]
                        await self.page.wait_for_load_state("domcontentloaded", timeout=30000)
                        break
                    if is_proxy_cnki(self.page.url):
                        break
                    await self.page.wait_for_timeout(250)
                self.gateway.release_page(entry)

        if is_proxy_cnki(self.page.url):
            # Wait only for the institution badge, never inspect form values.
            for _ in range(12):
                badge = self.page.locator("#ecpHeaderContainer")
                if (await badge.count() and await badge.is_visible()
                        and INSTITUTION in await badge.inner_text(timeout=5000)
                        and not await contains_login_form(self.page)):
                    self.ready = True
                    self.stage = "institution_verified"
                    self._prompted_page = None
                    return {**access, "success": True, "authenticated": True,
                            "access_mode": "bfsu_webvpn", "institution": INSTITUTION,
                            "school_authenticated": True, "shared_school_login": True,
                            "message": "已通过北外 WebVPN 确认机构身份；可继续检索和尝试全文下载。"}
                await self.page.wait_for_timeout(500)
            await self._show_once()
            return self.required("cnki_institution")
        await self._show_once()
        if is_school_login(self.page.url):
            # The ticket can expire between the portal check and resource entry.
            access = await self.gateway.ensure(context)
            if not access.get("success"):
                self.stage = access["authentication_stage"]
                return {**access, "source": "cnki"}
        return self.required("cnki_institution")
