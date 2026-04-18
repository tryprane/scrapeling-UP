"""
Configuration loader — mirrors src/config.js
Loads settings from .env file in the same directory.
"""

import os
from dotenv import load_dotenv

# Load .env from the same directory as this script
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
load_dotenv(_env_path)

# ── Parse config ─────────────────────────────────────────────────────

config = {
    'llm_provider': os.getenv('LLM_PROVIDER', 'auto').lower(),
    'codex_oauth_enabled': os.getenv('CODEX_OAUTH_ENABLED', 'false').lower() == 'true',
    'codex_model': os.getenv('CODEX_MODEL', 'gpt-5.3-codex'),

    'openai_api_key': os.getenv('OPENAI_API_KEY', ''),
    'openai_base_url': os.getenv('OPENAI_BASE_URL', '').rstrip('/'),
    'openai_model': os.getenv('OPENAI_MODEL', 'gpt-5.1'),
    'openai_analyzer_model': os.getenv('OPENAI_ANALYZER_MODEL', 'gpt-5.1'),

    'groq_api_key': os.getenv('GROQ_API_KEY', ''),
    'groq_model': os.getenv('GROQ_MODEL', 'openai/gpt-oss-20b'),
    'groq_analyzer_model': os.getenv('GROQ_ANALYZER_MODEL', 'llama-3.3-70b-versatile'),

    'poll_interval_minutes': int(os.getenv('POLL_INTERVAL_MINUTES', '15')),
    'ai_call_delay_seconds': int(os.getenv('AI_CALL_DELAY_SECONDS', '60')),

    'search_urls': [
        u.strip() for u in os.getenv('UPWORK_SEARCH_URLS', '').split(',')
        if u.strip()
    ],

    'enable_notifications': os.getenv('ENABLE_NOTIFICATIONS', 'true').lower() != 'false',

    # Browser mode for Gemini/login flows. Set HEADLESS=true for fully headless
    # runs, or keep false when using a visible browser / virtual display.
    'headless': os.getenv('HEADLESS', 'false').lower() == 'true',

    # Background outreach workers let contact discovery continue while email
    # verification and sending happen in parallel.
    'outreach_async_workers': int(os.getenv('OUTREACH_ASYNC_WORKERS', '1')),

    # Email verifier selection.
    # local             -> built-in syntax + DNS checks
    # mailtester_browser -> MailTester Ninja website via browser automation
    'email_verifier_provider': os.getenv('EMAIL_VERIFIER_PROVIDER', 'local').lower(),
    'mailtester_verifier_visible': os.getenv('MAILTESTER_VERIFIER_VISIBLE', 'true').lower() != 'false',
    'mailtester_verifier_url': os.getenv(
        'MAILTESTER_VERIFIER_URL',
        'https://mailtester.ninja/email-verifier/',
    ).rstrip('/'),
    'mailtester_verifier_wait_seconds': int(os.getenv('MAILTESTER_VERIFIER_WAIT_SECONDS', '90')),
    'mailtester_verifier_page_timeout_ms': int(os.getenv('MAILTESTER_VERIFIER_PAGE_TIMEOUT_MS', '30000')),
    'mailtester_verifier_batch_size': int(os.getenv('MAILTESTER_VERIFIER_BATCH_SIZE', '1')),

    # Gmail API — address used as the From: header when sending outreach emails.
    # Set this to your Gmail address in .env as GMAIL_SENDER=you@gmail.com
    # Leave blank to use 'me' (Gmail API resolves the authorized account automatically).
    'gmail_sender': os.getenv('GMAIL_SENDER', 'me'),

    # Prane / outbound_live email API
    'prane_base_url': os.getenv('PRANE_BASE_URL', 'https://prane.one').rstrip('/'),
    'prane_api_key': os.getenv('PRANE_API_KEY', os.getenv('OUTBOUND_LIVE_API_KEY', '')),
}

# Fallback search URLs if none configured
if not config['search_urls']:
    config['search_urls'] = [
        'https://www.upwork.com/nx/search/jobs/?sort=recency&q=web+development&per_page=20',
        'https://www.upwork.com/nx/search/jobs/?sort=recency&q=software+development&per_page=20',
        'https://www.upwork.com/nx/search/jobs/?sort=recency&q=automation&per_page=20',
    ]
