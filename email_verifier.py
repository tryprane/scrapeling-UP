"""
Email Verifier — Verifies email addresses before sending outreach.

Verification pipeline (per email):
1. Syntax check: basic regex validation
2. Domain MX check: DNS lookup to confirm the domain can receive email
3. SMTP probe: connects to the mail server and checks if the mailbox exists
   (without actually sending — uses RCPT TO handshake)
4. Disposable/free provider filter: flags generic free/throwaway providers

Returns a dict per email with status: 'valid', 'invalid', 'risky', or 'unknown'
Only 'valid' emails make it through to the outreach mailer.

Can be tested standalone:
    python email_verifier.py
"""

import re
import socket
import smtplib
import dns.resolver
from colorama import Fore, Style

# ── Config ────────────────────────────────────────────────────────────

SMTP_TIMEOUT = 8          # seconds per SMTP connection attempt
SMTP_FROM    = 'verify@example.com'   # envelope sender for the probe

# Free / disposable providers — emails from these are flagged 'risky'
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


# ── Core verification functions ───────────────────────────────────────

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
        # Sort by preference (lower = higher priority)
        sorted_records = sorted(records, key=lambda r: r.preference)
        return [str(r.exchange).rstrip('.') for r in sorted_records]
    except Exception:
        return []


def _smtp_probe(mx_server: str, email: str) -> tuple[str, str]:
    """
    Connect to the mail server and probe whether the mailbox exists,
    without sending any actual email.

    Returns:
        ('valid',   '') if the server accepted RCPT TO
        ('invalid', reason) if the server rejected it with a 5xx code
        ('unknown', reason) if we couldn't determine (graylisting, connection error, etc.)
    """
    try:
        smtp = smtplib.SMTP(timeout=SMTP_TIMEOUT)
        smtp.connect(mx_server, 25)
        smtp.helo('verify.example.com')
        smtp.mail(SMTP_FROM)
        code, message = smtp.rcpt(email)
        smtp.quit()

        msg = message.decode('utf-8', errors='replace') if isinstance(message, bytes) else str(message)

        if code == 250:
            return ('valid', '')
        elif code >= 500 and code < 600:
            return ('invalid', f"SMTP {code}: {msg[:80]}")
        else:
            # 4xx = temporary rejection (greylisting, rate limit)
            return ('unknown', f"SMTP {code}: {msg[:80]}")

    except smtplib.SMTPConnectError as e:
        return ('unknown', f"SMTP connect error: {e}")
    except smtplib.SMTPServerDisconnected:
        return ('unknown', 'Server disconnected early')
    except socket.timeout:
        return ('unknown', 'Connection timed out')
    except ConnectionRefusedError:
        return ('unknown', 'Connection refused on port 25')
    except Exception as e:
        return ('unknown', str(e)[:80])


def verify_email(email: str, do_smtp_probe: bool = True) -> dict:
    """
    Fully verify a single email address.

    Args:
        email: The email address to verify
        do_smtp_probe: If True, do SMTP handshake (slower but more accurate).
                       Set False to only check syntax + DNS.

    Returns:
        {
            'email':   str,
            'status':  'valid' | 'invalid' | 'risky' | 'unknown',
            'reason':  str,      # human-readable explanation
            'mx':      list[str] # MX records found
        }
    """
    email = email.strip().lower()
    result = {'email': email, 'status': 'unknown', 'reason': '', 'mx': []}

    # Step 1: Syntax
    if not _check_syntax(email):
        result.update(status='invalid', reason='Invalid email syntax')
        return result

    domain = email.split('@')[-1]

    # Step 2: Risky / free provider flag
    if domain in RISKY_DOMAINS:
        result.update(
            status='risky',
            reason=f"Free/personal email provider ({domain}) — low deliverability for cold outreach"
        )
        return result

    # Step 3: MX record lookup
    mx_records = _get_mx_records(domain)
    result['mx'] = mx_records

    if not mx_records:
        result.update(status='invalid', reason=f"No MX records found for domain '{domain}'")
        return result

    # Step 4: SMTP probe against the primary MX
    if do_smtp_probe:
        smtp_status, smtp_reason = _smtp_probe(mx_records[0], email)
        if smtp_status == 'valid':
            result.update(status='valid', reason='MX records found + SMTP accepted')
        elif smtp_status == 'invalid':
            result.update(status='invalid', reason=smtp_reason)
        else:
            # SMTP did not clearly accept the mailbox, so do not treat it as sendable.
            # This is stricter, but it avoids a lot of "mail not found" bounces.
            result.update(
                status='unknown',
                reason=f"MX OK, but SMTP inconclusive: {smtp_reason}"
            )
    else:
        result.update(status='valid', reason='MX records verified (no SMTP probe)')

    return result


