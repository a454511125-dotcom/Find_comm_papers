import asyncio
import copy
import hashlib
import json
import os
from unittest.mock import Mock

import pytest
import requests

from paper_search_mcp import comm_auth as auth
from paper_search_mcp import comm_manifest as manifests
from paper_search_mcp import comm_server as server
from paper_search_mcp import comm_workflow as workflow
from paper_search_mcp import comm_zotero as zotero
from paper_search_mcp.comm_ranking import canonical, rank_papers
from tests.test_comm_workflow import pdf_bytes


def paper(n=1, **extra):
    return canonical({"title": f"Algorithmic recommendations and political polarization {n}",
                      "doi": f"10.1234/paper{n}", "authors": ["Ada Test"],
                      "published_date": "2023-01-01", "venue": "Journal of Communication", **extra})


@pytest.fixture
def library(monkeypatch, tmp_path):
    monkeypatch.setenv("COMM_MCP_DATA_DIR", str(tmp_path))
    # Any forgotten mock must fail before accessing an external or local library.
    monkeypatch.setattr(requests.sessions.Session, "request", Mock(side_effect=AssertionError("unexpected network")))
    return tmp_path


class FakeClient:
    transport = "local"
    scope = {"transport": "local", "library_type": "user", "library_id": "0", "server_id": "test"}

    def __init__(self):
        self.items = {}
        self.created = []
        self.attachments = {}
        self.timeout_once = False

    def factory(self, **kwargs):
        return self

    def close(self):
        pass

    def check_collections(self, keys):
        if "INVALID1" in keys:
            raise zotero.ZoteroError("collection_not_found")

    def item(self, key):
        return copy.deepcopy(self.items.get(key))

    def find(self, record):
        matches = [x for x in self.items.values() if x["data"].get("DOI") == record["doi"]]
        if len(matches) > 1:
            raise zotero.ZoteroError("duplicate_doi_in_library")
        return copy.deepcopy(matches[0]) if matches else None

    def create(self, payload, key):
        assert key not in self.items
        self.items[key] = {"key": key, "version": 1, "data": copy.deepcopy(payload)}
        self.created.append(key)
        if self.timeout_once:
            self.timeout_once = False
            raise requests.Timeout("committed but response lost")
        return self.item(key)

    def add_memberships(self, item, collections, tags):
        return item

    def ensure_attachment(self, record, parent, receipt, reserve):
        from pathlib import Path
        md5 = hashlib.md5(Path(receipt["pdf_path"]).read_bytes()).hexdigest()
        key = zotero.stable_key(parent + md5)
        reserve(key, md5)
        reused = key in self.attachments
        self.attachments[key] = parent
        return {"status": "verified", "attachment_key": key, "md5": md5, "reused": reused}


def downloader(root):
    def get(record, _):
        path = root / (manifests.entry_key(record) + ".pdf")
        if not path.exists():
            path.write_bytes(pdf_bytes(record["doi"]))
        return {"status": "downloaded", "pdf_path": str(path), "paper": record}
    return get


def run(run_id, client, root, **kwargs):
    return workflow.execute(run_id, client_factory=client.factory, downloader=downloader(root),
                            enricher=lambda x: x, **kwargs)


def test_selection_preserves_order_reasons_and_distinct_dois(library):
    records = [paper(2), paper(1), paper(2), paper(3, title=paper(1)["title"])]
    records[0]["selection_reason"] = "Communication first after review"
    result = manifests.create(records, "political polarization")
    state = manifests.load(result["manifest_id"])
    assert [e["paper"]["doi"] for e in state["entries"]] == ["10.1234/paper2", "10.1234/paper1", "10.1234/paper3"]
    assert state["entries"][0]["selection_reason"] == records[0]["selection_reason"]
    assert result["metadata_verified"] == result["attachments_verified"] == 0


def test_snapshot_recovers_from_partial_write_without_overwrite(library):
    selection = manifests.create([paper()], "query")
    root = manifests.run_dir(selection["manifest_id"])
    broken = root / "revision_000002.json"
    broken.write_text('{"broken":', encoding="utf-8")
    state = manifests.load(selection["manifest_id"])
    manifests.save(state)
    assert broken.read_text() == '{"broken":'
    assert (root / "revision_000003.json").exists()


def test_same_twenty_papers_twice_and_new_selection_do_not_duplicate(library):
    client = FakeClient()
    records = [paper(n) for n in range(20)]
    for _ in range(2):
        run_id = manifests.create(records, "query")["manifest_id"]
        assert run(run_id, client, library, limit=10)["complete"] == 10
        final = run(run_id, client, library, limit=10)
        assert final["complete"] == final["attachments_verified"] == 20
        assert run(run_id, client, library, limit=10)["attempted_this_call"] == 0
    assert len(client.created) == len(client.attachments) == 20


