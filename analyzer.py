"""
Gemini AI job analyzer.

Classifies Upwork jobs as lead / no-lead and returns structured JSON.
"""

from __future__ import annotations

import json

from colorama import Fore, Style
from google import genai
from google.genai import types

from config import config

_client = None

SYSTEM_PROMPT = """You are "Lead Hunter" AI for Femur Studio.

Your only job is to decide whether an Upwork post contains a real, externally reachable lead.

Hard rule:
- Return "NO_LEAD" unless the post contains at least one concrete breadcrumb that can be verified outside Upwork.
- Generic hiring language, role titles, budgets, skills lists, urgency, and vague descriptions do NOT count.
- Do NOT infer a company name or person from the job title alone.
- If you are unsure, choose "NO_LEAD".

Concrete breadcrumbs include:
- A direct URL, partial URL, email, phone number, domain, product URL, staging URL, GitHub, Figma, Trello, Drive, or similar artifact
- A unique company name, legal entity, or brand that can be searched externally
- A named person, sign-off, or hiring manager clue
- A distinctive phrase or location+niche combination specific enough to identify the client outside Upwork

If the post looks like an agency post, a recruiter post, a boilerplate listing, or a generic "need a developer" request, return NO_LEAD.

If you do find a lead, keep the output conservative and only use information you can actually support from the post.

RESPONSE FORMAT - OUTPUT ONLY THIS JSON, NOTHING ELSE:
If you find a lead:
{
  "status": "LEAD_FOUND",
  "confidence_score": "High" or "Medium",
  "client_info": {
    "company_name": "Company or brand actually supported by the post",
    "guessed_person": "Person name only if there is a real clue; otherwise empty string",
    "website": "URL found or clearly supported",
    "search_query_used": "The exact breadcrumb or phrase that justified the lead"
  },
  "contact_strategy": {
    "email_subject": "Short subject line based on the specific problem",
    "cold_outreach_message": "A 50-word humanized email from 'Prashant at Femur Studio'. Mention the exact problem and keep it specific."
  },
  "evidence": [
    "Copy the exact breadcrumb snippets that made this a lead"
  ]
}

If NO lead is found:
{
  "status": "NO_LEAD",
  "reason": "A very short 1-sentence explanation"
}

CRITICAL: Output ONLY valid JSON. No markdown, no code fences, no explanation."""


def _get_client():
    """Get or create the Gemini client instance."""
    global _client
    if _client:
        return _client

    if not config["gemini_api_key"]:
        raise RuntimeError("GEMINI_API_KEY is not set in .env - cannot analyze jobs.")

    _client = genai.Client(api_key=config["gemini_api_key"])
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

Decision rules:
- If you cannot point to at least one concrete breadcrumb, return NO_LEAD.
- Do not classify a job as a lead based on title, skill tags, budget, or urgency alone.
- Prefer NO_LEAD when the post is generic, broad, or ambiguous.

Respond with ONLY the JSON as instructed."""

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=SYSTEM_PROMPT + "\n\n" + prompt,
            config=types.GenerateContentConfig(
                temperature=0.0,
                max_output_tokens=2048,
            ),
        )

        text = (response.text or "").strip()

        if text.startswith("```json"):
            text = text[7:]
        elif text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

        return json.loads(text)

    except Exception as e:
        title_preview = job.get("title", "")[:40]
        print(f"{Fore.YELLOW}  [!] Analyzer error for \"{title_preview}...\": {e}{Style.RESET_ALL}")
        return {"status": "ERROR", "error": str(e)}
