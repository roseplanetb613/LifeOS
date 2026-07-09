# LifeOS Agent/Tool 系统设计文档

## 概述

LifeOS 后端是一个 FastAPI 应用，为 Unity Android 客户端提供 AI 对话 + 长期记忆 + 工具调用平台。本文档描述 Agent/Tool 多智能体框架的设计与实现。

## 架构总览

```
┌─────────────────────────────────────────────────────────┐
│                    Unity Android Client                  │
│              (语音输入 → ASR → HTTP/WS)                  │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│                   FastAPI Backend                        │
│                                                         │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  │
│  │  REST /chat  │  │  WS /ws/chat │  │  /memory UI  │  │
│  └──────┬───────┘  └──────┬───────┘  └──────────────┘  │
│         │                 │                              │
│         ▼                 ▼                              │
│  ┌─────────────────────────────────────────────────┐    │
│  │              Orchestrator (编排器)                │    │
│  │         decompose → route → execute              │    │
│  └──────┬──────────────┬──────────────┬────────────┘    │
│         │              │              │                  │
│         ▼              ▼              ▼                  │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐              │
│  │ Agent A  │  │ Agent B  │  │ Agent C  │  ← Agent     │
│  │ think()  │  │ think()  │  │ think()  │    循环       │
│  │ act()    │  │ act()    │  │ act()    │              │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘              │
│       │              │              │                    │
│       └──────────────┼──────────────┘                    │
│                      ▼                                   │
│  ┌─────────────────────────────────────────────────┐    │
│  │              Tool Registry (工具注册表)           │    │
│  │  memory_query │ web_search │ image_gen │ ...     │    │
│  └──────────────────────┬──────────────────────────┘    │
│                         │                                │
│                         ▼                                │
│  ┌─────────────────────────────────────────────────┐    │
│  │                 外部服务                          │    │
│  │  DeepSeek LLM │ ChromaDB │ MySQL │ Redis │ Ollama │   │
│  └─────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────┘
```

## 核心概念

### Agent（智能体）

Agent 是自主决策的 AI 实体，遵循 **Think → Act → Observe** 循环：

```
┌──────────┐     ┌──────────┐     ┌──────────┐
│  Think   │ ──▶ │   Act    │ ──▶ │ Observe  │
│ 推理决策  │     │ 执行动作  │     │ 观察结果  │
└──────────┘     └──────────┘     └──────────┘
      ▲                                  │
      └──────────────────────────────────┘
                 循环 (最多5步)
```

**状态机**:

```
         ┌─────────┐
         │  start  │
         └────┬────┘
              │
              ▼
         ┌─────────┐
    ┌───▶│  think  │
    │    └────┬────┘
    │         │
    │    ┌────┴────┐
    │    │ action?  │
    │    └────┬────┘
    │     resp│     │use_tool
    │         │     │
    │    ┌────▼┐ ┌──▼────┐
    │    │respond│ │act   │
    │    └────┬─┘ └──┬───┘
    │         │       │
    │         │  ┌────▼────┐
    │         │  │observe  │
    │         │  └────┬────┘
    │         │       │
    │         │       ▼ (continue loop)
    │         │  ┌─────────┐
    │         │  │ append  │
    │         │  │ tool    │──┐
    │         │  │ result  │  │
    │         │  └─────────┘  │
    │         │               │
    │    ┌────▼────┐          │
    │    │ stream  │          │
    │    │ tokens  │          │
    │    └────┬────┘          │
    │         │               │
    │         ▼               │
    │    ┌─────────┐          │
    └────│  done   │◀─────────┘
         └─────────┘
```

### Tool（工具）

Tool 是 Agent 可调用的外部能力。每个 Tool 暴露 OpenAI Function Calling 兼容的 JSON Schema，LLM 根据 schema 决定何时调用、传什么参数。

### Orchestrator（编排器）

Orchestrator 负责将复杂用户请求分解为子任务，并路由到合适的 Agent 执行。

## 目录结构

```
backend/app/
├── agents/
│   ├── base.py              Agent 抽象类 (think → act → observe)
│   ├── registry.py          AgentRegistry 注册表
│   ├── orchestrator.py      Orchestrator 抽象 (decompose → route → execute)
│   └── builtins/            具体 Agent 实现
│       ├── conversation.py  ConversationAgent (默认对话Agent)
│       └── auto_load.py     自动注册入口
├── tools/
│   ├── base.py              Tool 抽象类 (name + schema + run)
│   ├── registry.py          ToolRegistry 注册表 (含 get_schemas() 给 LLM)
│   └── builtins/            具体 Tool 实现
│       ├── memory_query.py  MemoryQueryTool (搜索长期记忆)
│       └── auto_load.py     自动注册入口
├── models/
│   └── agent_task.py        AgentTask, TaskStep (多Agent执行记录)
├── services/
│   ├── llm.py               DeepSeek API (stream_chat + chat_complete)
│   ├── rag.py               ChromaDB 封装 (3-tier 记忆)
│   └── prompts.py           所有 LLM prompt 集中管理
└── main.py                  FastAPI 入口 + WebSocket
```

