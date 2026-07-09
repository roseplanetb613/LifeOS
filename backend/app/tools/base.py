"""Tool abstraction — LLM can invoke tools via function calling."""
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolResult:
    success: bool
    data: Any = None       # str (JSON) or any serializable object
    error: str | None = None

    @staticmethod
    def ok(data: dict | list | str) -> "ToolResult":
        """Create a successful result with JSON-serialized data.

        All tools should use this for structured output:
            return ToolResult.ok({"action": "created", "id": "abc123", ...})
        """
        if isinstance(data, str):
            return ToolResult(success=True, data=data)
        return ToolResult(success=True, data=json.dumps(data, ensure_ascii=False))

    @staticmethod
    def fail(error: str) -> "ToolResult":
        """Create a failed result."""
        return ToolResult(success=False, error=error)


@dataclass
class ToolSchema:
    """OpenAI-compatible function definition for LLM tool calling."""
    name: str
    description: str
    parameters: dict  # JSON Schema for parameters


class Tool(ABC):
    """Base class for all tools.

    To create a new tool:
    1. Inherit from this class
    2. Define name, description, schema, permission
    3. Implement run() → return ToolResult.ok({"action": "...", ...})
    4. Register in auto_load.py

    Permission levels:
      - "local"    → 只读/只写本地数据，无需确认（memory_query, save_memory, forget_memory）
      - "restricted" → 外部 API/网络，Agent 自动请求用户确认
      - "system"   → 仅内部调用，不暴露给 LLM（不注册到 ToolRegistry）
    """

    # ── Tool authors: override these 4 + run() ──

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique tool identifier, e.g. 'web_search', 'memory_query'."""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """Human-readable description. LLM uses this to decide when to invoke."""
        ...

    @property
    @abstractmethod
    def schema(self) -> ToolSchema:
        """JSON Schema for LLM function calling."""
        ...

    @property
    def permission(self) -> str:
        """Permission level: 'local' | 'restricted' | 'system'.

        - local (default): local data only, no confirmation needed
        - restricted: external API / network, needs user confirmation
        - system: internal use only, never exposed to LLM
        """
        return "local"

    @abstractmethod
    async def run(self, **params) -> ToolResult:
        """Execute the tool with given parameters.

        Return: ToolResult.ok({"action": "...", "key": value, ...})
        The dict is auto-serialized to JSON for the LLM.
        """
        ...
