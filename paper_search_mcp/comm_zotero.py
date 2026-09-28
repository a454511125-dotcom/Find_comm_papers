"""Zotero Web API / Zotero 10 local API adapter; no database or file deletion."""
from __future__ import annotations

import hashlib
import os
import re
try:
    import tomllib
except ImportError:  # Python 3.10
    import tomli as tomllib
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

import requests

from .comm_download import local_file, verify_pdf
from .comm_ranking import doi_key, normalize, load_profile
from .comm_auth import cached_key


class ZoteroError(RuntimeError):
    pass


def safe_failure(exc):
    # Do not expose API headers, tokens, signed URLs, or exception response bodies.
    if isinstance(exc, ZoteroError):
        return str(exc)
    if isinstance(exc, requests.Timeout):
        return "network_timeout: operation result must be reconciled before retry"
    if isinstance(exc, requests.RequestException):
        return "network_error"
    return type(exc).__name__


def stable_key(value):
    alphabet = "23456789ABCDEFGHIJKLMNPQRSTUVWXYZ"
    digest = hashlib.sha256(value.encode()).digest()
    return "".join(alphabet[b % len(alphabet)] for b in digest[:8])


def key(value):
    if not re.fullmatch(r"[A-Z0-9]{8}", value or ""):
        raise ZoteroError("invalid_zotero_key")
    return value


def identity_matches(item, paper):
    data = item.get("data", {})
    if data.get("deleted") or data.get("itemType") in {"attachment", "note", "annotation"}:
        return False
    doi = doi_key(paper.get("doi"))
    if doi:
        extra_doi = re.search(r"(?im)^DOI:\s*(\S+)\s*$", data.get("extra", ""))
        return doi_key(data.get("DOI") or (extra_doi[1] if extra_doi else "")) == doi
    authors = paper.get("authors") or []
    creators = [c for c in data.get("creators", []) if c.get("creatorType", "author") == "author"]
    name = " ".join([creators[0].get("firstName", ""), creators[0].get("lastName", creators[0].get("name", ""))]) if creators else ""
    expected_year = str(paper.get("published_date", ""))[:4]
    actual_year = str(data.get("date", ""))[:4]
    author_match = bool(authors and sorted(normalize(name).split()) == sorted(normalize(authors[0]).split()))
    if paper.get("language", "").startswith("zh") and creators and authors:
        creator = creators[0]
        expected_name = normalize(authors[0]).replace(" ", "")
        author_match = expected_name in {normalize(name).replace(" ", ""),
            normalize(creator.get("lastName", "") + creator.get("firstName", "")).replace(" ", "")}
    return bool(authors and normalize(name) and re.fullmatch(r"\d{4}", expected_year)
                and expected_year == actual_year
                and normalize(data.get("title")) == normalize(paper["title"])
                and author_match)


def credentials():
    """Reuse only the named Zotero integration; never copy credentials into receipts."""
    conf = load_profile().get("zotero", {})
    config_path = Path(os.environ.get("COMM_ZOTERO_MCP_CONFIG") or conf.get("mcp_config_path") or Path.home() / ".codex" / "config.toml")
    saved = {}
    if config_path.is_file():
        saved = tomllib.loads(config_path.read_text(encoding="utf-8-sig")).get("mcp_servers", {}).get("zotero", {}).get("env", {})
    return {name: os.environ.get(name) or saved.get(name, "") for name in ("ZOTERO_API_KEY", "ZOTERO_LIBRARY_ID", "ZOTERO_LIBRARY_TYPE")}


