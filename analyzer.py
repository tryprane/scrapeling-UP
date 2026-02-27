"""
Gemini AI job analyzer — mirrors src/analyzer.js

Analyzes Upwork job postings for client breadcrumbs using the "Lead Hunter" AI prompt.
Uses the new google.genai SDK.
"""

import json
from google import genai
from google.genai import types
from colorama import Fore, Style

from config import config

_client = None

SYSTEM_PROMPT = """You are "Lead Hunter" AI for Femur Studio. Your sole purpose is to de-anonymize Upwork job postings.

You are looking for "The Glitch" — a specific piece of information (a URL, a company description, a unique name, or a location+niche combo) that allows us to bypass Upwork and contact the client directly via Email or LinkedIn.

ANALYSIS PROTOCOL:
1. SCAN FOR BREADCRUMBS:
   - Direct Identifiers: URLs (even partial like app.bubble.io), emails, phone numbers, company names
   - The "Fingerprint": Unique phrases that can be Googled (e.g., "We are a 501c3 non-profit in Lutz Florida helping veterans")
   - Personal Names: Sign-offs like "Cheers, Dave" or "Ask for Sarah"
   - Technical Artifacts: Links to Figma, Trello, Google Drive, Staging sites

2. FILTERING (The "Femur Filter"):
   - IGNORE if: job is by a recruitment agency, description is less than 20 words, or is completely generic
   - PRIORITIZE if: client seems frustrated/urgent, project is high-value ($1k+), or client revealed identity

3. RESPONSE FORMAT — OUTPUT ONLY THIS JSON, NOTHING ELSE:
   If you find a lead:
   {
     "status": "LEAD_FOUND",
     "confidence_score": "High" or "Medium",
     "client_info": {
       "company_name": "Inferred Company Name",
       "guessed_person": "Name of likely owner/hiring manager",
       "website": "URL found or inferred",
       "search_query_used": "The phrase you identified that revealed them"
     },
     "contact_strategy": {
       "email_subject": "A short punchy subject line referencing their specific problem",
       "cold_outreach_message": "A 50-word humanized email from 'Prashant at Femur Studio'. Acknowledge the Upwork post but pivot to their specific pain point immediately. Do not sound like a bot."
     }
   }

   If NO lead is found:
   {
     "status": "NO_LEAD",
     "reason": "A very short 1-sentence explanation of why it was skipped (e.g. 'Agency posting', 'Too generic', 'No identifiers found')"
   }

CRITICAL: Output ONLY valid JSON. No markdown, no code fences, no explanation."""


def _get_client():
    """Get or create the Gemini client instance."""
    global _client
    if _client:
        return _client

    if not config['gemini_api_key']:
        raise RuntimeError('GEMINI_API_KEY is not set in .env — cannot analyze jobs.')

    _client = genai.Client(api_key=config['gemini_api_key'])
    return _client


def analyze_job(job: dict) -> dict:
    """
    Analyze a single job description for client breadcrumbs.
    Returns the parsed JSON response from Gemini AI.
    """
    client = _get_client()

    prompt = f"""Analyze this Upwork job posting for identifiable client information ("breadcrumbs").

JOB TITLE: {job.get('title', '')}
BUDGET: {job.get('budget', 'Not specified')}
SKILLS: {job.get('skills', 'None listed')}
POSTED: {job.get('posted_time', 'Unknown')}

FULL JOB DESCRIPTION:
{job.get('description', '(No description available)')}

Respond with ONLY the JSON as instructed."""

    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=SYSTEM_PROMPT + '\n\n' + prompt,
            config=types.GenerateContentConfig(
                temperature=0.2,
                max_output_tokens=1024,
            ),
        )

        text = response.text.strip()

        # Strip markdown code fences if AI adds them anyway
        if text.startswith('```json'):
            text = text[7:]
        elif text.startswith('```'):
            text = text[3:]
        if text.endswith('```'):
            text = text[:-3]
        text = text.strip()

        parsed = json.loads(text)
        return parsed

    except Exception as e:
        title_preview = job.get('title', '')[:40]
        print(f"{Fore.YELLOW}  ⚠ Analyzer error for \"{title_preview}...\": {e}{Style.RESET_ALL}")
        return {'status': 'NO_LEAD'}
