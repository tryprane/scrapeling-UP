"""
Outreach Mailer — Generates email drafts and sends via n8n webhook.

Handles:
1. Job filtering (skips WordPress, highly senior positions, etc.)
2. Email draft generation using Gemini AI (as Saimands Roy from Femur Studio)
3. Sending emails individually via n8n webhook

Can be tested standalone:
    python -c "from outreach_mailer import should_skip_job; print(should_skip_job({'title':'WordPress Developer','skills':'WordPress'}))"
"""

import json
import requests
from google import genai
from google.genai import types
from colorama import Fore, Style

from config import config

_client = None

# ── Job filter keywords ──────────────────────────────────────────────

SKIP_TITLE_KEYWORDS = [
    'wordpress', 'wp developer', 'wp expert',
    'senior developer', 'staff engineer', 'principal engineer',
    'lead developer', 'lead engineer', 'architect',
    'vp of engineering', 'cto', 'director of engineering',
    'devops engineer', 'sre ', 'site reliability',
]

SKIP_SKILL_KEYWORDS = [
    'wordpress', 'wp', 'elementor', 'woocommerce', 'divi',
    'shopify liquid',
]

# ── Email generation prompt ──────────────────────────────────────────

EMAIL_PROMPT = """You are writing a cold outreach email for Saimands Roy from Femur Studio.

CONTEXT: Saimands Roy saw a job posting on Upwork and wants to apply directly via email.
The email should be:
- Very concise (under 100 words for the body)
- Professional but human and friendly
- Reference the specific project/need from the job posting
- Highlight Femur Studio's relevant capabilities
- Include a clear call to action (schedule a call, reply, etc.)
- Do NOT sound robotic or templated

OUTPUT ONLY THIS JSON, NOTHING ELSE:
{
  "subject": "Short punchy subject line (under 10 words)",
  "body": "The full email body. Start with a greeting, mention their project, pitch briefly, end with CTA. Sign off as Saimands Roy, Femur Studio."
}

CRITICAL: Output ONLY valid JSON. No markdown, no code fences."""


def _get_client():
    """Get or create the Gemini client instance."""
    global _client
    if _client:
        return _client

    if not config['gemini_api_key']:
        raise RuntimeError('GEMINI_API_KEY is not set — cannot generate emails.')

    _client = genai.Client(api_key=config['gemini_api_key'])
    return _client


def should_skip_job(job: dict) -> tuple:
    """
    Check if a job should be skipped based on title and skills.

    Args:
        job: Job dict with 'title' and 'skills' keys

    Returns:
        (should_skip: bool, reason: str)
    """
    title = (job.get('title', '') or '').lower()
    skills = (job.get('skills', '') or '').lower()

    # Check title keywords
    for kw in SKIP_TITLE_KEYWORDS:
        if kw in title:
            return (True, f"Title contains '{kw}'")

    # Check skills keywords
    for kw in SKIP_SKILL_KEYWORDS:
        if kw in skills:
            return (True, f"Skills contain '{kw}'")

    return (False, '')


def generate_email_draft(job: dict, contacts: dict) -> dict:
    """
    Generate an email draft for applying to a job.

    Args:
        job: Job dict with title, description, budget, skills
        contacts: Extracted contacts dict with emails list

    Returns:
        {
            'subject': str,
            'body': str,
            'to_emails': list[str],
            'error': str (if any)
        }
    """
    emails = contacts.get('emails', [])
    if not emails:
        return {
            'subject': '',
            'body': '',
            'to_emails': [],
            'error': 'No email addresses found in contacts',
        }

    client = _get_client()

    prompt = f"""Generate a cold outreach email for this job:

JOB TITLE: {job.get('title', '')}
BUDGET: {job.get('budget', 'Not specified')}
SKILLS: {job.get('skills', 'None listed')}
DESCRIPTION: {job.get('description', '(No description)')[:500]}
COMPANY/PERSON: {contacts.get('summary', 'Unknown')}

Respond with ONLY the JSON as instructed."""

    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=EMAIL_PROMPT + '\n\n' + prompt,
            config=types.GenerateContentConfig(
                temperature=0.4,
                max_output_tokens=1024,
            ),
        )

        text = response.text.strip()

        # Strip markdown code fences
        if text.startswith('```json'):
            text = text[7:]
        elif text.startswith('```'):
            text = text[3:]
        if text.endswith('```'):
            text = text[:-3]
        text = text.strip()

        parsed = json.loads(text)

        result = {
            'subject': parsed.get('subject', ''),
            'body': parsed.get('body', ''),
            'to_emails': emails,
            'error': '',
        }

        print(f"{Fore.GREEN}  ✓ Email draft generated: \"{result['subject']}\"{Style.RESET_ALL}")
        print(f"{Style.DIM}    To: {', '.join(emails)}{Style.RESET_ALL}")

        return result

    except Exception as e:
        print(f"{Fore.RED}  ✗ Email draft generation error: {e}{Style.RESET_ALL}")
        return {
            'subject': '',
            'body': '',
            'to_emails': emails,
            'error': str(e),
        }


