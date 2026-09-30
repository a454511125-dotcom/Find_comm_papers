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


class BfsuWebVPN:
    """Resumable login on a dedicated page; callers never wait for user input."""

    def __init__(self):
        self.page = None
        self.ready = False
        self.home_url = proxy_url("https://www.cnki.net/")
        self._prompted_page = None

    async def _show_once(self):
        if self.page is not self._prompted_page:
            await self.page.bring_to_front()
            self._prompted_page = self.page

    def status(self):
        return {"access_mode": "bfsu_webvpn", "login_url": LOGIN_URL,
                "last_check_authenticated": self.ready,
                "browser_open": self.page is not None and not self.page.is_closed()}

    def required(self, stage="webvpn_login"):
        self.ready = False
        return {"success": False, "authentication_required": True,
                "access_mode": "bfsu_webvpn", "authentication_stage": stage,
                "login_url": LOGIN_URL,
                "message": ("请在 CNKI 专用浏览器完成北外统一身份认证；进入资源页后程序会点击“中国知网”。完成后重试原操作。"
                            if stage == "webvpn_login" else
                            "已打开 WebVPN 知网页面，尚未确认北外机构身份；请完成页面上的登录或验证后重试。")}

    async def ensure(self, context):
        if self.page is None or self.page.is_closed():
            self.page = await context.new_page()
            self.ready = False
            # /login may still show a login form even with a valid ticket.
            # The portal root validates an existing session and redirects an
            # unauthenticated browser into the normal login flow.
            await self.page.goto(ORIGIN + "/", wait_until="domcontentloaded", timeout=30000)
        elif self.ready:
            # Revalidate with the gateway before every search/download. A cookie
            # file or an old tab by itself is not proof of current authorization.
            self.ready = False
            await self.page.goto(self.home_url, wait_until="domcontentloaded", timeout=30000)

        # The user may have clicked CNKI themselves while the request was paused.
        if not is_proxy_cnki(self.page.url):
            for candidate in reversed(context.pages):
                if not candidate.is_closed() and is_proxy_cnki(candidate.url):
                    self.page = candidate
                    # Old CNKI content can survive a gateway logout. Revalidate.
                    await self.page.goto(self.home_url, wait_until="domcontentloaded", timeout=30000)
                    break

        parsed = urlsplit(self.page.url)
        if parsed.hostname == "webvpn.bfsu.edu.cn" and parsed.path == "/":
            card = self.page.get_by_role("link").filter(has_text="中国知网").first
            if not await card.count():
                # The portal is an SPA; DOMContentLoaded precedes its cards.
                with contextlib.suppress(Exception):
                    await card.wait_for(state="visible", timeout=8000)
            if await card.count() and await card.is_visible():
                before = list(context.pages)
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

        if is_proxy_cnki(self.page.url):
            # Wait only for the institution badge, never inspect form values.
            for _ in range(12):
                badge = self.page.locator("#ecpHeaderContainer")
                if (await badge.count() and await badge.is_visible()
                        and INSTITUTION in await badge.inner_text(timeout=5000)
                        and not await contains_login_form(self.page)):
                    self.ready = True
                    self._prompted_page = None
                    return {"success": True, "authenticated": True,
                            "access_mode": "bfsu_webvpn", "institution": INSTITUTION,
                            "message": "已通过北外 WebVPN 确认机构身份；可继续检索和尝试全文下载。"}
                await self.page.wait_for_timeout(500)
            await self._show_once()
            return self.required("cnki_institution")
        await self._show_once()
        return self.required()
