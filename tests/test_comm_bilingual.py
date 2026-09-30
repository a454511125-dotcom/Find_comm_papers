import asyncio
from types import SimpleNamespace

import pytest

from paper_search_mcp import comm_bilingual as bilingual
from paper_search_mcp import comm_cnki
from paper_search_mcp import comm_download
from paper_search_mcp import comm_manifest as manifests
from paper_search_mcp.comm_cnki_host import RetainedResponses
from paper_search_mcp.comm_zotero import identity_matches, metadata_payload


def paper(title, *, language="en", source="openalex", doi="", venue="", **extra):
    return {
        "title": title,
        "authors": ["Ada Test"],
        "published_date": "2024-01-01",
        "language": language,
        "source": source,
        "doi": doi,
        "venue": venue,
        "abstract": title,
        **extra,
    }


def test_same_doi_stays_separate_across_languages_and_manifest(monkeypatch, tmp_path):
    monkeypatch.setenv("COMM_MCP_DATA_DIR", str(tmp_path))
    zh = paper("算法推荐与政治极化", language="zh", source="cnki", doi="10.1234/same")
    en = paper("Algorithmic recommendation and political polarization", doi="10.1234/same")

    combined = bilingual.combine([zh], [en], 10)
    assert [(item["language"], item["doi"]) for item in combined] == [
        ("zh", "10.1234/same"),
        ("en", "10.1234/same"),
    ]

    saved = manifests.create(combined, "算法推荐 / algorithmic recommendation")
    assert saved["count"] == 2
    assert saved["entries"][0]["entry_id"] != saved["entries"][1]["entry_id"]
    assert {entry["language"] for entry in saved["entries"]} == {"zh", "en"}


def test_one_language_failure_preserves_the_other_lane():
    async def failed_cnki(**_kwargs):
        return {"success": False, "papers": [], "message": "offline"}

    async def english(**_kwargs):
        return {"papers": [paper("Algorithmic recommendation and polarization")], "searches": []}

    result = asyncio.run(
        bilingual.search(
            query="算法推荐与政治极化",
            chinese_query="算法推荐",
            english_queries=["algorithmic recommendation political polarization"],
            languages=["zh", "en"],
            max_results=10,
            per_language=10,
            year_start=2020,
            year_end=2025,
            english_sources=["wos"],
            db_code="CJFD",
            weights=None,
            cnki_search=failed_cnki,
            english_search=english,
        )
    )

    assert result["sources"]["zh"]["status"] == "unavailable"
    assert result["sources"]["en"]["status"] == "ok"
    assert result["counts_by_language"] == {"zh": 0, "en": 1}
    assert result["papers"][0]["title"] == "Algorithmic recommendation and polarization"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"query": ""}, "research query"),
        ({"languages": ["fr"]}, "languages"),
        ({"chinese_query": ""}, "chinese_query"),
        ({"chinese_query": "算法推荐 政治极化"}, "one keyword"),
        ({"chinese_query": "算法推荐，政治极化"}, "one keyword"),
        ({"chinese_query": "算法推荐 AND 政治极化"}, "one keyword"),
        ({"english_queries": []}, "english_queries"),
        ({"year_start": 2026, "year_end": 2020}, "year range"),
        ({"db_code": "INVALID"}, "db_code"),
        ({"max_results": 101}, "limits"),
    ],
)
def test_bilingual_query_validation(overrides, message):
    arguments = {
        "query": "算法推荐与政治极化",
        "chinese_query": "算法推荐",
        "english_queries": ["algorithmic recommendation political polarization"],
        "languages": ["zh", "en"],
        "max_results": 20,
        "per_language": 20,
        "year_start": 2020,
        "year_end": 2025,
        "english_sources": ["wos"],
        "db_code": "CJFD",
        "weights": None,
        "cnki_search": None,
        "english_search": None,
    }
    arguments.update(overrides)
    with pytest.raises(ValueError, match=message):
        asyncio.run(bilingual.search(**arguments))


