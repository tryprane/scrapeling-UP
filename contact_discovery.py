"""
Contact discovery for qualified leads.

Combines:
1. Gemini browser search results
2. Public web search fallbacks
3. Website email scraping on any discovered domains
"""

from __future__ import annotations

from colorama import Fore

from gemini_searcher import search_contacts_via_gemini
from website_scraper import (
    extract_emails_from_text,
    extract_website_urls_from_text,
    is_company_website,
    scrape_emails_from_websites,
)


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


def _filter_company_websites(urls: list[str]) -> list[str]:
    """Keep only company-like website URLs and preserve order."""
    filtered: list[str] = []
    seen = set()
    for raw in urls:
        url = (raw or "").strip()
        if not url:
            continue
        key = url.lower()
        if key in seen:
            continue
        seen.add(key)
        if is_company_website(url):
            filtered.append(url)
    return filtered


def build_lead_summary(job: dict, lead_result: dict) -> str:
    """Build a compact summary used for contact discovery searches."""
    ci = lead_result.get("client_info", {}) if lead_result else {}
    return (
        f"Job Title: {job.get('title', '')}\n"
        f"Budget: {job.get('budget', 'Not specified')}\n"
        f"Skills: {job.get('skills', '')}\n"
        f"Company: {ci.get('company_name', 'Unknown')}\n"
        f"Person: {ci.get('guessed_person', 'Unknown')}\n"
        f"Website: {ci.get('website', '')}\n"
        f"Search Query: {ci.get('search_query_used', '')}\n"
        f"Description: {job.get('description', '')[:500]}\n"
    )


def discover_contacts(page, job: dict, lead_result: dict, logger=None) -> dict:
    """
    Search for contact data using Gemini, public web fallbacks and website scraping.

    Args:
        page: Browser page used by Gemini and website scraping.
        job: Scraped job dict.
        lead_result: Gemini analysis result for the job.
        logger: Optional log callback with signature logger(stage, message, **fields).
    """
    lead_summary = build_lead_summary(job, lead_result)
    ci = lead_result.get("client_info", {}) if lead_result else {}

    def log(stage: str, message: str, **fields):
        if logger:
            logger(stage, message, **fields)

    log("contact-discovery", "searching Gemini for contact breadcrumbs")
    contacts = {
        "emails": [],
        "linkedin_urls": [],
        "instagram_handles": [],
        "twitter_handles": [],
        "websites": [],
        "phone_numbers": [],
        "other_contacts": [],
        "summary": "",
    }
    search_response = ""

    try:
        search_response = search_contacts_via_gemini(page, lead_summary)
    except Exception as exc:
        log("contact-discovery", f"Gemini contact search failed: {exc}", level="warning")

    if search_response:
        contacts["search_response"] = search_response
        found_emails = extract_emails_from_text(search_response)
        found_websites = _filter_company_websites(extract_website_urls_from_text(search_response))
        contacts["emails"].extend(found_emails)
        contacts["websites"].extend(found_websites)
        log(
            "contact-discovery",
            "Gemini contact search returned candidates",
            email_count=len(found_emails),
            website_count=len(found_websites),
        )

    websites = list(contacts.get("websites", []))

    ci_website = (ci.get("website", "") or "").strip()
    if ci_website and is_company_website(ci_website):
        websites.append(ci_website)

    websites = _filter_company_websites(_dedupe_keep_order(websites))
    scraped_emails: list[str] = []
    if websites:
        log(
            "contact-discovery",
            "scraping candidate websites",
            website_count=len(websites),
            sample=websites[:4],
        )
        try:
            scraped_emails = scrape_emails_from_websites(page, websites)
        except Exception as exc:
            log("contact-discovery", f"website scrape failed: {exc}", level="warning")

    merged_emails = _dedupe_keep_order([
        *(contacts.get("emails", []) or []),
        *scraped_emails,
    ])

    contacts["emails"] = merged_emails
    contacts["websites"] = websites
    contacts["scraped_emails"] = scraped_emails
    contacts["search_response"] = search_response

    return contacts
