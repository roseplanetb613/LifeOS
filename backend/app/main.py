"""LifeOS Backend — FastAPI entry point."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
import json
import uuid
from datetime import datetime
from sqlalchemy import func

from app.config import get_settings
from app.api.chat import router as chat_router
from app.api.upload import router as upload_router
from app.db.session import async_session
from app.models.chat import Message, Conversation
from app.services.llm import stream_chat, chat_complete, TokenUsage
from app.services.rag import rag_service
from app.services.prompts import ASR_CORRECT_SYSTEM, RAG_INJECT
from app.services.log_broadcaster import log_broadcaster
import app.agents.builtins   # noqa: F401 — trigger agent + tool registration
import app.tools.builtins    # noqa: F401
from app.agents.registry import AgentRegistry

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle."""
    from app.db.session import engine, Base
    from app.models.chat import Memory
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    import logging
    _cleanup_logger = logging.getLogger("uvicorn")

    # ── Register log broadcaster to capture all logs for Dashboard ──
    root_logger = logging.getLogger()
    log_broadcaster.setLevel(logging.INFO)
    root_logger.addHandler(log_broadcaster)
    _cleanup_logger.info("Log broadcaster registered")

    # ── Sync: ensure all MySQL long-term memories exist in ChromaDB ──
    async def resync_long_memories():
        """Re-add any MySQL long-term memories that are missing from ChromaDB."""
        async with async_session() as db:
            from sqlalchemy import select as sa_select
            result = await db.execute(
                sa_select(Memory)
                .where(Memory.is_deleted == False)
                .where(Memory.is_faded == False)
            )
            all_mems = result.scalars().all()
            synced = 0
            for m in all_mems:
                # Check if already in ChromaDB by trying to search for its content
                existing = rag_service.search_with_scores(m.content, top_k=1)
                if not existing or existing[0][2] != m.id:
                    rag_service.add_memory(m.id, m.content, "extracted",
                                           tier="long", ttl_days=0)
                    synced += 1
            if synced > 0:
                _cleanup_logger.info(f"Re-synced {synced} long-term memories to ChromaDB")

    await resync_long_memories()

    async def memory_maintenance():
        while True:
            await asyncio.sleep(3600)
            try:
                # 1. Remove expired medium-term memories from ChromaDB
                removed = rag_service.cleanup_expired()
                if removed > 0:
                    _cleanup_logger.info(f"Cleanup: {removed} expired medium memories")

                # 2. Recalculate confidence for long-term memories & mark faded
                async with async_session() as db:
                    from sqlalchemy import select as sa_select, update as sa_update
                    result = await db.execute(
                        sa_select(Memory)
                        .where(Memory.category.isnot(None))
                        .where(Memory.is_corrected == False)
                        .where(Memory.is_deleted == False)
                    )
                    all_mems = result.scalars().all()

                    now = datetime.utcnow()
                    faded = 0
                    for m in all_mems:
                        new_conf = _calc_confidence(
                            m.base_importance,
                            m.confirmations,
                            m.last_confirmed or m.created_at,
                            now,
                        )
                        m.confidence = new_conf
                        if new_conf < 40 and not m.is_faded:
                            m.is_faded = True
                            rag_service.remove_memory(m.id)  # remove from ChromaDB
                            faded += 1
                        elif new_conf >= 40 and m.is_faded:
                            m.is_faded = False
                            # Re-add to ChromaDB if revived
                            rag_service.add_memory(m.id, m.content, "extracted",
                                                   tier="long", ttl_days=0)

                    await db.commit()
                    if faded > 0:
                        _cleanup_logger.info(f"Fade: {faded} memories faded out")

            except asyncio.CancelledError:
                break
            except Exception as e:
                _cleanup_logger.error(f"Memory maintenance: {e}")
    cleanup_task = asyncio.create_task(memory_maintenance())

    yield
    # Shutdown — cancel background task and wait for it to finish
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass
    # Cleanup log broadcaster
    root_logger.removeHandler(log_broadcaster)
    try:
        await engine.dispose()
    except Exception as e:
        _cleanup_logger.error(f"Engine dispose: {e}")


