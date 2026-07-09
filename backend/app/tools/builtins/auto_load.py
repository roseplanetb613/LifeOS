"""Auto-register all builtin tools on import."""
from app.tools.registry import ToolRegistry
from app.tools.builtins.memory_query import MemoryQueryTool
from app.tools.builtins.memory_save import SaveMemoryTool, ForgetMemoryTool
from app.tools.builtins.time_tools import GetTimeTool

ToolRegistry.register(MemoryQueryTool())
ToolRegistry.register(SaveMemoryTool())
ToolRegistry.register(ForgetMemoryTool())
ToolRegistry.register(GetTimeTool())
