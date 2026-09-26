"""A very small metrics registry: counters, gauges and histograms.

Muninn deliberately ships no metrics *library* - the whole surface is a handful
of numbers on a single-user service, and a dependency that has to be kept
current is a poor trade for that. What it needs is a stable, scrapeable
exposition, so this renders the Prometheus text format directly:

    # TYPE muninn_http_requests_total counter
    muninn_http_requests_total{endpoint="/search",status="200"} 12

Gauges and histograms are supported because the useful numbers here are
*latencies* (how long a search waited in the queue, how long a fast-path fetch
or a browser render took), and an average hides the tail that actually hurts.
Fixed buckets are deliberate: they are the ones an operator can read without a
histogram_quantile() query, and they are also the only ones worth a dashboard
for a service with one user.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterable

# Latency buckets in seconds. Chosen around the real costs: a cache hit is
# ~0s, a fast-path fetch ~0.1-2s, a browser render 1-45s, a queued search up to
# REQUEST_TIMEOUT_SECONDS.
DEFAULT_BUCKETS: tuple[float, ...] = (
    0.005, 0.025, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0,
)

_LabelKey = tuple[tuple[str, str], ...]


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _format_labels(labels: _LabelKey) -> str:
    if not labels:
        return ""
    inner = ",".join(f'{k}="{_escape(v)}"' for k, v in labels)
    return "{" + inner + "}"


class Registry:
    """Thread-safe counters, gauges and histograms with a text exposition."""

    def __init__(self, buckets: Iterable[float] = DEFAULT_BUCKETS) -> None:
        self._buckets = tuple(sorted(buckets))
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, _LabelKey], float] = {}
        self._gauges: dict[tuple[str, _LabelKey], float] = {}
        self._histograms: dict[tuple[str, _LabelKey], list[float]] = {}
        self._help: dict[str, tuple[str, str]] = {}

    # -- declaration ---------------------------------------------------------

    def describe(self, name: str, kind: str, help_text: str) -> None:
        with self._lock:
            self._help[name] = (kind, help_text)

    # -- recording -----------------------------------------------------------

    def increment(self, name: str, labels: dict[str, str] | None = None, amount: float = 1.0) -> None:
        key = (name, tuple(sorted((labels or {}).items())))
        with self._lock:
            self._counters[key] = self._counters.get(key, 0.0) + amount

    def set_gauge(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        key = (name, tuple(sorted((labels or {}).items())))
        with self._lock:
            self._gauges[key] = float(value)

    def observe(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        """Record one observation: bucket counts, sum, count and max are kept."""
        key = (name, tuple(sorted((labels or {}).items())))
        with self._lock:
            hist = self._histograms.get(key)
            if hist is None:
                # [count, sum, max, <cumulative bucket counts...>]
                hist = [0.0, 0.0, float("-inf"), *([0.0] * len(self._buckets))]
                self._histograms[key] = hist
            hist[0] += 1                       # count
            hist[1] += value                    # sum
            if value > hist[2]:                 # max
                hist[2] = value
            # Stored per-bucket; made cumulative at render time, because the
            # Prometheus text format requires each bucket to count every
            # observation <= that edge, not only the ones that landed in it.
            for i, edge in enumerate(self._buckets):
                if value <= edge:
                    hist[3 + i] += 1
                    break

    # -- reading -------------------------------------------------------------

    def counter_value(self, name: str, labels: dict[str, str] | None = None) -> float:
        key = (name, tuple(sorted((labels or {}).items())))
        with self._lock:
            return self._counters.get(key, 0.0)

    def snapshot(self) -> dict[str, object]:
        """A JSON-friendly view, for ``/status`` and friends."""
        with self._lock:
            counters: dict[str, float] = {}
            for (name, labels), value in self._counters.items():
                counters[_series_name(name, labels)] = value
            gauges = {
                _series_name(name, labels): value
                for (name, labels), value in self._gauges.items()
            }
            histograms: dict[str, dict[str, object]] = {}
            for (name, labels), hist in self._histograms.items():
                count, total, maximum = hist[0], hist[1], hist[2]
                cumulative = _cumulative(hist[3:])
                histograms[_series_name(name, labels)] = {
                    "count": count,
                    "sum_seconds": round(total, 6),
                    "max_seconds": None if maximum == float("-inf") else maximum,
                    "avg_seconds": round(total / count, 6) if count else None,
                    "buckets": {
                        f"le_{edge}": cumulative[i]
                        for i, edge in enumerate(self._buckets)
                    },
                }
            return {"counters": counters, "gauges": gauges, "histograms": histograms}

    def render_text(self) -> str:
        """Prometheus text exposition (version 0.0.4)."""
        lines: list[str] = []
        with self._lock:
            counters = dict(self._counters)
            gauges = dict(self._gauges)
            histograms = {k: (list(v), list(self._buckets)) for k, v in self._histograms.items()}
            help_text = dict(self._help)

        for kind, series in (("counter", counters), ("gauge", gauges)):
            grouped: dict[str, list[tuple[_LabelKey, float]]] = {}
            for (name, labels), value in series.items():
                grouped.setdefault(name, []).append((labels, value))
            for name, entries in sorted(grouped.items()):
                declared_kind, help_line = help_text.get(name, (kind, name))
                lines.append(f"# HELP {name} {help_line}")
                lines.append(f"# TYPE {name} {declared_kind}")
                for labels, value in sorted(entries):
                    lines.append(f"{name}{_format_labels(labels)} {_num(value)}")

        by_histogram: dict[str, list[tuple[_LabelKey, list[float], list[float]]]] = {}
        for (name, labels), (hist, buckets) in histograms.items():
            by_histogram.setdefault(name, []).append((labels, hist, buckets))
        for hist_name, hist_entries in sorted(by_histogram.items()):
            _, help_line = help_text.get(hist_name, ("histogram", hist_name))
            lines.append(f"# HELP {hist_name} {help_line}")
            lines.append(f"# TYPE {hist_name} histogram")
            for labels, hist, buckets in sorted(hist_entries):
                count, total = hist[0], hist[1]
                cumulative = _cumulative(hist[3:])
                for i, edge in enumerate(buckets):
                    labels_with_le = labels + (("le", _num(edge)),)
                    lines.append(
                        f"{name}_bucket{_format_labels(labels_with_le)} "
                        f"{_num(cumulative[i])}"
                    )
                lines.append(
                    f"{hist_name}_bucket"
                    f"{_format_labels(labels + (('le', '+Inf'),))} {_num(count)}"
                )
                base = _format_labels(labels)
                lines.append(f"{hist_name}_sum{base} {_num(total)}")
                lines.append(f"{hist_name}_count{base} {_num(count)}")
        return "\n".join(lines) + "\n"


def _cumulative(per_bucket: list[float]) -> list[float]:
    """Turn per-bucket counts into the cumulative form Prometheus requires."""
    running = 0.0
    out: list[float] = []
    for value in per_bucket:
        running += value
        out.append(running)
    return out


def _series_name(name: str, labels: _LabelKey) -> str:
    if not labels:
        return name
    inner = ",".join(f"{k}={v}" for k, v in labels)
    return f"{name}{{{inner}}}"


def _num(value: float) -> str:
    if value == float("inf"):
        return "+Inf"
    if value == float("-inf"):
        return "-Inf"
    if isinstance(value, float) and not value.is_integer():
        return f"{value:.6g}"
    return str(int(value))


class Timer:
    """Context manager that records its own wall-clock duration.

    ``with Timer(registry, "muninn_scrape_leg_seconds", {"leg": "fast_path"}):``
    """

    __slots__ = ("_registry", "_name", "_labels", "_start")

    def __init__(
        self,
        registry: Registry,
        name: str,
        labels: dict[str, str] | None = None,
    ) -> None:
        self._registry = registry
        self._name = name
        self._labels = labels or {}
        self._start = 0.0

    def __enter__(self) -> Timer:
        self._start = time.perf_counter()
        return self

    def __exit__(self, *exc: object) -> None:
        self._registry.observe(self._name, time.perf_counter() - self._start, self._labels)
