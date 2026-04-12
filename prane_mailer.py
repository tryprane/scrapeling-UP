"""
Outbound email sender for the Prane / outbound_live API.
"""

from __future__ import annotations

import json
import os
from html import escape
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from config import config


def plain_text_to_html(body: str) -> str:
    """Convert a plain-text email draft into simple HTML."""
    body = (body or "").strip()
    if not body:
        return "<p></p>"

    paragraphs = []
    for chunk in body.split("\n\n"):
        chunk = chunk.strip()
        if not chunk:
            continue
        paragraphs.append("<p>" + "<br>".join(escape(line) for line in chunk.splitlines()) + "</p>")
    return "\n".join(paragraphs) if paragraphs else f"<p>{escape(body)}</p>"


def send_email(to: str, subject: str, html: str) -> dict:
    """
    Send a single email through the outbound API.

    Returns a dict with status and raw response data for dashboard logging.
    """
    base_url = config.get("prane_base_url", "https://prane.one").rstrip("/")
    api_key = config.get("prane_api_key", "")
    if not api_key:
        return {"to": to, "status": "failed", "error": "PRANE_API_KEY / OUTBOUND_LIVE_API_KEY is not set"}

    payload = json.dumps({"to": to, "subject": subject, "html": html}).encode("utf-8")
    request = Request(
        f"{base_url}/api/v1/email/send",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Origin": base_url,
            "Referer": f"{base_url}/",
        },
    )

    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(raw) if raw else {}
            except Exception:
                parsed = {"raw": raw}
            return {
                "to": to,
                "status": "sent",
                "response": parsed,
                "error": "",
            }
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if hasattr(exc, "read") else ""
        return {
            "to": to,
            "status": "failed",
            "error": f"HTTP {exc.code}: {body or exc.reason}",
        }
    except URLError as exc:
        return {
            "to": to,
            "status": "failed",
            "error": str(exc.reason),
        }
    except Exception as exc:
        return {
            "to": to,
            "status": "failed",
            "error": str(exc),
        }


def send_batch(emails: list[str], subject: str, html: str) -> list[dict]:
    """Send the same email to multiple recipients one-by-one."""
    results = []
    for email in emails:
        results.append(send_email(email, subject, html))
    return results
