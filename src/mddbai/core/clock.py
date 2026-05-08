from __future__ import annotations

"""Clock abstraction.

Provides ``FakeClock`` for advancing time arbitrarily in tests and
``SystemClock`` for production use against the system clock. ``Clock`` is
a Protocol so other implementations can be swapped in.
"""

import threading
import time
from typing import Protocol, runtime_checkable

from .types import Timestamp


@runtime_checkable
class Clock(Protocol):
    """Time source."""

    def now_ns(self) -> Timestamp:
        """Current wall-clock nanoseconds."""

    def monotonic_ns(self) -> int:
        """Monotonic nanoseconds (for benchmarks / timeout measurement)."""


class SystemClock:
    """Clock backed by the OS clock."""

    def now_ns(self) -> Timestamp:
        return Timestamp(time.time_ns())

    def monotonic_ns(self) -> int:
        return time.monotonic_ns()


class FakeClock:
    """Test clock. Manipulate time via ``advance(ns)`` or ``set(ns)``."""

    def __init__(self, *, start_ns: int = 0) -> None:
        self._lock = threading.Lock()
        self._wall_ns: int = int(start_ns)
        self._mono_ns: int = 0

    def now_ns(self) -> Timestamp:
        with self._lock:
            return Timestamp(self._wall_ns)

    def monotonic_ns(self) -> int:
        with self._lock:
            return self._mono_ns

    def advance(self, ns: int) -> None:
        if ns < 0:
            raise ValueError("FakeClock cannot move backwards via advance")
        with self._lock:
            self._wall_ns += ns
            self._mono_ns += ns

    def set(self, wall_ns: int) -> None:
        """Force the wall clock (allowed to move backwards). Monotonic is unaffected."""

        with self._lock:
            self._wall_ns = int(wall_ns)


__all__ = ["Clock", "FakeClock", "SystemClock"]
