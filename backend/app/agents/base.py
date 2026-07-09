"""Agent abstraction — think → act → observe loop."""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncGenerator
from app.tools.base import Tool


@dataclass
class Thought:
    """Agent's reasoning output."""
    reasoning: str
    action: str | None = None        # "use_tool" | "respond" | "delegate"
    tool_name: str | None = None
    tool_params: dict | None = None
    tool_call_id: str | None = None  # LLM's tool_call id, required for tool result
    response: str | None = None      # direct text response


@dataclass
class Observation:
    """Result of an action."""
    success: bool
    data: str = ""
    error: str | None = None


class Agent(ABC):
    """Base class for all agents."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique agent identifier."""
        ...

    @property
    @abstractmethod
    def system_prompt(self) -> str:
        """System prompt that defines this agent's role and behavior."""
        ...

    @property
    def tools(self) -> list[Tool]:
        """Tools available to this agent."""
        return []

    @abstractmethod
    async def think(self, messages: list[dict], context: dict | None = None) -> Thought:
        """Reason about what to do next."""
        ...

    async def act(self, tool_name: str, **params) -> Observation:
        """Execute a tool and return the observation."""
        from app.tools.registry import ToolRegistry
        tool = ToolRegistry.get(tool_name)
        if not tool:
            return Observation(success=False, error=f"Unknown tool: {tool_name}")
        try:
            result = await tool.run(**params)
            return Observation(success=result.success, data=str(result.data), error=result.error)
        except Exception as e:
            return Observation(success=False, error=str(e))

    async def loop(self, messages: list[dict], context: dict | None = None,
                   max_steps: int = 5) -> AsyncGenerator[str, None]:
        """
        Think → Act → Observe loop. Yields tokens for streaming.
        Stops when agent decides to respond or max_steps reached.
        """
        for _ in range(max_steps):
            thought = await self.think(messages, context)

            if thought.action == "respond" and thought.response:
                yield thought.response
                return

            if thought.action == "use_tool" and thought.tool_name:
                obs = await self.act(thought.tool_name, **(thought.tool_params or {}))
                messages.append({"role": "tool", "content": f"[{thought.tool_name}]: {obs.data or obs.error}"})
                continue

            # Fallback: agent couldn't decide
            yield "(无法决定下一步动作)"
            return
