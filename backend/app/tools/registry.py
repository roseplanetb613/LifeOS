"""Tool Registry — central place to register and discover tools."""
from app.tools.base import Tool


class ToolRegistry:
    _tools: dict[str, Tool] = {}

    @classmethod
    def register(cls, tool: Tool):
        cls._tools[tool.name] = tool

    @classmethod
    def get(cls, name: str) -> Tool | None:
        return cls._tools.get(name)

    @classmethod
    def list_all(cls) -> list[Tool]:
        return list(cls._tools.values())

    @classmethod
    def get_schemas(cls) -> list[dict]:
        """Return OpenAI-compatible function definitions for all tools."""
        return [
            {
                "type": "function",
                "function": {
                    "name": t.schema.name,
                    "description": t.schema.description,
                    "parameters": t.schema.parameters,
                },
            }
            for t in cls._tools.values()
        ]
