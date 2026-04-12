"""
Upwork job scraper using Scrapling.

This keeps the same job-card selectors as the Playwright version in
scraper.py, but uses Scrapling's session fetcher instead of a persistent
browser profile.
"""

from __future__ import annotations

import html as html_lib
import random
import re
import time
from urllib.parse import urljoin

from colorama import Fore, Style
from scrapling.fetchers import StealthySession

UPWORK_BASE = "https://www.upwork.com"


def _parse_posted_age_minutes(posted_text):
    if not posted_text:
        return None

    text = posted_text.lower().strip()

    if any(kw in text for kw in ["just now", "seconds ago", "moment", "second ago"]):
        return 0

    m = re.search(r"(\d+)\s*(?:minute|min)", text)
    if m:
        return int(m.group(1))

    m = re.search(r"(\d+)\s*hour", text)
    if m:
        return int(m.group(1)) * 60

    m = re.search(r"(\d+)\s*day", text)
    if m:
        return int(m.group(1)) * 60 * 24

    if "yesterday" in text:
        return 60 * 24
    if "week" in text:
        return 60 * 24 * 7
    if "month" in text:
        return 60 * 24 * 30

    return None


def _is_younger_than_age(posted_text, max_minutes=15):
    age = _parse_posted_age_minutes(posted_text)
    if age is None:
        print(f"{Style.DIM}    Time filter: could not parse '{posted_text}', skipping job{Style.RESET_ALL}")
        return False
    if age >= max_minutes:
        print(f"{Style.DIM}    Time filter: '{posted_text}' = {age}min >= {max_minutes}min{Style.RESET_ALL}")
        return False
    return True


def _dedupe_keep_order(items: list[str]) -> list[str]:
    seen = set()
    ordered: list[str] = []
    for item in items:
        item = (item or "").strip()
        if not item:
            continue
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(item)
    return ordered


def _normalise_url(url: str) -> str:
    if not url:
        return ""
    url = url.strip()
    if url.startswith("http"):
        return url
    return urljoin(UPWORK_BASE, url)


def _first_text(node, selectors: list[str]) -> str:
    for sel in selectors:
        try:
            found = node.css(sel)
            if not found:
                continue
            for item in found:
                text = (item.text or "").strip()
                if text:
                    return text
        except Exception:
            continue
    return ""


def _strip_html_to_text(raw_html: str) -> str:
    raw_html = raw_html or ""
    raw_html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw_html, flags=re.I | re.S)
    raw_html = re.sub(r"<[^>]+>", " ", raw_html)
    raw_html = html_lib.unescape(raw_html)
    raw_html = re.sub(r"\s+", " ", raw_html).strip()
    return raw_html


def _node_text(node) -> str:
    """Extract text from Scrapling nodes, falling back to HTML stripping."""
    raw_html = ""
    html_text = ""
    try:
        text = (node.text or "").strip()
    except Exception:
        text = ""

    try:
        raw_html = node.get() or ""
        html_text = _strip_html_to_text(raw_html)
    except Exception:
        html_text = ""

    if html_text and len(html_text) >= len(text):
        return html_text
    if text:
        return text
    return html_text


def _extract_description(page) -> str:
    selectors = [
        '[data-test="description"]',
        '[data-test*="job-description"]',
        '.job-description',
        'div.air3-slider-body section:nth-child(2) > div > p',
        'div.air3-slider-body [data-test="description"]',
        'div.air3-slider-body .job-description',
        'div.air3-slider-body section p',
        'div.details-slider p',
        'main p',
        'section p',
        'article p',
    ]

    for sel in selectors:
        try:
            found = page.css(sel)
            for item in found:
                text = (item.text or "").strip()
                if len(text) > 50:
                    return text
        except Exception:
            continue

    try:
        body_html = page.css("body").get() or ""
        body_text = _strip_html_to_text(body_html)
        if body_text:
            lowered = body_text.lower()
            for marker in ["job description", "description", "about the job"]:
                idx = lowered.find(marker)
                if idx >= 0:
                    snippet = body_text[idx: idx + 4000].strip()
                    if len(snippet) > 50:
                        return snippet
            if len(body_text) > 200:
                return body_text[:4000]
    except Exception:
        pass

    return ""


def _fetch(session, url: str):
    print(f"{Style.DIM}  Fetching: {url[:90]}...{Style.RESET_ALL}")
    return session.fetch(url)


