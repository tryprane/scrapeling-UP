"""
Notification and logging — mirrors src/notifier.js

Handles desktop notifications, console output, and JSON log file.
"""

import os
import json
from datetime import datetime
from colorama import Fore, Style

LEADS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'leads.json')


def notify_desktop(lead: dict, job_title: str):
    """Send a macOS desktop notification for a found lead."""
    try:
        from plyer import notification

        company = lead.get('client_info', {}).get('company_name', 'Unknown Company')
        confidence = lead.get('confidence_score', '?')
        subject = lead.get('contact_strategy', {}).get('email_subject', '')

        notification.notify(
            title=f"🎯 Lead Found! [{confidence}]",
            message=f"{company}\n{job_title[:60]}\n{subject}",
            timeout=15,
        )
    except Exception as e:
        # Notification failed, non-critical
        print(f"{Style.DIM}  ⚠ Desktop notification failed: {e}{Style.RESET_ALL}")


def log_lead_to_file(lead: dict, job: dict):
    """Append a lead to the leads.json file."""
    existing = []
    if os.path.exists(LEADS_FILE):
        try:
            with open(LEADS_FILE, 'r', encoding='utf-8') as f:
                existing = json.load(f)
        except Exception:
            existing = []

    existing.append({
        'timestamp': datetime.now().isoformat(),
        'job': {
            'title': job.get('title', ''),
            'url': job.get('job_url', ''),
            'budget': job.get('budget', ''),
            'posted_time': job.get('posted_time', ''),
        },
        'lead': lead,
    })

    with open(LEADS_FILE, 'w', encoding='utf-8') as f:
        json.dump(existing, f, indent=2, ensure_ascii=False)


def print_lead(lead: dict, job: dict):
    """Print a lead to the console in a formatted way."""
    ci = lead.get('client_info', {})
    cs = lead.get('contact_strategy', {})

    print()
    print(f"{Fore.GREEN}{Style.BRIGHT}  ═══════════════════════════════════════════════════{Style.RESET_ALL}")
    print(f"{Fore.GREEN}{Style.BRIGHT}  🎯 LEAD FOUND!{Style.RESET_ALL}")
    print(f"{Fore.GREEN}{Style.BRIGHT}  ═══════════════════════════════════════════════════{Style.RESET_ALL}")
    print(f"{Fore.WHITE}  Job:        {job.get('title', '')}{Style.RESET_ALL}")
    print(f"{Fore.WHITE}  URL:        {job.get('job_url', '')}{Style.RESET_ALL}")
    print(f"{Fore.CYAN}  Confidence: {lead.get('confidence_score', '')}{Style.RESET_ALL}")
    print(f"{Fore.CYAN}  Company:    {ci.get('company_name', '—')}{Style.RESET_ALL}")
    print(f"{Fore.CYAN}  Person:     {ci.get('guessed_person', '—')}{Style.RESET_ALL}")
    print(f"{Fore.CYAN}  Website:    {ci.get('website', '—')}{Style.RESET_ALL}")
    print(f"{Style.DIM}  Query:      {ci.get('search_query_used', '—')}{Style.RESET_ALL}")
    print()
    print(f"{Fore.YELLOW}  📧 Subject: {cs.get('email_subject', '—')}{Style.RESET_ALL}")
    print(f"{Style.DIM}  {cs.get('cold_outreach_message', '')}{Style.RESET_ALL}")
    print(f"{Fore.GREEN}{Style.BRIGHT}  ═══════════════════════════════════════════════════{Style.RESET_ALL}")
    print()
