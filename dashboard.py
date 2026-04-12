"""
Live Dashboard Server for Upwork Lead Hunter.
Auto-started by main.py, also runnable standalone.

Usage:  python3 dashboard.py
Opens:  http://localhost:5050

Email delivery is handled via Gmail API (see outreach_mailer.py).
"""

import json
import sqlite3
import os
import sys
from collections import deque
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# Fix Windows console encoding for emoji/unicode characters
if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'leads.db')
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'workflow.log')


def _ensure_schema(conn):
    """Add missing columns/tables (safe to call repeatedly)."""
    try:
        cols = [r[1] for r in conn.execute('PRAGMA table_info(leads)').fetchall()]
        if 'outreached' not in cols:
            conn.execute('ALTER TABLE leads ADD COLUMN outreached INTEGER DEFAULT 0')
            conn.commit()
    except Exception:
        pass
    # Ensure outreach_results table exists
    try:
        conn.execute('''CREATE TABLE IF NOT EXISTS outreach_results (
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
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT DEFAULT ''
        )''')
        conn.commit()
    except Exception:
        pass


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    return conn


def _parse_log_timestamp(value):
    if not value:
        return None
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M:%S.%f'):
        try:
            return datetime.strptime(value, fmt)
        except Exception:
            continue
    return None


def _normalize_log_entry(entry):
    if not isinstance(entry, dict):
        return {
            'timestamp': '',
            'cycle': None,
            'stage': 'raw',
            'message': str(entry),
            'fields': {},
        }

    base_keys = {'timestamp', 'cycle', 'stage', 'message'}
    return {
        'timestamp': entry.get('timestamp', ''),
        'cycle': entry.get('cycle'),
        'stage': entry.get('stage', 'raw'),
        'message': entry.get('message', ''),
        'fields': {k: v for k, v in entry.items() if k not in base_keys},
    }


def get_recent_logs(hours=24, limit=600):
    cutoff = datetime.now() - timedelta(hours=hours)
    entries = deque(maxlen=limit)
    scanned = 0

    if not os.path.exists(LOG_PATH):
        return {
            'entries': [],
            'count': 0,
            'truncated': False,
            'hours': hours,
        }

    try:
        with open(LOG_PATH, 'r', encoding='utf-8', errors='replace') as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line:
                    continue

                try:
                    payload = json.loads(line)
                except Exception:
                    payload = {'timestamp': '', 'stage': 'raw', 'message': line}

                ts = _parse_log_timestamp(payload.get('timestamp'))
                if ts and ts < cutoff:
                    break

                entries.append(_normalize_log_entry(payload))
                scanned += 1
    except Exception as e:
        return {
            'entries': [],
            'count': 0,
            'truncated': False,
            'hours': hours,
            'error': str(e),
        }

    return {
        'entries': list(entries),
        'count': scanned,
        'truncated': scanned > limit,
        'hours': hours,
    }


def get_dashboard_json():
    conn = get_db()
    try:
        last_run = conn.execute('SELECT * FROM run_status ORDER BY id DESC LIMIT 1').fetchone()
        recent_runs = conn.execute('SELECT * FROM run_status ORDER BY id DESC LIMIT 10').fetchall()
        recent_jobs = conn.execute('SELECT * FROM scraped_jobs ORDER BY id DESC LIMIT 60').fetchall()

        total_seen = conn.execute('SELECT COUNT(*) as c FROM seen_jobs').fetchone()['c']
        total_leads = conn.execute('SELECT COUNT(*) as c FROM leads').fetchone()['c']
        total_scraped = conn.execute('SELECT COUNT(*) as c FROM scraped_jobs').fetchone()['c']
        total_leads_found = conn.execute("SELECT COUNT(*) as c FROM scraped_jobs WHERE ai_status='lead_found'").fetchone()['c']
        total_errors = conn.execute("SELECT COUNT(*) as c FROM scraped_jobs WHERE ai_status='error'").fetchone()['c']
        total_no_lead = conn.execute("SELECT COUNT(*) as c FROM scraped_jobs WHERE ai_status='no_lead'").fetchone()['c']
        total_pending = conn.execute("SELECT COUNT(*) as c FROM scraped_jobs WHERE ai_status='pending'").fetchone()['c']
        total_skipped = conn.execute("SELECT COUNT(*) as c FROM scraped_jobs WHERE ai_status='skipped'").fetchone()['c']

        recent_leads = conn.execute('SELECT * FROM leads ORDER BY created_at DESC LIMIT 200').fetchall()

        # Outreach data
        outreach_results = conn.execute('SELECT * FROM outreach_results ORDER BY created_at DESC LIMIT 200').fetchall()
        total_emails_sent = conn.execute(
            "SELECT COUNT(*) as c FROM outreach_results WHERE send_status='sent' OR send_status='partial'"
        ).fetchone()['c']
        total_outreach_skipped = conn.execute(
            "SELECT COUNT(*) as c FROM outreach_results WHERE send_status='skipped'"
        ).fetchone()['c']

        # Build outreach map by lead_id
        outreach_map = {}
        for o in outreach_results:
            o_dict = dict(o)
            o_dict['contacts_json'] = json.loads(o_dict.get('contacts_json', '{}'))
            o_dict['emails_sent_to'] = json.loads(o_dict.get('emails_sent_to', '[]'))
            lead_id = o_dict.get('lead_id')
            if lead_id and lead_id not in outreach_map:
                outreach_map[lead_id] = o_dict

        return json.dumps({
            'last_run': dict(last_run) if last_run else None,
            'recent_runs': [dict(r) for r in recent_runs],
            'recent_jobs': [{**dict(j), 'ai_result': json.loads(j['ai_result'] or '{}')} for j in recent_jobs],
            'recent_leads': [{**dict(r), 'payload': json.loads(r['payload'] or '{}')} for r in recent_leads],
            'outreach_map': outreach_map,
            'stats': {
                'total_seen': total_seen, 'total_leads': total_leads,
                'total_scraped': total_scraped, 'total_leads_found': total_leads_found,
                'total_errors': total_errors, 'total_no_lead': total_no_lead,
                'total_pending': total_pending, 'total_skipped': total_skipped,
                'total_emails_sent': total_emails_sent,
                'total_outreach_skipped': total_outreach_skipped,
            },
        })
    except Exception as e:
        return json.dumps({'error': str(e)})
    finally:
        conn.close()


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Lead Hunter — Dashboard</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#000;--surface:#0a0a0a;--card:rgba(255,255,255,.04);--card-hover:rgba(255,255,255,.08);
  --border:rgba(255,255,255,.1);--border-bright:rgba(255,255,255,.25);
  --text:#fff;--text-secondary:#999;--text-muted:#555;
  --accent:#fff;--success:#fff;--error:#888;
  --green:rgba(34,197,94,.85);--green-bg:rgba(34,197,94,.1);--green-border:rgba(34,197,94,.4);
  --blue:rgba(59,130,246,.85);--blue-bg:rgba(59,130,246,.1);--blue-border:rgba(59,130,246,.4);
  --orange:rgba(249,115,22,.85);--orange-bg:rgba(249,115,22,.1);--orange-border:rgba(249,115,22,.4);
}
body{font-family:'Inter',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;-webkit-font-smoothing:antialiased}

