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
from .comm_download import data_dir, verify_pdf
from .comm_manifest import persistent_lock


def configuration():
    """Read this service's direct settings only; no other MCP installation is used."""
    binary = os.environ.get("COMM_CNKI_BROWSER_PATH") or os.environ.get("CLOAKBROWSER_BINARY_PATH")
    seed = os.environ.get("COMM_CNKI_COOKIE_FILE")
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
                        reply.set_result({"success": False, "message": "CNKI operation stopped; retry after restarting the MCP"})
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
        try:
            self.ready.set_result(True)
            while True:
                name, arguments, reply = await self.queue.get()
                try:
                    method = {"cnki_search": backend.search, "cnki_download": backend.download}[name]
                    with persistent_lock(data_dir() / "cnki" / "request.lock"):
                        value = await method(**arguments)
                    if not reply.done():
                        reply.set_result(value)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if not reply.done():
                        reply.set_result({"success": False, "message": "Integrated CNKI: " + type(exc).__name__})
        finally:
            await backend.close()

    async def call(self, name, arguments):
        if name not in {"cnki_search", "cnki_download"}:
            raise ValueError("Unknown CNKI operation")
        await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(self.ready)), timeout=45)
        if self.closing or not self.thread.is_alive():
            return {"success": False, "message": "CNKI worker stopped; restart the unified MCP"}
        future = concurrent.futures.Future()
        with self.pending_lock:
            self.pending.add(future)
        self.loop.call_soon_threadsafe(self.queue.put_nowait, (name, arguments, future))
        try:
            return await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(future)), timeout=240)
        except asyncio.TimeoutError:
            return {"success": False, "message": "CNKI timed out; the queued operation may still be running"}
        finally:
            with self.pending_lock:
                self.pending.discard(future)

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


async def download(paper):
    url = paper.get("extra", {}).get("cnki_url") or paper.get("url")
    result = await call("cnki_download", {"detail_url": url, "title": paper["title"],
        "referer":paper.get("extra", {}).get("cnki_referer", "")})
    receipt = {"status": "not_downloaded", "source": "cnki", "paper": paper,
               "message": result.get("message", ""), "captcha": result.get("captcha", False)}
    for field in ("diagnostic_snapshot", "screenshot", "page_title", "blank_verification_page"):
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
