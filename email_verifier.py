"""
Email Verifier - Verifies email addresses before sending outreach.

Verification pipeline (per email):
1. Syntax check: basic regex validation
2. Domain MX check: DNS lookup to confirm the domain can receive email
3. Disposable/free provider filter: flags generic free/throwaway providers

Returns a dict per email with status: 'valid', 'invalid', 'risky', or 'unknown'
Only 'valid' emails make it through to the outreach mailer.

Can be tested standalone:
    python email_verifier.py
"""

import re

import dns.resolver
from colorama import Fore, Style

# Free / disposable providers - emails from these are flagged 'risky'
RISKY_DOMAINS = {
    'yahoo.com', 'hotmail.com', 'outlook.com', 'icloud.com',
    'protonmail.com', 'aol.com', 'live.com', 'msn.com', 'me.com',
    'mailinator.com', 'guerrillamail.com', 'temp-mail.org', 'throwaway.email',
    'yopmail.com', 'sharklasers.com', 'dispostable.com', 'maildrop.cc',
    'trashmail.com', 'fakeinbox.com',
}

EMAIL_REGEX = re.compile(
    r'^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,10}$'
)


def _check_syntax(email: str) -> bool:
    """Check if email has valid syntax."""
    return bool(EMAIL_REGEX.match(email.strip()))


def _get_mx_records(domain: str) -> list[str]:
    """
    Look up MX records for a domain.
    Returns sorted list of mail server hostnames, or [] if none found.
    """
    try:
        records = dns.resolver.resolve(domain, 'MX', lifetime=8)
        sorted_records = sorted(records, key=lambda r: r.preference)
        return [str(r.exchange).rstrip('.') for r in sorted_records]
    except Exception:
        return []


def verify_email(email: str) -> dict:
    """
    Fully verify a single email address.

    Args:
        email: The email address to verify

    Returns:
        {
            'email':   str,
            'status':  'valid' | 'invalid' | 'risky' | 'unknown',
            'reason':  str,
            'mx':      list[str]
        }
    """
    email = email.strip().lower()
    result = {'email': email, 'status': 'unknown', 'reason': '', 'mx': []}

    if not _check_syntax(email):
        result.update(status='invalid', reason='Invalid email syntax')
        return result

    domain = email.split('@')[-1]

    if domain in RISKY_DOMAINS:
        result.update(
            status='risky',
            reason=f"Free/personal email provider ({domain}) - low deliverability for cold outreach"
        )
        return result

    mx_records = _get_mx_records(domain)
    result['mx'] = mx_records

    if not mx_records:
        result.update(status='invalid', reason=f"No MX records found for domain '{domain}'")
        return result

    result.update(status='valid', reason='Syntax and MX records verified')

    return result


def verify_emails(emails: list[str]) -> dict:
    """
    Verify a list of emails and categorise them.

    Args:
        emails: List of email addresses

    Returns:
        {
            'verified':  list[str],
            'risky':     list[str],
            'invalid':   list[str],
            'unknown':   list[str],
            'details':   list[dict]
        }
    """
    if not emails:
        return {'verified': [], 'risky': [], 'invalid': [], 'unknown': [], 'details': []}

    print(f"\n{Fore.CYAN}  Email Verifier: Checking {len(emails)} address(es)...{Style.RESET_ALL}")

    details = []
    verified, risky, invalid, unknown = [], [], [], []

    for email in emails:
        result = verify_email(email)
        details.append(result)

        status = result['status']
        icon = {'valid': 'OK', 'invalid': 'X', 'risky': '!', 'unknown': '?'}.get(status, '?')
        colour = {
            'valid':   Fore.GREEN,
            'invalid': Fore.RED,
            'risky':   Fore.YELLOW,
            'unknown': Fore.WHITE,
        }.get(status, Fore.WHITE)

        print(f"  {colour}{icon} {email:<40} [{status.upper()}] {result['reason']}{Style.RESET_ALL}")

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
        'risky':    risky,
        'invalid':  invalid,
        'unknown':  unknown,
        'details':  details,
    }


def _is_business_unknown(detail: dict) -> bool:
    """Return True when an inconclusive email still looks like a business mailbox."""
    if not detail:
        return False
    email = (detail.get('email') or '').strip().lower()
    if '@' not in email:
        return False
    domain = email.split('@')[-1]
    if domain in RISKY_DOMAINS:
        return False
    if not detail.get('mx'):
        return False
    status = (detail.get('status') or '').lower()
    return status == 'unknown'


def get_sendable_emails(
    verification_result: dict,
    include_risky: bool = False,
    include_unknown_business: bool = False,
) -> list[str]:
    """
    Get the list of emails safe to send to from a verify_emails() result.

    Args:
        verification_result: Output of verify_emails()
        include_risky: If True, also include 'risky' (free provider) emails.
        include_unknown_business: If True, include inconclusive business emails
                                  when MX records exist but SMTP probing was not
                                  definitive.

    Returns:
        List of email addresses to send to.
    """
    sendable = list(verification_result['verified'])
    if include_risky:
        sendable.extend(verification_result['risky'])

    if include_unknown_business:
        details = verification_result.get('details', []) or []
        for detail in details:
            if _is_business_unknown(detail):
                sendable.append(detail['email'])

    deduped = []
    seen = set()
    for email in sendable:
        key = email.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(email)
    return deduped


if __name__ == '__main__':
    from colorama import init as colorama_init

    colorama_init()

    print(f"\n{Fore.CYAN}{Style.BRIGHT}=== Email Verifier - Standalone Test ==={Style.RESET_ALL}\n")

    print(f"{Style.BRIGHT}[Test 1] Syntax & MX check - mixed dummy emails{Style.RESET_ALL}")
    dummy_emails = [
        'not-an-email',
        'test@nonexistentdomain12345.io',
        'example@gmail.com',
        'john@outlook.com',
    ]
    result = verify_emails(dummy_emails)
    print(f"\n  Sendable (no risky): {get_sendable_emails(result, include_risky=False)}")
    print(f"  Sendable (with risky): {get_sendable_emails(result, include_risky=True)}")

    print(f"\n{Style.BRIGHT}[Test 2] Full verification with syntax + MX only{Style.RESET_ALL}")
    real_emails = [
        'support@google.com',
        'nope@google.com',
    ]
    result2 = verify_emails(real_emails)
    print(f"\n  Sendable: {get_sendable_emails(result2)}")
