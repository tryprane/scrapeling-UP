"""
Browser-driven verifier for MailTester Ninja.

This drives the public verifier page in a Patchright browser tab and reads the
client-side results from the page state. It does not require an API key, but it
will only work if the website allows the current browser session to verify.
"""

from __future__ import annotations

import os
import json
import time
import tempfile
from pathlib import Path

from colorama import Fore, Style
from patchright.sync_api import sync_playwright


MAILTESTER_PROFILE_DIR = os.path.join(Path.home(), '.cliup-mailtester-profile-py')


def _chunked(items: list[str], size: int) -> list[list[str]]:
    if size <= 0:
        size = 1
    return [items[i:i + size] for i in range(0, len(items), size)]


def _map_mailtester_result(item: dict) -> dict:
    email = (item.get('email') or '').strip().lower()
    code = (item.get('code') or '').strip().lower()
    message = (item.get('message') or '').strip()
    mx = item.get('mx') or []
    if isinstance(mx, str):
        mx = [mx]
    elif not isinstance(mx, list):
        mx = []

    status = 'unknown'
    reason = f'MailTester: {message or code or "unverifiable"}'

    if code == 'ok' or message.lower() == 'accepted':
        status = 'valid'
        reason = 'MailTester: Accepted'
    elif code == 'ko' or message.lower() in {'no mx', 'mx error', 'rejected'}:
        status = 'invalid'
    elif code == 'mb' or message.lower() in {'limited', 'catch-all', 'timeout', 'spam block', 'greylisted'}:
        status = 'unknown'

    return {
        'email': email,
        'status': status,
        'reason': reason,
        'mx': mx,
    }


def _open_verifier_page(page):
    context = getattr(page, 'context', None)
    if context is not None and hasattr(context, 'new_page'):
        return context.new_page(), True
    return page, False


