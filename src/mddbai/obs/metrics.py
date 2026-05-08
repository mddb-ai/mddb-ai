from __future__ import annotations

"""Tiny in-memory metrics. Exported in the Prometheus text format."""

import threading
import time
from collections import defaultdict
from collections.abc import Iterator


class Counter:
    __slots__ = ("name", "help", "_value", "_lock")

    def __init__(self, name: str, help: str = "") -> None:  # noqa: A002 - prom convention
        self.name = name
        self.help = help
        self._value = 0
        self._lock = threading.Lock()

    def inc(self, n: int = 1) -> None:
        with self._lock:
            self._value += n

    def value(self) -> int:
        return self._value


class Histogram:
    """Bucket counts plus a running sum."""

    __slots__ = ("name", "help", "_buckets", "_counts", "_sum", "_count", "_lock")

    def __init__(self, name: str, *, buckets: tuple[float, ...], help: str = "") -> None:  # noqa: A002
        self.name = name
        self.help = help
        self._buckets = buckets
        self._counts = [0] * (len(buckets) + 1)
        self._sum = 0.0
        self._count = 0
        self._lock = threading.Lock()

    def observe(self, value: float) -> None:
        with self._lock:
            self._sum += value
            self._count += 1
            for i, b in enumerate(self._buckets):
                if value <= b:
                    self._counts[i] += 1
                    return
            self._counts[-1] += 1

    def snapshot(self) -> tuple[list[int], float, int]:
        with self._lock:
            return list(self._counts), self._sum, self._count


class Registry:
    """Registry of every metric."""

    def __init__(self) -> None:
        self._counters: dict[str, Counter] = {}
        self._histograms: dict[str, Histogram] = {}
        self._lock = threading.Lock()

    def counter(self, name: str, help: str = "") -> Counter:  # noqa: A002
        with self._lock:
            if name not in self._counters:
                self._counters[name] = Counter(name, help=help)
            return self._counters[name]

    def histogram(self, name: str, *, buckets: tuple[float, ...], help: str = "") -> Histogram:  # noqa: A002
        with self._lock:
            if name not in self._histograms:
                self._histograms[name] = Histogram(name, buckets=buckets, help=help)
            return self._histograms[name]

    def render_prometheus(self) -> str:
        lines: list[str] = []
        for c in self._counters.values():
            if c.help:
                lines.append(f"# HELP {c.name} {c.help}")
            lines.append(f"# TYPE {c.name} counter")
            lines.append(f"{c.name} {c.value()}")
        for h in self._histograms.values():
            counts, total, count = h.snapshot()
            if h.help:
                lines.append(f"# HELP {h.name} {h.help}")
            lines.append(f"# TYPE {h.name} histogram")
            cumulative = 0
            for i, b in enumerate(h._buckets):
                cumulative += counts[i]
                lines.append(f'{h.name}_bucket{{le="{b}"}} {cumulative}')
            cumulative += counts[-1]
            lines.append(f'{h.name}_bucket{{le="+Inf"}} {cumulative}')
            lines.append(f"{h.name}_sum {total}")
            lines.append(f"{h.name}_count {count}")
        return "\n".join(lines) + "\n"


REGISTRY = Registry()


class Timer:
    """Context manager that records elapsed seconds into a Histogram."""

    __slots__ = ("_h", "_start")

    def __init__(self, h: Histogram) -> None:
        self._h = h
        self._start = 0.0

    def __enter__(self) -> Timer:
        self._start = time.perf_counter()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._h.observe(time.perf_counter() - self._start)


__all__ = ["Counter", "Histogram", "REGISTRY", "Registry", "Timer"]
