"""
Local dashboard for upload, draft generation, approval, quota planning, and
scheduled daytime delivery through the Prane messaging API.

Important safety behavior:
- imports contacts from CSV/XLSX
- generates draft messages automatically
- requires per-lead approval before queueing for send
- enforces a per-campaign daily cap and India daytime window
"""

from __future__ import annotations

import csv
import io
import json
import math
import os
import random
import re
import sqlite3
import sys
import threading
import time
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.parser import BytesParser
from email.policy import default
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socket import gethostbyname, gethostname
from typing import Any
from urllib.parse import parse_qs, urlparse

from zoneinfo import ZoneInfo

from config import config
from llm_client import describe_exception, get_client, get_model_name, parse_json_response_text
from prane_whatsapp_sender import send_message

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "prane_whatsapp.db"
UPLOAD_DIR = APP_DIR / "prane_whatsapp_uploads"
LOG_PATH = APP_DIR / "prane_whatsapp.log"
TZ = ZoneInfo("Asia/Kolkata")
DEFAULT_DAILY_CAP = 90
DEFAULT_WINDOW_START = 8
DEFAULT_WINDOW_END = 24
PRE_DRAFT_LIMIT = 3
ONLINE_STORE_EXAMPLES = (
    "https://www.sweedesi.com/",
    "https://www.hopedesignltd.com/",
    "https://krmscale.com/",
    "https://store.ncmec.org/",
    "https://e-store.viewqwest.com/",
)
UPLOAD_DIR.mkdir(exist_ok=True)

WRITE_LOCK = threading.Lock()
SCHEDULE_LOCK = threading.Lock()
WORKERS_STARTED = False


def now_ist() -> datetime:
    return datetime.now(TZ)


def utc_now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False)


