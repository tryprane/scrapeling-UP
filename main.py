"""
Upwork Lead Hunter v2.0 (Python/Patchright Edition)
Entry point — polls Upwork, scrapes jobs, analyzes with Gemini AI,
and notifies the user of discovered leads.

Dashboard auto-starts at http://localhost:5050
"""

import sys
import os
import time
import signal
import random
import subprocess
from datetime import datetime

# Fix Windows console encoding for emoji/unicode characters
if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

from colorama import init as colorama_init, Fore, Style

from config import config
from db import (
    init_db, is_job_seen, mark_job_seen, save_lead, get_stats,
    start_run, complete_run, fail_run,
    save_scraped_job, update_job_ai_status, cleanup_old_data,
    save_outreach_result, update_outreach_status, get_setting,
)
from scraper import scrape_all_jobs, close_browser, start_browser
from analyzer import analyze_job
from notifier import notify_desktop, log_lead_to_file, print_lead
from grok_searcher import search_contacts_via_grok
from contact_extractor import extract_contacts
from outreach_mailer import should_skip_job, generate_email_draft, send_via_webhook

colorama_init()


# ── Banner ───────────────────────────────────────────────────────────

def print_banner():
    print()
    print(f"{Fore.MAGENTA}{Style.BRIGHT}  ╔══════════════════════════════════════════════════╗{Style.RESET_ALL}")
    print(f"{Fore.MAGENTA}{Style.BRIGHT}  ║                                                  ║{Style.RESET_ALL}")
    print(f"{Fore.MAGENTA}{Style.BRIGHT}  ║{Fore.WHITE}{Style.BRIGHT}        ⚡ UPWORK LEAD HUNTER v2.0 ⚡           {Fore.MAGENTA}║{Style.RESET_ALL}")
    print(f"{Fore.MAGENTA}{Style.BRIGHT}  ║{Style.DIM}       Python / Patchright Edition               {Fore.MAGENTA}{Style.BRIGHT}║{Style.RESET_ALL}")
    print(f"{Fore.MAGENTA}{Style.BRIGHT}  ║{Style.DIM}            by Femur Studio                     {Fore.MAGENTA}{Style.BRIGHT}║{Style.RESET_ALL}")
    print(f"{Fore.MAGENTA}{Style.BRIGHT}  ║                                                  ║{Style.RESET_ALL}")
    print(f"{Fore.MAGENTA}{Style.BRIGHT}  ╚══════════════════════════════════════════════════╝{Style.RESET_ALL}")
    print()
    print(f"{Style.DIM}  Polling every {config['poll_interval_minutes']} min | {len(config['search_urls'])} search URL(s) | Recent jobs only{Style.RESET_ALL}")
    print(f"{Style.DIM}  Dashboard: python3 dashboard.py → http://localhost:5050{Style.RESET_ALL}")
    print(f"{Style.DIM}  Press Ctrl+C to stop\n{Style.RESET_ALL}")


# ── Outreach pipeline ────────────────────────────────────────────────

