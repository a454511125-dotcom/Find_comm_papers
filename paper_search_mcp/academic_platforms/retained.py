"""Exclusive native-connector writes; scopes propagate to asyncio worker threads."""
from __future__ import annotations

import builtins
import contextlib
import contextvars
import re
import logging
from pathlib import Path
from urllib.parse import urlsplit

_ROOT = contextvars.ContextVar("upstream_retained_root", default=None)


def redact_diagnostic(value):
    def redact_url(match):
        try:
            parsed = urlsplit(match[0])
            return parsed.scheme + "://" + (parsed.hostname or "redacted") + "/[details omitted]"
        except ValueError:
            return "[URL omitted]"
    value = re.sub(r"https?://[^\s\"'<>]+", redact_url, str(value))
    return re.sub(r"(?i)(api[-_]?key|authorization|bearer|password|token)([\s:=]+)[^\s,;]+", r"\1\2[omitted]", value)


class SafeDiagnosticFilter(logging.Filter):
    def filter(self, record):
        record.msg = redact_diagnostic(record.getMessage())
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        return True


def safe_filename(value):
    return re.sub(r"[^\w.-]+", "_", str(value)).strip(". ")[:160] or "paper"


@contextlib.contextmanager
def retained_scope(root):
    token = _ROOT.set(Path(root).resolve())
    try:
        yield
    finally:
        _ROOT.reset(token)


def retained_open(file, mode="r", *args, **kwargs):
    """Never truncate files; reject connector writes outside its allocated directory."""
    if any(flag in mode for flag in "wax+"):
        path = Path(file).resolve()
        root = _ROOT.get()
        if root is not None and not path.is_relative_to(root):
            raise ValueError("Connector output escapes its retained directory")
        if "+" in mode or "a" in mode:
            raise ValueError("Connector updates of existing files are unsupported")
        mode = mode.replace("w", "x")
        path.parent.mkdir(parents=True, exist_ok=True)
    return builtins.open(file, mode, *args, **kwargs)
