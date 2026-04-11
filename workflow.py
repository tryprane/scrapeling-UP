"""
Main workflow runner for the lead hunter.

This version keeps the existing scraper/analyzer pipeline, but swaps the
outreach path to the outbound_live / prane email API and adds a public-web
fallback while finding contact details.
"""

from __future__ import annotations

import os
import json
import signal
import subprocess
import sys
import time
from datetime import datetime

from colorama import Fore, Style, init as colorama_init

if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from analyzer import analyze_job
from contact_discovery import discover_contacts
from config import config
from db import (
    cleanup_old_data,
    complete_run,
    fail_run,
    get_stats,
    init_db,
    is_job_seen,
    mark_job_seen,
    save_lead,
    save_outreach_result,
    save_scraped_job,
    start_run,
    update_job_ai_status,
)
from email_verifier import get_sendable_emails, verify_emails
from notifier import log_lead_to_file, notify_desktop, print_lead
from outreach_mailer import generate_email_draft, should_skip_job
from prane_mailer import plain_text_to_html, send_batch
from scraper import close_browser, start_browser
from scrapling_scraper import scrape_all_jobs

colorama_init()

_dashboard_proc = None
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workflow.log")


def print_banner():
    print()
    print(f"{Fore.MAGENTA}{Style.BRIGHT}  ╔════════════════════════════════════════════════════════════╗{Style.RESET_ALL}")
    print(f"{Fore.MAGENTA}{Style.BRIGHT}  ║                                                            ║{Style.RESET_ALL}")
    print(f"{Fore.MAGENTA}{Style.BRIGHT}  ║{Fore.WHITE}{Style.BRIGHT}       Lead Hunter Workflow with Prane Outbound API        {Fore.MAGENTA}║{Style.RESET_ALL}")
    print(f"{Fore.MAGENTA}{Style.BRIGHT}  ║{Style.DIM}       Upwork scrape -> AI qualify -> web search -> send   {Fore.MAGENTA}║{Style.RESET_ALL}")
    print(f"{Fore.MAGENTA}{Style.BRIGHT}  ║                                                            ║{Style.RESET_ALL}")
    print(f"{Fore.MAGENTA}{Style.BRIGHT}  ╚════════════════════════════════════════════════════════════╝{Style.RESET_ALL}")
    print()
    print(f"{Style.DIM}  Polling every {config['poll_interval_minutes']} min | {len(config['search_urls'])} search URL(s){Style.RESET_ALL}")
    print(f"{Style.DIM}  Dashboard: python3 dashboard.py -> http://localhost:5050{Style.RESET_ALL}")
    print(f"{Style.DIM}  Press Ctrl+C to stop\n{Style.RESET_ALL}")


def log_step(stage: str, message: str, cycle: int | None = None, **fields):
    """
    Emit a timestamped step log to stdout and append it to workflow.log.
    """
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    prefix = f"[{ts}]"
    if cycle is not None:
        prefix += f" [cycle {cycle}]"
    line = f"{prefix} [{stage}] {message}"
    if fields:
        line += f" | {json.dumps(fields, ensure_ascii=False, sort_keys=True)}"
    print(line)

    try:
        record = {
            "timestamp": ts,
            "cycle": cycle,
            "stage": stage,
            "message": message,
            **fields,
        }
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _start_dashboard():
    """Launch dashboard.py as a background subprocess."""
    global _dashboard_proc
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.py")
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


