"""Orchestrator — decompose user requests into sub-tasks and route to agents."""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from app.agents.base import Agent


@dataclass
class SubTask:
    """A sub-task decomposed from the user's request."""
    id: str
    description: str
    agent_name: str | None = None   # auto-routed if None
    status: str = "pending"
    result: str | None = None


@dataclass
class Plan:
    """Execution plan: a list of sub-tasks with dependencies."""
    tasks: list[SubTask]
    reasoning: str = ""


class Orchestrator(ABC):
    """Base class for task decomposition and routing."""

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @abstractmethod
    async def decompose(self, user_message: str, context: dict | None = None) -> Plan:
        """Break down a user request into sub-tasks."""
        ...

    async def execute(self, plan: Plan, agents: dict[str, Agent],
                      messages: list[dict]) -> list[SubTask]:
        """Execute a plan sequentially, returning completed tasks."""
        for task in plan.tasks:
            task.status = "running"

            agent = agents.get(task.agent_name) if task.agent_name else None
            if not agent:
                # Default: route to first available agent
                agent = next(iter(agents.values()), None)

            if not agent:
                task.status = "failed"
                task.result = "No agent available"
                continue

            task_messages = [
                {"role": "system", "content": agent.system_prompt},
                {"role": "user", "content": task.description},
            ]
            result_parts = []
            async for token in agent.loop(task_messages, context):
                result_parts.append(token)

            task.result = "".join(result_parts)
            task.status = "done"

        return plan.tasks
