"""
Website Scraper — Visits company websites found in the Gemini response and extracts real email addresses.

Flow:
1. Parse website URLs from Gemini's raw text response (or a contacts dict)
2. For each URL, use Scrapling (Patchright-backed) to load the page
3. Extract email addresses from the rendered HTML using regex
4. Check obvious sub-pages: /contact, /about, /team
5. Return a deduplicated list of emails found on the actual website

Can be tested standalone:
    python website_scraper.py
"""

import re
import time
from urllib.parse import urljoin, urlparse
from colorama import Fore, Style

# ── Regex for email extraction ────────────────────────────────────────

EMAIL_REGEX = re.compile(
    r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,10}\b'
)

# Domains that are clearly not real company emails — skip them
JUNK_EMAIL_DOMAINS = {
    'example.com', 'test.com', 'sentry.io', 'wixpress.com',
    'squarespace.com', 'shopify.com', 'wordpress.com',
    'placeholder.com', 'domain.com', 'yourdomain.com',
    'email.com', 'user.com', 'company.com',
}

# Paths to try on a site after the homepage
CONTACT_PATHS = ['/contact', '/contact-us', '/about', '/about-us', '/team', '/reach-us']

# Timeout for page.goto (ms)
NAV_TIMEOUT = 20_000


def _normalise_url(url: str) -> str:
    """Ensure URL has a scheme."""
    url = url.strip().rstrip('/')
    if url and not url.startswith('http'):
        url = 'https://' + url
    return url


def _extract_emails_from_text(text: str) -> list[str]:
    """Return clean, deduplicated emails from raw text, filtering obvious junk."""
    found = EMAIL_REGEX.findall(text)
    cleaned = []
    seen = set()
    for email in found:
        email = email.lower().strip('.,;')
        domain = email.split('@')[-1]
        if domain in JUNK_EMAIL_DOMAINS:
            continue
        # Skip image/file references that look like emails
        if any(email.endswith(ext) for ext in ['.png', '.jpg', '.gif', '.svg', '.css', '.js']):
            continue
        if email not in seen:
            seen.add(email)
            cleaned.append(email)
    return cleaned


def extract_emails_from_text(text: str) -> list[str]:
    """Public wrapper for email extraction from arbitrary text."""
    return _extract_emails_from_text(text)


def _get_page_text(page, url: str) -> str:
    """Navigate to URL and return the rendered inner text, or '' on failure."""
    try:
        page.goto(url, wait_until='domcontentloaded', timeout=NAV_TIMEOUT)
        # Give JS a moment to render dynamic content
        try:
            page.wait_for_load_state('networkidle', timeout=6000)
        except Exception:
            pass
        page.wait_for_timeout(1500)
        return page.inner_text('body') or ''
    except Exception as e:
        print(f"{Style.DIM}    ⌊ Could not load {url}: {e}{Style.RESET_ALL}")
        return ''


def scrape_emails_from_websites(page, website_urls: list[str]) -> list[str]:
    """
    Visit each website URL using Patchright and extract real email addresses.

    Checks the homepage + common contact/about sub-pages.

    Args:
        page: Patchright page object
        website_urls: List of website URLs to visit

    Returns:
        Deduplicated list of email addresses found on the websites.
    """
    all_emails: list[str] = []
    seen: set[str] = set()

    for raw_url in website_urls:
        url = _normalise_url(raw_url)
        if not url:
            continue

        domain = urlparse(url).netloc
        print(f"\n{Fore.CYAN}  🌐 Scraping website: {domain}{Style.RESET_ALL}")

        pages_to_check = [url] + [urljoin(url, path) for path in CONTACT_PATHS]

        for page_url in pages_to_check:
            text = _get_page_text(page, page_url)
            if not text:
                continue

            emails = _extract_emails_from_text(text)
            new_emails = [e for e in emails if e not in seen]

            if new_emails:
                print(f"{Fore.GREEN}    ✓ Found {len(new_emails)} email(s) on {page_url.split(domain)[-1] or '/'}: "
                      f"{', '.join(new_emails)}{Style.RESET_ALL}")
                all_emails.extend(new_emails)
                seen.update(new_emails)
            else:
                print(f"{Style.DIM}    ⌊ No emails on {page_url.split(domain)[-1] or '/'}{Style.RESET_ALL}")

            # Small delay between sub-page requests
            time.sleep(0.8)

        if not any(e for e in all_emails):
            print(f"{Fore.YELLOW}    ⚠ No emails found on {domain}{Style.RESET_ALL}")

    if all_emails:
        print(f"\n{Fore.GREEN}  ✓ Website scraping complete — {len(all_emails)} unique email(s) found: "
              f"{', '.join(all_emails)}{Style.RESET_ALL}")
    else:
        print(f"\n{Fore.YELLOW}  ⚠ Website scraping found no emails{Style.RESET_ALL}")

    return all_emails


