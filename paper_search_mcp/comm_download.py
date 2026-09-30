"""Bounded retrieval with identity checks and non-overwriting local files."""
from __future__ import annotations

import io
import ipaddress
import json
import os
import re
import socket
import time
import uuid
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

from .config import get_env
from .comm_ranking import canonical, doi_key, load_profile, normalize, tokens
from .academic_platforms.sci_hub import SciHubFetcher


def data_dir() -> Path:
    fallback = Path(__file__).resolve().parents[1] / "library"
    root = Path(os.environ.get("COMM_MCP_DATA_DIR", str(fallback))).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def local_file(path: str) -> Path:
    result = Path(path).resolve(strict=True)
    if not result.is_relative_to(data_dir()):
        raise ValueError("File must be inside COMM_MCP_DATA_DIR")
    return result


def validate_public_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("Only public HTTP(S) URLs without user credentials are supported")
    if parts.port not in (None, 80, 443):
        raise ValueError("Unexpected remote port")
    addresses = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80), type=socket.SOCK_STREAM)
    # Some desktop TUN proxies map public names into the benchmarking range.
    # Explicit local opt-in supports that mapping; literal/non-public hostnames
    # and every other private/reserved range remain blocked.
    fake_pool = ipaddress.ip_network("198.18.0.0/15")
    try:
        ipaddress.ip_address(parts.hostname)
        hostname_is_public_name = False
    except ValueError:
        hostname_is_public_name = "." in parts.hostname and not parts.hostname.endswith((".local", ".localhost", ".internal", ".lan", ".test"))
    allow_fake = os.environ.get("COMM_MCP_ALLOW_PROXY_FAKE_IP") == "1" and hostname_is_public_name
    def permitted(address):
        ip = ipaddress.ip_address(address)
        return ip.is_global or (allow_fake and ip in fake_pool)
    if not addresses or any(not permitted(a[4][0]) for a in addresses):
        raise ValueError("Private, loopback, reserved and link-local targets are blocked")
    return url


def fetch_bytes(url: str, max_bytes: int, timeout: float, headers: dict | None = None) -> tuple[bytes, str, str]:
    """Revalidate redirects; never forward an API key across hosts."""
    origin = urlsplit(url).hostname
    deadline = time.monotonic() + timeout
    for _ in range(6):
        validate_public_url(url)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Download time budget exceeded")
        request_headers = {"User-Agent": "comm-paper-mcp/0.1 (academic research)"}
        if urlsplit(url).hostname == origin:
            request_headers.update(headers or {})
        with requests.get(url, headers=request_headers, stream=True, allow_redirects=False, timeout=min(remaining, 15)) as response:
            if response.status_code in {301, 302, 303, 307, 308}:
                url = urljoin(url, response.headers["Location"])
                continue
            response.raise_for_status()
            length = response.headers.get("Content-Length", "")
            if length.isdigit() and int(length) > max_bytes:
                raise ValueError("Response exceeds configured size limit")
            chunks, size = [], 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError("Response exceeds configured size limit")
                if time.monotonic() > deadline:
                    raise TimeoutError("Download time budget exceeded")
                chunks.append(chunk)
            return b"".join(chunks), url, response.headers.get("Content-Type", "")
    raise ValueError("Too many redirects")


def fetch_json(url: str, headers: dict | None = None) -> dict:
    content, _, _ = fetch_bytes(url, 8 * 1024 * 1024, 20, headers)
    return json.loads(content)


def verify_pdf(content: bytes, paper: dict) -> tuple[bool, str]:
    if b"%PDF-" not in content[:1024]:
        return False, "not_pdf"
    try:
        reader = PdfReader(io.BytesIO(content))
        if not reader.pages:
            return False, "empty_pdf"
        first_page = reader.pages[0].extract_text() or ""
        expected = tokens(paper.get("title", ""))
        compact = re.sub(r"\s+", "", first_page.casefold())
        doi = doi_key(paper.get("doi"))
        doi_match = bool(doi and doi in compact)
        title_match = bool(expected and len(expected & tokens(first_page)) / len(expected) >= 0.75)
        if paper.get("language", "").startswith("zh") or paper.get("source") == "cnki":
            expected_title = re.sub(r"[\W_]+", "", paper.get("title", ""), flags=re.UNICODE)
            page_title = re.sub(r"[\W_]+", "", first_page, flags=re.UNICODE)
            title_match = len(expected_title) >= 6 and expected_title in page_title
        if doi_match or title_match:
            return True, "doi_on_first_page" if doi_match else "title_on_first_page"
        return False, "identity_unverified"
    except Exception:
        return False, "pdf_parse_failed"


