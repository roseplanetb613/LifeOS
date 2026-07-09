"""MemoryQueryTool — let the agent search the user's long-term memory on demand."""
import json
from app.tools.base import Tool, ToolResult, ToolSchema
from app.services.rag import rag_service


class MemoryQueryTool(Tool):
    """Search the user's long-term memories and past conversation records."""

    @property
    def name(self) -> str:
        return "memory_query"

    @property
    def description(self) -> str:
        return (
            "搜索用户的长期记忆和过往对话记录。"
            "当用户询问之前提过的信息、偏好、计划、项目时调用此工具。"
            "query 应尽量保留用户原话，不要自行总结，不要改写，不要推测。"
        )

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "中文搜索关键词，用自然语言描述要查什么",
                    },
                    "top_k": {
                        "type": "integer",
                        "default": 5,
                        "description": "返回的记忆条数，默认5",
                    },
                },
                "required": ["query"],
            },
        )

    async def run(self, query: str, top_k: int = 5) -> ToolResult:
        """Search ChromaDB for relevant memories. Returns structured JSON."""
        try:
            tiered = rag_service.search_by_tier(query, top_k=top_k, min_score=0.15)
            long_mem = tiered.get("long", [])
            medium_mem = tiered.get("medium", [])

            all_results = long_mem + medium_mem
            seen = set()
            deduped = []
            for text, score in all_results:
                if text not in seen:
                    seen.add(text)
                    tier = "long" if (text, score) in long_mem else "medium"
                    deduped.append({"content": text, "score": round(score, 2), "tier": tier})

            return ToolResult(
                success=True,
                data=json.dumps({
                    "query": query,
                    "found": len(deduped),
                    "results": deduped,
                }, ensure_ascii=False),
            )

        except Exception as e:
            return ToolResult(success=False, error=str(e))
