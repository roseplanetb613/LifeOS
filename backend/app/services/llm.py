"""LLM service — DeepSeek API wrapper with streaming, token tracking, and function calling."""
import json
import logging
from dataclasses import dataclass, field
from typing import AsyncGenerator
from openai import AsyncOpenAI
from app.config import get_settings

settings = get_settings()
logger = logging.getLogger("uvicorn")

_client: AsyncOpenAI | None = None


@dataclass
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class LLMResponse:
    """Return type for chat_complete — supports both direct text and tool calls."""
    content: str = ""
    tool_calls: list[dict] | None = None  # [{"name": "...", "arguments": {...}}, ...]
    usage: TokenUsage = field(default_factory=TokenUsage)


def get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
        )
    return _client


async def stream_chat(
    messages: list[dict[str, str]],
    model: str | None = None,
    max_tokens: int | None = None,
    tools: list[dict] | None = None,
    tool_choice: str = "auto",
) -> AsyncGenerator[str | TokenUsage, None]:
    """Stream a chat completion. Yields content tokens, then a TokenUsage at the end."""
    client = get_client()
    kwargs = {
        "model": model or settings.llm_model,
        "messages": messages,
        "max_tokens": max_tokens or settings.llm_max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = tool_choice

    stream = await client.chat.completions.create(**kwargs)
    async for chunk in stream:
        if chunk.choices and chunk.choices[0].delta.content:
            yield chunk.choices[0].delta.content
        if chunk.usage:
            yield TokenUsage(
                prompt_tokens=chunk.usage.prompt_tokens or 0,
                completion_tokens=chunk.usage.completion_tokens or 0,
                total_tokens=chunk.usage.total_tokens or 0,
            )


async def chat_complete(
    messages: list[dict[str, str]],
    model: str | None = None,
    max_tokens: int | None = None,
    tools: list[dict] | None = None,
    tool_choice: str = "auto",
) -> LLMResponse:
    """Non-streaming chat completion. Returns LLMResponse with content, optional tool_calls, and usage."""
    client = get_client()
    kwargs = {
        "model": model or settings.llm_model,
        "messages": messages,
        "max_tokens": max_tokens or settings.llm_max_tokens,
        "stream": False,
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = tool_choice

    response = await client.chat.completions.create(**kwargs)
    msg = response.choices[0].message

    usage = TokenUsage(
        prompt_tokens=response.usage.prompt_tokens or 0 if response.usage else 0,
        completion_tokens=response.usage.completion_tokens or 0 if response.usage else 0,
        total_tokens=response.usage.total_tokens or 0 if response.usage else 0,
    )

    # Parse tool_calls if present
    tool_calls = None
    if msg.tool_calls:
        tool_calls = []
        for tc in msg.tool_calls:
            try:
                arguments = json.loads(tc.function.arguments)
            except (json.JSONDecodeError, TypeError):
                arguments = {}
            tool_calls.append({
                "id": tc.id,
                "name": tc.function.name,
                "arguments": arguments,
            })
        logger.info(f"LLM tool_calls: {[tc['name'] for tc in tool_calls]}")

    return LLMResponse(
        content=msg.content or "",
        tool_calls=tool_calls,
        usage=usage,
    )
