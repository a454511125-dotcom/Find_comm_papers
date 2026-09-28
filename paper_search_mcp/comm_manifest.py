"""Append-only selections and receipts. No automatic removal of any file."""
from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .comm_download import data_dir
from .comm_ranking import canonical, doi_key, normalize

_locks: dict[str, threading.Lock] = {}
_guard = threading.Lock()


def now():
    return datetime.now(timezone.utc).isoformat()


def identity(paper):
    if doi_key(paper.get("doi")):
        return "doi:" + doi_key(paper["doi"])
    authors = paper.get("authors") or []
    return "title:" + normalize(paper["title"]) + ":" + normalize(authors[0] if authors else "") + ":" + str(paper.get("published_date", ""))[:4]


def entry_key(paper):
    prefix = "zh:" if paper.get("language", "").startswith("zh") else ""
    return "p_" + hashlib.sha256((prefix + identity(paper)).encode()).hexdigest()[:20]


def run_dir(run_id):
    if not re.fullmatch(r"[a-f0-9]{32}", run_id):
        raise ValueError("Invalid manifest_id")
    return data_dir() / "manifests" / run_id


@contextlib.contextmanager
def persistent_lock(path: Path):
    """Process + OS lock; keep the lock file after release (no stale-file deletion)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _guard:
        lock = _locks.setdefault(str(path), threading.Lock())
    if not lock.acquire(blocking=False):
        raise RuntimeError("workflow_busy: another operation is in progress")
    handle = None
    acquired = False
    try:
        handle = path.open("a+b")
        if path.stat().st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired = True
        yield
    except OSError as exc:
        raise RuntimeError("workflow_lock_error") from exc
    finally:
        if handle:
            if acquired:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
        lock.release()


def save(state):
    root = run_dir(state["manifest_id"])
    root.mkdir(parents=True, exist_ok=True)
    state["revision"] = state.get("revision", 0) + 1
    state["updated_at"] = now()
    path = root / f"revision_{state['revision']:06d}.json"
    with path.open("x", encoding="utf-8") as stream:
        json.dump(state, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    return str(path)


def load(run_id):
    paths = sorted(run_dir(run_id).glob("revision_*.json"), reverse=True)
    if not paths:
        raise ValueError("Manifest not found")
    # A crashed final write must not hide the last complete snapshot.
    for path in paths:
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            state["revision"] = int(paths[0].stem.split("_")[1])
            return state
        except (ValueError, OSError):
            continue
    raise ValueError("No readable manifest snapshot")


def create(papers, query, title=""):
    if not query.strip() or not 1 <= len(papers) <= 100:
        raise ValueError("Provide a query and 1..100 selected papers")
    entries, seen = [], set()
    for record in papers:
        paper = canonical(record.get("paper", record))
        if not paper["title"]:
            raise ValueError("Each selected paper must have a title")
        key = entry_key(paper)
        if key in seen:
            continue
        seen.add(key)
        receipt = copy.deepcopy(record) if record.get("status") == "downloaded" else None
        entries.append({"entry_id": key, "order": len(entries) + 1, "paper": paper,
                        "selection_reason": record.get("selection_reason", ""),
                        "metadata": {"status": "pending", "item_key": record.get("zotero_item_key")},
                        "attachment": {"status": "pending", "receipt": receipt},
                        "status": "pending", "attempts": []})
    state = {"schema_version": 1, "manifest_id": uuid.uuid4().hex, "title": title,
             "query": query, "created_at": now(), "target": None, "options": None,
             "entries": entries, "revision": 0}
    save(state)
    return report(state)


def report(state, include_papers=False):
    entries = []
    for e in state["entries"]:
        item = {"entry_id": e["entry_id"], "order": e["order"], "title": e["paper"]["title"],
                "language": e["paper"].get("language", "en"), "source": e["paper"].get("source", "unknown"),
                "doi": e["paper"]["doi"], "status": e["status"],
                "metadata": e["metadata"], "attachment": {k: v for k, v in e["attachment"].items() if k != "receipt"},
                "selection_reason": e["selection_reason"], "last_attempt": (e["attempts"] or [None])[-1]}
        if e["attachment"].get("receipt"):
            item["attachment"]["pdf_path"] = e["attachment"]["receipt"].get("pdf_path")
        if include_papers:
            item["paper"] = e["paper"]
        entries.append(item)
    return {"manifest_id": state["manifest_id"], "revision": state["revision"],
            "path": str(run_dir(state["manifest_id"])), "query": state["query"],
            "target": state["target"], "count": len(entries),
            "complete": sum(e["status"] == "complete" for e in entries),
            "metadata_verified": sum(e["metadata"]["status"] == "verified" for e in entries),
            "attachments_verified": sum(e["attachment"]["status"] == "verified" for e in entries),
            "entries": entries}
