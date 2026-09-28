"""Optional, bounded headless rendering for known open-access PDF candidates.

The caller owns OA provenance and PDF identity verification. This module only
retrieves bytes; it does not promote a publisher or other site to an OA source.
No browser downloads, external MCP service, or CNKI login profile are used.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import re
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urljoin, urlsplit


BROWSER_LAUNCH_LOCK = threading.RLock()
_BROWSER_SLOT = threading.Lock()
_MAX_TIMEOUT = 60.0
_MAX_BYTES = 50 * 1024 * 1024
_MAX_REQUESTS = 64
_REDIRECT_CODES = {301, 302, 303, 307, 308}
_CHALLENGES = [
    "iframe[src*='captcha']", ".g-recaptcha", ".h-captcha", ".cf-turnstile",
    "#challenge-form", "#cf-challenge-running", "input[type='password']",
]
_BROWSER_ENV_NAMES = {
    "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH", "USERPROFILE", "HOME",
    "HOMEDRIVE", "HOMEPATH", "LOCALAPPDATA", "APPDATA", "PROGRAMDATA",
    "PROGRAMFILES", "PROGRAMFILES(X86)", "COMMONPROGRAMFILES", "COMSPEC", "PATHEXT",
    "DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS",
    "LANG", "LC_ALL", "TZ", "XAUTHORITY", "SSL_CERT_FILE", "SSL_CERT_DIR",
}


def _validate(url: str) -> str:
    # A lazy import avoids a cycle with the downloader which invokes this fallback.
    from .comm_download import validate_public_url
    host = (urlsplit(url).hostname or "").lower()
    if re.search(r"(?:^|\.)sci-?hub\.", host):
        raise ValueError("This fallback only accepts normal OA candidates")
    return validate_public_url(url)


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Browser time budget exceeded")
    return remaining


class _Capture:
    """Route every request through validated, explicitly followed redirects."""

    def __init__(self, deadline: float, max_bytes: int):
        self.deadline, self.max_bytes = deadline, max_bytes
        self.content = None
        self.final_url = ""
        self.document_url = ""
        self.document_status = 0
        self.reason = "no_pdf_found"
        self.request_count = 0
        self.received_bytes = 0
        self.ready = asyncio.Event()
        self.inflight = asyncio.Semaphore(4)

    async def route(self, route):
        try:
            async with self.inflight:
                request = route.request
                current = request.url
                _validate(current)
                # Audio/video and websockets are unrelated to paper retrieval.
                if request.resource_type in {"media", "websocket"}:
                    await route.abort()
                    return
                method = request.method
                for _ in range(8):
                    if self.request_count >= _MAX_REQUESTS:
                        self.reason = "request_limit"
                        await route.abort()
                        return
                    self.request_count += 1
                    _validate(current)
                    response = await route.fetch(
                        url=current, method=method, max_redirects=0,
                        timeout=max(1, int(_remaining(self.deadline) * 1000)),
                    )
                    headers = {key.lower(): value for key, value in response.headers.items()}
                    if response.status in _REDIRECT_CODES:
                        target = urljoin(current, headers.get("location", ""))
                        if not headers.get("location"):
                            raise ValueError("Missing redirect destination")
                        _validate(target)
                        current = target
                        if response.status == 303 or (response.status in {301, 302} and method == "POST"):
                            method = "GET"
                        continue
                    _validate(response.url)
                    frame = getattr(request, "frame", None)
                    if request.resource_type == "document" and (frame is None or frame.parent_frame is None):
                        self.document_url, self.document_status = current, response.status
                    mime = headers.get("content-type", "").lower()
                    disposition = headers.get("content-disposition", "").lower()
                    possible_pdf = "application/pdf" in mime or ".pdf" in disposition or "application/octet-stream" in mime
                    limit = self.max_bytes if possible_pdf else min(self.max_bytes, 8 * 1024 * 1024)
                    length = headers.get("content-length", "")
                    if length.isdigit() and int(length) > limit:
                        self.reason = "size_limit"
                        await route.abort()
                        return
                    # Playwright buffers response bodies; check both declared and
                    # actual length, and bound aggregate traffic for this attempt.
                    body = await response.body()
                    self.received_bytes += len(body)
                    if len(body) > limit or self.received_bytes > self.max_bytes * 2:
                        self.reason = "size_limit"
                        await route.abort()
                        return
                    _remaining(self.deadline)
                    if b"%PDF-" in body[:1024] and 200 <= response.status < 300:
                        self.content, self.final_url = body, current
                        self.ready.set()
                        await route.abort()
                    elif possible_pdf or "attachment" in disposition:
                        self.reason = "not_pdf"
                        await route.abort()
                    else:
                        # Fulfill the validated final response, not a redirect:
                        # browser-internal redirect hops can bypass route handlers.
                        await route.fulfill(response=response, body=body)
                    return
                self.reason = "redirect_limit"
                await route.abort()
        except (ValueError, OSError):
            self.reason = "blocked_url"
            with contextlib.suppress(Exception):
                await route.abort()
        except (TimeoutError, asyncio.TimeoutError):
            self.reason = "timeout"
            with contextlib.suppress(Exception):
                await route.abort()
        except Exception:
            self.reason = "network_error"
            with contextlib.suppress(Exception):
                await route.abort()


async def _manual_reason(page, capture):
    from .comm_cnki_host import visible_challenge
    title = await page.title()
    if re.search(
        r"captcha|verify.*human|human.*verification|just a moment|security check|"
        r"making sure.{0,60}not a bot|checking.{0,30}(?:human|not a bot)",
        title, re.I,
    ):
        return "verification_required"
    if await visible_challenge(page, _CHALLENGES):
        return "login_or_verification_required"
    if capture.document_status in {401, 407}:
        return "login_required"
    if capture.document_status == 403:
        return "access_denied"
    return ""


_LINKS_SCRIPT = """() => {
  const links = [];
  for (const el of document.querySelectorAll('meta[name="citation_pdf_url"], meta[name="wkhealth_pdf_url"]')) {
    if (el.content) links.push(el.content);
  }
  for (const el of document.querySelectorAll('a[href]')) {
    const r = el.getBoundingClientRect(); const style = getComputedStyle(el);
    if (r.width <= 0 || r.height <= 0 || style.display === 'none' || style.visibility === 'hidden') continue;
    const label = (el.innerText || el.getAttribute('aria-label') || '').trim();
    if (/citation|bibtex|\\bris\\b/i.test(label)) continue;
    if (/\\.pdf(?:[?#]|$)|\\/pdf\\//i.test(el.getAttribute('href')) || /^(?:download\\s+(?:pdf|full\\s*text)|view\\s+pdf|pdf(?:\\s+download)?)$/i.test(label)) {
      links.push(el.getAttribute('href'));
    }
  }
  return [...new Set(links)].slice(0, 4);
}"""


async def _diagnostics(page, session: Path):
    paths = {}
    if page is None or page.is_closed():
        return paths
    with contextlib.suppress(Exception):
        snapshot = session / "failure.html"
        snapshot.write_text(await page.content(), encoding="utf-8")
        paths["diagnostic_snapshot"] = str(snapshot)
    with contextlib.suppress(Exception):
        screenshot = session / "failure.png"
        await page.screenshot(path=str(screenshot), timeout=1500)
        paths["screenshot"] = str(screenshot)
    return paths


async def _launch(session: Path, binary: Path, deadline: float):
    from .cnki.browser import local_browser_environment
    from .comm_cnki_host import retain_license_signals
    from cloakbrowser import launch_persistent_context_async
    artifacts = session / "browser-artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    profile = session / "profile"
    profile.mkdir(parents=True, exist_ok=True)
    # Same-process browser launches use one lock because the wrapper consumes
    # the binary override from the process environment.
    if not BROWSER_LAUNCH_LOCK.acquire(timeout=_remaining(deadline)):
        raise TimeoutError("Browser launch is busy")
    try:
        retain_license_signals(session)
        with local_browser_environment(binary):
            return await launch_persistent_context_async(
                str(profile), headless=True, accept_downloads=False,
                service_workers="block", artifacts_dir=str(artifacts),
                locale="en-US", args=["--no-first-run"],
                # Keep process essentials, not literature/Zotero API keys.
                # The wrapper may add its own browser-license credentials.
                env={key: value for key, value in os.environ.items() if key.upper() in _BROWSER_ENV_NAMES},
                timeout=max(1, int(_remaining(deadline) * 1000)),
            )
    finally:
        BROWSER_LAUNCH_LOCK.release()


async def _fetch(url: str, binary: Path, session: Path, timeout: float, max_bytes: int):
    deadline = time.monotonic() + timeout
    context, page = None, None
    capture = _Capture(deadline, max_bytes)

    async def navigate(target):
        for _ in range(3):
            _validate(target)
            capture.document_url = ""
            try:
                await page.goto(target, wait_until="domcontentloaded", timeout=max(1, int(_remaining(deadline) * 1000)))
            except Exception:
                if capture.content is None and capture.reason in {"blocked_url", "size_limit", "timeout"}:
                    raise
            if capture.content is not None:
                return
            final = capture.document_url
            if not final or final == target:
                break
            # The route handler fulfills a final response instead of letting
            # Chromium follow unchecked redirect hops. Render at the validated
            # final address again so relative assets and JS use its real origin.
            target = final
        if capture.content is None:
            await page.wait_for_timeout(min(1200, int(_remaining(deadline) * 1000)))

    async def attempt():
        nonlocal context, page
        context = await _launch(session, binary, deadline)
        await context.route("**/*", capture.route)
        if hasattr(context, "route_web_socket"):
            await context.route_web_socket("**/*", lambda socket: socket.close())
        page = await context.new_page()
        await navigate(url)
        if capture.content is not None:
            return ""
        manual = await _manual_reason(page, capture)
        if manual:
            return manual
        source_url = capture.document_url or page.url
        candidates = await page.evaluate(_LINKS_SCRIPT)
        for candidate in candidates[:4]:
            target = urljoin(source_url, candidate)
            try:
                _validate(target)
            except (ValueError, OSError):
                capture.reason = "blocked_url"
                continue
            await navigate(target)
            if capture.content is not None:
                return ""
            manual = await _manual_reason(page, capture)
            if manual:
                return manual
        # Some publishers expose a script-backed PDF button without a link.
        for button in await page.locator("button").all():
            label = (await button.inner_text()).strip()
            if await button.is_visible() and re.fullmatch(r"(?:download\s+(?:pdf|full\s*text)|view\s+pdf|pdf(?:\s+download)?)", label, re.I):
                await button.click(timeout=max(1, int(_remaining(deadline) * 1000)))
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(capture.ready.wait(), timeout=min(3, _remaining(deadline)))
                if capture.content is not None:
                    return ""
                return await _manual_reason(page, capture)
        return ""

    try:
        manual = await asyncio.wait_for(attempt(), timeout=timeout)
        if capture.content is not None:
            return {"status": "downloaded", "content": capture.content,
                    "final_url": capture.final_url, "transport": "headless_browser"}
        result = {"status": "needs_manual" if manual else "failed", "reason": manual or capture.reason}
    except (TimeoutError, asyncio.TimeoutError):
        result = {"status": "failed", "reason": "timeout"}
    except ImportError:
        result = {"status": "failed", "reason": "browser_dependency_missing"}
    except Exception:
        result = {"status": "failed", "reason": capture.reason if capture.reason != "no_pdf_found" else "browser_error"}
    finally:
        # Close only after the failure evidence is saved. All on-disk profiles,
        # status files and artifacts remain; no cleanup removes user files.
        if capture.content is None:
            with contextlib.suppress(Exception):
                evidence = await asyncio.wait_for(_diagnostics(page, session), timeout=2)
                if "result" in locals():
                    result.update(evidence)
        if context is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(context.close(), timeout=5)
    return result


def fetch_browser_pdf(url: str, paper: dict, *, timeout: float, max_bytes: int) -> dict:
    """Run from the downloader's executor thread; return bytes for its verifier.

    One English browser attempt may run at a time. Browser work is capped at 60
    seconds, with up to 7 additional seconds for failure evidence and driver close.
    Captchas and access/login gates are reported, never solved or submitted.
    """
    if timeout <= 0 or max_bytes <= 0:
        return {"status": "failed", "reason": "invalid_limits"}
    if str(paper.get("source", "")).lower().replace("-", "_") in {"sci_hub", "scihub"}:
        return {"status": "failed", "reason": "unsupported_source"}
    try:
        _validate(url)
    except (ValueError, OSError):
        return {"status": "failed", "reason": "blocked_url"}
    configured = os.environ.get("COMM_BROWSER_PATH") or os.environ.get("COMM_CNKI_BROWSER_PATH")
    binary = Path(configured).expanduser() if configured else None
    if binary is None or not binary.is_file():
        return {"status": "failed", "reason": "browser_not_configured"}
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        return {"status": "failed", "reason": "executor_thread_required"}
    if not _BROWSER_SLOT.acquire(blocking=False):
        return {"status": "failed", "reason": "browser_busy"}
    try:
        from .comm_download import data_dir
        session = data_dir() / "english-browser" / uuid.uuid4().hex
        session.mkdir(parents=True, exist_ok=False)
        return asyncio.run(_fetch(url, binary, session, min(float(timeout), _MAX_TIMEOUT), min(int(max_bytes), _MAX_BYTES)))
    finally:
        _BROWSER_SLOT.release()
