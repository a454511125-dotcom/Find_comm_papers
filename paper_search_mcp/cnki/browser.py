"""Owned browser lifecycle for the internal CNKI provider.

Adapted from the installed cnki-mcp 0.1.0 browser lifecycle (MIT, copyright
2026 wuruiqi; see third_party/CNKI-MCP-LICENSE). Imports and profile paths now
belong to Find_comm_papers; no second Python environment or MCP is involved.
"""
from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path


class BrowserConfigurationError(RuntimeError):
    """An actionable local configuration failure, safe to report to the user."""


@contextlib.contextmanager
def local_browser_environment(binary: Path):
    """Give CloakBrowser a pre-existing executable; never allow auto-downloads."""
    from ..comm_browser import BROWSER_LAUNCH_LOCK
    values = {
        "CLOAKBROWSER_BINARY_PATH": str(binary),
        "CLOAKBROWSER_AUTO_UPDATE": "false",
    }
    with BROWSER_LAUNCH_LOCK:
        previous = {key: os.environ.get(key) for key in values}
        os.environ.update(values)
        try:
            yield
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


class BrowserSession:
    """One lazy, visible browser owned by the provider's serialized event loop."""

    def __init__(self, session_path: Path, browser_binary: Path | None = None,
                 seed_cookie_file: Path | None = None, webvpn_enabled: bool = False):
        self.session_path = Path(session_path)
        self.browser_binary = Path(browser_binary) if browser_binary else None
        self.seed_cookie_file = Path(seed_cookie_file) if seed_cookie_file else None
        self.cookie_file = self.session_path.parent / "cookies.json"
        self.webvpn_enabled = webvpn_enabled
        # Diagnostics have a fresh session directory; the browser's own state
        # must survive worker/MCP restarts, including database local storage.
        mode = "bfsu-webvpn" if webvpn_enabled else "cnki-direct"
        self.profile_path = self.session_path.parent / "browser-profiles" / mode
        self._profile_lease = None
        self.session_persistence_status = {"gateway_restore": "not_checked", "gateway_save": "not_checked"}
        from .webvpn import BfsuGateway
        self.webvpn_gateway = BfsuGateway(on_authenticated=self._checkpoint_school_session) if webvpn_enabled else None
        self.webvpn_cookie_file = self.session_path.parent / "bfsu-webvpn-cookies.dpapi"
        self._context = None
        self._contexts = []

    def _release_profile(self):
        lease, self._profile_lease = self._profile_lease, None
        if lease is not None:
            lease.__exit__(None, None, None)

    async def _checkpoint_school_session(self):
        try:
            await self.save_cookies(school_verified=True)
        except Exception:
            self.session_persistence_status["gateway_save"] = "failed"
        return dict(self.session_persistence_status)

    async def get_context(self):
        if self._context is not None:
            return self._context
        # A window may have been closed by the user. Its wrapper still owns a
        # Playwright driver, so finish that lifecycle before opening another.
        if self._contexts:
            await self.close()
        binary = self.browser_binary
        if binary is None or not binary.is_file():
            raise BrowserConfigurationError(
                "Set COMM_CNKI_BROWSER_PATH to an existing Chromium executable; "
                "Find_comm_papers does not download or update browsers automatically."
            )
        self.session_path.mkdir(parents=True, exist_ok=True)
        profile = self.profile_path
        artifacts = self.session_path / "browser-artifacts"
        profile.mkdir(parents=True, exist_ok=True)
        artifacts.mkdir(parents=True, exist_ok=True)
        kwargs = dict(
            headless=False,
            accept_downloads=False,
            service_workers="block",
            args=["--no-first-run"],
            locale="zh-CN",
            artifacts_dir=str(artifacts),
        )
        try:
            from cloakbrowser import launch_persistent_context_async
        except ImportError as exc:
            raise BrowserConfigurationError(
                "Install the CNKI browser dependency in the Find_comm_papers "
                "environment (cloakbrowser==0.5.7 and playwright>=1.62.0); a separate "
                "CNKI MCP installation is not required."
            ) from exc
        # Retaining license status files changes file retention only: the
        # wrapper's license checks and errors remain active.
        from ..comm_cnki_host import retain_license_signals
        retain_license_signals(self.session_path)
        from ..comm_manifest import persistent_lock
        lease = persistent_lock(profile.with_suffix(".lock"))
        try:
            lease.__enter__()
        except RuntimeError as exc:
            raise BrowserConfigurationError(
                "统一文献浏览器正在由另一进程使用，或其配置目录无法锁定；请继续使用已有 MCP 会话。"
                "切换到辅助登录入口前请先关闭原文献浏览器。"
            ) from exc
        self._profile_lease = lease
        try:
            with local_browser_environment(binary):
                ctx = await launch_persistent_context_async(str(profile), **kwargs)
        except BaseException:
            self._release_profile()
            raise
        self._context = ctx
        self._contexts.append(ctx)

        def closed(*_):
            if self._context is ctx:
                self._context = None
                if self.webvpn_gateway:
                    self.webvpn_gateway.reset()
                self._release_profile()

        ctx.on("close", closed)
        try:
            await self.load_cookies(ctx)
        except BaseException:
            await self.close()
            raise
        return ctx

    @staticmethod
    def _cookie_identity(cookie):
        return (cookie.get("name"), cookie.get("domain"), cookie.get("path", "/"),
                json.dumps(cookie.get("partitionKey"), sort_keys=True))

    async def _restore_missing_cookies(self, context, cookies):
        # The persistent profile may be newer than the last export. Fill only
        # missing keys (including session cookies); never roll it back.
        present = {self._cookie_identity(cookie) for cookie in await context.cookies()}
        now = time.time()
        missing = [cookie for cookie in cookies if self._cookie_identity(cookie) not in present
                   and (float(cookie.get("expires", -1)) <= 0 or float(cookie["expires"]) > now)]
        if missing:
            await context.add_cookies(missing)

    async def load_cookies(self, context):
        # Once this provider has saved its own session, external seed cookies
        # must never overwrite a newer manual login or verification.
        candidates = [self.cookie_file]
        if self.seed_cookie_file and self.seed_cookie_file != self.cookie_file:
            candidates.append(self.seed_cookie_file)
        for path in candidates:
            if not path.is_file():
                continue
            try:
                cookies = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(cookies, list):
                    continue
                if cookies:
                    await self._restore_missing_cookies(context, cookies)
                break
            except (OSError, ValueError, TypeError):
                continue
        if self.webvpn_enabled:
            self.session_persistence_status["gateway_restore"] = "missing"
        if self.webvpn_enabled and self.webvpn_cookie_file.is_file():
            from ..comm_auth import protect
            try:
                cookies = json.loads(protect(self.webvpn_cookie_file.read_bytes(), decrypt=True))
                cookies = [cookie for cookie in cookies if self._webvpn_cookie(cookie)]
                if cookies:
                    await self._restore_missing_cookies(context, cookies)
                self.session_persistence_status["gateway_restore"] = "restored" if cookies else "empty"
            except (OSError, RuntimeError, ValueError, TypeError, KeyError):
                # Preserve the file and report the restore failure, rather than
                # silently treating it as an empty cache and overwriting it.
                self.session_persistence_status["gateway_restore"] = "unreadable"

    @staticmethod
    def _webvpn_cookie(cookie):
        return cookie.get("domain", "").lower().lstrip(".") == "webvpn.bfsu.edu.cn"

    async def save_cookies(self, *, school_verified=False):
        if self._context is None:
            return
        if self.webvpn_enabled and not school_verified:
            # A prior ready flag is not a new ticket check. Closing a window or
            # a failed search must not publish a possibly logged-out snapshot.
            return
        if self.webvpn_enabled and not self.webvpn_gateway.ready:
            self.session_persistence_status["gateway_save"] = "skipped_unverified"
            return
        cookies = await self._context.cookies()
        gateway_cookies = [cookie for cookie in cookies if self._webvpn_cookie(cookie)]
        if self.webvpn_enabled and not gateway_cookies:
            self.session_persistence_status["gateway_save"] = "skipped_empty"
            return
        retained = [cookie for cookie in cookies
                    if "cnki" in cookie.get("domain", "").lower()]
        self.cookie_file.parent.mkdir(parents=True, exist_ok=True)
        if retained:
            self.cookie_file.write_text(
                json.dumps(retained, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        if self.webvpn_enabled:
            from ..comm_auth import protect
            encrypted = protect(json.dumps(gateway_cookies).encode("utf-8"))
            self.webvpn_cookie_file.write_bytes(encrypted)
            self.session_persistence_status["gateway_save"] = "saved"

    async def close(self):
        with contextlib.suppress(Exception):
            await self.save_cookies()
        self._context = None
        if self.webvpn_gateway:
            self.webvpn_gateway.reset()
        contexts, self._contexts = self._contexts, []
        for ctx in contexts:
            # CloakBrowser's context.close also stops its Playwright driver,
            # including when the user already closed the native window.
            with contextlib.suppress(Exception):
                await ctx.close()
        self._release_profile()
