"""
SQLite database for job deduplication, lead storage, and dashboard data.
"""

import os
import json
import sqlite3
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'leads.db')

_conn = None


def init_db():
    """Initialize the database and create tables if they don't exist."""
    global _conn
    _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode = WAL")

    _conn.executescript("""
        CREATE TABLE IF NOT EXISTS seen_jobs (
            url_hash TEXT PRIMARY KEY,
            title    TEXT,
            seen_at  TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS leads (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            url_hash   TEXT,
            job_title  TEXT,
            job_url    TEXT,
            payload    TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS scraped_jobs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            cycle       INTEGER,
            title       TEXT,
            job_url     TEXT,
            posted_time TEXT,
            budget      TEXT,
            skills      TEXT,
            description TEXT,
            ai_status   TEXT DEFAULT 'pending',
            ai_result   TEXT DEFAULT '{}',
            ai_error    TEXT DEFAULT '',
            scraped_at  TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS run_status (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            cycle        INTEGER,
            status       TEXT DEFAULT 'running',
            started_at   TEXT DEFAULT (datetime('now')),
            completed_at TEXT,
            jobs_found   INTEGER DEFAULT 0,
            jobs_new     INTEGER DEFAULT 0,
            leads_found  INTEGER DEFAULT 0,
            error_msg    TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS outreach_results (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id         INTEGER,
            grok_response   TEXT DEFAULT '',
            contacts_json   TEXT DEFAULT '{}',
            email_subject   TEXT DEFAULT '',
            email_body      TEXT DEFAULT '',
            emails_sent_to  TEXT DEFAULT '[]',
            send_status     TEXT DEFAULT 'pending',
            skipped_reason  TEXT DEFAULT '',
            created_at      TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (lead_id) REFERENCES leads(id)
        );

        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT DEFAULT ''
        );
    """)
    _conn.commit()
    return _conn


def cleanup_old_data():
    """Delete records older than 24 hours."""
    _conn.execute("DELETE FROM seen_jobs WHERE seen_at < datetime('now', '-24 hours')")
    _conn.execute("DELETE FROM leads WHERE created_at < datetime('now', '-24 hours')")
    _conn.execute("DELETE FROM scraped_jobs WHERE scraped_at < datetime('now', '-24 hours')")
    _conn.execute("DELETE FROM run_status WHERE started_at < datetime('now', '-24 hours')")
    _conn.commit()


def get_connection():
    """Get the database connection (initialize if needed)."""
    global _conn
    if _conn is None:
        init_db()
    return _conn


def _hash_url(url: str) -> str:
    """Simple hash for dedup — matches the JS implementation."""
    h = 0
    for ch in url:
        h = ((31 * h) + ord(ch)) & 0xFFFFFFFF
        if h >= 0x80000000:
            h -= 0x100000000
    return str(h)


def is_job_seen(job_url: str) -> bool:
    """Check if we've already processed this job URL."""
    url_hash = _hash_url(job_url)
    row = _conn.execute(
        'SELECT 1 FROM seen_jobs WHERE url_hash = ?', (url_hash,)
    ).fetchone()
    return row is not None


def mark_job_seen(job_url: str, title: str):
    """Mark a job URL as seen."""
    url_hash = _hash_url(job_url)
    _conn.execute(
        'INSERT OR IGNORE INTO seen_jobs (url_hash, title) VALUES (?, ?)',
        (url_hash, title)
    )
    _conn.commit()


def save_lead(job_url: str, job_title: str, payload: dict) -> int:
    """Save a discovered lead to the database. Returns the lead row ID."""
    url_hash = _hash_url(job_url)
    cur = _conn.execute(
        'INSERT INTO leads (url_hash, job_title, job_url, payload) VALUES (?, ?, ?, ?)',
        (url_hash, job_title, job_url, json.dumps(payload))
    )
    _conn.commit()
    return cur.lastrowid


