"""Offline regressions for stable profiles and verified school checkpoints.

Run via tests.retained_tmp with pytest tmpdir/cache plugins and bytecode disabled.
The browser and license hook are finite fakes. The process-lock probe launches
only this Python runtime, never Chromium, accesses no network and removes no files.
"""
from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
import subprocess
import sys
import time
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from paper_search_mcp import comm_auth, comm_cnki_host, comm_wos_host
from paper_search_mcp.cnki.browser import BrowserConfigurationError, BrowserSession
from paper_search_mcp.cnki.webvpn import BfsuGateway, BfsuWebVPN
from paper_search_mcp.comm_wos_host import WOSBackend
from tests.test_shared_webvpn_login import FakeContext


ENVELOPE = b"TEST-COOKIE-ENVELOPE:"


def cookie(name, value, domain=".webvpn.bfsu.edu.cn", path="/", expires=-1):
    return {"name": name, "value": value, "domain": domain, "path": path,
            "expires": expires, "httpOnly": True, "secure": True, "sameSite": "Lax"}


def cookie_key(item):
    return (item["name"], item["domain"], item["path"],
            json.dumps(item.get("partitionKey"), sort_keys=True))


class CookieContext(FakeContext):
    """A local profile cookie jar plus the existing finite portal/page fake."""

    def __init__(self, cookies=(), **kwargs):
        super().__init__(**kwargs)
        self.current_cookies = copy.deepcopy(list(cookies))
        self.cookie_additions, self.cookie_reads = [], 0

    async def cookies(self, *args, **kwargs):
        self.cookie_reads += 1
        return copy.deepcopy(self.current_cookies)

    async def add_cookies(self, cookies):
        self.cookie_additions.append(copy.deepcopy(cookies))
        by_key = {cookie_key(item): item for item in self.current_cookies}
        by_key.update({cookie_key(item): copy.deepcopy(item) for item in cookies})
        self.current_cookies = list(by_key.values())


def test_envelope(data, decrypt=False):
    """Deterministic test substitute; no Windows credentials or DPAPI calls."""
    if decrypt:
        assert data.startswith(ENVELOPE)
        return data[len(ENVELOPE):]
    return ENVELOPE + data


test_envelope.__test__ = False


@pytest.fixture(autouse=True)
def no_real_cookie_protection_or_authentication_wait(monkeypatch):
    monkeypatch.setattr(comm_auth, "protect", test_envelope)

    async def no_wait(_):
        return None

    monkeypatch.setattr(comm_wos_host, "asyncio", SimpleNamespace(sleep=no_wait))


@pytest.fixture
def fake_launcher(monkeypatch, tmp_path):
    binary = tmp_path / "fake-browser.exe"
    binary.write_bytes(b"Test placeholder only; this executable is never run.")
    state = SimpleNamespace(binary=binary, calls=[], contexts=[], retained=[], fail_next=False)

    async def launch(profile, **kwargs):
        state.calls.append((Path(profile), kwargs))
        if state.fail_next:
            state.fail_next = False
            raise RuntimeError("synthetic browser launch failure")
        context = CookieContext()
        state.contexts.append(context)
        return context

    module = ModuleType("cloakbrowser")
    module.launch_persistent_context_async = launch
    monkeypatch.setitem(sys.modules, "cloakbrowser", module)
    monkeypatch.setattr(comm_cnki_host, "retain_license_signals",
                        lambda path: state.retained.append(Path(path)))
    return state


def test_stable_profile_reused_across_diagnostic_sessions_and_direct_is_separate(tmp_path, fake_launcher):
    async def scenario():
        first = BrowserSession(tmp_path / "run-one", fake_launcher.binary, webvpn_enabled=True)
        second = BrowserSession(tmp_path / "run-two", fake_launcher.binary, webvpn_enabled=True)
        direct = BrowserSession(tmp_path / "run-direct", fake_launcher.binary, webvpn_enabled=False)
        assert first.profile_path == second.profile_path == tmp_path / "browser-profiles" / "bfsu-webvpn"
        assert direct.profile_path == tmp_path / "browser-profiles" / "cnki-direct"
        assert direct.profile_path != first.profile_path
        assert first.session_path != second.session_path
        try:
            await first.get_context()
            await first.close()
            await second.get_context()
            await second.close()
            await direct.get_context()
            assert [profile for profile, _ in fake_launcher.calls] == [
                first.profile_path, second.profile_path, direct.profile_path]
            assert [Path(kwargs["artifacts_dir"]) for _, kwargs in fake_launcher.calls] == [
                browser.session_path / "browser-artifacts" for browser in (first, second, direct)]
            assert first.profile_path.is_dir() and direct.profile_path.is_dir()
        finally:
            await first.close()
            await second.close()
            await direct.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("release", ["session_close", "native_close"])
