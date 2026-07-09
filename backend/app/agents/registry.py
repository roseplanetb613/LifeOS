"""Agent Registry — central place to register and discover agents."""
from app.agents.base import Agent


class AgentRegistry:
    _agents: dict[str, Agent] = {}

    @classmethod
    def register(cls, agent: Agent):
        cls._agents[agent.name] = agent

    @classmethod
    def get(cls, name: str) -> Agent | None:
        return cls._agents.get(name)

    @classmethod
    def list_all(cls) -> list[Agent]:
        return list(cls._agents.values())
