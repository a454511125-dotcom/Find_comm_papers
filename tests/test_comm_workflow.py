import asyncio
import copy
import io
import json
import socket
from pathlib import Path
from unittest.mock import Mock

import pytest
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

from paper_search_mcp import comm_download as dl
from paper_search_mcp import comm_server as srv
from paper_search_mcp.comm_ranking import canonical, load_profile, merge_papers, rank_papers
from paper_search_mcp.academic_platforms.openalex import OpenAlexSearcher


def paper(title="Social media misinformation diffusion", venue="", doi="", **kwargs):
    return {"title": title, "venue": venue, "doi": doi, "authors": ["Ada Test"],
            "published_date": "2024-01-01", "source": "openalex", "paper_id": "W1", **kwargs}


def pdf_bytes(text):
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    content = DecodedStreamObject()
    content.set_data(f"BT /F1 12 Tf 50 700 Td ({text}) Tj ET".encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(content)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def test_equal_relevance_prefers_communication_but_keeps_science():
    records = [paper(venue="Science", doi="10.1/science"), paper(venue="Journal of Communication", doi="10.1/comm")]
    # Title intentionally avoids a discipline signal so journal prior is tested.
    for p in records:
        p["title"] = "Algorithmic amplification and civic participation"
    result = rank_papers(records, "algorithmic amplification")
    assert [p["doi"] for p in result] == ["10.1/comm", "10.1/science"]


def test_relevant_science_beats_unrelated_communication():
    result = rank_papers([paper("Radio spectrum allocation", "Journal of Communication"),
                          paper("Algorithmic amplification of political misinformation", "Science")],
                         "algorithmic amplification political misinformation")
    assert result[0]["venue"] == "Science"


def test_topic_can_promote_science_paper_to_communication_group():
    assert rank_papers([paper(venue="Nature")], "misinformation")[0]["ranking"]["discipline_group"] == "communication"


def test_no_false_nature_substring_or_telecommunications_signal():
    result = rank_papers([paper("Semiconductor heterostructures", "Nature Materials", categories=["Telecommunications"])], "semiconductor")
    assert result[0]["ranking"]["discipline_group"] == "other"


def test_weights_change_order_and_do_not_mutate_input():
    records = [paper("Algorithmic participation", "Journal of Communication", doi="10.1/a", citations=0),
               paper("Algorithmic participation", "Science", doi="10.1/b", citations=1000)]
    before = copy.deepcopy(records)
    result = rank_papers(records, "algorithmic", {"relevance": 0, "discipline": 0, "recency": 0, "citations": 1})
    assert result[0]["doi"] == "10.1/b"
    assert records == before


@pytest.mark.parametrize("overrides", [{"relevance": -1}, {"relevance": float("nan")}, {"wrong": 1}, {"relevance": 0, "discipline": 0, "recency": 0, "citations": 0}])
def test_invalid_weights_rejected(overrides):
    with pytest.raises(ValueError):
        rank_papers([], "social media", overrides)


def test_doi_merge_preserves_rich_metadata_and_both_sources():
    records = [paper(doi="https://doi.org/10.1234/ABC", pdf_url="", source="crossref"),
               paper(doi="10.1234/abc", pdf_url="https://example.org/a.pdf", source="openalex", extra={"venue": "Science"})]
    result = merge_papers(records)
    assert len(result) == 1
    assert result[0]["pdf_url"]
    assert result[0]["venue"] == "Science"
    assert len(result[0]["provenance"]) == 2


def test_missing_doi_merges_only_with_matching_author_and_title():
    p = paper(doi="10.1234/x")
    assert len(merge_papers([p, paper(source="arxiv")])) == 1
    other = paper(authors=["Other Author"])
    assert len(merge_papers([p, other])) == 2
    assert len(merge_papers([p, paper(doi="10.1234/y")])) == 2


def test_extra_serialization_and_topics_survive():
    p = canonical(paper(extra="{'venue': 'Science', 'topics': ['Social networks']}"))
    assert p["venue"] == "Science"
    assert "Social networks" in p["keywords"]


def test_profile_overrides_loaded_fresh(monkeypatch, tmp_path):
    config = load_profile()
    config["weights"]["discipline"] = 1
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setenv("COMM_MCP_PROFILE", str(path))
    assert load_profile()["weights"]["discipline"] > 0.5


def test_search_date_filter_applies_to_all_sources(monkeypatch):
    async def fake(source, *args):
        return {"status": "ok", "count": 3, "papers": [canonical(paper(published_date=d, doi=f"10.1/{d}")) for d in ["2019-01-01", "2024-01-01", ""]]}
    monkeypatch.setattr(srv, "bounded_search", fake)
    result = asyncio.run(srv.comm_search("social media", sources=["arxiv"], year_start=2020))
    assert result["count"] == 1
    assert result["excluded"]["date"] == 2


def test_source_failures_preserve_other_results(monkeypatch):
    async def fake(source, *args):
        return {"status": "timeout", "count": 0, "papers": []} if source == "semantic" else {"status": "ok", "count": 1, "papers": [canonical(paper())]}
    monkeypatch.setattr(srv, "bounded_search", fake)
    result = asyncio.run(srv.comm_search("social media", sources=["semantic", "crossref"]))
    assert result["count"] == 1
    assert result["sources"]["semantic"]["status"] == "timeout"


def test_retracted_records_excluded(monkeypatch):
    async def fake(*args):
        return {"status": "ok", "papers": [canonical(paper(extra={"is_retracted": True}))]}
    monkeypatch.setattr(srv, "bounded_search", fake)
    result = asyncio.run(srv.comm_search("social media", sources=["openalex"], boost_communication_recall=False))
    assert result["count"] == 0
    assert result["excluded"]["retracted"] == 1


@pytest.mark.parametrize("url", ["file:///C:/private", "http://127.0.0.1/a", "http://192.168.1.1/a", "http://169.254.169.254/", "http://user:pass@example.org/", "http://example.org:23119/"])
def test_private_urls_rejected(url, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))])
    with pytest.raises(ValueError):
        dl.validate_public_url(url)


