"""
QuickEmailVerification helpers.

This module supports:
- multiple API keys
- per-email retries across keys
- fallback detection when the API path is unavailable
"""

from __future__ import annotations

import argparse
import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

from colorama import Fore, Style, init as colorama_init

QUICKEMAILVERIFICATION_API_URL = "https://api.quickemailverification.com/v1/verify"


def _normalize_api_result(email: str, payload: dict[str, Any]) -> dict[str, Any]:
    verdict = str(payload.get("result", "")).strip().lower()
    reason = str(payload.get("reason", "")).strip()
    safe_to_send = str(payload.get("safe_to_send", "")).strip().lower()
    did_you_mean = str(payload.get("did_you_mean", "")).strip()
    disposable = str(payload.get("disposable", "")).strip().lower()
    accept_all = str(payload.get("accept_all", "")).strip().lower()
    mx_records = payload.get("mx_records") or payload.get("mx") or []

    if isinstance(mx_records, str):
        mx = [mx_records] if mx_records else []
    elif isinstance(mx_records, list):
        mx = [str(item).strip() for item in mx_records if str(item).strip()]
    else:
        mx = []

    status = "unknown"
    detail_parts = []

    if verdict == "valid":
        status = "valid"
    elif verdict in {"invalid", "unknown"}:
        status = "invalid" if verdict == "invalid" else "unknown"
    elif safe_to_send == "true":
        status = "valid"

    if disposable == "true":
        status = "risky"
        detail_parts.append("Disposable mailbox")
    if accept_all == "true":
        status = "unknown"
        detail_parts.append("Catch-all domain")
    if reason:
        detail_parts.append(reason)
    if did_you_mean:
        detail_parts.append(f"did you mean {did_you_mean}")

    summary = ", ".join(detail_parts) if detail_parts else verdict or "unverifiable"

    return {
        "email": email.lower(),
        "status": status,
        "reason": f"QuickEmailVerification API: {summary}",
        "mx": mx,
    }


def verify_email_via_api(email: str, key: str, timeout: int = 30) -> dict[str, Any]:
    """Verify one email via the QuickEmailVerification REST API."""
    email = email.strip().lower()
    params = urlencode({"email": email, "apikey": key.strip()})
    uri = f"{QUICKEMAILVERIFICATION_API_URL}?{params}"

    try:
        with urlopen(uri, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace") if hasattr(exc, "read") else ""
        raise RuntimeError(f"API HTTP {exc.code}: {detail or exc.reason}") from exc
    except URLError as exc:
        raise RuntimeError(f"API network error: {exc.reason}") from exc
    except Exception as exc:
        raise RuntimeError(f"API request failed: {exc}") from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"API returned non-JSON payload: {raw[:200]}") from exc

    if not isinstance(payload, dict) or not payload.get("email"):
        raise RuntimeError(f"API returned unusable payload: {payload}")

    return _normalize_api_result(email, payload)


def _is_retryable_api_error(error_text: str) -> bool:
    """Return True when another API key should be attempted."""
    lowered = (error_text or "").lower()
    retry_tokens = [
        "429",
        "401",
        "403",
        "quota",
        "credit",
        "rate limit",
        "rate-limit",
        "too many",
        "temporarily",
        "network",
        "timeout",
        "unavailable",
        "apikey",
        "invalid api key",
    ]
    return any(token in lowered for token in retry_tokens)


def verify_email_via_api_keys(email: str, keys: list[str], timeout: int = 30) -> tuple[dict[str, Any] | None, list[str]]:
    """Try an email against multiple API keys until one succeeds."""
    errors: list[str] = []
    for index, raw_key in enumerate(keys, start=1):
        key = (raw_key or "").strip()
        if not key:
            continue
        try:
            result = verify_email_via_api(email, key, timeout=timeout)
            result["verification_key_index"] = index
            return result, errors
        except Exception as exc:
            message = str(exc)
            errors.append(message)
            if not _is_retryable_api_error(message):
                break
    return None, errors


def verify_emails_via_api_keys(emails: list[str], keys: list[str], timeout: int = 30) -> dict[str, Any]:
    """
    Verify a list of emails via QuickEmailVerification using multiple keys.

    Emails whose API verification fails for operational reasons are returned in
    `unverified_due_to_api` for downstream fallback.
    """
    clean_keys = [key.strip() for key in keys if key and key.strip()]
    if not clean_keys:
        return {
            "source": "quickemailverification_api",
            "verified": [],
            "risky": [],
            "invalid": [],
            "unknown": [],
            "details": [],
            "unverified_due_to_api": list(dict.fromkeys(email.strip().lower() for email in emails if email.strip())),
            "api_errors": ["No QuickEmailVerification API keys configured"],
        }

    verified: list[str] = []
    risky: list[str] = []
    invalid: list[str] = []
    unknown: list[str] = []
    details: list[dict[str, Any]] = []
    api_errors: list[str] = []
    unverified_due_to_api: list[str] = []

    for raw_email in emails:
        email = (raw_email or "").strip().lower()
        if not email:
            continue
        result, errors = verify_email_via_api_keys(email, clean_keys, timeout=timeout)
        if result is None:
            unverified_due_to_api.append(email)
            api_errors.extend(errors)
            details.append(
                {
                    "email": email,
                    "status": "unknown",
                    "reason": f"QuickEmailVerification API unavailable: {' | '.join(errors) if errors else 'unknown error'}",
                    "mx": [],
                }
            )
            unknown.append(email)
            continue

        details.append(result)
        status = result.get("status", "unknown")
        if status == "valid":
            verified.append(email)
        elif status == "risky":
            risky.append(email)
        elif status == "invalid":
            invalid.append(email)
        else:
            unknown.append(email)

    return {
        "source": "quickemailverification_api",
        "verified": verified,
        "risky": risky,
        "invalid": invalid,
        "unknown": unknown,
        "details": details,
        "unverified_due_to_api": unverified_due_to_api,
        "api_errors": api_errors,
    }


def _print_summary(summary: dict[str, Any]) -> None:
    source = summary.get("source", "unknown")
    print(f"\n{Fore.CYAN}{Style.BRIGHT}=== Quick Mail Verification ==={Style.RESET_ALL}")
    print(f"Source: {source}")
    for item in summary.get("details", []) or []:
        print(
            f"Email: {item.get('email', '')}\n"
            f"Status: {item.get('status', 'unknown')}\n"
            f"Reason: {item.get('reason', '')}\n"
            f"MX: {item.get('mx', [])}\n"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Quick email verification via API.")
    parser.add_argument("email", help="Email address to verify")
    parser.add_argument("--key", action="append", required=True, help="QuickEmailVerification API key (repeatable)")
    args = parser.parse_args()

    colorama_init()
    summary = verify_emails_via_api_keys([args.email], args.key)
    _print_summary(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