## 接口设计

### Agent 抽象类

```python
class Agent(ABC):
    name: str              # 唯一标识符
    system_prompt: str     # 系统提示词 (定义角色和行为)
    tools: list[Tool]      # 可用工具列表

    async def think(messages, context) -> Thought  # 推理决策
    async def act(tool_name, **params) -> Observation  # 执行工具
    async def loop(messages, context, max_steps=5) -> AsyncGenerator[str]  # 主循环
```

### Thought（思考结果）

| 字段 | 类型 | 说明 |
|------|------|------|
| reasoning | str | 推理过程 |
| action | str \| None | "use_tool" \| "respond" \| "delegate" |
| tool_name | str \| None | 要调用的工具名 |
| tool_params | dict \| None | 工具参数 |
| response | str \| None | 直接回复文本 |

### Tool 抽象类

```python
class Tool(ABC):
    name: str              # 唯一标识符，如 "memory_query"
    description: str       # 描述，LLM 据此决定何时调用
    schema: ToolSchema     # OpenAI Function Calling JSON Schema

    async def run(**params) -> ToolResult  # 执行工具
```

### ToolSchema（工具定义）

```python
@dataclass
class ToolSchema:
    name: str              # 工具名
    description: str       # 功能描述
    parameters: dict       # JSON Schema 参数定义
```

## 数据流：完整对话链路

```
Unity Client                          Backend Server
───────────                          ──────────────
     │                                      │
     │  1. 语音输入                           │
     │  POST /chat/send ──────────────────▶  │
     │                                      │
     │  2. WS /ws/chat/{id} ──────────────▶  │
     │                                      │
     │                               ┌──────┴──────┐
     │                               │ ASR 纠错     │ (并行)
     │                               │ RAG 记忆检索  │ (并行)
     │                               └──────┬──────┘
     │                                      │
     │                               ┌──────┴──────────┐
     │                               │ RAG_INJECT      │
     │                               │ 注入 system     │
     │                               │ prompt          │
     │                               └──────┬──────────┘
     │                                      │
     │                               ┌──────┴──────────┐
     │                               │ Agent.loop()    │
     │                               │                 │
     │                               │  think()        │
     │                               │  ├─ LLM + tools │
     │                               │  ├─ respond? ───┤
     │                               │  └─ use_tool? ──┤
     │                               │       │         │
     │                               │       ▼         │
     │                               │  act()          │
     │                               │  └─ Tool.run()  │
     │                               │       │         │
     │                               │       ▼         │
     │                               │  observe        │
     │                               │  └─ loop继续    │
     │                               └──────┬──────────┘
     │                                      │
     │  ◀──── token / token / token ────    │  (流式输出)
     │  ◀──── {"type":"done"} ──────────    │
     │                                      │
     │                               ┌──────┴──────────┐
     │                               │ 保存 Message    │ (后台)
     │                               │ 记忆提取        │ (后台)
     │                               │ raw ingest     │ (后台)
     │                               └─────────────────┘
```

## ConversationAgent 设计

### 两阶段 LLM 调用策略

| 阶段 | 方法 | 是否流式 | 携带 Tools | 用途 |
|------|------|----------|------------|------|
| Think | `chat_complete()` | 否 | 是 | 决策：respond 还是 use_tool |
| Respond | `stream_chat()` | 是 | 否 | 生成最终回复 token 流 |

**为什么不合并为一次调用？** OpenAI 流式 tool_call 的 delta 增量拼接逻辑复杂。v1 采用两阶段：先用非流式快速判断意图（低延迟），确定 respond 后再用流式生成。代价是多一次 API 调用，但实现简单可靠。

### Think 阶段逻辑

```
输入: messages[] + tool_schemas[]
     │
     ▼
┌─────────────┐
│ LLM 推理     │
└──────┬──────┘
       │
       ▼
  ┌──────────┐
  │ 有 tool_ │──Yes──▶ Thought(action="use_tool")
  │ calls?   │         tool_name, tool_params
  └────┬─────┘
       │No
       ▼
  Thought(action="respond")
  (response 内容丢弃，loop阶段重新流式生成)
```

### Loop 阶段逻辑

```
step = 0
   │
   ▼
┌──────────┐     ┌──────────────┐
│ think()  │────▶│ respond?     │──▶ stream_chat() → token流 → 结束
└────┬─────┘     └──────────────┘
     │
     │ use_tool
     ▼
┌──────────┐     ┌──────────────┐
│ act()    │────▶│ tool结果     │──▶ 追加到messages
└────┬─────┘     │ 追加到msg    │     step++ (< 5)
     │           └──────────────┘        │
     └───────────────────────────────────┘
```

## MemoryQueryTool 设计

第一个具体 Tool，让 Agent 能主动搜索用户的长期记忆。

