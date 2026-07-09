"""Log broadcaster — captures Python logs and streams them to WebSocket clients."""
import asyncio
import json
import logging
from datetime import datetime


class LogBroadcaster(logging.Handler):
    """A logging handler that broadcasts messages to all connected WebSocket clients."""

    def __init__(self):
        super().__init__()
        self._listeners: list[asyncio.Queue] = []
        self.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
                                            datefmt="%H:%M:%S"))

    def emit(self, record: logging.LogRecord):
        """Called by the logging framework for each log message."""
        try:
            msg = self.format(record)
            entry = json.dumps({
                "time": datetime.utcnow().strftime("%H:%M:%S"),
                "level": record.levelname,
                "name": record.name,
                "msg": record.getMessage(),
                "full": msg,
            })
            # Push to all listeners (non-blocking)
            for q in self._listeners:
                try:
                    q.put_nowait(entry)
                except asyncio.QueueFull:
                    pass  # skip slow clients
        except Exception:
            pass

    def subscribe(self) -> asyncio.Queue:
        """Create a new listener queue. Returns the queue."""
        q = asyncio.Queue(maxsize=256)
        self._listeners.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        """Remove a listener queue."""
        try:
            self._listeners.remove(q)
        except ValueError:
            pass


# Singleton
log_broadcaster = LogBroadcaster()
