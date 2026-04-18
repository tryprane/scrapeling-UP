"""
LLM job analyzer.

Classifies Upwork jobs as lead / no-lead and returns structured JSON.
"""

from __future__ import annotations

import json

from colorama import Fore, Style

from llm_client import get_client, get_model_name, get_provider

SYSTEM_PROMPT = """You are Lead Hunter AI for Femur Studio.
Decide whether an Upwork post contains a real externally reachable lead.
Return NO_LEAD unless there is at least one concrete breadcrumb.
Generic hiring language, role titles, budgets, skills lists, urgency, and vague descriptions do not count.
Do not infer a company name or person from the job title alone.
If unsure, choose NO_LEAD.

Return only JSON with this shape:
{
  "status": "LEAD_FOUND" or "NO_LEAD",
  "confidence_score": "High" or "Medium" or null,
  "client_info": {
    "company_name": "",
    "guessed_person": "",
    "website": "",
    "search_query_used": ""
  },
  "contact_strategy": {
    "email_subject": "",
    "cold_outreach_message": ""
  },
  "evidence": [],
  "reason": ""
}

For NO_LEAD, keep the lead fields empty and evidence empty."""


def _normalize_result(data: dict) -> dict:
    """Fill missing keys so downstream code always gets the same shape."""
    client_info = data.get("client_info") or {}
    contact_strategy = data.get("contact_strategy") or {}
    evidence = data.get("evidence") or []

    return {
        "status": data.get("status", "ERROR"),
        "confidence_score": data.get("confidence_score"),
        "client_info": {
            "company_name": client_info.get("company_name", ""),
            "guessed_person": client_info.get("guessed_person", ""),
            "website": client_info.get("website", ""),
            "search_query_used": client_info.get("search_query_used", ""),
        },
        "contact_strategy": {
            "email_subject": contact_strategy.get("email_subject", ""),
            "cold_outreach_message": contact_strategy.get("cold_outreach_message", ""),
        },
        "evidence": evidence if isinstance(evidence, list) else [],
        "reason": data.get("reason", ""),
    }


def _build_user_prompt(job: dict) -> str:
    """Build the prompt for lead classification."""
    return f"""Analyze this Upwork job posting for identifiable client information ("breadcrumbs").

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

Respond with only JSON."""


def analyze_job(job: dict) -> dict:
    """
    Analyze a single job description for client breadcrumbs.

    Returns the parsed JSON response from the configured LLM backend.
    """
    client = get_client()
    prompt = _build_user_prompt(job)
    model_name = get_model_name("analyzer")
    provider = get_provider()

    try:
        response = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
            max_tokens=512,
            response_format={"type": "json_object"},
        )

        if provider == "codex_oauth":
            text = response.choices[0].message.content or "{}"
        else:
            text = (response.choices[0].message.content or "").strip()
        parsed = json.loads(text)
        return _normalize_result(parsed)

    except Exception as exc:
        title_preview = job.get("title", "")[:40]
        print(f"{Fore.YELLOW}  [!] Analyzer error for \"{title_preview}...\": {exc}{Style.RESET_ALL}")
        return {
            "status": "ERROR",
            "error": str(exc),
            "confidence_score": None,
            "client_info": {
                "company_name": "",
                "guessed_person": "",
                "website": "",
                "search_query_used": "",
            },
            "contact_strategy": {
                "email_subject": "",
                "cold_outreach_message": "",
            },
            "evidence": [],
            "reason": str(exc),
        }