def pdf_links(content: bytes, url: str) -> list[str]:
    soup = BeautifulSoup(content, "html.parser")
    links = []
    for meta in soup.select('meta[name="citation_pdf_url"], meta[name="wkhealth_pdf_url"]'):
        if meta.get("content"):
            links.append(urljoin(url, meta["content"]))
    for element in soup.select('a[href], embed[src], iframe[src]'):
        href = element.get("href") or element.get("src") or ""
        if ".pdf" in href.casefold() or "/pdf/" in href.casefold() or "/download/" in href.casefold():
            links.append(urljoin(url, href))
    return list(dict.fromkeys(links))[:4]


def oa_candidates(work: dict) -> list[dict]:
    candidates = []
    locations = [work.get("best_oa_location") or {}, *(work.get("locations") or [])]
    for location in locations:
        if not location.get("is_oa"):
            continue
        url = location.get("pdf_url") or location.get("landing_page_url")
        if url:
            candidates.append({"url": url, "source": "openalex_oa", "version": location.get("version"), "license": location.get("license")})
    content_url = (work.get("content_urls") or {}).get("pdf")
    key = get_env("OPENALEX_API_KEY")
    if content_url and key and urlsplit(content_url).hostname == "content.openalex.org":
        candidates.append({"url": content_url, "source": "openalex_content", "headers": {"Authorization": f"Bearer {key}"}})
    return candidates


def safe_error(exc: Exception) -> str:
    # Never return request URLs, Authorization headers or proxy credentials.
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return f"HTTP {status}" if status else type(exc).__name__


def download_selected(record: dict, use_scihub: bool | None = None) -> dict:
    if record.get("source") == "wos":
        from .comm_wos import download_sync
        return download_sync(record, use_scihub)
    if record.get("source") == "cnki":
        from .comm_cnki import download_sync
        return download_sync(record, use_scihub)
    return download_one(record, use_scihub)


