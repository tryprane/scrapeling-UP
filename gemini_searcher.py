"""
Gemini contact search helper.

Uses a fresh Gemini tab for each lookup so contact discovery cannot inherit
stale conversation state from the previous lead.
"""

from __future__ import annotations

import time

from colorama import Fore, Style

GEMINI_URL = "https://gemini.google.com/app"

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
            if el.is_visible(timeout=3000):
                print(f"{Style.DIM}  Using fallback selector for {element_name}: {sel}{Style.RESET_ALL}")
                return el
        except Exception:
            continue
    return None


def _build_gemini_prompt(lead_summary: str) -> str:
    """Build the prompt to send to Gemini for contact discovery."""
    return (
        "Please ignore all previous context. I have a NEW job posting.\n"
        "Use Deep Web Search to find the contact information of the person or company who posted this job:\n\n"
        f"{lead_summary}\n\n"
        "Based on this job posting, find out ALL external contact information of the person "
        "or company who potentially posted this job. I need:\n"
        "1. Email addresses (personal and business)\n"
        "2. LinkedIn profile URLs\n"
        "3. Instagram handles\n"
        "4. Twitter/X handles\n"
        "5. Company website\n"
        "6. Any other public contact information\n\n"
        "Search thoroughly using the company name, person name, website URLs, "
        "and any other identifiers mentioned in the job posting. "
        "Provide all the contact details you can find."
    )


def _open_gemini_page(page):
    """Open Gemini in a dedicated tab when possible."""
    try:
        context = getattr(page, "context", None)
        if context is not None and hasattr(context, "new_page"):
            fresh_page = context.new_page()
            return fresh_page, True
    except Exception:
        pass
    return page, False


def _navigate_to_gemini(page) -> bool:
    """Open Gemini and wait for the SPA to settle."""
    try:
        page.goto(GEMINI_URL, wait_until="domcontentloaded", timeout=30000)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        page.wait_for_timeout(2000)
        return True
    except Exception as exc:
        print(f"{Fore.RED}  Failed to navigate to Gemini: {exc}{Style.RESET_ALL}")
        return False


def _wait_for_response(page, max_wait_response: int) -> str:
    """Read the most recent Gemini model response text."""
    print(f"{Style.DIM}  Waiting for Gemini response (up to {max_wait_response}s)...{Style.RESET_ALL}")
    response_text = ""
    start_time = time.time()

    while time.time() - start_time < max_wait_response:
        page.wait_for_timeout(5000)
        try:
            result = page.evaluate(
                """() => {
                    const els = document.querySelectorAll('div[id^="model-response-message-content"]');
                    if (els.length === 0) return { busy: true, text: '' };
                    const last = els[els.length - 1];
                    return {
                        busy: last.getAttribute('aria-busy'),
                        text: (last.innerText || '').trim(),
                    };
                }"""
            )
            if result and result.get("text"):
                if result.get("busy") == "false":
                    response_text = result["text"]
                    print(f"{Fore.GREEN}  Gemini response received ({len(response_text)} chars){Style.RESET_ALL}")
                    break
                print(f"{Style.DIM}  Still generating... ({len(result['text'])} chars so far){Style.RESET_ALL}")
        except Exception:
            pass

    if response_text:
        return response_text

    try:
        result = page.evaluate(
            """() => {
                const els = document.querySelectorAll('div[id^="model-response-message-content"]');
                if (els.length === 0) return '';
                return (els[els.length - 1].innerText || '').trim();
            }"""
        )
        if result:
            print(f"{Fore.YELLOW}  Grabbed partial response ({len(result)} chars){Style.RESET_ALL}")
            return result
    except Exception:
        pass

    print(f"{Fore.YELLOW}  No response from Gemini{Style.RESET_ALL}")
    try:
        page.screenshot(path="debug-screenshots/gemini_no_response.png")
    except Exception:
        pass
    return ""