```
Tool: memory_query
├── 参数:
│   ├── query: str   (搜索关键词, 必填)
│   └── top_k: int   (返回数量, 默认5)
│
├── 执行:
│   └── rag_service.search_with_scores(query, top_k)
│       └── ChromaDB 余弦相似度搜索
│
└── 返回:
    └── ToolResult(success=True, data="[85%] 用户喜欢喝咖啡\n[72%] 用户住在北京...")
```

## 记忆系统三层

| 层 | 存储 | TTL | 写入时机 | 检索方式 |
|----|------|-----|----------|----------|
| 短期 | ChatClient._history | 当前会话 | 实时 | 直接拼入 LLM prompt |
| 中期 | ChromaDB tier=medium | 3-7天 | 对话结束批处理 | 语义检索 (RAG预注入) |
| 长期 | MySQL + ChromaDB tier=long | 永久(衰减) | LLM 提取 | 语义检索 (RAG预注入 + MemoryQueryTool) |

## LLM 服务扩展

为支持 Function Calling，`chat_complete()` 和 `stream_chat()` 扩展：

### 新增 LLMResponse 类型

```python
@dataclass
class LLMResponse:
    content: str = ""
    tool_calls: list[dict] | None = None  # [{"name": "...", "arguments": {...}}]
    usage: TokenUsage = field(default_factory=TokenUsage)
```

### 新增参数

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| tools | list[dict] \| None | None | OpenAI 格式的 tools 定义 |
| tool_choice | str | "auto" | "auto" \| "none" \| "required" |

## 自动注册机制

利用 Python 模块导入副作用实现自动注册：

```python
# app/tools/builtins/auto_load.py
from app.tools.registry import ToolRegistry
from app.tools.builtins.memory_query import MemoryQueryTool
ToolRegistry.register(MemoryQueryTool())

# app/agents/builtins/auto_load.py
from app.agents.registry import AgentRegistry
from app.agents.builtins.conversation import ConversationAgent
AgentRegistry.register(ConversationAgent())

# app/main.py (启动时触发)
import app.agents.builtins  # 副作用: AgentRegistry 注册
import app.tools.builtins   # 副作用: ToolRegistry 注册
```

## API 端点一览

```
GET   /health                         健康检查
GET   /memory                         记忆管理 Web UI
GET   /docs                           Swagger 文档
POST  /chat/send                      发送消息 (REST)
GET   /chat/conversations             对话列表
GET   /chat/conversations/{id}/messages  消息历史
DELETE /chat/conversations/{id}         删除对话
POST  /chat/conversations/{id}/tag      加标签
WS    /ws/chat/{id}                    流式对话 (含 Agent loop)
POST  /gaussian/upload                 上传照片训练3DGS
GET   /gaussian/tasks/{id}             查询训练状态
GET   /rag/memories                    ChromaDB 原始记忆
GET   /rag/extracted                   LLM 提取的长期记忆
POST  /rag/clear                       清空 ChromaDB
```

## 当前实现状态

| 模块 | 状态 |
|------|------|
| Agent 抽象类 (base.py) | ✅ 完成 |
| Tool 抽象类 (base.py) | ✅ 完成 |
| Orchestrator 抽象 (orchestrator.py) | ✅ 完成 |
| AgentRegistry / ToolRegistry | ✅ 完成 |
| LLM 服务 (DeepSeek) | ✅ 完成 |
| RAG 记忆系统 (ChromaDB + Ollama) | ✅ 完成 |
| 记忆提取 (3阶段) | ✅ 完成 |
| ASR 纠错 + 打断 | ✅ 完成 |
| 流式 TTS | ✅ 完成 |
| Token 消耗追踪 | ✅ 完成 |
| **MemoryQueryTool** | ⬜ 待实现 |
| **ConversationAgent** | ⬜ 待实现 |
| **WebSocket Agent 对接** | ⬜ 待实现 |
| 更多 Tool (web_search, image_gen...) | ⬜ 后续 |
| 更多 Agent (task_decomposition, memory_maintenance...) | ⬜ 后续 |
| Orchestrator 完整实现 | ⬜ 后续 |
| 高斯训练 pipeline 联调 | ⬜ 后续 |
| 云部署 | ⬜ 后续 |

## 配置 (.env)

```
DEEPSEEK_API_KEY=sk-xxx
DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat
LLM_MAX_TOKENS=4096
DATABASE_URL=mysql+aiomysql://lifeos:lifeos@localhost:3306/lifeos
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_EMBED_MODEL=bge-m3
```

## 验证清单

- [ ] MemoryQueryTool 独立调用测试
- [ ] ConversationAgent.think() 决策正确性
- [ ] Agent loop 工具调用后正确继续
- [ ] WebSocket 流式输出正常
- [ ] 多步工具调用不超 max_steps
- [ ] 客户端断开时 Agent loop 正确取消
- [ ] 普通对话（无工具调用）行为不变
- [ ] 记忆提取后台任务不受影响
- [ ] Token 消耗正确记录
