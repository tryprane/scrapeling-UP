"""
Grok Contact Searcher — Uses X/Twitter's Grok AI to find external contacts.

Navigates to https://x.com/i/grok via the existing Patchright browser,
types a crafted prompt about the lead, and extracts the response.

Can be tested standalone:
    python -c "from grok_searcher import search_contacts_via_grok; from scraper import start_browser; page = start_browser(headless=False); print(search_contacts_via_grok(page, 'Test lead summary'))"
"""

import time
import random
from colorama import Fore, Style

GROK_URL = "https://x.com/i/grok"
X_LOGIN_URL = "https://x.com/i/flow/login"

# CSS selectors for Grok UI elements
GROK_TEXTAREA_SELECTOR = (
    '#react-root > div > div > div.css-175oi2r.r-1f2l425.r-13qz1uu.r-417010.r-18u37iz '
    '> main > div > div > div > div > div > div.r-6koalj.r-eqz5dr.r-1pi2tsx.r-13qz1uu '
    '> div > div > div.css-175oi2r.r-1pi2tsx.r-11yh6sk.r-1rnoaur.r-bnwqim.r-13qz1uu '
    '> div > div > div.css-175oi2r.r-1awozwy.r-13qz1uu.r-1sg8ghl > div > div '
    '> div.css-175oi2r.r-eqz5dr.r-16y2uox.r-1wbh5a2.r-kemksi.r-1kqtdi0.r-nsiyw1'
    '.r-1phboty.r-rs99b7.r-13awgt0.r-ubezar.r-1wtj0ep.r-xyw6el.r-11f147o.r-1j8xsk'
    '.r-13qz1uu.r-143luy5 > div > div > div.css-175oi2r.r-1dqbpge.r-13awgt0.r-18u37iz '
    '> div.css-175oi2r.r-1wbh5a2.r-16y2uox > div > textarea'
)

GROK_SUBMIT_SELECTOR = (
    '#react-root > div > div > div.css-175oi2r.r-1f2l425.r-13qz1uu.r-417010.r-18u37iz '
    '> main > div > div > div > div > div > div.r-6koalj.r-eqz5dr.r-1pi2tsx.r-13qz1uu '
    '> div > div > div.css-175oi2r.r-1pi2tsx.r-11yh6sk.r-1rnoaur.r-bnwqim.r-13qz1uu '
    '> div > div > div.css-175oi2r.r-1awozwy.r-13qz1uu.r-1sg8ghl > div > div '
    '> div.css-175oi2r.r-eqz5dr.r-16y2uox.r-1wbh5a2.r-kemksi.r-1kqtdi0.r-nsiyw1'
    '.r-1phboty.r-rs99b7.r-13awgt0.r-ubezar.r-1wtj0ep.r-xyw6el.r-11f147o.r-1j8xsk'
    '.r-13qz1uu.r-143luy5 > div > div > div.css-175oi2r.r-1awozwy.r-18u37iz.r-1cmwbt1'
    '.r-17s6mgv > div > button'
)

GROK_RESPONSE_SELECTOR = (
    '#react-root > div > div > div.css-175oi2r.r-1f2l425.r-13qz1uu.r-417010.r-18u37iz '
    '> main > div > div > div > div > div > div.r-6koalj.r-eqz5dr.r-1pi2tsx.r-13qz1uu '
    '> div > div > div.css-175oi2r.r-16y2uox > div > div.css-175oi2r.r-13qz1uu '
    '> div > div > div.css-175oi2r.r-1wbh5a2.r-11niif6.r-bnwqim.r-13qz1uu '
    '> div.css-175oi2r.r-3pj75a > div > div > span > span > span > span'
)

# Fallback selectors in case the exact ones break
TEXTAREA_FALLBACKS = [
    'textarea[placeholder*="Ask"]',
    'textarea[placeholder*="ask"]',
    'textarea',
    'div[contenteditable="true"]',
]

SUBMIT_FALLBACKS = [
    'button[aria-label="Submit"]',
    'button[aria-label="send"]',
    'button[data-testid="send"]',
    'main button[type="button"]:last-of-type',
]

RESPONSE_FALLBACKS = [
    'div[data-testid="message-content"]',
    'main div.r-3pj75a span',
    'main div[dir="ltr"] span span',
    'main article span',
]


