"""
Public web search helpers for contact discovery.

This module uses a lightweight HTML search request to discover company
websites and public contact pages when the AI response does not already
contain enough direct contact data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import parse_qs, quote_plus, unquote, urlparse
from urllib.request import Request, urlopen


DUCKDUCKGO_HTML = "https://html.duckduckgo.com/html/?q="
RESULT_LINK_RE = re.compile(r'href="([^"]+)"[^>]*class="result__a"')
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,10}")

SKIP_DOMAINS = {
    "linkedin.com",
    "x.com",
    "twitter.com",
    "facebook.com",
    "instagram.com",
    "youtube.com",
    "upwork.com",
    "crunchbase.com",
    "wikipedia.org",
    "reddit.com",
    "medium.com",
}


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str = ""


def _is_company_domain(url: str) -> bool:
    try:
        netloc = urlparse(url).netloc.lower().lstrip("www.")
        return bool(netloc) and not any(netloc == d or netloc.endswith("." + d) for d in SKIP_DOMAINS)
    except Exception:
        return False


def _normalize_url(raw_url: str) -> str:
    if raw_url.startswith("/l/?"):
        query = parse_qs(urlparse(raw_url).query)
        if "uddg" in query and query["uddg"]:
            return unquote(query["uddg"][0])
    return raw_url


def search_public_web(query: str, max_results: int = 5) -> list[SearchResult]:
    """
    Query the public web for likely company websites or contact pages.

    The function intentionally stays small and dependency-free so it can be
    used as a fallback inside the outreach pipeline.
    """
    query = (query or "").strip()
    if not query:
        return []

    url = DUCKDUCKGO_HTML + quote_plus(query)
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})

    try:
        with urlopen(request, timeout=20) as response:
            html = response.read().decode("utf-8", errors="replace")
    except Exception:
        return []

    results: list[SearchResult] = []
    for match in RESULT_LINK_RE.finditer(html):
        if len(results) >= max_results:
            break
        raw_url = _normalize_url(match.group(1))
        if not raw_url.startswith("http") or not _is_company_domain(raw_url):
            continue
        results.append(SearchResult(title=raw_url, url=raw_url))

    if not results:
        for candidate in re.findall(r"https?://[^\s\"'<>]+", html):
            if len(results) >= max_results:
                break
            candidate = candidate.rstrip(").,;")
            if _is_company_domain(candidate):
                results.append(SearchResult(title=candidate, url=candidate))

    return results


def extract_contacts_from_text(text: str) -> list[str]:
    """Extract any direct email addresses from arbitrary text."""
    if not text:
        return []
    seen = set()
    emails = []
    for email in EMAIL_RE.findall(text):
        email = email.lower().strip(".,;")
        if email not in seen:
            seen.add(email)
            emails.append(email)
    return emails


def candidate_websites_from_query_terms(terms: Iterable[str]) -> list[str]:
    """
    Search public web for a set of terms and return the most likely company sites.
    """
    websites: list[str] = []
    seen = set()

    for term in terms:
        for result in search_public_web(term, max_results=3):
            if result.url not in seen:
                seen.add(result.url)
                websites.append(result.url)
    return websites