app = FastAPI(
    title="LifeOS Backend",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS — allow Unity client
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# REST routes
app.include_router(chat_router)
app.include_router(upload_router)


# ── Memory Viewer ──
@app.get("/memory", response_class=HTMLResponse)
async def memory_page():
    html = Path(__file__).parent / "templates" / "memory.html"
    return html.read_text(encoding="utf-8")


# ── RAG Stats ──
@app.get("/rag/stats")
async def rag_stats():
    """Return ChromaDB + MySQL memory counts."""
    chroma_medium = 0
    chroma_long = 0
    try:
        data = rag_service.collection.get(include=["metadatas"])
        for meta in data.get("metadatas", []):
            if meta and meta.get("tier") == "long":
                chroma_long += 1
            else:
                chroma_medium += 1
    except Exception:
        pass

    mysql_count = 0
    try:
        from app.models.chat import Memory
        from sqlalchemy import select as sa_select, func as sa_func
        async with async_session() as db:
            result = await db.execute(
                sa_select(sa_func.count(Memory.id))
                .where(Memory.is_deleted == False)
                .where(Memory.is_faded == False)
            )
            mysql_count = result.scalar() or 0
    except Exception:
        pass

    return {
        "chromadb": {"medium": chroma_medium, "long": chroma_long, "total": chroma_medium + chroma_long},
        "mysql_memories": mysql_count,
    }


# ── RAG Memory Viewer ──
@app.get("/rag/memories")
async def rag_memories(limit: int = 50):
    """View all stored RAG memories from ChromaDB."""
    try:
        results = rag_service.collection.get(
            limit=limit,
            include=["documents", "metadatas"]
        )
        return [
            {"id": rid, "content": doc, "role": meta.get("role", "?") if meta else "?"}
            for rid, doc, meta in zip(
                results.get("ids", []),
                results.get("documents", []),
                results.get("metadatas", [])
            )
        ]
    except Exception as e:
        return {"error": str(e)}


# ── Extracted Memories (MySQL) ──
@app.get("/rag/extracted")
async def rag_extracted(limit: int = 50):
    """View LLM-extracted memories from MySQL."""
    from app.models.chat import Memory
    from sqlalchemy import select as sa_select
    async with async_session() as db:
        result = await db.execute(
            sa_select(Memory)
            .where(Memory.is_deleted == False)
            .where(Memory.is_faded == False)
            .order_by(Memory.confidence.desc(), Memory.created_at.desc())
            .limit(limit)
        )
        memories = result.scalars().all()
        return [
            {
                "id": m.id,
                "content": m.content,
                "category": m.category,
                "importance": m.base_importance,
            "confidence": m.confidence,
            "confirmations": m.confirmations,
                "source_conv_id": m.source_conv_id,
                "created_at": m.created_at.isoformat(),
            }
            for m in memories
        ]


# ── Clear all memories ──
@app.post("/rag/clear")
async def clear_rag():
    ok = rag_service.clear_all()
    return {"cleared": ok}


# ── Agent Trace API ──
@app.get("/agent/traces/latest")
async def agent_traces_latest(limit: int = 10):
    """Get the most recent agent execution traces."""
    from app.services.agent_trace import get_latest_trace
    return await get_latest_trace(limit)


@app.get("/agent/traces/detail/{task_id}")
async def agent_trace_detail(task_id: str):
    """Get full step-by-step detail for a single agent execution."""
    from app.services.agent_trace import get_trace_detail
    return await get_trace_detail(task_id)


@app.get("/agent/traces/{conversation_id}")
async def agent_traces_for_conversation(conversation_id: str, limit: int = 5):
    """Get agent execution traces for a specific conversation."""
    from app.services.agent_trace import get_trace
    return await get_trace(conversation_id, limit)


# ── Dashboard ──
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page():
    html = Path(__file__).parent / "templates" / "dashboard.html"
    return html.read_text(encoding="utf-8")


# ── Live Log WebSocket ──
@app.websocket("/ws/logs")
async def websocket_logs(websocket: WebSocket):
    await websocket.accept()
    q = log_broadcaster.subscribe()
    try:
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=30)
                await websocket.send_text(msg)
            except asyncio.TimeoutError:
                await websocket.send_text(json.dumps({
                    "time": datetime.utcnow().strftime("%H:%M:%S"),
                    "level": "DEBUG",
                    "name": "ping",
                    "msg": "(keepalive)",
                }))
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        log_broadcaster.unsubscribe(q)


# ── Health Check ──
@app.get("/health")
async def health():
    return {"status": "ok", "version": "0.1.0"}


