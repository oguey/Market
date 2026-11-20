"""Streaming logic, tested with a real Spark streaming query on a folder of JSON files (no Kafka, no Delta)."""
import json
import os
import sys
import time

import pytest

pyspark = pytest.importorskip("pyspark")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lake", "spark"))

from pyspark.sql import SparkSession, functions as F  # noqa: E402

import stream_orders as so  # noqa: E402


@pytest.fixture(scope="module")
def spark():
    s = (SparkSession.builder.master("local[2]").appName("t").config("spark.sql.shuffle.partitions", "2")
         .config("spark.ui.enabled", "false").getOrCreate())
    yield s
    s.stop()


def ev(i, status, ts, order=None, cents=10000):
    return {"event_id": f"e{i}", "event_type": "order.status_changed", "order_id": order or i, "customer_id": 1,
            "status": status, "total_cents": cents, "occurred_at": ts}


def run(spark, tmp_path, events_by_file):
    src = tmp_path / "in"
    src.mkdir()
    for n, evs in enumerate(events_by_file):
        (src / f"f{n}.json").write_text("\n".join(json.dumps(e) for e in evs))
    raw = (spark.readStream.schema("event_id string, event_type string, order_id long, customer_id long, "
                                   "status string, total_cents long, occurred_at string").json(str(src)))
    # same path as Kafka after the first step: a "value" column of JSON bytes
    raw = raw.select(F.to_json(F.struct(*raw.columns)).alias("value"))
    out = []
    q = (so.window_stats(so.deduplicate(so.parse_events(raw))).writeStream.outputMode("update")
         .foreachBatch(lambda df, _: out.extend(r.asDict() for r in df.collect())).start())
    q.processAllAvailable()
    q.stop()
    latest = {}
    for r in out:  # update mode: keep the last value per (window, status)
        latest[(r["window_start"], r["status"])] = r
    return latest


def test_counts_per_window_and_status(spark, tmp_path):
    res = run(spark, tmp_path, [[ev(1, "paid", "2026-11-19T20:00:10Z"), ev(2, "paid", "2026-11-19T20:00:50Z"),
                                  ev(3, "payment_failed", "2026-11-19T20:00:55Z"),
                                  ev(4, "paid", "2026-11-19T20:01:05Z")]])
    by = {(k[0].minute, k[1]): v["orders"] for k, v in res.items()}
    assert by[(0, "paid")] == 2 and by[(0, "payment_failed")] == 1 and by[(1, "paid")] == 1


def test_duplicate_event_is_counted_once(spark, tmp_path):
    same = ev(1, "paid", "2026-11-19T20:00:10Z")
    res = run(spark, tmp_path, [[same, same], [same]])
    assert sum(v["orders"] for v in res.values()) == 1


def test_alert_when_threshold_reached(spark, tmp_path):
    fails = [ev(i, "payment_failed", f"2026-11-19T20:00:{10 + i}Z", cents=25000) for i in range(1, 4)]
    res = run(spark, tmp_path, [fails])
    stats = spark.createDataFrame([{**v} for v in res.values()])
    alerts = so.detect_alerts(stats, threshold=3).collect()
    assert len(alerts) == 1 and alerts[0]["failed_orders"] == 3 and alerts[0]["failed_amount_cents"] == 75000
    assert so.detect_alerts(stats, threshold=4).count() == 0


def test_garbage_message_is_dropped(spark):
    df = spark.createDataFrame([(b"not json",), (json.dumps(ev(1, "paid", "2026-11-19T20:00:10Z")).encode(),)], ["value"])
    assert so.parse_events(df).count() == 1


def test_kafka_lag_from_progress():
    class Src:
        endOffset = json.dumps({"market.orders": {"0": 10, "1": 5}})
        latestOffset = json.dumps({"market.orders": {"0": 14, "1": 5}})

    class P:
        sources = [Src()]

    assert so.kafka_lag(P()) == 4
