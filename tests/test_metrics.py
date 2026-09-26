"""Metrics registry, exposition format, and structured logging.

Deliberately dependency-free (REPORT2's "no metrics library" decision), so the
format has to be right by hand: counters and gauges render as Prometheus text,
histograms render the full bucket/sum/count triple set, and the log formatter
emits one parseable JSON object per line.
"""

from __future__ import annotations

import json
import logging

import pytest

from ops.logging import JsonFormatter
from ops.metrics import DEFAULT_BUCKETS, Registry

# --------------------------------------------------------------------------- registry


def test_counter_renders_prometheus_text() -> None:
    reg = Registry()
    reg.describe("muninn_test_total", "counter", "A test counter")
    reg.increment("muninn_test_total", {"outcome": "ok"})
    reg.increment("muninn_test_total", {"outcome": "ok"})
    reg.increment("muninn_test_total", {"outcome": "error"})

    text = reg.render_text()
    assert "# HELP muninn_test_total A test counter" in text
    assert "# TYPE muninn_test_total counter" in text
    assert 'muninn_test_total{outcome="ok"} 2' in text
    assert 'muninn_test_total{outcome="error"} 1' in text


def test_label_values_are_escaped() -> None:
    reg = Registry()
    reg.increment("x", {"path": 'a"b\\c'})
    assert r'path="a\"b\\c"' in reg.render_text()


def test_gauge_renders_and_is_replaced_not_accumulated() -> None:
    reg = Registry()
    reg.describe("muninn_g", "gauge", "A gauge")
    reg.set_gauge("muninn_g", 5)
    reg.set_gauge("muninn_g", 2)
    assert "muninn_g 2" in reg.render_text()


def test_histogram_renders_buckets_sum_and_count() -> None:
    reg = Registry()
    reg.describe("muninn_h_seconds", "histogram", "A histogram")
    reg.observe("muninn_h_seconds", 0.02, {"leg": "fast"})
    reg.observe("muninn_h_seconds", 3.0, {"leg": "fast"})

    text = reg.render_text()
    assert "# TYPE muninn_h_seconds histogram" in text
    # Cumulative bucket counts: 0.02 falls in the <=0.025 bucket, 3.0 in <=5.0.
    assert 'muninn_h_seconds_bucket{leg="fast",le="0.025"} 1' in text
    assert 'muninn_h_seconds_bucket{leg="fast",le="5"} 2' in text
    assert 'muninn_h_seconds_bucket{leg="fast",le="+Inf"} 2' in text
    assert 'muninn_h_seconds_count{leg="fast"} 2' in text
    assert "muninn_h_seconds_sum" in text


def test_histogram_observation_larger_than_every_bucket_lands_in_inf() -> None:
    reg = Registry()
    reg.observe("h", 10_000.0)
    text = reg.render_text()
    biggest = _num(DEFAULT_BUCKETS[-1])
    # Nothing fits a real bucket, so every one stays 0 and +Inf catches it.
    assert f'h_bucket{{le="{biggest}"}} 0' in text
    assert 'h_bucket{le="+Inf"} 1' in text


def test_buckets_are_cumulative() -> None:
    """Prometheus requires each bucket to count every observation <= its edge.

    Storing per-bucket counts and rendering them raw would make
    histogram_quantile() return nonsense, so this is asserted explicitly.
    """
    reg = Registry()
    for value in (0.001, 0.3, 7.0):
        reg.observe("h", value)
    counts = []
    for line in reg.render_text().splitlines():
        if not line.startswith("h_bucket"):
            continue
        edge = line.split('le="')[1].split('"')[0]
        counts.append((float(edge), float(line.split()[-1])))
    finite = [c for e, c in counts if e != float("inf")]
    assert finite == sorted(finite), f"bucket counts are not monotonic: {finite}"
    assert finite[-1] == 3, "the largest bucket must count every observation"


def _num(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.6g}"


def test_snapshot_is_json_friendly_and_computes_an_average() -> None:
    reg = Registry()
    reg.observe("h", 1.0)
    reg.observe("h", 3.0)
    snap = reg.snapshot()
    hist = snap["histograms"]["h"]  # type: ignore[index]
    assert hist["count"] == 2
    assert hist["sum_seconds"] == 4.0
    assert hist["avg_seconds"] == 2.0
    assert hist["max_seconds"] == 3.0
    json.dumps(snap)  # must be serialisable


def test_empty_registry_renders_empty_text() -> None:
    assert Registry().render_text() == "\n"


def test_registry_is_safe_to_share_across_threads() -> None:
    import threading

    reg = Registry()
    def worker() -> None:
        for _ in range(200):
            reg.increment("c")
            reg.observe("h", 0.01)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert reg.counter_value("c") == 800
    assert reg.snapshot()["histograms"]["h"]["count"] == 800  # type: ignore[index]


# --------------------------------------------------------------------------- logging


def test_json_formatter_emits_one_object_per_line() -> None:
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="muninn.test", level=logging.INFO, pathname=__file__, lineno=1,
        msg="query=%r served", args=("python",), exc_info=None,
    )
    record.request_id = "abc123"
    payload = json.loads(formatter.format(record))

    assert payload["message"] == "query='python' served"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "muninn.test"
    assert payload["request_id"] == "abc123"
    assert "ts" in payload


def test_json_formatter_keeps_the_traceback_out_of_the_message() -> None:
    formatter = JsonFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = logging.LogRecord(
            name="muninn", level=logging.ERROR, pathname=__file__, lineno=1,
            msg="failed", args=(), exc_info=sys.exc_info(),
        )
    payload = json.loads(formatter.format(record))
    assert payload["message"] == "failed"
    assert "ValueError: boom" in payload["exception"]


def test_json_formatter_survives_unserialisable_extras() -> None:
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="muninn", level=logging.INFO, pathname=__file__, lineno=1,
        msg="m", args=(), exc_info=None,
    )
    record.thing = object()
    assert json.loads(formatter.format(record))["thing"]


@pytest.mark.parametrize("value", [0, 0.5, -1, 1_000_000, 0.0001])
def test_number_formatting_is_valid_prometheus(value: float) -> None:
    reg = Registry()
    reg.set_gauge("g", value)
    line = next(x for x in reg.render_text().splitlines() if x.startswith("g "))
    float(line.split()[1])  # must parse
