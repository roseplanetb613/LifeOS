"""Planner — ToolSelector + Planner with async parallel execution."""
import asyncio
import json
import logging
from dataclasses import dataclass, field
from app.services.llm import chat_complete, TokenUsage
from app.services.ollama_llm import ollama_chat_complete
from app.services.prompts import PLANNER_PROMPT
from app.tools.registry import ToolRegistry

logger = logging.getLogger("uvicorn")

# ── Complexity detection ──
COMPLEXITY_KEYWORDS = ["然后", "再", "顺便", "同时", "先", "之后", "接着", "并且", "还有", "也"]
WAVES = ["查", "搜", "找", "记", "存", "忘", "删", "提醒", "几点", "时间", "天气",
         "怎么", "为什么", "帮我", "告诉我", "我要"]

# ── Tool selection: keyword → tool mapping ──
TOOL_HINTS = {
    "memory_query":  ["喜欢", "说过", "记得", "偏好", "之前", "爱好", "兴趣", "什么", "谁", "名字", "哪", "查", "搜", "找", "有没有"],
    "save_memory":   ["记住", "存", "记录", "记下来", "保存", "记"],
    "forget_memory": ["忘掉", "删除", "不要记", "去掉", "取消"],
    "get_time":      ["几点", "时间", "日期", "星期", "今天", "现在", "几点"],
}


@dataclass
class PlanStep:
    id: int                      # unique within plan
    tool_name: str
    tool_params: dict
    reasoning: str = ""
    depends_on: list[int] = field(default_factory=list)  # step IDs this depends on


@dataclass
class Plan:
    goal: str                    # what we're trying to achieve
    dag: list[PlanStep]          # execution DAG
    confidence: float = 1.0      # 0.0 ~ 1.0, planner's confidence in this plan
    reasoning: str = ""
    needs_tools: bool = True
    usage: TokenUsage = field(default_factory=TokenUsage)

    @property
    def steps(self) -> list[PlanStep]:
        """Backward compat alias."""
        return self.dag


def is_complex_request(user_message: str) -> bool:
    """Quick heuristic: does this message need a plan?"""
    if len(user_message) < 10:
        return False
    wave_count = sum(1 for kw in WAVES if kw in user_message)
    chain_count = sum(1 for kw in COMPLEXITY_KEYWORDS if kw in user_message)
    return wave_count >= 2 or chain_count >= 1


class ToolSelector:
    """Select which tools are relevant for a request. Rule-first, LLM fallback."""

    def select_by_rules(self, user_message: str) -> list[str]:
        """Keyword-based tool selection. Returns tool names."""
        scores = {}
        for tool_name, keywords in TOOL_HINTS.items():
            score = sum(1 for kw in keywords if kw in user_message)
            if score > 0:
                scores[tool_name] = score
        # Return tools sorted by match score (highest first)
        return [t for t, _ in sorted(scores.items(), key=lambda x: x[1], reverse=True)]

    async def select_by_llm(
        self, user_message: str, model: str = "qwen3-local:latest"
    ) -> list[str]:
        """Ask LLM to pick tools. Used when rules produce ambiguous results."""
        tools_desc = "\n".join(
            f"- {t.name}: {t.description}" for t in ToolRegistry.list_all()
        )
        prompt = (
            "你是一个工具选择器。根据用户请求，选出需要调用的工具。"
            "只回复工具名列表，一行一个，不要解释。\n\n"
            f"可用工具:\n{tools_desc}\n\n"
            f"用户请求: {user_message}\n\n"
            "需要的工具:"
        )
        try:
            resp = await ollama_chat_complete(
                messages=[{"role": "user", "content": prompt}],
                model=model, max_tokens=100,
            )
            tools = []
            for line in resp.content.strip().split("\n"):
                name = line.strip().lower()
                if name in [t.name for t in ToolRegistry.list_all()]:
                    tools.append(name)
            if tools:
                logger.info(f"[ToolSelector] LLM → {tools}")
                return tools
        except Exception as e:
            logger.warning(f"[ToolSelector] LLM failed: {e}")
        return []

    def select(self, user_message: str) -> list[str]:
        """Select tools: rules first, LLM if needed."""
        tools = self.select_by_rules(user_message)
        if tools:
            logger.info(f"[ToolSelector] rules → {tools}")
        return tools  # may be empty → caller can invoke LLM select

    async def select_smart(self, user_message: str) -> list[str]:
        """Rule-first with LLM fallback for ambiguous cases."""
        tools = self.select_by_rules(user_message)
        if tools:
            return tools
        # Rules produced nothing — try LLM
        return await self.select_by_llm(user_message)