def get_recent_leads(limit: int = 20) -> list:
    """Get the most recent leads."""
    rows = _conn.execute(
        'SELECT * FROM leads ORDER BY created_at DESC LIMIT ?', (limit,)
    ).fetchall()
    return [
        {**dict(r), 'payload': json.loads(r['payload'])} for r in rows
    ]


def get_stats() -> dict:
    """Get total counts for seen jobs and leads."""
    total_seen = _conn.execute('SELECT COUNT(*) as c FROM seen_jobs').fetchone()['c']
    total_leads = _conn.execute('SELECT COUNT(*) as c FROM leads').fetchone()['c']
    return {'total_seen': total_seen, 'total_leads': total_leads}


# ── Run status tracking ──────────────────────────────────────────────

def start_run(cycle: int) -> int:
    """Record the start of a scrape cycle. Returns the run ID."""
    cur = _conn.execute(
        'INSERT INTO run_status (cycle, status) VALUES (?, ?)',
        (cycle, 'running')
    )
    _conn.commit()
    return cur.lastrowid


def update_run_progress(run_id: int, jobs_found: int = None, jobs_new: int = None, leads_found: int = None):
    """Update a running cycle with live progress counters."""
    fields = []
    values = []
    if jobs_found is not None:
        fields.append("jobs_found=?")
        values.append(jobs_found)
    if jobs_new is not None:
        fields.append("jobs_new=?")
        values.append(jobs_new)
    if leads_found is not None:
        fields.append("leads_found=?")
        values.append(leads_found)
    if not fields:
        return

    values.append(run_id)
    _conn.execute(
        f"UPDATE run_status SET {', '.join(fields)} WHERE id=?",
        tuple(values),
    )
    _conn.commit()


def complete_run(run_id: int, jobs_found: int, jobs_new: int, leads_found: int):
    """Mark a run as completed."""
    _conn.execute(
        '''UPDATE run_status 
           SET status='completed', completed_at=datetime('now'),
               jobs_found=?, jobs_new=?, leads_found=?
           WHERE id=?''',
        (jobs_found, jobs_new, leads_found, run_id)
    )
    _conn.commit()


def fail_run(run_id: int, error_msg: str):
    """Mark a run as failed."""
    _conn.execute(
        '''UPDATE run_status 
           SET status='error', completed_at=datetime('now'), error_msg=?
           WHERE id=?''',
        (error_msg, run_id)
    )
    _conn.commit()


# ── Scraped jobs tracking ────────────────────────────────────────────

def save_scraped_job(cycle: int, job: dict) -> int:
    """Save a scraped job to the dashboard table. Returns job row ID."""
    cur = _conn.execute(
        '''INSERT INTO scraped_jobs 
           (cycle, title, job_url, posted_time, budget, skills, description)
           VALUES (?, ?, ?, ?, ?, ?, ?)''',
        (cycle, job.get('title', ''), job.get('job_url', ''),
         job.get('posted_time', ''), job.get('budget', ''),
         job.get('skills', ''), job.get('description', ''))
    )
    _conn.commit()
    return cur.lastrowid


def update_job_ai_status(job_db_id: int, status: str, result: dict = None, error: str = ''):
    """Update the AI analysis status for a scraped job."""
    _conn.execute(
        '''UPDATE scraped_jobs 
           SET ai_status=?, ai_result=?, ai_error=?
           WHERE id=?''',
        (status, json.dumps(result or {}), error, job_db_id)
    )
    _conn.commit()


# ── Dashboard queries ────────────────────────────────────────────────

