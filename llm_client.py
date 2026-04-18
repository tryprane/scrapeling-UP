"""
Shared LLM client factory.

Supports:
- Codex OAuth PKCE with a direct backend compatibility layer
- OpenAI API / OpenAI-compatible local proxy
- Groq fallback
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx

from config import config

_CLIENT: Any = None
_PROVIDER: str = ""

_CODEX_BACKEND_BASE_URL = "https://chatgpt.com/backend-api/codex"


def _provider_from_config() -> str:
    """Choose the active provider from config/env."""
    provider = (config.get("llm_provider") or "auto").lower()
    if provider != "auto":
        return provider

    if config.get("codex_oauth_enabled"):
        return "codex_oauth"
    if config.get("openai_base_url"):
        return "openai_proxy"
    if config.get("openai_api_key"):
        return "openai_api"
    if config.get("groq_api_key"):
        return "groq"
    return "none"


def _load_oauth_codex_client():
    """Create an oauth-codex Client and authenticate once."""
    try:
        from oauth_codex import Client
    except Exception as exc:
        raise RuntimeError(
            "oauth-codex is not available in this Python environment. "
            "It requires Python 3.11+ and an installed oauth-codex package."
        ) from exc

    client = Client()
    client.authenticate()
    return _CodexCompatClient(client)


def _load_openai_client():
    """Create an OpenAI client or OpenAI-compatible proxy client."""
    try:
        from openai import OpenAI
    except Exception as exc:
        raise RuntimeError("openai package is not installed.") from exc

    if config.get("openai_base_url"):
        return OpenAI(
            api_key=config.get("openai_api_key") or "local-proxy",
            base_url=config["openai_base_url"],
        )
    return OpenAI(api_key=config.get("openai_api_key") or "")


def _load_groq_client():
    """Create a Groq client."""
    try:
        from groq import Groq
    except Exception as exc:
        raise RuntimeError("groq package is not installed.") from exc

    if not config.get("groq_api_key"):
        raise RuntimeError("GROQ_API_KEY is not set.")
    return Groq(api_key=config["groq_api_key"])


def _dedupe_keep_order(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop duplicate chat messages while preserving order."""
    seen: set[tuple[str, str]] = set()
    ordered: list[dict[str, Any]] = []
    for item in items:
        role = str(item.get("role", "user"))
        content = item.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                str(part.get("text", "")) if isinstance(part, dict) else str(part)
                for part in content
            )
        normalized = (role, str(content))
        if normalized in seen:
            continue
        seen.add(normalized)
        ordered.append({"role": role, "content": content})
    return ordered


def _messages_to_codex_payload(messages: list[dict[str, Any]] | None) -> tuple[str, list[dict[str, Any]]]:
    """Convert chat-completions messages into Codex responses payload fields."""
    instructions: list[str] = []
    input_messages: list[dict[str, Any]] = []

    for message in messages or []:
        role = str(message.get("role", "user"))
        content = message.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                str(part.get("text", "")) if isinstance(part, dict) else str(part)
                for part in content
            )
        if role == "system":
            text = str(content).strip()
            if text:
                instructions.append(text)
            continue
        input_messages.append({"role": role, "content": content})

    joined_instructions = "\n\n".join(instructions).strip()
    if not joined_instructions:
        joined_instructions = "You are a helpful assistant."

    input_messages = _dedupe_keep_order(input_messages)
    if not input_messages:
        input_messages = [{"role": "user", "content": ""}]

    return joined_instructions, input_messages


class _ChatCompletionMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _ChatCompletionChoice:
    def __init__(self, content: str) -> None:
        self.message = _ChatCompletionMessage(content)


class _ChatCompletionResponse:
    def __init__(self, content: str) -> None:
        self.choices = [_ChatCompletionChoice(content)]


class _CodexChatCompletions:
    def __init__(self, outer: "_CodexCompatClient") -> None:
        self._outer = outer

    def create(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> _ChatCompletionResponse:
        return self._outer._create_completion(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
            **kwargs,
        )


class _CodexChatNamespace:
    def __init__(self, outer: "_CodexCompatClient") -> None:
        self.completions = _CodexChatCompletions(outer)


class _CodexCompatClient:
    """
    Thin compatibility layer for Codex OAuth.

    The oauth-codex SDK is used only for authentication and token storage.
    Actual model calls go directly to the authenticated Codex backend so we can
    keep the same chat-completions style that the rest of the app expects.
    """

    def __init__(self, auth_client: Any) -> None:
        self._auth_client = auth_client
        self.chat = _CodexChatNamespace(self)

    @property
    def auth(self) -> Any:
        return self._auth_client.auth

    def _create_completion(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> _ChatCompletionResponse:
        instructions, input_messages = _messages_to_codex_payload(messages)

        payload: dict[str, Any] = {
            "model": model,
            "instructions": instructions,
            "input": input_messages,
            "stream": True,
            "store": False,
        }
        # The Codex backend rejects several OpenAI-style extras, so we keep the
        # request narrow and ignore temperature, max_tokens, response_format,
        # and other compat-only kwargs.
        _ = temperature
        _ = max_tokens
        _ = response_format
        _ = kwargs

        headers = dict(self._auth_client.auth.get_headers())
        headers["Content-Type"] = "application/json"

        text_parts: list[str] = []
        with httpx.Client(timeout=60.0, headers=headers) as http_client:
            with http_client.stream(
                "POST",
                f"{_CODEX_BACKEND_BASE_URL}/responses",
                json=payload,
            ) as response:
                if response.status_code >= 400:
                    body = response.read().decode("utf-8", errors="replace")
                    raise RuntimeError(
                        f"Codex backend request failed with status {response.status_code}: {body}"
                    )

                for line in response.iter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    try:
                        event = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue
                    if event.get("type") == "response.output_text.delta":
                        text_parts.append(event.get("delta", ""))
                    elif event.get("type") == "response.completed":
                        break

        return _ChatCompletionResponse("".join(text_parts).strip())


def get_client():
    """Return the singleton client and cache the chosen provider."""
    global _CLIENT, _PROVIDER
    if _CLIENT is not None:
        return _CLIENT

    provider = _provider_from_config()
    if provider == "codex_oauth":
        _CLIENT = _load_oauth_codex_client()
    elif provider in {"openai_proxy", "openai_api"}:
        _CLIENT = _load_openai_client()
    elif provider == "groq":
        _CLIENT = _load_groq_client()
    else:
        raise RuntimeError(
            "No LLM provider is configured. Set CODEX_OAUTH_ENABLED, "
            "OPENAI_BASE_URL / OPENAI_API_KEY, or GROQ_API_KEY."
        )

    _PROVIDER = provider
    return _CLIENT


def get_provider() -> str:
    """Return the cached provider name, loading the client if needed."""
    if _PROVIDER:
        return _PROVIDER
    get_client()
    return _PROVIDER


def get_model_name(purpose: str) -> str:
    """
    Resolve the model name for a purpose.

    purpose: "analyzer" or "draft"
    """
    provider = get_provider()
    if provider == "codex_oauth":
        return config.get("codex_model", "gpt-5.3-codex")
    if provider in {"openai_proxy", "openai_api"}:
        if purpose == "analyzer":
            return config.get("openai_analyzer_model", "gpt-5.1")
        return config.get("openai_model", "gpt-5.1")
    if provider == "groq":
        if purpose == "analyzer":
            return config.get("groq_analyzer_model", "llama-3.3-70b-versatile")
        return config.get("groq_model", "openai/gpt-oss-20b")
    raise RuntimeError(f"Unknown LLM provider: {provider}")
