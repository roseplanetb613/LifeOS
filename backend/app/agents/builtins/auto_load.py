"""Auto-register all builtin agents on import."""
from app.agents.registry import AgentRegistry
from app.agents.builtins.conversation import LifeOSAgent

AgentRegistry.register(LifeOSAgent())
