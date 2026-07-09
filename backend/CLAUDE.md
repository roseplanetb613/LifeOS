# LifeOS Backend — AI Handoff Guide

## 一句话概述

FastAPI 后端，Unity Android 客户端的 AI 对话 + 长期记忆 + 工具调用平台。

## 快速启动

```bash
conda activate sharp
cd backend
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

前置：MySQL 8.0 + Redis + Ollama (bge-m3)。数据库 `lifeos`，用户 `lifeos:lifeos`。

## 目录结构 & 模块职责

```
app/
├── main.py                   FastAPI 入口 + WebSocket + 全部端点
├── config.py                 pydantic 配置 (env → Settings)
├── api/
│   ├── chat.py               REST: 对话 CRUD + 标签
│   └── upload.py             REST: 照片上传 → 高斯训练
├── models/
│   ├── chat.py               Conversation, Message, Memory, ExtractionTask
│   ├── gaussian.py           GaussianTask
│   └── agent_task.py         AgentTask, TaskStep (多agent系统)
├── services/
│   ├── llm.py                DeepSeek API: stream_chat + chat_complete (含 TokenUsage)
│   ├── rag.py                ChromaDB 封装: 3-tier 记忆 + Ollama bge-m3
│   ├── prompts.py            所有 LLM prompt 集中管理
│   ├── agent_trace.py        Agent 执行轨迹记录
│   ├── storage.py            MinIO 对象存储
│   └── gaussian.py           Celery 高斯训练任务
├── agents/                   【多Agent框架 — 接口已就绪】
│   ├── base.py               Agent 抽象类 (think → act → observe loop)
│   ├── registry.py           AgentRegistry
│   ├── orchestrator.py       Orchestrator 抽象 (decompose → route → execute)
│   └── builtins/             (空，待实现具体Agent)
├── tools/                    【工具框架 — 接口已就绪】
│   ├── base.py               Tool 抽象类 (name + schema + run)
│   ├── registry.py           ToolRegistry (含 get_schemas() 给 LLM function calling)
│   └── builtins/             (空，待实现具体Tool)
├── tasks/                    (空，Celery 任务目录)
├── db/session.py             SQLAlchemy async engine
└── templates/
    └── memory.html           记忆管理 Web UI (localhost:8000/memory)
```

## 核心数据流

```
Unity 语音 → ASR → POST /chat/send → WS /ws/chat/{id} →
  ├── ASR纠错 + RAG检索 (并行 async)
  ├── LLM 流式回复 (DeepSeek)
  ├── 提取触发 (msg≥2 → 后台)
  │     ├── Judge: 值得记？
  │     ├── Extract: LLM 提取 JSON
  │     └── Dedup: ChromaDB 余弦去重
  └── 写入 MySQL + ChromaDB (tier=medium|long)
```

## 记忆系统三层

| 层 | 存储 | TTL | 写入 | 检索 |
|----|------|-----|------|------|
| 短期 | ChatClient._history | 当前会话 | 实时 | 直接拼入 LLM prompt |
| 中期 | ChromaDB tier=medium | 3-7天 | 对话结束批处理 | 语义检索 |
| 长期 | MySQL+ChromaDB tier=long | 永久(衰减) | LLM 提取 | 语义检索+类别分层 |

## 关键 API 端点

```
GET  /health
GET  /memory                    记忆 Web UI
GET  /docs                      Swagger
POST /chat/send                发送消息
GET  /chat/conversations        对话列表
GET  /chat/conversations/{id}/messages  消息历史
DELETE /chat/conversations/{id}         删除对话
POST /chat/conversations/{id}/tag      加标签
WS   /ws/chat/{id}              流式对话
POST /gaussian/upload           上传照片训练3DGS
GET  /gaussian/tasks/{id}       查询训练状态
GET  /rag/memories              ChromaDB 原始记忆
GET  /rag/extracted             LLM 提取的长期记忆
POST /rag/clear                 清空 ChromaDB
```

## 待实现的接口 (下个 AI 从这里开始)

### 1. 第一个 Tool: MemoryQueryTool

```python
# tools/builtins/memory_query.py
from app.tools.base import Tool, ToolResult, ToolSchema
from app.tools.registry import ToolRegistry

class MemoryQueryTool(Tool):
    name = "memory_query"
    description = "搜索用户的长期记忆和过往对话"
    schema = ToolSchema(
        name="memory_query",
        description="搜索用户的长期记忆",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词"},
                "top_k": {"type": "integer", "default": 5}
            },
            "required": ["query"]
        }
    )
    
    async def run(self, query: str, top_k: int = 5) -> ToolResult:
        from app.services.rag import rag_service
        results = rag_service.search(query, top_k=top_k)
        return ToolResult(success=True, data=results)

# 注册
ToolRegistry.register(MemoryQueryTool())
```

### 2. 第一个 Agent: ConversationAgent

```python
# agents/builtins/conversation.py
from app.agents.base import Agent, Thought

class ConversationAgent(Agent):
    name = "conversation"
    system_prompt = "你是一个友好助手的默认设定..."
    
    async def think(self, messages, context=None) -> Thought:
        # 判断: 直接回复 or 调用工具
        ...
```

### 3. Orchestrator 对接现有 WebSocket

```python
# main.py websocket_chat 里，替换 stream_chat 调用:
# 现在: async for token in stream_chat(messages=messages)
# 以后: agent = get_conversation_agent()
#       async for token in agent.loop(messages, context):
```

## 配置项 (.env)

```
DEEPSEEK_API_KEY=sk-xxx
DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat
DATABASE_URL=mysql+aiomysql://lifeos:lifeos@localhost:3306/lifeos
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_EMBED_MODEL=bge-m3
```

## 架构决策：不使用 LangChain

**结论：自研轻量 Agent/Tool 框架，不引入 LangChain。**

原因：
- 项目已有 LangChain 核心能力的等价实现（Agent loop、Tool 接口、Memory、Prompt 管理、Function Calling、Streaming）
- 当前场景是单 Agent + 最多 5 步循环，不需要 LangChain 的复杂编排
- 自研框架 ~100 行抽象，零额外依赖，调试透明
- LangChain 版本迭代快、break change 频繁、堆栈深难追踪
- 未来如需复杂编排，可针对性引入，不提前背重框架

## 设计文档

详细设计见：`docs/superpowers/specs/2026-07-05-agent-tool-design.md`

## 当前状态 (截至 2025-07-05)

### 已实现
- ✅ 对话 + 语音全链路
- ✅ 三层记忆 (短期/中期/长期)
- ✅ 记忆提取 + 去重 + 衰减
- ✅ ASR 纠错 + 打断
- ✅ 流式 TTS
- ✅ Token 消耗追踪
- ✅ Agent/Tool 接口体系 (抽象已就绪)

### 本次开发 (按依赖顺序)
1. ⬜ `tools/builtins/memory_query.py` — MemoryQueryTool
2. ⬜ `agents/builtins/conversation.py` — ConversationAgent
3. ⬜ `services/llm.py` — 扩展 chat_complete 支持 tools + LLMResponse
4. ⬜ `main.py` — WebSocket 接入 Agent loop

### 后续
- ⬜ Orchestrator 完整实现 (多Agent协作)
- ⬜ 更多 Tool (web_search, image_gen...)
- ⬜ 高斯训练 pipeline 联调
- ⬜ 云部署