def scrape_search_page(session, search_url: str):
    jobs = []
    page = _fetch(session, search_url)

    cards = page.css("article")
    if not cards:
        print(f"{Fore.YELLOW}  No job cards found on page{Style.RESET_ALL}")
        return jobs

    print(f"{Fore.GREEN}  Found {len(cards)} job listings{Style.RESET_ALL}")

    for i, card in enumerate(cards):
        try:
            link_candidates = card.css('a[data-test="job-tile-title-link"]')
            if not link_candidates:
                link_candidates = card.css('a[href*="/jobs/"]')
            if not link_candidates:
                continue

            link_el = link_candidates[0]
            title = _node_text(link_el)
            if not title:
                title = _node_text(card.css("h2").first) if card.css("h2") else ""
            href = link_el.attrib.get("href", "")
            job_url = _normalise_url(href)
            if not title or not job_url:
                continue

            posted_time = _first_text(card, [
                '[data-test="job-pubilshed-date"]',
                'small[data-test="job-pubilshed-date"]',
                '[data-test="posted-on"]',
                'span[data-test="posted-on"]',
                'small span:nth-child(2)',
                'small span',
                'small.text-muted',
                'small',
            ])

            budget = _first_text(card, [
                '[data-test="budget"]',
                '[data-test="is-fixed-price"]',
            ])

            skills = ""
            try:
                skill_nodes = card.css('[data-test="token"] span, a[data-test="attr-item"], span.air3-badge')
                skill_list = []
                for node in skill_nodes[:10]:
                    text = (node.text or "").strip()
                    if text:
                        skill_list.append(text)
                skills = ", ".join(skill_list)
            except Exception:
                pass

            jobs.append({
                "title": title,
                "job_url": job_url,
                "posted_time": posted_time,
                "budget": budget,
                "skills": skills,
                "description": "",
                "_card_index": i,
            })
        except Exception as e:
            print(f"{Style.DIM}  Skipped card {i}: {e}{Style.RESET_ALL}")

    return jobs


def get_job_summaries(session, jobs):
    if not jobs:
        return jobs

    print(f"{Style.DIM}  Extracting job summaries ({len(jobs)} jobs)...{Style.RESET_ALL}")

    for job in jobs:
        try:
            detail_page = _fetch(session, job["job_url"])
            summary = _extract_description(detail_page)
            job["description"] = summary
            if summary:
                print(f"{Fore.GREEN}  Got summary: {job['title'][:45]}... ({len(summary)} chars){Style.RESET_ALL}")
            else:
                print(f"{Style.DIM}  No summary found for: {job['title'][:45]}{Style.RESET_ALL}")
        except Exception as e:
            print(f"{Style.DIM}  Error on job {job.get('title', '?')[:30]}: {e}{Style.RESET_ALL}")

    return jobs


def scrape_all_jobs(search_urls, headless=False, max_minutes=15):
    """
    Scrape all configured search URLs using Scrapling.

    The logic matches scraper.py:
    1. Load each search page
    2. Extract job cards
    3. Filter to jobs younger than max_minutes
    4. Load the job page and extract the full description
    5. Deduplicate across URLs
    """
    all_jobs = []
    seen_urls = set()

    with StealthySession(headless=headless, solve_cloudflare=True) as session:
        for i, url in enumerate(search_urls):
            print(f"\n{Fore.BLUE}  Search URL {i + 1}/{len(search_urls)}{Style.RESET_ALL}")

            raw_jobs = scrape_search_page(session, url)

            recent_jobs = []
            for job in raw_jobs:
                if job["job_url"] in seen_urls:
                    continue
                if _is_younger_than_age(job["posted_time"], max_minutes):
                    recent_jobs.append(job)
                    seen_urls.add(job["job_url"])
                else:
                    print(f"{Style.DIM}  Skipped (too old: {job['posted_time']}): {job['title'][:40]}{Style.RESET_ALL}")

            print(f"{Fore.WHITE}  {len(recent_jobs)} jobs younger than {max_minutes} min{Style.RESET_ALL}")

            if recent_jobs:
                get_job_summaries(session, recent_jobs)

            all_jobs.extend(recent_jobs)

            if i < len(search_urls) - 1:
                wait_sec = 2.0 + random.random() * 3.0
                print(f"{Style.DIM}  Waiting {wait_sec:.1f}s before next search...{Style.RESET_ALL}")
                time.sleep(wait_sec)

    return all_jobs
