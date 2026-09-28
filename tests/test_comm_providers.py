"""Offline adapter checks against the actual bundled source APIs."""
import ast
from datetime import date
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from paper_search_mcp import comm_providers as providers
from paper_search_mcp.paper import Paper


@pytest.fixture(autouse=True)
def prohibit_network(monkeypatch):
    def unexpected(*_args, **_kwargs):
        raise AssertionError("Provider adapter tests must never access the network")
    monkeypatch.setattr(requests.sessions.Session, "request", unexpected)


def install_provider(monkeypatch, source, records=None, error="", failure=None, nested=False):
    state = SimpleNamespace(calls=[], closed=0)

    class Session:
        def close(self):
            state.closed += 1

    class Searcher:
        def __init__(self):
            self.last_error = error
            if nested:
                self.resolver = SimpleNamespace(session=Session())
            else:
                self.session = Session()

        def search(self, query, max_results=10, **kwargs):
            state.calls.append((query, max_results, kwargs))
            if failure:
                raise failure
            return records or []

    monkeypatch.setitem(providers.PROVIDERS, source, Searcher)
    return state


def test_registry_reuses_every_upstream_searcher_without_importing_legacy_server():
    path = Path(providers.__file__).with_name("server.py")
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    imported_searchers = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("academic_platforms."):
            imported_searchers.update(alias.name for alias in node.names if alias.name.lower().endswith("searcher"))
    registered = {cls.__name__ for cls in providers.PROVIDERS.values()}
    assert imported_searchers <= registered
    assert len(providers.PROVIDERS) == 24
    assert providers.PROVIDERS["openalex"] is providers.PROVIDERS["openalex_communication"]


@pytest.mark.parametrize("source,kwargs", [
    ("openalex", {"filter": "publication_year:2020-2024"}),
    ("crossref", {"filter": "from-pub-date:2020-01-01,until-pub-date:2024-12-31"}),
    ("semantic", {"year": "2020-2024"}),
    ("citeseerx", {"year": "2020-2024"}),
    ("core", {"publishedAfter": "2020-01-01", "publishedBefore": "2024-12-31"}),
    ("openaire", {"from_date": "2020-01-01", "to_date": "2024-12-31"}),
    ("iacr", {"fetch_details": False}),
    ("google_scholar", {"timeout_seconds": 20.0}),
    ("pubmed", {}), ("pmc", {}), ("base", {}), ("dblp", {}),
    ("hal", {}), ("zenodo", {}), ("doaj", {}), ("europepmc", {}), ("ssrn", {}),
])
def test_native_arguments_match_actual_search_signatures(monkeypatch, source, kwargs):
    signature = inspect.signature(providers.PROVIDERS[source].search)
    state = install_provider(monkeypatch, source)
    result = providers.search_records(source, "algorithmic polarization", 7, 2020, 2024)
    assert result["status"] == "empty_or_unreported_error"
    assert state.calls == [("algorithmic polarization", 7, kwargs)]
    signature.bind(None, "algorithmic polarization", max_results=7, **kwargs)
    assert state.closed == 1


@pytest.mark.parametrize("source", ["hal", "zenodo", "doaj", "europepmc"])
def test_exact_year_only_for_connectors_without_year_range_argument(monkeypatch, source):
    state = install_provider(monkeypatch, source)
    providers.search_records(source, "news", 5, 2023, 2023)
    assert state.calls[0][2] == {"year": 2023}


def test_arxiv_plain_words_expand_but_explicit_query_is_preserved(monkeypatch):
    state = install_provider(monkeypatch, "arxiv")
    first = providers.search_records("arxiv", "recommendation polarization", 5, 2020, None)
    second = providers.search_records("arxiv", 'ti:"political polarization"', 5, None, None)
    assert first["query_sent"] == "all:recommendation AND all:polarization"
    assert second["query_sent"] == 'ti:"political polarization"'
    assert state.calls[0][2] == {}


def test_communication_alias_keeps_the_native_source_and_targeted_issns(monkeypatch):
    monkeypatch.setattr(providers, "load_profile", lambda: {"communication_issns": ["1111-1111", "2222-2222"]})
    state = install_provider(monkeypatch, "openalex_communication", [{"title": "News", "source": "openalex"}])
    result = providers.search_records("openalex_communication", "news", 5, 2020, None)
    assert state.calls[0][2] == {
        "filter": "publication_year:2020-2100,primary_location.source.issn:1111-1111|2222-2222"}
    assert result["papers"][0]["source"] == "openalex"
    assert result["papers"][0]["discovery_channel"] == "openalex_communication"


