"""Thread-safe event bus connecting worker threads to the Tkinter dashboard."""

import queue
import time
from dataclasses import dataclass, field


@dataclass
class Event:
    kind: str
    data: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


class EventBus:
    def __init__(self):
        self._queue = queue.Queue()

    def emit(self, kind, **data):
        self._queue.put(Event(kind, data))

    def log(self, level, message):
        self.emit("log", level=level, message=message)

    def info(self, message):
        self.log("info", message)

    def warn(self, message):
        self.log("warn", message)

    def error(self, message):
        self.log("error", message)

    def success(self, message):
        self.log("success", message)

    def drain(self):
        """Return all pending events without blocking. Called from the UI thread."""
        events = []
        while True:
            try:
                events.append(self._queue.get_nowait())
            except queue.Empty:
                return events
