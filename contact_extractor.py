"""
Contact Extractor — Uses Gemini AI to extract structured contacts from Grok's response.

Takes the raw text response from Grok and extracts:
- Email addresses
- LinkedIn URLs
- Instagram handles
- Other contact info

Can be tested standalone:
    python -c "from contact_extractor import extract_contacts; print(extract_contacts('Email: john@company.com, LinkedIn: linkedin.com/in/john'))"
"""

import json
from google import genai
from google.genai import types
from colorama import Fore, Style

from config import config

_client = None

EXTRACTION_PROMPT = """You are a contact information extractor. Given a text response from an AI search about a company/person, extract ALL contact information found.

OUTPUT ONLY THIS JSON, NOTHING ELSE:
{
  "emails": ["list of email addresses found"],
  "linkedin_urls": ["list of LinkedIn profile/company URLs"],
  "instagram_handles": ["list of Instagram handles or URLs"],
  "twitter_handles": ["list of Twitter/X handles or URLs"],
  "websites": ["list of website URLs"],
  "phone_numbers": ["list of phone numbers"],
  "other_contacts": ["any other contact methods found"],
  "summary": "Brief 1-2 sentence summary of who this person/company is"
}

RULES:
- Only include REAL contact info that was actually mentioned in the text
- Do NOT make up or guess contact info
- Remove duplicates
- For LinkedIn, include the full URL if available
- For Instagram/Twitter, include the @ handle
- If no contacts found for a category, use an empty list []
- Output ONLY valid JSON. No markdown, no code fences, no explanation."""


def _get_client():
    """Get or create the Gemini client instance."""
    global _client
    if _client:
        return _client

    if not config['gemini_api_key']:
        raise RuntimeError('GEMINI_API_KEY is not set — cannot extract contacts.')

    _client = genai.Client(api_key=config['gemini_api_key'])
    return _client


def extract_contacts(grok_response: str) -> dict:
    """
    Extract structured contact information from a Grok AI response.

    Args:
        grok_response: Raw text response from Grok

    Returns:
        Dict with keys: emails, linkedin_urls, instagram_handles, twitter_handles,
        websites, phone_numbers, other_contacts, summary
    """
    if not grok_response or len(grok_response.strip()) < 10:
        print(f"{Fore.YELLOW}  ⚠ Grok response too short to extract contacts{Style.RESET_ALL}")
        return _empty_result()

    client = _get_client()

    prompt = f"""Extract all contact information from this text:

{grok_response}

Respond with ONLY the JSON as instructed."""

    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=EXTRACTION_PROMPT + '\n\n' + prompt,
            config=types.GenerateContentConfig(
                temperature=0.1,
                max_output_tokens=2048,
            ),
        )

        text = response.text.strip()

        # Strip markdown code fences if present
        if text.startswith('```json'):
            text = text[7:]
        elif text.startswith('```'):
            text = text[3:]
        if text.endswith('```'):
            text = text[:-3]
        text = text.strip()

        parsed = json.loads(text)

        # Count total contacts found
        total = (
            len(parsed.get('emails', [])) +
            len(parsed.get('linkedin_urls', [])) +
            len(parsed.get('instagram_handles', []))
        )

        if total > 0:
            print(f"{Fore.GREEN}  ✓ Extracted {total} contact(s): "
                  f"{len(parsed.get('emails', []))} emails, "
                  f"{len(parsed.get('linkedin_urls', []))} LinkedIn, "
                  f"{len(parsed.get('instagram_handles', []))} Instagram{Style.RESET_ALL}")
        else:
            print(f"{Fore.YELLOW}  ⚠ No contacts found in Grok response{Style.RESET_ALL}")

        return parsed

    except Exception as e:
        print(f"{Fore.RED}  ✗ Contact extraction error: {e}{Style.RESET_ALL}")
        return _empty_result(str(e))


def _empty_result(error=''):
    """Return an empty contact result."""
    return {
        'emails': [],
        'linkedin_urls': [],
        'instagram_handles': [],
        'twitter_handles': [],
        'websites': [],
        'phone_numbers': [],
        'other_contacts': [],
        'summary': '',
        'error': error,
    }


# ── Standalone test ──────────────────────────────────────────────────
if __name__ == '__main__':
    test_response = """
    Based on my search, I found the following information about TechVault Solutions:

    The company is run by Dave Martinez who is the CEO.
    - Email: dave@techvault.io, contact@techvault.io
    - LinkedIn: https://linkedin.com/in/davemartinez
    - Instagram: @techvault_solutions
    - Website: https://techvault.io
    - Twitter: @TechVaultHQ

    They are a B2B SaaS company based in Austin, TX focused on analytics tools.
    """

    result = extract_contacts(test_response)
    print(f"\n{'='*60}")
    print(json.dumps(result, indent=2))
    print(f"{'='*60}")
