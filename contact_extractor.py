"""
Email-only contact extractor.

This helper intentionally avoids any model call so we save Groq tokens.
It scans text for email addresses, dedupes them, and returns a small
structured result that matches the rest of the pipeline.

Can be tested standalone:
    python -c "from contact_extractor import extract_contacts; print(extract_contacts('Email: john@company.com, LinkedIn: linkedin.com/in/john'))"
"""

from __future__ import annotations

import json
import re

from colorama import Fore, Style

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}")


def _dedupe_keep_order(items: list[str]) -> list[str]:
    seen = set()
    ordered: list[str] = []
    for item in items:
        item = (item or "").strip().lower()
        if not item or item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered


def _empty_result(error: str = "") -> dict:
    """Return an empty contact result."""
    return {
        "emails": [],
        "linkedin_urls": [],
        "instagram_handles": [],
        "twitter_handles": [],
        "websites": [],
        "phone_numbers": [],
        "other_contacts": [],
        "summary": "",
        "error": error,
    }


def _extract_emails(text: str) -> list[str]:
    """Extract and dedupe email addresses from arbitrary text."""
    if not text:
        return []
    emails = [match.lower().strip(".,;:") for match in EMAIL_RE.findall(text)]
    return _dedupe_keep_order(emails)


def extract_contacts(grok_response: str) -> dict:
    """
    Extract email addresses from a text response.

    Args:
        grok_response: Raw text response from any AI/search source

    Returns:
        Dict with emails populated and the other fields left empty.
    """
    if not grok_response or len(grok_response.strip()) < 10:
        print(f"{Fore.YELLOW}  WARN response too short to extract emails{Style.RESET_ALL}")
        return _empty_result()

    emails = _extract_emails(grok_response)

    if emails:
        print(f"{Fore.GREEN}  OK Extracted {len(emails)} email(s){Style.RESET_ALL}")
    else:
        print(f"{Fore.YELLOW}  WARN No email addresses found{Style.RESET_ALL}")

    return {
        "emails": emails,
        "linkedin_urls": [],
        "instagram_handles": [],
        "twitter_handles": [],
        "websites": [],
        "phone_numbers": [],
        "other_contacts": [],
        "summary": "",
        "error": "",
    }


# ── Standalone test ───────────────────────────────────────────────────────
if __name__ == "__main__":
    test_response = """
    Based on my search, I found the following information about TechVault Solutions:

    The company is run by Dave Martinez who is the CEO.
    - Email: dave@techvault.io, contact@techvault.io
    - LinkedIn: https://linkedin.com/in/davemartinez
    - Instagram: @techvault_solutions
    - Website: https://techvault.io
    - Twitter: @TechVaultHQ

    They are a B2B SaaS company based in Austin, TX focused on analytics tools.
    """

    result = extract_contacts(test_response)
    print(f"\n{'='*60}")
    print(json.dumps(result, indent=2))
    print(f"{'='*60}")