def _check_x_login(page):
    """Check if X/Twitter requires login."""
    try:
        current_url = page.url
        if '/login' in current_url or '/flow/login' in current_url:
            return True
        # Check for login prompts
        login_btn = page.locator('a[href="/login"], a[data-testid="loginButton"]')
        if login_btn.count() > 0 and login_btn.first.is_visible(timeout=2000):
            return True
    except Exception:
        pass
    return False


def _wait_for_login(page, timeout_seconds=300):
    """
    Wait for the user to manually log into X/Twitter.
    Since the browser runs in headed mode, the user can interact with it directly.
    """
    print(f"{Fore.YELLOW}  🔐 X/Twitter login required!{Style.RESET_ALL}")
    print(f"{Fore.YELLOW}  Please log into X in the browser window.{Style.RESET_ALL}")
    print(f"{Fore.YELLOW}  Waiting up to {timeout_seconds // 60} minutes...{Style.RESET_ALL}")

    page.goto(X_LOGIN_URL, wait_until='domcontentloaded', timeout=30000)
    page.wait_for_timeout(3000)

    start = time.time()
    while time.time() - start < timeout_seconds:
        current_url = page.url
        # If we're no longer on login page, login succeeded
        if '/login' not in current_url and '/flow/login' not in current_url:
            print(f"{Fore.GREEN}  ✓ X login successful!{Style.RESET_ALL}")
            return True
        page.wait_for_timeout(3000)

    print(f"{Fore.RED}  ✗ X login timed out after {timeout_seconds}s{Style.RESET_ALL}")
    return False


def _find_element(page, primary_selector, fallbacks, element_name, timeout=5000):
    """Try primary selector first, then fallbacks."""
    # Try primary
    try:
        el = page.locator(primary_selector).first
        if el.is_visible(timeout=timeout):
            return el
    except Exception:
        pass

    # Try fallbacks
    for sel in fallbacks:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=2000):
                print(f"{Style.DIM}  ⌊ Using fallback selector for {element_name}: {sel}{Style.RESET_ALL}")
                return el
        except Exception:
            continue

    return None


def _build_grok_prompt(lead_summary: str) -> str:
    """Build the prompt to send to Grok for contact discovery."""
    return (
        f"This is a job posting summary from Upwork:\n\n"
        f"{lead_summary}\n\n"
        f"Based on this job posting, find out ALL external contact information of the person "
        f"or company who potentially posted this job. I need:\n"
        f"1. Email addresses (personal and business)\n"
        f"2. LinkedIn profile URLs\n"
        f"3. Instagram handles\n"
        f"4. Twitter/X handles\n"
        f"5. Company website\n"
        f"6. Any other public contact information\n\n"
        f"Search thoroughly using the company name, person name, website URLs, "
        f"and any other identifiers mentioned in the job posting. "
        f"Provide all the contact details you can find."
    )


