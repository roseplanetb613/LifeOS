"""Agent Trace — record agent execution steps to task_step / agent_task tables."""
import uuid
import json
from datetime import datetime
from app.db.session import async_session
from app.models.agent_task import AgentTask, TaskStep


def _id():
    return uuid.uuid4().hex


async def start_task(conversation_id: str, user_message: str) -> str:
    """Create an agent_task record. Returns task_id."""
    task_id = _id()
    async with async_session() as db:
        task = AgentTask(
            id=task_id,
            conversation_id=conversation_id,
            user_request=user_message[:500],
            status="executing",
        )
        db.add(task)
        await db.commit()
    return task_id


async def record_step(task_id: str, agent_name: str, step_num: int,
                      action: str, input_text: str = "", output_text: str = "",
                      tool_name: str | None = None, tokens_used: int = 0,
                      status: str = "done", error: str | None = None):
    """Record a single agent loop step."""
    step_id = _id()
    async with async_session() as db:
        step = TaskStep(
            id=step_id,
            task_id=task_id,
            agent_name=agent_name,
            tool_name=tool_name,
            input_text=input_text[:2000],
            output_text=output_text[:2000] if not error else f"ERROR: {error}"[:2000],
            status=status,
            tokens_used=tokens_used,
            created_at=datetime.utcnow(),
            completed_at=datetime.utcnow() if status != "running" else None,
        )
        db.add(step)
        await db.commit()
    return step_id


async def finish_task(task_id: str, total_tokens: int = 0, result_summary: str = ""):
    """Mark agent_task as done."""
    async with async_session() as db:
        task = await db.get(AgentTask, task_id)
        if task:
            task.status = "done"
            task.total_tokens = total_tokens
            task.result_summary = result_summary[:500]
            task.updated_at = datetime.utcnow()
            await db.commit()


async def fail_task(task_id: str, error: str):
    """Mark agent_task as failed."""
    async with async_session() as db:
        task = await db.get(AgentTask, task_id)
        if task:
            task.status = "failed"
            task.result_summary = error[:500]
            task.updated_at = datetime.utcnow()
            await db.commit()


async def get_trace(conversation_id: str, limit: int = 5) -> list[dict]:
    """Get recent agent execution traces for a conversation."""
    from sqlalchemy import select as sa_select
    async with async_session() as db:
        result = await db.execute(
            sa_select(AgentTask)
            .where(AgentTask.conversation_id == conversation_id)
            .order_by(AgentTask.created_at.desc())
            .limit(limit)
        )
        tasks = result.scalars().all()

        traces = []
        for task in tasks:
            steps_result = await db.execute(
                sa_select(TaskStep)
                .where(TaskStep.task_id == task.id)
                .order_by(TaskStep.created_at.asc())
            )
            steps = steps_result.scalars().all()
            traces.append({
                "task_id": task.id,
                "conversation_id": task.conversation_id,
                "user_request": task.user_request,
                "status": task.status,
                "total_tokens": task.total_tokens,
                "created_at": task.created_at.isoformat(),
                "steps": [{
                    "step": i + 1,
                    "agent": s.agent_name,
                    "action": "use_tool" if s.tool_name else "respond",
                    "tool": s.tool_name,
                    "input": s.input_text,
                    "output": s.output_text,
                    "status": s.status,
                    "tokens": s.tokens_used,
                    "ms": int((s.completed_at - s.created_at).total_seconds() * 1000) if s.completed_at and s.created_at else 0,
                } for i, s in enumerate(steps)],
            })
        return traces


async def get_trace_detail(task_id: str) -> dict | None:
    """Get a single task with full step-by-step detail."""
    import json
    from sqlalchemy import select as sa_select
    async with async_session() as db:
        task = await db.get(AgentTask, task_id)
        if not task:
            return None

        steps_result = await db.execute(
            sa_select(TaskStep)
            .where(TaskStep.task_id == task_id)
            .order_by(TaskStep.created_at.asc())
        )
        steps = steps_result.scalars().all()

        step_list = []
        for i, s in enumerate(steps):
            info = {
                "step": i + 1,
                "agent": s.agent_name,
                "status": s.status,
                "tokens": s.tokens_used,
            }
            if s.tool_name:
                info["action"] = "use_tool"
                info["tool"] = s.tool_name
                try:
                    info["params"] = json.loads(s.input_text) if s.input_text and s.input_text.startswith("{") else s.input_text
                except (json.JSONDecodeError, TypeError):
                    info["params"] = s.input_text
                # Parse output for key metrics
                try:
                    if s.output_text and s.output_text.startswith("{"):
                        out = json.loads(s.output_text)
                        info["result"] = {
                            k: out[k] for k in ["action", "found", "deleted", "datetime", "time", "weekday"]
                            if k in out
                        }
                        if "found" in out:
                            info["result"]["results"] = [
                                {"content": r["content"][:40], "score": r["score"], "tier": r["tier"]}
                                for r in out.get("results", [])[:3]
                            ]
                    else:
                        info["result"] = str(s.output_text)[:100] if s.output_text else ""
                except Exception:
                    info["result"] = str(s.output_text)[:100] if s.output_text else ""
            else:
                info["action"] = "think" if "Direct response" in (s.input_text or "") else "respond"
                info["reasoning"] = s.input_text
            if s.completed_at and s.created_at:
                info["ms"] = int((s.completed_at - s.created_at).total_seconds() * 1000)
            step_list.append(info)

        return {
            "task_id": task.id,
            "conversation_id": task.conversation_id,
            "user_request": task.user_request,
            "status": task.status,
            "total_tokens": task.total_tokens,
            "total_ms": int((task.updated_at - task.created_at).total_seconds() * 1000) if task.updated_at and task.created_at else 0,
            "created_at": task.created_at.isoformat(),
            "steps": step_list,
        }


async def get_latest_trace(limit: int = 10) -> list[dict]:
    """Get the most recent agent traces across all conversations."""
    from sqlalchemy import select as sa_select
    async with async_session() as db:
        result = await db.execute(
            sa_select(AgentTask)
            .order_by(AgentTask.created_at.desc())
            .limit(limit)
        )
        tasks = result.scalars().all()

        traces = []
        for task in tasks:
            traces.append({
                "task_id": task.id,
                "conversation_id": task.conversation_id,
                "user_request": task.user_request[:80],
                "status": task.status,
                "total_tokens": task.total_tokens,
                "created_at": task.created_at.isoformat(),
            })
        return traces
