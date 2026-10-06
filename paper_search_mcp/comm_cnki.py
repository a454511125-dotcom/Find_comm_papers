"""CNKI functions integrated in the same process and Python environment as English."""
from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import json
import os
import threading
import uuid
from pathlib import Path

from .comm_cnki_host import CNKIBackend
from .cnki.browser import BrowserConfigurationError
from .comm_download import data_dir, verify_pdf
from .comm_manifest import persistent_lock
from .config import get_env


def configuration():
    """Read this service's direct settings only; no other MCP installation is used."""
    binary = get_env("COMM_CNKI_BROWSER_PATH") or get_env("CLOAKBROWSER_BINARY_PATH")
    seed = get_env("COMM_CNKI_COOKIE_FILE")
    if binary and not Path(binary).is_file():
        raise RuntimeError("cnki_browser_binary_missing: configure COMM_CNKI_BROWSER_PATH")
    if seed and not Path(seed).is_file():
        raise RuntimeError("cnki_seed_cookie_file_missing: configure COMM_CNKI_COOKIE_FILE")
    session = data_dir() / "cnki" / uuid.uuid4().hex
    session.mkdir(parents=True)
    return {"session_path": session, "browser_binary": Path(binary) if binary else None,
            "seed_cookie_file": Path(seed) if seed else None}


class Worker:
    """Serialize direct Python calls on one long-lived browser event loop."""
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.ready = concurrent.futures.Future()
        self.pending = set()
        self.wos_inflight = {}
        self.pending_lock = threading.Lock()
        self.closing = False
        self.thread = threading.Thread(target=self._run, daemon=True, name="find-cnki")
        self.thread.start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        try:
            self.task = self.loop.create_task(self._serve())
            self.loop.run_until_complete(self.task)
        except BaseException as exc:
            if not self.ready.done():
                self.ready.set_exception(RuntimeError(type(exc).__name__ + ": integrated CNKI unavailable"))
        finally:
            with self.pending_lock:
                for reply in self.pending:
                    if not reply.done():
                        reply.set_result({"success": False, "message": "Literature operation stopped; retry after restarting the MCP"})
                self.pending.clear()
            tasks = asyncio.all_tasks(self.loop)
            for task in tasks:
                task.cancel()
            if tasks:
                self.loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
            self.loop.run_until_complete(self.loop.shutdown_asyncgens())
            self.loop.close()

    async def _serve(self):
        self.queue = asyncio.Queue()
        backend = CNKIBackend(**configuration())
        wos = None
        wos_browser = None
        try:
            self.ready.set_result(True)
            while True:
                name, arguments, reply = await self.queue.get()
                try:
                    provider, operation = name.split("_", 1)
                    if provider == "wos":
                        if wos is None:
                            from .comm_wos_host import WOSBackend
                            if backend.browser.webvpn_enabled:
                                wos_browser = backend.browser
                            else:
                                from .cnki.browser import BrowserSession
                                wos_browser = BrowserSession(**configuration(), webvpn_enabled=True)
                            wos = WOSBackend(wos_browser)
                        method = getattr(wos, operation)
                    else:
                        method = getattr(backend, operation)
                    with persistent_lock(data_dir() / "cnki" / "request.lock"):
                        value = await asyncio.wait_for(method(**arguments), timeout=300) if provider == 'wos' else await method(**arguments)
                    if not reply.done():
                        reply.set_result(value)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if not reply.done():
                        reply.set_result({"success": False, "status": "error",
                                          "stage": wos.stage if provider == 'wos' and wos is not None else operation,
                                          "message": str(exc) if isinstance(exc, BrowserConfigurationError)
                                                     else provider.upper() + ": " + type(exc).__name__})
        finally:
            if wos_browser is not None and wos_browser is not backend.browser:
                await wos_browser.close()
            await backend.close()

    async def call(self, name, arguments):
        if name not in {p + "_" + op for p in ("cnki", "wos") for op in ("search", "download", "authenticate", "access_status")}:
            raise ValueError("Unknown literature operation")
        await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(self.ready)), timeout=45)
        if self.closing or not self.thread.is_alive():
            return {"success": False, "message": "Literature worker stopped; restart the unified MCP"}
        key = (name, json.dumps(arguments,sort_keys=True,ensure_ascii=False)) if name.startswith('wos_') else None
        queued = True
        with self.pending_lock:
            future = self.wos_inflight.get(key) if key else None
            if future is not None:
                queued = False
            else:
                future = concurrent.futures.Future()
                if key:
                    self.wos_inflight[key] = future
            self.pending.add(future)
        if queued:
            self.loop.call_soon_threadsafe(self.queue.put_nowait, (name, arguments, future))
        answered = False
        try:
            value = await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(future)), timeout=330 if key else 240)
            answered = True
            return value
        except asyncio.TimeoutError:
            return {"success": False, "status": "running", "operation_continues": True,
                    "message": "Operation still queued/running; an identical WoS retry joins the existing request without repeating it." if key else "CNKI timed out; the queued operation may still be running"}
        finally:
            with self.pending_lock:
                self.pending.discard(future)
                if key and answered and self.wos_inflight.get(key) is future:
                    self.wos_inflight.pop(key,None)

    def close(self):
        self.closing = True
        if self.thread.is_alive() and hasattr(self, "task"):
            self.loop.call_soon_threadsafe(self.task.cancel)
            self.thread.join(timeout=20)


_worker = None
_guard = threading.Lock()


async def call(name, arguments):
    global _worker
    with _guard:
        if _worker is None or not _worker.thread.is_alive():
            _worker = Worker()
            atexit.register(_worker.close)
    return await _worker.call(name, arguments)


async def search(**arguments):
    return await call("cnki_search", arguments)


async def authenticate():
    return await call("cnki_authenticate", {})


async def access_status():
    return await call("cnki_access_status", {})


async def download(paper):
    url = paper.get("extra", {}).get("cnki_url") or paper.get("url")
    result = await call("cnki_download", {"detail_url": url, "title": paper["title"],
        "referer":paper.get("extra", {}).get("cnki_referer", "")})
    receipt = {"status": "not_downloaded", "source": "cnki", "paper": paper,
               "message": result.get("message", ""), "captcha": result.get("captcha", False)}
    for field in ("diagnostic_snapshot", "screenshot", "page_title", "blank_verification_page",
                  "authentication_required", "authentication_stage", "access_mode", "login_url", "network_diagnostics"):
        if field in result:
            receipt[field] = result[field]
    if result.get("file_path"):
        path = Path(result["file_path"]).resolve(strict=True)
        if not path.is_relative_to(data_dir()):
            raise ValueError("CNKI response path must be inside the library directory")
        receipt["retained_path"] = str(path)
        if result.get("success") and result.get("format") == "pdf":
            valid, evidence = verify_pdf(path.read_bytes(), paper)
            receipt.update({"status": "downloaded" if valid else "identity_unverified", "identity_check": evidence})
            if valid:
                receipt["pdf_path"] = str(path)
        elif result.get("format") == "caj":
            receipt["status"] = "unsupported_format"
            receipt["message"] = "CAJ retained; conversion to a verified PDF is required before attachment import"
    path = data_dir() / ("cnki_receipt_" + uuid.uuid4().hex + ".json")
    with path.open("x", encoding="utf-8") as stream:
        json.dump(receipt, stream, ensure_ascii=False, indent=2)
    receipt["receipt_path"] = str(path)
    return receipt


def download_sync(paper, use_scihub=None):
    return asyncio.run(download(paper))
