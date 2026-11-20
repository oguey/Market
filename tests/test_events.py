"""Kafka events: published after the DB write, never blocking, off unless configured."""
import json

from common import events


class FakeProducer:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def produce(self, topic, key, value, on_delivery=None):
        if self.fail:
            raise BufferError("queue full")
        self.sent.append((topic, key, json.loads(value)))

    def poll(self, _):
        pass

    def flush(self, _):
        pass


def test_event_payload_and_key():
    p = FakeProducer()
    events.set_producer(p)
    events.publish_order_status(42, 7, "payment_failed", 21990)
    topic, key, ev = p.sent[0]
    assert topic == "market.orders" and key == "42"
    assert ev["status"] == "payment_failed" and ev["total_cents"] == 21990 and ev["customer_id"] == 7
    assert ev["event_type"] == "order.status_changed" and "occurred_at" in ev
    events.set_producer(None)


def test_event_id_is_deterministic():
    assert events.order_event(1, 1, "paid", 100)["event_id"] == events.order_event(1, 1, "paid", 100)["event_id"]
    assert events.order_event(1, 1, "paid", 100)["event_id"] != events.order_event(1, 1, "pending", 100)["event_id"]


def test_broker_problem_never_raises():
    events.set_producer(FakeProducer(fail=True))
    events.publish_order_status(1, 1, "paid", 100)  # must not raise
    events.set_producer(None)


def test_noop_without_kafka_configured(monkeypatch):
    monkeypatch.delenv("KAFKA_BOOTSTRAP_SERVERS", raising=False)
    events.set_producer(None)
    events.publish_order_status(1, 1, "paid", 100)  # no producer, no error