# ── WebSocket for streaming LLM responses ──
@app.websocket("/ws/chat/{conversation_id}")
async def websocket_chat(websocket: WebSocket, conversation_id: str):
    await websocket.accept()
    import logging
    logger = logging.getLogger("uvicorn")
    try:
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)
            messages = msg.get("messages", [])
            logger.info(f"WS received {len(messages)} messages")

            # ── Parallel: ASR Correction + RAG Search ──
            rag_prompt = None  # ensure defined even if user_msg is missing
            user_msg = next((m for m in reversed(messages) if m.get("role") == "user"), None)
            if user_msg:
                raw = user_msg["content"]
                logger.info(f"ASR raw: {raw}")

                # Launch correction and RAG search concurrently
                async def do_correct():
                    llm_resp = await chat_complete(
                        messages=[{
                            "role": "system",
                            "content": ASR_CORRECT_SYSTEM
                        }, {
                            "role": "user",
                            "content": raw
                        }],
                        max_tokens=200,
                    )
                    return llm_resp.content.strip().strip('"').strip("'")

                correction_task = asyncio.create_task(do_correct())

                corrected_text = await correction_task

                # Apply correction
                if corrected_text and corrected_text != raw:
                    logger.info(f"ASR corrected: '{corrected_text}'")
                    for i, m in enumerate(messages):
                        if m.get("role") == "user":
                            messages[i]["content"] = corrected_text
                            break
                    user_msg["content"] = corrected_text

                # Index raw message for RAG — now deferred to batch background task

                # ── RAG: ChromaDB 分层检索 (medium + long) ──
                query_text = user_msg["content"] if user_msg else raw
                detected_cat = _detect_category(query_text)
                logger.info(f"RAG: detected category = {detected_cat}")

                from app.services.memory_retriever import ChromaDBRetriever
                retriever = ChromaDBRetriever()
                # Future: swap to HybridRetriever when needed
                # retriever = HybridRetriever([(ChromaDBRetriever(), 0.7), (MySQLKeywordRetriever(), 0.3)])
                results = await retriever.search(query_text, top_k=8, min_score=0.25)
                medium = [(r.content, r.score) for r in results if r.tier == "medium"]
                long_mem = [(r.content, r.score) for r in results if r.tier == "long"]

                logger.info(f"RAG: {len(medium)} medium + {len(long_mem)} long (ChromaDB)")

                # ── Persona: always load from MySQL (identity doesn't depend on semantic match) ──
                persona_context = "(无)"
                try:
                    async with async_session() as pdb:
                        from app.models.chat import Memory as MemModel
                        from sqlalchemy import select as sa_select
                        presult = await pdb.execute(
                            sa_select(MemModel)
                            .where(MemModel.is_deleted == False, MemModel.is_faded == False,
                                   MemModel.category == "persona")
                            .order_by(MemModel.confidence.desc())
                            .limit(5)
                        )
                        personas = [m.content for m in presult.scalars().all()]
                        if personas:
                            persona_context = "\n".join(f"- {p}" for p in personas)
                            logger.info(f"RAG: {len(personas)} persona memories injected")
                except Exception:
                    pass

                if medium or long_mem or persona_context != "(无)":
                    MAX_MEMORY_CHARS = 800

                    # Boost same-category results
                    if detected_cat:
                        medium.sort(key=lambda x: x[1] + (0.1 if detected_cat in (_detect_category(x[0]) or "") else 0), reverse=True)
                        long_mem.sort(key=lambda x: x[1] + (0.15 if detected_cat in (_detect_category(x[0]) or "") else 0), reverse=True)

                    def _build_context(items: list, char_budget: int) -> str:
                        lines = []
                        used = 0
                        for text, score in items:
                            cat_tag = ""
                            if detected_cat and detected_cat in (_detect_category(text) or ""):
                                cat_tag = "🎯"
                            chunk = f"{cat_tag}[{score:.0%}] {text}"
                            if used + len(chunk) > char_budget:
                                break
                            lines.append(chunk)
                            used += len(chunk) + 5
                        return "\n".join(lines) if lines else "(无)"

                    # Long-term priority: 85% budget, medium supplemental: 15%
                    long_budget = int(MAX_MEMORY_CHARS * 0.85)
                    med_budget = MAX_MEMORY_CHARS - long_budget

                    medium_text = _build_context(medium, med_budget)
                    long_text = _build_context(long_mem, long_budget)

                    rag_prompt = RAG_INJECT.format(
                        persona_context=persona_context,
                        medium_context=medium_text,
                        long_context=long_text,
                    )
                    logger.info(f"RAG: {len(medium)} medium + {len(long_mem)} long injected")
                else:
                    rag_prompt = None
            else:
                # Build persona-only prompt even without RAG results
                if persona_context != "(无)":
                    rag_prompt = RAG_INJECT.format(
                        persona_context=persona_context,
                        medium_context="(无)",
                        long_context="(无)",
                    )

            # ── Agent loop (with tool calling + streaming) ──
            response_parts = []
            usage_holder = {}
            agent = AgentRegistry.get("lifeos")
            logger.info(f"Agent lookup: conversation={agent is not None}")
            if agent:
                context = {"rag_prompt": rag_prompt, "conversation_id": conversation_id} if rag_prompt else {"conversation_id": conversation_id}
                stream_task = asyncio.create_task(
                    _stream_agent_loop(agent, websocket, messages, response_parts, usage_holder, context)
                )
            else:
                stream_task = asyncio.create_task(_stream_tokens(websocket, messages, response_parts, usage_holder))
            try:
                await stream_task
            except WebSocketDisconnect:
                stream_task.cancel()
                logger.info("WS client disconnected during stream — LLM cancelled")
                raise
            except asyncio.CancelledError:
                logger.info("LLM stream cancelled (client disconnected)")

            full_response = "".join(response_parts)
            if not full_response:
                continue
            logger.info(f"WS stream complete: {len(full_response)} chars")

            # Save assistant message to DB (with token tracking)
            async with async_session() as db:
                usage = usage_holder.get("usage", TokenUsage())
                ai_msg = Message(
                    id=uuid.uuid4().hex,
                    conversation_id=conversation_id,
                    role="assistant",
                    content=full_response,
                    token_count=usage.total_tokens,
                )
                db.add(ai_msg)

                # Update conversation token totals
                conv = await db.get(Conversation, conversation_id)
                if conv:
                    conv.total_tokens = (conv.total_tokens or 0) + usage.total_tokens
                    conv.message_count = (conv.message_count or 0) + 1

                msg_count = len(messages)
                await db.commit()
                logger.info(f"Saved assistant message ({usage.total_tokens} tokens)")

            # Send done IMMEDIATELY — don't wait for extraction
            await websocket.send_text(json.dumps({
                "type": "done",
                "conversation_id": conversation_id,
                "content": full_response,
            }))

            # ── Background: raw message ingest for medium-term memory ──
            asyncio.create_task(_run_raw_ingest(conversation_id))

    except WebSocketDisconnect:
        logger.info("WS client disconnected")
    except asyncio.CancelledError:
        logger.info("WS task cancelled")
    except Exception as e:
        logger.error(f"WS error: {e}", exc_info=True)
        try:
            await websocket.send_text(json.dumps({"type": "error", "content": str(e)}))
        except Exception:
            pass


