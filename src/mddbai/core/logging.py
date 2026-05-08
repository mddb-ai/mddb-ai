from __future__ import annotations

"""Structured logger.

Outputs JSON Lines. A call like ``logger.info("event_name", key=value, ...)``
emits one ``{"ts": ..., "level": "info", "event": "event_name", ...}`` line
to stderr. Level is controlled by the ``MDDB_LOG_LEVEL`` environment variable.
"""

import json
import logging
import os
import sys
import time
from typing import Any, TextIO

_LEVELS: dict[str, int] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warn": logging.WARNING,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}


def _resolve_level() -> int:
    raw = os.environ.get("MDDB_LOG_LEVEL", "info").lower()
    return _LEVELS.get(raw, logging.INFO)


class StructuredLogger:
    """Take an event name + key/value pairs and serialise as JSON Lines."""

    __slots__ = ("name", "_level", "_stream")

    def __init__(self, name: str, *, stream: TextIO | None = None, level: int | None = None) -> None:
        self.name = name
        self._level = level if level is not None else _resolve_level()
        self._stream = stream if stream is not None else sys.stderr

    # --- public level helpers ---------------------------------------------

    def debug(self, event: str, **fields: Any) -> None:
        self._emit(logging.DEBUG, "debug", event, fields)

    def info(self, event: str, **fields: Any) -> None:
        self._emit(logging.INFO, "info", event, fields)

    def warn(self, event: str, **fields: Any) -> None:
        self._emit(logging.WARNING, "warn", event, fields)

    warning = warn

    def error(self, event: str, **fields: Any) -> None:
        self._emit(logging.ERROR, "error", event, fields)

    def critical(self, event: str, **fields: Any) -> None:
        self._emit(logging.CRITICAL, "critical", event, fields)

    # --- introspection -----------------------------------------------------

    @property
    def level(self) -> int:
        return self._level

    def set_level(self, level: int) -> None:
        self._level = level

    def is_enabled_for(self, level: int) -> bool:
        return level >= self._level

    # --- internals ---------------------------------------------------------

    def _emit(self, level_int: int, level_name: str, event: str, fields: dict[str, Any]) -> None:
        if level_int < self._level:
            return
        payload: dict[str, Any] = {
            "ts": time.time_ns(),
            "level": level_name,
            "logger": self.name,
            "event": event,
        }
        for key, value in fields.items():
            payload[key] = _safe_jsonable(value)
        line = json.dumps(payload, ensure_ascii=False, sort_keys=False, default=str)
        self._stream.write(line + "\n")
        self._stream.flush()


def _safe_jsonable(value: Any) -> Any:
    try:
        json.dumps(value, default=str)
    except (TypeError, ValueError):
        return repr(value)
    return value


def get_logger(name: str, *, stream: TextIO | None = None) -> StructuredLogger:
    """Return a structured logger with the given name."""

    return StructuredLogger(name, stream=stream)


__all__ = ["StructuredLogger", "get_logger"]
