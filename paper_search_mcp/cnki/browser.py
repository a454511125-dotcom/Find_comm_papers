"""Owned browser lifecycle for the internal CNKI provider.

Adapted from the installed cnki-mcp 0.1.0 browser lifecycle (MIT, copyright
2026 wuruiqi; see third_party/CNKI-MCP-LICENSE). Imports and profile paths now
belong to Find_comm_papers; no second Python environment or MCP is involved.
"""
from __future__ import annotations

import contextlib
import json
import os
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
                 seed_cookie_file: Path | None = None):
        self.session_path = Path(session_path)
        self.browser_binary = Path(browser_binary) if browser_binary else None
        self.seed_cookie_file = Path(seed_cookie_file) if seed_cookie_file else None
        self.cookie_file = self.session_path.parent / "cookies.json"
        self._context = None
        self._contexts = []

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
        profile = self.session_path / "profile"
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
        with local_browser_environment(binary):
            ctx = await launch_persistent_context_async(str(profile), **kwargs)
        self._context = ctx
        self._contexts.append(ctx)

        def closed(*_):
            if self._context is ctx:
                self._context = None

        ctx.on("close", closed)
        await self.load_cookies(ctx)
        return ctx

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
                    await context.add_cookies(cookies)
                return
            except (OSError, ValueError, TypeError):
                continue

    async def save_cookies(self):
        if self._context is None:
            return
        cookies = await self._context.cookies()
        retained = [cookie for cookie in cookies
                    if "cnki" in cookie.get("domain", "").lower()]
        self.cookie_file.parent.mkdir(parents=True, exist_ok=True)
        self.cookie_file.write_text(
            json.dumps(retained, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    async def close(self):
        with contextlib.suppress(Exception):
            await self.save_cookies()
        self._context = None
        contexts, self._contexts = self._contexts, []
        for ctx in contexts:
            # CloakBrowser's context.close also stops its Playwright driver,
            # including when the user already closed the native window.
            with contextlib.suppress(Exception):
                await ctx.close()