def get_dashboard_data() -> dict:
    """Get all data needed for the dashboard."""
    # Last run
    last_run = _conn.execute(
        'SELECT * FROM run_status ORDER BY id DESC LIMIT 1'
    ).fetchone()

    # Recent runs
    recent_runs = _conn.execute(
        'SELECT * FROM run_status ORDER BY id DESC LIMIT 10'
    ).fetchall()

    # Recent scraped jobs (last 50)
    recent_jobs = _conn.execute(
        '''SELECT * FROM scraped_jobs ORDER BY id DESC LIMIT 50'''
    ).fetchall()

    # Stats
    stats = get_stats()
    total_scraped = _conn.execute('SELECT COUNT(*) as c FROM scraped_jobs').fetchone()['c']
    total_leads_found = _conn.execute(
        "SELECT COUNT(*) as c FROM scraped_jobs WHERE ai_status='lead_found'"
    ).fetchone()['c']
    total_errors = _conn.execute(
        "SELECT COUNT(*) as c FROM scraped_jobs WHERE ai_status='error'"
    ).fetchone()['c']

    return {
        'last_run': dict(last_run) if last_run else None,
        'recent_runs': [dict(r) for r in recent_runs],
        'recent_jobs': [
            {**dict(j), 'ai_result': json.loads(j['ai_result'] or '{}')}
            for j in recent_jobs
        ],
        'stats': {
            **stats,
            'total_scraped': total_scraped,
            'total_leads_found': total_leads_found,
            'total_errors': total_errors,
        },
    }


# ── Outreach tracking ────────────────────────────────────────────────

def save_outreach_result(
    lead_id: int,
    grok_response: str = '',
    contacts: dict = None,
    email_subject: str = '',
    email_body: str = '',
    emails_sent_to: list = None,
    send_status: str = 'pending',
    skipped_reason: str = '',
) -> int:
    """Save an outreach result for a lead. Returns the row ID."""
    cur = _conn.execute(
        '''INSERT INTO outreach_results
           (lead_id, grok_response, contacts_json, email_subject,
            email_body, emails_sent_to, send_status, skipped_reason)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
        (
            lead_id,
            grok_response,
            json.dumps(contacts or {}),
            email_subject,
            email_body,
            json.dumps(emails_sent_to or []),
            send_status,
            skipped_reason,
        )
    )
    _conn.commit()
    return cur.lastrowid


def update_outreach_status(outreach_id: int, send_status: str, emails_sent_to: list = None):
    """Update the send status of an outreach result."""
    if emails_sent_to is not None:
        _conn.execute(
            'UPDATE outreach_results SET send_status=?, emails_sent_to=? WHERE id=?',
            (send_status, json.dumps(emails_sent_to), outreach_id)
        )
    else:
        _conn.execute(
            'UPDATE outreach_results SET send_status=? WHERE id=?',
            (send_status, outreach_id)
        )
    _conn.commit()


def get_outreach_for_lead(lead_id: int) -> dict:
    """Get outreach result for a specific lead."""
    row = _conn.execute(
        'SELECT * FROM outreach_results WHERE lead_id = ? ORDER BY id DESC LIMIT 1',
        (lead_id,)
    ).fetchone()
    if row:
        result = dict(row)
        result['contacts_json'] = json.loads(result.get('contacts_json', '{}'))
        result['emails_sent_to'] = json.loads(result.get('emails_sent_to', '[]'))
        return result
    return None


def get_all_outreach_results() -> list:
    """Get all outreach results."""
    rows = _conn.execute(
        'SELECT * FROM outreach_results ORDER BY created_at DESC LIMIT 50'
    ).fetchall()
    results = []
    for row in rows:
        r = dict(row)
        r['contacts_json'] = json.loads(r.get('contacts_json', '{}'))
        r['emails_sent_to'] = json.loads(r.get('emails_sent_to', '[]'))
        results.append(r)
    return results


# ── Settings ─────────────────────────────────────────────────────────

def save_setting(key: str, value: str):
    """Save a setting to the database."""
    _conn.execute(
        'INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)',
        (key, value)
    )
    _conn.commit()


def get_setting(key: str, default: str = '') -> str:
    """Get a setting from the database."""
    row = _conn.execute(
        'SELECT value FROM settings WHERE key = ?', (key,)
    ).fetchone()
    return row['value'] if row else default