def search_contacts_via_gemini(page, lead_summary: str, max_wait_response=220) -> str:
    """
    Use Gemini AI to search for external contact information of a lead.

    A fresh tab is used for every lookup so previous lead responses cannot leak
    into the next job's contact discovery.
    """
    print(f"\n{Fore.CYAN}  Gemini Search: Looking up contacts...{Style.RESET_ALL}")

    gemini_page, own_page = _open_gemini_page(page)
    try:
        if not _navigate_to_gemini(gemini_page):
            return ""

        input_el = None
        max_reloads = 5
        for attempt in range(max_reloads):
            input_el = _find_element(
                gemini_page,
                GEMINI_INPUT_SELECTOR,
                INPUT_FALLBACKS,
                "input area",
                timeout=15000,
            )
            if input_el:
                break

            print(
                f"{Fore.YELLOW}  Input area not found (Attempt {attempt + 1}/{max_reloads}). Reloading Gemini...{Style.RESET_ALL}"
            )
            try:
                gemini_page.reload(wait_until="domcontentloaded", timeout=30000)
                try:
                    gemini_page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass
                gemini_page.wait_for_timeout(3000)
            except Exception as exc:
                print(f"{Style.DIM}  Reload failed: {exc}{Style.RESET_ALL}")

        if not input_el:
            print(f"{Fore.RED}  Could not find Gemini input area{Style.RESET_ALL}")
            try:
                gemini_page.screenshot(path="debug-screenshots/gemini_input_not_found.png")
                with open("debug-screenshots/gemini_input_not_found.html", "w", encoding="utf-8") as handle:
                    handle.write(gemini_page.content())
            except Exception:
                pass
            return ""

        prompt = _build_gemini_prompt(lead_summary)

        try:
            input_el.click()
            gemini_page.wait_for_timeout(500)
            gemini_page.keyboard.press("Control+A")
            gemini_page.keyboard.press("Backspace")
            gemini_page.wait_for_timeout(200)
            gemini_page.keyboard.insert_text(prompt)
            gemini_page.wait_for_timeout(1000)
            print(f"{Fore.GREEN}  Prompt entered{Style.RESET_ALL}")
        except Exception as exc:
            print(f"{Fore.RED}  Failed to type prompt: {exc}{Style.RESET_ALL}")
            return ""

        gemini_page.wait_for_timeout(1000)
        send_btn = _find_element(
            gemini_page,
            GEMINI_SUBMIT_SELECTOR,
            SUBMIT_FALLBACKS,
            "submit button",
            timeout=5000,
        )

        if send_btn:
            try:
                send_btn.click()
                print(f"{Fore.GREEN}  Prompt submitted{Style.RESET_ALL}")
            except Exception:
                pass
        else:
            try:
                gemini_page.keyboard.press("Enter")
                print(f"{Fore.GREEN}  Prompt submitted (Enter key){Style.RESET_ALL}")
            except Exception:
                print(f"{Fore.RED}  Could not submit prompt{Style.RESET_ALL}")
                return ""

        return _wait_for_response(gemini_page, max_wait_response)
    finally:
        if own_page:
            try:
                gemini_page.close()
            except Exception:
                pass


if __name__ == "__main__":
    from scraper import close_browser, start_browser

    print(f"\n{Fore.CYAN}{Style.BRIGHT}=== Gemini Searcher - Standalone Test ==={Style.RESET_ALL}\n")

    page = start_browser(headless=False)
    test_summary = (
        "Job Title: Build a React Dashboard for SaaS Platform\n"
        "Company: TechVault Solutions\n"
        "Person: Dave Martinez\n"
        "Website: techvault.io\n"
        "Description: We need a senior React developer to build an analytics dashboard. "
        "We are a B2B SaaS company based in Austin, TX."
    )

    result = search_contacts_via_gemini(page, test_summary)
    print(f"\n{'=' * 60}")
    print(f"GEMINI RESPONSE:\n{result}")
    print(f"{'=' * 60}")
    close_browser()