def run_outreach_pipeline(page, job: dict, lead_result: dict, lead_db_id: int):
    """
    Run outreach for a single lead.
    """
    log_step("outreach", "pipeline started", title=job.get("title", ""), lead_id=lead_db_id)

    skip, skip_reason = should_skip_job(job)
    if skip:
        log_step("outreach", f"skipping outreach: {skip_reason}", lead_id=lead_db_id)
        save_outreach_result(
            lead_id=lead_db_id,
            send_status="skipped",
            skipped_reason=skip_reason,
        )
        return

    contacts = discover_contacts(page, job, lead_result)
    raw_search_response = contacts.get("search_response", "")
    candidate_emails = contacts.get("emails", [])
    log_step("outreach", "contact discovery completed", lead_id=lead_db_id, candidate_emails=len(candidate_emails))

    if not candidate_emails:
        log_step("outreach", "no email candidates found; skipping outreach", lead_id=lead_db_id)
        save_outreach_result(
            lead_id=lead_db_id,
            grok_response=raw_search_response,
            contacts=contacts,
            send_status="no_emails",
        )
        return

    log_step("outreach", "verifying candidate emails", lead_id=lead_db_id, email_count=len(candidate_emails))
    verification = verify_emails(candidate_emails, do_smtp_probe=True)
    sendable_emails = get_sendable_emails(verification, include_risky=False)

    if not sendable_emails:
        log_step("outreach", "no verified emails after verification", lead_id=lead_db_id)
        contacts["emails"] = []
        contacts["verification"] = verification["details"]
        save_outreach_result(
            lead_id=lead_db_id,
            grok_response=raw_search_response,
            contacts=contacts,
            send_status="no_verified_emails",
        )
        return

    log_step("outreach", "emails verified and ready", lead_id=lead_db_id, sendable_emails=sendable_emails)
    contacts["emails"] = sendable_emails
    contacts["verification"] = verification["details"]

    log_step("outreach", "generating email draft", lead_id=lead_db_id)
    email_data = generate_email_draft(job, contacts)
    if email_data.get("error") and not email_data.get("subject"):
        log_step("outreach", f"email draft failed: {email_data['error']}", lead_id=lead_db_id)
        save_outreach_result(
            lead_id=lead_db_id,
            grok_response=raw_search_response,
            contacts=contacts,
            send_status="draft_failed",
        )
        return

    log_step("outreach", "sending emails via Prane API", lead_id=lead_db_id, recipients=email_data.get("to_emails", []))
    email_html = plain_text_to_html(email_data.get("body", ""))
    send_results = send_batch(email_data.get("to_emails", []), email_data.get("subject", ""), email_html)

    sent_count = sum(1 for r in send_results if r["status"] == "sent")
    total = len(send_results)
    if sent_count == total and total > 0:
        status = "sent"
    elif sent_count > 0:
        status = "partial"
    else:
        status = "send_failed"

    log_step(
        "outreach",
        "send completed",
        lead_id=lead_db_id,
        status=status,
        sent_count=sent_count,
        total=total,
    )

    save_outreach_result(
        lead_id=lead_db_id,
        grok_response=raw_search_response,
        contacts=contacts,
        email_subject=email_data.get("subject", ""),
        email_body=email_html,
        emails_sent_to=send_results,
        send_status=status,
    )

    log_step("outreach", f"outreach complete: {sent_count}/{total} emails sent", lead_id=lead_db_id, status=status)