def test_chinese_communication_journal_wins_when_relevance_is_equal():
    records = [
        paper("算法推荐与政治极化", language="zh", source="cnki", venue="中国科学"),
        paper("算法推荐与政治极化", language="zh", source="cnki", venue="新闻与传播研究"),
    ]
    ranked = bilingual.rank_chinese(records, "算法推荐 政治极化")
    assert ranked[0]["venue"] == "新闻与传播研究"
    assert ranked[0]["ranking"]["discipline_group"] == "communication"
    assert ranked[0]["ranking"]["components"]["relevance"] == ranked[1]["ranking"]["components"]["relevance"]


def test_cnki_record_normalizes_authors_year_and_nonnumeric_citations():
    record = bilingual.cnki_record(
        {
            "title": "算法推荐研究",
            "authors": "张三；李四,王五",
            "year": "网络首发于2023年12月",
            "citations": "被引 17 次",
            "journal": "国际新闻界",
        },
        rank=3,
    )
    assert record["authors"] == ["张三", "李四", "王五"]
    assert record["published_date"] == "2023"
    assert record["citations"] == 0
    assert record["provider_rank"] == 3


def test_chinese_thesis_metadata_and_identity():
    thesis = bilingual.cnki_record(
        {
            "title": "算法推荐与政治极化研究",
            "authors": "张三",
            "year": "2022",
            "journal": "中国传媒大学",
            "doi": "10.1234/thesis",
        },
        db_code="CDFD",
    )
    payload = metadata_payload(thesis)
    assert payload["itemType"] == "thesis"
    assert payload["university"] == "中国传媒大学"
    assert payload["thesisType"] == "博士学位论文"
    assert "DOI" not in payload
    assert "DOI: 10.1234/thesis" in payload["extra"]
    assert payload["language"] == "zh"

    zotero_item = {
        "data": {
            **payload,
            "creators": [{"creatorType": "author", "name": "张三"}],
        }
    }
    assert identity_matches(zotero_item, thesis)


class FakeResponse:
    def __init__(self, headers, body):
        self.headers = headers
        self._body = body

    async def body(self):
        return self._body


class FakeRoute:
    def __init__(self, response, resource_type="document"):
        self.request = SimpleNamespace(resource_type=resource_type, url="https://kns.cnki.net/test")
        self.response = response
        self.aborted = False
        self.fulfilled = None
        self.continued = False

    async def fetch(self, **_kwargs):
        return self.response

    async def abort(self):
        self.aborted = True

    async def fulfill(self, **kwargs):
        self.fulfilled = kwargs

    async def continue_(self):
        self.continued = True


def test_retained_route_writes_one_pdf_and_aborts_response(tmp_path):
    retained = RetainedResponses(tmp_path)
    route = FakeRoute(FakeResponse({"content-type": "application/pdf"}, b"%PDF-1.7\ncontent"))
    asyncio.run(retained.route(route))

    files = list(tmp_path.iterdir())
    assert len(files) == 1
    assert files[0].suffix == ".pdf"
    assert files[0].read_bytes().startswith(b"%PDF-")
    assert retained.files == [{
        "success": True,
        "format": "pdf",
        "file_path": str(files[0]),
        "message": "Response retained without a browser download artifact",
    }]
    assert route.aborted and route.fulfilled is None


def test_retained_route_fulfills_html_without_writing(tmp_path):
    retained = RetainedResponses(tmp_path)
    response = FakeResponse({"content-type": "text/html; charset=utf-8"}, b"<html>ok</html>")
    route = FakeRoute(response)
    asyncio.run(retained.route(route))

    assert not tmp_path.exists() or list(tmp_path.iterdir()) == []
    assert retained.files == []
    assert route.fulfilled == {"response": response}
    assert not route.aborted


def test_retained_route_keeps_caj_without_mislabeling_pdf(tmp_path):
    retained = RetainedResponses(tmp_path)
    route = FakeRoute(FakeResponse({"content-type": "application/caj"}, b"CAJ\x00document"))
    asyncio.run(retained.route(route))

    assert len(retained.files) == 1
    assert retained.files[0]["success"] is True
    assert retained.files[0]["format"] == "caj"
    assert retained.files[0]["file_path"].endswith(".caj")
    assert not retained.files[0]["file_path"].endswith(".pdf")


