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
    'gemini_api_key': os.getenv('GEMINI_API_KEY', ''),

    'poll_interval_minutes': int(os.getenv('POLL_INTERVAL_MINUTES', '15')),

    'search_urls': [
        u.strip() for u in os.getenv('UPWORK_SEARCH_URLS', '').split(',')
        if u.strip()
    ],

    'enable_notifications': os.getenv('ENABLE_NOTIFICATIONS', 'true').lower() != 'false',

    'headless': os.getenv('HEADLESS', 'false').lower() == 'true',
}

# Fallback search URLs if none configured
if not config['search_urls']:
    config['search_urls'] = [
        'https://www.upwork.com/nx/search/jobs/?sort=recency&q=web+development&per_page=20',
        'https://www.upwork.com/nx/search/jobs/?sort=recency&q=software+development&per_page=20',
        'https://www.upwork.com/nx/search/jobs/?sort=recency&q=automation&per_page=20',
    ]