QWEN_SYSTEM = (
    "你是一个友好助手。用简洁的中文回复。"
    "如果你不确定能否正确回答、问题需要深度推理、涉及代码、数学计算、"
    "或者你无法给出准确答案，请在回复末尾加上 [COMPLEX]。"
    "对于简单寒暄、常识问答、日常闲聊，正常回复不要加 [COMPLEX]。"
)


async def _stream_with_router(websocket, messages, response_parts, usage_holder):
    """Try Qwen locally first. If [COMPLEX] or error → fallback to DeepSeek API."""
    from app.services.ollama_llm import ollama_stream_chat, OllamaUsage

    # Inject Qwen system prompt (RAG context already in messages)
    qwen_messages = [{"role": "system", "content": QWEN_SYSTEM}] + messages

    try:
        # ── Try Qwen first ──
        logger.info("Router: trying Qwen...")
        qwen_text = []
        qwen_usage = None
        async for item in ollama_stream_chat(messages=qwen_messages):
            if isinstance(item, OllamaUsage):
                qwen_usage = item
            else:
                qwen_text.append(item)

        full_qwen = "".join(qwen_text).strip()
        logger.info(f"Router: Qwen response {len(full_qwen)} chars")

        # Check if Qwen flagged it as complex
        if "[COMPLEX]" in full_qwen or len(full_qwen) < 3:
            clean_text = full_qwen.replace("[COMPLEX]", "").strip()
            if clean_text and len(clean_text) > 3:
                # Qwen had a partial answer + flagged — send the partial first, then fallback
                logger.info(f"Router: Qwen partial + [COMPLEX], falling back to DeepSeek")
            else:
                logger.info(f"Router: Qwen flagged [COMPLEX] or short, falling back to DeepSeek")

            # Fallback to DeepSeek (stream tokens to client)
            async for item in stream_chat(messages=messages):
                if isinstance(item, TokenUsage):
                    usage_holder["usage"] = item
                else:
                    response_parts.append(item)
                    await websocket.send_text(json.dumps({"type": "token", "content": item}))
                    await asyncio.sleep(0)
        else:
            # Qwen handled it — send all tokens at once (Qwen is fast, didn't stream)
            logger.info(f"Router: Qwen handled ({len(full_qwen)} chars)")
            response_parts.append(full_qwen)
            await websocket.send_text(json.dumps({"type": "token", "content": full_qwen}))
            if qwen_usage:
                usage_holder["usage"] = TokenUsage(
                    prompt_tokens=qwen_usage.prompt_tokens,
                    completion_tokens=qwen_usage.completion_tokens,
                    total_tokens=qwen_usage.total_tokens,
                )

    except Exception as e:
        # Qwen failed entirely — fallback to DeepSeek
        logger.warning(f"Router: Qwen failed ({e}), falling back to DeepSeek")
        async for item in stream_chat(messages=messages):
            if isinstance(item, TokenUsage):
                usage_holder["usage"] = item
            else:
                response_parts.append(item)
                await websocket.send_text(json.dumps({"type": "token", "content": item}))
                await asyncio.sleep(0)


