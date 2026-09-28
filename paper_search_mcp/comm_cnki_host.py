"""CNKI runtime called directly inside the single Find_comm_papers process.

The DOM helpers live in paper_search_mcp.cnki; this module has no MCP server,
stdio client, or dependency on another CNKI installation. Browser imports are
lazy so English discovery remains usable without a running browser.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import re
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from .cnki.browser import BrowserConfigurationError, BrowserSession
from .cnki import download as cnki_download
from .cnki import search as cnki_search

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
    def __init__(self, root):
        self.root = root
        self.files = []
        self.errors = []
        self.arrived = asyncio.Event()

    async def route(self, route):
        request = route.request
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
        self.browser = BrowserSession(self.session_path, browser_binary, seed_cookie_file)
        self.retained = RetainedResponses(self.session_path / "downloads")
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

    async def _verification_result(self, page, stage):
        """Preserve visible evidence and distinguish an empty challenge page."""
        await page.bring_to_front()
        prefix = self.session_path / (stage + "-verification-" + uuid.uuid4().hex)
        with prefix.with_suffix(".html").open("x", encoding="utf-8") as stream:
            stream.write(await page.content())
        await page.screenshot(path=str(prefix.with_suffix(".png")))
        body_text = await page.locator("body").inner_text()
        blank = len(body_text.strip()) < 20
        return {
            "success": False, "captcha": not blank, "blank_verification_page": blank,
            "message": "知网返回空白验证页，需要检查访问会话或页面加载" if blank
                       else "请在知网浏览器中手动完成验证后重试",
            "diagnostic_snapshot": str(prefix.with_suffix(".html")),
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
            self.retained.errors.clear()
            if self._pending_search and self._pending_search[1] == query and not self._pending_search[0].is_closed():
                page, submitted = self._pending_search[0], True
            else:
                page = await ctx.new_page()
                await page.goto(CNKI_HOME, wait_until="domcontentloaded", timeout=25000)
                await page.wait_for_timeout(3500)
                home_page = page
                page, submitted = await submit_homepage(page, ctx, query)
                if page is not home_page:
                    await home_page.close()
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
                captcha = await self._requires_verification(page)
                keep_open = True
                self._pending_search = (page, query)
                if captcha:
                    return {**await self._verification_result(page, "search"), "papers": [],
                            "network_diagnostics": self.retained.errors[-5:]}
                snapshot = self.session_path / ("search-page-" + uuid.uuid4().hex + ".html")
                with snapshot.open("x", encoding="utf-8") as stream:
                    stream.write(await page.content())
                return {"success": False, "papers": [], "captcha": captcha,
                        "message": "知网未识别到结果表格；" + ("请在打开的浏览器手动完成验证后重试" if captcha else "已保留当前页面，请检查登录或页面加载情况"),
                        "network_diagnostics": self.retained.errors[-5:], "page_title": (await page.title())[:120],
                        "diagnostic_snapshot": str(snapshot)}
            await cnki_search._restrict_to_db(page, db_code)
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
                    paper["extra"] = {**paper.get("extra", {}), "cnki_referer": page.url}
                papers.extend(eligible)
                if len(papers) >= max_results or page_index == 4:
                    break
                next_page = page.locator("#PageNext")
                if not await next_page.count() or not await next_page.is_visible():
                    break
                await next_page.click(timeout=15000)
                await page.wait_for_timeout(1500)
            papers = papers[:max_results]
            if not papers:
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
        host = (urlsplit(detail_url).hostname or "").lower()
        if urlsplit(detail_url).scheme not in {"http", "https"} or not (host == "cnki.net" or host.endswith(".cnki.net") or host.endswith(".cnki.com.cn")):
            return {"success": False, "message": "Expected a CNKI detail URL"}
        referer_host = (urlsplit(referer).hostname or "").lower()
        if referer and (urlsplit(referer).scheme not in {"http", "https"} or not (referer_host == "cnki.net" or referer_host.endswith(".cnki.net"))):
            return {"success": False, "message": "Expected a CNKI source page"}
        page = None
        keep_open = False
        self.retained.files.clear()  # Memory only; retained files are never removed.
        self.retained.arrived.clear()
        try:
            ctx = await self._context()
            if self._pending_download and self._pending_download[1] == detail_url and not self._pending_download[0].is_closed():
                page = self._pending_download[0]
            else:
                page = await ctx.new_page()
                await page.goto(detail_url, referer=referer or CNKI_HOME, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(2000)
            if await self._requires_verification(page):
                keep_open = True
                self._pending_download = (page, detail_url)
                return await self._verification_result(page, "download")
            self._pending_download = None
            button, _ = await cnki_download._find_download_btn(page)
            if button is None:
                return {"success": False, "message": "未找到全文下载按钮；请检查机构登录及全文权限"}
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
                    keep_open = await self._requires_verification(page)
            if keep_open:
                self._pending_download = (page, detail_url)
                return await self._verification_result(page, "download")
            return {"success": False, "captcha": keep_open,
                    "message": "CNKI response retrieval failed: " + type(exc).__name__}
        finally:
            with contextlib.suppress(Exception):
                await self.browser.save_cookies()
            if page is not None and not keep_open:
                with contextlib.suppress(Exception):
                    await page.close()