@dataclass
class VerificationResult:
    """Verifier's judgment of plan execution results."""
    satisfied: bool                # goal achieved?
    score: float                   # 0.0 ~ 1.0, how well the goal was met
    feedback: str = ""             # what's missing or wrong (for RePlanner)
    summary: str = ""              # human-readable summary of what happened


class Verifier:
    """Verify that executed plan results satisfy the goal. LLM-powered."""

    async def verify(
        self, goal: str, results: list[dict],
        model: str = "qwen3-local:latest",
    ) -> VerificationResult:
        """Check if plan results achieve the goal."""
        results_text = "\n".join(
            f"[Step {r['step']} | {r['tool']}]: {str(r['data'])[:200]}"
            for r in results
        )
        prompt = (
            "你是计划验证器。判断已执行的计划步骤是否达成了目标。\n\n"
            f"目标: {goal}\n\n"
            f"执行结果:\n{results_text}\n\n"
            "判断标准:\n"
            "- satisfied=true: 所有必要信息已获取，目标达成\n"
            "- satisfied=false: 缺少关键信息、工具未找到相关内容、或结果不相关\n"
            "- score: 0.0~1.0 完成度\n"
            "- feedback: 如不满意，具体指出缺什么（供重新规划用）\n\n"
            '只回复 JSON: {"satisfied": true/false, "score": 0.8, "feedback": "原因", "summary": "一句话总结"}'
        )
        try:
            resp = await ollama_chat_complete(
                messages=[{"role": "user", "content": prompt}],
                model=model, max_tokens=200,
            )
            raw = resp.content.strip().lstrip("```json").rstrip("```").strip()
            # Extract JSON if embedded in text
            if not raw.startswith("{"):
                import re
                m = re.search(r'\{[\s\S]*\}', raw)
                if m: raw = m.group(0)
            data = json.loads(raw)
            result = VerificationResult(
                satisfied=data.get("satisfied", True),
                score=float(data.get("score", 0.8)),
                feedback=data.get("feedback", ""),
                summary=data.get("summary", ""),
            )
            logger.info(f"[Verifier] satisfied={result.satisfied} score={result.score:.0%} "
                       f"→ {result.summary[:60]}")
            return result
        except Exception as e:
            logger.warning(f"[Verifier] failed: {e}, assuming satisfied")
            return VerificationResult(satisfied=True, score=0.8, summary="verifier error — assuming ok")


class RePlanner:
    """Create a corrected plan based on verification feedback."""

    async def replan(
        self, goal: str, feedback: str, tools: list[str],
        model: str = "qwen3-local:latest",
    ) -> Plan:
        """Generate a new plan that addresses the verifier's feedback."""
        tools_desc = "\n".join(
            f"- {t.name}: {t.description}"
            for t in ToolRegistry.list_all()
            if t.name in tools
        )
        prompt = (
            "你是任务重规划器。原计划执行后未完全达成目标，请根据反馈调整计划。\n\n"
            f"可用工具:\n{tools_desc}\n\n"
            f"目标: {goal}\n"
            f"反馈: {feedback}\n\n"
            "规则:\n"
            "- 调整工具参数（如更精准的查询词）\n"
            "- 如确实无法完成，降低 confidence 并在 reasoning 中说明\n"
            "- 输出格式与规划器相同\n\n"
            '只回复 JSON (含 goal, confidence, dag)'
        )
        try:
            resp = await ollama_chat_complete(
                messages=[{"role": "user", "content": prompt}],
                model=model, max_tokens=256,
            )
            raw = resp.content.strip().lstrip("```json").rstrip("```").strip()
            import re
            if not raw.startswith("{"):
                m = re.search(r'\{[\s\S]*\}', raw)
                if m: raw = m.group(0)
            data = json.loads(raw)
            steps_data = data.get("dag") or data.get("steps", [])
            dag = [PlanStep(
                id=s.get("id", i),
                tool_name=s["tool_name"],
                tool_params=s.get("tool_params", {}),
                reasoning=s.get("reasoning", ""),
                depends_on=s.get("depends_on") if isinstance(s.get("depends_on"), list) else [],
            ) for i, s in enumerate(steps_data)]
            goal_text = data.get("goal", goal)
            confidence = float(data.get("confidence", 0.6))
            logger.info(f"[RePlanner] → goal='{goal_text[:30]}' confidence={confidence:.0%} "
                       f"{len(dag)} steps")
            return Plan(
                goal=goal_text, dag=dag, confidence=confidence,
                reasoning=data.get("reasoning", ""),
                needs_tools=len(dag) > 0,
                usage=TokenUsage(),  # Qwen local usage not tracked here
            )
        except Exception as e:
            logger.warning(f"[RePlanner] failed: {e}")
            return Plan(goal=goal, dag=[], confidence=0.0,
                       reasoning=f"replan failed: {e}", needs_tools=False)