def test_two_instances_exclude_each_other_until_browser_lifecycle_ends(tmp_path, fake_launcher, release):
    async def scenario():
        first = BrowserSession(tmp_path / "owner", fake_launcher.binary, webvpn_enabled=True)
        second = BrowserSession(tmp_path / "contender", fake_launcher.binary, webvpn_enabled=True)
        try:
            context = await first.get_context()
            assert await first.get_context() is context
            with pytest.raises(BrowserConfigurationError):
                await second.get_context()
            assert len(fake_launcher.calls) == 1, "A locked profile must fail before browser launch"
            if release == "session_close":
                await first.close()
            else:
                await context.close()  # simulate the user's native window close event
            assert await second.get_context() is fake_launcher.contexts[-1]
            assert len(fake_launcher.calls) == 2
        finally:
            await first.close()
            await second.close()
    asyncio.run(scenario())


def test_launch_failure_releases_profile_lock_for_another_session(tmp_path, fake_launcher):
    async def scenario():
        first = BrowserSession(tmp_path / "failed", fake_launcher.binary, webvpn_enabled=True)
        second = BrowserSession(tmp_path / "retry", fake_launcher.binary, webvpn_enabled=True)
        fake_launcher.fail_next = True
        try:
            with pytest.raises(RuntimeError, match="synthetic browser launch failure"):
                await first.get_context()
            assert len(fake_launcher.calls) == 1
            assert await second.get_context() is fake_launcher.contexts[-1]
            assert len(fake_launcher.calls) == 2
        finally:
            await first.close()
            await second.close()
    asyncio.run(scenario())


def _profile_lock_probe(session_path, binary_path):
    """Bounded subprocess helper: acquire once, report once, release on exit."""
    module = ModuleType("cloakbrowser")

    async def launch(_profile, **_kwargs):
        return CookieContext()

    module.launch_persistent_context_async = launch
    sys.modules["cloakbrowser"] = module
    comm_cnki_host.retain_license_signals = lambda _path: None

    async def scenario():
        browser = BrowserSession(Path(session_path), Path(binary_path), webvpn_enabled=True)
        try:
            try:
                await browser.get_context()
            except BrowserConfigurationError:
                print("BLOCKED", flush=True)
            else:
                print("LAUNCHED", flush=True)
        finally:
            await browser.close()
    asyncio.run(scenario())


def _run_profile_lock_probe(tmp_path, binary):
    app = Path(__file__).resolve().parents[1]
    code = ("import sys; sys.path.insert(0, sys.argv[1]); "
            "from tests.test_browser_session_persistence import _profile_lock_probe; "
            "_profile_lock_probe(sys.argv[2], sys.argv[3])")
    result = subprocess.run([
        sys.executable, "-I", "-B", "-X", "utf8", "-c", code,
        str(app), str(tmp_path / "child-diagnostic-session"), str(binary),
    ], capture_output=True, text=True, encoding="utf-8", timeout=15, check=False)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_profile_lock_is_process_wide_and_reusable_after_close(tmp_path, fake_launcher):
    async def scenario():
        owner = BrowserSession(tmp_path / "owner", fake_launcher.binary, webvpn_enabled=True)
        try:
            await owner.get_context()
            assert _run_profile_lock_probe(tmp_path, fake_launcher.binary) == "BLOCKED"
            await owner.close()
            assert _run_profile_lock_probe(tmp_path, fake_launcher.binary) == "LAUNCHED"
        finally:
            await owner.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("verified,current,fresh,expected", [
    (False, [cookie("gateway", "candidate-unverified")], True, "skipped_unverified"),
    (True, [], True, "skipped_empty"),
    (True, [cookie("cnki", "candidate-resource-only", domain=".cnki.net")], True, "skipped_empty"),
    (True, [cookie("gateway", "candidate-stale-ready")], False, "not_checked"),
], ids=["unverified", "empty", "no-gateway-cookie", "no-fresh-school-check"])
def test_unverified_or_empty_school_session_cannot_overwrite_saved_cache(tmp_path, verified, current, fresh, expected):
    async def scenario():
        browser = BrowserSession(tmp_path / "session", webvpn_enabled=True)
        original = test_envelope(json.dumps([cookie("gateway", "old-verified-ticket")]).encode())
        browser.webvpn_cookie_file.write_bytes(original)
        browser._context = CookieContext(cookies=current)
        browser.webvpn_gateway.ready = verified

        await browser.save_cookies(school_verified=fresh)

        assert browser.webvpn_cookie_file.read_bytes() == original
        assert browser.session_persistence_status["gateway_save"] == expected
        status = json.dumps(browser.session_persistence_status, ensure_ascii=False)
        assert "old-verified-ticket" not in status and "candidate-" not in status
        await browser.close()
        assert browser.webvpn_cookie_file.read_bytes() == original
        assert browser.session_persistence_status["gateway_save"] == expected
    asyncio.run(scenario())