def send_via_webhook(webhook_url: str, email_data: dict) -> list:
    """
    Send emails individually via n8n webhook.

    Each email is sent as a separate POST request to the webhook.

    Args:
        webhook_url: The n8n webhook URL
        email_data: Dict with 'subject', 'body', 'to_emails'

    Returns:
        List of dicts: [{'email': str, 'status': 'sent'|'failed', 'error': str}]
    """
    if not webhook_url:
        print(f"{Fore.YELLOW}  ⚠ No webhook URL configured — skipping email send{Style.RESET_ALL}")
        return [{'email': e, 'status': 'no_webhook', 'error': 'Webhook URL not configured'}
                for e in email_data.get('to_emails', [])]

    results = []
    subject = email_data.get('subject', '')
    body = email_data.get('body', '')
    emails = email_data.get('to_emails', [])

    if not emails:
        return []

    for email_addr in emails:
        payload = {
            'to': email_addr,
            'subject': subject,
            'body': body,
            'from_name': 'Saimands Roy',
            'from_company': 'Femur Studio',
        }

        try:
            print(f"{Style.DIM}  📧 Sending email to {email_addr}...{Style.RESET_ALL}")
            resp = requests.post(
                webhook_url,
                json=payload,
                headers={'Content-Type': 'application/json'},
                timeout=30,
            )

            if resp.status_code in (200, 201, 202):
                print(f"{Fore.GREEN}  ✓ Email sent to {email_addr}{Style.RESET_ALL}")
                results.append({
                    'email': email_addr,
                    'status': 'sent',
                    'error': '',
                })
            else:
                error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                print(f"{Fore.RED}  ✗ Failed to send to {email_addr}: {error}{Style.RESET_ALL}")
                results.append({
                    'email': email_addr,
                    'status': 'failed',
                    'error': error,
                })

        except Exception as e:
            print(f"{Fore.RED}  ✗ Webhook error for {email_addr}: {e}{Style.RESET_ALL}")
            results.append({
                'email': email_addr,
                'status': 'failed',
                'error': str(e),
            })

    return results


# ── Standalone test ──────────────────────────────────────────────────
if __name__ == '__main__':
    # Test filtering
    print("=== Job Filter Tests ===")
    test_jobs = [
        {'title': 'WordPress Developer Needed', 'skills': 'WordPress, PHP'},
        {'title': 'Senior Developer for Enterprise', 'skills': 'Java, Spring'},
        {'title': 'Build a React Dashboard', 'skills': 'React, Node.js'},
        {'title': 'Staff Engineer - Platform Team', 'skills': 'Go, Docker'},
        {'title': 'Mobile App Development', 'skills': 'React Native, Flutter'},
    ]

    for job in test_jobs:
        skip, reason = should_skip_job(job)
        status = f"SKIP ({reason})" if skip else "KEEP"
        print(f"  {status}: {job['title']}")

    # Test email generation (requires Gemini key)
    print("\n=== Email Draft Test ===")
    try:
        test_contacts = {
            'emails': ['dave@techvault.io'],
            'summary': 'TechVault Solutions, B2B SaaS company in Austin TX',
        }
        test_job = {
            'title': 'React Dashboard for SaaS Platform',
            'budget': '$5,000 - $10,000',
            'skills': 'React, TypeScript, Chart.js',
            'description': 'We need a modern analytics dashboard built with React.',
        }
        result = generate_email_draft(test_job, test_contacts)
        print(json.dumps(result, indent=2))
    except Exception as e:
        print(f"  Email draft test error: {e}")