class ZoteroClient:
    def __init__(self, transport="auto", timeout=15):
        self.timeout = timeout
        self.session = requests.Session()
        self.headers = {"Zotero-API-Version": "3"}
        creds = credentials()
        if transport == "auto":
            transport = load_profile().get("zotero", {}).get("preferred_transport", "local")
        if transport not in {"local", "web"}:
            raise ValueError("transport must be auto, local, or web")
        self.transport = transport
        kind = creds["ZOTERO_LIBRARY_TYPE"] or "user"
        if kind not in {"user", "group"}:
            raise ZoteroError("invalid_library_type")
        self.library_id = creds["ZOTERO_LIBRARY_ID"]
        if transport == "local":
            self.origin = "http://127.0.0.1:23119"
            self.api = self.origin + "/api"
            self.session.trust_env = False
            probe = self.session.get(self.api + "/", timeout=5)
            probe.raise_for_status()
            self.server_id = probe.headers.get("Zotero-Server-ID", "")
            if not self.server_id:
                raise ZoteroError("local_zotero_10_required")
            local_key = cached_key(self.server_id)
            if not local_key:
                raise ZoteroError("local_write_authorization_required: request authorization explicitly before importing")
            self.headers.update({"Zotero-Server-ID": self.server_id, "Zotero-API-Key": local_key})
            self.library_id = self.library_id or "0"
        else:
            self.origin = "https://api.zotero.org"
            self.api = self.origin
            self.server_id = None
            if not creds["ZOTERO_API_KEY"] or not re.fullmatch(r"\d+", self.library_id):
                raise ZoteroError("zotero_web_credentials_missing")
            self.headers["Zotero-API-Key"] = creds["ZOTERO_API_KEY"]
        self.prefix = f"/{kind}s/{self.library_id}"
        self.scope = {"transport": transport, "library_type": kind, "library_id": self.library_id, "server_id": self.server_id}

    def close(self):
        self.session.close()

    def request(self, method, route, **kwargs):
        headers = {**self.headers, **kwargs.pop("headers", {})}
        response = self.session.request(method, self.api + route, headers=headers, timeout=self.timeout, allow_redirects=False, **kwargs)
        if response.status_code >= 300:
            # 404 needs a distinct sentinel for lookups. Never turn access/network errors into absence.
            if response.status_code == 404 and method == "GET":
                return None
            raise ZoteroError(f"zotero_http_{response.status_code}")
        return response

    def get(self, route, **params):
        response = self.request("GET", route, params=params or None)
        return response.json() if response is not None else None

    def item(self, item_key):
        return self.get(self.prefix + "/items/" + key(item_key))

    def children(self, item_key):
        result = self.get(self.prefix + "/items/" + key(item_key) + "/children", limit=100)
        if result is None:
            raise ZoteroError("parent_not_found")
        if len(result) >= 100:
            raise ZoteroError("too_many_attachments_for_safe_check")
        return result

    def check_collections(self, collections):
        for value in collections:
            if self.get(self.prefix + "/collections/" + key(value)) is None:
                raise ZoteroError("collection_not_found")

    def find(self, paper):
        doi = doi_key(paper.get("doi"))
        query = doi or paper["title"]
        found = self.get(self.prefix + "/items", q=query, qmode="everything", limit=100)
        if found is None:
            raise ZoteroError("library_not_found")
        if len(found) >= 100:
            raise ZoteroError("lookup_truncated: refine manually before importing")
        matches = []
        for record in found:
            if identity_matches(record, paper):
                matches.append(record)
        if len(matches) > 1:
            raise ZoteroError("duplicate_doi_in_library: manual resolution required")
        return matches[0] if matches else None

    def create(self, payload, item_key):
        # A client-generated key requires version 0: create only if absent.
        # This also rejects an intervening write to the same key without overwriting it.
        body = {**payload, "key": key(item_key), "version": 0}
        response = self.request("POST", self.prefix + "/items", json=[body])
        result = response.json()
        if result.get("failed"):
            errors = result["failed"]
            error = next(iter(errors.values()))
            code = error.get("code", "unknown")
            # Zotero 10.0.3 does not initialize primaryData after assigning a new
            # caller-supplied key. Only this confirmed failure triggers fallback.
            if self.transport == "local" and code == 400 and "'primaryData' not loaded" in error.get("message", ""):
                return self.create_server_key(payload, item_key)
            raise ZoteroError(f"zotero_item_write_failed_{code}")
        verified = self.item(item_key)
        if verified is None:
            raise ZoteroError("write_not_visible_yet")
        return verified

    def create_server_key(self, payload, reservation_key):
        """Recover server-assigned keys by a stable relation marker, even after a lost response."""
        marker = "urn:comm-papers:reservation:" + key(reservation_key)
        if payload["itemType"] == "attachment":
            candidates = self.children(payload["parentItem"])
        else:
            candidates = self.get(self.prefix + "/items", q=payload.get("DOI") or payload["title"], qmode="everything", limit=100)
            if candidates is None or len(candidates) >= 100:
                raise ZoteroError("reservation_lookup_incomplete")
        marked = []
        for item in candidates:
            data = item["data"]
            relations = data.get("relations", {}).get("dc:relation", [])
            relations = [relations] if isinstance(relations, str) else relations
            if marker in relations:
                if data.get("deleted") or data.get("itemType") != payload["itemType"]:
                    raise ZoteroError("reservation_identity_conflict")
                if payload["itemType"] == "attachment":
                    if data.get("parentItem") != payload["parentItem"] or data.get("contentType") != payload["contentType"]:
                        raise ZoteroError("reservation_identity_conflict")
                elif doi_key(data.get("DOI")) != doi_key(payload.get("DOI")) or normalize(data.get("title")) != normalize(payload["title"]):
                    raise ZoteroError("reservation_identity_conflict")
                marked.append(item)
        if len(marked) > 1:
            raise ZoteroError("duplicate_reservation_in_library")
        if marked:
            return marked[0]
        body = {**payload, "relations": {**payload.get("relations", {})}}
        existing = body["relations"].get("dc:relation", [])
        existing = [existing] if isinstance(existing, str) else existing
        body["relations"]["dc:relation"] = list(dict.fromkeys([*existing, marker]))
        token = hashlib.sha256((self.prefix + ":server-key:" + reservation_key).encode()).hexdigest()[:32]
        response = self.request("POST", self.prefix + "/items", json=[body], headers={"Zotero-Write-Token": token})
        result = response.json()
        if result.get("failed"):
            raise ZoteroError("zotero_server_key_write_failed_" + str(next(iter(result["failed"].values())).get("code", "unknown")))
        saved = result.get("successful", {}).get("0")
        assigned = saved.get("key") if isinstance(saved, dict) else result.get("success", {}).get("0")
        if not assigned:
            raise ZoteroError("assigned_key_not_returned: reconcile reservation on retry")
        item = self.item(assigned)
        if not item:
            raise ZoteroError("write_not_visible_yet")
        return item

    def add_memberships(self, item, collections, tags):
        d = item["data"]
        merged_collections = list(dict.fromkeys(d.get("collections", []) + list(collections)))
        merged_tags = list(d.get("tags", []))
        for tag in tags:
            if not any(t.get("tag") == tag for t in merged_tags):
                merged_tags.append({"tag": tag})
        if merged_collections != d.get("collections", []) or merged_tags != d.get("tags", []):
            self.request("PATCH", self.prefix + "/items/" + item["key"],
                         json={"collections": merged_collections, "tags": merged_tags},
                         headers={"If-Unmodified-Since-Version": str(item["version"])})
            item = self.item(item["key"])
        return item

    def upload(self, attachment_key, path, expected_md5):
        route = self.prefix + "/items/" + key(attachment_key) + "/file"
        body = path.read_bytes()
        response = self.request("POST", route, data={"md5": expected_md5, "filename": path.name,
                     "filesize": len(body), "mtime": int(path.stat().st_mtime * 1000)}, headers={"If-None-Match": "*"})
        auth = response.json()
        if auth.get("exists"):
            return
        upload_url = auth.get("url", "")
        parsed = urlsplit(upload_url)
        if self.transport == "local":
            good = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"} and parsed.port == 23119 and parsed.path.startswith("/api/local/uploads/")
        else:
            good = parsed.scheme == "https" and (parsed.hostname or "").endswith(".amazonaws.com") and parsed.port in {None, 443}
        if not good or parsed.username or parsed.password:
            raise ZoteroError("unexpected_upload_destination")
        payload = auth.get("prefix", "").encode() + body + auth.get("suffix", "").encode()
        # Signed upload destination is authorized by Zotero. Never forward API credentials to it.
        with requests.Session() as upload_session:
            if self.transport == "local":
                upload_session.trust_env = False
            r = upload_session.post(upload_url, data=payload, headers={"Content-Type": auth.get("contentType", "application/octet-stream")}, timeout=self.timeout, allow_redirects=False)
        if r.status_code not in {200, 201, 204}:
            raise ZoteroError(f"upload_http_{r.status_code}")
        self.request("POST", route, data={"upload": auth["uploadKey"]}, headers={"If-None-Match": "*"})

    def verify_stored_file(self, attachment_key, expected_md5):
        if self.transport != "local":
            return "server_metadata_md5"  # Web transport cannot attest local availability.
        response = self.request("GET", self.prefix + "/items/" + key(attachment_key) + "/file/view/url")
        parsed = urlsplit(response.text.strip()) if response is not None else None
        if not parsed or parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
            raise ZoteroError("attachment_file_unavailable")
        path = Path(url2pathname(parsed.path))
        if not path.is_file() or path.stat().st_size > 50 * 1024 * 1024:
            raise ZoteroError("attachment_file_unavailable")
        if hashlib.md5(path.read_bytes()).hexdigest() != expected_md5:
            raise ZoteroError("stored_attachment_content_mismatch")
        return "local_file_bytes_md5"

    def ensure_attachment(self, paper, parent_key, receipt, reserve):
        path = local_file(receipt["pdf_path"])
        if path.stat().st_size > 50 * 1024 * 1024:
            raise ZoteroError("pdf_too_large")
        body = path.read_bytes()
        valid, evidence = verify_pdf(body, paper)
        if not valid:
            raise ZoteroError("pdf_identity_check_failed")
        md5 = hashlib.md5(body).hexdigest()
        attachment_key = stable_key(self.prefix + ":pdf:" + parent_key + ":" + md5)
        # Check existing child files by hash, including attachments created outside this tool.
        for child in self.children(parent_key):
            data = child["data"]
            if not data.get("deleted") and data.get("contentType") == "application/pdf" and data.get("md5") == md5:
                verification = self.verify_stored_file(child["key"], md5)
                return {"status": "verified", "attachment_key": child["key"], "md5": md5,
                        "identity_check": evidence, "storage_check": verification, "reused": True}
        reserve(attachment_key, md5)
        item = self.item(attachment_key)
        if item is not None:
            data = item["data"]
            if data.get("deleted") or data.get("parentItem") != parent_key or data.get("contentType") != "application/pdf":
                raise ZoteroError("attachment_key_conflict")
            if data.get("md5") and data["md5"] != md5:
                raise ZoteroError("attachment_content_conflict")
        else:
            item = self.create({"itemType": "attachment", "parentItem": parent_key,
                                "linkMode": "imported_file", "title": "Full Text PDF", "contentType": "application/pdf",
                                "filename": path.name, "tags": [{"tag": "comm_papers"}]}, attachment_key)
            attachment_key = item["key"]
            reserve(attachment_key, md5)
        if item["data"].get("md5") != md5:
            self.upload(attachment_key, path, md5)
        verified = self.item(attachment_key)
        if not verified or verified["data"].get("md5") != md5 or verified["data"].get("parentItem") != parent_key:
            raise ZoteroError("attachment_readback_failed")
        verification = self.verify_stored_file(attachment_key, md5)
        return {"status": "verified", "attachment_key": attachment_key, "md5": md5,
                "identity_check": evidence, "storage_check": verification, "reused": False}