def _launch_dedicated_verifier_page(headless: bool):
    """Launch a dedicated browser page for MailTester verification."""
    playwright = sync_playwright().start()
    launch_dirs = [MAILTESTER_PROFILE_DIR, tempfile.mkdtemp(prefix="cliup-mailtester-profile-")]
    last_error = None

    for user_data_dir in launch_dirs:
        try:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                headless=headless,
                channel='chrome',
                viewport={'width': 1366, 'height': 768},
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--no-sandbox',
                ],
                ignore_default_args=['--enable-automation'],
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            """)
            return page, playwright, context
        except Exception as exc:
            last_error = exc

    playwright.stop()
    raise last_error


def _wait_for_results(page, expected_count: int, timeout_ms: int) -> str:
    deadline = time.time() + (timeout_ms / 1000.0)
    last_info = ''

    while time.time() < deadline:
        try:
            info = page.evaluate("""
                (() => {
                    const el = document.getElementById('info');
                    return el ? (el.textContent || el.innerText || '') : '';
                })()
            """)
            last_info = (info or '').strip()
        except Exception:
            last_info = ''

        try:
            results_len = page.evaluate("document.querySelectorAll('#result .ninja_row').length")
        except Exception:
            results_len = 0

        try:
            messages = page.evaluate("""
                Array.from(document.querySelectorAll('#result .ninja_msg'))
                    .map(el => (el.textContent || '').trim())
            """)
            if not isinstance(messages, list):
                messages = []
        except Exception:
            messages = []

        if results_len >= expected_count and messages:
            if all(msg and msg != 'Click & Unlock' for msg in messages[:expected_count]):
                return last_info
            if any(msg == 'Click & Unlock' for msg in messages[:expected_count]):
                return 'PAYWALL'

        if last_info and any(
            token in last_info.lower()
            for token in ['subscribe', 'please provide', 'unauthorized', 'disabled']
        ):
            return last_info

        time.sleep(0.75)

    return last_info


def _message_to_status(message: str) -> tuple[str, str]:
    message = (message or '').strip()
    lowered = message.lower()
    if not message:
        return 'unknown', 'MailTester did not return a result message'
    if message == 'Click & Unlock':
        return 'unknown', 'MailTester page is showing the subscription paywall'
    if lowered == 'accepted':
        return 'valid', 'MailTester: Accepted'
    if lowered in {'limited', 'catch-all', 'timeout', 'spam block', 'greylisted'}:
        return 'unknown', f'MailTester: {message}'
    if lowered in {'no mx', 'mx error', 'rejected'}:
        return 'invalid', f'MailTester: {message}'
    if lowered in {'ok', 'ko', 'mb'}:
        return 'unknown', f'MailTester: {message}'
    return 'unknown', f'MailTester: {message}'


def _verify_batch(page, emails: list[str], verifier_url: str, page_timeout_ms: int, wait_seconds: int) -> dict:
    try:
        page.goto(verifier_url, wait_until='domcontentloaded', timeout=page_timeout_ms)
        try:
            page.wait_for_load_state('networkidle', timeout=min(page_timeout_ms, 15000))
        except Exception:
            pass

        textarea = None
        for selector in ['textarea#emails', '#emails', 'textarea']:
            try:
                loc = page.locator(selector).first
                if loc.count() or loc.is_visible(timeout=2000):
                    textarea = loc
                    break
            except Exception:
                continue

        if textarea is None:
            raise RuntimeError('MailTester page did not expose the email textarea')

        textarea.fill("\n".join(emails))

        clicked = False
        for selector in [
            'input#btn1',
            'input[type="submit"][value*="Ninja"]',
            'button:has-text("Ninja Verify")',
            'button:has-text("Verify")',
        ]:
            try:
                btn = page.locator(selector).first
                if btn.count() or btn.is_visible(timeout=1500):
                    btn.click()
                    clicked = True
                    break
            except Exception:
                continue

        if not clicked:
            try:
                textarea.press('Enter')
                clicked = True
            except Exception:
                pass

        last_info = _wait_for_results(page, len(emails), wait_seconds * 1000)

        if last_info == 'PAYWALL':
            return {
                email.lower(): {
                    'email': email.lower(),
                    'status': 'unknown',
                    'reason': 'MailTester page is showing the subscription paywall; a valid token is required for full results',
                    'mx': [],
                }
                for email in emails
            }
    except Exception as exc:
        fallback_reason = f'MailTester browser error: {exc}'
        return {
            email: {
                'email': email.lower(),
                'status': 'unknown',
                'reason': fallback_reason,
                'mx': [],
            }
            for email in emails
        }

    result_map: dict[str, dict] = {}
    for idx, email in enumerate(emails):
        try:
            msg = page.evaluate(f"(document.getElementById('msg_{idx}')?.textContent || '').trim()")
        except Exception:
            msg = ''
        try:
            row_email = page.evaluate(f"(document.querySelector('#email_{idx} .ninja_column_left')?.textContent || '').trim()")
        except Exception:
            row_email = ''

        mapped_status, mapped_reason = _message_to_status(msg)
        result_map[email.lower()] = {
            'email': (row_email or email).lower(),
            'status': mapped_status,
            'reason': mapped_reason,
            'mx': [],
        }

    if not result_map:
        fallback_reason = last_info or 'MailTester page did not return results'
        for email in emails:
            result_map[email.lower()] = {
                'email': email.lower(),
                'status': 'unknown',
                'reason': fallback_reason,
                'mx': [],
            }

    return result_map


def verify_emails_via_browser(
    page,
    emails: list[str],
    verifier_url: str = 'https://mailtester.ninja/email-verifier/',
    page_timeout_ms: int = 30000,
    wait_seconds: int = 90,
    batch_size: int = 4,
    headless: bool = True,
) -> dict:
    """
    Verify a list of emails using the MailTester Ninja browser UI.

    The page is opened in a separate tab when possible so the caller's browser
    session stays on the lead-scraping page.
    """
    if not emails:
        return {'verified': [], 'risky': [], 'invalid': [], 'unknown': [], 'details': []}

    print(f"\n{Fore.CYAN}  MailTester browser verifier: checking {len(emails)} address(es)...{Style.RESET_ALL}")

    playwright = None
    context = None
    verifier_page = page
    own_page = False
    if verifier_page is None:
        verifier_page, playwright, context = _launch_dedicated_verifier_page(headless=headless)
        own_page = True
    else:
        verifier_page, own_page = _open_verifier_page(page)
    try:
        details_by_email: dict[str, dict] = {}
        effective_batch_size = 1
        if batch_size != 1:
            print(f"{Style.DIM}  MailTester UI works best one email at a time; forcing batch size to 1.{Style.RESET_ALL}")

        for batch in _chunked(emails, effective_batch_size):
            batch_results = _verify_batch(
                verifier_page,
                batch,
                verifier_url,
                page_timeout_ms,
                wait_seconds,
            )
            details_by_email.update(batch_results)

        verified, risky, invalid, unknown = [], [], [], []
        details = []

        for email in emails:
            detail = details_by_email.get(email.lower(), {
                'email': email.lower(),
                'status': 'unknown',
                'reason': 'No MailTester result returned',
                'mx': [],
            })
            details.append(detail)

            status = detail['status']
            icon = {'valid': 'OK', 'invalid': 'X', 'risky': '!', 'unknown': '?'}.get(status, '?')
            colour = {
                'valid': Fore.GREEN,
                'invalid': Fore.RED,
                'risky': Fore.YELLOW,
                'unknown': Fore.WHITE,
            }.get(status, Fore.WHITE)

            print(f"  {colour}{icon} {email:<40} [{status.upper()}] {detail['reason']}{Style.RESET_ALL}")

            if status == 'valid':
                verified.append(email)
            elif status == 'risky':
                risky.append(email)
            elif status == 'invalid':
                invalid.append(email)
            else:
                unknown.append(email)

        print(f"\n  {Fore.GREEN}Valid:   {len(verified)}{Style.RESET_ALL}  "
              f"{Fore.YELLOW}Risky: {len(risky)}{Style.RESET_ALL}  "
              f"{Fore.RED}Invalid: {len(invalid)}{Style.RESET_ALL}")

        return {
            'verified': verified,
            'risky': risky,
            'invalid': invalid,
            'unknown': unknown,
            'details': details,
        }
    finally:
        if own_page:
            try:
                verifier_page.close()
            except Exception:
                pass
            try:
                if context:
                    context.close()
            except Exception:
                pass
            try:
                if playwright:
                    playwright.stop()
            except Exception:
                pass