def search_contacts_via_grok(page, lead_summary: str, max_wait_response=120) -> str:
    """
    Use Grok AI to search for external contact information of a lead.

    Args:
        page: Patchright page object (from scraper.start_browser)
        lead_summary: Summary/description of the lead including company, person, etc.
        max_wait_response: Max seconds to wait for Grok's response

    Returns:
        Raw response text from Grok, or empty string on failure.
    """
    print(f"\n{Fore.CYAN}  🔍 Grok Search: Looking up contacts...{Style.RESET_ALL}")

    # 1. Navigate to Grok
    try:
        page.goto(GROK_URL, wait_until='domcontentloaded', timeout=30000)
        page.wait_for_timeout(4000)
    except Exception as e:
        print(f"{Fore.RED}  ✗ Failed to navigate to Grok: {e}{Style.RESET_ALL}")
        return ''

    # 2. Check if login is needed
    if _check_x_login(page):
        if not _wait_for_login(page):
            return ''
        # After login, navigate back to Grok
        try:
            page.goto(GROK_URL, wait_until='domcontentloaded', timeout=30000)
            page.wait_for_timeout(4000)
        except Exception as e:
            print(f"{Fore.RED}  ✗ Failed to navigate to Grok after login: {e}{Style.RESET_ALL}")
            return ''

    # 3. Find and fill the textarea
    textarea = _find_element(page, GROK_TEXTAREA_SELECTOR, TEXTAREA_FALLBACKS, 'textarea')
    if not textarea:
        print(f"{Fore.RED}  ✗ Could not find Grok textarea{Style.RESET_ALL}")
        try:
            page.screenshot(path='debug-screenshots/grok_textarea_not_found.png')
        except Exception:
            pass
        return ''

    prompt = _build_grok_prompt(lead_summary)

    # Type the prompt with human-like delays
    try:
        textarea.click()
        page.wait_for_timeout(random.randint(300, 600))

        # Use fill for textarea (faster than typing char by char)
        textarea.fill(prompt)
        page.wait_for_timeout(random.randint(500, 1000))

        print(f"{Fore.GREEN}  ✓ Prompt entered into Grok{Style.RESET_ALL}")
    except Exception as e:
        print(f"{Fore.RED}  ✗ Failed to type prompt: {e}{Style.RESET_ALL}")
        return ''

    # 4. Click the submit button
    submit_btn = _find_element(page, GROK_SUBMIT_SELECTOR, SUBMIT_FALLBACKS, 'submit button')
    if not submit_btn:
        # Try pressing Enter as fallback
        print(f"{Style.DIM}  ⌊ Submit button not found, trying Enter key...{Style.RESET_ALL}")
        try:
            textarea.press('Enter')
        except Exception:
            print(f"{Fore.RED}  ✗ Could not submit the prompt{Style.RESET_ALL}")
            return ''
    else:
        try:
            submit_btn.click()
            print(f"{Fore.GREEN}  ✓ Prompt submitted{Style.RESET_ALL}")
        except Exception as e:
            print(f"{Fore.RED}  ✗ Failed to click submit: {e}{Style.RESET_ALL}")
            return ''

    # 5. Wait for Grok's response
    print(f"{Style.DIM}  ⏳ Waiting for Grok response (up to {max_wait_response}s)...{Style.RESET_ALL}")
    page.wait_for_timeout(5000)  # Initial wait for response to start

    response_text = ''
    last_text_length = 0
    stable_count = 0
    start_time = time.time()

    while time.time() - start_time < max_wait_response:
        # Try to get the response text
        current_text = ''

        # Try primary selector
        try:
            resp_elements = page.locator(GROK_RESPONSE_SELECTOR)
            if resp_elements.count() > 0:
                texts = []
                for i in range(resp_elements.count()):
                    t = resp_elements.nth(i).inner_text().strip()
                    if t:
                        texts.append(t)
                current_text = '\n'.join(texts)
        except Exception:
            pass

        # Try fallback selectors if primary failed
        if not current_text:
            for sel in RESPONSE_FALLBACKS:
                try:
                    els = page.locator(sel)
                    if els.count() > 0:
                        texts = []
                        for i in range(min(els.count(), 50)):
                            t = els.nth(i).inner_text().strip()
                            if t:
                                texts.append(t)
                        candidate = '\n'.join(texts)
                        if len(candidate) > len(current_text):
                            current_text = candidate
                except Exception:
                    continue

        if current_text:
            response_text = current_text

            # Check if response has stabilized (AI finished generating)
            if len(response_text) == last_text_length and len(response_text) > 50:
                stable_count += 1
                if stable_count >= 3:
                    print(f"{Fore.GREEN}  ✓ Grok response received ({len(response_text)} chars){Style.RESET_ALL}")
                    break
            else:
                stable_count = 0
                last_text_length = len(response_text)

        page.wait_for_timeout(3000)

    if not response_text:
        print(f"{Fore.YELLOW}  ⚠ No response from Grok after {max_wait_response}s{Style.RESET_ALL}")
        try:
            page.screenshot(path='debug-screenshots/grok_no_response.png')
        except Exception:
            pass

    return response_text


# ── Standalone test ──────────────────────────────────────────────────
if __name__ == '__main__':
    from scraper import start_browser

    test_summary = (
        "Job Title: Build a React Dashboard for SaaS Platform\n"
        "Company: TechVault Solutions\n"
        "Person: Dave Martinez\n"
        "Website: techvault.io\n"
        "Description: We need a senior React developer to build an analytics dashboard. "
        "We are a B2B SaaS company based in Austin, TX."
    )

    page = start_browser(headless=False)
    result = search_contacts_via_grok(page, test_summary)
    print(f"\n{'='*60}")
    print(f"GROK RESPONSE:\n{result}")
    print(f"{'='*60}")
