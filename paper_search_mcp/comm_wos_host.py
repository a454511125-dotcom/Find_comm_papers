"""Web of Science via BFSU's visible, user-authenticated library browser.

Searches and exports use the ordinary UI. No private search API or passwords.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import html
import re
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

from .cnki.webvpn import ORIGIN, LOGIN_URL, PUBLIC_ADDRESS_KEY, _cipher, contains_login_form
from .comm_ranking import canonical


def upstream_url(url):
    """Decode a public Wengine address; accept only normal public DNS URLs."""
    if not isinstance(url, str) or any(c in url for c in "\\\r\n\t"):
        raise ValueError("Invalid resource URL")
    p = urlsplit(url)
    if p.scheme not in {"http", "https"} or p.username or p.password or p.port not in {None, 80, 443}:
        raise ValueError("Invalid resource URL")
    if p.hostname == "webvpn.bfsu.edu.cn":
        m = re.fullmatch(r"/(https?)/([0-9a-f]+)(/.*)?", p.path)
        if not m or not m[2].startswith(PUBLIC_ADDRESS_KEY.hex()):
            raise ValueError("Not a resource URL")
        host = _cipher().decryptor().update(bytes.fromhex(m[2][32:])).decode("ascii")
        p = urlsplit(urlunsplit((m[1], host, m[3] or "/", p.query, p.fragment)))
    host = p.hostname or ""
    if not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", host) or re.fullmatch(r"[0-9.]+", host):
        raise ValueError("Expected a DNS resource hostname")
    if host.endswith((".localhost", ".local", ".internal")):
        raise ValueError("Private resource hostname")
    return urlunsplit((p.scheme, host, p.path or "/", p.query, p.fragment))


def resource_url(url):
    p = urlsplit(upstream_url(url))
    token = PUBLIC_ADDRESS_KEY.hex() + _cipher().encryptor().update(p.hostname.encode("ascii")).hex()
    return urlunsplit(("https", "webvpn.bfsu.edu.cn", f"/{p.scheme}/{token}{p.path}", p.query, p.fragment))


def wos_url(url):
    p = urlsplit(upstream_url(url))
    if not any(p.hostname == root or p.hostname.endswith('.' + root)
               for root in ('webofscience.com', 'webofscience.clarivate.cn')):
        raise ValueError("Expected a Web of Science URL")
    return urlunsplit((p.scheme, p.hostname, p.path, p.query, p.fragment))


def is_wos(url):
    try:
        return bool(wos_url(url))
    except (ValueError, UnicodeError, TypeError):
        return False


def parse_export(text):
    """Parse full-record tagged text without mistaking line continuations for tags."""
    rows, row, tag = [], {}, None
    for line in text.lstrip("\ufeff").splitlines():
        if line == "ER":
            if row.get("TI") and row.get("UT"):
                rows.append(row)
            row, tag = {}, None
        elif line.startswith("   ") and tag:
            row[tag].append(line[3:].strip())
        elif re.match(r"^[A-Z0-9]{2} ", line):
            tag = line[:2]
            row.setdefault(tag, []).append(line[3:].strip())
    papers = []
    for item in rows:
        one = lambda key: " ".join(item.get(key, []))
        uid = one("UT")
        if not re.fullmatch(r"WOS:[A-Za-z0-9]+", uid):
            continue
        authors = item.get("AF") or item.get("AU") or item.get("BA") or []
        doi, year = one("DI"), one("PY")
        lang = one("LA")
        creators = [{"creatorType": "author", "lastName": name.split(',',1)[0].strip(),
                     "firstName": name.split(',',1)[1].strip()} if ',' in name else
                    {"creatorType": "author", "name": name} for name in authors]
        extra = {"wos_id": uid, "wos_language": lang, "volume": one("VL"), "creators": creators,
                 "issue": one("IS"), "page": "-".join(filter(None, [one("BP"), one("EP")])) or one("AR"),
                 "work_type": one("DT").casefold(), "wos_indexes": html.unescape(one("WE")),
                 "fulltext_via": "WoS record links -> publisher or BFSU SFX"}
        papers.append(canonical({"paper_id": uid, "source": "wos", "title": one("TI"),
            "authors": authors, "abstract": one("AB"), "doi": doi,
            "published_date": year, "venue": one("SO"), "pdf_url": "",
            "url": "https://www.webofscience.com/wos/woscc/full-record/" + uid,
            "language": "en" if lang.casefold() == "english" else lang.casefold(),
            "citations": int(one("TC")) if one("TC").isdigit() else 0,
            "keywords": [x.strip() for x in one("DE").split(";") if x.strip()],
            "categories": [x.strip() for x in one("WC").split(";") if x.strip()], "extra": extra}))
    return papers


def search_expression(query, year_start=None, year_end=None):
    if not isinstance(query, str) or not query.strip() or len(query) > 2000 or any(c in query for c in "\r\n"):
        raise ValueError("Supply one nonempty WoS topic expression (maximum 2000 characters)")
    start, end = year_start or 1800, year_end or datetime.now().year
    if not 1800 <= start <= end <= datetime.now().year + 1:
        raise ValueError("Invalid year range")
    # Query is a topic expression: Boolean operators/quotes remain user-controlled.
    return f"TS=({query.strip()}) AND PY=({start}-{end}) AND LA=(English)"


EXPORT_CAPTURE = r"""() => {
  const state = {text:null, error:null}; window.__commWosExport = state;
  const blobs = new Map(), create = URL.createObjectURL.bind(URL);
  const click = HTMLAnchorElement.prototype.click;
  URL.createObjectURL = function(blob) {
    const url = create(blob); blobs.set(url, blob);
    if (blob instanceof Blob && blob.size <= 10000000) blob.text().then(text => {
      if (/^\uFEFF?FN /m.test(text) && /\nUT WOS:/m.test(text)) state.text=text;
    }).catch(() => {});
    return url;
  };
  HTMLAnchorElement.prototype.click = function() {
    const blob = blobs.get(this.href);
    if (blob && this.download && blob.size <= 10000000) {
      blob.text().then(text => {
        if (/^\uFEFF?FN /m.test(text) && /\nUT WOS:/m.test(text)) state.text=text;
        else state.error='unexpected_export_format';
      }).catch(() => {state.error='export_read_failed';});
      return;
    }
    return click.call(this);
  };
  window.__commWosRestore = () => {URL.createObjectURL=create; HTMLAnchorElement.prototype.click=click;};
}"""


class WOSBackend:
    def __init__(self, browser):
        self.browser = browser
        self.page = None
        self.ready = False
        self.stage = "not_checked"
        self.root = browser.session_path / "wos"
        self.root.mkdir(parents=True, exist_ok=True)
        self.pending_download = None

    async def access_status(self):
        return {"source": "wos", "access_mode": "bfsu_webvpn", "login_url": LOGIN_URL,
                "last_check_authenticated": self.ready, "stage": self.stage,
                "browser_open": self.page is not None and not self.page.is_closed()}

    async def _dismiss(self):
        for selector in ('[aria-label="Close this tour"]', '#onetrust-reject-all-handler', '.onetrust-close-btn-handler'):
            locator = self.page.locator(selector).first
            if await locator.count() and await locator.is_visible():
                with contextlib.suppress(Exception):
                    await locator.click(timeout=2000)

    async def _follow(self, locator):
        context = await self.browser.get_context()
        before = list(context.pages)
        old_url = self.page.url
        await locator.click(timeout=15000)
        for _ in range(60):
            opened = [p for p in context.pages if p not in before and not p.is_closed()]
            if opened:
                self.page = opened[-1]
                await self.page.wait_for_load_state("domcontentloaded", timeout=30000)
                return
            if self.page.url != old_url:
                await self.page.wait_for_load_state("domcontentloaded", timeout=30000)
                return
            await asyncio.sleep(0.25)

    async def _required(self, stage):
        self.ready, self.stage = False, stage
        await self.page.bring_to_front()
        return {"success": False, "status": "needs_attention", "source": "wos",
            "authentication_required": True, "authentication_stage": stage, "login_url": LOGIN_URL,
            "message": "请在已打开的文献浏览器完成北外登录或页面验证，然后重试。英文路径：WebVPN → 图书馆资源 → Web of Science。"}

    async def authenticate(self):
        context = await self.browser.get_context()
        if self.page is None or self.page.is_closed():
            self.page = await context.new_page()
            await self.page.goto(ORIGIN + "/", wait_until="domcontentloaded", timeout=30000)
        if self.ready:
            # Validate the gateway ticket while retaining the WoS SPA session.
            probe = await context.new_page()
            try:
                await probe.goto(ORIGIN + "/", wait_until="domcontentloaded", timeout=30000)
                if await contains_login_form(probe) or urlsplit(probe.url).path != "/":
                    self.ready = False
                    self.page = probe
                    return await self._required("webvpn_login")
            finally:
                if probe is not self.page:
                    await probe.close()
            if self.ready and is_wos(self.page.url):
                # A valid gateway ticket does not prove that WoS's SPA session
                # is still alive. Reload its landing page and check the badge.
                base = urlsplit(wos_url(self.page.url))
                self.ready = False
                await self.page.goto(resource_url(urlunsplit((base.scheme,base.netloc,'/wos/woscc/smart-search','',''))), wait_until='domcontentloaded', timeout=30000)
        if urlsplit(self.page.url).path.startswith('/login') or await contains_login_form(self.page):
            return await self._required("webvpn_login")
        if not is_wos(self.page.url):
            for candidate in reversed(context.pages):
                if not candidate.is_closed() and is_wos(candidate.url):
                    self.page = candidate
                    break
        if not is_wos(self.page.url):
            parsed = urlsplit(self.page.url)
            if parsed.hostname == "webvpn.bfsu.edu.cn" and parsed.path == "/":
                card = self.page.get_by_role("link").filter(has_text="图书馆资源").first
                with contextlib.suppress(Exception):
                    await card.wait_for(state="visible", timeout=8000)
                if not await card.count():
                    return await self._required("webvpn_login")
                await self._follow(card)
            # Public library information page observed through the user's portal.
            await self.page.goto(resource_url("https://lib.bfsu.edu.cn/info/71321.jspx"), wait_until="domcontentloaded", timeout=30000)
            link = self.page.locator('a[href*="webofscience"]').first
            if not await link.count():
                # WebVPN rewrites hostname text into its public address token.
                token = urlsplit(resource_url("http://www.webofscience.com/")).path.split("/")[2]
                link = self.page.locator(f'a[href*="{token}"]').first
            if not await link.count():
                return await self._required("library_entry")
            await self._follow(link)
        if '/wengine-vpn/failed' in self.page.url:
            # WoS's global login redirect can fail inside Wengine. This is the
            # regional landing URL observed after BFSU's successful entry, and
            # still uses the same gateway authorization (no alternate credentials).
            await self.page.goto(resource_url('https://webofscience.clarivate.cn/wos/woscc/smart-search'), wait_until='domcontentloaded', timeout=30000)
        for _ in range(80):
            await self._dismiss()
            visible = await self.page.locator('body').inner_text(timeout=5000)
            if ("Beijing Foreign Studies University" in visible and is_wos(self.page.url)
                    and not await contains_login_form(self.page)):
                self.ready, self.stage = True, "institution_verified"
                await self.browser.save_cookies()
                return {"success": True, "authenticated": True, "source": "wos",
                        "institution": "Beijing Foreign Studies University", "access_mode": "bfsu_webvpn"}
            await asyncio.sleep(0.5)
        return await self._required("wos_institution")

    async def search(self, query, max_results=30, year_start=None, year_end=None):
        expression = search_expression(query, year_start, year_end)
        if not 1 <= max_results <= 100:
            raise ValueError("max_results must be 1..100")
        access = await self.authenticate()
        if not access.get("success"):
            return {**access, "papers": []}
        self.stage = 'search_query'
        # Use the same actual regional host that the library entry redirected to.
        base = urlsplit(wos_url(self.page.url))
        await self.page.goto(resource_url(urlunsplit((base.scheme, base.netloc, "/wos/woscc/advanced-search", "", ""))), wait_until="domcontentloaded", timeout=30000)
        editor = self.page.locator("#advancedSearchInputArea")
        await editor.wait_for(state="visible", timeout=30000)
        await self._dismiss()
        await editor.fill(expression)
        await self.page.get_by_role("button", name="Search", exact=True).click(timeout=10000)
        try:
            await self.page.wait_for_url("**/summary/**", timeout=45000)
        except Exception:
            text = await self.page.locator("body").inner_text(timeout=5000)
            empty = bool(re.search(r"no (?:results|records)|0 results", text, re.I))
            return {"success": empty, "status": "empty" if empty else "search_failed", "papers": [],
                    "query": expression, "message": "WoS 返回空结果" if empty else "WoS 未生成结果页，请检查查询语法或浏览器提示。"}
        await self._dismiss()
        header = self.page.get_by_role("heading", level=1).filter(has_text=re.compile(r"results?", re.I)).first
        count_text = await header.inner_text(timeout=15000)
        match = re.search(r"([\d,]+)\s+results?", count_text, re.I)
        total = int(match[1].replace(",", "")) if match else None
        limit = min(max_results, total) if total else max_results
        self.stage = 'export_records'
        await self.page.evaluate(EXPORT_CAPTURE)
        response_text = []
        response_tasks = set()
        async def read_export_response(response):
            # The native export may be an XHR blob. Do not replay the request.
            try:
                headers = await response.all_headers()
                if not is_wos(response.url) or not ("export" in urlsplit(response.url).path.lower() or "attachment" in headers.get("content-disposition", "")):
                    return
                body = await asyncio.wait_for(response.body(), timeout=15)
                if len(body) <= 10000000:
                    value = body.decode("utf-8-sig")
                    if re.search(r"^FN ", value, re.M) and "\nUT WOS:" in value:
                        response_text.append(value)
            except Exception:
                pass
        def response_seen(response):
            task = asyncio.create_task(read_export_response(response))
            response_tasks.add(task)
            task.add_done_callback(response_tasks.discard)
        self.page.on('response', response_seen)
        try:
            await self.page.locator("#export-trigger-btn").click()
            await self.page.locator("#exportToFieldTaggedButton").click()
            await self.page.locator("#radio3-input").check()
            await self.page.get_by_label("Input starting record range", exact=True).fill("1")
            await self.page.get_by_label(re.compile("Input ending record range")).fill(str(limit))
            await self.page.get_by_text('Author, Title, Source', exact=True).click(timeout=10000)
            await self.page.locator("#option-fullRecord").click()
            await self.page.locator("#exportButton").click()
            captured = {}
            for _ in range(90):
                captured = await self.page.evaluate("window.__commWosExport")
                if response_text:
                    captured = {"text": response_text[0]}
                if captured.get("text") or captured.get("error"):
                    break
                await asyncio.sleep(.5)
        finally:
            self.page.remove_listener('response', response_seen)
            if response_tasks:
                await asyncio.gather(*list(response_tasks), return_exceptions=True)
            with contextlib.suppress(Exception):
                await self.page.evaluate("window.__commWosRestore && window.__commWosRestore()")
        if response_text:
            captured = {"text": response_text[0]}
        if not captured.get("text"):
            return {"success": False, "status": "export_failed", "papers": [],
                    "message": captured.get("error") or "未取得 WoS Full Record 导出；请检查浏览器提示后重试。"}
        path = self.root / ("records_" + uuid.uuid4().hex + ".txt")
        path.write_text(captured["text"], encoding="utf-8")
        records = parse_export(captured["text"])
        if not records:
            return {"success": False, "status": "export_failed", "papers": [], "export_path": str(path),
                    "message": "WoS 导出未解析出完整记录；原始文件已保留。"}
        start, end = year_start or 1800, year_end or datetime.now().year
        papers = [p for p in records if p["language"] == "en" and p["published_date"].isdigit() and start <= int(p["published_date"]) <= end]
        for p in papers:
            p["extra"]["retrieval_queries"] = [query]
        self.stage = 'records_ready'
        return {"success": True, "status": "ok" if papers else "empty", "query": expression,
                "total_results": total, "exported": len(records), "count": len(papers), "papers": papers,
                "export_path": str(path), "excluded_language_or_date": len(records)-len(papers),
                "scope": "Web of Science Core Collection; school subscription; English"}

    async def download(self, paper):
        """Follow live record links in the institutional session; keep failed files."""
        uid = paper.get("extra", {}).get("wos_id") or paper.get("paper_id", "")
        if not re.fullmatch(r"WOS:[A-Za-z0-9]+", uid):
            raise ValueError("A WoS accession ID from a retrieved record is required")
        access = await self.authenticate()
        if not access.get("success"):
            return access
        context = await self.browser.get_context()
        self.stage = 'fulltext_link'
        pending = self.pending_download
        if pending and pending[0] == uid and not pending[1].is_closed():
            page = pending[1]
        else:
            page = await context.new_page()
        capture = PDFCapture(self.root)
        await capture.arm(context, page)
        try:
            if not pending or page is not pending[1]:
                base = urlsplit(wos_url(self.page.url))
                url = urlunsplit((base.scheme, base.netloc, "/wos/woscc/full-record/" + uid, "", ""))
                await page.goto(resource_url(url), wait_until="domcontentloaded", timeout=30000)
                publisher = page.get_by_role("link", name=re.compile(r"full\s*text\s+at\s+publisher|view full text", re.I)).first
                with contextlib.suppress(Exception):
                    await publisher.wait_for(state="visible", timeout=20000)
                if not await publisher.count():
                    publisher = page.get_by_role("link", name=re.compile(r"SFX.*Full Text", re.I)).first
                if not await publisher.count():
                    self.pending_download = (uid, page)
                    return {"success": False, "status": "no_fulltext_link", "message": "WoS 记录没有可用的出版商或馆藏全文链接。"}
                href = await publisher.get_attribute('href')
                if not href or not urljoin(page.url,href).startswith(('https://','http://')):
                    self.pending_download = (uid, page)
                    return {"success": False, "status": "no_fulltext_link", "message": "全文控件未提供可跟随的网页链接，请在 WoS 页面手动选择。"}
                with contextlib.suppress(Exception):
                    await page.goto(urljoin(page.url,href), referer=page.url, wait_until='domcontentloaded', timeout=25000)
            self.stage = 'publisher_pdf'
            tried = set()
            clicked = set()
            for _ in range(4):
                if capture.path:
                    return {"success": True, "file_path": str(capture.path), "format": "pdf"}
                with contextlib.suppress(Exception):
                    await page.wait_for_load_state("domcontentloaded", timeout=15000)
                await asyncio.sleep(2)
                if capture.path:
                    return {"success": True, "file_path": str(capture.path), "format": "pdf"}
                if await contains_login_form(page) or urlsplit(page.url).path.startswith('/login'):
                    self.pending_download = (uid, page)
                    await page.bring_to_front()
                    return {"success": False, "status": "needs_attention", "authentication_required": True,
                            "message": "已打开出版商页面，请完成必要的机构登录后重试这篇论文。"}
                text = await page.locator("body").inner_text(timeout=5000)
                if re.search(r"no services found for current record", text, re.I):
                    self.pending_download = (uid, page)
                    return {"success": False, "status": "no_library_fulltext",
                            "message": "北外 SFX 对这条记录返回无可用全文服务；WoS 收录本身不提供 PDF。"}
                if re.search(r"verify you are human|checking your browser|人机验证|captcha", text[:6000], re.I):
                    self.pending_download = (uid, page)
                    await page.bring_to_front()
                    return {"success": False, "status": "needs_attention", "captcha": True,
                            "message": "出版商要求页面验证，请在浏览器处理后重试。"}
                # Some authorized publishers expose PDF through a button/menu,
                # with no anchor href. Keep its ordinary navigation in this owned
                # tab so native PDF capture is attached before the response.
                pdf_button = page.get_by_role('button', name=re.compile(r'^(?:PDF|Download PDF|下载\s*PDF|PDF\s*下载)$',re.I)).first
                if not await pdf_button.count():
                    pdf_button = page.get_by_text('PDF',exact=True).first
                button_key = (page.url, await pdf_button.inner_text() if await pdf_button.count() else '')
                if (button_key not in clicked and await pdf_button.count() and await pdf_button.is_visible()):
                    clicked.add(button_key)
                    await page.evaluate(r"""() => {
                        if (!window.__commPdfOpen) {
                            window.__commPdfOpen = window.open;
                            window.open = function(url, ...args) {
                                if (url && /^(https?:|\/)/i.test(String(url))) { location.assign(url); return window; }
                                return window.__commPdfOpen.call(window,url,...args);
                            };
                        }
                    }""")
                    with contextlib.suppress(Exception):
                        await pdf_button.click(timeout=10000)
                    continue
                candidates = await page.locator('meta[name="citation_pdf_url"], a[href]').evaluate_all(r"""els => els.map(el => ({url:el.content || el.href, text:el.innerText || '', meta:el.tagName==='META'})).filter(x => x.url && (x.meta || /^(download\s+)?(full[ -]?text\s+)?pdf\b|^下载\s*PDF|^PDF\s*下载/i.test(x.text.trim()))).slice(0,12)""")
                next_url = None
                for candidate in candidates:
                    value = candidate['url']
                    try:
                        # Validate both the visible URL and its decoded resource.
                        from .comm_download import validate_public_url
                        validate_public_url(upstream_url(value))
                        if value not in tried:
                            next_url = value
                            break
                    except (ValueError, OSError):
                        continue
                if not next_url:
                    break
                tried.add(next_url)
                with contextlib.suppress(Exception):
                    await page.goto(next_url, wait_until="domcontentloaded", timeout=20000)
            if capture.path:
                return {"success": True, "file_path": str(capture.path), "format": "pdf"}
            self.pending_download = (uid, page)
            return {"success": False, "status": "fulltext_unavailable", "message": "已到达全文页面，但未取得 PDF；可能需要馆藏权限、手动选择全文或页面验证。可在当前浏览器处理后重试。"}
        finally:
            await capture.close()
            await self.browser.save_cookies()
            if capture.path:
                self.pending_download = None
                await page.close()

    async def download_state(self):
        """Bounded diagnostics from the retained full-text tab; no query tokens."""
        page = self.pending_download[1] if self.pending_download else self.page
        if page is None or page.is_closed():
            return {'open': False}
        password = await contains_login_form(page)
        result = {'open': True, 'page_title': await page.title(), 'password_form': password,
                  'host': urlsplit(page.url).hostname, 'path':urlsplit(page.url).path}
        if not password:
            result['text'] = re.sub(r'https?://\S+', '[URL]', (await page.locator('body').inner_text())[:5000])
            result['links'] = await page.locator('a[href]').evaluate_all("els => els.map(e=>({text:e.innerText, path:new URL(e.href,document.baseURI).pathname})).filter(x => /pdf|full.?text|download/i.test(x.text+' '+x.path)).slice(0,10)")
            result['pdf_controls'] = await page.locator('button, [role="button"]').evaluate_all("els => els.filter(e=>/pdf|下载/i.test(e.innerText)).map(e=>({tag:e.tagName,text:e.innerText,id:e.id,role:e.getAttribute('role')})).slice(0,10)")
        return result


class PDFCapture:
    """Retain PDF response bytes before Chromium creates disposable downloads."""
    def __init__(self, root):
        self.root, self.path, self.cdp = Path(root), None, None
        self.tasks = set()

    async def arm(self, context, page):
        self.page = page
        self.cdp = await context.new_cdp_session(page)
        def paused(event):
            task = asyncio.create_task(self._response(event))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        self.cdp.on("Fetch.requestPaused", paused)
        await self.cdp.send("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Response"}]})

    async def _response(self, event):
        request = event["requestId"]
        headers = {h["name"].lower(): h["value"] for h in event.get("responseHeaders", [])}
        is_pdf = "application/pdf" in headers.get("content-type", "").lower() or ".pdf" in headers.get("content-disposition", "").lower()
        status = event.get('responseStatusCode')
        full_range = re.fullmatch(r'bytes 0-(\d+)/(\d+)', headers.get('content-range',''))
        whole_206 = status == 206 and full_range and int(full_range[1]) + 1 == int(full_range[2])
        if not is_pdf or not (status == 200 or whole_206):
            with contextlib.suppress(Exception):
                await self.cdp.send("Fetch.continueRequest", {"requestId": request})
            return
        try:
            size = int(headers.get("content-length", "0"))
            if size > 50 * 1024 * 1024:
                return
            body = await self.cdp.send("Fetch.getResponseBody", {"requestId": request})
            data = base64.b64decode(body["body"]) if body.get("base64Encoded") else body["body"].encode()
            if data and len(data) <= 50 * 1024 * 1024 and self.path is None:
                suffix = ".pdf" if b"%PDF-" in data[:1024] else ".bin"
                path = self.root / ("fulltext_" + uuid.uuid4().hex + suffix)
                path.write_bytes(data)
                self.path = path
                try:
                    await self.cdp.send("Fetch.failRequest", {"requestId": request, "errorReason": "Aborted"})
                except Exception:
                    # Keep the saved file discoverable and stop this owned tab
                    # before a disposable browser download can be created.
                    with contextlib.suppress(Exception):
                        await self.page.close()
                return
        except Exception:
            pass
        finally:
            with contextlib.suppress(Exception):
                await self.cdp.send("Fetch.failRequest", {"requestId": request, "errorReason": "Aborted"})

    async def close(self):
        if self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)
        if self.cdp:
            with contextlib.suppress(Exception):
                await self.cdp.send("Fetch.disable")
            with contextlib.suppress(Exception):
                await self.cdp.detach()