async def _stream_agent_loop(agent, websocket, messages, response_parts, usage_holder, context):
    """Stream agent loop output to WebSocket. Collects tokens and TokenUsage."""
    async for item in agent.loop(messages, context=context):
        if isinstance(item, TokenUsage):
            usage_holder["usage"] = item
        else:
            token = str(item)
            response_parts.append(token)
            await websocket.send_text(json.dumps({"type": "token", "content": token}))
            await asyncio.sleep(0)


async def _stream_tokens(websocket, messages, response_parts, usage_holder):
    """Stream tokens to client. Cancellable on disconnect. Collects TokenUsage."""
    async for item in stream_chat(messages=messages):
        if isinstance(item, TokenUsage):
            usage_holder["usage"] = item
        else:
            response_parts.append(item)
            await websocket.send_text(json.dumps({"type": "token", "content": item}))
            await asyncio.sleep(0)


# ── Category detection for layered RAG ──
CATEGORY_KW = {
    "persona": {"你叫", "你是", "名字", "设定", "角色", "身份", "称呼"},
    "fact": {"我", "年龄", "岁", "住在", "工作", "职业", "公司", "电话", "地址"},
    "preference": {"喜欢", "爱好", "习惯", "口味", "讨厌", "想要"},
    "project": {"项目", "在做", "开发", "正在", "任务", "目标", "进度"},
    "plan": {"打算", "计划", "准备", "下周", "明天", "将来", "以后", "去"},
}


