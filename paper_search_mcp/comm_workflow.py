"""Resumable selected-paper ingestion. Writes are serialized, checkpointed and read back."""
from __future__ import annotations

import json
from urllib.parse import quote

from . import comm_manifest as manifests
from .comm_download import data_dir, download_selected as download_one, fetch_json, local_file
from .comm_ranking import canonical, doi_key, normalize
from .comm_zotero import ZoteroClient, ZoteroError, safe_failure, stable_key, metadata_payload, identity_matches


def enrich(paper):
    """Only enrich new items. Never overwrite existing Zotero metadata."""
    paper = canonical(paper)
    if paper.get("doi") and paper.get("source") not in {"cnki", "wos"}:
        try:
            message = fetch_json("https://api.crossref.org/works/" + quote(paper["doi"], safe=""))["message"]
            if doi_key(message.get("DOI")) != paper["doi"]:
                raise ValueError("DOI mismatch")
            extra = paper["extra"]
            extra["creators"] = [{"creatorType": "author", "firstName": a.get("given", ""), "lastName": a["family"]}
                                 if a.get("family") else {"creatorType": "author", "name": a.get("name", "")}
                                 for a in message.get("author", []) if a.get("family") or a.get("name")]
            for name in ("volume", "issue", "page"):
                if message.get(name):
                    extra[name] = message[name]
            extra["crossref_type"] = message.get("type", "")
            if message.get("container-title"):
                paper["venue"] = message["container-title"][0]
            if not paper["published_date"]:
                parts = message.get("published", {}).get("date-parts", [[]])[0]
                paper["published_date"] = "-".join(str(x).zfill(2) for x in parts)
            if not paper["authors"]:
                paper["authors"] = [" ".join([a.get("firstName", ""), a.get("lastName", a.get("name", ""))]).strip() for a in extra["creators"]]
            if not paper["abstract"]:
                paper["abstract"] = message.get("abstract", "")
            paper = canonical(paper)
        except Exception:
            paper["extra"]["metadata_enrichment"] = "unavailable; retained supplied metadata"
    if not paper["authors"] or not paper["published_date"] or not paper["venue"]:
        raise ZoteroError("metadata_incomplete: authors, date and venue are required for new items")
    return paper


def matches(item, paper):
    return identity_matches(item, paper)


def execute(manifest_id, collections=None, tags=None, download_pdfs=True, limit=5,
            transport="auto", use_scihub=None, retry_only=False,
            client_factory=ZoteroClient, downloader=download_one, enricher=enrich):
    if not 1 <= limit <= 10:
        raise ValueError("limit must be 1..10; it counts attempted entries")
    # One durable lock across selections prevents simultaneous duplicate creation.
    with manifests.persistent_lock(data_dir() / "manifests" / "zotero.lock"):
        state = manifests.load(manifest_id)
        if retry_only:
            if not state.get("options"):
                raise ValueError("No previous import to retry")
            opts = state["options"]
            collections, tags = opts["collections"], opts["tags"]
            download_pdfs, transport, use_scihub = opts["download_pdfs"], opts["transport"], opts["use_scihub"]
        else:
            collections = list(dict.fromkeys(collections or []))
            tags = list(dict.fromkeys(["comm_papers"] if tags is None else tags))
        client = client_factory(transport=transport)
        try:
            target = {**client.scope, "collections": collections}
            if state["target"] is not None and target != state["target"]:
                raise ValueError("Selection target is frozen; create a new selection for a different library/collection")
            client.check_collections(collections)
            options = {"collections": collections, "tags": tags, "download_pdfs": download_pdfs,
                       "transport": client.transport, "use_scihub": use_scihub}
            if state["options"] is not None and options != state["options"]:
                raise ValueError("Import options are frozen; create a new selection to change them")
            state["target"], state["options"] = target, options
            manifests.save(state)
            attempted = 0
            for entry in state["entries"]:
                if entry["status"] == "complete":
                    continue
                if retry_only and entry["status"] not in {"needs_retry", "running"}:
                    continue
                if attempted >= limit:
                    break
                attempted += 1
                paper = entry["paper"]
                attempt = {"started_at": manifests.now(), "stage": "metadata"}
                entry["attempts"].append(attempt)
                entry["status"] = "running"
                manifests.save(state)
                try:
                    item_key = entry["metadata"].get("item_key")
                    item = client.item(item_key) if item_key else None
                    if item is not None and not matches(item, paper):
                        raise ZoteroError("saved_item_identity_conflict")
                    if item_key and item is None and entry["metadata"].get("status") == "verified":
                        raise ZoteroError("previously_imported_item_missing: no automatic recreation")
                    if item is None:
                        item = client.find(paper)
                    created = False
                    if item is None:
                        # Deterministic reserved key survives timeout/crash and cross-manifest retries.
                        reserved = stable_key(json.dumps(client.scope, sort_keys=True) + ":" + manifests.identity(paper))
                        entry["metadata"].update({"item_key": reserved, "status": "write_pending"})
                        manifests.save(state)
                        item = client.item(reserved)
                        if item is not None and not matches(item, paper):
                            raise ZoteroError("reserved_item_key_conflict")
                        if item is None:
                            paper = enricher(paper)
                            entry["paper"] = paper
                            manifests.save(state)
                            payload = metadata_payload(paper)
                            payload["collections"], payload["tags"] = collections, [{"tag": t} for t in tags]
                            item = client.create(payload, reserved)
                            created = True
                    item = client.add_memberships(item, collections, tags)
                    verified = client.item(item["key"])
                    if not verified or not matches(verified, paper):
                        raise ZoteroError("metadata_readback_failed")
                    entry["metadata"] = {"status": "verified", "item_key": item["key"], "created": created,
                                         "verified_at": manifests.now()}
                    manifests.save(state)
                    if download_pdfs:
                        attempt["stage"] = "download"
                        receipt = entry["attachment"].get("receipt")
                        if receipt and receipt.get("status") == "downloaded":
                            try:
                                local_file(receipt["pdf_path"])
                            except (OSError, ValueError, KeyError):
                                receipt = None  # Keep old snapshots/files; reacquire unavailable PDF.
                        if not receipt or receipt.get("status") != "downloaded":
                            receipt = downloader(paper, use_scihub)
                            entry["attachment"]["receipt"] = receipt
                            manifests.save(state)
                        if receipt.get("status") != "downloaded":
                            raise ZoteroError("pdf_not_downloaded")
                        attempt["stage"] = "attachment"
                        def reserve(attachment_key, md5):
                            entry["attachment"].update({"status": "write_pending", "attachment_key": attachment_key, "md5": md5})
                            manifests.save(state)
                        result = client.ensure_attachment(paper, item["key"], receipt, reserve)
                        entry["attachment"].update(result)
                    else:
                        entry["attachment"]["status"] = "not_requested"
                    entry["status"] = "complete"
                    attempt["status"] = "complete"
                except Exception as exc:
                    entry["status"] = "needs_retry"
                    if attempt["stage"] in {"download", "attachment"}:
                        entry["attachment"]["status"] = "needs_retry"
                    attempt.update({"status": "needs_retry", "error": safe_failure(exc)})
                attempt["finished_at"] = manifests.now()
                manifests.save(state)
            result = manifests.report(state)
            result["attempted_this_call"] = attempted
            return result
        finally:
            client.close()