def download_one(record: dict, use_scihub: bool | None = None) -> dict:
    profile = load_profile()
    settings = profile["download"]
    paper = canonical(record)
    if not paper["title"] and not paper["doi"]:
        raise ValueError("A title or DOI is required to verify the downloaded paper")
    use_scihub = settings["use_scihub"] if use_scihub is None else use_scihub
    timeout = min(60, max(5, float(settings["timeout_seconds"])))
    maximum = min(100, max(1, float(settings["max_pdf_mb"]))) * 1024 * 1024
    attempts, seen, browser_candidates = [], set(), []
    root = data_dir()
    total_budget = min(180, max(20, float(settings.get("total_timeout_seconds", 90))))
    deadline = time.monotonic() + total_budget
    browser_enabled = os.environ.get("COMM_BROWSER_FALLBACK", "1") != "0" and settings.get("browser_fallback", True)
    browser_budget = min(30, total_budget * 0.4) if browser_enabled else 0
    http_deadline = deadline - browser_budget

    def remaining():
        left = http_deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("Per-paper retrieval budget exceeded")
        return min(timeout, left)

    def save_verified(body, location, source, candidate):
        valid, evidence = verify_pdf(body, paper)
        if not valid:
            attempts.append({"source": source, "status": evidence})
            return None
        stem = re.sub(r"[^\w.-]+", "_", paper["title"], flags=re.UNICODE).strip("_.")[:70] or "paper"
        output = root / f"{stem}_{uuid.uuid4().hex[:12]}.pdf"
        with output.open("xb") as f:
            f.write(body)
        receipt = {"status": "downloaded", "pdf_path": str(output), "source": source,
                   "download_url": location, "identity_check": evidence,
                   "version": candidate.get("version", "unknown"), "license": candidate.get("license"),
                   "paper": paper, "attempts": attempts}
        try:
            with output.with_suffix(".json").open("x", encoding="utf-8") as stream:
                json.dump(receipt, stream, ensure_ascii=False, indent=2)
        except Exception as exc:
            receipt["receipt_error"] = safe_error(exc)
        return receipt

    def attempt(candidate):
        url = candidate.get("url")
        if not url or url in seen:
            return None
        seen.add(url)
        source = candidate["source"]
        # Browser sessions never receive API credentials or Sci-Hub candidates.
        if browser_enabled and source != "scihub" and not candidate.get("headers"):
            browser_candidates.append(candidate)
        try:
            content, final_url, _ = fetch_bytes(url, int(maximum), remaining(), candidate.get("headers"))
            possibilities = [(content, final_url)]
            if b"%PDF-" not in content[:1024]:
                possibilities = []
                for link in pdf_links(content, final_url):
                    if link in seen:
                        continue
                    seen.add(link)
                    try:
                        body, location, _ = fetch_bytes(link, int(maximum), remaining())
                        possibilities.append((body, location))
                    except Exception as exc:
                        attempts.append({"source": source, "status": safe_error(exc)})
            for body, location in possibilities:
                receipt = save_verified(body, location, source, candidate)
                if receipt:
                    return receipt
            attempts.append({"source": source, "status": "no_verified_pdf"})
        except Exception as exc:
            attempts.append({"source": source, "status": safe_error(exc)})
        return None

    candidates = []
    if paper.get("pdf_url"):
        candidates.append({"url": paper["pdf_url"], "source": "record_pdf"})
    for item in paper.get("provenance", []):
        if item.get("pdf_url"):
            candidates.append({"url": item["pdf_url"], "source": item.get("source", "record")})
    candidates.extend(oa_candidates(paper["extra"]))
    if paper.get("url"):
        candidates.append({"url": paper["url"], "source": "publisher_page"})
    for candidate in candidates[:8]:
        result = attempt(candidate)
        if result:
            return result

    if paper["doi"]:
        try:
            remaining()
            key = get_env("OPENALEX_API_KEY")
            work = fetch_json("https://api.openalex.org/works/https://doi.org/" + quote(paper["doi"], safe="/"), {"Authorization": f"Bearer {key}"} if key else None)
            for candidate in oa_candidates(work)[:8]:
                result = attempt(candidate)
                if result:
                    return result
        except Exception as exc:
            attempts.append({"source": "openalex_resolve", "status": safe_error(exc)})
        email = get_env("UNPAYWALL_EMAIL")
        if email:
            try:
                remaining()
                data = fetch_json(f"https://api.unpaywall.org/v2/{quote(paper['doi'], safe='/')}?email={quote(email)}")
                for location in (data.get("oa_locations") or [])[:5]:
                    result = attempt({"url": location.get("url_for_pdf") or location.get("url"), "source": "unpaywall", "version": location.get("version"), "license": location.get("license")})
                    if result:
                        return result
            except Exception as exc:
                attempts.append({"source": "unpaywall", "status": safe_error(exc)})
        else:
            attempts.append({"source": "unpaywall", "status": "email_not_configured"})

    if browser_enabled and browser_candidates and time.monotonic() < deadline:
        from .comm_browser import fetch_browser_pdf
        # Try OA repository locations first, then the record's own landing page.
        ordered = sorted(browser_candidates, key=lambda c: c["source"] not in {"openalex_oa", "unpaywall"})
        for candidate in ordered[:2]:
            left = deadline - time.monotonic()
            if left <= 1:
                break
            try:
                result = fetch_browser_pdf(candidate["url"], paper, timeout=min(browser_budget, left), max_bytes=int(maximum))
                if result.get("status") == "downloaded":
                    receipt = save_verified(result["content"], result["final_url"], "headless_browser", candidate)
                    if receipt:
                        return receipt
                attempts.append({"source": "headless_browser", "status": result.get("status", "failed"),
                                 **{k: result[k] for k in ("reason", "diagnostic_snapshot", "screenshot") if k in result}})
                if result.get("reason") in {"browser_not_configured", "browser_missing", "browser_busy"}:
                    break
            except Exception as exc:
                attempts.append({"source": "headless_browser", "status": safe_error(exc)})

    # Unused browser time remains available to the explicitly enabled last fallback.
    http_deadline = deadline
    if use_scihub and paper["doi"]:
        base = settings["scihub_base_url"].rstrip("/")
        try:
            remaining()
            validate_public_url(base)
            # Reuse upstream Sci-Hub HTML resolver with our bounded HTTP client.
            class Response:
                def __init__(self, url):
                    self.content, self.url, _ = fetch_bytes(url, 4 * 1024 * 1024, remaining())
                    self.status_code = 200
                    self.text = self.content.decode("utf-8", "replace")
            class Session:
                def get(self, url, **kwargs):
                    return Response(url)
            resolver = SciHubFetcher(base_url=base, output_dir=str(root))
            resolver.session.close()
            resolver.session = Session()
            pdf_url = resolver._get_direct_url(paper["doi"])
            result = attempt({"url": pdf_url, "source": "scihub"}) if pdf_url else None
            if result:
                return result
            attempts.append({"source": "scihub", "status": "no_available_pdf"})
        except Exception as exc:
            attempts.append({"source": "scihub", "status": safe_error(exc)})
    return {"status": "not_downloaded", "paper": paper, "attempts": attempts,
            "scihub_enabled": use_scihub, "pdf_path": None}


def read_pdf(path: str, start_page: int = 1, page_count: int = 3) -> dict:
    if start_page < 1 or not 1 <= page_count <= 20:
        raise ValueError("Pages are 1-based; page_count must be 1..20")
    file = local_file(path)
    if file.suffix.casefold() != ".pdf":
        raise ValueError("Only PDF files are supported")
    reader = PdfReader(str(file))
    if start_page > len(reader.pages):
        raise ValueError("start_page exceeds document length")
    pages = []
    for number in range(start_page, min(start_page + page_count, len(reader.pages) + 1)):
        raw = reader.pages[number - 1].extract_text() or ""
        pages.append({"page": number, "text": raw[:20000], "truncated": len(raw) > 20000})
    return {"path": str(file), "total_pages": len(reader.pages), "pages": pages,
            "note": "Page numbers refer to PDF file pages; image-only pages require OCR."}