def verify_emails(emails: list[str], do_smtp_probe: bool = True) -> dict:
    """
    Verify a list of emails and categorise them.

    Args:
        emails: List of email addresses
        do_smtp_probe: Whether to do SMTP handshake verification

    Returns:
        {
            'verified':  list[str],   # safe to send — 'valid' status
            'risky':     list[str],   # free providers, might still work
            'invalid':   list[str],   # do not send
            'unknown':   list[str],   # inconclusive (shouldn't happen after probe)
            'details':   list[dict]   # per-email verification result
        }
    """
    if not emails:
        return {'verified': [], 'risky': [], 'invalid': [], 'unknown': [], 'details': []}

    print(f"\n{Fore.CYAN}  🔍 Email Verifier: Checking {len(emails)} address(es)...{Style.RESET_ALL}")

    details = []
    verified, risky, invalid, unknown = [], [], [], []

    for email in emails:
        result = verify_email(email, do_smtp_probe=do_smtp_probe)
        details.append(result)

        status = result['status']
        icon = {'valid': '✓', 'invalid': '✗', 'risky': '⚡', 'unknown': '?'}.get(status, '?')
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

    # Summary
    print(f"\n  {Fore.GREEN}✓ Valid:   {len(verified)}{Style.RESET_ALL}  "
          f"{Fore.YELLOW}⚡ Risky: {len(risky)}{Style.RESET_ALL}  "
          f"{Fore.RED}✗ Invalid: {len(invalid)}{Style.RESET_ALL}")

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
                       Default False — only send to verified business emails.
        include_unknown_business: If True, include inconclusive business emails
                                  when MX records exist but SMTP probing was not
                                  definitive. Useful when outbound SMTP probing
                                  is blocked by the VPS/network.

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

    # Preserve order while deduplicating.
    deduped = []
    seen = set()
    for email in sendable:
        key = email.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(email)
    return deduped


# ── Standalone test ──────────────────────────────────────────────────

if __name__ == '__main__':
    from colorama import init as colorama_init
    colorama_init()

    print(f"\n{Fore.CYAN}{Style.BRIGHT}=== Email Verifier — Standalone Test ==={Style.RESET_ALL}\n")

    # ── Test 1: Syntax + Dummy data (no SMTP probe) ────────────────────
    print(f"{Style.BRIGHT}[Test 1] Syntax & MX check (no SMTP probe) — mixed dummy emails{Style.RESET_ALL}")
    dummy_emails = [
        'not-an-email',                   # bad syntax
        'test@nonexistentdomain12345.io',  # valid syntax, no MX
        'contact@gmail.com',              # risky (free provider)
        'support@femur.studio',           # real business email (MX check)
        'info@github.com',               # known valid domain
        'ceo@fakecorp99999.xyz',          # probably no MX
    ]

    result = verify_emails(dummy_emails, do_smtp_probe=False)
    print(f"\n  Sendable (no risky): {get_sendable_emails(result, include_risky=False)}")
    print(f"  Sendable (with risky): {get_sendable_emails(result, include_risky=True)}")

    # ── Test 2: Full SMTP probe on real-world emails ───────────────────
    print(f"\n{Style.BRIGHT}[Test 2] Full SMTP probe — real email addresses{Style.RESET_ALL}")
    print(f"{Style.DIM}  Note: SMTP port 25 may be blocked on some networks.{Style.RESET_ALL}")
    real_emails = [
        'support@github.com',       # almost certainly valid
        'hr@microsoft.com',         # large domain, valid MX but might block probe
        'fake.user.12345@adobe.com',  # valid domain but mailbox shouldn't exist
    ]

    result2 = verify_emails(real_emails, do_smtp_probe=True)
    print(f"\n  Sendable: {get_sendable_emails(result2)}")

    print(f"\n{Fore.GREEN}{Style.BRIGHT}=== Tests Complete ==={Style.RESET_ALL}")
