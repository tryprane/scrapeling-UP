"""
Upwork job scraper using Patchright (Playwright) directly.

Uses a persistent browser session with stealth capabilities.
Clicks through Cloudflare Turnstile, scrapes job listings,
and clicks into each job to extract the full description/summary.
"""

import os
import re
import time
import random
import tempfile
from pathlib import Path
from colorama import Fore, Style

from patchright.sync_api import sync_playwright

DEBUG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'debug-screenshots')
USER_DATA_DIR = os.path.join(Path.home(), '.cliup-browser-profile-py')
VISIBLE_USER_DATA_DIR = os.path.join(Path.home(), '.cliup-browser-profile-visible-py')

# Cloudflare Turnstile iframe URL pattern
_CF_IFRAME_PATTERN = re.compile(r"challenges\.cloudflare\.com/cdn-cgi/challenge-platform/.*")

# ── Browser lifecycle ─────────────────────────────────────────────────

_playwright = None
_context = None
_page = None


def _ensure_dirs():
    os.makedirs(DEBUG_DIR, exist_ok=True)
    os.makedirs(USER_DATA_DIR, exist_ok=True)


def start_browser(headless=False):
    """Launch a persistent Patchright/Chromium browser and return the page."""
    global _playwright, _context, _page
    _ensure_dirs()

    if _page is not None:
        return _page

    print(f"{Style.DIM}  🔧 Launching persistent browser (headless={headless})...{Style.RESET_ALL}")
    _playwright = sync_playwright().start()

    launch_dirs = [USER_DATA_DIR]
    if not headless:
        launch_dirs.append(VISIBLE_USER_DATA_DIR)
        launch_dirs.append(tempfile.mkdtemp(prefix="cliup-visible-profile-"))

    launch_error = None
    for user_data_dir in launch_dirs:
        try:
            _context = _playwright.chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                headless=headless,
                channel='chrome',                   # Use installed Chrome
                viewport={'width': 1366, 'height': 768},
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--no-sandbox',
                ],
                ignore_default_args=['--enable-automation'],
            )
            break
        except Exception as exc:
            launch_error = exc
            print(f"{Fore.YELLOW}  ⚠ Browser launch failed for {user_data_dir}: {exc}{Style.RESET_ALL}")
            _context = None

    if _context is None:
        raise launch_error

    _page = _context.pages[0] if _context.pages else _context.new_page()

    # Extra stealth: remove webdriver flag
    _page.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    """)

    print(f"{Fore.GREEN}  ✓ Browser ready{Style.RESET_ALL}")
    return _page


def close_browser():
    """Shut down the browser cleanly."""
    global _playwright, _context, _page
    try:
        if _context:
            _context.close()
        if _playwright:
            _playwright.stop()
    except Exception:
        pass
    _playwright = _context = _page = None


# ── Cloudflare Turnstile solver ───────────────────────────────────────

def _solve_turnstile(page, max_attempts=7):
    """
    Detect and click the Cloudflare Turnstile checkbox.
    The checkbox lives inside a cross-domain iframe; the wrapper ID is dynamic
    (e.g. #CZUq4) so we locate it via the iframe URL pattern instead.
    Returns True if challenge was solved or not present, False on failure.
    """
    # Quick check — is there even a Turnstile challenge?
    page_text = page.content()
    turnstile_markers = [
        'Verify you are human',
        'Just a moment...',
        'challenges.cloudflare.com',
    ]
    if not any(m in page_text for m in turnstile_markers):
        return True   # No challenge

    print(f"{Fore.YELLOW}  🛡️  Cloudflare Turnstile detected — solving...{Style.RESET_ALL}")

    for attempt in range(1, max_attempts + 1):
        # Re-check if challenge is gone
        page_text = page.content()
        if not any(m in page_text for m in turnstile_markers):
            print(f"{Fore.GREEN}  ✓ Turnstile passed (attempt {attempt}){Style.RESET_ALL}")
            return True

        # ── Strategy 1: click inside the Turnstile iframe ──
        cf_iframe = page.frame(url=_CF_IFRAME_PATTERN)
        if cf_iframe is not None:
            try:
                frame_el = cf_iframe.frame_element()
                if frame_el.is_visible():
                    box = frame_el.bounding_box()
                    if box:
                        # The checkbox is in the left portion of the iframe widget
                        click_x = box['x'] + random.randint(22, 32)
                        click_y = box['y'] + random.randint(22, 30)

                        # Human-like: slow mouse move → pause → click
                        page.mouse.move(
                            box['x'] + box['width'] * 0.5,
                            box['y'] + box['height'] * 0.5,
                            steps=random.randint(8, 20),
                        )
                        page.wait_for_timeout(random.randint(300, 700))
                        page.mouse.click(
                            click_x, click_y,
                            delay=random.randint(80, 220),
                            button='left',
                        )
                        print(f"{Style.DIM}  🖱️  Clicked Turnstile iframe (attempt {attempt}){Style.RESET_ALL}")
                        page.wait_for_timeout(random.randint(3000, 5000))
                        continue
            except Exception as e:
                print(f"{Style.DIM}  ⌊ iframe click error: {e}{Style.RESET_ALL}")

        # ── Strategy 2: find the checkbox directly on the main page ──
        try:
            # The Turnstile widget wrapper has a dynamic ID but contains
            # a label > input[type=checkbox].  Try locating it.
            checkbox = page.locator('input[type="checkbox"]').first
            if checkbox.is_visible(timeout=2000):
                box = checkbox.bounding_box()
                if box:
                    page.mouse.click(
                        box['x'] + random.randint(2, int(max(3, box['width'] - 2))),
                        box['y'] + random.randint(2, int(max(3, box['height'] - 2))),
                        delay=random.randint(80, 200),
                        button='left',
                    )
                    print(f"{Style.DIM}  🖱️  Clicked checkbox directly (attempt {attempt}){Style.RESET_ALL}")
                    page.wait_for_timeout(random.randint(3000, 5000))
                    continue
        except Exception:
            pass

        # ── Strategy 3: widget container selectors ──
        widget_selectors = [
            '#cf-turnstile',
            '#cf_turnstile',
            '[class*="turnstile"]',
            '.main-content p+div > div > div',
        ]
        for sel in widget_selectors:
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=1000):
                    box = el.bounding_box()
                    if box:
                        page.mouse.click(
                            box['x'] + random.randint(20, 30),
                            box['y'] + random.randint(20, 28),
                            delay=random.randint(80, 200),
                            button='left',
                        )
                        print(f"{Style.DIM}  🖱️  Clicked via '{sel}' (attempt {attempt}){Style.RESET_ALL}")
                        page.wait_for_timeout(random.randint(3000, 5000))
                        break
            except Exception:
                continue

        page.wait_for_timeout(random.randint(2000, 4000))

    # Final check
    page_text = page.content()
    solved = not any(m in page_text for m in turnstile_markers)
    if solved:
        print(f"{Fore.GREEN}  ✓ Turnstile solved!{Style.RESET_ALL}")
    else:
        print(f"{Fore.YELLOW}  ⚠ Turnstile may still be present after {max_attempts} attempts{Style.RESET_ALL}")
    return solved


# ── Navigation helpers ────────────────────────────────────────────────

def _navigate(page, url, timeout=60000):
    """Navigate to a URL, solve Turnstile if it appears, retry once."""
    print(f"{Style.DIM}  🌐 Navigating: {url[:80]}...{Style.RESET_ALL}")
    try:
        page.goto(url, wait_until='domcontentloaded', timeout=timeout)
        page.wait_for_timeout(3000)
    except Exception as e:
        print(f"{Fore.YELLOW}  ⚠ Navigation warning: {e}{Style.RESET_ALL}")

    # Solve Turnstile if present
    if _solve_turnstile(page):
        # After Turnstile is solved, the browser may stay on the challenge page.
        # Check if we need to re-navigate to the actual target URL.
        current_url = page.url
        if 'challenges.cloudflare.com' in current_url or 'search/jobs' not in current_url:
            print(f"{Style.DIM}  🔄 Re-navigating to target after Turnstile...{Style.RESET_ALL}")
            try:
                page.goto(url, wait_until='domcontentloaded', timeout=timeout)
                page.wait_for_timeout(3000)
            except Exception:
                pass
            # Solve again if needed (second redirect)
            _solve_turnstile(page)

    # Wait for page to settle
    try:
        page.wait_for_load_state('networkidle', timeout=15000)
    except Exception:
        pass
    page.wait_for_timeout(2000)


def _check_login_required(page):
    """
    Check if Upwork is showing a login FORM (not just a nav link).
    Only returns True if the user is on the actual login page.
    """
    try:
        current_url = page.url
        # If the URL itself is the login page
        if '/ab/account-security/login' in current_url:
            return True

        # Check for the login form input field (only present on the login page)
        login_field = page.locator('#login_username, #login_password')
        if login_field.count() > 0 and login_field.first.is_visible(timeout=1000):
            return True

    except Exception:
        pass
    return False


# ── Time parsing ──────────────────────────────────────────────────────

def _parse_posted_age_minutes(posted_text):
    """
    Parse a posted-time string into an approximate age in minutes.

    Returns:
        int minutes, or None if the text cannot be parsed.
    """
    if not posted_text:
        return None

    text = posted_text.lower().strip()

    if any(kw in text for kw in ['just now', 'seconds ago', 'moment', 'second ago']):
        return 0

    m = re.search(r'(\d+)\s*(?:minute|min)', text)
    if m:
        return int(m.group(1))

    m = re.search(r'(\d+)\s*hour', text)
    if m:
        return int(m.group(1)) * 60

    m = re.search(r'(\d+)\s*day', text)
    if m:
        return int(m.group(1)) * 60 * 24

    if 'yesterday' in text:
        return 60 * 24
    if 'week' in text:
        return 60 * 24 * 7
    if 'month' in text:
        return 60 * 24 * 30

    return None


def _is_younger_than_age(posted_text, max_minutes=15):
    """
    Return True if a job is younger than max_minutes old.
    Unknown ages are treated as False so we do not over-collect uncertain jobs.
    """
    age = _parse_posted_age_minutes(posted_text)
    if age is None:
        print(f"{Style.DIM}    ⌊ Time filter: couldn't parse '{posted_text}', skipping job{Style.RESET_ALL}")
        return False
    if age >= max_minutes:
        print(f"{Style.DIM}    ⌊ Time filter: '{posted_text}' = {age}min >= {max_minutes}min{Style.RESET_ALL}")
        return False
    return True


# ── Job card scraping ─────────────────────────────────────────────────

def scrape_search_page(page, search_url):
    """
    Scrape jobs from a single Upwork search URL using the live Playwright page.
    Returns a list of job dicts with: title, job_url, posted_time, budget, skills.
    Descriptions are NOT yet filled — call get_job_summaries() next.
    """
    jobs = []

    _navigate(page, search_url)

    if _check_login_required(page):
        print(f"{Fore.RED}  ✗ Upwork login required!{Style.RESET_ALL}")
        print(f"{Fore.YELLOW}  💡 Log into Upwork in the browser window. Session will persist.{Style.RESET_ALL}")
        return jobs

    # Wait for job cards to appear
    try:
        page.wait_for_selector('article', timeout=15000)
    except Exception:
        pass

    # ── Extract job cards ──────────────────────────────────────────────
    # Find all job title links (the most reliable anchor)
    title_links = page.locator('a[data-test="job-tile-title-link"]')
    count = title_links.count()

    if count == 0:
        # Fallback selectors
        for fallback_sel in [
            'a.air3-link[data-test*="job-tile-title"]',
            'a[href*="/jobs/"][data-ev-label="link"]',
            'a[href*="/jobs/"]',
        ]:
            title_links = page.locator(fallback_sel)
            count = title_links.count()
            if count > 0:
                break

    if count == 0:
        print(f"{Fore.YELLOW}  ⚠ No job cards found on page{Style.RESET_ALL}")
        # Save debug screenshot
        try:
            page.screenshot(path=os.path.join(DEBUG_DIR, 'no_jobs_found.png'))
        except Exception:
            pass
        return jobs

    print(f"{Fore.GREEN}  ✓ Found {count} job listings{Style.RESET_ALL}")

    for i in range(count):
        try:
            link_el = title_links.nth(i)
            title = link_el.inner_text().strip()
            href = link_el.get_attribute('href') or ''

            if href and not href.startswith('http'):
                job_url = f"https://www.upwork.com{href}"
            else:
                job_url = href

            # Navigate up to the article card container to find sibling data
            # The card structure: article > div.job-tile-header > ... > small > span (time)
            article = link_el.locator('xpath=ancestor::article')

            # Posted time — from screenshot: "Posted 1 minute ago" as plain text
            posted_time = ''
            for time_sel in [
                '[data-test="posted-on"]',
                'span[data-test="posted-on"]',
                'small span:nth-child(2)',
                'small span',
                'small.text-muted',
                'small',
            ]:
                try:
                    time_el = article.locator(time_sel).first
                    if time_el.is_visible(timeout=500):
                        t = time_el.inner_text().strip()
                        # Look for time-like text (contains 'ago', 'just', 'minute', 'hour', 'posted')
                        if t and any(kw in t.lower() for kw in ['ago', 'just', 'minute', 'hour', 'posted', 'second']):
                            posted_time = t
                            break
                except Exception:
                    continue
            
            # If none of the selectors worked, try to get any text containing time info
            if not posted_time:
                try:
                    all_smalls = article.locator('small')
                    for si in range(min(all_smalls.count(), 5)):
                        t = all_smalls.nth(si).inner_text().strip()
                        if t and any(kw in t.lower() for kw in ['ago', 'just', 'minute', 'hour', 'posted']):
                            posted_time = t
                            break
                except Exception:
                    pass

            # Budget
            budget = ''
            for budget_sel in [
                '[data-test="budget"]',
                '[data-test="is-fixed-price"]',
            ]:
                try:
                    budget_el = article.locator(budget_sel).first
                    if budget_el.is_visible(timeout=500):
                        budget = budget_el.inner_text().strip()
                        if budget:
                            break
                except Exception:
                    continue

            # Skills
            skills = ''
            try:
                skill_els = article.locator('[data-test="token"] span, a[data-test="attr-item"], span.air3-badge')
                skill_count = skill_els.count()
                if skill_count > 0:
                    skill_list = []
                    for si in range(min(skill_count, 10)):
                        s = skill_els.nth(si).inner_text().strip()
                        if s:
                            skill_list.append(s)
                    skills = ', '.join(skill_list)
            except Exception:
                pass

            if title and job_url:
                jobs.append({
                    'title': title,
                    'job_url': job_url,
                    'posted_time': posted_time,
                    'budget': budget,
                    'skills': skills,
                    'description': '',         # Filled later by get_job_summaries
                    '_card_index': i,           # Internal: used for clicking
                })

        except Exception as e:
            print(f"{Style.DIM}  ⌊ Skipped card {i}: {e}{Style.RESET_ALL}")
            continue

    return jobs


# ── Job detail extraction (click-to-open slider) ─────────────────────

def get_job_summaries(page, jobs):
    """
    For each job, click the title link on the search page to open the detail
    slider, extract the summary/description, then close the slider.
    Modifies jobs in-place.
    """
    if not jobs:
        return jobs

    print(f"{Style.DIM}  📝 Extracting job summaries ({len(jobs)} jobs)...{Style.RESET_ALL}")

    for job in jobs:
        try:
            idx = job.get('_card_index', -1)
            if idx < 0:
                continue

            # Re-find the title link (DOM may have shifted)
            title_links = page.locator('a[data-test="job-tile-title-link"]')
            if title_links.count() <= idx:
                # Try fallback
                for fallback_sel in [
                    'a.air3-link[data-test*="job-tile-title"]',
                    'a[href*="/jobs/"][data-ev-label="link"]',
                ]:
                    title_links = page.locator(fallback_sel)
                    if title_links.count() > idx:
                        break

            if title_links.count() <= idx:
                continue

            link_el = title_links.nth(idx)

            # Click to open the detail slider
            link_el.click()
            page.wait_for_timeout(random.randint(1500, 2500))

            # Wait for the detail slider to appear
            slider_visible = False
            for slider_sel in [
                'div.details-slider',
                'div.air3-fullscreen-element',
                'div.air3-slider-body',
            ]:
                try:
                    slider = page.locator(slider_sel).first
                    slider.wait_for(state='visible', timeout=5000)
                    slider_visible = True
                    break
                except Exception:
                    continue

            if not slider_visible:
                print(f"{Style.DIM}  ⌊ Slider didn't open for: {job['title'][:40]}{Style.RESET_ALL}")
                continue

            # Extract the description/summary from the slider
            summary = ''
            desc_selectors = [
                'div.air3-slider-body section:nth-child(2) > div > p',
                'div.air3-slider-body [data-test="description"]',
                'div.air3-slider-body .job-description',
                'div.air3-slider-body section p',
                'div.details-slider p',
            ]
            for dsel in desc_selectors:
                try:
                    desc_el = page.locator(dsel).first
                    if desc_el.is_visible(timeout=2000):
                        summary = desc_el.inner_text().strip()
                        if summary and len(summary) > 20:
                            break
                except Exception:
                    continue

            # If first selector got a short blurb, try grabbing all <p> tags
            if len(summary) < 50:
                try:
                    all_ps = page.locator('div.air3-slider-body p')
                    texts = []
                    for pi in range(min(all_ps.count(), 10)):
                        t = all_ps.nth(pi).inner_text().strip()
                        if t:
                            texts.append(t)
                    combined = '\n'.join(texts)
                    if len(combined) > len(summary):
                        summary = combined
                except Exception:
                    pass

            job['description'] = summary
            if summary:
                print(f"{Fore.GREEN}  ✓ Got summary: {job['title'][:45]}... ({len(summary)} chars){Style.RESET_ALL}")
            else:
                print(f"{Style.DIM}  ⌊ No summary found for: {job['title'][:45]}{Style.RESET_ALL}")

            # Close the slider — press Escape or click the close button
            try:
                close_btn = page.locator('button[aria-label="Close"], .air3-fullscreen-element button.close, button.air3-btn-close')
                if close_btn.count() > 0 and close_btn.first.is_visible(timeout=1000):
                    close_btn.first.click()
                else:
                    page.keyboard.press('Escape')
            except Exception:
                page.keyboard.press('Escape')
            page.wait_for_timeout(random.randint(800, 1500))

        except Exception as e:
            print(f"{Style.DIM}  ⌊ Error on job {job.get('title', '?')[:30]}: {e}{Style.RESET_ALL}")
            # Try to close any open slider
            try:
                page.keyboard.press('Escape')
                page.wait_for_timeout(500)
            except Exception:
                pass
            continue

    return jobs


# ── Main scrape orchestrator ──────────────────────────────────────────

def scrape_all_jobs(search_urls, headless=False, max_minutes=15):
    """
    Scrape all configured search URLs using a persistent browser session.

    Flow:
    1. Open/reuse the persistent browser
    2. Navigate to each search URL, solve Turnstile if needed
    3. Extract job cards, filter to jobs posted younger than max_minutes
    4. Click into each job to get the full summary/description
    5. Deduplicate across URLs

    Returns a list of unique, recent job dicts with descriptions.
    """
    page = start_browser(headless=headless)
    all_jobs = []
    seen_urls = set()

    for i, url in enumerate(search_urls):
        print(f"\n{Fore.BLUE}  📡 Search URL {i + 1}/{len(search_urls)}{Style.RESET_ALL}")

        raw_jobs = scrape_search_page(page, url)

        # Filter to jobs that are younger than the target age
        recent_jobs = []
        for job in raw_jobs:
            if job['job_url'] in seen_urls:
                continue
            if _is_younger_than_age(job['posted_time'], max_minutes):
                recent_jobs.append(job)
                seen_urls.add(job['job_url'])
            else:
                print(f"{Style.DIM}  ⌊ Skipped (too old: {job['posted_time']}): {job['title'][:40]}{Style.RESET_ALL}")

        print(f"{Fore.WHITE}  🕐 {len(recent_jobs)} jobs younger than {max_minutes} min{Style.RESET_ALL}")

        # Click into each eligible job to get the summary
        if recent_jobs:
            get_job_summaries(page, recent_jobs)

        all_jobs.extend(recent_jobs)

        # Human-like delay between searches
        if i < len(search_urls) - 1:
            wait_sec = 2.0 + random.random() * 3.0
            print(f"{Style.DIM}  ⏳ Waiting {wait_sec:.1f}s before next search...{Style.RESET_ALL}")
            time.sleep(wait_sec)

    return all_jobs