def test_canonical_metadata_rank_and_session_cleanup(monkeypatch):
    record = Paper("sample", "Algorithms &amp; News", ["Ada Test"], "<p>Evidence</p>",
                   "https://doi.org/10.1234/ABC", date(2023, 2, 3), "https://example.org/p.pdf",
                   "https://example.org/p", "hal", citations=7, extra={"venue": "Journal of Communication"})
    state = install_provider(monkeypatch, "hal", [record, {"title": "Second"}])
    result = providers.search_records("hal", "news", 2, None, None)
    first, second = result["papers"]
    assert result["status"] == "ok" and result["count"] == 2
    assert first["title"] == "Algorithms & News"
    assert first["doi"] == "10.1234/abc"
    assert first["published_date"] == "2023-02-03"
    assert first["venue"] == "Journal of Communication" and first["citations"] == 7
    assert first["provider_rank"] == 1 and second["provider_rank"] == 2
    assert second["source"] == "hal" and second["discovery_channel"] == "hal"
    assert state.closed == 1


@pytest.mark.parametrize("source", ["ieee", "acm"])
def test_placeholders_are_reported_as_unimplemented_without_fake_results(monkeypatch, source):
    monkeypatch.setitem(providers.PROVIDERS, source, lambda: pytest.fail("Placeholder must not be instantiated"))
    result = providers.search_records(source, "news", 5, None, None)
    assert result["status"] == "unsupported"
    assert result["error"] == "upstream_search_not_implemented"
    assert result["papers"] == []


def test_unpaywall_requires_a_doi_and_email_before_instantiation(monkeypatch):
    monkeypatch.setitem(providers.PROVIDERS, "unpaywall", lambda: pytest.fail("No eligible DOI request"))
    monkeypatch.setattr(providers, "get_env", lambda *_: "")
    broad = providers.search_records("unpaywall", "political polarization", 5, None, None)
    doi = providers.search_records("unpaywall", "10.1234/test", 5, None, None)
    assert broad["error"] == "doi_required"
    assert doi["status"] == "credentials_required"


def test_unpaywall_extracts_doi_and_closes_its_nested_resolver_session(monkeypatch):
    monkeypatch.setattr(providers, "get_env", lambda *_: "researcher@example.org")
    state = install_provider(monkeypatch, "unpaywall", [{"title": "OA paper"}], nested=True)
    result = providers.search_records("unpaywall", "https://doi.org/10.1234/test", 5, None, None)
    assert state.calls == [("10.1234/test", 5, {})]
    assert result["status"] == "ok" and state.closed == 1


@pytest.mark.parametrize("source,query,supported,actual", [
    ("biorxiv", "algorithmic polarization", False, ""),
    ("medrxiv", "algorithmic polarization", False, ""),
    ("biorxiv", "category:neuroscience", True, "neuroscience"),
    ("medrxiv", "category:public health", True, "public health"),
    ("biorxiv", "2023-01-01/2023-02-01", True, "2023-01-01/2023-02-01"),
    ("biorxiv", "10.1101/2023.123456", True, "10.1101/2023.123456"),
    ("medrxiv", "10.1101/2023.123456", False, ""),
])
def test_preprint_archive_query_modes_are_not_misrepresented_as_keyword_search(monkeypatch, source, query, supported, actual):
    state = install_provider(monkeypatch, source)
    result = providers.search_records(source, query, 5, None, None)
    if supported:
        assert state.calls == [(actual, 5, {})]
        assert result["status"] == "empty_or_unreported_error"
    else:
        assert state.calls == [] and result["status"] == "unsupported_query"


def test_provider_errors_preserved_without_leaking_url_credentials(monkeypatch):
    state = install_provider(monkeypatch, "openalex", error="HTTP 429 https://example.org/?api_key=secret")
    result = providers.search_records("openalex", "news", 5, None, None)
    assert result["status"] == "error" and result["error"] == "HTTP 429"
    assert "secret" not in str(result) and state.closed == 1


def test_search_exception_closes_session_and_reports_only_safe_error(monkeypatch):
    state = install_provider(monkeypatch, "core", failure=RuntimeError("https://private.example/?key=secret"))
    result = providers.search_records("core", "news", 5, None, None)
    assert result["status"] == "error" and result["error"] == "RuntimeError"
    assert "secret" not in str(result) and state.closed == 1


def test_constructor_failure_becomes_source_diagnostic(monkeypatch):
    def unavailable():
        raise ImportError("optional dependency unavailable")
    monkeypatch.setitem(providers.PROVIDERS, "core", unavailable)
    result = providers.search_records("core", "news", 5, None, None)
    assert result["status"] == "error" and result["error"] == "ImportError"
