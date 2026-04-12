"""
Outreach Mailer — Generates email drafts and sends via Gmail API.

Handles:
1. Job filtering with conservative skip rules
2. Email draft generation using Gemini AI (as Saimands Roy from Femur Studio)
3. Sending emails individually via Gmail API (Google Cloud)

Gmail API Setup:
  1. Enable Gmail API in Google Cloud Console
  2. Create OAuth2 credentials (Desktop app) → download credentials.json
  3. Place credentials.json in the same directory as this file
  4. On first run a browser window opens for one-time authorization
  5. token.json is saved automatically for subsequent runs

Can be tested standalone:
    python -c "from outreach_mailer import should_skip_job; print(should_skip_job({'title':'WordPress Developer','skills':'WordPress'}))"
"""

import json
import os
import base64
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from google import genai
from google.genai import types
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from colorama import Fore, Style

from config import config

_genai_client = None
_gmail_service = None

# ── Gmail API OAuth scopes ────────────────────────────────────────────

GMAIL_SCOPES = ['https://www.googleapis.com/auth/gmail.send']

# Paths for OAuth token / credentials files (same folder as this script)
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_FILE = os.path.join(_BASE_DIR, 'credentials.json')
TOKEN_FILE       = os.path.join(_BASE_DIR, 'token.json')

# ── Job filter keywords ──────────────────────────────────────────────

SKIP_TITLE_KEYWORDS = []

SKIP_SKILL_KEYWORDS = []

# Never skip WordPress leads. We want to keep these available for review
# because real client opportunities often come through WordPress posts.
NEVER_SKIP_KEYWORDS = ['wordpress']

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


def _get_genai_client():
    """Get or create the Gemini client instance."""
    global _genai_client
    if _genai_client:
        return _genai_client

    if not config['gemini_api_key']:
        raise RuntimeError('GEMINI_API_KEY is not set — cannot generate emails.')

    _genai_client = genai.Client(api_key=config['gemini_api_key'])
    return _genai_client


def _get_gmail_service():
    """
    Get or create an authorized Gmail API service.

    Uses OAuth2 credentials.json (Desktop app type) from Google Cloud Console.
    Saves the refresh token to token.json for reuse.
    """
    global _gmail_service
    if _gmail_service:
        return _gmail_service

    if not os.path.exists(CREDENTIALS_FILE):
        raise FileNotFoundError(
            f'credentials.json not found at {CREDENTIALS_FILE}\n'
            'Please download OAuth2 credentials from Google Cloud Console and place them there.\n'
            'See the Gmail API Setup guide.'
        )

    creds = None

    # Load existing token
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, GMAIL_SCOPES)

    # Refresh or re-authorize if needed
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        # Save for next run
        with open(TOKEN_FILE, 'w') as f:
            f.write(creds.to_json())

    _gmail_service = build('gmail', 'v1', credentials=creds)
    return _gmail_service


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

    for kw in NEVER_SKIP_KEYWORDS:
        if kw in title or kw in skills:
            return (False, '')

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

    client = _get_genai_client()

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
                response_mime_type="application/json",
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

        print(f"{Fore.GREEN}  + Email draft generated: \"{result['subject']}\"{Style.RESET_ALL}")
        print(f"{Style.DIM}    To: {', '.join(emails)}{Style.RESET_ALL}")

        return result

    except Exception as e:
        print(f"{Fore.RED}  - Email draft generation error: {e}{Style.RESET_ALL}")
        return {
            'subject': '',
            'body': '',
            'to_emails': emails,
            'error': str(e),
        }


def send_via_gmail(email_data: dict, sender_email: str = None) -> list:
    """
    Send emails individually via Gmail API.

    Each recipient gets a separate email.

    Args:
        email_data: Dict with 'subject', 'body', 'to_emails'
        sender_email: The Gmail address to send from (defaults to config value).
                      Use 'me' to let Gmail resolve from the authorized account.

    Returns:
        List of dicts: [{'email': str, 'status': 'sent'|'failed', 'error': str}]
    """
    results = []
    subject = email_data.get('subject', '')
    body    = email_data.get('body', '')
    emails  = email_data.get('to_emails', [])

    if not emails:
        return []

    # sender_email can be 'me' (Gmail resolves it) or an explicit address
    from_addr = sender_email or config.get('gmail_sender', 'me')

    try:
        service = _get_gmail_service()
    except Exception as e:
        print(f"{Fore.RED}  - Gmail service error: {e}{Style.RESET_ALL}")
        return [{'email': e_addr, 'status': 'failed', 'error': str(e)} for e_addr in emails]

    for email_addr in emails:
        try:
            print(f"{Style.DIM}  > Sending email to {email_addr} via Gmail...{Style.RESET_ALL}")

            msg = MIMEMultipart('alternative')
            msg['Subject'] = subject
            msg['From']    = from_addr
            msg['To']      = email_addr
            msg.attach(MIMEText(body, 'plain'))

            raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
            send_body = {'raw': raw}

            service.users().messages().send(userId='me', body=send_body).execute()

            print(f"{Fore.GREEN}  + Email sent to {email_addr}{Style.RESET_ALL}")
            results.append({'email': email_addr, 'status': 'sent', 'error': ''})

        except Exception as e:
            print(f"{Fore.RED}  - Gmail send error for {email_addr}: {e}{Style.RESET_ALL}")
            results.append({'email': email_addr, 'status': 'failed', 'error': str(e)})

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