def test_pdf_identity_and_html_rejection():
    good = pdf_bytes("Social media misinformation diffusion")
    assert dl.verify_pdf(good, paper())[0]
    assert not dl.verify_pdf(b"<html>Access denied</html>", paper())[0]
    assert not dl.verify_pdf(pdf_bytes("Unrelated chemistry article"), paper())[0]


def test_download_receipt_and_no_overwrite(monkeypatch, tmp_path):
    monkeypatch.setenv("COMM_MCP_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(dl, "fetch_bytes", lambda url, *args: (pdf_bytes("Social media misinformation diffusion"), url, "application/pdf"))
    p = paper(pdf_url="https://example.org/p.pdf")
    first = dl.download_one(p, False)
    second = dl.download_one(p, False)
    assert first["status"] == second["status"] == "downloaded"
    assert first["pdf_path"] != second["pdf_path"]
    assert Path(first["pdf_path"]).exists()
    assert Path(first["pdf_path"]).with_suffix(".json").exists()


def test_oa_only_never_contacts_scihub(monkeypatch, tmp_path):
    monkeypatch.setenv("COMM_MCP_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(dl, "fetch_json", Mock(side_effect=RuntimeError("offline")))
    resolver = Mock(side_effect=AssertionError("must not call"))
    monkeypatch.setattr(dl, "SciHubFetcher", resolver)
    result = dl.download_one(paper(doi="10.1234/a"), False)
    assert result["status"] == "not_downloaded"
    resolver.assert_not_called()


def test_scihub_uses_requested_mirror_and_validates_pdf(monkeypatch, tmp_path):
    monkeypatch.setenv("COMM_MCP_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(dl, "fetch_json", Mock(side_effect=RuntimeError("offline")))
    monkeypatch.setattr(dl, "validate_public_url", lambda u: u)
    urls = []
    def fetch(url, *args):
        urls.append(url)
        if "sci-hub.st" in url:
            return b'<embed type="application/pdf" src="https://example.org/p.pdf">', url, "text/html"
        return pdf_bytes("Social media misinformation diffusion"), url, "application/pdf"
    monkeypatch.setattr(dl, "fetch_bytes", fetch)
    result = dl.download_one(paper(doi="10.1234/a"), True)
    assert result["status"] == "downloaded"
    assert result["source"] == "scihub"
    assert urls[0].startswith("https://www.sci-hub.st/")


def test_batch_downloads_in_priority_order_and_continues(monkeypatch, tmp_path):
    monkeypatch.setenv("COMM_MCP_DATA_DIR", str(tmp_path))
    order = []
    def fake(paper, use_scihub):
        order.append(paper["title"])
        if len(order) == 1:
            raise RuntimeError("fail")
        return {"status": "downloaded", "paper": paper}
    monkeypatch.setattr(srv, "download_one", fake)
    result = asyncio.run(srv.comm_download_batch([paper("Unrelated physics"), paper()], "misinformation", 2))
    assert order[0] == "Social media misinformation diffusion"
    assert result["attempted"] == 2 and result["downloaded"] == 1


def test_page_read_and_ris_with_attachment(monkeypatch, tmp_path):
    monkeypatch.setenv("COMM_MCP_DATA_DIR", str(tmp_path))
    path = tmp_path / "test.pdf"
    path.write_bytes(pdf_bytes("Social media misinformation diffusion"))
    read = dl.read_pdf(str(path))
    assert read["pages"][0]["page"] == 1
    assert "misinformation" in read["pages"][0]["text"]
    result = srv.comm_export_ris([{"paper": paper(doi="10.1234/a"), "pdf_path": str(path)}])
    content = Path(result["path"]).read_text(encoding="utf-8")
    assert "DO  - 10.1234/a" in content
    assert "L1  - file:" in content
    assert result["zotero_imported"] is False


def test_openalex_retains_venue_and_oa_locations():
    response = Mock(status_code=200)
    response.json.return_value = {"results": [{"id": "https://openalex.org/W1", "title": "Misinformation", "primary_location": {"source": {"display_name": "Science", "issn": ["0036-8075"]}}, "best_oa_location": {"is_oa": True, "pdf_url": "https://example.org/a.pdf"}, "topics": [{"display_name": "Political communication"}]}]}
    searcher = OpenAlexSearcher(api_key="")
    searcher.session.get = Mock(return_value=response)
    record = canonical(searcher.search("misinformation")[0])
    assert record["venue"] == "Science"
    assert record["extra"]["best_oa_location"]["pdf_url"]
    assert "Political communication" in record["keywords"]


def test_receipt_error_does_not_hide_download_success(monkeypatch, tmp_path):
    monkeypatch.setenv("COMM_MCP_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(dl, "fetch_bytes", lambda url, *args: (pdf_bytes("Social media misinformation diffusion"), url, "application/pdf"))
    monkeypatch.setattr(Path, "write_text", Mock(side_effect=OSError("disk full")))
    result = dl.download_one(paper(pdf_url="https://example.org/p.pdf"), False)
    assert result["status"] == "downloaded"
    assert result["receipt_error"] == "OSError"
    assert Path(result["pdf_path"]).exists()


def test_fake_ip_proxy_optin_is_narrow(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.141", 443))])
    monkeypatch.delenv("COMM_MCP_ALLOW_PROXY_FAKE_IP", raising=False)
    with pytest.raises(ValueError):
        dl.validate_public_url("https://arxiv.org/a")
    monkeypatch.setenv("COMM_MCP_ALLOW_PROXY_FAKE_IP", "1")
    assert dl.validate_public_url("https://arxiv.org/a")
    for url in ("http://198.18.0.141/a", "http://host.local/a", "http://host.internal/a"):
        with pytest.raises(ValueError):
            dl.validate_public_url(url)


def test_communication_venue_beats_same_topic_in_science():
    result = rank_papers([paper(venue="Science", doi="10.1/a"), paper(venue="Journal of Communication", doi="10.1/b")], "social media misinformation")
    assert result[0]["venue"] == "Journal of Communication"


def test_prior_provenance_survives_reranking():
    first = paper(doi="10.1/a", source="openalex")
    second = paper(doi="10.1/a", source="crossref", provenance=[{"source": "semantic", "paper_id": "s2", "provider_rank": 2}])
    merged = merge_papers([first, second])
    assert {p["source"] for p in merged[0]["provenance"]} == {"openalex", "crossref", "semantic"}


def test_core_recall_is_added_alongside_broad_search(monkeypatch):
    requested = []
    async def fake(source, *args):
        requested.append(source)
        return {"status": "ok", "count": 0, "papers": []}
    monkeypatch.setattr(srv, "bounded_search", fake)
    asyncio.run(srv.comm_search("social media", sources=["openalex", "arxiv"]))
    assert set(requested) == {"openalex", "arxiv", "openalex_communication"}


def test_retraction_signal_propagates_across_sources(monkeypatch):
    async def fake(source, *args):
        p = canonical(paper(doi="10.1234/retracted", extra={"is_retracted": source == "openalex"}))
        return {"status": "ok", "count": 1, "papers": [p]}
    monkeypatch.setattr(srv, "bounded_search", fake)
    result = asyncio.run(srv.comm_search("social media", sources=["openalex", "crossref"], boost_communication_recall=False))
    assert result["count"] == 0
    assert result["excluded"]["retracted"] == 2


def test_arxiv_service_error_is_not_reported_as_empty(monkeypatch):
    from paper_search_mcp.academic_platforms.arxiv import ArxivSearcher
    monkeypatch.setattr(ArxivSearcher, "_request_with_retries", lambda *a: Mock(status_code=406))
    result = srv.search_source("arxiv", "misinformation", 2, None, None)
    assert result["status"] == "error"
    assert result["error"] == "HTTP 406"