class Planner:
    """Create and execute multi-step plans."""

    async def plan(
        self, user_message: str, tools: list[str],
        context: str = "", model: str = "qwen3-local:latest",
    ) -> Plan:
        """Generate an execution plan using the given tool subset."""
        if not tools:
            return Plan(goal="", dag=[], reasoning="no tools selected", needs_tools=False)

        tools_desc = "\n".join(
            f"- {t.name}: {t.description}"
            for t in ToolRegistry.list_all()
            if t.name in tools
        )

        prompt = PLANNER_PROMPT.format(
            tools=tools_desc,
            request=user_message,
            context=context or "(无)",
        )

        # ── Try Qwen local first ──
        raw = ""
        usage = TokenUsage()
        source = "deepseek"
        import time

        try:
            t0 = time.perf_counter()
            qwen_resp = await ollama_chat_complete(
                messages=[{"role": "user", "content": prompt}],
                model=model, max_tokens=256,
            )
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            raw = qwen_resp.content.strip()
            if raw:
                usage = TokenUsage(
                    prompt_tokens=qwen_resp.usage.prompt_tokens,
                    completion_tokens=qwen_resp.usage.completion_tokens,
                    total_tokens=qwen_resp.usage.total_tokens,
                )
                source = "qwen"
                logger.info(f"[Planner] Qwen ✓ {len(raw)} chars {elapsed_ms}ms "
                           f"(prompt={qwen_resp.usage.prompt_tokens} "
                           f"completion={qwen_resp.usage.completion_tokens})")
        except Exception as e:
            logger.warning(f"[Planner] Qwen error: {type(e).__name__}: {e}")

        # ── Fallback: DeepSeek ──
        if not raw:
            try:
                t0 = time.perf_counter()
                deep_resp = await chat_complete(
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=256,
                )
                elapsed_ms = int((time.perf_counter() - t0) * 1000)
                raw = deep_resp.content.strip()
                usage = deep_resp.usage
                source = "deepseek"
                logger.info(f"[Planner] DeepSeek fallback: {len(raw)} chars {elapsed_ms}ms")
            except Exception as e:
                logger.error(f"[Planner] DeepSeek also failed: {e}")
                return Plan(goal="", dag=[], reasoning="both failed", needs_tools=False, usage=usage)

        # ── Parse JSON ──
        try:
            return self._parse_plan(raw, source, usage, prompt)
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"[Planner] parse failed ({source}): {e}")
            if source == "qwen":
                return await self._deepseek_retry(prompt, usage)
            return Plan(goal="", dag=[], reasoning="parse failed", needs_tools=False, usage=usage)

    def _parse_plan(self, raw: str, source: str, usage: TokenUsage, prompt: str = "") -> Plan:
        raw = raw.lstrip("```json").rstrip("```").strip()
        if not raw.startswith("{"):
            import re
            m = re.search(r'\{[\s\S]*"(?:dag|steps)"[\s\S]*\}', raw)
            if m:
                raw = m.group(0)
                logger.info(f"[Planner] extracted JSON from text ({len(raw)} chars)")
        data = json.loads(raw)
        # Support both old "steps" and new "dag" format
        steps_data = data.get("dag") or data.get("steps", [])
        dag = []
        for s in steps_data:
            dep = s.get("depends_on")
            if dep is None:
                dep = []
            elif isinstance(dep, int):
                dep = [dep]
            elif isinstance(dep, str):
                # "step0" → [0]; also handle bare numbers like "0"
                try:
                    dep = [int(dep)]
                except ValueError:
                    dep = [int(dep.replace("step", ""))] if dep.startswith("step") else []
            elif isinstance(dep, list):
                dep = [
                    int(d) if isinstance(d, int) else
                    int(str(d).replace("step", ""))
                    for d in dep
                ]
            dag.append(PlanStep(
                id=s.get("id", len(dag)),
                tool_name=s["tool_name"],
                tool_params=s.get("tool_params", {}),
                reasoning=s.get("reasoning", ""),
                depends_on=dep,
            ))
        goal = data.get("goal", "") or data.get("reasoning", "")
        confidence = float(data.get("confidence", 0.8 if dag else 0.3))
        logger.info(f"[Planner] {source} → goal='{goal[:40]}' confidence={confidence:.0%} "
                    f"dag={len(dag)} steps: {[s.tool_name for s in dag]}")
        return Plan(
            goal=goal, dag=dag, confidence=confidence,
            reasoning=data.get("reasoning", ""),
            needs_tools=len(dag) > 0,
            usage=usage,
        )

    async def _deepseek_retry(self, prompt: str, fallback_usage: TokenUsage) -> Plan:
        try:
            resp = await chat_complete(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=256,
            )
            raw = resp.content.strip().lstrip("```json").rstrip("```").strip()
            data = json.loads(raw)
            steps = [PlanStep(
                tool_name=s["tool_name"],
                tool_params=s.get("tool_params", {}),
                reasoning=s.get("reasoning", ""),
                depends_on=s.get("depends_on") if isinstance(s.get("depends_on"), list) else [],
            ) for s in data.get("steps", [])]
            logger.info(f"[Planner] deepseek retry → {len(steps)} steps")
            return Plan(goal=data.get("goal", ""), dag=steps,
                       confidence=data.get("confidence", 0.8), reasoning=data.get("reasoning", ""),
                       needs_tools=len(steps) > 0, usage=resp.usage)
        except Exception:
            return Plan(goal="", dag=[], reasoning="parse failed", needs_tools=False, usage=fallback_usage)

    async def execute(self, plan: Plan, act_fn) -> list[dict]:
        """Execute plan: parallel for independent steps, serial for dependent ones."""
        results: list[dict | None] = [None] * len(plan.steps)
        pending = list(enumerate(plan.steps))
        wave_num = 0

        while pending:
            wave_num += 1
            ready, still_pending = [], []
            for idx, step in pending:
                # Validate deps: ignore out-of-range references
                valid_deps = [d for d in step.depends_on if 0 <= d < len(results)]
                if all(results[i] is not None for i in valid_deps):
                    ready.append((idx, step))
                else:
                    still_pending.append((idx, step))
            pending = still_pending

            if not ready:
                logger.error(f"[Planner] deadlock — {len(pending)} steps stuck")
                break

            async def exec_step(idx: int, step: PlanStep):
                logger.info(f"[Planner] wave {wave_num}: {step.tool_name}"
                           f"({json.dumps(step.tool_params, ensure_ascii=False)})")
                obs = await act_fn(step.tool_name, **(step.tool_params or {}))
                return idx, {
                    "step": idx, "tool": step.tool_name, "params": step.tool_params,
                    "success": obs.success, "data": obs.data if obs.success else obs.error,
                    "reasoning": step.reasoning,
                }

            wave_results = await asyncio.gather(
                *(exec_step(idx, step) for idx, step in ready), return_exceptions=True,
            )
            for wr in wave_results:
                if isinstance(wr, Exception):
                    logger.error(f"[Planner] wave {wave_num} error: {wr}")
                else:
                    idx, result = wr
                    results[idx] = result

        return [r for r in results if r is not None]