def test_retry_does_not_import_unattempted_entries(library):
    client = FakeClient()
    client.timeout_once = True
    run_id = manifests.create([paper(1), paper(2)], "query")["manifest_id"]
    run(run_id, client, library, limit=1)
    result = run(run_id, client, library, retry_only=True)
    assert result["complete"] == 1
    assert result["entries"][1]["status"] == "pending"
    assert len(client.created) == 1


def test_doi_less_identity_checks_author_and_year(library):
    record = paper(doi="")
    item = {"data": {"itemType": "journalArticle", "title": record["title"], "date": "2023",
                     "creators": [{"firstName": "Ada", "lastName": "Test"}]}}
    assert workflow.matches(item, record)
    item["data"]["creators"][0]["firstName"] = "Another"
    assert not workflow.matches(item, record)
    item["data"]["creators"][0]["firstName"] = "Ada"
    item["data"]["date"] = "2022"
    assert not workflow.matches(item, record)


def test_timeout_after_creation_reuses_reserved_key(library):
    client = FakeClient()
    client.timeout_once = True
    run_id = manifests.create([paper()], "query")["manifest_id"]
    first = run(run_id, client, library)
    assert first["entries"][0]["status"] == "needs_retry"
    final = run(run_id, client, library, retry_only=True)
    assert final["complete"] == 1
    assert len(client.created) == 1


def test_pdf_failure_retains_metadata_and_retry_only_attaches(library):
    client = FakeClient()
    run_id = manifests.create([paper()], "query")["manifest_id"]
    result = workflow.execute(run_id, client_factory=client.factory, enricher=lambda x: x,
                              downloader=lambda *_: {"status": "not_downloaded"})
    assert result["metadata_verified"] == 1 and result["attachments_verified"] == 0
    assert run(run_id, client, library, retry_only=True)["complete"] == 1
    assert len(client.created) == 1


def test_missing_cached_pdf_reacquired(library):
    receipt = {"status": "downloaded", "pdf_path": str(library / "never-existed.pdf"), "paper": paper()}
    run_id = manifests.create([receipt], "query")["manifest_id"]
    assert run(run_id, FakeClient(), library)["complete"] == 1


def test_existing_item_preserved_and_missing_previous_item_not_recreated(library):
    client = FakeClient()
    original = {"itemType": "journalArticle", "DOI": paper()["doi"], "title": "User corrected title", "tags": [{"tag": "mine"}]}
    client.items["EXIST234"] = {"key": "EXIST234", "version": 7, "data": original}
    run_id = manifests.create([paper()], "query")["manifest_id"]
    partial = workflow.execute(run_id, client_factory=client.factory,
                               downloader=lambda *_: {"status": "not_downloaded"})
    assert partial["metadata_verified"] == 1
    assert client.items["EXIST234"]["data"] == original
    # Simulate a remote library change using a separate in-memory store.
    client.items = {}
    retried = run(run_id, client, library, retry_only=True)
    assert retried["entries"][0]["last_attempt"]["error"].startswith("previously_imported_item_missing")
    assert client.created == []


def test_target_and_options_frozen(library):
    client = FakeClient()
    run_id = manifests.create([paper()], "query")["manifest_id"]
    run(run_id, client, library, download_pdfs=False)
    with pytest.raises(ValueError, match="target is frozen"):
        run(run_id, client, library, collections=["ABCD2345"], download_pdfs=False)
    with pytest.raises(ValueError, match="options are frozen"):
        run(run_id, client, library)


def test_no_authorization_does_not_change_manifest(library):
    run_id = manifests.create([paper()], "query")["manifest_id"]
    def refused(**kwargs):
        raise zotero.ZoteroError("local_write_authorization_required")
    with pytest.raises(zotero.ZoteroError):
        workflow.execute(run_id, client_factory=refused)
    assert manifests.load(run_id)["revision"] == 1
    assert asyncio.run(server.comm_zotero_authorize(False))["status"] == "authorization_required"


def bare_client():
    client = object.__new__(zotero.ZoteroClient)
    client.prefix = "/users/0"
    client.transport = "local"
    client.timeout = 5
    client.api = "http://127.0.0.1:23119/api"
    client.headers = {"Zotero-API-Key": "SECRET", "Zotero-Server-ID": "test"}
    return client


def response(code=200, data=None, **attrs):
    return Mock(status_code=code, json=Mock(return_value=data), **attrs)


