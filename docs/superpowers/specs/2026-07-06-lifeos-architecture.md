# LifeOS 全系统架构文档

## 概述

LifeOS 是一个 AI 伴侣系统，由 Unity Android 客户端 + FastAPI 后端组成。支持语音对话、长期记忆、Agent 工具调用、Web 控制面板。

```
┌──────────────────────────────────────────────────────────────────────────┐
│                         LifeOS 系统全景                                    │
│                                                                          │
│  ┌──────────────────────────┐       HTTP/WS       ┌───────────────────┐  │
│  │     Unity Android        │ ◄─────────────────► │  FastAPI Backend  │  │
│  │                          │   192.168.x.x:8000   │                   │  │
│  │  🎤 Sherpa ASR (本地)    │                      │  🤖 LifeOSAgent  │  │
│  │  🔊 Sherpa TTS (本地)    │                      │  🛠 MemoryQuery   │  │
│  │  📡 ChatClient           │                      │  🧠 RAG (ChromaDB)│  │
│  │  🐛 MobileDebugHUD       │                      │  📦 MySQL         │  │
│  └──────────────────────────┘                      │  🖥 Dashboard     │  │
│                                                    └───────────────────┘  │
│                                                              │           │
│                                                    ┌─────────┴─────────┐ │
│                                                    │   External APIs    │ │
│                                                    │  DeepSeek  Ollama  │ │
│                                                    └───────────────────┘ │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## 一、Unity 客户端

### 1.1 目录结构

```
Assets/Scripts/
├── Core/
│   ├── AppCore.cs             DontDestroyOnLoad 单例，服务生命周期 + 场景切换
│   ├── IService.cs            启动时服务接口（ServiceName/LoadWeight/IsReady/Init/Dispose）
│   ├── BootSceneLoader.cs     注册 ASRService + TTSService，启动加载流程
│   └── ThreadedLoader.cs      后台线程 yield instruction，避免 ONNX 加载卡主线程
├── Scene/
│   └── CoreController.cs      3D 球体点击开关语音对话（Collider 点击切换）
├── Speech/
│   ├── SherpaASR.cs           Sherpa-ONNX OnlineRecognizer 封装（transducer 解码 + 端点检测）
│   ├── ASRService.cs          IService：拷贝 ONNX 模型 + 后台线程初始化 SherpaASR（权重 0.6）
│   ├── ASRController.cs       MonoBehaviour：麦克风采集 + 每 100ms 喂 SherpaASR + 轮询结果
│   ├── SherpaTTS.cs           MonoBehaviour：后台线程合成 + 主线程 AudioSource.PlayOneShot
│   ├── StreamingTTS.cs        流式 TTS：token 累积 → 句子断点分割 → 后台合成 → 队列顺序播放
│   └── TTSService.cs          IService：拷贝 VITS 模型 + 初始化 OfflineTts（权重 0.3）
├── AI/
│   ├── ConversationManager.cs 对话状态机（Idle/Listening/Thinking/Speaking）+ Barge-in 打断
│   ├── LLMClient.cs           直连 DeepSeek API 的 SSE 流式客户端（UnityWebRequest + StreamingDownloadHandler）
├── Network/
│   ├── ChatClient.cs          REST POST /chat/send + WebSocket /ws/chat/{id} 客户端
│   ├── WebSocketConnection.cs System.Net.WebSockets.ClientWebSocket 封装
│   └── UnityMainThreadDispatcher.cs 线程安全 Action 队列 → 主线程执行
└── UI/
    ├── MobileDebugHUD.cs       移动端 OnGUI 调试面板（FPS/内存/ASR/LLM/TTS/网络/日志，四指触摸切换）
    ├── ChatInputDebug.cs       OnGUI 文本输入框（Enter 打开/发送，Esc 关闭，N 新对话）
    ├── CoreDisplayText.cs      TMPro 全息滚动文字显示 + 静态事件总线（OnUserMessage/OnAIStreamToken/OnAIStreamDone）
    └── LoadingUI.cs            启动进度条 + 百分比 + 旋转环
```

### 1.2 启动流程

```
App.Start()
  │ 不在 Loading 场景 → SceneManager.LoadScene("Loading")
  ▼
BootSceneLoader.Start()
  │ 注册 ASRService(0.6) + TTSService(0.3)
  │ 启动 LoadAndGo() 协程
  ▼
