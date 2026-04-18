"""
Interactive OAuth login helper for oauth-codex.

Run this on a machine with Python 3.11+:
    python codex_login.py
"""

from __future__ import annotations

def main():
    try:
        from oauth_codex import Client
    except Exception as exc:
        raise SystemExit(
            "oauth-codex is not available here. Use Python 3.11+ and install requirements first."
        ) from exc

    client = Client()
    client.authenticate()
    print("oauth-codex authentication complete.")


if __name__ == "__main__":
    main()
