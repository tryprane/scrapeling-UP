"""
Live Dashboard Server for Upwork Lead Hunter.
Auto-started by main.py, also runnable standalone.

Usage:  python3 dashboard.py
Opens:  http://localhost:5050
"""

import json
import sqlite3
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'leads.db')


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


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

        recent_leads = conn.execute('SELECT * FROM leads ORDER BY created_at DESC LIMIT 20').fetchall()

        return json.dumps({
            'last_run': dict(last_run) if last_run else None,
            'recent_runs': [dict(r) for r in recent_runs],
            'recent_jobs': [{**dict(j), 'ai_result': json.loads(j['ai_result'] or '{}')} for j in recent_jobs],
            'recent_leads': [{**dict(r), 'payload': json.loads(r['payload'] or '{}')} for r in recent_leads],
            'stats': {
                'total_seen': total_seen, 'total_leads': total_leads,
                'total_scraped': total_scraped, 'total_leads_found': total_leads_found,
                'total_errors': total_errors, 'total_no_lead': total_no_lead,
                'total_pending': total_pending, 'total_skipped': total_skipped,
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
<title>Upwork Lead Hunter — Dashboard</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #0a0a0f; --bg-card: rgba(255,255,255,0.03); --bg-card-hover: rgba(255,255,255,0.06);
  --border: rgba(255,255,255,0.06); --text: #e4e4e7; --text-dim: #71717a; --text-bright: #fafafa;
  --accent: #8b5cf6; --accent-glow: rgba(139,92,246,0.15);
  --green: #22c55e; --green-dim: rgba(34,197,94,0.12);
  --red: #ef4444; --red-dim: rgba(239,68,68,0.12);
  --yellow: #eab308; --yellow-dim: rgba(234,179,8,0.12);
  --blue: #3b82f6; --blue-dim: rgba(59,130,246,0.12);
  --cyan: #06b6d4;
}
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family:'Inter',-apple-system,sans-serif; background:var(--bg); color:var(--text); min-height:100vh; line-height:1.5; }
.noise { position:fixed; inset:0; z-index:-1; background-image:url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='0.03'/%3E%3C/svg%3E"); }
.container { max-width:1400px; margin:0 auto; padding:24px; }
.header { display:flex; align-items:center; justify-content:space-between; margin-bottom:32px; padding-bottom:24px; border-bottom:1px solid var(--border); }
.header h1 { font-size:24px; font-weight:700; background:linear-gradient(135deg,var(--accent),var(--cyan)); -webkit-background-clip:text; -webkit-text-fill-color:transparent; }
.header .subtitle { color:var(--text-dim); font-size:13px; margin-top:2px; }
.live-badge { display:flex; align-items:center; gap:8px; padding:6px 14px; border-radius:20px; background:var(--green-dim); color:var(--green); font-size:12px; font-weight:600; }
.live-dot { width:8px; height:8px; border-radius:50%; background:var(--green); animation:pulse 2s ease-in-out infinite; }
@keyframes pulse { 0%,100%{opacity:1;box-shadow:0 0 0 0 rgba(34,197,94,0.4)} 50%{opacity:.7;box-shadow:0 0 0 6px rgba(34,197,94,0)} }
.stats-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:14px; margin-bottom:28px; }
.stat-card { background:var(--bg-card); border:1px solid var(--border); border-radius:12px; padding:18px; transition:all .2s; }
.stat-card:hover { background:var(--bg-card-hover); transform:translateY(-1px); }
.stat-card .label { font-size:11px; color:var(--text-dim); text-transform:uppercase; letter-spacing:.5px; margin-bottom:6px; }
.stat-card .value { font-size:26px; font-weight:700; color:var(--text-bright); }
.stat-card .value.green{color:var(--green)} .stat-card .value.red{color:var(--red)} .stat-card .value.yellow{color:var(--yellow)} .stat-card .value.blue{color:var(--blue)} .stat-card .value.accent{color:var(--accent)}
.run-banner { display:flex; align-items:center; gap:20px; background:var(--bg-card); border:1px solid var(--border); border-radius:12px; padding:14px 22px; margin-bottom:24px; flex-wrap:wrap; }
.run-banner .run-item { display:flex; flex-direction:column; gap:2px; }
.run-banner .run-label { font-size:10px; color:var(--text-dim); text-transform:uppercase; letter-spacing:.5px; }
.run-banner .run-value { font-size:14px; font-weight:500; }
.status-badge { display:inline-flex; align-items:center; gap:6px; padding:3px 10px; border-radius:6px; font-size:11px; font-weight:600; }
.status-running{background:var(--blue-dim);color:var(--blue)} .status-completed{background:var(--green-dim);color:var(--green)} .status-error{background:var(--red-dim);color:var(--red)}
.section-header { display:flex; align-items:center; justify-content:space-between; margin-bottom:14px; }
.section-header h2 { font-size:16px; font-weight:600; }
.section-header .count { font-size:12px; color:var(--text-dim); }
.jobs-table { width:100%; border-collapse:collapse; background:var(--bg-card); border:1px solid var(--border); border-radius:12px; overflow:hidden; margin-bottom:32px; }
.jobs-table thead { background:rgba(255,255,255,0.02); }
.jobs-table th { text-align:left; padding:10px 14px; font-size:10px; color:var(--text-dim); text-transform:uppercase; letter-spacing:.5px; font-weight:600; border-bottom:1px solid var(--border); }
.jobs-table td { padding:10px 14px; font-size:13px; border-bottom:1px solid var(--border); vertical-align:middle; }
.jobs-table tr:last-child td { border-bottom:none; }
.jobs-table tr:hover { background:var(--bg-card-hover); }
.job-title { font-weight:500; color:var(--text-bright); max-width:300px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.job-title a { color:var(--accent); text-decoration:none; }
.job-title a:hover { text-decoration:underline; }
.ai-badge { display:inline-flex; align-items:center; gap:4px; padding:3px 10px; border-radius:5px; font-size:11px; font-weight:600; white-space:nowrap; }
.ai-lead_found{background:var(--green-dim);color:var(--green)} .ai-no_lead{background:rgba(255,255,255,.05);color:var(--text-dim)}
.ai-error{background:var(--red-dim);color:var(--red)} .ai-pending{background:var(--yellow-dim);color:var(--yellow)}
.ai-skipped{background:rgba(255,255,255,.04);color:var(--text-dim)} .ai-analyzing{background:var(--blue-dim);color:var(--blue)}
.posted-time { color:var(--text-dim); font-size:12px; white-space:nowrap; }
.cycle-num { color:var(--text-dim); font-size:12px; text-align:center; }
.btn-info { padding:4px 12px; border-radius:6px; border:1px solid var(--accent); background:var(--accent-glow); color:var(--accent); font-size:11px; font-weight:600; cursor:pointer; transition:all .15s; white-space:nowrap; }
.btn-info:hover { background:var(--accent); color:#fff; }
.runs-list { display:grid; gap:8px; margin-bottom:32px; }
.run-row { display:grid; grid-template-columns:60px 100px 1fr 80px 80px 80px; align-items:center; gap:12px; background:var(--bg-card); border:1px solid var(--border); border-radius:8px; padding:10px 16px; font-size:13px; }
.run-row .run-time { color:var(--text-dim); font-size:12px; }
.lead-card { background:var(--bg-card); border:1px solid var(--accent); border-radius:12px; padding:20px; margin-bottom:12px; border-left:3px solid var(--accent); }
.lead-card h3 { font-size:14px; font-weight:600; margin-bottom:8px; color:var(--accent); }
.lead-meta { font-size:12px; color:var(--text-dim); margin-bottom:12px; }
.lead-info { display:grid; grid-template-columns:1fr 1fr; gap:8px; font-size:13px; }
.lead-info .k { color:var(--text-dim); font-size:11px; text-transform:uppercase; }
.lead-info .v { color:var(--text-bright); }
.lead-msg { margin-top:12px; padding:12px; background:rgba(255,255,255,.02); border-radius:8px; font-size:13px; line-height:1.6; border-left:2px solid var(--accent); }
.footer { text-align:center; padding:24px; color:var(--text-dim); font-size:12px; border-top:1px solid var(--border); margin-top:40px; }

/* ── Modal ── */
.modal-overlay { display:none; position:fixed; inset:0; background:rgba(0,0,0,.7); backdrop-filter:blur(4px); z-index:1000; justify-content:center; align-items:flex-start; padding:40px 20px; overflow-y:auto; }
.modal-overlay.active { display:flex; }
.modal {
  background:#131318; border:1px solid var(--border); border-radius:16px;
  width:100%; max-width:720px; padding:0; position:relative;
  box-shadow:0 24px 48px rgba(0,0,0,.5); animation:modalIn .2s ease;
}
@keyframes modalIn { from{opacity:0;transform:translateY(20px)} to{opacity:1;transform:translateY(0)} }
.modal-close { position:absolute; top:16px; right:16px; width:32px; height:32px; border-radius:8px; border:1px solid var(--border); background:var(--bg-card); color:var(--text-dim); font-size:18px; cursor:pointer; display:flex; align-items:center; justify-content:center; transition:all .15s; z-index:2; }
.modal-close:hover { background:var(--red-dim); color:var(--red); border-color:var(--red); }
.modal-header { padding:24px 28px 16px; border-bottom:1px solid var(--border); }
.modal-header h2 { font-size:18px; font-weight:700; color:var(--text-bright); line-height:1.4; padding-right:40px; }
.modal-header .modal-url { margin-top:6px; }
.modal-header .modal-url a { color:var(--accent); font-size:12px; text-decoration:none; word-break:break-all; }
.modal-header .modal-url a:hover { text-decoration:underline; }
.modal-body { padding:20px 28px 28px; }
.modal-grid { display:grid; grid-template-columns:1fr 1fr; gap:16px; margin-bottom:20px; }
.modal-field { }
.modal-field .mf-label { font-size:10px; color:var(--text-dim); text-transform:uppercase; letter-spacing:.5px; margin-bottom:4px; }
.modal-field .mf-value { font-size:14px; color:var(--text-bright); font-weight:500; }
.modal-desc-label { font-size:10px; color:var(--text-dim); text-transform:uppercase; letter-spacing:.5px; margin-bottom:8px; }
.modal-desc {
  background:rgba(255,255,255,.02); border:1px solid var(--border);
  border-radius:10px; padding:16px; font-size:13px; line-height:1.7;
  color:var(--text); max-height:300px; overflow-y:auto; white-space:pre-wrap; word-break:break-word;
}
.modal-ai { margin-top:20px; padding:16px; border-radius:10px; border:1px solid var(--border); }
.modal-ai.lead { border-color:var(--green); background:var(--green-dim); }
.modal-ai.error { border-color:var(--red); background:var(--red-dim); }
.modal-ai h4 { font-size:12px; text-transform:uppercase; letter-spacing:.5px; margin-bottom:10px; }
.modal-ai-grid { display:grid; grid-template-columns:1fr 1fr; gap:8px; font-size:13px; }
.modal-ai-grid .k { color:var(--text-dim); font-size:11px; }
.modal-ai-grid .v { color:var(--text-bright); }
.modal-ai-msg { margin-top:10px; padding:10px; background:rgba(0,0,0,.2); border-radius:8px; font-size:13px; line-height:1.6; border-left:2px solid var(--accent); }
.modal-ai-error { margin-top:8px; font-size:12px; color:var(--red); }

@media (max-width:768px) {
  .stats-grid{grid-template-columns:repeat(2,1fr)} .run-row{grid-template-columns:1fr} .modal-grid{grid-template-columns:1fr} .modal{max-width:100%}
}
</style>
</head>
<body>
<div class="noise"></div>
<div class="container">
  <div class="header">
    <div><h1>⚡ Upwork Lead Hunter</h1><div class="subtitle">Live Dashboard — Auto-refreshes every 10s</div></div>
    <div class="live-badge"><div class="live-dot"></div><span id="live-text">LIVE</span></div>
  </div>
  <div class="stats-grid" id="stats-grid"></div>
  <div class="run-banner" id="run-banner"></div>
  <div class="section-header"><h2>📋 Scraped Jobs</h2><span class="count" id="jobs-count"></span></div>
  <table class="jobs-table">
    <thead><tr><th>Cycle</th><th>Title</th><th>Posted</th><th>AI Status</th><th></th></tr></thead>
    <tbody id="jobs-body"></tbody>
  </table>
  <div class="section-header"><h2>🎯 Leads Found</h2><span class="count" id="leads-count"></span></div>
  <div id="leads-list"></div>
  <div class="section-header" style="margin-top:32px"><h2>📊 Run History</h2></div>
  <div class="runs-list" id="runs-list"></div>
  <div class="footer">Upwork Lead Hunter v2.0 — Python/Patchright Edition — by Femur Studio</div>
</div>

<!-- Job Detail Modal -->
<div class="modal-overlay" id="modal-overlay" onclick="if(event.target===this)closeModal()">
  <div class="modal">
    <button class="modal-close" onclick="closeModal()">✕</button>
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
const API='/api/data';
let allJobs=[];

function esc(s){if(!s)return'';return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
function trunc(s,n){if(!s)return'—';return s.length>n?s.slice(0,n)+'…':s}
function fmtTime(t){if(!t)return'—';try{const d=new Date(t+'Z');return d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'})}catch{return t}}
function fmtDT(t){if(!t)return'—';try{const d=new Date(t+'Z');return d.toLocaleDateString([],{month:'short',day:'numeric'})+' '+d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})}catch{return t}}

const AI_LABELS={'lead_found':'🎯 LEAD','no_lead':'— No Lead','error':'⚠ Error','pending':'⏳ Pending','skipped':'⊘ Skipped','analyzing':'⟳ Analyzing'};

function openModal(idx){
  const j=allJobs[idx]; if(!j)return;
  document.getElementById('modal-title').textContent=j.title||'—';
  const urlEl=document.getElementById('modal-url');urlEl.href=j.job_url||'#';urlEl.textContent=j.job_url||'—';
  document.getElementById('modal-posted').textContent=j.posted_time||'—';
  document.getElementById('modal-budget').textContent=j.budget||'—';
  document.getElementById('modal-skills').textContent=j.skills||'—';
  document.getElementById('modal-cycle').textContent='#'+(j.cycle||'-');
  document.getElementById('modal-scraped').textContent=fmtDT(j.scraped_at);
  const aiStatusEl=document.getElementById('modal-ai-status');
  aiStatusEl.innerHTML=`<span class="ai-badge ai-${j.ai_status||'pending'}">${AI_LABELS[j.ai_status]||j.ai_status}</span>`;
  document.getElementById('modal-desc').textContent=j.description||'No description available';

  // AI result section
  const aiSec=document.getElementById('modal-ai-section');
  const r=j.ai_result||{};
  const ci=r.client_info||{};
  const cs=r.contact_strategy||{};
  if(j.ai_status==='lead_found'&&ci.company_name){
    aiSec.innerHTML=`<div class="modal-ai lead"><h4 style="color:var(--green)">🎯 Lead Intelligence</h4><div class="modal-ai-grid">
      <div><div class="k">Company</div><div class="v">${esc(ci.company_name)}</div></div>
      <div><div class="k">Contact</div><div class="v">${esc(ci.guessed_person||'—')}</div></div>
      <div><div class="k">Website</div><div class="v">${esc(ci.website||'—')}</div></div>
      <div><div class="k">Search Query</div><div class="v">${esc(ci.search_query_used||'—')}</div></div>
      <div><div class="k">Confidence</div><div class="v">${esc(r.confidence_score||'—')}</div></div>
    </div>${cs.cold_outreach_message?`<div class="modal-ai-msg"><strong>Suggested Outreach:</strong><br>${esc(cs.cold_outreach_message)}</div>`:''}</div>`;
  } else if(j.ai_error){
    aiSec.innerHTML=`<div class="modal-ai error"><h4 style="color:var(--red)">⚠ AI Error</h4><div class="modal-ai-error">${esc(j.ai_error)}</div></div>`;
  } else if(j.ai_status==='no_lead'){
    aiSec.innerHTML=`<div class="modal-ai" style="border-color:var(--border)"><h4 style="color:var(--text-dim)">⚠ Skipped Lead</h4>${r.reason?`<div class="modal-ai-msg" style="border-left-color:var(--text-dim)"><strong style="color:var(--text-bright)">Reason:</strong> ${esc(r.reason)}</div>`:`<pre style="font-size:12px;color:var(--text-dim);margin-top:8px;white-space:pre-wrap;font-family:monospace">${esc(JSON.stringify(r, null, 2))}</pre>`}</div>`;
  } else { aiSec.innerHTML=`<div class="modal-ai" style="border-color:var(--border)"><h4 style="color:var(--text-dim)">AI Raw Response</h4><pre style="font-size:12px;color:var(--text-dim);margin-top:8px;white-space:pre-wrap;font-family:monospace">${esc(JSON.stringify(r, null, 2))}</pre></div>`; }

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
    <div class="stat-card"><div class="label">Total Scraped</div><div class="value blue">${s.total_scraped}</div></div>
    <div class="stat-card"><div class="label">Leads Found</div><div class="value green">${s.total_leads_found}</div></div>
    <div class="stat-card"><div class="label">No Lead</div><div class="value">${s.total_no_lead}</div></div>
    <div class="stat-card"><div class="label">AI Errors</div><div class="value red">${s.total_errors}</div></div>
    <div class="stat-card"><div class="label">Pending</div><div class="value yellow">${s.total_pending}</div></div>
    <div class="stat-card"><div class="label">Skipped</div><div class="value">${s.total_skipped}</div></div>`;
}
function renderRunBanner(r){
  const b=document.getElementById('run-banner');
  if(!r){b.innerHTML='<span style="color:var(--text-dim)">No runs yet</span>';return}
  const sc=r.status==='running'?'status-running':r.status==='error'?'status-error':'status-completed';
  b.innerHTML=`
    <div class="run-item"><span class="run-label">Cycle</span><span class="run-value">#${r.cycle}</span></div>
    <div class="run-item"><span class="run-label">Status</span><span class="status-badge ${sc}">${r.status==='running'?'⟳':r.status==='completed'?'✓':'✗'} ${r.status.toUpperCase()}</span></div>
    <div class="run-item"><span class="run-label">Started</span><span class="run-value">${fmtTime(r.started_at)}</span></div>
    <div class="run-item"><span class="run-label">Completed</span><span class="run-value">${fmtTime(r.completed_at)}</span></div>
    <div class="run-item"><span class="run-label">Jobs Found</span><span class="run-value">${r.jobs_found}</span></div>
    <div class="run-item"><span class="run-label">New Jobs</span><span class="run-value">${r.jobs_new}</span></div>
    <div class="run-item"><span class="run-label">Leads</span><span class="run-value" style="color:var(--green)">${r.leads_found}</span></div>
    ${r.error_msg?`<div class="run-item" style="grid-column:1/-1"><span class="run-label">Error</span><span class="run-value" style="color:var(--red)">${esc(r.error_msg)}</span></div>`:''}`;
}
function renderJobs(jobs){
  allJobs=jobs;
  document.getElementById('jobs-count').textContent=`${jobs.length} recent jobs`;
  const tbody=document.getElementById('jobs-body');
  if(!jobs.length){tbody.innerHTML='<tr><td colspan="5" style="text-align:center;color:var(--text-dim);padding:40px">No jobs scraped yet</td></tr>';return}
  tbody.innerHTML=jobs.map((j,i)=>{
    const ac='ai-'+(j.ai_status||'pending');
    const al=AI_LABELS[j.ai_status]||j.ai_status;
    return `<tr>
      <td class="cycle-num">#${j.cycle||'-'}</td>
      <td class="job-title"><a href="${esc(j.job_url)}" target="_blank">${esc(trunc(j.title,50))}</a></td>
      <td class="posted-time">${esc(j.posted_time||'—')}</td>
      <td><span class="ai-badge ${ac}">${al}</span></td>
      <td><button class="btn-info" onclick="openModal(${i})">More Info</button></td>
    </tr>`}).join('');
}
function renderLeads(leads){
  const list=document.getElementById('leads-list');
  document.getElementById('leads-count').textContent=`${leads.length} leads`;
  if(!leads.length){list.innerHTML='<div style="color:var(--text-dim);padding:20px;text-align:center">No leads discovered yet</div>';return}
  list.innerHTML=leads.map(l=>{
    const p=l.payload||{};const ci=p.client_info||{};const cs=p.contact_strategy||{};
    return `<div class="lead-card"><h3>${esc(l.job_title)}</h3>
      <div class="lead-meta">${fmtDT(l.created_at)} · <a href="${esc(l.job_url)}" target="_blank" style="color:var(--accent)">View on Upwork ↗</a></div>
      <div class="lead-info">
        <div><div class="k">Company</div><div class="v">${esc(ci.company_name||'—')}</div></div>
        <div><div class="k">Contact</div><div class="v">${esc(ci.guessed_person||'—')}</div></div>
        <div><div class="k">Website</div><div class="v">${esc(ci.website||'—')}</div></div>
        <div><div class="k">Confidence</div><div class="v">${esc(p.confidence_score||'—')}</div></div>
      </div>${cs.cold_outreach_message?`<div class="lead-msg"><strong>Outreach:</strong> ${esc(cs.cold_outreach_message)}</div>`:''}</div>`}).join('');
}
function renderRuns(runs){
  const list=document.getElementById('runs-list');
  if(!runs.length){list.innerHTML='';return}
  list.innerHTML=runs.map(r=>{
    const sc=r.status==='running'?'status-running':r.status==='error'?'status-error':'status-completed';
    return `<div class="run-row"><span class="cycle-num">#${r.cycle}</span><span class="status-badge ${sc}">${r.status}</span><span class="run-time">${fmtTime(r.started_at)} → ${fmtTime(r.completed_at)}</span><span>${r.jobs_found} jobs</span><span>${r.jobs_new} new</span><span style="color:var(--green)">${r.leads_found} leads</span></div>`}).join('');
}
async function refresh(){
  try{const res=await fetch(API);const data=await res.json();if(data.error){console.error(data.error);return}
    renderStats(data.stats);renderRunBanner(data.last_run);renderJobs(data.recent_jobs||[]);renderLeads(data.recent_leads||[]);renderRuns(data.recent_runs||[]);
    document.getElementById('live-text').textContent='LIVE';
  }catch(e){document.getElementById('live-text').textContent='OFFLINE';console.error('Refresh error:',e)}}
refresh();setInterval(refresh,10000);
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
        else:
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(DASHBOARD_HTML.encode())

    def log_message(self, format, *args):
        pass


def main():
    port = 5050
    server = HTTPServer(('0.0.0.0', port), DashboardHandler)
    print(f"\n  ⚡ Dashboard running at http://localhost:{port}")
    print(f"  Auto-refreshes every 10 seconds\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Dashboard stopped.")
        server.server_close()


if __name__ == '__main__':
    main()
