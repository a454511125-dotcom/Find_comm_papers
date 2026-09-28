"""Explicit Zotero 10 authorization. Remembered keys are Windows-DPAPI protected."""
from __future__ import annotations

import ctypes
import hashlib
import os
import uuid
from ctypes import wintypes

import requests

from .comm_download import data_dir


def protect(data: bytes, decrypt=False) -> bytes:
    if os.name != "nt":
        raise RuntimeError("Remembered local authorization currently requires Windows DPAPI")
    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    dest = Blob()
    fn = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(dest)):
        raise RuntimeError("Local authorization cache is not accessible to this Windows account")
    try:
        return ctypes.string_at(dest.pbData, dest.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(dest.pbData)


def cache_prefix(server_id):
    return hashlib.sha256(server_id.encode()).hexdigest()[:24]


def cached_key(server_id):
    direct = os.environ.get("COMM_ZOTERO_LOCAL_KEY")
    if direct:
        return direct
    root = data_dir() / "authorization"
    paths = sorted(root.glob(cache_prefix(server_id) + "_*.dpapi"), key=lambda p: p.stat().st_mtime_ns, reverse=True)
    for path in paths:
        try:
            return protect(path.read_bytes(), decrypt=True).decode()
        except RuntimeError:
            continue
    return ""


def probe():
    with requests.Session() as session:
        session.trust_env = False
        try:
            response = session.get("http://127.0.0.1:23119/api/", timeout=5)
            sid = response.headers.get("Zotero-Server-ID", "")
            return {"status": "available" if response.status_code == 200 else "unavailable",
                    "http_status": response.status_code, "version": response.headers.get("X-Zotero-Version"),
                    "server_id": sid, "remembered_local_key": bool(sid and cached_key(sid)),
                    "note": "A cached key may have been revoked; only a successful write verifies authorization."}
        except requests.RequestException:
            return {"status": "unavailable", "reason": "local_api_unreachable"}


def authorize():
    """Call only after the user requests local authorization; never called by import."""
    state = probe()
    if state.get("status") != "available" or not state.get("server_id"):
        return {"status": "unavailable", "reason": "Zotero 10 local API required"}
    with requests.Session() as session:
        session.trust_env = False
        response = session.post("http://127.0.0.1:23119/api/local/authorize",
                                json={"appName": "Find_comm_papers"}, headers={"Zotero-Server-ID": state["server_id"]}, timeout=55)
    if response.status_code != 200:
        return {"status": "not_authorized", "http_status": response.status_code}
    result = response.json()
    if not result.get("remember"):
        # No background attempt to consume a one-use grant or silently reprompt.
        return {"status": "one_time_grant", "usable_for_workflow": False,
                "reason": "Multi-step attachment upload requires Always Allow; no key was saved or printed."}
    encrypted = protect(result["key"].encode())
    root = data_dir() / "authorization"
    root.mkdir(parents=True, exist_ok=True)
    path = root / (cache_prefix(state["server_id"]) + "_" + uuid.uuid4().hex + ".dpapi")
    with path.open("xb") as stream:
        stream.write(encrypted)
    return {"status": "authorized", "server_id": state["server_id"], "storage": "Windows DPAPI", "path": str(path)}