def metadata_payload(paper):
    extra = paper.get("extra", {})
    structured = extra.get("creators")
    creators = structured or [{"creatorType": "author", "name": name} for name in paper.get("authors", [])]
    work_type = extra.get("crossref_type") or extra.get("work_type") or ""
    kind = "thesis" if "thesis" in work_type else "conferencePaper" if "proceeding" in work_type or "conference" in work_type else "journalArticle"
    payload = {"itemType": kind, "title": paper["title"], "creators": creators,
               "date": paper.get("published_date", "")[:10], "DOI": paper.get("doi", ""),
               "url": paper.get("url") or ("https://doi.org/" + paper["doi"] if paper.get("doi") else ""),
               "abstractNote": paper.get("abstract", ""), "language": paper.get("language") or "en",
               "extra": "Source: comm_papers", "tags": [], "collections": []}
    payload["university" if kind == "thesis" else "proceedingsTitle" if kind == "conferencePaper" else "publicationTitle"] = paper.get("venue", "")
    if kind == "thesis":
        if paper.get("doi"):
            payload["extra"] += "\nDOI: " + payload["DOI"]
        payload.pop("DOI", None)
        payload["thesisType"] = "博士学位论文" if extra.get("db_code") == "CDFD" else "硕士学位论文"
    for field, source in (("volume", "volume"), ("issue", "issue"), ("pages", "page")):
        if kind != "thesis" and extra.get(source) and (kind != "conferencePaper" or field != "issue"):
            payload[field] = str(extra[source])
    return payload
