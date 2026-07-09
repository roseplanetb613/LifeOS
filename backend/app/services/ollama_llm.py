"""Ollama LLM service — local Qwen for simple tasks, streaming + non-streaming."""
import json
import logging
from dataclasses import dataclass, field
from typing import AsyncGenerator
import httpx
from app.config import get_settings

settings = get_settings()
logger = logging.getLogger("uvicorn")


@dataclass
class OllamaUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class OllamaResponse:
    content: str = ""
    usage: OllamaUsage = field(default_factory=OllamaUsage)


async def ollama_stream_chat(
    messages: list[dict[str, str]],
    model: str = "qwen3:8b",
) -> AsyncGenerator[str | OllamaUsage, None]:
    """Stream a chat completion from Ollama. Yields content tokens, then OllamaUsage."""
    base = settings.ollama_base_url
    async with httpx.AsyncClient(timeout=120) as client:
        async with client.stream(
            "POST",
            f"{base}/api/chat",
            json={
                "model": model,
                "messages": messages,
                "stream": True,
                "think": False,
                "options": {
                    "temperature": 0.7,
                    "num_predict": 512,
                },
            },
        ) as response:
            response.raise_for_status()
            prompt_tokens = 0
            completion_tokens = 0
            async for line in response.aiter_lines():
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if chunk.get("done"):
                    prompt_tokens = chunk.get("prompt_eval_count", 0)
                    completion_tokens = chunk.get("eval_count", 0)
                elif "message" in chunk:
                    token = chunk["message"].get("content", "")
                    if token:
                        yield token

            yield OllamaUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            )


async def ollama_chat_complete(
    messages: list[dict[str, str]],
    model: str = "qwen3:8b",
    max_tokens: int = 512,
) -> OllamaResponse:
    """Non-streaming chat completion from Ollama."""
    base = settings.ollama_base_url
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            f"{base}/api/chat",
            json={
                "model": model,
                "messages": messages,
                "stream": False,
                "think": False,
                "options": {
                    "temperature": 0.7,
                    "num_predict": max_tokens,
                },
            },
        )
        response.raise_for_status()
        data = response.json()

        msg = data.get("message", {})
        content = msg.get("content", "")
        prompt_tokens = data.get("prompt_eval_count", 0)
        completion_tokens = data.get("eval_count", 0)

        return OllamaResponse(
            content=content,
            usage=OllamaUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            ),
        )
