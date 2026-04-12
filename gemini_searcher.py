"""
Gemini Contact Searcher — Uses Google's Gemini AI web UI to find external contacts.

Navigates to https://gemini.google.com/app via the existing Patchright browser,
types a crafted prompt about the lead, and extracts the response.

Replaces grok_searcher.py to avoid Grok's rate limits.

Can be tested standalone:
    python gemini_searcher.py
"""

import time
import random
from colorama import Fore, Style

GEMINI_URL = "https://gemini.google.com/app"

# ── CSS selectors ────────────────────────────────────────────────────

GEMINI_INPUT_SELECTOR = 'div.ql-editor[contenteditable="true"], div[contenteditable="true"][role="textbox"]'
GEMINI_SUBMIT_SELECTOR = 'button.send-button'

INPUT_FALLBACKS = [
    'div[contenteditable="true"][aria-label*="prompt"]',
    'div[contenteditable="true"][aria-label*="Gemini"]',
    'div[contenteditable="true"]',
]

SUBMIT_FALLBACKS = [
    'button.send-button',
    'button[aria-label="Send message"]',
    'button[aria-label*="Send"]',
    'mat-icon[data-mat-icon-name="send"]',
    'mat-icon.send-button-icon',
]

# Model response only (never matches user prompt)
MODEL_RESPONSE_SELECTOR = 'div[id^="model-response-message-content"]'


def _find_element(page, primary_selector, fallbacks, element_name, timeout=5000):
    """Try primary selector first, then fallbacks."""
    try:
        el = page.locator(primary_selector).first
        if el.is_visible(timeout=timeout):
            return el
    except Exception:
        pass

    for sel in fallbacks:
        try:
            el = page.locator(sel).first
            # Increase the fallback timeout slightly to be more robust
            if el.is_visible(timeout=3000):
                print(f"{Style.DIM}  ⌊ Using fallback selector for {element_name}: {sel}{Style.RESET_ALL}")
                return el
        except Exception:
            continue
    return None


