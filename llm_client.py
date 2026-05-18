"""
Shared LLM client factory.

Supports:
- Codex OAuth PKCE with a direct backend compatibility layer
- OpenAI API / OpenAI-compatible local proxy
- Groq
- AgentRouter / DeepSeek
- automatic fallback when the primary provider hits quota, auth, rate, or
  transient server issues
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from config import config

_CLIENT: Any = None
_PROVIDER: str = ""
_FALLBACK_PROVIDER: str = ""

_CODEX_BACKEND_BASE_URL = "https://chatgpt.com/backend-api/codex"


def describe_exception(exc: Exception) -> str:
    """Return a readable exception string even when str(exc) is empty."""
    message = str(exc).strip()
    if message:
        return message
    return exc.__class__.__name__


def parse_json_response_text(text: str) -> Any:
    """
    Parse a model response that should contain JSON.

    Handles common model drift such as code fences, leading prose, or trailing
    notes after a valid JSON object/array.
    """
    raw = (text or "").strip()
    if not raw:
        raise ValueError("Model returned an empty response.")

    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines).strip()

    decoder = json.JSONDecoder()
    try:
        return decoder.decode(raw)
    except json.JSONDecodeError:
        pass

    for marker in ("{", "["):
        start = raw.find(marker)
        if start == -1:
            continue
        try:
            parsed, _ = decoder.raw_decode(raw[start:])
            return parsed
        except json.JSONDecodeError:
            continue

    raise ValueError(f"Model did not return valid JSON. First 200 chars: {raw[:200]!r}")


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
    if config.get("agentrouter_api_key"):
        return "agentrouter"
    return "none"


def _fallback_provider_from_config(primary_provider: str) -> str:
    fallback = (config.get("llm_fallback_provider") or "").lower().strip()
    if fallback and fallback != primary_provider:
        return fallback

    if config.get("agentrouter_api_key") and primary_provider != "agentrouter":
        return "agentrouter"

    return ""


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


def _load_agentrouter_client():
    """Create an AgentRouter client for DeepSeek fallback/direct usage."""
    try:
        from agentrouter import Client
    except Exception as exc:
        raise RuntimeError(
            "agentrouter package is not installed. Install the agentrouter dependency on Python 3.11+."
        ) from exc

    if not config.get("agentrouter_api_key"):
        raise RuntimeError("AGENTROUTER_API_KEY is not set.")

    return _AgentRouterCompatClient(
        Client(
            api_key=config["agentrouter_api_key"],
            model=config.get("agentrouter_model", "deepseek-v4-flash"),
            base_url=config.get("agentrouter_base_url", "https://agentrouter.org/v1"),
            timeout=60.0,
        )
    )


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


def _messages_to_agentrouter_payload(
    messages: list[dict[str, Any]] | None,
) -> tuple[str, str | None, list[dict[str, Any]]]:
    """Convert chat-completions messages into AgentRouter ask(...) args."""
    system_parts: list[str] = []
    conversation: list[dict[str, Any]] = []

    for message in messages or []:
        role = str(message.get("role", "user"))
        content = message.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                str(part.get("text", "")) if isinstance(part, dict) else str(part)
                for part in content
            )
        text = str(content).strip()
        if role == "system":
            if text:
                system_parts.append(text)
            continue
        conversation.append({"role": role, "content": text})

    if not conversation:
        conversation = [{"role": "user", "content": ""}]

    prompt = str(conversation[-1].get("content", "")).strip()
    history = conversation[:-1]
    system = "\n\n".join(part for part in system_parts if part).strip() or None
    return prompt, system, history


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
        final_text = ""
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
                    event_type = event.get("type")
                    if event_type == "response.output_text.delta":
                        text_parts.append(event.get("delta", ""))
                    elif event_type == "response.output_text.done":
                        final_text = event.get("text", "") or final_text
                    elif event_type == "response.content_part.done":
                        part = event.get("part") or {}
                        if part.get("type") == "output_text":
                            final_text = part.get("text", "") or final_text
                    elif event_type == "response.completed":
                        break

        content = "".join(text_parts).strip() or final_text.strip()
        if not content:
            raise RuntimeError("Codex backend returned empty response text.")
        return _ChatCompletionResponse(content)


class _AgentRouterChatCompletions:
    def __init__(self, outer: "_AgentRouterCompatClient") -> None:
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


class _AgentRouterChatNamespace:
    def __init__(self, outer: "_AgentRouterCompatClient") -> None:
        self.completions = _AgentRouterChatCompletions(outer)


class _AgentRouterCompatClient:
    """OpenAI-style compatibility wrapper for the AgentRouter SDK."""

    def __init__(self, sdk_client: Any) -> None:
        self._sdk_client = sdk_client
        self.chat = _AgentRouterChatNamespace(self)

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
        prompt, system, history = _messages_to_agentrouter_payload(messages)
        agentrouter_messages: list[dict[str, Any]] = []
        if system:
            agentrouter_messages.append({"role": "system", "content": system})
        agentrouter_messages.extend(history)
        agentrouter_messages.append({"role": "user", "content": prompt})

        body: dict[str, Any] = {
            "model": model,
            "messages": agentrouter_messages,
            "stream": False,
        }
        if temperature is not None:
            body["temperature"] = temperature
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        _ = response_format
        _ = kwargs

        payload = self._sdk_client._transport.request("POST", "/chat/completions", json_body=body)

        content = ""
        if isinstance(payload, dict):
            choices = payload.get("choices") or []
            if choices:
                first = choices[0] or {}
                message = first.get("message") or {}
                content = str(message.get("content") or first.get("text") or "").strip()
            if not content:
                content = str(payload.get("output_text") or "").strip()
        if not content:
            raise RuntimeError("AgentRouter returned empty response text.")
        return _ChatCompletionResponse(content)


class _FallbackChatCompletions:
    def __init__(
        self,
        primary_client: Any,
        fallback_client: Any,
        primary_provider: str,
        fallback_provider: str,
    ) -> None:
        self._primary_client = primary_client
        self._fallback_client = fallback_client
        self._primary_provider = primary_provider
        self._fallback_provider = fallback_provider

    def create(self, **kwargs: Any) -> Any:
        try:
            return self._primary_client.chat.completions.create(**kwargs)
        except Exception as exc:
            if not _should_use_fallback(exc):
                raise
            fallback_kwargs = dict(kwargs)
            fallback_kwargs["model"] = _map_model_for_fallback(
                requested_model=str(kwargs.get("model", "")),
                primary_provider=self._primary_provider,
                fallback_provider=self._fallback_provider,
            )
            print(
                f"[llm-fallback] {self._primary_provider} failed: "
                f"{describe_exception(exc)} -> retrying with {self._fallback_provider}"
            )
            return self._fallback_client.chat.completions.create(**fallback_kwargs)


class _FallbackChatNamespace:
    def __init__(
        self,
        primary_client: Any,
        fallback_client: Any,
        primary_provider: str,
        fallback_provider: str,
    ) -> None:
        self.completions = _FallbackChatCompletions(
            primary_client,
            fallback_client,
            primary_provider,
            fallback_provider,
        )


class _FallbackCompatClient:
    """Thin wrapper that retries model calls on a fallback provider."""

    def __init__(
        self,
        primary_client: Any,
        fallback_client: Any,
        primary_provider: str,
        fallback_provider: str,
    ) -> None:
        self.primary_client = primary_client
        self.fallback_client = fallback_client
        self.primary_provider = primary_provider
        self.fallback_provider = fallback_provider
        self.chat = _FallbackChatNamespace(
            primary_client,
            fallback_client,
            primary_provider,
            fallback_provider,
        )


def _should_use_fallback(exc: Exception) -> bool:
    if not config.get("llm_fallback_on_errors", True):
        return False

    message = describe_exception(exc).lower()
    fallback_markers = (
        "429",
        "quota",
        "rate limit",
        "too many requests",
        "insufficient_quota",
        "authentication",
        "unauthorized",
        "forbidden",
        "token expired",
        "status 500",
        "status 502",
        "status 503",
        "status 504",
        "service unavailable",
        "connection reset",
        "timed out",
        "timeout",
    )
    return any(marker in message for marker in fallback_markers)


def _model_name_for_provider(provider: str, purpose: str) -> str:
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
    if provider == "agentrouter":
        if purpose == "analyzer":
            return config.get("agentrouter_analyzer_model", "deepseek-v4-flash")
        return config.get("agentrouter_model", "deepseek-v4-flash")
    raise RuntimeError(f"Unknown LLM provider: {provider}")


def _map_model_for_fallback(
    *,
    requested_model: str,
    primary_provider: str,
    fallback_provider: str,
) -> str:
    primary_analyzer = _model_name_for_provider(primary_provider, "analyzer")
    primary_draft = _model_name_for_provider(primary_provider, "draft")
    purpose = "analyzer" if requested_model == primary_analyzer else "draft"
    return _model_name_for_provider(fallback_provider, purpose)


def _create_provider_client(provider: str) -> Any:
    if provider == "codex_oauth":
        return _load_oauth_codex_client()
    if provider in {"openai_proxy", "openai_api"}:
        return _load_openai_client()
    if provider == "groq":
        return _load_groq_client()
    if provider == "agentrouter":
        return _load_agentrouter_client()
    raise RuntimeError(f"Unknown LLM provider: {provider}")


def get_client():
    """Return the singleton client and cache the chosen provider."""
    global _CLIENT, _PROVIDER, _FALLBACK_PROVIDER
    if _CLIENT is not None:
        return _CLIENT

    provider = _provider_from_config()
    if provider == "none":
        raise RuntimeError(
            "No LLM provider is configured. Set CODEX_OAUTH_ENABLED, "
            "OPENAI_BASE_URL / OPENAI_API_KEY, GROQ_API_KEY, or AGENTROUTER_API_KEY."
        )

    primary_client = _create_provider_client(provider)
    fallback_provider = _fallback_provider_from_config(provider)
    if fallback_provider:
        fallback_client = _create_provider_client(fallback_provider)
        _CLIENT = _FallbackCompatClient(primary_client, fallback_client, provider, fallback_provider)
    else:
        _CLIENT = primary_client

    _PROVIDER = provider
    _FALLBACK_PROVIDER = fallback_provider
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
    return _model_name_for_provider(provider, purpose)
