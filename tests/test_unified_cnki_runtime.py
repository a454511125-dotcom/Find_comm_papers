"""Offline acceptance tests for the single-service CNKI runtime.

These tests use the project's retained ``tmp_path`` fixture. No browser, old
CNKI environment, subprocess, Zotero write, or network connection is required.
"""
from __future__ import annotations

import ast
import asyncio
import builtins
import threading
from pathlib import Path

import pytest

from paper_search_mcp import comm_cnki
from paper_search_mcp import comm_cnki_host


@pytest.fixture
def runtime_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("COMM_MCP_DATA_DIR", str(tmp_path))
    for name in ("COMM_CNKI_BROWSER_PATH", "CLOAKBROWSER_BINARY_PATH", "COMM_CNKI_COOKIE_FILE"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def test_configuration_does_not_read_a_configured_cnki_mcp(monkeypatch, runtime_directory):
    legacy = runtime_directory / "old-codex-config.toml"
    legacy.write_text("not valid TOML: the old MCP must be irrelevant", encoding="utf-8")
    monkeypatch.setenv("COMM_CNKI_MCP_CONFIG", str(legacy))
    original_read = Path.read_text

    def forbid_legacy_read(path, *args, **kwargs):
        assert path != legacy, "The unified service must not read an old MCP configuration"
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", forbid_legacy_read)
    config = comm_cnki.configuration()
    assert set(config) == {"session_path", "browser_binary", "seed_cookie_file"}
    assert config["session_path"].parent == runtime_directory / "cnki"
    assert config["session_path"].is_dir()
    assert config["browser_binary"] is None
    assert config["seed_cookie_file"] is None


def test_configuration_keeps_distinct_sessions_and_uses_explicit_browser(monkeypatch, runtime_directory):
    preferred = runtime_directory / "preferred-browser.exe"
    fallback = runtime_directory / "fallback-browser.exe"
    cookies = runtime_directory / "approved-cookies.json"
    preferred.write_bytes(b"test-only browser placeholder")
    fallback.write_bytes(b"test-only browser placeholder")
    cookies.write_text("[]", encoding="utf-8")
    monkeypatch.setenv("COMM_CNKI_BROWSER_PATH", str(preferred))
    monkeypatch.setenv("CLOAKBROWSER_BINARY_PATH", str(fallback))
    monkeypatch.setenv("COMM_CNKI_COOKIE_FILE", str(cookies))
    first, second = comm_cnki.configuration(), comm_cnki.configuration()
    assert first["session_path"] != second["session_path"]
    assert first["session_path"].is_dir() and second["session_path"].is_dir()
    assert first["browser_binary"] == preferred
    assert first["seed_cookie_file"] == cookies
    monkeypatch.delenv("COMM_CNKI_BROWSER_PATH")
    assert comm_cnki.configuration()["browser_binary"] == fallback


@pytest.mark.parametrize("filename", [
    "comm_cnki.py", "comm_cnki_host.py", "cnki/browser.py", "cnki/search.py", "cnki/download.py",
])
def test_cnki_runtime_has_no_nested_mcp_or_foreign_cnki_imports(filename):
    source = (Path(comm_cnki.__file__).parent / filename).read_text(encoding="utf-8-sig")
    tree = ast.parse(source)
    forbidden_roots = {"mcp", "fastmcp", "subprocess", "cnki", "server", "tomllib", "tomli"}
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            imports.append(node.module or "")
    assert not [name for name in imports if name.split(".")[0] in forbidden_roots]

    forbidden_names = {
        "ClientSession", "StdioServerParameters", "stdio_client", "FastMCP", "Popen",
        # Redirecting process-wide stdout inside an async CNKI operation would
        # corrupt the single MCP server's concurrent JSON-RPC output.
        "redirect_stdout",
    }
    used_names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    used_names.update(node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute))
    assert not (forbidden_names & used_names)
    literals = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    assert not ({"COMM_CNKI_MCP_CONFIG", "mcp_servers", "config.toml"} & literals)


def test_real_backend_constructs_without_external_cnki_or_browser(monkeypatch, runtime_directory):
    original_import = builtins.__import__

    def block_external_cnki(name, *args, **kwargs):
        if name == "cnki" or name.startswith("cnki.") or name == "server":
            raise AssertionError("A separately installed CNKI service must not be imported")
        if name == "cloakbrowser" or name.startswith("cloakbrowser."):
            raise AssertionError("Constructing the unified backend must not launch/load a browser")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", block_external_cnki)

    async def scenario():
        backend = comm_cnki_host.CNKIBackend(runtime_directory / "unstarted-session")
        await backend.close()

    asyncio.run(scenario())


def test_missing_browser_is_reported_before_loading_download_capable_wrapper(monkeypatch, runtime_directory):
    from paper_search_mcp.cnki.browser import BrowserConfigurationError, BrowserSession

    original_import = builtins.__import__

    def prohibit_browser_wrapper(name, *args, **kwargs):
        assert not (name == "cloakbrowser" or name.startswith("cloakbrowser.")), \
            "A missing executable must not invoke the browser wrapper's auto-install path"
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", prohibit_browser_wrapper)

    async def scenario():
        browser = BrowserSession(runtime_directory / "no-browser")
        with pytest.raises(BrowserConfigurationError, match="COMM_CNKI_BROWSER_PATH"):
            await browser.get_context()
        await browser.close()

    asyncio.run(scenario())


