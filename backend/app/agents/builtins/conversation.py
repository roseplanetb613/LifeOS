"""LifeOSAgent — the core agent with tool access, streaming, and execution tracing."""
import json
import logging
from typing import AsyncGenerator
from app.agents.base import Agent, Thought, Observation
from app.tools.registry import ToolRegistry
from app.services.llm import chat_complete, stream_chat, TokenUsage
from app.services.prompts import DEFAULT_CHAT
from app.services.agent_trace import start_task, record_step, finish_task, fail_task
from app.tools.builtins.time_tools import get_time_context
from app.agents.planner import Planner, ToolSelector, Verifier, RePlanner, is_complex_request

logger = logging.getLogger("uvicorn")


class LifeOSAgent(Agent):
    """LifeOS core agent — the single entry point for all conversations."""

    def __init__(self):
        self._pending_tool = None  # (tool_name, tool_params) awaiting confirmation

    @property
    def name(self) -> str:
        return "lifeos"

    @property
    def system_prompt(self) -> str:
        return DEFAULT_CHAT

    @property
    def tools(self) -> list:
        return ToolRegistry.list_all()

    async def think(self, messages: list[dict], context: dict | None = None) -> Thought:
        """Decide: respond directly or use a tool. Uses non-streaming LLM call."""
        tool_hint = (
            "\n\n可用工具: memory_query(查记忆), save_memory(存信息), forget_memory(删记忆), get_time(查时间)。"
            "简单问候、闲聊时不要调工具，直接回复。"
            "只在需要查/存/删信息或查时间时才调用工具。"
        )
        base_prompt = context["rag_prompt"] if (context and context.get("rag_prompt")) else self.system_prompt
        time_ctx = get_time_context()
        system_msg = {"role": "system", "content": f"{time_ctx}\n{base_prompt}{tool_hint}"}

        full_messages = [system_msg] + messages
        tool_schemas = ToolRegistry.get_schemas()
        logger.info(f"[LifeOS] think: system_prompt_len={len(system_msg['content'])}, "
                    f"tools_available={[t['function']['name'] for t in tool_schemas]}, "
                    f"user_msg={messages[-1]['content'][:40] if messages else 'none'}")

        resp = await chat_complete(
            messages=full_messages,
            tools=tool_schemas if tool_schemas else None,
        )
        self._last_think_usage = resp.usage

        if resp.tool_calls:
            tc = resp.tool_calls[0]
            logger.info(f"[LifeOS] think → use_tool: {tc['name']}({json.dumps(tc['arguments'], ensure_ascii=False)})")
            return Thought(
                reasoning=f"Need to call {tc['name']}",
                action="use_tool",
                tool_name=tc["name"],
                tool_params=tc["arguments"],
                tool_call_id=tc.get("id", ""),
            )
        else:
            logger.info(f"[LifeOS] think → respond ({len(resp.content)} chars non-streamed)")
            return Thought(
                reasoning="Direct response",
                action="respond",
                response=resp.content,
            )

    async def loop(
        self,
        messages: list[dict],
        context: dict | None = None,
        max_steps: int = 5,
    ) -> AsyncGenerator[str | TokenUsage, None]:
        """Think → Act → Observe loop with streaming + trace recording."""
        working_messages = list(messages)
        total_usage = TokenUsage()

        # ── Planner: complex multi-step requests ──
        user_msg = next((m["content"] for m in reversed(working_messages) if m.get("role") == "user"), "")
        if is_complex_request(user_msg):
            logger.info(f"[LifeOS] → Planner (complex: '{user_msg[:40]}')")
            selector = ToolSelector()
            planner = Planner()
            try:
                # Step 1: select tools (rule-first, LLM fallback)
                tools = await selector.select_smart(user_msg)
                # Step 2: plan execution order (only if tools found)
                plan = None
                if tools:
                    plan = await planner.plan(user_msg, tools,
                                             context.get("rag_prompt", "") if context else "")
                    total_usage.prompt_tokens += plan.usage.prompt_tokens
                    total_usage.completion_tokens += plan.usage.completion_tokens
                    total_usage.total_tokens += plan.usage.total_tokens

                if plan and plan.needs_tools and plan.dag:
                    logger.info(f"[LifeOS] Plan: goal='{plan.goal[:40]}' "
                               f"confidence={plan.confidence:.0%} {len(plan.dag)} steps")
                    if plan.confidence < 0.5:
                        logger.info(f"[LifeOS] Plan confidence too low ({plan.confidence:.0%}), "
                                   f"falling back to reactive")
                        plan = None  # triggers fallback
                if plan and plan.needs_tools and plan.dag:
                    results = await planner.execute(plan, self.act)

                    # ── Verify + RePlan loop (max 2 retries) ──
                    verifier = Verifier()
                    replanner = RePlanner()
                    for retry in range(3):  # 1 initial + 2 retries
                        v = await verifier.verify(plan.goal, results)
                        if v.satisfied or retry >= 2:
                            if not v.satisfied:
                                logger.info(f"[LifeOS] Verifier: not satisfied after {retry+1} "
                                           f"attempts, responding with partial results")
                            break
                        # Replan with feedback
                        logger.info(f"[LifeOS] Verifier: not satisfied → "
                                   f"RePlanner (retry {retry+1}/2): {v.feedback[:60]}")
                        plan = await replanner.replan(plan.goal, v.feedback, tools)
                        if not plan.dag:
                            break
                        results = await planner.execute(plan, self.act)

                    # ── Synthesize final response from all results ──
                    results_text = "\n".join(
                        f"[Step {r['step']} | {r['tool']}]: {str(r['data'])[:300]}"
                        for r in results
                    )
                    working_messages.append({
                        "role": "user",
                        "content": f"[系统: 已执行以下计划步骤]\n{results_text}\n请基于以上结果生成回复。",
                    })

                    # Re-think to generate final response
                    thought = await self.think(working_messages, context)
                    if thought.action == "respond":
                        base = context["rag_prompt"] if (context and context.get("rag_prompt")) else self.system_prompt
                        stream_msgs = [{"role": "system", "content": f"{get_time_context()}\n{base}"}] + working_messages
                        async for item in stream_chat(messages=stream_msgs):
                            if isinstance(item, TokenUsage):
                                total_usage.prompt_tokens += item.prompt_tokens
                                total_usage.completion_tokens += item.completion_tokens
                                total_usage.total_tokens += item.total_tokens
                            else:
                                yield item
                        yield total_usage
                        return
                else:
                    logger.info("[LifeOS] Planner: no tools needed, falling back to reactive")
            except Exception as e:
                logger.error(f"[LifeOS] Planner failed: {e}, falling back to reactive")

        # ── Trace: start task ──
        conv_id = context.get("conversation_id", "") if context else ""
        user_msg = next((m["content"] for m in reversed(working_messages) if m.get("role") == "user"), "")
        task_id = None
        if conv_id:
            try:
                task_id = await start_task(conv_id, user_msg)
            except Exception as e:
                logger.warning(f"[LifeOS] trace start failed: {e}")

        try:
            for step in range(max_steps):
                thought = await self.think(working_messages, context)

                # ── Trace: record think step ──
                if task_id:
                    try:
                        await record_step(
                            task_id=task_id, agent_name=self.name,
                            step_num=step + 1,
                            action=f"think→{thought.action}" if thought.action else "think",
                            input_text=thought.reasoning,
                            tool_name=thought.tool_name,
                            tokens_used=getattr(self, '_last_think_usage', TokenUsage()).total_tokens,
                            status="done",
                        )
                    except Exception:
                        pass

                if thought.action == "respond":
                    if task_id:
                        try:
                            await record_step(
                                task_id=task_id, agent_name=self.name,
                                step_num=step + 1,
                                action="respond",
                                input_text="streaming response",
                                tokens_used=getattr(self, '_last_think_usage', TokenUsage()).total_tokens,
                                status="done",
                            )
                        except Exception:
                            pass

                    base = context["rag_prompt"] if (context and context.get("rag_prompt")) else self.system_prompt
                    system_msg = {"role": "system", "content": f"{get_time_context()}\n{base}"}
                    stream_msgs = [system_msg] + working_messages

                    response_text = []
                    async for item in stream_chat(messages=stream_msgs):
                        if isinstance(item, TokenUsage):
                            total_usage.prompt_tokens += item.prompt_tokens
                            total_usage.completion_tokens += item.completion_tokens
                            total_usage.total_tokens += item.total_tokens
                        else:
                            response_text.append(item)
                            yield item

                    if task_id:
                        try:
                            await finish_task(task_id, total_usage.total_tokens,
                                             "".join(response_text)[:200])
                        except Exception:
                            pass
                    yield total_usage
                    return

                elif thought.action == "use_tool" and thought.tool_name:
                    # ── Permission check ──
                    from app.tools.registry import ToolRegistry as TR
                    tool = TR.get(thought.tool_name)
                    if tool and tool.permission == "restricted":
                        # Was this tool already confirmed by user?
                        user_last = next((m["content"] for m in reversed(working_messages)
                                         if m.get("role") == "user"), "")
                        confirmed = self._pending_tool and self._pending_tool[0] == thought.tool_name \
                                    and any(w in user_last for w in ["确认", "好的", "可以", "行", "yes", "ok", "允许"])
                        if not confirmed:
                            logger.info(f"[LifeOS] tool {thought.tool_name} needs confirmation")
                            self._pending_tool = (thought.tool_name, thought.tool_params)
                            yield json.dumps({
                                "type": "confirm",
                                "tool": thought.tool_name,
                                "args": thought.tool_params,
                                "hint": f"回复'确认'以允许调用 {thought.tool_name}",
                            }, ensure_ascii=False)
                            yield total_usage
                            return
                        else:
                            logger.info(f"[LifeOS] tool {thought.tool_name} confirmed by user")
                            self._pending_tool = None
                    obs = await self.act(thought.tool_name, **(thought.tool_params or {}))
                    tool_result = obs.data if obs.success else f"Error: {obs.error}"

                    # ── Trace: record tool result ──
                    if task_id:
                        try:
                            await record_step(
                                task_id=task_id, agent_name=self.name,
                                step_num=step + 1,
                                action="use_tool",
                                input_text=json.dumps(thought.tool_params, ensure_ascii=False),
                                output_text=str(tool_result)[:2000],
                                tool_name=thought.tool_name,
                                tokens_used=getattr(self, '_last_think_usage', TokenUsage()).total_tokens,
                                status="done",
                            )
                        except Exception:
                            pass

                    working_messages.append({
                        "role": "assistant", "content": None,
                        "tool_calls": [{
                            "id": thought.tool_call_id, "type": "function",
                            "function": {
                                "name": thought.tool_name,
                                "arguments": json.dumps(thought.tool_params, ensure_ascii=False),
                            }
                        }]
                    })
                    working_messages.append({
                        "role": "tool", "tool_call_id": thought.tool_call_id,
                        "content": f"[Tool {thought.tool_name} result]: {tool_result}",
                    })
                    logger.info(f"[LifeOS] step {step+1}/{max_steps}: tool {thought.tool_name} → {len(str(tool_result))} chars")
                    continue

                else:
                    logger.warning(f"[LifeOS] step {step+1}: couldn't decide, thought.action={thought.action}")
                    if task_id:
                        try:
                            await fail_task(task_id, f"Agent couldn't decide at step {step+1}")
                        except Exception:
                            pass
                    yield "抱歉，我暂时无法处理这个请求。"
                    yield total_usage
                    return

            logger.warning(f"[LifeOS] max steps ({max_steps}) exceeded")
            if task_id:
                try:
                    await fail_task(task_id, "max steps exceeded")
                except Exception:
                    pass
            yield "抱歉，我暂时无法处理这个请求。"
            yield total_usage

        except Exception as e:
            logger.error(f"[LifeOS] loop error: {e}")
            if task_id:
                try:
                    await fail_task(task_id, str(e))
                except Exception:
                    pass
            raise