AppCore.LoadAllServices()
  │ 顺序 yield IService.Init()：
  │
  ├─ ASRService.Init():
  │    Android: StreamingAssets → persistentDataPath 拷贝 4 个 ONNX 文件
  │    ThreadedLoader.Run(() => new SherpaASR().Init(modelPath))
  │    → 进度条 0% → 60%
  │
  └─ TTSService.Init():
       Android: 拷贝 VITS 模型文件
       ThreadedLoader.Run(() => new OfflineTts(config))
       → 进度条 60% → 100%

LoadingUI: 订阅 OnProgressChanged → 更新进度条 + 百分比 + 旋转环
AppCore.OnAllReady → BootSceneLoader.GoToMain() → SceneManager.LoadScene("Main")
```

### 1.3 语音对话数据流

```
用户说话
    │
    ▼
┌──────────────┐
│ ASRController │  Microphone.Start() 采集 16kHz
│ (MonoBehaviour)│  每 100ms 喂 SherpaASR.Feed(samples)
└──────┬───────┘
       │ OnResult(text)  /  OnPartialResult(text)
       ▼
┌──────────────────┐
│ ConversationManager│  状态机: Idle → Listening → Thinking → Speaking
│ (MonoBehaviour)   │  Barge-in: 说话时检测到新输入 → 打断 TTS + 取消 LLM
└──────┬───────────┘
       │ SendToLLM(text)
       ▼
┌──────────────┐
│  ChatClient  │  POST /chat/send → 获取 conv_id
│ (MonoBehaviour)│  WS /ws/chat/{id} → streaming tokens
└──────┬───────┘
       │ OnTokenReceived(token)  /  OnStreamComplete(full)
       ▼
┌──────────────┐
│ConversationManager│  token → StreamingTTS.Feed() 流式播放
│              │  done → SherpaTTS.Speak() 完整播放
└──────┬───────┘
       │
       ▼
   用户听到回复
```

### 1.3 ConversationManager 状态机

```
                    ┌──────────┐
        voice on →  │  Idle    │ ← 对话结束
                    └────┬─────┘
                         │ StartListening()
                         ▼
                    ┌──────────┐    用户说完
                    │Listening │ ──────────┐
                    └────┬─────┘           │
         partial speech    │ OnResult(text) │
         (barge-in check)  ▼                │
                    ┌──────────┐           │
         打断 ←──── │Speaking  │           │
                    └──────────┘           │
                              ▲            ▼
                              │      ┌──────────┐
                              │      │ Thinking │
                              │      └────┬─────┘
                              │           │
                              └───────────┘
                            LLM 回复到达
```

### 1.4 关键组件职责

| 组件 | 一句话 |
|------|--------|
| **AppCore** | 跨场景单例，管理 IService 加载顺序和场景切换 |
| **SherpaASR** | 音频帧 → 文字（transducer 解码 + 端点检测） |
| **ASRController** | 每 100ms 喂麦克风帧给 SherpaASR，轮询结果，发布 debug 指标 |
| **SherpaTTS** | 整句文字 → 后台合成 → 主线程播放 |
| **StreamingTTS** | token 到达即合成播放，按标点断句，队列顺序播 |
| **ConversationManager** | 听→想→说 状态机，支持语音打断 |
| **ChatClient** | REST 发消息 + WS 收流，挂载到 ConversationManager |
| **LLMClient** | 直连 DeepSeek API，SSE 流式，502/503/429 自动重试 3 次 |
| **WebSocketConnection** | ClientWebSocket 封装，后台接收 → 主线程分发 |
| **UnityMainThreadDispatcher** | 线程安全队列，后台线程通过它操作 Unity API |
| **CoreDisplayText** | TMPro 全息滚动文字，静态事件总线连接各模块 |
| **CoreController** | 3D 球体点击开关语音对话 |
| **MobileDebugHUD** | OnGUI 叠加层：FPS/内存/ASR mic/LLM/TTS/网络/日志，四指切换 |
| **ChatInputDebug** | 键盘文本输入备用（Enter 发送，N 新对话） |

### 1.5 完整端到端数据流

```
[Microphone]
    │ raw PCM, ~100ms chunks
    ▼
[ASRController.Update()]
    │ float[] → SherpaASR.Feed()
    ▼
[SherpaASR]  transducer greedy_search + endpoint 检测
    │
    ▼
[ASRController]  OnResult(final_text) 回调
    │
    ▼
[ConversationManager.OnASRResult]
    │ state = Thinking
    │ CoreDisplayText.OnUserMessage(final_text)
    ▼