def run_poll_cycle(cycle_number: int):
    start_time = time.time()
    now = datetime.now().strftime("%H:%M:%S")
    log_step("cycle", f"started at {now}", cycle=cycle_number)

    cleanup_old_data()
    run_id = start_run(cycle_number)
    log_step("cycle", "database cleanup complete and run registered", cycle=cycle_number, run_id=run_id)

    log_step("scrape", f"scraping jobs younger than {config['poll_interval_minutes']} minutes", cycle=cycle_number)
    try:
        jobs = scrape_all_jobs(
            config["search_urls"],
            headless=True,
            max_minutes=config["poll_interval_minutes"],
        )
    except Exception as e:
        log_step("scrape", f"failed: {e}", cycle=cycle_number)
        fail_run(run_id, str(e))
        return

    log_step("scrape", f"found {len(jobs)} eligible job listings", cycle=cycle_number)
    if not jobs:
        log_step("cycle", "no jobs found; cycle complete", cycle=cycle_number)
        complete_run(run_id, jobs_found=0, jobs_new=0, leads_found=0)
        return

    new_jobs = [j for j in jobs if not is_job_seen(j["job_url"])]
    log_step("filter", f"{len(new_jobs)} new jobs to analyze", cycle=cycle_number)

    if not new_jobs:
        log_step("cycle", "all jobs already processed", cycle=cycle_number)
        complete_run(run_id, jobs_found=len(jobs), jobs_new=0, leads_found=0)
        return

    leads_found = 0
    for job in new_jobs:
        mark_job_seen(job["job_url"], job["title"])
        job_db_id = save_scraped_job(cycle_number, job)

        desc = job.get("description", "")
        if not desc or len(desc) < 50:
            log_step("analyze", "skipped job with missing/short description", cycle=cycle_number, title=job["title"][:80])
            update_job_ai_status(job_db_id, "skipped")
            continue

        log_step("analyze", "sending job to Gemini for classification", cycle=cycle_number, title=job["title"][:80])
        update_job_ai_status(job_db_id, "analyzing")
        result = analyze_job(job)

        if result.get("status") == "LEAD_FOUND":
            leads_found += 1
            log_step("analyze", "lead found", cycle=cycle_number, title=job["title"][:80], confidence=result.get("confidence_score"))
            print_lead(result, job)
            lead_db_id = save_lead(job["job_url"], job["title"], result)
            log_lead_to_file(result, job)
            update_job_ai_status(job_db_id, "lead_found", result)

            if config["enable_notifications"]:
                notify_desktop(result, job["title"])

            try:
                page = start_browser(headless=config["headless"])
                run_outreach_pipeline(page, job, result, lead_db_id)
            except Exception as oe:
                log_step("outreach", f"pipeline error: {oe}", cycle=cycle_number, lead_id=lead_db_id)
        elif result.get("status") == "NO_LEAD":
            reason = result.get("reason", "")
            reason_str = f" ({reason})" if reason else ""
            log_step("analyze", f"no lead{reason_str}", cycle=cycle_number, title=job["title"][:80])
            update_job_ai_status(job_db_id, "no_lead", result)
        else:
            error_msg = str(result.get("error", ""))
            log_step("analyze", f"AI issue: {error_msg}", cycle=cycle_number, title=job["title"][:80])
            update_job_ai_status(job_db_id, "error", result, error_msg)

        log_step("throttle", f"waiting {config['ai_call_delay_seconds']} seconds before next AI call", cycle=cycle_number)
        time.sleep(config["ai_call_delay_seconds"])

    complete_run(run_id, jobs_found=len(jobs), jobs_new=len(new_jobs), leads_found=leads_found)
    elapsed = f"{time.time() - start_time:.1f}"
    stats = get_stats()
    log_step("cycle", f"complete in {elapsed}s", cycle=cycle_number, leads_found=leads_found, total_leads=stats["total_leads"], total_scanned=stats["total_seen"])


def main():
    print_banner()

    if not config["gemini_api_key"]:
        print(f"\n{Fore.RED}{Style.BRIGHT}  ✗ ERROR: GEMINI_API_KEY is not set.{Style.RESET_ALL}")
        print(f"{Fore.RED}  Copy .env.example to .env and add your key from https://aistudio.google.com\n{Style.RESET_ALL}")
        sys.exit(1)

    init_db()
    log_step("startup", "database initialized")
    _start_dashboard()

    if not config["headless"]:
        log_step("startup", "starting visible browser warm-up")
        try:
            start_browser(headless=config["headless"])
            log_step("startup", "browser opened in visible mode")
        except Exception as e:
            log_step("startup", f"browser warm-up failed: {e}")

    running = {"value": True}

    def handle_signal(sig, frame):
        log_step("shutdown", "shutting down gracefully")
        running["value"] = False
        _stop_dashboard()
        close_browser()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    cycle = 0
    while running["value"]:
        cycle += 1
        try:
            run_poll_cycle(cycle)
        except Exception as e:
            log_step("cycle", f"unhandled cycle error: {e}", cycle=cycle)

        if not running["value"]:
            break

        sleep_sec = config["poll_interval_minutes"] * 60
        log_step("sleep", f"sleeping {config['poll_interval_minutes']} min until next cycle", cycle=cycle)
        time.sleep(sleep_sec)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"{Fore.RED}Fatal error: {e}{Style.RESET_ALL}")
        _stop_dashboard()
        close_browser()
        sys.exit(1)
