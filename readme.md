# scrapeling-up

an upwork job scraper that polls search results, analyzes them with gemini ai, and alerts you when a good lead shows up. runs a small web dashboard at `localhost:5050` so you can monitor everything without touching the terminal.

---

## what it does

- polls upwork search urls every N minutes (configurable)
- scrapes job titles, budgets, skills, and full descriptions using Scrapling
- passes each job to gemini ai for analysis — it decides if it's a real lead or not
- shows results on a live dashboard at `http://localhost:5050`
- sends desktop notifications when a lead is found
- auto-cleans data older than 24 hours from the database

---

## requirements

- python 3.10+
- google chrome installed (the scraper uses your local chrome via patchright)
- a gemini api key → [get one here](https://aistudio.google.com)

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

copy the example and fill in your values:

```bash
cp .env.example .env
```

open `.env` and set:

```
GEMINI_API_KEY=your_key_here
POLL_INTERVAL_MINUTES=5
UPWORK_SEARCH_URLS=https://www.upwork.com/nx/search/jobs/?category2_uid=...&sort=recency
HEADLESS=true
ENABLE_NOTIFICATIONS=false
```

> **vps note:** set `HEADLESS=true` and `ENABLE_NOTIFICATIONS=false` on a server — there's no display to show a browser window or desktop notifications.

### 6. run it

```bash
python3 main.py
```

the dashboard starts automatically at `http://localhost:5050`.

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

> these are chromium's runtime dependencies on ubuntu. without them patchright will crash on launch.

### clone, venv, install (same as above)

```bash
git clone https://github.com/tryprane/scrapeling-UP.git
cd scrapeling-UP
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python -m patchright install chromium
```

### configure `.env` for headless

```
HEADLESS=true
ENABLE_NOTIFICATIONS=false
```

### keep it running with screen or nohup

using `screen`:

```bash
screen -S upwork
source venv/bin/activate
python3 main.py
# press Ctrl+A then D to detach
```

to come back to it:

```bash
screen -r upwork
```

using `nohup` (simpler but no live output):

```bash
nohup python3 main.py > bot.log 2>&1 &
```

check logs:

```bash
tail -f bot.log
```

### access the dashboard from outside the vps

by default the dashboard binds to `localhost:5050`. to access it from your browser, either:

- use an ssh tunnel: `ssh -L 5050:localhost:5050 user@your-vps-ip`
- or open port 5050 in your vps firewall and change the dashboard host binding to `0.0.0.0`

---

## project structure

```
.
├── main.py          # entry point — poll loop
├── scraper.py       # patchright scraper + cloudflare solver
├── analyzer.py      # gemini ai analysis
├── dashboard.py     # flask dashboard server
├── db.py            # sqlite database helpers
├── notifier.py      # desktop notifications + file logging
├── config.py        # loads settings from .env
├── requirements.txt
└── .env.example
```

---

## env variables

| variable | default | description |
|---|---|---|
| `GEMINI_API_KEY` | — | required. your google gemini api key |
| `POLL_INTERVAL_MINUTES` | `15` | how often to check for new jobs |
| `UPWORK_SEARCH_URLS` | — | comma-separated upwork search urls |
| `HEADLESS` | `false` | run browser headless (`true` on vps) |
| `ENABLE_NOTIFICATIONS` | `true` | desktop toast notifications (`false` on vps) |

---

## notes

- Gemini browser login uses a persistent browser profile stored at `~/.cliup-browser-profile-py`. If Gemini asks you to log in on the first run, do it once and the session is saved.
- Upwork scraping now runs through Scrapling instead of the persistent browser profile.
- gemini api calls are spaced 60 seconds apart to stay within free tier rate limits.
- the sqlite database (`leads.db`) is created automatically on first run.

## VPS note

- On a Ubuntu VPS, run the workflow inside a virtual display if you want to log into Google/Gemini manually at least once.
- A common pattern is `xvfb-run -a python3 main.py`.
- Set `HEADLESS=false` if you want the browser UI/login flow to stay available.