[ConversationManager.SendToLLM]
    │
    ├─ ChatClient 模式:
    │    POST /chat/send → WebSocket /ws/chat/{id}
    │    ← JSON chunks: {"type":"token"} / {"type":"done"}
    │
    └─ LLMClient 模式:
         POST DeepSeek API → SSE: data: {...} delta
         重试: 502/503/429 → 最多 3 次
    │
    ▼
[ConversationManager.AppendToken(token)]
    │ state = Speaking
    ├─ CoreDisplayText.OnAIStreamToken(token)  → TMPro 逐字滚动
    └─ StreamingTTS.Feed(token)
         │ 累积到标点或 30 字
         ▼
       后台合成 → Queue<AudioClip> → PlayNext() 协程顺序播放
    │
    ▼
[ConversationManager.OnLLMDone]
    ├─ CoreDisplayText.OnAIStreamDone  → 滚动收尾 + 2s 后清屏
    ├─ StreamingTTS.Flush()           → 等待所有音频块播完
    ├─ SherpaTTS.Speak(fullText)     → 整句 TTS 兜底
    └─ Invoke(StartListening, duration+0.3s) → 循环继续

[Barge-in 打断]
    ASRController.OnPartialResult("...") → state==Speaking && len>=5
      → tts.Stop(), streamingTTS.Stop(), CancelLLM()
      → AI 停止输出，保持监听
```

---

## 二、FastAPI 后端

### 2.1 目录结构

```
backend/app/
├── main.py                   FastAPI 入口 + WebSocket + 所有路由
├── config.py                 pydantic Settings（环境变量）
│
├── agents/
│   ├── base.py               Agent ABC + Thought + Observation
│   ├── registry.py           AgentRegistry（注册/查找/列表）
│   ├── orchestrator.py       Orchestrator ABC（decompose → route → execute）
│   └── builtins/
│       ├── conversation.py   LifeOSAgent（核心 Agent）
│       └── auto_load.py      自动注册
│
├── tools/
│   ├── base.py               Tool ABC + ToolResult + ToolSchema
│   ├── registry.py           ToolRegistry（注册/查找/get_schemas）
│   └── builtins/
│       ├── memory_query.py   MemoryQueryTool
│       └── auto_load.py      自动注册
│
├── models/
│   ├── chat.py               Conversation, Message, Memory, ExtractionTask
│   ├── gaussian.py           GaussianTask
│   └── agent_task.py         AgentTask, TaskStep
│
├── services/
│   ├── llm.py                DeepSeek API（stream_chat + chat_complete + LLMResponse）
│   ├── ollama_llm.py         Ollama Qwen 客户端（预留，未启用）
│   ├── rag.py                ChromaDB 封装（3-tier 记忆 + Ollama bge-m3 嵌入）
│   ├── prompts.py            所有 LLM prompt 集中管理
│   ├── extraction.py         记忆提取（Judge → Extract → LLM Dedup）
│   ├── log_broadcaster.py    日志广播（捕获 Python logging → WebSocket）
│   ├── storage.py            MinIO 对象存储
│   └── gaussian.py           Celery 高斯训练任务
│
├── api/
│   ├── chat.py               REST 对话 CRUD + 标签
│   └── upload.py             照片上传 → 高斯训练
│
├── db/session.py             SQLAlchemy async engine + session
├── templates/
│   ├── dashboard.html        Web 控制面板
│   └── memory.html           记忆管理 Web UI
└── tasks/                    Celery 任务目录
```

### 2.2 核心数据流

```
Unity Client                         Backend
───────────                         ───────
     │                                  │
     │  POST /chat/send ──────────────► │ 创建 Message + Conversation
     │                                  │
     │  WS /ws/chat/{id} ─────────────► │ websocket_chat()
     │                                  │
     │                           ┌──────┴──────┐
     │                           │ ASR 纠错     │ DeepSeek (并行)
     │                           │ RAG 检索     │ ChromaDB
     │                           └──────┬──────┘
     │                                  │
     │                           ┌──────┴──────────┐
     │                           │ LifeOSAgent      │
     │                           │  .loop()         │
     │                           │                  │
     │                           │  think()         │
     │                           │  ├─ LLM + tools  │
     │                           │  ├─ respond? ────┤
     │                           │  └─ use_tool? ───┤
     │                           │       │          │
     │                           │       ▼          │
     │                           │  act()           │
     │                           │  └─ Tool.run()   │
     │                           │       │          │
     │                           │       ▼          │
     │                           │  tool result     │
     │                           │  └─ loop 继续    │
     │                           └──────┬──────────┘
     │                                  │
     │  ◀──── token / token ────────────┤  stream 流式输出
     │  ◀──── {"type":"done"} ──────────┤
     │                                  │
     │                           ┌──────┴──────────┐
     │                           │ 保存 Message    │
     │                           │ 记忆提取 (后台) │
     │                           │ raw ingest (后台)│
     │                           └─────────────────┘
