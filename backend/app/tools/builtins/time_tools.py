"""Time tools — time awareness, reminders, and scheduling."""
from datetime import datetime, timezone, timedelta
from app.tools.base import Tool, ToolResult, ToolSchema

CST = timezone(timedelta(hours=8))


class GetTimeTool(Tool):
    """Get the current date and time."""

    @property
    def name(self) -> str:
        return "get_time"

    @property
    def description(self) -> str:
        return (
            "获取当前日期和时间。"
            "当用户问几点、今天几号、星期几、或者需要时间上下文时使用。"
        )

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
            },
        )

    async def run(self) -> ToolResult:
        now = datetime.now(CST)
        return ToolResult.ok({
            "datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M"),
            "weekday": ["周一","周二","周三","周四","周五","周六","周日"][now.weekday()],
            "timestamp": int(now.timestamp()),
            "timezone": "Asia/Shanghai",
        })


def get_time_context() -> str:
    """Return a time context string for injection into the system prompt."""
    now = datetime.now(CST)
    return (
        f"[当前时间: {now.strftime('%Y年%m月%d日')} "
        f"{['周一','周二','周三','周四','周五','周六','周日'][now.weekday()]} "
        f"{now.strftime('%H:%M')}]"
    )