def extract_website_urls_from_text(text: str) -> list[str]:
    """
    Pull website URLs from raw Gemini response text.

    Looks for patterns like:
    - Website: https://example.com
    - https://example.com
    - www.example.com
    """
    # Match full URLs
    url_pattern = re.compile(
        r'https?://(?:www\.)?[A-Za-z0-9\-]+\.[A-Za-z]{2,}(?:/[^\s,\)"\']*)?'
    )
    www_pattern = re.compile(
        r'\bwww\.[A-Za-z0-9\-]+\.[A-Za-z]{2,}(?:/[^\s,\)"\']*)?'
    )

    urls = set()

    for match in url_pattern.finditer(text):
        u = match.group(0).rstrip('.,;)')
        # Skip social media / known non-company URLs
        if not _is_company_website(u):
            continue
        urls.add(u)

    for match in www_pattern.finditer(text):
        u = 'https://' + match.group(0).rstrip('.,;)')
        if not _is_company_website(u):
            continue
        urls.add(u)

    return list(urls)


def _is_company_website(url: str) -> bool:
    """Filter out social/tracking URLs — we only want real company sites."""
    skip_domains = {
        'linkedin.com', 'twitter.com', 'x.com', 'instagram.com', 'facebook.com',
        'youtube.com', 'github.com', 'google.com', 'bing.com', 'gemini.google.com',
        'upwork.com', 'freelancer.com', 'fiverr.com', 'indeed.com',
        'glassdoor.com', 'crunchbase.com', 'bloomberg.com', 'techcrunch.com',
        'wikipedia.org', 'reddit.com', 'medium.com', 'quora.com',
        'w3.org', 'duckduckgo.com', 'search.google.com', 'maps.google.com',
    }
    try:
        netloc = urlparse(url).netloc.lower()
        if netloc.startswith('www.'):
            netloc = netloc[4:]
        return not any(netloc == d or netloc.endswith('.' + d) for d in skip_domains)
    except Exception:
        return False


def is_company_website(url: str) -> bool:
    """Public wrapper for company-site filtering."""
    return _is_company_website(url)


# ── Standalone test ──────────────────────────────────────────────────

if __name__ == '__main__':
    import sys
    from colorama import init as colorama_init
    colorama_init()

    print(f"\n{Fore.CYAN}{Style.BRIGHT}=== Website Scraper — Standalone Test ==={Style.RESET_ALL}\n")

    # ── Test 1: URL extraction from dummy Gemini text ──────────────────
    print(f"{Style.BRIGHT}[Test 1] URL extraction from Gemini response text{Style.RESET_ALL}")
    dummy_gemini_response = """
    I found the following information about TechVault Solutions:
    - Website: https://techvault.io
    - LinkedIn: https://linkedin.com/company/techvault
    - Twitter: https://twitter.com/techvaulthq
    - Contact page: https://techvault.io/contact
    - Dave Martinez personal site: https://davemartinez.dev
    Email mentioned: dave@techvault.io, contact@techvault.io
    """

    urls = extract_website_urls_from_text(dummy_gemini_response)
    print(f"  Extracted URLs: {urls}")
    print(f"  Expected: techvault.io and davemartinez.dev (not linkedin/twitter)")
    print()

    # ── Test 2: Email extraction from dummy HTML content ──────────────
    print(f"{Style.BRIGHT}[Test 2] Email extraction from dummy page text{Style.RESET_ALL}")
    dummy_page_text = """
    Contact Us
    For business inquiries: hello@femur.studio
    Support: support@femur.studio
    Booking: book@femur.studio
    Ignore: noreply@example.com, test@test.com, icon@2x.png
    Also from Wix: builder@wixpress.com
    """
    emails = _extract_emails_from_text(dummy_page_text)
    print(f"  Extracted emails: {emails}")
    print(f"  Expected: hello@femur.studio, support@femur.studio, book@femur.studio")
    print()

    # ── Test 3: Live website scraping ────────────────────────────────
    print(f"{Style.BRIGHT}[Test 3] Live website scraping — femur.studio{Style.RESET_ALL}")
    try:
        sys.path.insert(0, '.')
        from scraper import start_browser, close_browser
        page = start_browser(headless=False)
        found_emails = scrape_emails_from_websites(page, ['https://femur.studio'])
        print(f"\n  Live emails found: {found_emails}")
        close_browser()
    except ImportError:
        print(f"  {Fore.YELLOW}⚠ scraper.py not found — skipping live test{Style.RESET_ALL}")
    except Exception as e:
        print(f"  {Fore.RED}✗ Live test error: {e}{Style.RESET_ALL}")

    print(f"\n{Fore.GREEN}{Style.BRIGHT}=== Tests Complete ==={Style.RESET_ALL}")
