# scrapeling-up

an upwork job scraper that polls search results, analyzes them with a configured llm backend, and alerts you when a good lead shows up. runs a small web dashboard at `localhost:5050` so you can monitor everything without touching the terminal.

---

## what it does

- polls upwork search urls every N minutes
- scrapes job titles, budgets, skills, and full descriptions using Scrapling
- passes each job to the configured llm backend for analysis
- shows results on a live dashboard at `http://localhost:5050`
- sends desktop notifications when a lead is found
- auto-cleans data older than 24 hours from the database
- keeps email verification staged so browser checks do not block scraping
- verifies emails with QuickEmailVerification first, then falls back to local MX checks and browser verification when the API path is unavailable

---

## supported llm modes

- `oauth-codex` on Python 3.11+ with interactive login
- OpenAI API key or local OpenAI-compatible proxy
- Groq fallback

---

## requirements

- python 3.10+ for the scraper
- python 3.11+ if you want to use `oauth-codex`
- google chrome installed

---

## setup

### 1. clone the repo

```bash
git clone https://github.com/tryprane/scrapeling-UP.git
cd scrapeling-UP
```

### 2. create a virtual environment

```bash
python3 -m venv venv
source venv/bin/activate   # on windows: venv\Scripts\activate
```

### 3. install dependencies

```bash
pip install -r requirements.txt
```

### 4. install patchright browsers

```bash
python -m patchright install chromium
```

### 5. set up your `.env`

```bash
cp .env.example .env
```

open `.env` and choose one provider path:

```env
LLM_PROVIDER=auto
CODEX_OAUTH_ENABLED=true
CODEX_MODEL=gpt-5.3-codex

OPENAI_API_KEY=local-proxy
OPENAI_BASE_URL=http://127.0.0.1:10531/v1
OPENAI_MODEL=gpt-5.1
OPENAI_ANALYZER_MODEL=gpt-5.1

GROQ_API_KEY=
GROQ_MODEL=openai/gpt-oss-20b
GROQ_ANALYZER_MODEL=llama-3.3-70b-versatile

POLL_INTERVAL_MINUTES=5
UPWORK_SEARCH_URLS=https://www.upwork.com/nx/search/jobs/?category2_uid=...&sort=recency
HEADLESS=true
ENABLE_NOTIFICATIONS=false
OUTREACH_ASYNC_WORKERS=1
QUICKEMAILVERIFICATION_API_KEYS=key_one,key_two
```

---

## oauth-codex setup

If you want to use Codex OAuth instead of an API key:

```bash
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python codex_login.py
```

That starts the PKCE login flow, prints the authorization URL, and stores the token locally after you finish sign-in.

---

## run it

```bash
python3 main.py
```

the dashboard starts automatically at `http://localhost:5050`.

`main.py` is only a thin wrapper around `workflow.py`, so starting either entrypoint runs the same workflow.

---

## running on a ubuntu vps

on a headless vps you need a few extra steps since there's no gui.

### install system dependencies

```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxrandr2 libgbm1 libasound2
```

### if you want oauth-codex on the vps

```bash
sudo apt install -y python3.11 python3.11-venv
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python codex_login.py
```

### keep it running

Use `systemd` on the VPS so the workflow survives logout and reboots.

```bash
sudo cp deploy/upworkbot.service /etc/systemd/system/upworkbot.service
sudo systemctl daemon-reload
sudo systemctl enable --now upworkbot
sudo systemctl status upworkbot --no-pager
```

The workflow starts `dashboard.py` itself, so nginx can keep proxying
`/upwork/` to `http://127.0.0.1:5050/` once `upworkbot.service` is active.

---

## project structure

```text
.
├── main.py
├── scraper.py
├── analyzer.py
├── dashboard.py
├── db.py
├── notifier.py
├── config.py
├── llm_client.py
├── oauth login helper
└── requirements.txt
```

---

## notes

  - `oauth-codex` requires Python 3.11+ and an initial interactive login on each new machine.
- Groq is still available as a fallback.
- Upwork scraping now runs through Scrapling instead of the persistent browser profile.
- the sqlite database (`leads.db`) is created automatically on first run.
- MailTester Ninja browser verification uses browser automation and should submit one email at a time.
- `QUICKEMAILVERIFICATION_API_KEYS` accepts a comma-separated list. The workflow tries each key in order and only falls back to local/browser verification when the API path fails operationally.
