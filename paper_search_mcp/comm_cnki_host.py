"""Minimal CNKI host, executed by the existing CNKI environment; no import/batch cleanup."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit

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
            await route.abort()


def main():
    session_path = Path(os.environ["COMM_CNKI_SESSION"])
    retain_license_signals(session_path)
    from cnki import browser
    from cnki import download as upstream_download
    from cnki import search as upstream_search
    from mcp.server.fastmcp import FastMCP

    original_launch = browser._cloak_launch_persistent_context_async
    if original_launch is None:
        raise RuntimeError("The existing CloakBrowser environment is required")
    async def launch(*args, **kwargs):
        kwargs["accept_downloads"] = False
        kwargs["service_workers"] = "block"
        artifacts = session_path / "browser-artifacts"
        artifacts.mkdir(parents=True, exist_ok=True)
        kwargs["artifacts_dir"] = str(artifacts)
        return await original_launch(*args, **kwargs)
    browser._cloak_launch_persistent_context_async = launch
    browser._USE_CLOAKBROWSER = True
    retained = RetainedResponses(Path(os.environ["PDF_DIR"]))
    active_context = None
    pending_search = None

    async def requires_verification(page):
        return await visible_challenge(page, [".tencent-captcha-dy__header-text",
            ".tencent-captcha-dy__footer-title", *upstream_download._CAPTCHA_SELECTORS])

    async def context():
        nonlocal active_context
        ctx = await browser.get_context()
        if ctx is not active_context:
            await ctx.route("**/*", retained.route)
            active_context = ctx
            def closed(*_):
                nonlocal active_context, pending_search
                if active_context is ctx:
                    active_context = None
                    pending_search = None
                if browser._context is ctx:
                    browser._context = None
            ctx.on("close", closed)
            # Seed once, so a manual login/verification is not overwritten by older cookies.
            cookies_path = os.environ.get("COMM_CNKI_ORIGINAL_COOKIES")
            if cookies_path and Path(cookies_path).is_file():
                await ctx.add_cookies(json.loads(Path(cookies_path).read_text(encoding="utf-8")))
        return ctx

    @contextlib.asynccontextmanager
    async def lifespan(_):
        try:
            yield {}
        finally:
            await browser.close_context()

    mcp = FastMCP("comm_cnki_backend", lifespan=lifespan)

    @mcp.tool()
    async def cnki_search(query: str, year_start: int, year_end: int, max_results: int, db_code: str = "CJFD") -> dict:
        nonlocal pending_search
        query = single_keyword(query)
        page, keep_open = None, False
        try:
            with contextlib.redirect_stdout(sys.stderr):
                ctx = await context()
                retained.errors.clear()
                if pending_search and pending_search[1] == query and not pending_search[0].is_closed():
                    page, submitted = pending_search[0], True
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
                        for marker in upstream_search._RESULT_MARKERS:
                            if await page.locator(marker).count():
                                reached = True
                                break
                        if reached or await requires_verification(page):
                            break
                        await page.wait_for_timeout(1000)
                # The current homepage can open /search with a prefilled keyword
                # without issuing a result request. Submit the actual search-page form once.
                if not reached and not await requires_verification(page) and await page.locator("input#txt_search").count():
                    reached = await upstream_search._fill_and_search(page, query)
                if not reached:
                    captcha = await requires_verification(page)
                    keep_open = True
                    pending_search = (page, query)
                    snapshot = session_path / ("search-page-" + uuid.uuid4().hex + ".html")
                    with snapshot.open("x", encoding="utf-8") as stream:
                        stream.write(await page.content())
                    return {"success": False, "papers": [], "captcha": captcha,
                            "message": "知网未识别到结果表格；" + ("请在打开的浏览器手动完成验证后重试" if captcha else "已保留当前页面，请检查登录或页面加载情况"),
                            "network_diagnostics": retained.errors[-5:], "page_title": (await page.title())[:120],
                            "diagnostic_snapshot": str(snapshot)}
                await upstream_search._restrict_to_db(page, db_code)
                sort = page.locator("#orderList #FFD")
                relevance_sorted = False
                if await sort.count() and await sort.is_visible():
                    await sort.click(timeout=15000)
                    await page.wait_for_timeout(4500)
                    relevance_sorted = True
                pending_search = None
                raw, papers, in_years, pages_read = [], [], 0, 0
                previous = None
                for page_index in range(5):
                    current = []
                    for _ in range(10):
                        current = await upstream_search._extract_rows(page, 200)
                        signature = tuple(p["title"] for p in current)
                        if current and signature != previous:
                            break
                        await page.wait_for_timeout(1000)
                    if not current or signature == previous:
                        break
                    previous = signature
                    pages_read += 1
                    raw.extend(current)
                    eligible = upstream_search._filter_by_year(current, year_start, year_end, len(current) + 1)
                    in_years += len(eligible)
                    if db_code == "CJFD":
                        eligible = upstream_search._filter_by_journal(eligible)
                    papers.extend(eligible)
                    if len(papers) >= max_results or page_index == 4:
                        break
                    next_page = page.locator("#PageNext")
                    if not await next_page.count() or not await next_page.is_visible():
                        break
                    await next_page.click(timeout=15000)
                    await page.wait_for_timeout(1500)
                papers = papers[:max_results]
                for paper in papers:
                    paper["extra"] = {**paper.get("extra", {}), "cnki_referer": page.url}
                if not papers:
                    snapshot = session_path / ("empty-results-" + uuid.uuid4().hex + ".html")
                    with snapshot.open("x", encoding="utf-8") as stream:
                        stream.write(await page.content())
                return {"success": True, "papers": papers, "count": len(papers),
                        "diagnostics": {"extracted":len(raw), "within_years":in_years,
                            "retained":len(papers), "relevance_sorted":relevance_sorted,
                            "pages_read":pages_read, "page_limit":5,
                            "snapshot":str(snapshot) if not papers else None},
                        "message": "期刊检索沿用知网传播学白名单；按相关度最多读取5页，未满额不表示没有其他相关文献。"}
        except Exception as exc:
            code = re.search(r"(?:net::)?ERR_[A-Z_]+|CERT_[A-Z_]+", str(exc))
            return {"success": False, "papers": [], "message": "CNKI search failed: " + (code[0] if code else type(exc).__name__),
                    "network_diagnostics": retained.errors[-5:]}
        finally:
            if page is not None and not keep_open:
                await page.close()

    @mcp.tool()
    async def cnki_download(detail_url: str, title: str, referer: str = "") -> dict:
        host = (urlsplit(detail_url).hostname or "").lower()
        if urlsplit(detail_url).scheme not in {"http", "https"} or not (host == "cnki.net" or host.endswith(".cnki.net") or host.endswith(".cnki.com.cn")):
            return {"success": False, "message": "Expected a CNKI detail URL"}
        if referer and (urlsplit(referer).scheme not in {"http", "https"} or not (urlsplit(referer).hostname or "").endswith(".cnki.net")):
            return {"success": False, "message": "Expected a CNKI source page"}
        ctx = await context()
        page = await ctx.new_page()
        keep_open = False
        retained.files.clear()  # Memory only; retained files are never removed.
        retained.arrived.clear()
        try:
            await page.goto(detail_url, referer=referer or CNKI_HOME, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2000)
            if await requires_verification(page):
                keep_open = True
                await page.bring_to_front()
                prefix = session_path / ("download-verification-" + uuid.uuid4().hex)
                with prefix.with_suffix(".html").open("x", encoding="utf-8") as stream:
                    stream.write(await page.content())
                await page.screenshot(path=str(prefix.with_suffix(".png")))
                body_text = await page.locator("body").inner_text()
                blank = len(body_text.strip()) < 20
                return {"success": False, "captcha": not blank, "blank_verification_page": blank,
                        "message": "知网返回空白验证页，需要检查访问会话或页面加载" if blank else "请在知网浏览器中手动完成验证后重试",
                        "diagnostic_snapshot":str(prefix.with_suffix(".html")), "screenshot":str(prefix.with_suffix(".png")),
                        "page_title":await page.title()}
            button, _ = await upstream_download._find_download_btn(page)
            if button is None:
                return {"success": False, "message": "未找到全文下载按钮；请检查机构登录及全文权限"}
            try:
                await button.click(timeout=20000)
            except Exception:
                if not retained.arrived.is_set():
                    raise
            await asyncio.wait_for(retained.arrived.wait(), timeout=50)
            return retained.files[-1]
        except Exception as exc:
            keep_open = await requires_verification(page)
            return {"success": False, "captcha": keep_open,
                    "message": "CNKI response retrieval failed: " + type(exc).__name__}
        finally:
            if not keep_open:
                await page.close()

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
