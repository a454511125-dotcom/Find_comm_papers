"""CNKI runtime called directly inside the single Find_comm_papers process.

The DOM helpers live in paper_search_mcp.cnki; this module has no MCP server,
stdio client, or dependency on another CNKI installation. Browser imports are
lazy so English discovery remains usable without a running browser.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import re
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from .cnki.browser import BrowserConfigurationError, BrowserSession
from .cnki import download as cnki_download
from .cnki import search as cnki_search
from .cnki.webvpn import BfsuWebVPN, canonical_url, proxy_url, is_proxy_cnki, cnki_host, contains_login_form
from .config import get_env

CNKI_HOME = "https://www.cnki.net/"


def visible_geometry(state):
    if any(s.get("display") == "none" or s.get("visibility") in {"hidden", "collapse"}
           or float(s.get("opacity", 1)) <= 0 for s in state["ancestors"]):
        return False
    x, y, width, height = state["rect"]
    vw, vh = state["viewport"]
    return width > 0 and height > 0 and x < vw and y < vh and x + width > 0 and y + height > 0


async def visible_challenge(page, selectors):
    if re.search("安全验证|访问验证|人机验证|验证码", await page.title()):
        return True
    for selector in selectors:
        elements = page.locator(selector)
        for index in range(await elements.count()):
            state = await elements.nth(index).evaluate("""el => {
                const ancestors = [];
                for (let node = el; node; node = node.parentElement) {
                    const s = getComputedStyle(node);
                    ancestors.push({display:s.display, visibility:s.visibility, opacity:s.opacity});
                }
                const r = el.getBoundingClientRect();
                return {ancestors, rect:[r.x,r.y,r.width,r.height], viewport:[innerWidth,innerHeight]};
            }""")
            if visible_geometry(state):
                return True
    return False


def single_keyword(query):
    """One explicit keyword/concept phrase per CNKI request; never silently rewrite it."""
    keyword = query.strip()
    if not keyword or re.search(r"\s|[,，;；、+|&]|\b(?:AND|OR|NOT)\b", keyword, re.I):
        raise ValueError("chinese_query must contain one keyword/concept, such as 算法推荐; run 政治极化 separately")
    return keyword


async def submit_homepage(page, context, query):
    """Use the current homepage form and follow its own same-tab or popup navigation."""
    field = page.locator("textarea#txt_SearchText")
    if not await field.count():
        return page, False
    await field.fill(query)
    before = list(context.pages)
    await page.locator(".search-form .search-btn").first.click(timeout=15000)
    await page.wait_for_timeout(5000)
    opened = [p for p in context.pages if p not in before]
    target = opened[-1] if opened else page
    if target is not page:
        await target.wait_for_load_state("domcontentloaded", timeout=25000)
    return target, True


def retain_license_signals(session_path):
    import cloakbrowser.license as license_module
    import cloakbrowser.browser as browser_module
    def read_denial_file(path):
        cached = license_module._OBSERVED_DENIALS.get(path)
        if cached is not None:
            return cached
        try:
            code = int(json.loads(Path(path).read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            return None
        license_module._OBSERVED_DENIALS[path] = code
        return code
    def mint_denial_file():
        directory = session_path / "license-signals"
        directory.mkdir(parents=True, exist_ok=True)
        return str(directory / (uuid.uuid4().hex + ".json"))
    license_module.read_denial_file = browser_module.read_denial_file = read_denial_file
    license_module.mint_denial_file = browser_module.mint_denial_file = mint_denial_file
    license_module._sweep_stale_denials = lambda _: None


class RetainedResponses:
    """Capture file responses in memory before Chromium receives them; no Download objects."""
    def __init__(self, root, webvpn_enabled=False):
        self.root = root
        self.webvpn_enabled = webvpn_enabled
        self.capture_enabled = not webvpn_enabled
        self.files = []
        self.errors = []
        self.arrived = asyncio.Event()
        self._cdp_sessions = {}

    @staticmethod
    def _binary_headers(headers):
        mime = headers.get("content-type", "").lower()
        disposition = headers.get("content-disposition", "").lower()
        return ("attachment" in disposition or any(kind in mime for kind in
                ("application/pdf", "application/octet-stream", "application/caj", "application/force-download")))

    def _retain_body(self, headers, body, signal=True):
        mime = headers.get("content-type", "").lower()
        disposition = headers.get("content-disposition", "").lower()
        if len(body) > 50 * 1024 * 1024:
            self.files.append({"success": False, "message": "File exceeds 50 MB"})
        else:
            kind = "pdf" if b"%PDF-" in body[:1024] else "caj" if "caj" in mime or ".caj" in disposition or body.startswith(b"CAJ") else "bin"
            self.root.mkdir(parents=True, exist_ok=True)
            path = self.root / ("cnki_" + uuid.uuid4().hex + "." + kind)
            with path.open("xb") as stream:
                stream.write(body)
            self.files.append({"success": kind in {"pdf", "caj"}, "format": kind, "file_path": str(path),
                               "message": "Native browser response retained without a download artifact"})
        if signal:
            self.arrived.set()

    async def arm_native(self, context, page):
        """Intercept file responses after Chromium has performed authenticated I/O.

        WebVPN's HTML/session rewriting must not be replayed through route.fetch.
        CDP response interception also avoids Playwright's auto-deleted Download
        artifacts. Only headers and actual file bodies are inspected.
        """
        self.capture_enabled = True

        async def attach(page):
            try:
                session = await context.new_cdp_session(page)
                self._cdp_sessions[page] = session

                async def paused(event):
                    request_id = event["requestId"]
                    finished = False
                    try:
                        headers = {item["name"].lower(): item["value"] for item in event.get("responseHeaders", [])}
                        url = event.get("request", {}).get("url", "")
                        if (self.capture_enabled and is_proxy_cnki(url)
                                and self._binary_headers(headers)):
                            # Once recognized as a file, never continue it to a
                            # browser Download artifact, including error paths.
                            finished = True
                            try:
                                if int(headers.get("content-length", "0") or 0) > 50 * 1024 * 1024:
                                    self.files.append({"success": False, "message": "File exceeds 50 MB"})
                                else:
                                    result = await session.send("Fetch.getResponseBody", {"requestId": request_id})
                                    body = base64.b64decode(result["body"]) if result.get("base64Encoded") else result["body"].encode("utf-8")
                                    self._retain_body(headers, body, signal=False)
                            except Exception as exc:
                                self.files.append({"success": False, "message": "Native response retention failed: " + type(exc).__name__})
                            finally:
                                try:
                                    await session.send("Fetch.failRequest", {"requestId": request_id, "errorReason": "Aborted"})
                                except Exception:
                                    # This is the provider's own download tab.
                                    with contextlib.suppress(Exception):
                                        await page.close()
                                self.arrived.set()
                    except Exception as exc:
                        self.errors.append({"error": "native_capture_" + type(exc).__name__})
                    finally:
                        if not finished:
                            with contextlib.suppress(Exception):
                                await session.send("Fetch.continueRequest", {"requestId": request_id})

                session.on("Fetch.requestPaused", paused)
                await session.send("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Response"}]})
            except Exception as exc:
                raise BrowserConfigurationError("Unable to attach native CNKI download capture: " + type(exc).__name__) from exc
        # The PDF anchor is set to _self before clicking. Attach synchronously
        # to this download tab only, avoiding both popup races and other tabs.
        await attach(page)

    async def disarm_native(self):
        self.capture_enabled = False
        sessions, self._cdp_sessions = self._cdp_sessions, {}
        for session in sessions.values():
            with contextlib.suppress(Exception):
                await session.send("Fetch.disable")
            with contextlib.suppress(Exception):
                await session.detach()

    async def route(self, route):
        request = route.request
        # WebVPN uses arm_native(), never HTTP request replay.
        if self.webvpn_enabled:
            await route.continue_()
            return
        # Login/SSO requests belong to Chromium. Never inspect or retain their
        # form bodies or responses, and let browser redirects update the URL.
        host = urlsplit(request.url).hostname or ""
        if host == "bfsu.edu.cn" or host.endswith(".bfsu.edu.cn") and not is_proxy_cnki(request.url):
            await route.continue_()
            return
        if request.resource_type not in {"document", "xhr", "fetch", "other"}:
            await route.continue_()
            return
        try:
            response = await route.fetch(timeout=45000)
            headers = {k.lower(): v for k, v in response.headers.items()}
            mime = headers.get("content-type", "").lower()
            disposition = headers.get("content-disposition", "").lower()
            binary = ("attachment" in disposition or "application/pdf" in mime or "application/octet-stream" in mime
                      or "application/caj" in mime or "application/force-download" in mime)
            if binary:
                if int(headers.get("content-length", "0") or 0) > 50 * 1024 * 1024:
                    self.files.append({"success": False, "message": "File exceeds 50 MB"})
                else:
                    body = await response.body()
                    if len(body) > 50 * 1024 * 1024:
                        self.files.append({"success": False, "message": "File exceeds 50 MB"})
                    else:
                        kind = "pdf" if b"%PDF-" in body[:1024] else "caj" if "caj" in mime or ".caj" in disposition or body.startswith(b"CAJ") else "bin"
                        self.root.mkdir(parents=True, exist_ok=True)
                        path = self.root / ("cnki_" + uuid.uuid4().hex + "." + kind)
                        with path.open("xb") as stream:
                            stream.write(body)
                        self.files.append({"success": kind in {"pdf", "caj"}, "format": kind, "file_path": str(path),
                                           "message": "Response retained without a browser download artifact"})
                self.arrived.set()
                await route.abort()
            else:
                await route.fulfill(response=response)
        except Exception as exc:
            code = re.search(r"(?:net::)?ERR_[A-Z_]+|CERT_[A-Z_]+", str(exc))
            self.errors.append({"host": urlsplit(request.url).hostname,
                                "error": code[0] if code else type(exc).__name__})
            with contextlib.suppress(Exception):
                await route.abort()


class CNKIBackend:
    """A provider owned by Find_comm_papers, called on its internal event loop."""

    def __init__(self, session_path: Path, browser_binary: Path | None = None,
                 seed_cookie_file: Path | None = None):
        self.session_path = Path(session_path)
        mode = get_env("COMM_CNKI_ACCESS_MODE", "direct")
        if mode not in {"direct", "bfsu_webvpn"}:
            raise BrowserConfigurationError("COMM_CNKI_ACCESS_MODE must be direct or bfsu_webvpn")
        self.browser = BrowserSession(self.session_path, browser_binary, seed_cookie_file,
                                      webvpn_enabled=mode == "bfsu_webvpn")
        self.webvpn = BfsuWebVPN(self.browser.webvpn_gateway) if mode == "bfsu_webvpn" else None
        self.retained = RetainedResponses(self.session_path / "downloads", webvpn_enabled=self.webvpn is not None)
        self._active_context = None
        self._pending_search = None
        self._pending_download = None

    async def _requires_verification(self, page):
        return await visible_challenge(page, [
            ".tencent-captcha-dy__header-text", ".tencent-captcha-dy__footer-title",
            *cnki_download._CAPTCHA_SELECTORS,
        ])

    async def _context(self):
        ctx = await self.browser.get_context()
        if ctx is not self._active_context:
            if not self.webvpn:
                await ctx.route("**/*", self.retained.route)
            self._active_context = ctx

            def closed(*_):
                if self._active_context is ctx:
                    self._active_context = None
                    self._pending_search = None
                    self._pending_download = None

            ctx.on("close", closed)
        return ctx

    async def close(self):
        await self.browser.close()
        self._active_context = None
        self._pending_search = None
        self._pending_download = None

    async def access_status(self):
        if not self.webvpn:
            return {"access_mode": "direct", "authentication_required": False,
                    "message": "当前使用知网直连；校外可配置 COMM_CNKI_ACCESS_MODE=bfsu_webvpn。"}
        return {**self.webvpn.status(),
                "session_persistence": dict(self.browser.session_persistence_status),
                "cached_session_present": self.browser.webvpn_cookie_file.is_file(),
                "message": "这是最近一次认证状态；调用 comm_cnki_authenticate 可实时检查或打开登录页。"}

    async def authenticate(self):
        if not self.webvpn:
            return await self.access_status()
        try:
            result = await self.webvpn.ensure(await self._context())
            return {**result, "session_persistence": dict(self.browser.session_persistence_status)}
        except BrowserConfigurationError as exc:
            return {"success": False, "message": str(exc)}
        except Exception as exc:
            return {"success": False, "access_mode": "bfsu_webvpn",
                    "message": "WebVPN authentication check failed: " + type(exc).__name__}

    async def _prepare_access(self, ctx):
        if self.webvpn:
            result = await self.webvpn.ensure(ctx)
            if not result.get("success"):
                self._pending_search = None
                self._pending_download = None
                return result
        return None

    def _access_url(self, url):
        return proxy_url(url) if self.webvpn else canonical_url(url)

    async def _lost_access(self, page):
        login_form = await contains_login_form(page)
        if self.webvpn and (not is_proxy_cnki(page.url) or login_form):
            self.webvpn.page = page
            access = await self.browser.webvpn_gateway.ensure(await self._context())
            if not access.get("success"):
                self.webvpn.ready = False
                return {**access, "source": "cnki"}
            await page.bring_to_front()
            return self.webvpn.required("cnki_institution")
        if login_form:
            await page.bring_to_front()
            return {"success": False, "authentication_required": True,
                    "message": "请在知网浏览器完成登录后重试；登录表单不会保存为诊断快照。"}
        return None

    async def _verification_result(self, page, stage):
        """Preserve visible evidence and distinguish an empty challenge page."""
        required = await self._lost_access(page)
        if required:
            return required
        await page.bring_to_front()
        prefix = self.session_path / (stage + "-verification-" + uuid.uuid4().hex)
        snapshot = None
        if not self.webvpn:
            snapshot = str(prefix.with_suffix(".html"))
            with prefix.with_suffix(".html").open("x", encoding="utf-8") as stream:
                stream.write(await page.content())
        await page.screenshot(path=str(prefix.with_suffix(".png")))
        body_text = await page.locator("body").inner_text()
        blank = len(body_text.strip()) < 20
        return {
            "success": False, "captcha": not blank, "blank_verification_page": blank,
            "message": "知网返回空白验证页，需要检查访问会话或页面加载" if blank
                       else "请在知网浏览器中手动完成验证后重试",
            "diagnostic_snapshot": snapshot,
            "screenshot": str(prefix.with_suffix(".png")), "page_title": await page.title(),
        }

    async def search(self, query: str, year_start: int, year_end: int, max_results: int, db_code: str = "CJFD") -> dict:
        query = single_keyword(query)
        if year_start > year_end or max_results < 1:
            raise ValueError("Expected a valid year range and positive max_results")
        if db_code not in {"CJFD", "CDFD", "CMFD"}:
            raise ValueError("db_code must be CJFD, CDFD, or CMFD")
        page, keep_open = None, False
        try:
            ctx = await self._context()
            required = await self._prepare_access(ctx)
            if required:
                return {**required, "papers": []}
            self.retained.errors.clear()
            if self._pending_search and self._pending_search[1] == query and not self._pending_search[0].is_closed():
                page, submitted = self._pending_search[0], True
            else:
                page = await ctx.new_page()
                await page.goto(self._access_url(CNKI_HOME), wait_until="domcontentloaded", timeout=25000)
                required = await self._lost_access(page)
                if required:
                    keep_open = True
                    return {**required, "papers": []}
                await page.wait_for_timeout(3500)
                home_page = page
                page, submitted = await submit_homepage(page, ctx, query)
                if page is not home_page:
                    await home_page.close()
            required = await self._lost_access(page)
            if required:
                keep_open = True
                return {**required, "papers": []}
            reached = False
            if submitted:
                for _ in range(5):
                    for marker in cnki_search._RESULT_MARKERS:
                        if await page.locator(marker).count():
                            reached = True
                            break
                    if reached or await self._requires_verification(page):
                        break
                    await page.wait_for_timeout(1000)
            # The current homepage can open /search with a prefilled keyword
            # without issuing a result request. Submit the actual search-page form once.
            if not reached and not await self._requires_verification(page) and await page.locator("input#txt_search").count():
                reached = await cnki_search._fill_and_search(page, query)
            if not reached:
                required = await self._lost_access(page)
                if required:
                    keep_open = True
                    return {**required, "papers": []}
                captcha = await self._requires_verification(page)
                keep_open = True
                self._pending_search = (page, query)
                if captcha:
                    return {**await self._verification_result(page, "search"), "papers": [],
                            "network_diagnostics": self.retained.errors[-5:]}
                snapshot = None
                if not self.webvpn:
                    snapshot = self.session_path / ("search-page-" + uuid.uuid4().hex + ".html")
                    with snapshot.open("x", encoding="utf-8") as stream:
                        stream.write(await page.content())
                return {"success": False, "papers": [], "captcha": captcha,
                        "message": "知网未识别到结果表格；" + ("请在打开的浏览器手动完成验证后重试" if captcha else "已保留当前页面，请检查登录或页面加载情况"),
                        "network_diagnostics": self.retained.errors[-5:], "page_title": (await page.title())[:120],
                        "diagnostic_snapshot": str(snapshot) if snapshot else None}
            await cnki_search._restrict_to_db(page, db_code)
            required = await self._lost_access(page)
            if required:
                keep_open = True
                return {**required, "papers": []}
            sort = page.locator("#orderList #FFD")
            relevance_sorted = False
            if await sort.count() and await sort.is_visible():
                await sort.click(timeout=15000)
                await page.wait_for_timeout(4500)
                relevance_sorted = True
            self._pending_search = None
            raw, papers, in_years, pages_read = [], [], 0, 0
            previous = None
            for page_index in range(5):
                required = await self._lost_access(page)
                if required:
                    keep_open = True
                    return {**required, "papers": []}
                current = []
                for _ in range(10):
                    current = await cnki_search._extract_rows(page, 200)
                    signature = tuple(p["title"] for p in current)
                    if current and signature != previous:
                        break
                    await page.wait_for_timeout(1000)
                if not current or signature == previous:
                    break
                previous = signature
                pages_read += 1
                raw.extend(current)
                eligible = cnki_search._filter_by_year(current, year_start, year_end, len(current) + 1)
                in_years += len(eligible)
                if db_code == "CJFD":
                    eligible = cnki_search._filter_by_journal(eligible)
                for paper in eligible:
                    paper["extra"] = {**paper.get("extra", {}), "cnki_referer": canonical_url(page.url),
                                      "cnki_access_mode": "bfsu_webvpn" if self.webvpn else "direct"}
                papers.extend(eligible)
                if len(papers) >= max_results or page_index == 4:
                    break
                next_page = page.locator("#PageNext")
                if not await next_page.count() or not await next_page.is_visible():
                    break
                await next_page.click(timeout=15000)
                await page.wait_for_timeout(1500)
            papers = papers[:max_results]
            required = await self._lost_access(page)
            if required:
                keep_open = True
                return {**required, "papers": []}
            snapshot = None
            if not papers and not self.webvpn:
                snapshot = self.session_path / ("empty-results-" + uuid.uuid4().hex + ".html")
                with snapshot.open("x", encoding="utf-8") as stream:
                    stream.write(await page.content())
            return {"success": True, "papers": papers, "count": len(papers),
                    "diagnostics": {"extracted":len(raw), "within_years":in_years,
                        "retained":len(papers), "relevance_sorted":relevance_sorted,
                        "pages_read":pages_read, "page_limit":5,
                        "snapshot":str(snapshot) if not papers else None},
                    "message": "期刊检索沿用知网传播学白名单；按相关度最多读取5页，未满额不表示没有其他相关文献。"}
        except BrowserConfigurationError as exc:
            return {"success": False, "papers": [], "message": str(exc)}
        except Exception as exc:
            code = re.search(r"(?:net::)?ERR_[A-Z_]+|CERT_[A-Z_]+", str(exc))
            return {"success": False, "papers": [], "message": "CNKI search failed: " + (code[0] if code else type(exc).__name__),
                    "network_diagnostics": self.retained.errors[-5:]}
        finally:
            with contextlib.suppress(Exception):
                await self.browser.save_cookies()
            if page is not None and not keep_open:
                with contextlib.suppress(Exception):
                    await page.close()


    async def download(self, detail_url: str, title: str, referer: str = "") -> dict:
        try:
            detail_url = canonical_url(detail_url)
            referer = canonical_url(referer or CNKI_HOME)
        except (ValueError, TypeError):
            return {"success": False, "message": "Expected a CNKI URL or a BFSU proxy URL targeting CNKI"}
        page = None
        keep_open = False
        self.retained.files.clear()  # Memory only; retained files are never removed.
        self.retained.arrived.clear()
        try:
            ctx = await self._context()
            required = await self._prepare_access(ctx)
            if required:
                return required
            if self._pending_download and self._pending_download[1] == detail_url and not self._pending_download[0].is_closed():
                page = self._pending_download[0]
            else:
                page = await ctx.new_page()
                await page.goto(self._access_url(detail_url), referer=self._access_url(referer), wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(2000)
            required = await self._lost_access(page)
            if required:
                keep_open = True
                return required
            if await self._requires_verification(page):
                keep_open = True
                self._pending_download = (page, detail_url)
                return await self._verification_result(page, "download")
            self._pending_download = None
            button, _ = await cnki_download._find_download_btn(page)
            if button is None:
                return {"success": False, "message": "未找到全文下载按钮；请检查机构登录及全文权限"}
            if self.webvpn:
                await button.evaluate("el => el.setAttribute('target', '_self')")
                await self.retained.arm_native(ctx, page)
            else:
                self.retained.capture_enabled = True
            try:
                await button.click(timeout=20000)
            except Exception:
                if not self.retained.arrived.is_set():
                    raise
            await asyncio.wait_for(self.retained.arrived.wait(), timeout=50)
            return self.retained.files[-1]
        except BrowserConfigurationError as exc:
            return {"success": False, "message": str(exc)}
        except Exception as exc:
            if page is not None:
                with contextlib.suppress(Exception):
                    required = await self._lost_access(page)
                    if required:
                        keep_open = True
                        return required
            if page is not None:
                with contextlib.suppress(Exception):
                    keep_open = await self._requires_verification(page)
            if keep_open:
                self._pending_download = (page, detail_url)
                return await self._verification_result(page, "download")
            return {"success": False, "captcha": keep_open,
                    "message": "CNKI response retrieval failed: " + type(exc).__name__,
                    "network_diagnostics": self.retained.errors[-5:]}
        finally:
            if self.webvpn:
                await self.retained.disarm_native()
            with contextlib.suppress(Exception):
                await self.browser.save_cookies()
            if page is not None and not keep_open:
                with contextlib.suppress(Exception):
                    await page.close()