/* Grain overlay */
body::before{content:'';position:fixed;inset:0;z-index:0;pointer-events:none;opacity:.03;
  background-image:url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='1'/%3E%3C/svg%3E")}

.container{max-width:1400px;margin:0 auto;padding:20px;position:relative;z-index:1}

/* Header */
.header{display:flex;align-items:center;justify-content:space-between;padding:20px 0 24px;border-bottom:1px solid var(--border);margin-bottom:28px}
.header h1{font-size:20px;font-weight:800;letter-spacing:-.5px;text-transform:uppercase}
.header .subtitle{color:var(--text-muted);font-size:11px;margin-top:4px;font-family:'JetBrains Mono',monospace;letter-spacing:1px;text-transform:uppercase}
.live-badge{display:flex;align-items:center;gap:8px;padding:6px 16px;border-radius:100px;border:1px solid var(--border-bright);font-size:11px;font-weight:700;letter-spacing:2px;text-transform:uppercase;font-family:'JetBrains Mono',monospace}
.live-dot{width:6px;height:6px;border-radius:50%;background:#fff;animation:blink 2s ease-in-out infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.2}}

/* Gmail badge */
.gmail-badge{display:inline-flex;align-items:center;gap:8px;padding:6px 16px;border-radius:100px;border:1px solid var(--green-border);font-size:10px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;font-family:'JetBrains Mono',monospace;color:var(--green);margin-bottom:20px}

/* Stats */
.stats-grid{display:grid;grid-template-columns:repeat(7,1fr);gap:12px;margin-bottom:24px}
.stat-card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:16px 18px;transition:all .2s ease;position:relative;overflow:hidden}
.stat-card:hover{border-color:var(--border-bright);background:var(--card-hover);transform:translateY(-2px)}
.stat-card .label{font-size:10px;color:var(--text-muted);text-transform:uppercase;letter-spacing:1.5px;font-weight:600;font-family:'JetBrains Mono',monospace;margin-bottom:8px}
.stat-card .value{font-size:28px;font-weight:900;letter-spacing:-1px}
.stat-card .value.highlight{text-shadow:0 0 20px rgba(255,255,255,.3)}
.stat-card .value.green{color:var(--green)}

/* Run Banner */
.run-banner{display:flex;align-items:center;gap:24px;background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px 24px;margin-bottom:28px;flex-wrap:wrap}
.run-banner .run-item{display:flex;flex-direction:column;gap:2px}
.run-banner .run-label{font-size:9px;color:var(--text-muted);text-transform:uppercase;letter-spacing:1.5px;font-family:'JetBrains Mono',monospace}
.run-banner .run-value{font-size:14px;font-weight:600}
.status-badge{display:inline-flex;align-items:center;gap:6px;padding:4px 12px;border-radius:100px;font-size:10px;font-weight:700;letter-spacing:1px;text-transform:uppercase;font-family:'JetBrains Mono',monospace;border:1px solid}
.status-running{border-color:var(--border-bright);color:var(--text)}
.status-completed{border-color:var(--border-bright);color:var(--text)}
.status-error{border-color:var(--text-muted);color:var(--text-muted)}

/* Section Headers */
.section-header{display:flex;align-items:baseline;justify-content:space-between;margin-bottom:14px}
.section-header h2{font-size:13px;font-weight:800;text-transform:uppercase;letter-spacing:2px;font-family:'JetBrains Mono',monospace}
.section-header .count{font-size:11px;color:var(--text-muted);font-family:'JetBrains Mono',monospace}

/* Jobs Table (desktop) */
.jobs-table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--border);border-radius:10px;overflow:hidden;margin-bottom:32px}
.jobs-table thead{background:rgba(255,255,255,.03)}
.jobs-table th{text-align:left;padding:12px 16px;font-size:9px;color:var(--text-muted);text-transform:uppercase;letter-spacing:1.5px;font-weight:700;border-bottom:1px solid var(--border);font-family:'JetBrains Mono',monospace}
.jobs-table td{padding:12px 16px;font-size:13px;border-bottom:1px solid rgba(255,255,255,.04);vertical-align:middle}
.jobs-table tr:last-child td{border-bottom:none}
.jobs-table tr:hover{background:var(--card-hover)}
.job-title{font-weight:500;max-width:320px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.job-title a{color:var(--text);text-decoration:none;transition:opacity .15s}
.job-title a:hover{opacity:.7;text-decoration:underline}

/* AI Status Badges */
.ai-badge{display:inline-flex;align-items:center;gap:5px;padding:4px 12px;border-radius:100px;font-size:10px;font-weight:700;white-space:nowrap;letter-spacing:.5px;font-family:'JetBrains Mono',monospace;border:1px solid}
.ai-lead_found{border-color:#fff;color:#fff;background:rgba(255,255,255,.1)}
.ai-no_lead{border-color:var(--text-muted);color:var(--text-muted);background:transparent}
.ai-error{border-color:var(--text-muted);color:var(--text-muted);background:transparent}
.ai-pending{border-color:var(--text-secondary);color:var(--text-secondary);background:transparent}
.ai-skipped{border-color:var(--text-muted);color:var(--text-muted);background:transparent}
.ai-analyzing{border-color:var(--text-secondary);color:var(--text-secondary);background:transparent}
.posted-time{color:var(--text-muted);font-size:12px;white-space:nowrap;font-family:'JetBrains Mono',monospace}
.cycle-num{color:var(--text-muted);font-size:12px;text-align:center;font-family:'JetBrains Mono',monospace}
.btn-info{padding:5px 14px;border-radius:100px;border:1px solid var(--border-bright);background:transparent;color:var(--text);font-size:10px;font-weight:700;cursor:pointer;transition:all .15s;white-space:nowrap;letter-spacing:.5px;text-transform:uppercase;font-family:'JetBrains Mono',monospace}
.btn-info:hover{background:#fff;color:#000}
.btn-outreached{padding:5px 14px;border-radius:100px;border:1px solid rgba(34,197,94,.5);background:transparent;color:rgba(34,197,94,.85);font-size:10px;font-weight:700;cursor:pointer;transition:all .15s;white-space:nowrap;letter-spacing:.5px;text-transform:uppercase;font-family:'JetBrains Mono',monospace}
.btn-outreached:hover{background:rgba(34,197,94,1);color:#000;border-color:rgba(34,197,94,1)}
.btn-outreached.active{background:rgba(34,197,94,.15);border-color:rgba(34,197,94,.7);color:rgba(34,197,94,1);cursor:default}
.btn-delete{padding:5px 14px;border-radius:100px;border:1px solid rgba(239,68,68,.5);background:transparent;color:rgba(239,68,68,.85);font-size:10px;font-weight:700;cursor:pointer;transition:all .15s;white-space:nowrap;letter-spacing:.5px;text-transform:uppercase;font-family:'JetBrains Mono',monospace}
.btn-delete:hover{background:rgba(239,68,68,1);color:#fff;border-color:rgba(239,68,68,1)}
.btn-danger-big{padding:8px 20px;border-radius:100px;border:1px solid rgba(239,68,68,.4);background:transparent;color:rgba(239,68,68,.85);font-size:11px;font-weight:700;cursor:pointer;transition:all .15s;white-space:nowrap;letter-spacing:1px;text-transform:uppercase;font-family:'JetBrains Mono',monospace}
.btn-danger-big:hover{background:rgba(239,68,68,1);color:#fff;border-color:rgba(239,68,68,1)}
.lead-actions{display:flex;gap:8px;margin-top:14px;flex-wrap:wrap}

/* Leads */
.lead-card{background:var(--card);border:1px solid var(--border-bright);border-radius:10px;padding:20px;margin-bottom:12px;transition:all .2s}
.lead-card:hover{border-color:#fff;background:var(--card-hover)}
.lead-card h3{font-size:14px;font-weight:700;margin-bottom:8px}
.lead-meta{font-size:11px;color:var(--text-muted);margin-bottom:14px;font-family:'JetBrains Mono',monospace}
.lead-meta a{color:var(--text-secondary);text-decoration:none}
.lead-meta a:hover{color:#fff}
.lead-info{display:grid;grid-template-columns:1fr 1fr;gap:10px;font-size:13px}
.lead-info .k{color:var(--text-muted);font-size:9px;text-transform:uppercase;letter-spacing:1px;font-family:'JetBrains Mono',monospace}
.lead-info .v{color:var(--text);font-weight:500}
.lead-msg{margin-top:14px;padding:14px;background:rgba(255,255,255,.03);border-radius:8px;font-size:13px;line-height:1.7;border-left:2px solid rgba(255,255,255,.3)}

/* Outreach section in lead card */
.outreach-section{margin-top:16px;padding:16px;border-radius:10px;border:1px solid var(--border);background:rgba(255,255,255,.02)}
.outreach-section h4{font-size:10px;text-transform:uppercase;letter-spacing:1.5px;font-family:'JetBrains Mono',monospace;margin-bottom:12px;color:var(--text-secondary)}
.outreach-status{display:inline-flex;align-items:center;gap:6px;padding:4px 14px;border-radius:100px;font-size:10px;font-weight:700;letter-spacing:.5px;font-family:'JetBrains Mono',monospace;border:1px solid;margin-bottom:10px}
.os-sent{border-color:var(--green-border);color:var(--green);background:var(--green-bg)}
.os-partial{border-color:var(--orange-border);color:var(--orange);background:var(--orange-bg)}
.os-pending,.os-no_webhook{border-color:var(--blue-border);color:var(--blue);background:var(--blue-bg)}
.os-skipped,.os-grok_failed,.os-no_emails,.os-no_verified_emails,.os-draft_failed,.os-send_failed{border-color:var(--text-muted);color:var(--text-muted);background:transparent}
.grok-box{margin-top:12px;font-size:11px}
.grok-box summary{cursor:pointer;color:var(--text-secondary);font-family:'JetBrains Mono',monospace;text-transform:uppercase;letter-spacing:1px;font-weight:700;margin-bottom:8px;user-select:none;transition:color .15s}
.grok-box summary:hover{color:#fff}
.grok-content{padding:12px;background:rgba(255,255,255,.02);border:1px solid var(--border);border-radius:6px;color:var(--text-muted);white-space:pre-wrap;line-height:1.6;max-height:200px;overflow-y:auto;font-family:'JetBrains Mono',monospace;font-size:10px}
.contact-badges{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
.contact-badge{display:inline-flex;align-items:center;gap:5px;padding:4px 12px;border-radius:100px;font-size:10px;font-weight:600;font-family:'JetBrains Mono',monospace;border:1px solid var(--border);color:var(--text-secondary);background:var(--card)}
.contact-badge.email{border-color:var(--green-border);color:var(--green)}
.contact-badge.linkedin{border-color:var(--blue-border);color:var(--blue)}
.contact-badge.instagram{border-color:var(--orange-border);color:var(--orange)}
.email-preview{margin-top:12px;padding:12px;background:rgba(255,255,255,.03);border-radius:8px;border-left:2px solid var(--green-border)}
.email-preview .ep-subject{font-size:12px;font-weight:600;margin-bottom:6px}
.email-preview .ep-body{font-size:12px;color:var(--text-secondary);line-height:1.6;white-space:pre-wrap}
.email-sent-list{margin-top:10px}
.email-sent-item{display:flex;align-items:center;gap:8px;font-size:11px;font-family:'JetBrains Mono',monospace;padding:4px 0}
.email-sent-item .sent-ok{color:var(--green)}
.email-sent-item .sent-fail{color:rgba(239,68,68,.85)}
.email-sent-item .sent-pending{color:var(--text-muted)}

/* Run History */
.runs-list{display:grid;gap:8px;margin-bottom:32px}
.run-row{display:grid;grid-template-columns:60px 120px 1fr 80px 80px 80px;align-items:center;gap:12px;background:var(--card);border:1px solid var(--border);border-radius:8px;padding:12px 18px;font-size:13px;transition:all .15s}
.run-row:hover{border-color:var(--border-bright);background:var(--card-hover)}
.run-row .run-time{color:var(--text-muted);font-size:11px;font-family:'JetBrains Mono',monospace}

/* Logs */
.log-panel{background:var(--card);border:1px solid var(--border);border-radius:12px;max-height:520px;overflow:auto;padding:8px}
.log-entry{padding:12px 14px;border-bottom:1px solid rgba(255,255,255,.05)}
.log-entry:last-child{border-bottom:none}
.log-head{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:6px}
.log-ts{color:var(--text-muted);font-size:10px;font-family:'JetBrains Mono',monospace}
.log-stage{display:inline-flex;align-items:center;padding:3px 8px;border-radius:999px;border:1px solid var(--border-bright);font-size:9px;font-family:'JetBrains Mono',monospace;text-transform:uppercase;letter-spacing:1px}
.log-cycle{color:var(--text-secondary);font-size:10px;font-family:'JetBrains Mono',monospace}
.log-msg{font-size:12px;line-height:1.5;color:var(--text)}
.log-fields{margin-top:8px;padding:10px;background:rgba(255,255,255,.03);border:1px solid var(--border);border-radius:8px;color:var(--text-muted);font-size:11px;line-height:1.6;white-space:pre-wrap;word-break:break-word;font-family:'JetBrains Mono',monospace}
.log-empty{padding:22px;color:var(--text-muted);text-align:center;border:1px dashed var(--border);border-radius:10px}

/* Modal */
.modal-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.85);backdrop-filter:blur(8px);z-index:1000;justify-content:center;align-items:flex-start;padding:40px 20px;overflow-y:auto}
.modal-overlay.active{display:flex}
.modal{background:#0a0a0a;border:1px solid var(--border-bright);border-radius:14px;width:100%;max-width:720px;padding:0;position:relative;box-shadow:0 0 80px rgba(255,255,255,.03);animation:modalIn .25s ease}
@keyframes modalIn{from{opacity:0;transform:translateY(20px) scale(.98)}to{opacity:1;transform:translateY(0) scale(1)}}
.modal-close{position:absolute;top:16px;right:16px;width:32px;height:32px;border-radius:50%;border:1px solid var(--border);background:transparent;color:var(--text-muted);font-size:16px;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:all .15s;z-index:2}
.modal-close:hover{background:#fff;color:#000;border-color:#fff}
.modal-header{padding:28px 28px 18px;border-bottom:1px solid var(--border)}
.modal-header h2{font-size:16px;font-weight:800;line-height:1.4;padding-right:40px}
.modal-header .modal-url{margin-top:8px}
.modal-header .modal-url a{color:var(--text-muted);font-size:11px;text-decoration:none;word-break:break-all;font-family:'JetBrains Mono',monospace}
.modal-header .modal-url a:hover{color:#fff}
.modal-body{padding:22px 28px 28px}
.modal-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:20px}
.modal-field .mf-label{font-size:9px;color:var(--text-muted);text-transform:uppercase;letter-spacing:1.5px;margin-bottom:4px;font-family:'JetBrains Mono',monospace}
.modal-field .mf-value{font-size:14px;font-weight:600}
.modal-desc-label{font-size:9px;color:var(--text-muted);text-transform:uppercase;letter-spacing:1.5px;margin-bottom:8px;font-family:'JetBrains Mono',monospace}
.modal-desc{background:rgba(255,255,255,.03);border:1px solid var(--border);border-radius:8px;padding:16px;font-size:13px;line-height:1.7;max-height:300px;overflow-y:auto;white-space:pre-wrap;word-break:break-word}
.modal-ai{margin-top:20px;padding:18px;border-radius:10px;border:1px solid var(--border)}
.modal-ai.lead{border-color:var(--border-bright);background:rgba(255,255,255,.04)}
.modal-ai.error{border-color:var(--text-muted);background:rgba(255,255,255,.02)}
.modal-ai h4{font-size:10px;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:12px;font-family:'JetBrains Mono',monospace}
.modal-ai-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;font-size:13px}
.modal-ai-grid .k{color:var(--text-muted);font-size:9px;font-family:'JetBrains Mono',monospace;text-transform:uppercase;letter-spacing:1px}
.modal-ai-grid .v{font-weight:500}
.modal-ai-msg{margin-top:12px;padding:12px;background:rgba(255,255,255,.03);border-radius:8px;font-size:13px;line-height:1.7;border-left:2px solid rgba(255,255,255,.3)}
.modal-ai-error{margin-top:8px;font-size:12px;color:var(--text-muted)}

/* Footer */
.footer{text-align:center;padding:28px;color:var(--text-muted);font-size:10px;border-top:1px solid var(--border);margin-top:40px;font-family:'JetBrains Mono',monospace;letter-spacing:1px;text-transform:uppercase}

/* ── RESPONSIVE ── */
@media(max-width:1024px){
  .stats-grid{grid-template-columns:repeat(4,1fr)}
}
@media(max-width:768px){
  .container{padding:14px}
  .header{flex-direction:column;align-items:flex-start;gap:12px}
  .stats-grid{grid-template-columns:repeat(2,1fr);gap:8px}
  .stat-card{padding:14px}
  .stat-card .value{font-size:22px}

  /* Table → Cards on mobile */
  .jobs-table thead{display:none}
  .jobs-table,.jobs-table tbody,.jobs-table tr,.jobs-table td{display:block;width:100%}
  .jobs-table{background:transparent;border:none}
  .jobs-table tr{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px;margin-bottom:8px}
  .jobs-table tr:hover{background:var(--card-hover)}
  .jobs-table td{padding:4px 0;border:none;font-size:13px}
  .jobs-table td:before{content:attr(data-label);display:block;font-size:9px;color:var(--text-muted);text-transform:uppercase;letter-spacing:1px;font-family:'JetBrains Mono',monospace;margin-bottom:2px}
  .job-title{max-width:100%;white-space:normal}

  .run-row{grid-template-columns:1fr 1fr;gap:8px}
  .run-banner{gap:14px;padding:14px}
  .lead-info{grid-template-columns:1fr}

  .modal{max-width:100%;border-radius:10px;margin:10px}
  .modal-grid{grid-template-columns:1fr}
  .modal-ai-grid{grid-template-columns:1fr}
  .modal-header{padding:20px 18px 14px}
  .modal-body{padding:16px 18px 20px}
}
@media(max-width:400px){
  .stats-grid{grid-template-columns:1fr 1fr;gap:6px}
  .stat-card .value{font-size:20px}
  .header h1{font-size:16px}
}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <div><h1>// Lead Hunter</h1><div class="subtitle">Live Dashboard &mdash; Auto-refresh 10s</div></div>
    <div style="display:flex;align-items:center;gap:12px">
      <button class="btn-danger-big" onclick="deleteAllData()">Delete All Data</button>
      <div class="live-badge"><div class="live-dot"></div><span id="live-text">Live</span></div>
    </div>
  </div>

  <!-- Gmail API Badge -->
  <div class="gmail-badge">&#9993; Gmail API</div>

  <div class="stats-grid" id="stats-grid"></div>
  <div class="run-banner" id="run-banner"></div>
  <div class="section-header"><h2>// Scraped Jobs</h2><span class="count" id="jobs-count"></span></div>
  <table class="jobs-table">
    <thead><tr><th>Cycle</th><th>Title</th><th>Posted</th><th>Status</th><th></th></tr></thead>
    <tbody id="jobs-body"></tbody>
  </table>
  <div id="jobs-toggle" style="text-align:center;margin:-20px 0 32px"></div>
  <div class="section-header"><h2>// Leads Found</h2><span class="count" id="leads-count"></span></div>
  <div id="leads-list"></div>
  <div id="leads-toggle" style="text-align:center;margin:4px 0 12px"></div>
  <div class="section-header" style="margin-top:32px"><h2>// Run History</h2></div>
  <div class="runs-list" id="runs-list"></div>
  <div class="section-header" style="margin-top:32px"><h2>// Logs (24h)</h2><span class="count" id="logs-count"></span></div>
  <div class="log-panel" id="logs-panel"></div>
  <div class="footer">Lead Hunter v2.0 &mdash; Python/Patchright &mdash; Femur Studio</div>
</div>

<!-- Job Detail Modal -->
<div class="modal-overlay" id="modal-overlay" onclick="if(event.target===this)closeModal()">
  <div class="modal">
    <button class="modal-close" onclick="closeModal()">&times;</button>
    <div class="modal-header">
      <h2 id="modal-title"></h2>
      <div class="modal-url"><a id="modal-url" href="#" target="_blank"></a></div>
    </div>
    <div class="modal-body">
      <div class="modal-grid">
        <div class="modal-field"><div class="mf-label">Posted</div><div class="mf-value" id="modal-posted"></div></div>
        <div class="modal-field"><div class="mf-label">Budget</div><div class="mf-value" id="modal-budget"></div></div>
        <div class="modal-field"><div class="mf-label">Skills</div><div class="mf-value" id="modal-skills"></div></div>
        <div class="modal-field"><div class="mf-label">Cycle</div><div class="mf-value" id="modal-cycle"></div></div>
        <div class="modal-field"><div class="mf-label">Scraped At</div><div class="mf-value" id="modal-scraped"></div></div>
        <div class="modal-field"><div class="mf-label">AI Status</div><div class="mf-value" id="modal-ai-status"></div></div>
      </div>
      <div class="modal-desc-label">Full Description</div>
      <div class="modal-desc" id="modal-desc"></div>
      <div id="modal-ai-section"></div>
    </div>
  </div>
</div>

<script>
const BASE_PATH = window.location.pathname.startsWith('/upwork') ? '/upwork' : '';
const API = BASE_PATH + '/api/data';
const API_LEAD_OUTREACH = BASE_PATH + '/api/lead/outreach';
const API_LEAD_DELETE = BASE_PATH + '/api/lead/delete';
const API_DELETE_ALL = BASE_PATH + '/api/delete-all';
const API_LOGS = BASE_PATH + '/api/logs?hours=24&limit=600';
let allJobs=[];
let showAllJobs=false;
let showAllLeads=false;
let _allLeadsData=[];
let _outreachMap={};

function esc(s){if(!s)return'';return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
function trunc(s,n){if(!s)return'\u2014';return s.length>n?s.slice(0,n)+'\u2026':s}
function fmtTime(t){if(!t)return'\u2014';try{const d=new Date(t+'Z');return d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'})}catch{return t}}
function fmtDT(t){if(!t)return'\u2014';try{const d=new Date(t+'Z');return d.toLocaleDateString([],{month:'short',day:'numeric'})+' '+d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})}catch{return t}}

const AI_LABELS={'lead_found':'\u25cf LEAD','no_lead':'\u2014 No Lead','error':'! Error','pending':'... Pending','skipped':'x Skipped','analyzing':'~ Analyzing'};
const OS_LABELS={'sent':'\u2713 Sent','partial':'\u26a0 Partial','pending':'... Pending','no_webhook':'! No Webhook','skipped':'\u23ed Skipped','grok_failed':'! Grok Failed','no_emails':'\u2014 No Emails','no_verified_emails':'\u2014 No Verified Emails','draft_failed':'! Draft Failed','send_failed':'! Send Failed'};



function openModal(idx){
  const j=allJobs[idx]; if(!j)return;
  document.getElementById('modal-title').textContent=j.title||'\u2014';
  const urlEl=document.getElementById('modal-url');urlEl.href=j.job_url||'#';urlEl.textContent=j.job_url||'\u2014';
  document.getElementById('modal-posted').textContent=j.posted_time||'\u2014';
  document.getElementById('modal-budget').textContent=j.budget||'\u2014';
  document.getElementById('modal-skills').textContent=j.skills||'\u2014';
  document.getElementById('modal-cycle').textContent='#'+(j.cycle||'-');
  document.getElementById('modal-scraped').textContent=fmtDT(j.scraped_at);
  const aiStatusEl=document.getElementById('modal-ai-status');
  aiStatusEl.innerHTML=`<span class="ai-badge ai-${j.ai_status||'pending'}">${AI_LABELS[j.ai_status]||j.ai_status}</span>`;
  document.getElementById('modal-desc').textContent=j.description||'No description available';

  const aiSec=document.getElementById('modal-ai-section');
  const r=j.ai_result||{};
  const ci=r.client_info||{};
  const cs=r.contact_strategy||{};
  if(j.ai_status==='lead_found'&&ci.company_name){
    aiSec.innerHTML=`<div class="modal-ai lead"><h4>\u25cf Lead Intelligence</h4><div class="modal-ai-grid">
      <div><div class="k">Company</div><div class="v">${esc(ci.company_name)}</div></div>
      <div><div class="k">Contact</div><div class="v">${esc(ci.guessed_person||'\u2014')}</div></div>
      <div><div class="k">Website</div><div class="v">${esc(ci.website||'\u2014')}</div></div>
      <div><div class="k">Search Query</div><div class="v">${esc(ci.search_query_used||'\u2014')}</div></div>
      <div><div class="k">Confidence</div><div class="v">${esc(r.confidence_score||'\u2014')}</div></div>
    </div>${cs.cold_outreach_message?`<div class="modal-ai-msg"><strong>Suggested Outreach:</strong><br>${esc(cs.cold_outreach_message)}</div>`:''}</div>`;
  } else if(j.ai_error){
    aiSec.innerHTML=`<div class="modal-ai error"><h4>! AI Error</h4><div class="modal-ai-error">${esc(j.ai_error)}</div></div>`;
  } else if(j.ai_status==='no_lead'){
    aiSec.innerHTML=`<div class="modal-ai"><h4>// No Lead</h4>${r.reason?`<div class="modal-ai-msg"><strong>Reason:</strong> ${esc(r.reason)}</div>`:`<pre style="font-size:12px;color:var(--text-muted);margin-top:8px;white-space:pre-wrap;font-family:'JetBrains Mono',monospace">${esc(JSON.stringify(r, null, 2))}</pre>`}</div>`;
  } else { aiSec.innerHTML=`<div class="modal-ai"><h4>// AI Response</h4><pre style="font-size:12px;color:var(--text-muted);margin-top:8px;white-space:pre-wrap;font-family:'JetBrains Mono',monospace">${esc(JSON.stringify(r, null, 2))}</pre></div>`; }

  document.getElementById('modal-overlay').classList.add('active');
  document.body.style.overflow='hidden';
}
function closeModal(){
  document.getElementById('modal-overlay').classList.remove('active');
  document.body.style.overflow='';
}
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeModal()});

function renderStats(s){
  document.getElementById('stats-grid').innerHTML=`
    <div class="stat-card"><div class="label">Scraped</div><div class="value">${s.total_scraped}</div></div>
    <div class="stat-card"><div class="label">Leads</div><div class="value highlight">${s.total_leads_found}</div></div>
    <div class="stat-card"><div class="label">No Lead</div><div class="value">${s.total_no_lead}</div></div>
    <div class="stat-card"><div class="label">Errors</div><div class="value">${s.total_errors}</div></div>
    <div class="stat-card"><div class="label">Pending</div><div class="value">${s.total_pending}</div></div>
    <div class="stat-card"><div class="label">Skipped</div><div class="value">${s.total_skipped}</div></div>
    <div class="stat-card"><div class="label">Emails Sent</div><div class="value green">${s.total_emails_sent||0}</div></div>`;
}
function renderRunBanner(r){
  const b=document.getElementById('run-banner');
  if(!r){b.innerHTML='<span style="color:var(--text-muted)">No runs yet</span>';return}
  const sc=r.status==='running'?'status-running':r.status==='error'?'status-error':'status-completed';
  b.innerHTML=`
    <div class="run-item"><span class="run-label">Cycle</span><span class="run-value">#${r.cycle}</span></div>
    <div class="run-item"><span class="run-label">Status</span><span class="status-badge ${sc}">${r.status.toUpperCase()}</span></div>
    <div class="run-item"><span class="run-label">Started</span><span class="run-value">${fmtTime(r.started_at)}</span></div>
    <div class="run-item"><span class="run-label">Completed</span><span class="run-value">${fmtTime(r.completed_at)}</span></div>
    <div class="run-item"><span class="run-label">Jobs</span><span class="run-value">${r.jobs_found}</span></div>
    <div class="run-item"><span class="run-label">New</span><span class="run-value">${r.jobs_new}</span></div>
    <div class="run-item"><span class="run-label">Leads</span><span class="run-value">${r.leads_found}</span></div>
    ${r.error_msg?`<div class="run-item" style="grid-column:1/-1"><span class="run-label">Error</span><span class="run-value" style="color:var(--text-muted)">${esc(r.error_msg)}</span></div>`:''}`;
}
function toggleJobs(){showAllJobs=!showAllJobs;renderJobs(allJobs)}
function toggleLeads(){showAllLeads=!showAllLeads;renderLeadsView()}
function renderJobs(jobs){
  allJobs=jobs;
  document.getElementById('jobs-count').textContent=`${jobs.length} total`;
  const tbody=document.getElementById('jobs-body');
  const tog=document.getElementById('jobs-toggle');
  if(!jobs.length){tbody.innerHTML='<tr><td colspan="5" style="text-align:center;color:var(--text-muted);padding:40px">No jobs scraped yet</td></tr>';tog.innerHTML='';return}
  const visible=showAllJobs?jobs:jobs.slice(0,10);
  tbody.innerHTML=visible.map((j,i)=>{
    const ac='ai-'+(j.ai_status||'pending');
    const al=AI_LABELS[j.ai_status]||j.ai_status;
    return `<tr>
      <td class="cycle-num" data-label="Cycle">#${j.cycle||'-'}</td>
      <td class="job-title" data-label="Title"><a href="${esc(j.job_url)}" target="_blank">${esc(trunc(j.title,50))}</a></td>
      <td class="posted-time" data-label="Posted">${esc(j.posted_time||'\u2014')}</td>
      <td data-label="Status"><span class="ai-badge ${ac}">${al}</span></td>
      <td data-label=""><button class="btn-info" onclick="openModal(${i})">Details</button></td>
    </tr>`}).join('');
  if(jobs.length>10){tog.innerHTML=`<button class="btn-info" onclick="toggleJobs()" style="margin-top:12px;padding:8px 24px">${showAllJobs?'Show Less':'View All '+jobs.length+' Jobs'}</button>`}else{tog.innerHTML=''}
}

function renderOutreachSection(leadId){
  const o=_outreachMap[leadId];
  if(!o)return '';
  const st=o.send_status||'pending';
  const stLabel=OS_LABELS[st]||st;
  const stClass='os-'+st;

  let html=`<div class="outreach-section"><h4>// Outreach Pipeline Details</h4>`;
  html+=`<div class="outreach-status ${stClass}">${stLabel}</div>`;

  if(o.skipped_reason){
    html+=`<div style="font-size:12px;color:var(--text-muted);margin-top:6px">Reason: ${esc(o.skipped_reason)}</div>`;
  }

  // Grok response
  if(o.grok_response){
    html+=`<details class="grok-box"><summary>\u25b8 View Grok Search Response</summary><div class="grok-content">${esc(o.grok_response)}</div></details>`;
  }

  // Contact badges
  const c=o.contacts_json||{};
  const emails=c.emails||[];
  const candidateEmails=c.candidate_emails||[];
  const verifiedEmails=c.verified_emails||[];
  const linkedins=c.linkedin_urls||[];
  const instas=c.instagram_handles||[];
  if(emails.length||linkedins.length||instas.length){
    html+=`<div class="contact-badges">`;
    emails.forEach(e=>html+=`<span class="contact-badge email">\u2709 ${esc(e)}</span>`);
    linkedins.forEach(l=>html+=`<span class="contact-badge linkedin">in ${esc(l.replace(/https?:\/\/(www\.)?linkedin\.com\//,''))}</span>`);
    instas.forEach(ig=>html+=`<span class="contact-badge instagram">\u25cf ${esc(ig)}</span>`);
    html+=`</div>`;
  }

  if(candidateEmails.length || verifiedEmails.length){
    html+=`<div style="margin-top:10px;font-size:11px;color:var(--text-muted);font-family:'JetBrains Mono',monospace">`;
    html+=`Candidates: ${candidateEmails.length || 0} | Verified: ${verifiedEmails.length || 0}`;
    html+=`</div>`;
  }

  // Email preview
  if(o.email_subject){
    html+=`<div class="email-preview"><div class="ep-subject">\u2709 ${esc(o.email_subject)}</div><div class="ep-body">${o.email_body||''}</div></div>`;
  }

  // Send status per email
  const sentTo=o.emails_sent_to||[];
  if(sentTo.length){
    html+=`<div class="email-sent-list">`;
    sentTo.forEach(r=>{
      const cls=r.status==='sent'?'sent-ok':r.status==='failed'?'sent-fail':'sent-pending';
      const icon=r.status==='sent'?'\u2713':r.status==='failed'?'\u2717':'\u2026';
      html+=`<div class="email-sent-item"><span class="${cls}">${icon}</span><span>${esc(r.email)}</span><span class="${cls}">${r.status}</span></div>`;
    });
    html+=`</div>`;
  }

  html+=`</div>`;
  return html;
}

function renderLeads(leads){_allLeadsData=leads;renderLeadsView()}
function renderLeadsView(){
  const leads=_allLeadsData;
  const list=document.getElementById('leads-list');
  const tog=document.getElementById('leads-toggle');
  document.getElementById('leads-count').textContent=`${leads.length} leads`;
  if(!leads.length){list.innerHTML='<div style="color:var(--text-muted);padding:24px;text-align:center;border:1px dashed var(--border);border-radius:10px">No leads discovered yet</div>';tog.innerHTML='';return}
  const visible=showAllLeads?leads:leads.slice(0,10);
  list.innerHTML=visible.map(l=>{
    const p=l.payload||{};const ci=p.client_info||{};const cs=p.contact_strategy||{};
    const isOutreached=l.outreached==1;
    const outreachHtml=renderOutreachSection(l.id);
    return `<div class="lead-card" id="lead-card-${l.id}"><h3>${esc(l.job_title)}</h3>
      <div class="lead-meta">${fmtDT(l.created_at)} &middot; <a href="${esc(l.job_url)}" target="_blank">View on Upwork &rarr;</a></div>
      <div class="lead-info">
        <div><div class="k">Company</div><div class="v">${esc(ci.company_name||'\u2014')}</div></div>
        <div><div class="k">Contact</div><div class="v">${esc(ci.guessed_person||'\u2014')}</div></div>
        <div><div class="k">Website</div><div class="v">${esc(ci.website||'\u2014')}</div></div>
        <div><div class="k">Confidence</div><div class="v">${esc(p.confidence_score||'\u2014')}</div></div>
      </div>${cs.cold_outreach_message?`<div class="lead-msg"><strong>Outreach:</strong> ${esc(cs.cold_outreach_message)}</div>`:''}
      ${outreachHtml}
      <div class="lead-actions">
        <button class="btn-outreached${isOutreached?' active':''}" onclick="toggleOutreached(${l.id},this)" ${isOutreached?'disabled':''}>${isOutreached?'\u2713 Outreached':'Mark Outreached'}</button>
        <button class="btn-delete" onclick="deleteLead(${l.id})">Delete</button>
      </div></div>`}).join('');
  if(leads.length>10){tog.innerHTML=`<button class="btn-info" onclick="toggleLeads()" style="margin-top:12px;padding:8px 24px">${showAllLeads?'Show Less':'View All '+leads.length+' Leads'}</button>`}else{tog.innerHTML=''}
}
function renderRuns(runs){
  const list=document.getElementById('runs-list');
  if(!runs.length){list.innerHTML='';return}
  list.innerHTML=runs.map(r=>{
    const sc=r.status==='running'?'status-running':r.status==='error'?'status-error':'status-completed';
    return `<div class="run-row"><span class="cycle-num">#${r.cycle}</span><span class="status-badge ${sc}">${r.status.toUpperCase()}</span><span class="run-time">${fmtTime(r.started_at)} \u2192 ${fmtTime(r.completed_at)}</span><span>${r.jobs_found} jobs</span><span>${r.jobs_new} new</span><span>${r.leads_found} leads</span></div>`}).join('');
}
function renderLogs(payload){
  const panel=document.getElementById('logs-panel');
  const countEl=document.getElementById('logs-count');
  const entries=(payload&&payload.entries)||[];
  const count=(payload&&payload.count)||entries.length;
  countEl.textContent=`${count} lines`;

  if(!entries.length){
    panel.innerHTML='<div class="log-empty">No workflow logs for the last 24 hours</div>';
    return;
  }

  const rows=entries.map((entry)=>{
    const ts=esc(entry.timestamp||'\u2014');
    const stage=esc(entry.stage||'raw');
    const cycle=entry.cycle===null||entry.cycle===undefined||entry.cycle===''?'':'#'+esc(entry.cycle);
    const msg=esc(entry.message||'');
    const fields=entry.fields&&Object.keys(entry.fields).length?`<div class="log-fields">${esc(JSON.stringify(entry.fields, null, 2))}</div>`:'';
    return `<div class="log-entry">
      <div class="log-head">
        <span class="log-ts">${ts}</span>
        <span class="log-stage">${stage}</span>
        ${cycle?`<span class="log-cycle">${cycle}</span>`:''}
      </div>
      <div class="log-msg">${msg}</div>
      ${fields}
    </div>`;
  }).join('');

  panel.innerHTML=rows;
  if(payload.truncated){
    countEl.textContent=`${count} lines (capped)`;
  }
}
async function refreshLogs(){
  try{
    const res=await fetch(API_LOGS);
    const data=await res.json();
    if(data.error){console.error(data.error);return}
    renderLogs(data);
  }catch(e){
    console.error('Logs refresh error:',e);
  }
}
async function toggleOutreached(id,btn){
  if(!confirm('Mark this lead as outreached?'))return;
  try{const r=await fetch(API_LEAD_OUTREACH,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});
    if(r.ok){btn.textContent='\u2713 Outreached';btn.classList.add('active');btn.disabled=true}
    else{alert('Failed to update')}}
  catch(e){alert('Error: '+e.message)}}

async function deleteLead(id){
  if(!confirm('Delete this lead permanently?'))return;
  try{const r=await fetch(API_LEAD_DELETE,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});
    if(r.ok){const card=document.getElementById('lead-card-'+id);if(card)card.remove();refresh()}
    else{alert('Failed to delete')}}
  catch(e){alert('Error: '+e.message)}}

async function deleteAllData(){
  if(!confirm('\u26a0\ufe0f DELETE ALL DATA?\n\nThis will permanently remove ALL scraped jobs, leads, run history, and seen jobs.\n\nThis action cannot be undone!'))return;
  if(!confirm('Are you absolutely sure? ALL data will be lost.'))return;
  try{const r=await fetch(API_DELETE_ALL,{method:'POST'});
    if(r.ok){alert('All data has been deleted.');refresh()}
    else{alert('Failed to delete data')}}
  catch(e){alert('Error: '+e.message)}}

async function refresh(){
  try{const res=await fetch(API);const data=await res.json();if(data.error){console.error(data.error);return}
    _outreachMap=data.outreach_map||{};
    renderStats(data.stats);renderRunBanner(data.last_run);renderJobs(data.recent_jobs||[]);renderLeads(data.recent_leads||[]);renderRuns(data.recent_runs||[]);
    document.getElementById('live-text').textContent='Live';
  }catch(e){document.getElementById('live-text').textContent='Offline';console.error('Refresh error:',e)}}
refresh();
refreshLogs();
setInterval(refresh,10000);
setInterval(refreshLogs,30000);
</script>
</body>
</html>"""


class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path == '/api/data':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(get_dashboard_json().encode())

        elif path == '/api/logs':
            query = parse_qs(urlparse(self.path).query)
            try:
                hours = max(1, min(int(query.get('hours', ['24'])[0]), 72))
            except Exception:
                hours = 24
            try:
                limit = max(50, min(int(query.get('limit', ['600'])[0]), 1000))
            except Exception:
                limit = 600
            payload = get_recent_logs(hours=hours, limit=limit)
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

        else:
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(DASHBOARD_HTML.encode())

    def do_POST(self):
        path = urlparse(self.path).path
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length) if content_length else b'{}'

        if path == '/api/lead/outreach':
            try:
                data = json.loads(body)
                lead_id = data.get('id')
                conn = get_db()
                conn.execute('UPDATE leads SET outreached = 1 WHERE id = ?', (lead_id,))
                conn.commit()
                conn.close()
                self._json_response({'ok': True})
            except Exception as e:
                self._json_response({'error': str(e)}, 500)

        elif path == '/api/lead/delete':
            try:
                data = json.loads(body)
                lead_id = data.get('id')
                conn = get_db()
                conn.execute('DELETE FROM leads WHERE id = ?', (lead_id,))
                conn.execute('DELETE FROM outreach_results WHERE lead_id = ?', (lead_id,))
                conn.commit()
                conn.close()
                self._json_response({'ok': True})
            except Exception as e:
                self._json_response({'error': str(e)}, 500)



        elif path == '/api/delete-all':
            try:
                conn = get_db()
                conn.execute('DELETE FROM scraped_jobs')
                conn.execute('DELETE FROM leads')
                conn.execute('DELETE FROM run_status')
                conn.execute('DELETE FROM seen_jobs')
                conn.execute('DELETE FROM outreach_results')
                conn.commit()
                conn.close()
                self._json_response({'ok': True})
            except Exception as e:
                self._json_response({'error': str(e)}, 500)

        else:
            self.send_response(404)
            self.end_headers()

    def _json_response(self, data, code=200):
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, format, *args):
        pass


def main():
    port = 5050
    server = HTTPServer(('0.0.0.0', port), DashboardHandler)
    print(f"\n  // Dashboard running at http://localhost:{port}")
    print(f"  Auto-refreshes every 10 seconds\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Dashboard stopped.")
        server.server_close()


if __name__ == '__main__':
    main()