def _detect_category(query: str) -> str | None:
    """Detect likely memory category from user query."""
    scores = {cat: sum(1 for kw in kws if kw in query) for cat, kws in CATEGORY_KW.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else None


# ── Message quality scoring for medium-term memory ──
PERSONAL_KW = {"我", "我的", "喜欢", "想", "要", "会", "在", "有", "做", "工作", "住",
               "计划", "打算", "觉得", "认为", "希望", "需要", "名字", "叫", "电话",
               "微信", "地址", "公司", "学校", "家", "去", "来", "过", "知道", "可以",
               "应该", "已经", "以前", "最近", "一直", "经常", "爱好", "兴趣", "专业",
               "行业", "项目", "目标", "梦想", "准备", "正在", "学习", "开始", "决定"}
TIME_KW = {"今天", "明天", "昨天", "下周", "周末", "这个月", "今年", "上次", "最近", "刚才",
           "一直", "总是", "每天", "经常", "偶尔", "以前", "小时候", "大学时"}
NOISE_SET = {"好的", "谢谢", "嗯", "哦", "知道了", "明白了", "再见", "你好", "拜拜", "ok", "OK", "好"}


def _quality_score(content: str) -> tuple[float, str]:
    """
    Score message quality for medium-term memory.
    Returns (score, reason).
    >= 0.6 → medium, TTL=7d
    0.4-0.6 → medium, TTL=3d
    < 0.4 → skip
    """
    stripped = content.strip()

    # Count meaningful characters — CJK is 1 char = 1 semantic unit, so count them
    cjk_count = sum(1 for c in stripped if '一' <= c <= '鿿' or '㐀' <= c <= '䶿')
    effective_len = max(len(stripped), cjk_count * 3)  # 1 CJK ≈ 3 ASCII in information density

    if effective_len < 8:
        return 0.0, "too_short"
    if stripped in NOISE_SET or len(stripped) <= 2:
        return 0.0, "noise"

    total_chars = len(stripped)
    meaningful = sum(1 for c in stripped if '一' <= c <= '鿿')  # CJK chars
    if total_chars == 0:
        return 0.0, "empty"

    # 1. Information density (0-1)
    info_density = min(meaningful / total_chars, 1.0)
    if total_chars >= 20:
        info_density = min(info_density * 1.2, 1.0)

    # 2. Personal relevance (0-1)
    personal_matches = sum(1 for kw in PERSONAL_KW if kw in stripped)
    personal_score = min(personal_matches / 4, 1.0)

    # 3. Timeliness (0-1): time-related words → more weight for medium-term
    time_matches = sum(1 for kw in TIME_KW if kw in stripped)
    time_score = min(time_matches / 2, 1.0)

    # 4. Traceability (0-1): longer + structured → more likely to be useful context
    trace_score = min(total_chars / 30, 1.0)
    if any(c in stripped for c in "：:，。！？"):
        trace_score *= 1.3

    composite = (info_density * 0.3 + personal_score * 0.4 +
                 time_score * 0.15 + trace_score * 0.15)

    # Clamp
    composite = max(0.0, min(1.0, composite))

    # Determine tier
    if composite >= 0.6:
        return composite, "medium_7d"
    elif composite >= 0.4:
        return composite, "medium_3d"
    else:
        return composite, "skip"


async def _run_raw_ingest(conversation_id: str):
    """Background: filter raw messages and add quality ones to ChromaDB."""
    import logging
    logger = logging.getLogger("uvicorn")
    try:
        async with async_session() as db:
            from sqlalchemy import select as sa_select

            result = await db.execute(
                sa_select(Message)
                .where(Message.conversation_id == conversation_id)
                .where(Message.role == "user")
                .where(Message.embedding_status == "none")
                .order_by(Message.created_at.asc())
            )
            pending = result.scalars().all()

            ingested_7d = 0
            ingested_3d = 0

            for msg in pending:
                score, reason = _quality_score(msg.content)
                if reason.startswith("medium"):
                    ttl = 7 if reason == "medium_7d" else 3
                    rag_service.add_memory(msg.id, msg.content, "user",
                                           tier="medium", ttl_days=ttl)
                    msg.embedding_status = f"d_{reason}" if reason.startswith("medium") else f"s_{reason[:6]}"
                    if ttl == 7:
                        ingested_7d += 1
                    else:
                        ingested_3d += 1
                else:
                    msg.embedding_status = f"s_{reason[:8]}"

            await db.commit()
            logger.info(f"Raw ingest: {ingested_7d}×7d + {ingested_3d}×3d → ChromaDB, "
                       f"{len(pending)-ingested_7d-ingested_3d} skipped")
    except Exception as e:
        logger.error(f"Raw ingest failed: {e}")


def _calc_confidence(base_imp: int, confirmations: int,
                     last_confirmed: datetime, now: datetime) -> int:
    """Calculate dynamic confidence with time decay."""
    import math
    days_since = (now - last_confirmed).total_seconds() / 86400

    # Decay rate λ depends on base importance
    if base_imp >= 80:
        lam = 0.01
    elif base_imp >= 50:
        lam = 0.03
    else:
        lam = 0.07

    # Time decay: e^(-λ × days)
    decay = math.exp(-lam * days_since)

    # Confirmation bonus: +20% per confirmation, capped at 5
    conf_bonus = 1.0 + 0.2 * min(confirmations - 1, 4)

    confidence = int(base_imp * decay * conf_bonus)
    return max(0, min(100, confidence))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
    )