def _build_gemini_prompt(lead_summary: str) -> str:
    """Build the prompt to send to Gemini for contact discovery."""
    return (
        f"Please ignore all previous context. I have a NEW job posting.\n"
        f"Use Deep Web Search to find the contact information of the person or company who posted this job:\n\n"
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


def search_contacts_via_gemini(page, lead_summary: str, max_wait_response=220) -> str:
    """
    Use Gemini AI to search for external contact information of a lead.

    Args:
        page: Patchright page object (from scraper.start_browser)
        lead_summary: Summary/description of the lead
        max_wait_response: Max seconds to wait for Gemini's response

    Returns:
        Raw response text from Gemini, or empty string on failure.
    """
    print(f"\n{Fore.CYAN}  🔍 Gemini Search: Looking up contacts...{Style.RESET_ALL}")

    # 1. Check if we are already on Gemini and the input is visible
    input_el = None
    if "gemini.google.com" in page.url:
        print(f"{Style.DIM}  ⌊ Already on Gemini. Checking for input area without reloading...{Style.RESET_ALL}")
        input_el = _find_element(page, GEMINI_INPUT_SELECTOR, INPUT_FALLBACKS, 'input area', timeout=3000)

    # 2. If not found or not on Gemini, navigate to Gemini
    if not input_el:
        try:
            page.goto(GEMINI_URL, wait_until='domcontentloaded', timeout=30000)
            # Wait for network idle to give the heavy JS SPA time to build the UI
            try:
                page.wait_for_load_state('networkidle', timeout=10000)
            except Exception:
                pass  # Network idle might timeout due to polling, but gives it more time
            page.wait_for_timeout(2000)
        except Exception as e:
            print(f"{Fore.RED}  ✗ Failed to navigate to Gemini: {e}{Style.RESET_ALL}")
            return ''

        # Find and click the input area. Retry if not found.
        max_reloads = 5
        for attempt in range(max_reloads):
            input_el = _find_element(page, GEMINI_INPUT_SELECTOR, INPUT_FALLBACKS, 'input area', timeout=15000)
            if input_el:
                break
                
            print(f"{Fore.YELLOW}  ⚠ Input area not found (Attempt {attempt+1}/{max_reloads}). Reloading Gemini...{Style.RESET_ALL}")
            try:
                page.reload(wait_until='domcontentloaded', timeout=30000)
                try:
                    page.wait_for_load_state('networkidle', timeout=5000)
                except Exception:
                    pass
                page.wait_for_timeout(3000)
            except Exception as e:
                print(f"{Style.DIM}  ⌊ Reload failed: {e}{Style.RESET_ALL}")

    if not input_el:
        print(f"{Fore.RED}  ✗ Could not find Gemini input area{Style.RESET_ALL}")
        try:
            page.screenshot(path='debug-screenshots/gemini_input_not_found.png')
            # Save HTML for debugging
            with open('debug-screenshots/gemini_input_not_found.html', 'w', encoding='utf-8') as f:
                f.write(page.content())
        except Exception:
            pass
        return ''

    prompt = _build_gemini_prompt(lead_summary)

    # 3. Type the prompt
    try:
        input_el.click()
        page.wait_for_timeout(500)
        page.keyboard.press('Control+A')
        page.keyboard.press('Backspace')
        page.wait_for_timeout(200)
        page.keyboard.insert_text(prompt)
        page.wait_for_timeout(1000)
        print(f"{Fore.GREEN}  ✓ Prompt entered{Style.RESET_ALL}")
    except Exception as e:
        print(f"{Fore.RED}  ✗ Failed to type prompt: {e}{Style.RESET_ALL}")
        return ''

    # 4. Click the send button
    page.wait_for_timeout(1000)
    send_btn = _find_element(page, GEMINI_SUBMIT_SELECTOR, SUBMIT_FALLBACKS, 'submit button', timeout=5000)
    
    if send_btn:
        try:
            send_btn.click()
            print(f"{Fore.GREEN}  ✓ Prompt submitted{Style.RESET_ALL}")
        except Exception:
            pass
    else:
        try:
            page.keyboard.press('Enter')
            print(f"{Fore.GREEN}  ✓ Prompt submitted (Enter key){Style.RESET_ALL}")
        except Exception:
            print(f"{Fore.RED}  ✗ Could not submit prompt{Style.RESET_ALL}")
            return ''

    # 5. Wait for Gemini's response
    # Poll using evaluate() on the DOM directly — avoids Patchright locator timeouts
    print(f"{Style.DIM}  ⏳ Waiting for Gemini response (up to {max_wait_response}s)...{Style.RESET_ALL}")

    response_text = ''
    start_time = time.time()

    while time.time() - start_time < max_wait_response:
        page.wait_for_timeout(5000)

        # Use page.evaluate() to read the DOM directly — fast and no timeout issues
        try:
            result = page.evaluate('''() => {
                // Find all model response divs (never matches user prompt)
                const els = document.querySelectorAll('div[id^="model-response-message-content"]');
                if (els.length === 0) return {busy: true, text: ''};

                // Get the last one (most recent response)
                const last = els[els.length - 1];
                const busy = last.getAttribute('aria-busy');
                const text = last.innerText || '';

                return {busy: busy, text: text.trim()};
            }''')

            if result and result.get('text'):
                if result.get('busy') == 'false':
                    response_text = result['text']
                    print(f"{Fore.GREEN}  ✓ Gemini response received ({len(response_text)} chars){Style.RESET_ALL}")
                    break
                else:
                    print(f"{Style.DIM}  ⌊ Still generating... ({len(result['text'])} chars so far){Style.RESET_ALL}")
        except Exception:
            pass

    if not response_text:
        # Last attempt — grab whatever text is there even if still busy
        try:
            result = page.evaluate('''() => {
                const els = document.querySelectorAll('div[id^="model-response-message-content"]');
                if (els.length === 0) return '';
                return (els[els.length - 1].innerText || '').trim();
            }''')
            if result:
                response_text = result
                print(f"{Fore.YELLOW}  ⚠ Grabbed partial response ({len(response_text)} chars){Style.RESET_ALL}")
        except Exception:
            pass

    if not response_text:
        print(f"{Fore.YELLOW}  ⚠ No response from Gemini{Style.RESET_ALL}")
        try:
            page.screenshot(path='debug-screenshots/gemini_no_response.png')
        except Exception:
            pass

    return response_text


# ── Standalone test ──────────────────────────────────────────────────
if __name__ == '__main__':
    from scraper import start_browser, close_browser

    print(f"\n{Fore.CYAN}{Style.BRIGHT}=== Gemini Searcher - Standalone Test ==={Style.RESET_ALL}\n")

    page = start_browser(headless=False)

    # Navigate to Gemini first so user can log in if needed
    print(f"{Fore.CYAN}  🌐 Opening Gemini...{Style.RESET_ALL}")
    page.goto(GEMINI_URL, wait_until='domcontentloaded', timeout=30000)
    page.wait_for_timeout(3000)

    # Check if login is needed
    try:
        sign_in = page.locator('a[aria-label="Sign in"]')
        if sign_in.count() > 0 and sign_in.first.is_visible(timeout=2000):
            print(f"{Fore.YELLOW}  🔐 Please log in to Google in the browser window...{Style.RESET_ALL}")
            print(f"{Fore.YELLOW}  Waiting up to 5 minutes...{Style.RESET_ALL}")
            start = time.time()
            while time.time() - start < 300:
                try:
                    if sign_in.count() == 0 or not sign_in.first.is_visible(timeout=1000):
                        print(f"{Fore.GREEN}  ✓ Logged in!{Style.RESET_ALL}")
                        break
                except Exception:
                    break
                page.wait_for_timeout(3000)
    except Exception:
        pass

    # Run test query
    test_summary = (
        "Job Title: Build a React Dashboard for SaaS Platform\n"
        "Company: TechVault Solutions\n"
        "Person: Dave Martinez\n"
        "Website: techvault.io\n"
        "Description: We need a senior React developer to build an analytics dashboard. "
        "We are a B2B SaaS company based in Austin, TX."
    )

    result = search_contacts_via_gemini(page, test_summary)
    print(f"\n{'='*60}")
    print(f"GEMINI RESPONSE:\n{result}")
    print(f"{'='*60}")

    close_browser()
