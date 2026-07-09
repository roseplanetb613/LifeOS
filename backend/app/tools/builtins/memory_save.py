"""save_memory + forget_memory — Agent tools for explicit memory management."""
import json
import uuid
from datetime import datetime
from app.tools.base import Tool, ToolResult, ToolSchema
from app.services.rag import rag_service
from app.db.session import async_session
from app.models.chat import Memory
import logging

logger = logging.getLogger("uvicorn")

VALID_TYPES = {"preference", "project", "fact", "plan", "persona"}


def _ok(data: dict) -> str:
    """Serialize structured result as JSON string."""
    return json.dumps(data, ensure_ascii=False)


class SaveMemoryTool(Tool):
    """Explicitly save a fact/memory about the user."""

    @property
    def name(self) -> str:
        return "save_memory"

    @property
    def description(self) -> str:
        return (
            "将一条关于用户的信息明确存入长期记忆。"
            "当用户说'记住这个'、'别忘了'、或者在对话中表达了明确的个人信息时使用。"
        )

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "要记住的内容，详细记录重点信息，例如：'用户喜欢喝黑咖啡'、'用户住在北京朝阳区'",
                    },
                    "mem_type": {
                        "type": "string",
                        "enum": list(VALID_TYPES),
                        "default": "fact",
                        "description": "信息类型：preference(偏好), fact(事实), plan(计划), project(项目), persona(角色设定)",
                    },
                },
                "required": ["content"],
            },
        )

    async def run(self, content: str, mem_type: str = "fact") -> ToolResult:
        """Dedup + dual-write to MySQL + ChromaDB. Returns structured JSON."""
        if mem_type not in VALID_TYPES:
            mem_type = "fact"

        try:
            # ── Dedup check ──
            existing = rag_service.search_with_scores(content, top_k=3)
            if existing and existing[0][1] > 0.75:
                best_text, best_score, best_id = existing[0]
                async with async_session() as db:
                    mem = await db.get(Memory, best_id)
                    if mem:
                        mem.confirmations = (mem.confirmations or 1) + 1
                        mem.last_confirmed = datetime.utcnow()
                        await db.commit()
                return ToolResult(success=True, data=_ok({
                    "action": "duplicate",
                    "reason": f"相似度{best_score:.0%}的已有记忆",
                    "existing_content": best_text,
                    "confirmations": mem.confirmations if mem else "?",
                }))

            # ── Create new memory ──
            mem_id = uuid.uuid4().hex
            now = datetime.utcnow()

            async with async_session() as db:
                memory = Memory(
                    id=mem_id, content=content, category=mem_type,
                    base_importance=70, confidence=70,
                    confirmations=1, last_confirmed=now,
                )
                db.add(memory)
                await db.commit()

            rag_service.add_memory(mem_id, content, "extracted", tier="long", ttl_days=0)
            logger.info(f"save_memory: '{content[:40]}...' → {mem_type}")

            return ToolResult(success=True, data=_ok({
                "action": "created",
                "id": mem_id,
                "content": content,
                "type": mem_type,
            }))

        except Exception as e:
            return ToolResult(success=False, error=str(e))


class ForgetMemoryTool(Tool):
    """Forget/delete a memory about the user."""

    @property
    def name(self) -> str:
        return "forget_memory"

    @property
    def description(self) -> str:
        return (
            "删除关于用户的某条长期记忆。"
            "当用户说'忘掉这个'、'这个不对'、'删除这个记忆'时使用。"
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
                        "description": "要删除的记忆关键词，用于搜索匹配",
                    },
                },
                "required": ["query"],
            },
        )

    async def run(self, query: str) -> ToolResult:
        """Search and delete matching memory. Returns structured JSON."""
        try:
            results = rag_service.search_with_scores(query, top_k=3)
            if not results or results[0][1] < 0.4:
                return ToolResult(success=True, data=_ok({
                    "action": "not_found",
                    "query": query,
                    "deleted": 0,
                }))

            deleted = []
            for text, score, chroma_id in results:
                if score > 0.5:
                    async with async_session() as db:
                        mem = await db.get(Memory, chroma_id)
                        if mem:
                            mem.is_deleted = True
                            await db.commit()
                    rag_service.remove_memory(chroma_id)
                    deleted.append({"content": text, "score": round(score, 2)})
                    logger.info(f"forget_memory: deleted '{text[:40]}...' (score={score:.0%})")

            return ToolResult(success=True, data=_ok({
                "action": "deleted" if deleted else "no_match",
                "query": query,
                "deleted": len(deleted),
                "items": deleted,
            }))

        except Exception as e:
            return ToolResult(success=False, error=str(e))
