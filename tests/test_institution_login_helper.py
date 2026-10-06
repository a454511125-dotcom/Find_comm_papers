"""Unified source helper control flow, without browser/network or cleanup."""
import asyncio
import runpy
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from paper_search_mcp import comm_cnki, comm_wos
from paper_search_mcp.comm_manifest import persistent_lock


@pytest.fixture
def helper(monkeypatch, tmp_path):
    monkeypatch.setenv("COMM_MCP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMM_CNKI_ACCESS_MODE", "bfsu_webvpn")
    monkeypatch.setattr(comm_cnki, "_worker", None)
    path = Path(__file__).resolve().parents[1] / "library_login.py"
    return runpy.run_path(str(path), run_name="test_only")


@pytest.mark.parametrize("stage", ["webvpn_login", "webvpn_portal"])
def test_pending_school_login_checks_one_provider_until_it_completes(helper, monkeypatch, tmp_path, stage):
    calls = []

    async def cnki():
        calls.append("cnki")
        return ({"authentication_required": True, "authentication_stage": stage}
                if len(calls) == 1 else {"authenticated": True})

    async def wos():
        calls.append("wos")
        return {"authenticated": True}

    pause = AsyncMock()
    monkeypatch.setattr(comm_cnki, "authenticate", cnki)
    monkeypatch.setattr(comm_wos, "authenticate", wos)
    monkeypatch.setattr(asyncio, "sleep", pause)
    status = tmp_path / "status.json"
    result = asyncio.run(helper["main"](status))
    assert calls == ["cnki", "cnki", "wos"]
    assert pause.await_count == 1 and result["authenticated"] and status.is_file()


def test_school_login_pending_in_second_provider_keeps_polling_that_provider(helper, monkeypatch):
    calls, wos_checks = [], 0

    async def cnki():
        calls.append("cnki")
        return {"authenticated": True}

    async def wos():
        nonlocal wos_checks
        calls.append("wos")
        wos_checks += 1
        return ({"authentication_required": True, "authentication_stage": "webvpn_login"}
                if wos_checks < 3 else {"authenticated": True})

    pause = AsyncMock()
    monkeypatch.setattr(comm_cnki, "authenticate", cnki)
    monkeypatch.setattr(comm_wos, "authenticate", wos)
    monkeypatch.setattr(asyncio, "sleep", pause)
    result = asyncio.run(helper["main"]())
    assert calls == ["cnki", "wos", "wos", "wos", "cnki"]
    assert pause.await_count == 2 and result["authenticated"]


def test_database_verification_does_not_block_the_other_database(helper, monkeypatch):
    calls = []

    async def cnki():
        calls.append("cnki")
        return ({"authentication_required": True, "authentication_stage": "cnki_institution"}
                if len(calls) == 1 else {"authenticated": True})

    async def wos():
        calls.append("wos")
        return {"authenticated": True}

    monkeypatch.setattr(comm_cnki, "authenticate", cnki)
    monkeypatch.setattr(comm_wos, "authenticate", wos)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    assert asyncio.run(helper["main"]())["authenticated"]
    assert calls == ["cnki", "wos", "cnki"]


@pytest.mark.parametrize("raised", [False, True])
def test_fatal_database_error_still_checks_other_database_and_closes_worker(helper, monkeypatch, capsys, raised):
    calls = []
    worker = Mock()
    monkeypatch.setattr(comm_cnki, "_worker", worker)
    secret = "SYNTHETIC-PROVIDER-SECRET"

    async def cnki():
        calls.append("cnki")
        if raised:
            raise RuntimeError("synthetic exception " + secret)
        return {"success": False, "message": "test_auth_failed"}

    async def wos():
        calls.append("wos")
        return {"authenticated": True}

    monkeypatch.setattr(comm_cnki, "authenticate", cnki)
    monkeypatch.setattr(comm_wos, "authenticate", wos)
    with pytest.raises(RuntimeError):
        asyncio.run(helper["main"]())
    assert calls == ["cnki", "wos"]
    worker.close.assert_called_once()
    assert secret not in capsys.readouterr().out


def test_direct_cnki_remains_a_completed_check_without_institution_claim(helper, monkeypatch):
    monkeypatch.setenv("COMM_CNKI_ACCESS_MODE", "direct")
    cnki = AsyncMock(return_value={"access_mode": "direct", "authentication_required": False})
    wos = AsyncMock()
    monkeypatch.setattr(comm_cnki, "authenticate", cnki)
    monkeypatch.setattr(comm_wos, "authenticate", wos)
    result = asyncio.run(helper["main"](sources=("cnki",)))
    assert result["completed"] and not result["authenticated"]
    assert not result["shared_school_login"]
    cnki.assert_awaited_once()
    wos.assert_not_awaited()


@pytest.mark.parametrize("saved", ["saved", "failed", "skipped_empty", None])
def test_only_actual_saved_state_claims_cache_was_saved(helper, monkeypatch, capsys, saved):
    result = {"authenticated": True}
    if saved is not None:
        result["session_persistence"] = {"gateway_save": saved}
    monkeypatch.setattr(comm_cnki, "authenticate", AsyncMock(return_value=result))
    monkeypatch.setattr(comm_wos, "authenticate", AsyncMock(return_value=result))
    state = asyncio.run(helper["main"]())
    assert state["authenticated"]
    assert ("会话已加密保存" in capsys.readouterr().out) == (saved == "saved")


@pytest.mark.parametrize("argv,sources", [
    ([], ("cnki", "wos")),
    (["cnki"], ("cnki",)),
    (["wos"], ("wos",)),
    (["--source", "cnki"], ("cnki",)),
    (["--source", "wos"], ("wos",)),
    (["--source", "both"], ("cnki", "wos")),
])
def test_cli_defaults_and_old_position_arguments_select_expected_sources(helper, monkeypatch, tmp_path, argv, sources):
    fake_main = AsyncMock()
    monkeypatch.setitem(helper["run"].__globals__, "main", fake_main)
    status = tmp_path / "selected-status.json"
    assert helper["run"]([*argv, "--status-file", str(status)]) == 0
    fake_main.assert_awaited_once_with(status, sources)
    assert (tmp_path / "cnki" / "institution-login.lock").is_file()


def test_second_helper_exits_before_opening_another_login(helper, monkeypatch, tmp_path):
    fake_main = AsyncMock()
    monkeypatch.setitem(helper["run"].__globals__, "main", fake_main)
    with persistent_lock(tmp_path / "cnki" / "institution-login.lock"):
        assert helper["run"]([]) == 1
    fake_main.assert_not_awaited()
    assert (tmp_path / "cnki" / "institution-login.lock").is_file()


@pytest.mark.parametrize("error,code", [
    (RuntimeError("synthetic check failed"), 1),
    (OSError("synthetic status write failed"), 1),
    (KeyboardInterrupt(), 130),
])
def test_cli_exception_exit_status_releases_lifetime_lock(helper, monkeypatch, tmp_path, error, code):
    fake_main = AsyncMock(side_effect=error)
    monkeypatch.setitem(helper["run"].__globals__, "main", fake_main)
    assert helper["run"]([]) == code
    fake_main.assert_awaited_once()
    with persistent_lock(tmp_path / "cnki" / "institution-login.lock"):
        pass