def test_profile_cookies_take_precedence_and_only_missing_valid_cache_cookies_are_restored(tmp_path):
    async def scenario():
        browser = BrowserSession(tmp_path / "session", webvpn_enabled=True)
        now = time.time()
        profile_partition = dict(cookie("partitioned", "profile-partition-new", domain=".cnki.net"),
                                 partitionKey="https://first.example")
        existing = [cookie("sid", "profile-cnki-new", domain=".cnki.net"),
                    cookie("ticket", "profile-gateway-new"), profile_partition]
        cnki_cache = [cookie("sid", "cache-cnki-old", domain=".cnki.net"),
                      cookie("sid", "cache-scoped-live", domain=".cnki.net", path="/scope", expires=now + 3600),
                      cookie("cnki-session", "cache-session", domain=".cnki.net", expires=-1),
                      cookie("cnki-expired", "cache-expired", domain=".cnki.net", expires=now - 3600),
                      dict(profile_partition, value="cache-partition-old"),
                      dict(profile_partition, value="cache-other-partition", partitionKey="https://second.example")]
        school_cache = [cookie("ticket", "cache-gateway-old"),
                        cookie("gateway-session", "cache-gateway-session", expires=-1),
                        cookie("gateway-expired", "cache-gateway-expired", expires=now - 3600),
                        cookie("foreign-sso", "foreign-secret", domain="my.bfsu.edu.cn")]
        browser.cookie_file.write_text(json.dumps(cnki_cache), encoding="utf-8")
        browser.webvpn_cookie_file.write_bytes(test_envelope(json.dumps(school_cache).encode()))
        context = CookieContext(cookies=existing)

        await browser.load_cookies(context)

        by_key = {cookie_key(item): item for item in context.current_cookies}
        assert by_key[cookie_key(existing[0])]["value"] == "profile-cnki-new"
        assert by_key[cookie_key(existing[1])]["value"] == "profile-gateway-new"
        assert by_key[cookie_key(profile_partition)]["value"] == "profile-partition-new"
        restored = [item for batch in context.cookie_additions for item in batch]
        assert {item["name"] for item in restored} == {"sid", "cnki-session", "gateway-session", "partitioned"}
        assert next(item for item in restored if item["name"] == "sid")["path"] == "/scope"
        assert {cookie_key(item) for item in restored}.isdisjoint({cookie_key(item) for item in existing})
        assert all(item.get("expires", -1) == -1 or item["expires"] > now for item in restored)
        assert browser.session_persistence_status["gateway_restore"] == "restored"
        status = json.dumps(browser.session_persistence_status, ensure_ascii=False)
        assert all(item["value"] not in status for item in existing + cnki_cache + school_cache)
    asyncio.run(scenario())


@pytest.mark.parametrize("cache,state", [(None, "missing"), ([], "empty"), (b"synthetic retained cache", "unreadable")])
def test_restore_status_preserves_profile_and_retains_unreadable_cache(monkeypatch, tmp_path, cache, state):
    async def scenario():
        browser = BrowserSession(tmp_path / "session", webvpn_enabled=True)
        original = None
        secret = "SYNTHETIC-RESTORE-SECRET"
        if isinstance(cache, bytes):
            original = cache
            browser.webvpn_cookie_file.write_bytes(original)

            def unreadable_envelope(_data, decrypt=False):
                raise RuntimeError("synthetic decryption failure " + secret)

            monkeypatch.setattr(comm_auth, "protect", unreadable_envelope)
        elif cache is not None:
            original = test_envelope(json.dumps(cache).encode())
            browser.webvpn_cookie_file.write_bytes(original)
        existing = [cookie("profile-ticket", "current-profile-ticket")]
        context = CookieContext(cookies=existing)

        await browser.load_cookies(context)

        assert browser.session_persistence_status["gateway_restore"] == state
        assert context.current_cookies == existing and not context.cookie_additions
        if original is not None:
            assert browser.webvpn_cookie_file.read_bytes() == original
        assert secret not in json.dumps(browser.session_persistence_status, ensure_ascii=False)
    asyncio.run(scenario())