def test_authorization_errors_never_become_item_absence(library):
    client = bare_client()
    client.session = Mock()
    for status in (401, 403, 412, 500):
        client.session.request.return_value = response(status)
        with pytest.raises(zotero.ZoteroError, match=f"zotero_http_{status}"):
            client.item("ABCD2345")
    client.session.request.return_value = response(404)
    assert client.item("ABCD2345") is None


def test_create_explicit_key_requires_version_zero_and_does_not_overwrite(library):
    client = bare_client()
    saved = {"key": "ABCD2345", "data": {"title": "Saved title"}}
    client.item = Mock(return_value=saved)
    client.request = Mock(return_value=response(data={"successful": {"0": saved}, "failed": {}}))
    assert client.create({"itemType": "journalArticle", "title": "Saved title"}, "ABCD2345") == saved
    body = client.request.call_args.kwargs["json"][0]
    assert body["key"] == "ABCD2345" and body["version"] == 0
    client.request.return_value = response(data={"failed": {"0": {"code": 412, "message": "Key already exists"}}})
    with pytest.raises(zotero.ZoteroError, match="zotero_item_write_failed_412"):
        client.create({"itemType": "journalArticle", "title": "Conflicting title"}, "ABCD2345")


def test_server_key_fallback_reconciles_lost_creation_response(library):
    client = bare_client()
    storage = []
    client.children = lambda _: copy.deepcopy(storage)
    client.item = lambda k: next((copy.deepcopy(x) for x in storage if x["key"] == k), None)
    def request(method, route, **kwargs):
        body = kwargs["json"][0]
        assert "key" not in body and "version" not in body
        assert "Zotero-Write-Token" in kwargs["headers"]
        storage.append({"key": "SERVER23", "data": body})
        raise requests.Timeout("Saved but response lost")
    client.request = Mock(side_effect=request)
    payload = {"itemType": "attachment", "parentItem": "ABCD2345", "contentType": "application/pdf"}
    with pytest.raises(requests.Timeout):
        client.create_server_key(payload, "RESERV23")
    recovered = client.create_server_key(payload, "RESERV23")
    assert recovered["key"] == "SERVER23"
    assert client.request.call_count == 1


def test_only_confirmed_local_primarydata_bug_uses_fallback(library):
    client = bare_client()
    client.request = Mock(return_value=response(data={"failed": {"0": {"code": 400, "message": "'primaryData' not loaded for item"}}}))
    client.create_server_key = Mock(return_value={"key": "SERVER23"})
    assert client.create({"itemType": "attachment"}, "ABCD2345")["key"] == "SERVER23"
    client.transport = "web"
    with pytest.raises(zotero.ZoteroError):
        client.create({"itemType": "attachment"}, "ABCD2345")
    assert client.create_server_key.call_count == 1


def test_upload_three_phases_never_forward_api_key(library, monkeypatch):
    path = library / "fulltext.pdf"
    path.write_bytes(pdf_bytes(paper()["doi"]))
    digest = hashlib.md5(path.read_bytes()).hexdigest()
    client = bare_client()
    client.request = Mock(side_effect=[response(data={"url": "http://localhost:23119/api/local/uploads/test",
                         "uploadKey": "upload", "contentType": "application/octet-stream", "prefix": "", "suffix": ""}), response(204)])
    upload = Mock()
    upload.post.return_value = response(201)
    session = Mock()
    session.__enter__ = Mock(return_value=upload)
    session.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(zotero.requests, "Session", Mock(return_value=session))
    client.upload("ABCD2345", path, digest)
    assert client.request.call_args_list[0].kwargs["data"]["md5"] == digest
    assert client.request.call_args_list[1].kwargs["data"] == {"upload": "upload"}
    assert upload.post.call_args.kwargs["headers"] == {"Content-Type": "application/octet-stream"}
    assert upload.post.call_args.kwargs["data"] == path.read_bytes()
    assert upload.post.call_args.kwargs["allow_redirects"] is False


def test_upload_rejects_unexpected_destination(library):
    path = library / "sample.pdf"
    path.write_bytes(b"x")
    client = bare_client()
    client.request = Mock(return_value=response(data={"url": "https://evil.example/upload"}))
    with pytest.raises(zotero.ZoteroError, match="unexpected_upload_destination"):
        client.upload("ABCD2345", path, "digest")


def test_pdf_identity_failure_prevents_attachment_creation(library):
    path = library / "wrong.pdf"
    path.write_bytes(pdf_bytes("10.1234/another-paper"))
    client = bare_client()
    client.create = Mock()
    with pytest.raises(zotero.ZoteroError, match="pdf_identity_check_failed"):
        client.ensure_attachment(paper(), "ABCD2345", {"pdf_path": str(path)}, Mock())
    client.create.assert_not_called()