def install_fake_backend(monkeypatch):
    instances = []

    class Backend:
        def __init__(self, session_path, browser_binary=None, seed_cookie_file=None):
            self.config = (session_path, browser_binary, seed_cookie_file)
            self.calls = []
            self.loops = []
            self.threads = []
            self.active = 0
            self.max_active = 0
            self.closed = 0
            self.started = threading.Event()
            instances.append(self)

        async def operation(self, kind, arguments):
            self.loops.append(asyncio.get_running_loop())
            self.threads.append(threading.get_ident())
            self.calls.append((kind, arguments))
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.started.set()
            try:
                if arguments.get("query") == "hold":
                    await asyncio.Event().wait()
                await asyncio.sleep(0.002)
                if arguments.get("query") == "fail":
                    raise ValueError("simulated provider error")
                return {"success": True, "operation": kind, "arguments": arguments}
            finally:
                self.active -= 1

        async def search(self, **arguments):
            return await self.operation("search", arguments)

        async def download(self, **arguments):
            return await self.operation("download", arguments)

        async def close(self):
            self.closed += 1
            self.loops.append(asyncio.get_running_loop())
            self.threads.append(threading.get_ident())

    monkeypatch.setattr(comm_cnki, "CNKIBackend", Backend)
    return instances


def test_search_and_download_share_one_backend_thread_and_loop(monkeypatch, runtime_directory):
    instances = install_fake_backend(monkeypatch)
    caller_thread = threading.get_ident()
    worker = comm_cnki.Worker()
    try:
        async def scenario():
            caller_loop = asyncio.get_running_loop()
            search = await worker.call("cnki_search", {"query": "算法推荐"})
            download = await worker.call("cnki_download", {"detail_url": "https://kns.cnki.net/test", "title": "测试论文"})
            assert search["success"] and download["success"]
            assert len(instances) == 1
            backend = instances[0]
            assert [kind for kind, _ in backend.calls] == ["search", "download"]
            assert len(set(backend.loops)) == 1 and backend.loops[0] is not caller_loop
            assert len(set(backend.threads)) == 1 and backend.threads[0] != caller_thread
        asyncio.run(scenario())
    finally:
        worker.close()
    assert instances[0].closed == 1
    assert len(set(instances[0].loops)) == 1
    assert not worker.thread.is_alive()


def test_separate_caller_event_loops_preserve_the_backend_session(monkeypatch, runtime_directory):
    instances = install_fake_backend(monkeypatch)
    worker = comm_cnki.Worker()
    try:
        first = asyncio.run(worker.call("cnki_search", {"query": "算法推荐"}))
        second = asyncio.run(worker.call("cnki_download", {"detail_url": "https://kns.cnki.net/test", "title": "测试论文"}))
        assert first["success"] and second["success"]
        assert len(instances) == 1
        assert len(set(instances[0].loops)) == 1
        assert instances[0].closed == 0
    finally:
        worker.close()


def test_concurrent_requests_are_serialized_without_replacing_backend(monkeypatch, runtime_directory):
    instances = install_fake_backend(monkeypatch)
    worker = comm_cnki.Worker()
    try:
        async def scenario():
            requests = [worker.call("cnki_search", {"query": str(index)}) for index in range(12)]
            results = await asyncio.gather(*requests)
            assert all(result["success"] for result in results)
            assert {result["arguments"]["query"] for result in results} == {str(i) for i in range(12)}
        asyncio.run(scenario())
        assert len(instances) == 1
        assert instances[0].max_active == 1
        assert len(instances[0].calls) == 12
    finally:
        worker.close()


def test_provider_error_does_not_poison_the_shared_worker(monkeypatch, runtime_directory):
    instances = install_fake_backend(monkeypatch)
    worker = comm_cnki.Worker()
    try:
        async def scenario():
            failed = await worker.call("cnki_search", {"query": "fail"})
            recovered = await worker.call("cnki_search", {"query": "算法推荐"})
            assert failed["success"] is False
            assert recovered["success"] is True
        asyncio.run(scenario())
        assert len(instances) == 1
        assert len(instances[0].calls) == 2
    finally:
        worker.close()


def test_closing_during_a_request_releases_caller_and_backend(monkeypatch, runtime_directory):
    instances = install_fake_backend(monkeypatch)
    worker = comm_cnki.Worker()
    try:
        async def scenario():
            await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(worker.ready)), timeout=5)
            pending = asyncio.create_task(worker.call("cnki_search", {"query": "hold"}))
            assert await asyncio.to_thread(instances[0].started.wait, 5)
            worker.close()
            result = await asyncio.wait_for(pending, timeout=2)
            assert result["success"] is False
        asyncio.run(scenario())
    finally:
        worker.close()
    assert instances[0].active == 0
    assert instances[0].closed == 1
    assert not worker.thread.is_alive()


def test_initialization_failure_is_reported_without_a_hanging_worker(monkeypatch, runtime_directory):
    def broken_backend(**_kwargs):
        raise ValueError("invalid local backend setup")

    monkeypatch.setattr(comm_cnki, "CNKIBackend", broken_backend)
    worker = comm_cnki.Worker()
    try:
        async def scenario():
            with pytest.raises(RuntimeError):
                await asyncio.wait_for(worker.call("cnki_search", {"query": "算法推荐"}), timeout=5)
        asyncio.run(scenario())
    finally:
        worker.close()
    assert not worker.thread.is_alive()