```

### 2.3 API 端点

```
GET   /health                              健康检查
GET   /dashboard                           Web 控制面板
GET   /memory                              记忆管理 Web UI
GET   /docs                                Swagger 文档

POST  /chat/send                           发送消息（REST）
GET   /chat/conversations                  对话列表
GET   /chat/conversations/{id}/messages    消息历史
DELETE /chat/conversations/{id}            删除对话
POST  /chat/conversations/{id}/tag         加标签
WS    /ws/chat/{id}                        流式对话（含 Agent loop）

GET   /rag/stats                           ChromaDB + MySQL 记忆统计
GET   /rag/memories                        ChromaDB 原始记忆
GET   /rag/extracted                       MySQL 长期记忆
POST  /rag/clear                           清空 ChromaDB
WS    /ws/logs                             实时日志流

POST  /gaussian/upload                     上传照片训练 3DGS
GET   /gaussian/tasks/{id}                 查询训练状态
```

### 2.4 记忆系统

#### 存储架构

```
                    写入                              检索
                    ════                              ════

中期记忆 ────── ChromaDB tier=medium ──────── ChromaDB.search_by_tier()
              (原始用户消息, TTL 3-7d)

长期记忆 ──┬── ChromaDB tier=long ──────────── ChromaDB.search_by_tier()
           │  (LLM 提取的事实, TTL 永久)
           │
           └── MySQL Memory 表 ─────────────── /rag/extracted (Web UI)
              (结构化: category, importance, confidence)
```

#### 写入路径

| 路径 | 触发 | 流程 |
|------|------|------|
| **raw ingest** | 每次对话后 | `_quality_score()` 打分 → ≥0.4 进 ChromaDB medium |
| **LLM 提取** | 每 ≥2 条消息 | Judge → Extract → LLM Dedup → MySQL + ChromaDB long |

#### 检索路径

| 场景 | 来源 | 方法 |
|------|------|------|
| **RAG 预注入** | ChromaDB | `search_by_tier(query, top_k=8, min_score=0.25)` |
| **MemoryQueryTool** | ChromaDB | `search_with_scores(query, top_k=5)` |

#### 维护

| 任务 | 频率 | 逻辑 |
|------|------|------|
| **中期过期** | 每小时 | `cleanup_expired()` — 删 ChromaDB medium 过期条目 |
| **长期间衰减** | 每小时 | `_calc_confidence()` — confidence < 40 → is_faded=True → 从 ChromaDB 删除 |
| **启动同步** | 启动时 | `resync_long_memories()` — MySQL long → ChromaDB 回灌 |

### 2.5 Agent/Tool 系统

#### 架构

```
┌──────────────────────────────────────────────┐
│              LifeOSAgent                      │
│                                              │
│  think(messages, context) → Thought          │
│    │                                         │
│    ├─ chat_complete() + ToolRegistry.get_schemas()
│    │                                         │
│    ├─ tool_calls? → use_tool                 │
│    └─ content?    → respond                  │
│                                              │
│  loop(messages, context, max_steps=5)         │
│    │                                         │
│    ├─ respond → stream_chat() → tokens → WS  │
│    └─ use_tool → act() → result → continue   │
│                                              │
│  tools: ToolRegistry.list_all()              │
│  system_prompt: DEFAULT_CHAT (可被 RAG 覆盖) │
└──────────────────────────────────────────────┘
              │
              │ 调用
              ▼
┌──────────────────────────────────────────────┐
│              ToolRegistry                     │
│                                              │
│  register() / get() / list_all()             │
│  get_schemas() → OpenAI function calling     │
└──────────────────────────────────────────────┘
              │
              │ 已注册
              ▼
┌──────────────────────────────────────────────┐
│           MemoryQueryTool                     │
│                                              │
│  name: "memory_query"                        │
│  run(query, top_k=5) → ToolResult            │
│    └─ rag_service.search_with_scores()       │
└──────────────────────────────────────────────┘
```

#### 当前注册

| 类型 | 名称 | 说明 |
|------|------|------|
| Agent | `lifeos` | LifeOSAgent — 单一入口 |
| Tool | `memory_query` | ChromaDB 记忆搜索 |

### 2.6 LLM 调用策略

```
LifeOSAgent.think():
  调用: chat_complete() + tools schemas
  模式: 非流式
  用途: 判断 respond 还是 use_tool
  模型: DeepSeek API