@pytest.mark.parametrize("engine", ["cnki", "wos"])
def test_verified_school_checkpoint_is_saved_before_resource_badge_failure(tmp_path, engine):
    async def scenario():
        browser = BrowserSession(tmp_path / "session", webvpn_enabled=True)
        context = CookieContext(cookies=[cookie("gateway", "verified-school-ticket")],
                                school_logged_in=True, cnki_badge=False, wos_badge=False)
        browser._context = context
        browser.get_context = AsyncMock(return_value=context)
        open_resource = context.open_resource_link

        async def resource_after_checkpoint(href):
            assert browser.webvpn_cookie_file.is_file(), "Save before following a database resource link"
            assert browser.session_persistence_status["gateway_save"] == "saved"
            return await open_resource(href)

        context.open_resource_link = resource_after_checkpoint
        if engine == "cnki":
            result = await BfsuWebVPN(gateway=browser.webvpn_gateway).ensure(context)
        else:
            result = await WOSBackend(browser).authenticate()

        assert result["authentication_stage"] == engine + "_institution"
        assert result["authentication_required"] and browser.webvpn_gateway.ready
        assert browser.webvpn_cookie_file.is_file(), "Checkpoint cannot wait for a resource badge"
        cached = json.loads(test_envelope(browser.webvpn_cookie_file.read_bytes(), decrypt=True))
        assert [item["value"] for item in cached] == ["verified-school-ticket"]
        assert browser.session_persistence_status["gateway_save"] == "saved"
    asyncio.run(scenario())


def test_browser_checkpoint_save_failure_preserves_old_cache_and_authenticated_result(monkeypatch, tmp_path):
    async def scenario():
        browser = BrowserSession(tmp_path / "session", webvpn_enabled=True)
        original = test_envelope(json.dumps([cookie("gateway", "previous-school-ticket")]).encode())
        browser.webvpn_cookie_file.write_bytes(original)
        context = CookieContext(cookies=[cookie("gateway", "fresh-school-ticket")], school_logged_in=True)
        browser._context = context
        secret = "SYNTHETIC-ENCRYPTION-SECRET"

        def failed_encryption(_data, decrypt=False):
            raise RuntimeError("synthetic encryption failure " + secret)

        monkeypatch.setattr(comm_auth, "protect", failed_encryption)
        result = await browser.webvpn_gateway.ensure(context)

        assert result["success"] and result["authenticated"] and browser.webvpn_gateway.ready
        assert result["school_authenticated"] is True and result["persistence_warning"]
        assert result["session_persistence"]["gateway_save"] == "failed"
        assert browser.session_persistence_status["gateway_save"] == "failed"
        assert browser.webvpn_cookie_file.read_bytes() == original
        assert secret not in json.dumps(result, ensure_ascii=False)
    asyncio.run(scenario())


def test_checkpoint_callback_failure_warns_without_revoking_verified_school_session():
    async def scenario():
        calls = []
        secret = "SYNTHETIC-COOKIE-SECRET"

        async def failed_checkpoint():
            calls.append("attempted")
            raise OSError("synthetic persistence failure " + secret)

        gateway = BfsuGateway(on_authenticated=failed_checkpoint)
        result = await gateway.ensure(CookieContext(school_logged_in=True))

        assert calls == ["attempted"]
        assert result["success"] and result["authenticated"] and gateway.ready
        assert result["school_authenticated"] is True
        assert result["session_persistence"]["gateway_save"] == "failed"
        warnings = {key: value for key, value in result.items() if "warning" in key}
        assert warnings, "A failed checkpoint must be reported with an authentication-preserving warning"
        assert secret not in json.dumps(result, ensure_ascii=False)
    asyncio.run(scenario())
