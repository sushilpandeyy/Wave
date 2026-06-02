"""Structured logging + request tracing.

JSON logs to stdout, written off the event loop via a QueueHandler/QueueListener so a log
call is just an in-memory enqueue. A correlation id (the message_id) and a context dict
(tier, user, op, pool…) live in contextvars and are stamped onto every record automatically,
so one turn is traceable across the api and worker by its corr_id. `timed()` measures a block
and auto-logs a WARNING when it runs slow.
"""

import contextvars
import json
import logging
import sys
import time
from contextlib import asynccontextmanager
from logging.handlers import QueueHandler, QueueListener
from queue import SimpleQueue

from app.config import settings

_corr_id: contextvars.ContextVar[str] = contextvars.ContextVar("corr_id", default="-")
_context: contextvars.ContextVar[dict] = contextvars.ContextVar("context", default={})


def set_corr(corr_id: str) -> None:
    _corr_id.set(corr_id)


def bind(**fields) -> None:
    """Merge fields into the per-task logging context (tier, user_id, op, pool…)."""
    _context.set({**_context.get(), **fields})


def clear_context() -> None:
    _corr_id.set("-")
    _context.set({})


class _ContextFilter(logging.Filter):
    """Stamp corr_id + context onto the record in the *calling* thread/task."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.corr_id = _corr_id.get()
        record.context = _context.get()
        return True


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        obj = {
            "ts": round(record.created, 3),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "corr_id": getattr(record, "corr_id", "-"),
            **getattr(record, "context", {}),
            **getattr(record, "data", {}),
        }
        if record.exc_info:
            obj["exc"] = self.formatException(record.exc_info)
        return json.dumps(obj, default=str)


_log_queue: SimpleQueue = SimpleQueue()
_listener: QueueListener | None = None
_logger = logging.getLogger("wave")


def start_logging() -> None:
    """Wire non-blocking JSON logging. Idempotent."""
    global _listener
    if _listener is not None:
        return
    qh = QueueHandler(_log_queue)
    qh.addFilter(_ContextFilter())  # stamp context at call time, before enqueue
    _logger.handlers = [qh]
    _logger.setLevel(settings.log_level)
    _logger.propagate = False

    sink = logging.StreamHandler(sys.stdout)
    sink.setFormatter(_JsonFormatter())
    _listener = QueueListener(_log_queue, sink, respect_handler_level=True)
    _listener.start()


def stop_logging() -> None:
    """Flush + stop the background log writer (graceful shutdown)."""
    global _listener
    if _listener is not None:
        _listener.stop()  # drains the queue before returning
        _listener = None


def log(level: int, msg: str, **fields) -> None:
    _logger.log(level, msg, extra={"data": fields})


def info(msg: str, **fields) -> None:
    _logger.log(logging.INFO, msg, extra={"data": fields})


def warning(msg: str, **fields) -> None:
    _logger.log(logging.WARNING, msg, extra={"data": fields})


def exception(msg: str, **fields) -> None:
    _logger.error(msg, exc_info=True, extra={"data": fields})


@asynccontextmanager
async def timed(op: str, *, threshold_ms: float | None = None, **fields):
    """Time a block; record a timing analytics event and WARN if it runs slow."""
    t0 = time.perf_counter()
    try:
        yield
    finally:
        ms = (time.perf_counter() - t0) * 1000.0
        from app.analytics import track  # lazy import avoids a cycle

        track("op", op=op, ms=round(ms, 2), **fields)
        if ms > (settings.slow_op_ms if threshold_ms is None else threshold_ms):
            warning("slow_op", op=op, ms=round(ms, 2), **fields)