LifeOSAgent.loop() (respond 分支):
  调用: stream_chat()  (不带 tools)
  模式: 流式
  用途: 生成最终回复 token 流
  模型: DeepSeek API

RAG ASR 纠错:
  调用: chat_complete()
  模式: 非流式
  用途: 纠正语音识别错误

记忆提取 Judge:
  调用: chat_complete()
  模式: 非流式
  用途: 判断对话是否值得提取记忆

记忆提取 Extract:
  调用: chat_complete()
  模式: 非流式
  用途: 从对话提取关键信息

Dedup Judge:
  调用: chat_complete() + DEDUP_JUDGE prompt
  模式: 非流式
  用途: LLM 判断新旧记忆关系 → skip/update/keep_both/correct/create
```

### 2.7 关键配置 (.env)

```bash
# LLM
DEEPSEEK_API_KEY=sk-xxx
DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat

# Database
DATABASE_URL=mysql+aiomysql://lifeos:lifeos@localhost:3306/lifeos

# Ollama (本地 embedding + 预留 Qwen)
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_EMBED_MODEL=bge-m3
```

---

## 三、部署架构

```
┌─────────────────────────────────────────────────────────┐
│                  Windows PC (开发机)                     │
│                                                         │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐              │
│  │ MySQL 8  │  │ Ollama   │  │ Redis    │              │
│  │ :3306    │  │ :11434   │  │ :6379    │              │
│  └──────────┘  └──────────┘  └──────────┘              │
│       │              │              │                    │
│       └──────────────┼──────────────┘                    │
│                      │                                   │
│              ┌───────┴───────┐                          │
│              │  FastAPI      │                          │
│              │  uvicorn      │                          │
│              │  0.0.0.0:8000 │                          │
│              └───────┬───────┘                          │
│                      │                                   │
│  ┌───────────────────┼───────────────────┐              │
│  │                   │                   │              │
│  ▼                   ▼                   ▼              │
│ Dashboard       Unity Editor        Android 手机        │
│ :8000/dashboard :8000/ws/chat      :8000/ws/chat       │
│ (浏览器)        (localhost)        (192.168.x.x)       │
└─────────────────────────────────────────────────────────┘
```

---

## 四、启动方式

### 后端

```bash
# 方式1: 命令行
conda activate sharp
cd backend
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# 方式2: 双击批处理
backend/start_lifeos.bat

# 方式3: 浏览器打开控制面板
http://localhost:8000/dashboard
```

### Unity

```
1. 打开 LifeOS 项目
2. 确认 ChatClient.ServerUrl = http://<PC_IP>:8000
3. Play 运行
4. 手机端: Build & Run (Android)
5. Debug 面板: 四指触摸切换显示
```

---

## 五、当前实现状态

### ✅ 已完成

| 模块 | 说明 |
|------|------|
| 语音识别 | Sherpa-ONNX 流式 ASR + 端点检测 |
| 语音合成 | Sherpa-ONNX TTS + 流式播放 |
| 对话状态机 | 听→想→说 + Barge-in 打断 |
| 网络通信 | REST + WebSocket 流式 |
| LifeOSAgent | think → act → observe 循环 + 工具调用 |
| MemoryQueryTool | ChromaDB 记忆搜索 |
| 三层记忆 | 短期/中期(ChromaDB)/长期(MySQL+ChromaDB) |
| LLM 提取 | Judge → Extract → LLM Dedup |
| Memory Dedup | ChromaDB 预筛 + LLM 判断 (skip/update/keep_both/correct/create) |
| RAG 检索 | ChromaDB 分层检索 + category boost |
| ASR 纠错 | DeepSeek 后处理 |
| Token 追踪 | 每次 LLM 调用的 token 消耗 |
| Web Dashboard | 实时日志 + Pipeline 可视化 + Agent 状态 + Memory 统计 |
| 启动脚本 | start_lifeos.bat 一键启动 |
| Bug 修复 | CJK 字符长度修复、preference dedup、clear ChromaDB |

### ⬜ 待实现

| 模块 | 说明 |
|------|------|
| 更多 Tool | WebSearchTool, ImageTool 等 |
| 更多 Agent | 子 Agent 供 LifeOSAgent 委托 |
| Orchestrator | 多 Agent 协作编排 |
| Qwen Router | 简单问题本地 Qwen 处理（接口已就绪，模型需手动 pull） |
| 高斯训练 | 3DGS pipeline 联调 |
| 云端部署 | Docker + 生产环境 |
| Unity 录音优化 | 噪声抑制、VAD |