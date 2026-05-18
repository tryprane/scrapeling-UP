"""
Prane message sender client.

Mirrors the existing Prane email sender pattern but targets the
/api/v1/whatsapp/send endpoint provided by the user's backend.
"""

from __future__ import annotations

import json
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from config import config


def send_message(to_phone: str, message: str) -> dict:
    """
    Send a single message through the Prane API.

    Returns a normalized status payload suitable for dashboard logging.
    """
    normalized_phone = re.sub(r"\D", "", str(to_phone or ""))
    if not normalized_phone:
        return {
            "to_phone": to_phone,
            "status": "failed",
            "error": "No valid phone digits found",
        }

    base_url = config.get("prane_base_url", "https://prane.one").rstrip("/")
    api_key = config.get("prane_api_key", "")
    if not api_key:
        return {
            "to_phone": to_phone,
            "status": "failed",
            "error": "PRANE_API_KEY / OUTBOUND_LIVE_API_KEY is not set",
        }

    payload = json.dumps({"toPhone": normalized_phone, "message": message}).encode("utf-8")
    request = Request(
        f"{base_url}/api/v1/whatsapp/send",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
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
                "to_phone": to_phone,
                "status": "sent",
                "response": parsed,
                "error": "",
            }
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if hasattr(exc, "read") else ""
        return {
            "to_phone": to_phone,
            "status": "failed",
            "error": f"HTTP {exc.code}: {body or exc.reason}",
        }
    except URLError as exc:
        return {
            "to_phone": to_phone,
            "status": "failed",
            "error": str(exc.reason),
        }
    except Exception as exc:
        return {
            "to_phone": to_phone,
            "status": "failed",
            "error": str(exc),
        }