def run_outreach_pipeline(page, job: dict, lead_result: dict, lead_db_id: int):
    """
    Run the full outreach pipeline for a discovered lead:
    1. Search contacts via Grok
    2. Extract contacts via Gemini
    3. Filter job suitability
    4. Generate email draft
    5. Send via n8n webhook
    """
    print(f"\n{Fore.CYAN}{Style.BRIGHT}  ══ Outreach Pipeline ══{Style.RESET_ALL}")

    # Step 0: Check if job should be skipped
    skip, skip_reason = should_skip_job(job)
    if skip:
        print(f"{Fore.YELLOW}  ⏭ Skipping outreach: {skip_reason}{Style.RESET_ALL}")
        save_outreach_result(
            lead_id=lead_db_id,
            send_status='skipped',
            skipped_reason=skip_reason,
        )
        return

    # Build a summary from the lead data for Grok
    ci = lead_result.get('client_info', {})
    cs = lead_result.get('contact_strategy', {})
    lead_summary = (
        f"Job Title: {job.get('title', '')}\n"
        f"Budget: {job.get('budget', 'Not specified')}\n"
        f"Skills: {job.get('skills', '')}\n"
        f"Company: {ci.get('company_name', 'Unknown')}\n"
        f"Person: {ci.get('guessed_person', 'Unknown')}\n"
        f"Website: {ci.get('website', '')}\n"
        f"Search Query: {ci.get('search_query_used', '')}\n"
        f"Description: {job.get('description', '')[:500]}\n"
    )

    # Step 1: Grok search
    print(f"{Fore.CYAN}  Step 1/4: Searching contacts via Grok...{Style.RESET_ALL}")
    grok_response = search_contacts_via_grok(page, lead_summary)

    if not grok_response:
        print(f"{Fore.YELLOW}  ⚠ No Grok response — saving partial result{Style.RESET_ALL}")
        save_outreach_result(
            lead_id=lead_db_id,
            grok_response='',
            send_status='grok_failed',
        )
        return

    # Step 2: Extract contacts
    print(f"{Fore.CYAN}  Step 2/4: Extracting contacts via Gemini...{Style.RESET_ALL}")
    contacts = extract_contacts(grok_response)

    if not contacts.get('emails'):
        print(f"{Fore.YELLOW}  ⚠ No email contacts found — saving result{Style.RESET_ALL}")
        save_outreach_result(
            lead_id=lead_db_id,
            grok_response=grok_response,
            contacts=contacts,
            send_status='no_emails',
        )
        return

    # Step 3: Generate email draft
    print(f"{Fore.CYAN}  Step 3/4: Generating email draft...{Style.RESET_ALL}")
    email_data = generate_email_draft(job, contacts)

    if email_data.get('error') and not email_data.get('subject'):
        print(f"{Fore.YELLOW}  ⚠ Email draft failed: {email_data['error']}{Style.RESET_ALL}")
        save_outreach_result(
            lead_id=lead_db_id,
            grok_response=grok_response,
            contacts=contacts,
            send_status='draft_failed',
        )
        return

    # Step 4: Send via webhook
    webhook_url = get_setting('n8n_webhook_url', '')
    print(f"{Fore.CYAN}  Step 4/4: Sending emails via n8n webhook...{Style.RESET_ALL}")
    send_results = send_via_webhook(webhook_url, email_data)

    # Determine overall status
    sent_count = sum(1 for r in send_results if r['status'] == 'sent')
    total = len(send_results)
    if sent_count == total and total > 0:
        status = 'sent'
    elif sent_count > 0:
        status = 'partial'
    elif any(r['status'] == 'no_webhook' for r in send_results):
        status = 'no_webhook'
    else:
        status = 'send_failed'

    # Save complete outreach result
    save_outreach_result(
        lead_id=lead_db_id,
        grok_response=grok_response,
        contacts=contacts,
        email_subject=email_data.get('subject', ''),
        email_body=email_data.get('body', ''),
        emails_sent_to=send_results,
        send_status=status,
    )

    print(f"{Fore.GREEN}{Style.BRIGHT}  ✓ Outreach complete: {sent_count}/{total} emails sent{Style.RESET_ALL}")


# ── Main poll cycle ──────────────────────────────────────────────────

def run_poll_cycle(cycle_number: int):
    start_time = time.time()
    now = datetime.now().strftime('%H:%M:%S')
    print(f"\n{Fore.BLUE}{Style.BRIGHT}  ────── Cycle #{cycle_number} [{now}] ──────{Style.RESET_ALL}")

    # Clean up data older than 24 hours
    cleanup_old_data()

    # Record run start in DB
    run_id = start_run(cycle_number)

    # 1. Scrape — browser stays open, only fetches recent jobs
    print(f"{Style.DIM}  📡 Scraping Upwork for jobs posted in last {config['poll_interval_minutes']} min...{Style.RESET_ALL}")
    try:
        jobs = scrape_all_jobs(
            config['search_urls'],
            headless=config['headless'],
            max_minutes=config['poll_interval_minutes'],
        )
    except Exception as e:
        print(f"{Fore.RED}  ✗ Scraping failed: {e}{Style.RESET_ALL}")
        fail_run(run_id, str(e))
        return

    print(f"{Fore.WHITE}  📋 Found {len(jobs)} recent job listings{Style.RESET_ALL}")

    if len(jobs) == 0:
        print(f"{Style.DIM}  ✓ No recent jobs found. Will check again next cycle.{Style.RESET_ALL}")
        complete_run(run_id, jobs_found=0, jobs_new=0, leads_found=0)
        return

    # 2. Filter out already-seen jobs
    new_jobs = [j for j in jobs if not is_job_seen(j['job_url'])]
    print(f"{Fore.WHITE}  🆕 {len(new_jobs)} new jobs to analyze{Style.RESET_ALL}")

    if len(new_jobs) == 0:
        print(f"{Style.DIM}  ✓ All jobs already processed. Sleeping...{Style.RESET_ALL}")
        complete_run(run_id, jobs_found=len(jobs), jobs_new=0, leads_found=0)
        return

    # 3. Save and analyze each new job with Gemini AI
    leads_found = 0
    for job in new_jobs:
        mark_job_seen(job['job_url'], job['title'])

        # Save to dashboard DB
        job_db_id = save_scraped_job(cycle_number, job)

        # Skip super-short descriptions
        desc = job.get('description', '')
        if not desc or len(desc) < 50:
            print(f"{Style.DIM}  ⌊ Skipped (no/short summary): {job['title'][:50]}{Style.RESET_ALL}")
            update_job_ai_status(job_db_id, 'skipped')
            continue

        print(f"{Style.DIM}  🔍 Analyzing: {job['title'][:60]}...{Style.RESET_ALL}")
        update_job_ai_status(job_db_id, 'analyzing')

        result = analyze_job(job)

        if result.get('status') == 'LEAD_FOUND':
            leads_found += 1
            print_lead(result, job)
            lead_db_id = save_lead(job['job_url'], job['title'], result)
            log_lead_to_file(result, job)
            update_job_ai_status(job_db_id, 'lead_found', result)

            if config['enable_notifications']:
                notify_desktop(result, job['title'])

            # Run outreach pipeline
            try:
                page = start_browser(headless=config['headless'])
                run_outreach_pipeline(page, job, result, lead_db_id)
            except Exception as oe:
                print(f"{Fore.YELLOW}  ⚠ Outreach pipeline error: {oe}{Style.RESET_ALL}")
        elif result.get('status') == 'NO_LEAD':
            reason = result.get('reason', '')
            reason_str = f" ({reason})" if reason else ""
            print(f"{Style.DIM}  ⌊ No lead: {job['title'][:50]}{reason_str}{Style.RESET_ALL}")
            update_job_ai_status(job_db_id, 'no_lead', result)
        else:
            # Error or unexpected response
            error_msg = str(result.get('error', ''))
            print(f"{Style.DIM}  ⌊ AI issue: {job['title'][:50]}{Style.RESET_ALL}")
            update_job_ai_status(job_db_id, 'error', result, error_msg)

        # Delay between AI calls to avoid rate limits (1 per minute)
        print(f"{Style.DIM}  ⏳ Waiting 60 seconds before next API call...{Style.RESET_ALL}")
        time.sleep(60)

    # Complete run
    complete_run(run_id, jobs_found=len(jobs), jobs_new=len(new_jobs), leads_found=leads_found)

    # Stats
    elapsed = f"{time.time() - start_time:.1f}"
    stats = get_stats()
    print()
    print(f"{Fore.BLUE}  ✓ Cycle #{cycle_number} complete in {elapsed}s{Style.RESET_ALL}")
    print(f"{Fore.BLUE}    Leads this cycle: {leads_found} | Total leads: {stats['total_leads']} | Total scanned: {stats['total_seen']}{Style.RESET_ALL}")


