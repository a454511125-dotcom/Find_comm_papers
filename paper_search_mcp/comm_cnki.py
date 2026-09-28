"""Serialized, long-lived bridge to the installed CNKI Python environment."""
from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import json
import os
import threading
import uuid
from datetime import timedelta
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .comm_download import data_dir, verify_pdf
from .comm_manifest import persistent_lock


def configuration():
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib
    path = Path(os.environ.get("COMM_CNKI_MCP_CONFIG") or Path.home() / ".codex" / "config.toml")
    config = tomllib.loads(path.read_text(encoding="utf-8-sig")).get("mcp_servers", {}).get("cnki", {})
    command = config.get("command", "")
    if not command or not Path(command).is_file():
        raise RuntimeError("cnki_python_not_configured")
    if not Path(command).name.lower().startswith("python") or config.get("args") != ["-m", "server"]:
        raise RuntimeError("cnki_configuration_not_supported: expected the installed Python server")
    env = {**os.environ, **config.get("env", {})}
    binary = env.get("CLOAKBROWSER_BINARY_PATH", "")
    if not binary or not Path(binary).is_file():
        raise RuntimeError("cnki_browser_binary_required: automatic installation is disabled")
    session = data_dir() / "cnki-bridge" / uuid.uuid4().hex
    session.mkdir(parents=True)
    env.update({"PROFILE_DIR": str(session / "profile"), "COOKIE_FILE": str(session / "cookies.json"),
                "COMM_CNKI_ORIGINAL_COOKIES": config.get("env", {}).get("COOKIE_FILE", ""),
                "PDF_DIR": str(session / "retained-files"), "COMM_CNKI_SESSION": str(session),
                "DELETE_PDF_AFTER_IMPORT": "false", "CLOAKBROWSER_AUTO_UPDATE": "false",
                "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"})
    return StdioServerParameters(command=command, args=["-B", "-X", "utf8", str(Path(__file__).with_name("comm_cnki_host.py"))], env=env)


class Worker:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.ready = concurrent.futures.Future()
        self.pending = set()
        self.pending_lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, daemon=True, name="comm-cnki")
        self.thread.start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        try:
            self.task = self.loop.create_task(self._serve())
            self.loop.run_until_complete(self.task)
        except BaseException as exc:
            if not self.ready.done():
                self.ready.set_exception(RuntimeError(type(exc).__name__ + ": CNKI bridge unavailable"))
        finally:
            with self.pending_lock:
                for reply in self.pending:
                    if not reply.done():
                        reply.set_result({"success": False, "message": "CNKI bridge stopped; retry after restarting the MCP"})
                self.pending.clear()
            self.loop.close()

    async def _serve(self):
        self.queue = asyncio.Queue()
        async with stdio_client(configuration()) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=180)) as session:
                await session.initialize()
                self.ready.set_result(True)
                while True:
                    request = await self.queue.get()
                    if request is None:
                        return
                    name, arguments, reply = request
                    try:
                        with persistent_lock(data_dir() / "cnki-bridge" / "request.lock"):
                            result = await session.call_tool(name, arguments)
                        if result.isError:
                            raise RuntimeError("cnki_tool_failed")
                        value = result.structuredContent
                        if value is None:
                            value = json.loads(next(c.text for c in result.content if c.type == "text"))
                        if not reply.done():
                            reply.set_result(value)
                    except Exception as exc:
                        if not reply.done():
                            reply.set_result({"success": False, "message": "CNKI bridge: " + type(exc).__name__})

    async def call(self, name, arguments):
        await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(self.ready)), timeout=45)
        if not self.thread.is_alive():
            return {"success": False, "message": "CNKI bridge stopped; restart the unified MCP"}
        future = concurrent.futures.Future()
        with self.pending_lock:
            self.pending.add(future)
        self.loop.call_soon_threadsafe(self.queue.put_nowait, (name, arguments, future))
        try:
            return await asyncio.wait_for(asyncio.wrap_future(future), timeout=240)
        except asyncio.TimeoutError:
            return {"success": False, "message": "CNKI bridge timed out; the queued operation may still be running"}
        finally:
            with self.pending_lock:
                self.pending.discard(future)

    def close(self):
        if self.thread.is_alive() and hasattr(self, "task"):
            self.loop.call_soon_threadsafe(self.task.cancel)
            self.thread.join(timeout=15)


_worker = None
_guard = threading.Lock()


async def call(name, arguments):
    global _worker
    with _guard:
        if _worker is None:
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