def test_cnki_caj_receipt_never_runs_pdf_identity_check(monkeypatch, tmp_path):
    monkeypatch.setenv("COMM_MCP_DATA_DIR", str(tmp_path))
    retained = tmp_path / "paper.caj"
    retained.write_bytes(b"CAJ\x00document")

    async def fake_call(name, arguments):
        assert name == "cnki_download"
        return {"success": True, "format": "caj", "file_path": str(retained)}

    monkeypatch.setattr(comm_cnki, "call", fake_call)
    monkeypatch.setattr(comm_cnki, "verify_pdf", lambda *_args: pytest.fail("CAJ must not be verified as PDF"))
    receipt = asyncio.run(comm_cnki.download(paper("中文论文", language="zh", source="cnki")))

    assert receipt["status"] == "unsupported_format"
    assert receipt["retained_path"] == str(retained.resolve())
    assert "pdf_path" not in receipt


def test_cnki_download_dispatch_never_uses_english_downloader(monkeypatch):
    calls = []

    def cnki_download(record, use_scihub=None):
        calls.append((record["title"], use_scihub))
        return {"status": "not_downloaded", "source": "cnki"}

    monkeypatch.setattr(comm_cnki, "download_sync", cnki_download)
    monkeypatch.setattr(
        comm_download,
        "download_one",
        lambda *_args: pytest.fail("CNKI records must not use the English/OA downloader"),
    )
    result = comm_download.download_selected(
        paper("中文论文", language="zh", source="cnki"),
        use_scihub=True,
    )

    assert result["source"] == "cnki"
    assert calls == [("中文论文", True)]


def test_mixed_reranking_does_not_merge_equal_dois():
    from paper_search_mcp.comm_server import comm_rank
    result = comm_rank([
        paper("算法推荐与政治极化", source="cnki", language="zh", doi="10.1234/same"),
        paper("Algorithmic recommendation and political polarization", doi="10.1234/same"),
    ], "算法推荐 政治极化 algorithmic recommendation political polarization")
    assert result["count"] == 2
    assert [p["language"] for p in result["papers"]] == ["zh", "en"]


def test_chinese_pdf_requires_matching_full_title(monkeypatch):
    expected = paper("算法推荐与政治极化的关系", source="cnki", language="zh")
    def reader(text):
        return lambda *_: SimpleNamespace(pages=[SimpleNamespace(extract_text=lambda: text)])
    monkeypatch.setattr(comm_download, "PdfReader", reader("算法 推荐与政治极化\n的关系"))
    assert comm_download.verify_pdf(b"%PDF-1.7", expected) == (True, "title_on_first_page")
    monkeypatch.setattr(comm_download, "PdfReader", reader("算法推荐与政治极化的测量"))
    assert comm_download.verify_pdf(b"%PDF-1.7", expected) == (False, "identity_unverified")


def test_newline_separated_cnki_authors():
    result = bilingual.cnki_record({"title": "论文", "authors": "张三\n李四；王五", "year": 2024})
    assert result["authors"] == ["张三", "李四", "王五"]


def test_cnki_retrieves_one_keyword_but_ranks_for_whole_question():
    sent = []
    async def cnki(**args):
        sent.append(args["query"])
        return {"success": True, "papers": [
            {"title": "算法推荐的商业应用", "year": "2024", "journal": "国际新闻界"},
            {"title": "算法推荐与政治极化", "year": "2024", "journal": "国际新闻界"},
        ]}
    result = asyncio.run(bilingual.search(
        query="2020年以来算法推荐与政治极化", chinese_query="算法推荐", english_queries=None,
        languages=["zh"], max_results=10, per_language=10, year_start=2020, year_end=2025,
        english_sources=None, db_code="CJFD", weights=None, cnki_search=cnki, english_search=None))
    assert sent == ["算法推荐"]
    assert result["queries"]["zh"] == "算法推荐"
    assert result["papers"][0]["title"] == "算法推荐与政治极化"
    assert "政治极化" in result["papers"][0]["ranking"]["query_terms_matched"]

