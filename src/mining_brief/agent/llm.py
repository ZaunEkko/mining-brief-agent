"""Optional LLM backends. The agent works without any of them.

Two capabilities:

* ``complete_json`` - one structured-output call (brief composition);
* ``tool_session`` - a multi-turn tool-use conversation (ask mode). Each
  provider keeps its history in its native format: Anthropic assistant turns are
  appended unchanged, as required for thinking blocks on current models.
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

import anthropic
import httpx

from mining_brief.common.config import Settings

logger = logging.getLogger(__name__)

MAX_TOKENS = 16000
_FALLBACK_BETA = "server-side-fallback-2026-07-01"
OPENAI_ATTEMPTS = 3
OPENAI_BACKOFF_S = 2.0
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class LLMError(Exception):
    """The model call failed or returned unusable output."""


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolResult:
    call_id: str
    content: str
    is_error: bool = False


@dataclass(slots=True)
class AssistantTurn:
    text: str | None
    calls: list[ToolCall] = field(default_factory=list)


class ToolSession(Protocol):
    async def start(self, user: str) -> AssistantTurn: ...

    async def submit(self, results: list[ToolResult]) -> AssistantTurn: ...


class LLMClient(Protocol):
    @property
    def name(self) -> str: ...

    async def complete_json(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        """Return a JSON object that conforms to ``schema``."""
        ...

    def tool_session(self, system: str, tools: list[ToolSpec]) -> ToolSession:
        """Start a tool-use conversation; the model decides which tools to call."""
        ...


# --- Anthropic ----------------------------------------------------------------


class AnthropicLLM:
    def __init__(self, settings: Settings, client: anthropic.AsyncAnthropic | None = None) -> None:
        key = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else None
        # Without an explicit key the SDK resolves credentials itself (auth token, profile).
        self._client = client or anthropic.AsyncAnthropic(api_key=key)
        self._model = settings.anthropic_model
        self._effort = settings.anthropic_effort
        self._fallback = settings.anthropic_server_fallback

    @property
    def name(self) -> str:
        return f"anthropic:{self._model}"

    async def create(self, **kwargs: Any) -> Any:
        """``beta.messages.create`` with the shared model, effort, fallback and error mapping."""
        extra: dict[str, Any] = (
            {"betas": [_FALLBACK_BETA], "fallbacks": "default"} if self._fallback else {}
        )
        output_config = {"effort": self._effort, **kwargs.pop("output_config", {})}
        # kwargs are forwarded verbatim; the SDK's overloads cannot type a dynamic call.
        create = cast(Any, self._client.beta.messages.create)
        try:
            response = await create(
                model=self._model,
                max_tokens=MAX_TOKENS,
                output_config=output_config,
                **kwargs,
                **extra,
            )
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"cannot reach Anthropic API: {exc}") from exc
        except anthropic.RateLimitError as exc:
            raise LLMError("Anthropic rate limit reached") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc
        if response.stop_reason == "refusal":
            raise LLMError("model declined the request")
        if response.stop_reason == "max_tokens":
            raise LLMError("model output was truncated")
        return response

    async def complete_json(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        response = await self.create(
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            raise LLMError("response contained no text block")
        return _loads(text)

    def tool_session(self, system: str, tools: list[ToolSpec]) -> ToolSession:
        return _AnthropicToolSession(self, system, tools)


class _AnthropicToolSession:
    def __init__(self, llm: AnthropicLLM, system: str, tools: list[ToolSpec]) -> None:
        self._llm = llm
        self._system = system
        self._tools = [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema}
            for t in tools
        ]
        self._messages: list[dict[str, Any]] = []

    async def start(self, user: str) -> AssistantTurn:
        self._messages.append({"role": "user", "content": user})
        return await self._turn()

    async def submit(self, results: list[ToolResult]) -> AssistantTurn:
        blocks = [
            {
                "type": "tool_result",
                "tool_use_id": r.call_id,
                "content": r.content,
                "is_error": r.is_error,
            }
            for r in results
        ]
        self._messages.append({"role": "user", "content": blocks})
        return await self._turn()

    async def _turn(self) -> AssistantTurn:
        response = await self._llm.create(
            system=self._system, tools=self._tools, messages=self._messages
        )
        # Append the assistant content unchanged (thinking blocks must round-trip as-is).
        self._messages.append({"role": "assistant", "content": response.content})
        texts = [b.text for b in response.content if b.type == "text"]
        calls = [
            ToolCall(id=b.id, name=b.name, arguments=dict(b.input))
            for b in response.content
            if b.type == "tool_use"
        ]
        return AssistantTurn(text="\n".join(texts).strip() or None, calls=calls)


# --- OpenAI-compatible ----------------------------------------------------------


class OpenAICompatibleLLM:
    """Any ``/chat/completions`` endpoint (DeepSeek, Qwen/DashScope, GLM, ...)."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        if not (settings.openai_base_url and settings.openai_api_key and settings.openai_model):
            raise ValueError(
                "MB_OPENAI_BASE_URL, MB_OPENAI_API_KEY and MB_OPENAI_MODEL are required"
            )
        self._url = settings.openai_base_url.rstrip("/") + "/chat/completions"
        self._key = settings.openai_api_key.get_secret_value()
        self._model = settings.openai_model
        self._client = client or httpx.AsyncClient(timeout=180)

    @property
    def name(self) -> str:
        return f"openai-compatible:{self._model}"

    async def complete_json(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        message = await self.chat(
            {
                "messages": [
                    {
                        "role": "system",
                        "content": f"{system}\n\nRespond with one JSON object matching this "
                        "JSON Schema:\n" + json.dumps(schema, ensure_ascii=False),
                    },
                    {"role": "user", "content": user},
                ],
                "response_format": {"type": "json_object"},
            }
        )
        text = message.get("content")
        if not isinstance(text, str):
            raise LLMError("chat completion returned no text content")
        return _loads(text)

    def tool_session(self, system: str, tools: list[ToolSpec]) -> ToolSession:
        return _OpenAIToolSession(self, system, tools)

    async def chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        """One completion; returns ``choices[0].message``."""
        response = await self._post({"model": self._model, **payload})
        content_type = response.headers.get("content-type", "")
        if "json" not in content_type:
            # Typical misconfiguration: base URL without "/v1" hits the provider's web page.
            raise LLMError(
                f"{self._url} returned {content_type or 'no content-type'}, not JSON; "
                "check MB_OPENAI_BASE_URL (most providers need a trailing /v1)"
            )
        try:
            message = response.json()["choices"][0]["message"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise LLMError(f"unexpected chat completion payload: {exc!r}") from exc
        if not isinstance(message, dict):
            raise LLMError("chat completion message is not an object")
        return message

    async def _post(self, payload: dict[str, Any]) -> httpx.Response:
        """POST with retries on transport errors and 429/5xx; raises LLMError otherwise."""
        headers = {"Authorization": f"Bearer {self._key}"}
        for attempt in range(1, OPENAI_ATTEMPTS + 1):
            try:
                response = await self._client.post(self._url, json=payload, headers=headers)
            except httpx.TransportError as exc:
                if attempt == OPENAI_ATTEMPTS:
                    raise LLMError(f"cannot reach {self._url}: {exc!r}") from exc
            else:
                if response.status_code < 400:
                    return response
                if response.status_code not in _RETRY_STATUSES or attempt == OPENAI_ATTEMPTS:
                    raise LLMError(
                        f"chat completion failed: HTTP {response.status_code} {response.text[:200]}"
                    )
            logger.warning("chat completion attempt %d failed, retrying", attempt)
            await asyncio.sleep(OPENAI_BACKOFF_S * attempt)
        raise AssertionError("unreachable")


class _OpenAIToolSession:
    def __init__(self, llm: OpenAICompatibleLLM, system: str, tools: list[ToolSpec]) -> None:
        self._llm = llm
        self._tools = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.input_schema,
                },
            }
            for t in tools
        ]
        self._messages: list[dict[str, Any]] = [{"role": "system", "content": system}]

    async def start(self, user: str) -> AssistantTurn:
        self._messages.append({"role": "user", "content": user})
        return await self._turn()

    async def submit(self, results: list[ToolResult]) -> AssistantTurn:
        for r in results:
            content = f"ERROR: {r.content}" if r.is_error else r.content
            self._messages.append({"role": "tool", "tool_call_id": r.call_id, "content": content})
        return await self._turn()

    async def _turn(self) -> AssistantTurn:
        message = await self._llm.chat({"messages": self._messages, "tools": self._tools})
        self._messages.append(
            {k: v for k, v in message.items() if k in {"role", "content", "tool_calls"}}
        )
        calls: list[ToolCall] = []
        for raw in message.get("tool_calls") or []:
            fn = raw.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"_invalid_json": fn.get("arguments")}
            calls.append(ToolCall(id=raw["id"], name=fn.get("name", ""), arguments=args))
        text = message.get("content")
        return AssistantTurn(
            text=text.strip() if isinstance(text, str) and text.strip() else None, calls=calls
        )


def _loads(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMError(f"model returned invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise LLMError("model returned JSON that is not an object")
    return data


def build_llm(settings: Settings) -> LLMClient | None:
    provider = settings.llm_provider
    if provider == "none":
        return None
    if provider == "anthropic" or (provider == "auto" and settings.anthropic_api_key):
        return AnthropicLLM(settings)
    if provider == "openai" or (provider == "auto" and settings.openai_api_key):
        return OpenAICompatibleLLM(settings)
    return None