# ── Entry Point ──────────────────────────────────────────────────────

# ── Dashboard auto-start ─────────────────────────────────────────────

_dashboard_proc = None

def _start_dashboard():
    """Launch dashboard.py as a background subprocess."""
    global _dashboard_proc
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'dashboard.py')
    try:
        _dashboard_proc = subprocess.Popen(
            [sys.executable, script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print(f"{Fore.GREEN}  ✓ Dashboard started at http://localhost:5050{Style.RESET_ALL}")
    except Exception as e:
        print(f"{Fore.YELLOW}  ⚠ Could not start dashboard: {e}{Style.RESET_ALL}")

def _stop_dashboard():
    """Kill the dashboard subprocess."""
    global _dashboard_proc
    if _dashboard_proc:
        try:
            _dashboard_proc.terminate()
            _dashboard_proc.wait(timeout=3)
        except Exception:
            try:
                _dashboard_proc.kill()
            except Exception:
                pass
        _dashboard_proc = None


def main():
    print_banner()

    # Validate config
    if not config['gemini_api_key']:
        print(f"\n{Fore.RED}{Style.BRIGHT}  ✗ ERROR: GEMINI_API_KEY is not set.{Style.RESET_ALL}")
        print(f"{Fore.RED}  Copy .env.example to .env and add your key from https://aistudio.google.com\n{Style.RESET_ALL}")
        sys.exit(1)

    # Init DB
    init_db()
    print(f"{Style.DIM}  ✓ Database initialized{Style.RESET_ALL}")

    # Start dashboard server
    _start_dashboard()

    # Graceful shutdown
    running = {'value': True}

    def handle_signal(sig, frame):
        print(f"\n\n{Fore.YELLOW}  ⏹  Shutting down gracefully...{Style.RESET_ALL}")
        running['value'] = False
        _stop_dashboard()
        close_browser()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # Poll loop
    cycle = 0
    while running['value']:
        cycle += 1
        try:
            run_poll_cycle(cycle)
        except Exception as e:
            print(f"{Fore.RED}  ✗ Cycle error: {e}{Style.RESET_ALL}")

        if not running['value']:
            break

        # Wait for next cycle
        sleep_sec = config['poll_interval_minutes'] * 60
        print(f"\n{Style.DIM}  💤 Sleeping {config['poll_interval_minutes']} min until next cycle...\n{Style.RESET_ALL}")
        time.sleep(sleep_sec)


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(f"{Fore.RED}Fatal error: {e}{Style.RESET_ALL}")
        _stop_dashboard()
        close_browser()
        sys.exit(1)