def app_log(event: str, **fields: Any) -> None:
    payload = {
        "timestamp": now_ist().strftime("%Y-%m-%d %H:%M:%S"),
        "event": event,
        **fields,
    }
    line = json.dumps(payload, ensure_ascii=False)
    with WRITE_LOCK:
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def get_local_ip() -> str:
    try:
        return gethostbyname(gethostname())
    except Exception:
        return "127.0.0.1"


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS campaigns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            prompt TEXT NOT NULL,
            source_filename TEXT NOT NULL,
            source_path TEXT NOT NULL,
            source_kind TEXT NOT NULL,
            uploaded_at TEXT NOT NULL,
            daily_cap INTEGER NOT NULL DEFAULT 90,
            window_start_hour INTEGER NOT NULL DEFAULT 8,
            window_end_hour INTEGER NOT NULL DEFAULT 24,
            timezone TEXT NOT NULL DEFAULT 'Asia/Kolkata',
            total_rows INTEGER NOT NULL DEFAULT 0,
            valid_rows INTEGER NOT NULL DEFAULT 0,
            invalid_rows INTEGER NOT NULL DEFAULT 0,
            duplicate_rows INTEGER NOT NULL DEFAULT 0,
            require_approval INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER NOT NULL,
            row_number INTEGER NOT NULL,
            raw_row_json TEXT NOT NULL,
            row_summary_json TEXT NOT NULL DEFAULT '{}',
            display_name TEXT NOT NULL DEFAULT '',
            phone_raw TEXT NOT NULL DEFAULT '',
            phone_e164 TEXT NOT NULL DEFAULT '',
            phone_valid INTEGER NOT NULL DEFAULT 0,
            invalid_reason TEXT NOT NULL DEFAULT '',
            duplicate_of_lead_id INTEGER,
            duplicate_reason TEXT NOT NULL DEFAULT '',
            draft_status TEXT NOT NULL DEFAULT 'pending',
            draft_message TEXT NOT NULL DEFAULT '',
            draft_error TEXT NOT NULL DEFAULT '',
            personalization_json TEXT NOT NULL DEFAULT '{}',
            approval_status TEXT NOT NULL DEFAULT 'needs_review',
            queue_status TEXT NOT NULL DEFAULT 'not_queued',
            scheduled_at TEXT NOT NULL DEFAULT '',
            sent_at TEXT NOT NULL DEFAULT '',
            api_response_json TEXT NOT NULL DEFAULT '{}',
            send_error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (campaign_id) REFERENCES campaigns(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS send_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER NOT NULL,
            lead_id INTEGER,
            event_type TEXT NOT NULL,
            status TEXT NOT NULL,
            details_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


def log_send_event(
    conn: sqlite3.Connection,
    *,
    campaign_id: int,
    lead_id: int | None,
    event_type: str,
    status: str,
    details: dict[str, Any],
) -> None:
    conn.execute(
        """
        INSERT INTO send_logs (campaign_id, lead_id, event_type, status, details_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (campaign_id, lead_id, event_type, status, json_dumps(details), utc_now_iso()),
    )


def column_letters_to_index(value: str) -> int:
    result = 0
    for char in value:
        if not char.isalpha():
            break
        result = result * 26 + (ord(char.upper()) - 64)
    return max(result - 1, 0)


def parse_csv_bytes(payload: bytes) -> list[dict[str, str]]:
    text = payload.decode("utf-8-sig", errors="replace")
    rows = list(csv.DictReader(io.StringIO(text)))
    return [{str(k or "").strip(): str(v or "").strip() for k, v in row.items()} for row in rows]


def parse_xlsx_bytes(payload: bytes) -> list[dict[str, str]]:
    ns = {
        "a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
        "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    }
    shared_strings: list[str] = []
    rows: list[dict[str, str]] = []

    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = set(archive.namelist())
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in root.findall("a:si", ns):
                shared_strings.append("".join(text.text or "" for text in item.iterfind(".//a:t", ns)))

        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        rel_map = {rel.attrib["Id"]: rel.attrib["Target"] for rel in relationships}
        first_sheet = workbook.find("a:sheets/a:sheet", ns)
        if first_sheet is None:
            return []

        target = rel_map[first_sheet.attrib["{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"]]
        target = target.lstrip("/")
        if not target.startswith("xl/"):
            target = f"xl/{target}"

        sheet = ET.fromstring(archive.read(target))
        matrix: list[list[str]] = []

        for row in sheet.findall("a:sheetData/a:row", ns):
            values: list[str] = []
            max_index = -1
            for cell in row.findall("a:c", ns):
                ref = cell.attrib.get("r", "")
                idx = column_letters_to_index(ref)
                if idx > max_index:
                    max_index = idx
                while len(values) <= idx:
                    values.append("")
                cell_type = cell.attrib.get("t")
                if cell_type == "s":
                    value = cell.find("a:v", ns)
                    values[idx] = shared_strings[int(value.text)] if value is not None and value.text else ""
                elif cell_type == "inlineStr":
                    values[idx] = "".join(text.text or "" for text in cell.iterfind(".//a:t", ns))
                else:
                    value = cell.find("a:v", ns)
                    values[idx] = value.text if value is not None and value.text is not None else ""
            if max_index >= 0:
                matrix.append([str(item).strip() for item in values])

        if not matrix:
            return []

        headers = [str(item or "").strip() for item in matrix[0]]
        for data_row in matrix[1:]:
            normalized = {}
            for index, header in enumerate(headers):
                key = header or f"Column {index + 1}"
                normalized[key] = str(data_row[index]).strip() if index < len(data_row) else ""
            rows.append(normalized)
    return rows


def read_rows_from_upload(filename: str, payload: bytes) -> tuple[str, list[dict[str, str]]]:
    lower = filename.lower()
    if lower.endswith(".csv"):
        return ("csv", parse_csv_bytes(payload))
    if lower.endswith(".xlsx"):
        return ("xlsx", parse_xlsx_bytes(payload))
    raise ValueError("Only .csv and .xlsx files are supported.")


def parse_multipart_form(content_type: str, payload: bytes) -> tuple[dict[str, str], dict[str, Any] | None]:
    """Parse multipart/form-data without the removed cgi module."""
    if "multipart/form-data" not in (content_type or "").lower():
        raise ValueError("Content-Type must be multipart/form-data.")

    raw = (
        f"Content-Type: {content_type}\r\n"
        "MIME-Version: 1.0\r\n"
        "\r\n"
    ).encode("utf-8") + payload
    message = BytesParser(policy=default).parsebytes(raw)

    fields: dict[str, str] = {}
    uploaded: dict[str, Any] | None = None

    for part in message.iter_parts():
        if part.get_content_disposition() != "form-data":
            continue
        field_name = part.get_param("name", header="Content-Disposition")
        if not field_name:
            continue

        filename = part.get_filename()
        part_payload = part.get_payload(decode=True) or b""
        if filename:
            uploaded = {
                "filename": filename,
                "payload": part_payload,
                "content_type": part.get_content_type(),
            }
            continue

        charset = part.get_content_charset() or "utf-8"
        fields[field_name] = part_payload.decode(charset, errors="replace")

    return fields, uploaded


PHONE_HINTS = (
    "whatsapp number",
    "whatsapp",
    "phone",
    "mobile",
    "contact",
    "number",
)


NAME_HINTS = ("name", "business", "brand")


def find_best_column(row: dict[str, str], hints: tuple[str, ...]) -> str:
    lowered = {key.lower(): key for key in row.keys()}
    for hint in hints:
        for candidate_lower, original in lowered.items():
            if hint in candidate_lower:
                return original
    return ""


def extract_phone_candidates(value: str) -> list[str]:
    text = str(value or "")
    if not text:
        return []
    matches = re.findall(r"\+?\d[\d\-\s()]{7,}\d", text)
    cleaned = []
    for match in matches:
        digits = re.sub(r"\D", "", match)
        if digits:
            cleaned.append(digits)
    for url_match in re.findall(r"wa\.me/(?:message/)?([0-9]{8,15})", text, flags=re.I):
        cleaned.append(url_match)
    return cleaned


def normalize_phone_number(raw_values: list[str]) -> tuple[str, bool, str]:
    for raw in raw_values:
        for digits in extract_phone_candidates(raw):
            if len(digits) == 10:
                return (f"+91{digits}", True, "")
            if len(digits) == 11 and digits.startswith("0"):
                return (f"+91{digits[1:]}", True, "")
            if len(digits) == 12 and digits.startswith("91"):
                return (f"+{digits}", True, "")
            if 11 <= len(digits) <= 15:
                return (f"+{digits}", True, "")
    return ("", False, "No valid phone number found")


def summarize_row(row: dict[str, str]) -> dict[str, Any]:
    name_col = find_best_column(row, NAME_HINTS)
    phone_col = find_best_column(row, PHONE_HINTS)
    url_col = ""
    username_col = ""
    category_col = ""
    bio_col = ""
    followers_col = ""
    offer_col = ""
    why_fit_col = ""

    for key in row.keys():
        lowered = key.lower()
        if "instagram url" in lowered or lowered.endswith("url"):
            url_col = key
        elif "username" in lowered:
            username_col = key
        elif "category" in lowered:
            category_col = key
        elif lowered == "bio" or "bio" in lowered:
            bio_col = key
        elif "followers" in lowered:
            followers_col = key
        elif "suggested offer" in lowered:
            offer_col = key
        elif "why good fit" in lowered:
            why_fit_col = key

    phone_sources = []
    if phone_col:
        phone_sources.append(row.get(phone_col, ""))
    for key, value in row.items():
        lowered = key.lower()
        if "whatsapp link" in lowered or "contact" in lowered:
            phone_sources.append(value)

    phone_e164, phone_valid, invalid_reason = normalize_phone_number(phone_sources)
    summary = {
        "display_name": row.get(name_col, "").strip() if name_col else "",
        "username": row.get(username_col, "").strip() if username_col else "",
        "instagram_url": row.get(url_col, "").strip() if url_col else "",
        "category": row.get(category_col, "").strip() if category_col else "",
        "bio": row.get(bio_col, "").strip() if bio_col else "",
        "followers": row.get(followers_col, "").strip() if followers_col else "",
        "suggested_offer": row.get(offer_col, "").strip() if offer_col else "",
        "why_good_fit": row.get(why_fit_col, "").strip() if why_fit_col else "",
        "phone_raw": row.get(phone_col, "").strip() if phone_col else "",
        "phone_e164": phone_e164,
        "phone_valid": phone_valid,
        "invalid_reason": invalid_reason,
    }
    return summary


def create_campaign(
    name: str,
    prompt: str,
    filename: str,
    source_kind: str,
    saved_path: Path,
    daily_cap: int,
) -> int:
    conn = get_db()
    try:
        cursor = conn.execute(
            """
            INSERT INTO campaigns (
                name, prompt, source_filename, source_path, source_kind, uploaded_at,
                daily_cap, window_start_hour, window_end_hour, timezone, total_rows,
                valid_rows, invalid_rows, duplicate_rows, require_approval
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0, 0)
            """,
            (
                name.strip() or f"Campaign {now_ist().strftime('%d %b %H:%M')}",
                prompt.strip(),
                filename,
                str(saved_path),
                source_kind,
                utc_now_iso(),
                daily_cap,
                DEFAULT_WINDOW_START,
                DEFAULT_WINDOW_END,
                "Asia/Kolkata",
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def import_rows_into_campaign(campaign_id: int, rows: list[dict[str, str]]) -> dict[str, int]:
    conn = get_db()
    valid_rows = 0
    invalid_rows = 0
    duplicate_rows = 0
    seen_numbers: dict[str, int] = {}

    try:
        existing_rows = conn.execute(
            "SELECT id, phone_e164 FROM leads WHERE phone_valid = 1"
        ).fetchall()
        for existing in existing_rows:
            if existing["phone_e164"]:
                seen_numbers[existing["phone_e164"]] = int(existing["id"])

        for index, row in enumerate(rows, start=2):
            summary = summarize_row(row)
            duplicate_of = None
            duplicate_reason = ""
            approval_status = "approved" if summary["phone_valid"] and not duplicate_of else "rejected"
            queue_status = "not_queued"

            if summary["phone_valid"] and summary["phone_e164"] in seen_numbers:
                duplicate_of = seen_numbers[summary["phone_e164"]]
                duplicate_reason = "Phone number already exists in the database"
                duplicate_rows += 1

            if summary["phone_valid"] and not duplicate_of:
                valid_rows += 1
            else:
                invalid_rows += 1

            cursor = conn.execute(
                """
                INSERT INTO leads (
                    campaign_id, row_number, raw_row_json, row_summary_json, display_name,
                    phone_raw, phone_e164, phone_valid, invalid_reason, duplicate_of_lead_id,
                    duplicate_reason, draft_status, draft_message, draft_error,
                    personalization_json, approval_status, queue_status, scheduled_at,
                    sent_at, api_response_json, send_error, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', '', '{}', ?, ?, '', '', '{}', '', ?, ?)
                """,
                (
                    campaign_id,
                    index,
                    json_dumps(row),
                    json_dumps(summary),
                    summary["display_name"],
                    summary["phone_raw"],
                    summary["phone_e164"],
                    1 if summary["phone_valid"] else 0,
                    summary["invalid_reason"],
                    duplicate_of,
                    duplicate_reason,
                    "pending" if summary["phone_valid"] and not duplicate_of else "skipped",
                    approval_status,
                    queue_status,
                    utc_now_iso(),
                    utc_now_iso(),
                ),
            )
            if summary["phone_valid"] and not duplicate_of:
                seen_numbers[summary["phone_e164"]] = int(cursor.lastrowid)

        conn.execute(
            """
            UPDATE campaigns
            SET total_rows = ?, valid_rows = ?, invalid_rows = ?, duplicate_rows = ?
            WHERE id = ?
            """,
            (len(rows), valid_rows, invalid_rows, duplicate_rows, campaign_id),
        )
        log_send_event(
            conn,
            campaign_id=campaign_id,
            lead_id=None,
            event_type="upload",
            status="ok",
            details={
                "rows": len(rows),
                "valid_rows": valid_rows,
                "invalid_rows": invalid_rows,
                "duplicate_rows": duplicate_rows,
            },
        )
        conn.commit()
    finally:
        conn.close()

    app_log(
        "campaign_imported",
        campaign_id=campaign_id,
        rows=len(rows),
        valid_rows=valid_rows,
        invalid_rows=invalid_rows,
        duplicate_rows=duplicate_rows,
    )
    return {
        "rows": len(rows),
        "valid_rows": valid_rows,
        "invalid_rows": invalid_rows,
        "duplicate_rows": duplicate_rows,
    }


MESSAGE_PROMPT = """You write very concise personalized WhatsApp outreach messages for Indian daytime sending.

Rules:
- Keep the message concise. If you include the example link, keep the non-link text very short.
- Use simple English.
- Sound human, warm, specific, and natural.
- Personalize using only the provided row details.
- Focus on the offered service and one relevant observation from the row.
- If it fits naturally, mention that we can build a simple online store or similar sales flow.
- If the example link is relevant, include exactly that one link and no other link.
- Avoid hype, spammy wording, fake urgency, and emojis.
- Do not mention scraping, automation, AI, or bulk outreach.
- Do not sound like a template.
- Make it feel like one thoughtful first message from a real person.
- Prefer 1 to 2 short sentences.
- Output only valid JSON.

Return JSON:
{
  "message": "final draft message",
  "why": "one short sentence explaining the personalization"
}
"""


def choose_example_store_url(lead_id: int) -> str:
    rng = random.Random(f"store-example-{lead_id}-{int(time.time() // 300)}")
    return rng.choice(ONLINE_STORE_EXAMPLES)


def build_lead_prompt(campaign: sqlite3.Row, lead: sqlite3.Row) -> str:
    summary = json.loads(lead["row_summary_json"] or "{}")
    raw_row = json.loads(lead["raw_row_json"] or "{}")
    example_store_url = choose_example_store_url(int(lead["id"]))
    compact_row = {
        key: value
        for key, value in raw_row.items()
        if str(value or "").strip()
    }
    return f"""Create a short outreach draft for this business.

Service pitch prompt:
{campaign['prompt']}

Reference store example:
- Use this only as inspiration for the kind of store we can build, not as a claim about the lead.
- Mention the idea briefly only if it fits naturally.
- Example link: {example_store_url}

Lead summary:
- Name: {summary.get('display_name', '')}
- Username: {summary.get('username', '')}
- Category: {summary.get('category', '')}
- Followers: {summary.get('followers', '')}
- Instagram URL: {summary.get('instagram_url', '')}
- Suggested Offer: {summary.get('suggested_offer', '')}
- Why good fit: {summary.get('why_good_fit', '')}
- Bio: {summary.get('bio', '')}

Raw row data:
{json_dumps(compact_row)}

Return only JSON."""


def _set_lead_generating(conn: sqlite3.Connection, lead_id: int) -> None:
    conn.execute(
        "UPDATE leads SET draft_status = 'generating', updated_at = ? WHERE id = ?",
        (utc_now_iso(), lead_id),
    )


def claim_next_draft_lead(campaign_id: int | None = None) -> tuple[sqlite3.Row, sqlite3.Row] | tuple[None, None]:
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        campaign_filter = ""
        params: list[Any] = []
        if campaign_id is not None:
            campaign_filter = "AND l.campaign_id = ?"
            params.append(int(campaign_id))
        params.append(PRE_DRAFT_LIMIT)
        upcoming = conn.execute(
            """
            SELECT l.*, c.prompt, c.name AS campaign_name
            FROM leads l
            JOIN campaigns c ON c.id = l.campaign_id
            WHERE queue_status = 'queued'
              AND approval_status = 'approved'
              AND phone_valid = 1
              AND duplicate_of_lead_id IS NULL
              AND scheduled_at != ''
              {campaign_filter}
            ORDER BY scheduled_at ASC, id ASC
            LIMIT ?
            """
            .format(campaign_filter=campaign_filter),
            tuple(params),
        ).fetchall()
        if not upcoming:
            conn.commit()
            return (None, None)

        lead = next(
            (
                row for row in upcoming
                if row["draft_status"] in {"pending", "failed"}
            ),
            None,
        )
        if not lead:
            conn.commit()
            return (None, None)
        _set_lead_generating(conn, int(lead["id"]))
        conn.commit()
        campaign = conn.execute("SELECT * FROM campaigns WHERE id = ?", (lead["campaign_id"],)).fetchone()
        return (campaign, lead)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def prepare_upcoming_drafts(limit: int = PRE_DRAFT_LIMIT, campaign_id: int | None = None) -> None:
    for _ in range(limit):
        campaign, lead = claim_next_draft_lead(campaign_id=campaign_id)
        if not lead:
            break
        generate_draft_for_lead(campaign, lead)


def generate_draft_for_lead(campaign: sqlite3.Row, lead: sqlite3.Row) -> None:
    conn = get_db()
    try:
        client = get_client()
        response = client.chat.completions.create(
            model=get_model_name("draft"),
            messages=[
                {"role": "system", "content": MESSAGE_PROMPT},
                {"role": "user", "content": build_lead_prompt(campaign, lead)},
            ],
            temperature=0.5,
            max_tokens=400,
            response_format={"type": "json_object"},
        )
        text = (response.choices[0].message.content or "").strip()
        parsed = parse_json_response_text(text)
        message = str(parsed.get("message", "")).strip()
        why = str(parsed.get("why", "")).strip()
        if not message:
            raise ValueError("Model returned an empty message")
        require_approval = bool(campaign["require_approval"])
        approval_status = "needs_review" if require_approval else "approved"
        conn.execute(
            """
            UPDATE leads
            SET draft_status = 'generated',
                draft_message = ?,
                draft_error = '',
                personalization_json = ?,
                approval_status = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (message, json_dumps({"why": why}), approval_status, utc_now_iso(), lead["id"]),
        )
        log_send_event(
            conn,
            campaign_id=lead["campaign_id"],
            lead_id=lead["id"],
            event_type="draft",
            status="generated",
            details={"why": why},
        )
        conn.commit()
        app_log("draft_generated", campaign_id=lead["campaign_id"], lead_id=lead["id"])
    except Exception as exc:
        error = describe_exception(exc)
        conn.execute(
            """
            UPDATE leads
            SET draft_status = 'failed',
                draft_error = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (error, utc_now_iso(), lead["id"]),
        )
        log_send_event(
            conn,
            campaign_id=lead["campaign_id"],
            lead_id=lead["id"],
            event_type="draft",
            status="failed",
            details={"error": error},
        )
        conn.commit()
        app_log("draft_failed", campaign_id=lead["campaign_id"], lead_id=lead["id"], error=error)
    finally:
        conn.close()


def draft_worker() -> None:
    while True:
        try:
            prepare_upcoming_drafts()
            time.sleep(3)
        except Exception as exc:
            app_log("draft_worker_error", error=describe_exception(exc))
            time.sleep(5)


def get_daytime_window(day: datetime, start_hour: int, end_hour: int) -> tuple[datetime, datetime]:
    start = day.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    end = day.replace(hour=23 if end_hour >= 24 else end_hour, minute=59, second=0, microsecond=0)
    return (start, end)


def get_campaign_sent_count_for_day(conn: sqlite3.Connection, campaign_id: int, date_str: str) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM leads
        WHERE campaign_id = ?
          AND queue_status = 'sent'
          AND sent_at != ''
          AND substr(sent_at, 1, 10) = ?
        """,
        (campaign_id, date_str),
    ).fetchone()
    return int(row["c"]) if row else 0


def schedule_campaign(campaign_id: int) -> None:
    with SCHEDULE_LOCK:
        conn = get_db()
        try:
            campaign = conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
            if not campaign:
                return

            approved = conn.execute(
                """
                SELECT *
                FROM leads
                WHERE campaign_id = ?
                  AND approval_status = 'approved'
                  AND phone_valid = 1
                  AND duplicate_of_lead_id IS NULL
                  AND draft_status != 'skipped'
                  AND queue_status = 'not_queued'
                ORDER BY id ASC
                """,
                (campaign_id,),
            ).fetchall()
            if not approved:
                return

            start_hour = int(campaign["window_start_hour"] or DEFAULT_WINDOW_START)
            end_hour = int(campaign["window_end_hour"] or DEFAULT_WINDOW_END)
            daily_cap = int(campaign["daily_cap"] or DEFAULT_DAILY_CAP)
            baseline = now_ist() + timedelta(minutes=5)
            day_cursor = baseline.astimezone(TZ)
            used_slots: dict[str, list[datetime]] = {}

            existing = conn.execute(
                """
                SELECT scheduled_at
                FROM leads
                WHERE campaign_id = ?
                  AND queue_status = 'queued'
                  AND scheduled_at != ''
                """,
                (campaign_id,),
            ).fetchall()
            for row in existing:
                try:
                    scheduled = datetime.fromisoformat(row["scheduled_at"])
                except Exception:
                    continue
                key = scheduled.astimezone(TZ).strftime("%Y-%m-%d")
                used_slots.setdefault(key, []).append(scheduled.astimezone(TZ))

            for lead in approved:
                while True:
                    day_key = day_cursor.strftime("%Y-%m-%d")
                    used_today = len(used_slots.get(day_key, [])) + get_campaign_sent_count_for_day(conn, campaign_id, day_key)
                    if used_today >= daily_cap:
                        day_cursor = (day_cursor + timedelta(days=1)).replace(hour=start_hour, minute=0, second=0, microsecond=0)
                        continue

                    window_start, window_end = get_daytime_window(day_cursor, start_hour, end_hour)
                    earliest = max(window_start, baseline if day_key == baseline.strftime("%Y-%m-%d") else window_start)
                    if earliest > window_end:
                        day_cursor = (day_cursor + timedelta(days=1)).replace(hour=start_hour, minute=0, second=0, microsecond=0)
                        continue

                    seconds_range = max(int((window_end - earliest).total_seconds()), 1)
                    candidate = earliest + timedelta(seconds=random.randint(0, seconds_range))
                    existing_for_day = used_slots.setdefault(day_key, [])
                    collision = any(abs((candidate - item).total_seconds()) < 120 for item in existing_for_day)
                    if collision and seconds_range > 180:
                        continue

                    existing_for_day.append(candidate)
                    conn.execute(
                        """
                        UPDATE leads
                        SET queue_status = 'queued',
                            scheduled_at = ?,
                            send_error = '',
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (candidate.isoformat(), utc_now_iso(), lead["id"]),
                    )
                    break

            log_send_event(
                conn,
                campaign_id=campaign_id,
                lead_id=None,
                event_type="schedule",
                status="ok",
                details={"queued": len(approved)},
            )
            conn.commit()
        finally:
            conn.close()


def send_due_messages() -> None:
    conn = get_db()
    try:
        campaigns = conn.execute("SELECT * FROM campaigns ORDER BY id DESC").fetchall()
        current_time = now_ist()
        today_key = current_time.strftime("%Y-%m-%d")
        for campaign in campaigns:
            if current_time.hour < int(campaign["window_start_hour"]) or current_time.hour >= int(campaign["window_end_hour"]):
                continue
            sent_today = get_campaign_sent_count_for_day(conn, int(campaign["id"]), today_key)
            if sent_today >= int(campaign["daily_cap"]):
                continue

            lead = conn.execute(
                """
                SELECT *
                FROM leads
                WHERE campaign_id = ?
                  AND queue_status = 'queued'
                  AND approval_status = 'approved'
                  AND scheduled_at != ''
                ORDER BY scheduled_at ASC
                LIMIT 1
                """,
                (campaign["id"],),
            ).fetchone()
            if not lead:
                continue

            try:
                scheduled_at = datetime.fromisoformat(lead["scheduled_at"]).astimezone(TZ)
            except Exception:
                scheduled_at = current_time
            if scheduled_at > current_time:
                continue

            if lead["draft_status"] != "generated" or not str(lead["draft_message"] or "").strip():
                conn.execute("BEGIN IMMEDIATE")
                _set_lead_generating(conn, int(lead["id"]))
                conn.commit()
                conn.close()
                campaign_row = dict(campaign)
                lead_row = dict(lead)
                generate_draft_for_lead(campaign_row, lead_row)
                conn = get_db()
                lead = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_row["id"],)).fetchone()
                if not lead or lead["draft_status"] != "generated" or not str(lead["draft_message"] or "").strip():
                    continue

            result = send_message(lead["phone_e164"], lead["draft_message"])
            status = result.get("status", "failed")
            if status == "sent":
                conn.execute(
                    """
                    UPDATE leads
                    SET queue_status = 'sent',
                        sent_at = ?,
                        api_response_json = ?,
                        send_error = '',
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        current_time.isoformat(),
                        json_dumps(result.get("response", {})),
                        utc_now_iso(),
                        lead["id"],
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE leads
                    SET queue_status = 'failed',
                        api_response_json = ?,
                        send_error = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        json_dumps(result.get("response", {})),
                        str(result.get("error", "Unknown error")),
                        utc_now_iso(),
                        lead["id"],
                    ),
                )

            log_send_event(
                conn,
                campaign_id=int(campaign["id"]),
                lead_id=int(lead["id"]),
                event_type="send",
                status=status,
                details=result,
            )
            conn.commit()
            app_log("message_sent" if status == "sent" else "message_failed", lead_id=lead["id"], status=status)
            schedule_campaign(int(campaign["id"]))
            prepare_upcoming_drafts()
            break
    finally:
        conn.close()


def send_lead_now(lead_id: int) -> tuple[bool, str]:
    conn = get_db()
    try:
        lead = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        if not lead:
            return (False, "Lead not found.")
        if not lead["phone_valid"] or lead["duplicate_of_lead_id"] is not None:
            return (False, "Lead does not have a sendable phone number.")

        campaign = conn.execute("SELECT * FROM campaigns WHERE id = ?", (lead["campaign_id"],)).fetchone()
        if not campaign:
            return (False, "Campaign not found.")

        conn.execute(
            """
            UPDATE leads
            SET approval_status = 'approved',
                queue_status = 'queued',
                scheduled_at = ?,
                send_error = '',
                updated_at = ?
            WHERE id = ?
            """,
            ((now_ist() - timedelta(seconds=5)).isoformat(), utc_now_iso(), lead_id),
        )
        if lead["draft_status"] != "generated" or not str(lead["draft_message"] or "").strip():
            _set_lead_generating(conn, lead_id)
        log_send_event(
            conn,
            campaign_id=int(lead["campaign_id"]),
            lead_id=lead_id,
            event_type="send_now",
            status="queued",
            details={},
        )
        conn.commit()
    finally:
        conn.close()

    if lead["draft_status"] != "generated" or not str(lead["draft_message"] or "").strip():
        generate_draft_for_lead(campaign, lead)
    send_due_messages()
    return (True, "Lead queued for immediate send.")


def sender_worker() -> None:
    while True:
        try:
            send_due_messages()
        except Exception as exc:
            app_log("sender_worker_error", error=describe_exception(exc))
        time.sleep(15)


def ensure_workers_started() -> None:
    global WORKERS_STARTED
    if WORKERS_STARTED:
        return
    WORKERS_STARTED = True
    threading.Thread(target=draft_worker, daemon=True, name="prane-whatsapp-drafts").start()
    threading.Thread(target=sender_worker, daemon=True, name="prane-whatsapp-sender").start()


def project_days(total: int, daily_cap: int) -> int:
    if daily_cap <= 0:
        return 0
    return int(math.ceil(total / daily_cap))


def get_dashboard_payload() -> dict[str, Any]:
    conn = get_db()
    try:
        campaign = conn.execute("SELECT * FROM campaigns ORDER BY id DESC LIMIT 1").fetchone()
        if not campaign:
            return {
                "campaign": None,
                "stats": {
                    "total_rows": 0,
                    "valid_rows": 0,
                    "invalid_rows": 0,
                    "duplicates": 0,
                    "drafted": 0,
                    "draft_failed": 0,
                    "approved": 0,
                    "queued": 0,
                    "sent": 0,
                    "send_failed": 0,
                    "days_for_total": 0,
                    "days_for_approved": 0,
                },
                "leads": [],
                "recent_logs": [],
                "runtime": {
                    "local_ip": get_local_ip(),
                    "api_base_url": config.get("prane_base_url", ""),
                    "timezone": "Asia/Kolkata",
                    "day_window": f"{DEFAULT_WINDOW_START}:00 - {DEFAULT_WINDOW_END}:00",
                },
            }

        campaign_id = int(campaign["id"])
        aggregate = conn.execute(
            """
            SELECT
                COUNT(*) AS total_rows,
                SUM(CASE WHEN phone_valid = 1 AND duplicate_of_lead_id IS NULL THEN 1 ELSE 0 END) AS valid_rows,
                SUM(CASE WHEN phone_valid = 0 THEN 1 ELSE 0 END) AS invalid_rows,
                SUM(CASE WHEN duplicate_of_lead_id IS NOT NULL THEN 1 ELSE 0 END) AS duplicates,
                SUM(CASE WHEN draft_status = 'generated' THEN 1 ELSE 0 END) AS drafted,
                SUM(CASE WHEN draft_status = 'failed' THEN 1 ELSE 0 END) AS draft_failed,
                SUM(CASE WHEN approval_status = 'approved' THEN 1 ELSE 0 END) AS approved,
                SUM(CASE WHEN queue_status = 'queued' THEN 1 ELSE 0 END) AS queued,
                SUM(CASE WHEN queue_status = 'sent' THEN 1 ELSE 0 END) AS sent,
                SUM(CASE WHEN queue_status = 'failed' THEN 1 ELSE 0 END) AS send_failed
            FROM leads
            WHERE campaign_id = ?
            """,
            (campaign_id,),
        ).fetchone()
        leads = conn.execute(
            """
            SELECT *
            FROM leads
            WHERE campaign_id = ?
            ORDER BY
                CASE
                    WHEN draft_status = 'generated' THEN 0
                    WHEN draft_status = 'generating' THEN 1
                    WHEN queue_status = 'queued' THEN 2
                    ELSE 3
                END ASC,
                CASE WHEN scheduled_at = '' THEN 1 ELSE 0 END ASC,
                scheduled_at ASC,
                id DESC
            """,
            (campaign_id,),
        ).fetchall()
        logs = conn.execute(
            """
            SELECT *
            FROM send_logs
            WHERE campaign_id = ?
            ORDER BY id DESC
            LIMIT 60
            """,
            (campaign_id,),
        ).fetchall()

        daily_cap = int(campaign["daily_cap"] or DEFAULT_DAILY_CAP)
        stats = {
            key: int(aggregate[key] or 0)
            for key in (
                "total_rows",
                "valid_rows",
                "invalid_rows",
                "duplicates",
                "drafted",
                "draft_failed",
                "approved",
                "queued",
                "sent",
                "send_failed",
            )
        }
        stats["days_for_total"] = project_days(stats["valid_rows"], daily_cap)
        stats["days_for_approved"] = project_days(stats["approved"], daily_cap)

        lead_payload = []
        for lead in leads:
            personalization = json.loads(lead["personalization_json"] or "{}")
            summary = json.loads(lead["row_summary_json"] or "{}")
            lead_payload.append(
                {
                    "id": lead["id"],
                    "row_number": lead["row_number"],
                    "display_name": lead["display_name"],
                    "phone_e164": lead["phone_e164"],
                    "phone_valid": bool(lead["phone_valid"]),
                    "invalid_reason": lead["invalid_reason"],
                    "duplicate_reason": lead["duplicate_reason"],
                    "draft_status": lead["draft_status"],
                    "draft_message": lead["draft_message"],
                    "draft_error": lead["draft_error"],
                    "approval_status": lead["approval_status"],
                    "queue_status": lead["queue_status"],
                    "scheduled_at": lead["scheduled_at"],
                    "sent_at": lead["sent_at"],
                    "send_error": lead["send_error"],
                    "why": personalization.get("why", ""),
                    "summary": summary,
                }
            )

        return {
            "campaign": {
                "id": campaign_id,
                "name": campaign["name"],
                "prompt": campaign["prompt"],
                "source_filename": campaign["source_filename"],
                "daily_cap": daily_cap,
                "window_start_hour": int(campaign["window_start_hour"]),
                "window_end_hour": int(campaign["window_end_hour"]),
                "uploaded_at": campaign["uploaded_at"],
            },
            "stats": stats,
            "leads": lead_payload,
            "recent_logs": [
                {
                    "event_type": row["event_type"],
                    "status": row["status"],
                    "created_at": row["created_at"],
                    "details": json.loads(row["details_json"] or "{}"),
                }
                for row in logs
            ],
            "runtime": {
                "local_ip": get_local_ip(),
                "api_base_url": config.get("prane_base_url", ""),
                "timezone": "Asia/Kolkata",
                "day_window": f"{int(campaign['window_start_hour'])}:00 - {int(campaign['window_end_hour'])}:00",
            },
        }
    finally:
        conn.close()


HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Prane WhatsApp Dashboard</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;700&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
  <style>
    :root{
      --bg:#f6f1e7;
      --surface:#fffaf2;
      --ink:#1e1d19;
      --muted:#6b665b;
      --line:rgba(30,29,25,.12);
      --accent:#0d7c66;
      --accent-2:#ea580c;
      --soft:#efe3cf;
      --good:#18794e;
      --bad:#b42318;
      --warn:#9a6700;
      --card-shadow:0 24px 60px rgba(57,46,24,.08);
    }
    *{box-sizing:border-box}
    body{
      margin:0;
      font-family:"Space Grotesk",sans-serif;
      color:var(--ink);
      background:
        radial-gradient(circle at top left, rgba(13,124,102,.14), transparent 32%),
        radial-gradient(circle at right 20%, rgba(234,88,12,.16), transparent 28%),
        linear-gradient(180deg, #f8f4ec 0%, #f3ecde 100%);
      min-height:100vh;
    }
    .shell{max-width:1400px;margin:0 auto;padding:28px 18px 40px}
    .hero{
      background:linear-gradient(135deg, rgba(255,250,242,.95), rgba(255,244,226,.92));
      border:1px solid var(--line);
      border-radius:28px;
      padding:24px;
      box-shadow:var(--card-shadow);
      display:grid;
      grid-template-columns:1.3fr .9fr;
      gap:18px;
    }
    h1,h2,h3{margin:0}
    h1{font-size:clamp(28px,4vw,52px);line-height:.96;letter-spacing:-.04em}
    .hero p,.muted{color:var(--muted)}
    .pill{
      display:inline-flex;align-items:center;gap:8px;padding:8px 12px;border-radius:999px;
      background:rgba(13,124,102,.1);color:var(--accent);font-size:13px;font-weight:700;margin-bottom:12px
    }
    .stack{display:grid;gap:18px;margin-top:18px}
    .grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-top:18px}
    .card{
      background:rgba(255,250,242,.92);
      border:1px solid var(--line);
      border-radius:22px;
      box-shadow:var(--card-shadow);
      padding:18px;
    }
    .card small{display:block;color:var(--muted);margin-bottom:8px}
    .stat{font-size:34px;font-weight:700;letter-spacing:-.03em}
    .form-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
    input,textarea,button{
      font:inherit;border-radius:16px;border:1px solid var(--line);padding:13px 14px;background:#fffdf8;color:var(--ink)
    }
    textarea{min-height:120px;resize:vertical}
    button{
      background:var(--ink);color:#fff;border:none;cursor:pointer;font-weight:700;transition:transform .16s ease,opacity .16s ease
    }
    button:hover{transform:translateY(-1px)}
    button.secondary{background:rgba(13,124,102,.12);color:var(--accent)}
    button.warn{background:rgba(234,88,12,.12);color:var(--accent-2)}
    .meta{display:grid;gap:10px}
    .mono{font-family:"IBM Plex Mono",monospace}
    .lead-list{display:grid;gap:14px}
    .lead{
      background:rgba(255,255,255,.74);
      border:1px solid var(--line);
      border-radius:22px;
      padding:16px;
      display:grid;
      gap:12px;
    }
    .lead-top{display:flex;justify-content:space-between;gap:14px;align-items:flex-start}
    .lead-title{font-size:19px;font-weight:700}
    .tags{display:flex;flex-wrap:wrap;gap:8px}
    .tag{
      font-size:12px;font-weight:700;padding:6px 10px;border-radius:999px;border:1px solid var(--line);background:#fff7ea
    }
    .tag.good{color:var(--good);border-color:rgba(24,121,78,.28);background:rgba(24,121,78,.08)}
    .tag.bad{color:var(--bad);border-color:rgba(180,35,24,.22);background:rgba(180,35,24,.08)}
    .tag.warn{color:var(--warn);border-color:rgba(154,103,0,.22);background:rgba(154,103,0,.08)}
    .lead-body{display:grid;grid-template-columns:1.1fr .9fr;gap:12px}
    .draft{background:#fffdf8;border:1px dashed rgba(30,29,25,.2);padding:14px;border-radius:16px}
    .actions{display:flex;flex-wrap:wrap;gap:10px}
    .logs{display:grid;gap:8px;max-height:360px;overflow:auto}
    .log{padding:12px 13px;border-radius:14px;background:#fffdf8;border:1px solid var(--line)}
    .banner{padding:12px 14px;border-radius:14px;background:rgba(13,124,102,.08);color:var(--accent);font-weight:700;display:none}
    .empty{padding:32px;text-align:center;color:var(--muted)}
    @media (max-width: 1080px){
      .hero,.grid,.lead-body,.form-grid{grid-template-columns:1fr}
    }
  </style>
</head>
<body>
  <div class="shell">
    <section class="hero">
      <div>
        <div class="pill">Prane WhatsApp Campaign Studio</div>
        <h1>Upload leads, auto-schedule daytime WhatsApp sends, and pre-draft only the next few.</h1>
        <p>Valid leads queue immediately with random IST send times. The app only pre-drafts the next 3 upcoming messages, then drafts the rest just before send.</p>
      </div>
      <div class="card">
        <h3>New Campaign</h3>
        <div class="banner" id="banner"></div>
        <form id="upload-form">
          <div class="form-grid">
            <input type="text" id="campaign-name" name="campaign_name" placeholder="Campaign name">
            <input type="file" id="file" name="file" accept=".csv,.xlsx" required>
          </div>
          <div style="height:10px"></div>
          <div class="form-grid">
            <input type="number" id="daily-cap" name="daily_cap" min="1" max="90" value="90" placeholder="Daily cap">
            <input type="text" value="08:00 - 24:00 IST" disabled>
          </div>
          <div style="height:10px"></div>
          <textarea id="pitch-prompt" name="pitch_prompt" placeholder="Describe the service, tone, and how each message should personalize the pitch from one CSV row." required></textarea>
          <div style="height:10px"></div>
          <div class="actions">
            <button type="submit">Upload And Start Drafting</button>
            <button type="button" class="warn" onclick="clearEverything()">Clear Everything</button>
          </div>
        </form>
      </div>
    </section>

    <section class="grid" id="stats"></section>

    <section class="stack">
      <div class="card">
        <h3>Runtime</h3>
        <div class="meta" id="runtime"></div>
      </div>
      <div class="card">
        <h3>Recent Activity</h3>
        <div class="logs" id="logs"></div>
      </div>
      <div class="card">
        <h3>Lead Queue</h3>
        <div class="actions" id="lead-pagination" style="margin:12px 0 16px"></div>
        <div class="lead-list" id="leads"></div>
      </div>
    </section>
  </div>

  <script>
    const statsEl = document.getElementById('stats');
    const leadsEl = document.getElementById('leads');
    const paginationEl = document.getElementById('lead-pagination');
    const logsEl = document.getElementById('logs');
    const runtimeEl = document.getElementById('runtime');
    const banner = document.getElementById('banner');
    const BASE_PATH = window.location.pathname.replace(/\/+$/, '');
    const apiUrl = (path) => `${BASE_PATH}${path}`;
    const LEADS_PER_PAGE = 20;
    let currentLeadPage = 1;

    function showBanner(text, ok=true){
      banner.style.display='block';
      banner.style.background = ok ? 'rgba(13,124,102,.08)' : 'rgba(180,35,24,.08)';
      banner.style.color = ok ? 'var(--accent)' : 'var(--bad)';
      banner.textContent = text;
      setTimeout(()=> banner.style.display='none', 5000);
    }

    function statCard(label, value, note){
      return `<div class="card"><small>${label}</small><div class="stat">${value}</div><div class="muted">${note || ''}</div></div>`;
    }

    async function postJson(url, payload){
      const res = await fetch(url, {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify(payload)
      });
      return res.json();
    }

    async function refresh(){
      const res = await fetch(apiUrl('/api/data'));
      const data = await res.json();
      const s = data.stats || {};
      statsEl.innerHTML = [
        statCard('Valid Leads', s.valid_rows || 0, `${s.invalid_rows || 0} invalid, ${s.duplicates || 0} duplicates`),
        statCard('Drafted', s.drafted || 0, `${s.draft_failed || 0} draft failures`),
        statCard('Approved', s.approved || 0, `${s.queued || 0} queued, ${s.sent || 0} sent`),
        statCard('Quota Plan', `${data.campaign ? data.campaign.daily_cap : 90}/day`, `${s.days_for_total || 0} days for all valid, ${s.days_for_approved || 0} days for approved`)
      ].join('');

      runtimeEl.innerHTML = '';
      const runtime = data.runtime || {};
      const campaign = data.campaign;
      const runtimeRows = [
        ['Campaign', campaign ? campaign.name : 'No campaign yet'],
        ['File', campaign ? campaign.source_filename : '-'],
        ['API Base URL', runtime.api_base_url || '-'],
        ['Local IP', runtime.local_ip || '-'],
        ['Timezone', runtime.timezone || '-'],
        ['Send Window', runtime.day_window || '-'],
      ];
      runtimeRows.forEach(([label, value])=>{
        const row = document.createElement('div');
        row.innerHTML = `<strong>${label}:</strong> <span class="mono">${String(value)}</span>`;
        runtimeEl.appendChild(row);
      });

      logsEl.innerHTML = '';
      (data.recent_logs || []).forEach(item=>{
        const div = document.createElement('div');
        div.className = 'log';
        div.innerHTML = `<strong>${item.event_type}</strong> <span class="tag ${item.status === 'sent' || item.status === 'ok' || item.status === 'generated' ? 'good' : item.status === 'failed' ? 'bad' : 'warn'}">${item.status}</span><div class="muted mono">${item.created_at}</div><div class="muted">${JSON.stringify(item.details)}</div>`;
        logsEl.appendChild(div);
      });
      if (!logsEl.innerHTML) logsEl.innerHTML = '<div class="empty">No activity yet.</div>';

      const allLeads = data.leads || [];
      const totalPages = Math.max(1, Math.ceil(allLeads.length / LEADS_PER_PAGE));
      currentLeadPage = Math.min(currentLeadPage, totalPages);
      currentLeadPage = Math.max(1, currentLeadPage);
      const startIndex = (currentLeadPage - 1) * LEADS_PER_PAGE;
      const pagedLeads = allLeads.slice(startIndex, startIndex + LEADS_PER_PAGE);

      paginationEl.innerHTML = '';
      if (allLeads.length > LEADS_PER_PAGE) {
        paginationEl.innerHTML = `
          <button class="secondary" ${currentLeadPage === 1 ? 'disabled' : ''} onclick="changeLeadPage(-1)">Previous</button>
          <div class="muted">Page ${currentLeadPage} of ${totalPages} • ${allLeads.length} leads</div>
          <button class="secondary" ${currentLeadPage >= totalPages ? 'disabled' : ''} onclick="changeLeadPage(1)">Next</button>
        `;
      } else {
        paginationEl.innerHTML = `<div class="muted">${allLeads.length} lead${allLeads.length === 1 ? '' : 's'}</div>`;
      }

      leadsEl.innerHTML = '';
      pagedLeads.forEach(lead=>{
        const summary = lead.summary || {};
        const div = document.createElement('div');
        div.className = 'lead';
        const statusClass = lead.queue_status === 'sent' ? 'good' : (lead.queue_status === 'failed' ? 'bad' : 'warn');
        const approvalClass = lead.approval_status === 'approved' ? 'good' : (lead.approval_status === 'rejected' ? 'bad' : 'warn');
        div.innerHTML = `
          <div class="lead-top">
            <div>
              <div class="lead-title">${lead.display_name || summary.username || 'Unnamed lead'}</div>
              <div class="muted mono">${lead.phone_e164 || 'No valid number'}</div>
            </div>
            <div class="tags">
              <span class="tag ${approvalClass}">${lead.approval_status}</span>
              <span class="tag ${statusClass}">${lead.queue_status}</span>
              <span class="tag">${lead.draft_status}</span>
            </div>
          </div>
          <div class="lead-body">
            <div class="draft">
              <strong>Draft</strong>
              <div style="height:8px"></div>
              <div>${lead.draft_message ? lead.draft_message : '<span class="muted">Draft will appear shortly before send, or when you use Send Now.</span>'}</div>
              ${lead.why ? `<div style="height:10px"></div><div class="muted"><strong>Why:</strong> ${lead.why}</div>` : ''}
              ${lead.draft_error ? `<div style="height:10px"></div><div class="muted" style="color:var(--bad)">${lead.draft_error}</div>` : ''}
              ${lead.send_error ? `<div style="height:10px"></div><div class="muted" style="color:var(--bad)">${lead.send_error}</div>` : ''}
            </div>
            <div class="draft">
              <strong>Context</strong>
              <div style="height:8px"></div>
              <div class="muted">Category: ${summary.category || '-'}</div>
              <div class="muted">Followers: ${summary.followers || '-'}</div>
              <div class="muted">Offer hint: ${summary.suggested_offer || '-'}</div>
              <div class="muted">Reason: ${summary.why_good_fit || '-'}</div>
              <div class="muted">Scheduled: ${lead.scheduled_at || '-'}</div>
              <div class="actions" style="margin-top:12px">
                <button onclick="sendLeadNow(${lead.id})">Send Now</button>
                <button class="secondary" onclick="approveLead(${lead.id}, 'approved')">Approve</button>
                <button class="warn" onclick="approveLead(${lead.id}, 'rejected')">Reject</button>
                <button class="secondary" onclick="regenerateLead(${lead.id})">Regenerate</button>
              </div>
            </div>
          </div>
        `;
        leadsEl.appendChild(div);
      });
      if (!leadsEl.innerHTML) leadsEl.innerHTML = '<div class="empty">Upload a CSV or XLSX to start.</div>';
    }

    function changeLeadPage(delta){
      currentLeadPage = Math.max(1, currentLeadPage + delta);
      refresh();
    }

    async function approveLead(id, status){
      const res = await postJson(apiUrl('/api/lead/approval'), {id, status});
      showBanner(res.ok ? `Lead ${status}.` : (res.error || 'Action failed'), !!res.ok);
      refresh();
    }

    async function regenerateLead(id){
      const res = await postJson(apiUrl('/api/lead/regenerate'), {id});
      showBanner(res.ok ? 'Lead queued for regeneration.' : (res.error || 'Action failed'), !!res.ok);
      refresh();
    }

    async function sendLeadNow(id){
      const res = await postJson(apiUrl('/api/lead/send-now'), {id});
      showBanner(res.ok ? (res.message || 'Lead queued for immediate send.') : (res.error || 'Action failed'), !!res.ok);
      refresh();
    }

    async function clearEverything(){
      if (!window.confirm('Clear all WhatsApp campaigns, leads, logs, and uploaded files?')) return;
      const res = await postJson(apiUrl('/api/clear-all'), {});
      showBanner(res.ok ? 'WhatsApp dashboard data cleared.' : (res.error || 'Clear failed'), !!res.ok);
      refresh();
    }

    document.getElementById('upload-form').addEventListener('submit', async (event)=>{
      event.preventDefault();
      const form = new FormData(event.target);
      const res = await fetch(apiUrl('/api/upload'), {method:'POST', body:form});
      const data = await res.json();
      showBanner(data.ok ? `Uploaded ${data.rows} rows.` : (data.error || 'Upload failed'), !!data.ok);
      if (data.ok) event.target.reset();
      refresh();
    });

    refresh();
    setInterval(refresh, 10000);
  </script>
</body>
</html>"""


class PraneWhatsAppHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        ensure_workers_started()
        path = urlparse(self.path).path
        if path == "/api/data":
            self._json_response(get_dashboard_payload())
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(HTML.encode("utf-8"))

    def do_POST(self) -> None:
        ensure_workers_started()
        path = urlparse(self.path).path
        content_length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(content_length) if content_length else b""
        if path == "/api/upload":
            self.handle_upload(body)
            return

        try:
            data = json.loads((body or b"{}").decode("utf-8"))
        except Exception:
            data = {}

        if path == "/api/lead/approval":
            self.handle_approval(data)
        elif path == "/api/lead/regenerate":
            self.handle_regenerate(data)
        elif path == "/api/lead/send-now":
            self.handle_send_now(data)
        elif path == "/api/clear-all":
            self.handle_clear_all()
        else:
            self._json_response({"error": "Not found"}, 404)

    def handle_upload(self, body: bytes) -> None:
        try:
            fields, uploaded = parse_multipart_form(self.headers.get("Content-Type", ""), body)
            if uploaded is None or not uploaded.get("filename"):
                self._json_response({"error": "Please attach a CSV or XLSX file."}, 400)
                return

            prompt = str(fields.get("pitch_prompt", "")).strip()
            campaign_name = str(fields.get("campaign_name", "")).strip()
            daily_cap_raw = str(fields.get("daily_cap", str(DEFAULT_DAILY_CAP))).strip()
            if not prompt:
                self._json_response({"error": "Pitch prompt is required."}, 400)
                return
            try:
                daily_cap = max(1, min(int(daily_cap_raw), 90))
            except Exception:
                daily_cap = DEFAULT_DAILY_CAP

            payload = uploaded["payload"]
            source_kind, rows = read_rows_from_upload(uploaded["filename"], payload)
            if not rows:
                self._json_response({"error": "The uploaded file did not contain any rows."}, 400)
                return

            timestamp = now_ist().strftime("%Y%m%d_%H%M%S")
            safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", uploaded["filename"])
            saved_path = UPLOAD_DIR / f"{timestamp}_{safe_name}"
            saved_path.write_bytes(payload)

            campaign_id = create_campaign(
                campaign_name,
                prompt,
                uploaded["filename"],
                source_kind,
                saved_path,
                daily_cap,
            )
            summary = import_rows_into_campaign(campaign_id, rows)
            schedule_campaign(campaign_id)
            prepare_upcoming_drafts(campaign_id=campaign_id)
            self._json_response({"ok": True, "campaign_id": campaign_id, **summary})
        except Exception as exc:
            self._json_response({"error": describe_exception(exc)}, 500)

    def handle_approval(self, data: dict[str, Any]) -> None:
        lead_id = int(data.get("id") or 0)
        status = str(data.get("status") or "").strip().lower()
        if lead_id <= 0 or status not in {"approved", "rejected"}:
            self._json_response({"error": "Invalid approval payload."}, 400)
            return

        conn = get_db()
        try:
            lead = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
            if not lead:
                self._json_response({"error": "Lead not found."}, 404)
                return
            queue_status = "not_queued" if status == "rejected" else lead["queue_status"]
            conn.execute(
                """
                UPDATE leads
                SET approval_status = ?,
                    queue_status = ?,
                    scheduled_at = CASE WHEN ? = 'rejected' THEN '' ELSE scheduled_at END,
                    updated_at = ?
                WHERE id = ?
                """,
                (status, queue_status, status, utc_now_iso(), lead_id),
            )
            log_send_event(
                conn,
                campaign_id=int(lead["campaign_id"]),
                lead_id=lead_id,
                event_type="approval",
                status=status,
                details={},
            )
            conn.commit()
        finally:
            conn.close()

        if status == "approved":
            schedule_campaign(int(lead["campaign_id"]))
            prepare_upcoming_drafts(campaign_id=int(lead["campaign_id"]))
        self._json_response({"ok": True})

    def handle_regenerate(self, data: dict[str, Any]) -> None:
        lead_id = int(data.get("id") or 0)
        if lead_id <= 0:
            self._json_response({"error": "Lead id is required."}, 400)
            return
        conn = get_db()
        try:
            lead = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
            if not lead:
                self._json_response({"error": "Lead not found."}, 404)
                return
            conn.execute(
                """
                UPDATE leads
                SET draft_status = 'pending',
                    draft_message = '',
                    draft_error = '',
                    personalization_json = '{}',
                    queue_status = CASE WHEN queue_status = 'queued' THEN 'not_queued' ELSE queue_status END,
                    scheduled_at = CASE WHEN queue_status = 'queued' THEN '' ELSE scheduled_at END,
                    updated_at = ?
                WHERE id = ?
                """,
                (utc_now_iso(), lead_id),
            )
            log_send_event(
                conn,
                campaign_id=int(lead["campaign_id"]),
                lead_id=lead_id,
                event_type="draft",
                status="requeued",
                details={},
            )
            conn.commit()
        finally:
            conn.close()
        schedule_campaign(int(lead["campaign_id"]))
        prepare_upcoming_drafts(campaign_id=int(lead["campaign_id"]))
        self._json_response({"ok": True})

    def handle_send_now(self, data: dict[str, Any]) -> None:
        lead_id = int(data.get("id") or 0)
        if lead_id <= 0:
            self._json_response({"error": "Lead id is required."}, 400)
            return
        ok, message = send_lead_now(lead_id)
        if ok:
            self._json_response({"ok": True, "message": message})
        else:
            self._json_response({"error": message}, 400)

    def handle_clear_all(self) -> None:
        conn = get_db()
        try:
            conn.execute("DELETE FROM send_logs")
            conn.execute("DELETE FROM leads")
            conn.execute("DELETE FROM campaigns")
            conn.commit()
        finally:
            conn.close()

        for path in UPLOAD_DIR.glob("*"):
            try:
                if path.is_file():
                    path.unlink()
            except Exception:
                pass

        try:
            if LOG_PATH.exists():
                LOG_PATH.unlink()
        except Exception:
            pass

        self._json_response({"ok": True})

    def _json_response(self, data: dict[str, Any], status_code: int = 200) -> None:
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def log_message(self, format: str, *args: Any) -> None:
        return


def main() -> None:
    ensure_workers_started()
    port = 5060
    server = HTTPServer(("0.0.0.0", port), PraneWhatsAppHandler)
    print(f"Prane WhatsApp dashboard running at http://localhost:{port}")
    print("Draft generation worker and daytime sender are active.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping dashboard.")
        server.server_close()


if __name__ == "__main__":
    main()