def test_attachment_committed_but_response_lost_reconciles(library):
    record = paper()
    receipt = downloader(library)(record, False)
    client = bare_client()
    storage = {}
    client.children = Mock(return_value=[])
    client.item = lambda key: copy.deepcopy(storage.get(key))
    def create(payload, key):
        storage[key] = {"key": key, "data": payload}
        return client.item(key)
    client.create = Mock(side_effect=create)
    def committed(key, path, md5):
        storage[key]["data"]["md5"] = md5
        raise requests.Timeout()
    client.upload = Mock(side_effect=committed)
    client.request = Mock(return_value=Mock(text=__import__("pathlib").Path(receipt["pdf_path"]).as_uri()))
    with pytest.raises(requests.Timeout):
        client.ensure_attachment(record, "ABCD2345", receipt, Mock())
    result = client.ensure_attachment(record, "ABCD2345", receipt, Mock())
    assert result["status"] == "verified"
    assert client.create.call_count == client.upload.call_count == 1


def test_stored_file_must_exist_and_match_not_just_metadata(library):
    path = library / "stored.pdf"
    path.write_bytes(b"actual stored bytes")
    client = bare_client()
    client.request = Mock(return_value=Mock(text=path.as_uri()))
    md5 = hashlib.md5(path.read_bytes()).hexdigest()
    assert client.verify_stored_file("ABCD2345", md5) == "local_file_bytes_md5"
    with pytest.raises(zotero.ZoteroError, match="stored_attachment_content_mismatch"):
        client.verify_stored_file("ABCD2345", "wrong")
    client.request.return_value = Mock(text=(library / "missing.pdf").as_uri())
    with pytest.raises(zotero.ZoteroError, match="attachment_file_unavailable"):
        client.verify_stored_file("ABCD2345", md5)


@pytest.mark.skipif(os.name != "nt", reason="Windows-only credential encryption")
def test_windows_credential_encryption_round_trip():
    plaintext = b"synthetic-test-key-never-a-real-credential"
    ciphertext = auth.protect(plaintext)
    assert plaintext not in ciphertext
    assert auth.protect(ciphertext, decrypt=True) == plaintext


def test_authorization_declined_never_saved_or_retried(library, monkeypatch):
    monkeypatch.setattr(auth, "probe", lambda: {"status": "available", "server_id": "test"})
    session = Mock()
    session.__enter__ = Mock(return_value=session)
    session.__exit__ = Mock(return_value=False)
    session.post.return_value = response(403, {"denied": True})
    monkeypatch.setattr(auth.requests, "Session", Mock(return_value=session))
    assert auth.authorize()["status"] == "not_authorized"
    assert session.post.call_count == 1
    assert not (library / "authorization").exists()


def test_auth_success_stores_only_encrypted_key(library, monkeypatch):
    monkeypatch.setattr(auth, "probe", lambda: {"status": "available", "server_id": "test"})
    session = Mock()
    session.__enter__ = Mock(return_value=session)
    session.__exit__ = Mock(return_value=False)
    session.post.return_value = response(data={"key": "synthetic-key", "remember": True})
    monkeypatch.setattr(auth.requests, "Session", Mock(return_value=session))
    monkeypatch.setattr(auth, "protect", lambda _: b"encrypted")
    result = auth.authorize()
    assert result["status"] == "authorized"
    assert "synthetic-key" not in json.dumps(result)
    assert list((library / "authorization").glob("*.dpapi"))[0].read_bytes() == b"encrypted"


def test_synonyms_political_context_and_venue_identity():
    results = rank_papers([
        paper(1, title="Recommender systems and ideological polarisation", venue="Science"),
        paper(2, title="Algorithms for macrophage polarization", abstract="Cell differentiation and immune response.", venue="Nature")
    ], "algorithmic recommendation political polarization")
    assert results[0]["doi"] == "10.1234/paper1"
    assert results[0]["ranking"]["venue_groups"] == ["multidisciplinary"]
    assert results[1]["ranking"]["review_flags"]


def test_query_variants_merge_provenance(monkeypatch):
    async def search(query, *args):
        record = paper()
        record["provenance"] = [{"source": "mock", "provider_rank": 1}]
        return {"papers": [record], "query": query, "sources": {"mock": {"status": "ok"}}}
    monkeypatch.setattr(server, "comm_search", search)
    result = asyncio.run(server.comm_search_many(["algorithmic recommendation", "news feed"], "political polarization"))
    assert result["count"] == 1
    assert {p.get("retrieval_query") for p in result["papers"][0]["provenance"]} >= {"algorithmic recommendation", "news feed"}
