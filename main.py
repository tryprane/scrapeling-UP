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

from colorama import init as colorama_init, Fore, Style

from config import config
from db import (
    init_db, is_job_seen, mark_job_seen, save_lead, get_stats,
    start_run, complete_run, fail_run,
    save_scraped_job, update_job_ai_status, cleanup_old_data
)
from scraper import scrape_all_jobs, close_browser
from analyzer import analyze_job
from notifier import notify_desktop, log_lead_to_file, print_lead

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
            save_lead(job['job_url'], job['title'], result)
            log_lead_to_file(result, job)
            update_job_ai_status(job_db_id, 'lead_found', result)

            if config['enable_notifications']:
                notify_desktop(result, job['title'])
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
