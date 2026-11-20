"""Business events on Kafka (best effort).

Publishing must never slow down or break an order: the call is non-blocking, any error is logged,
and nothing happens at all unless KAFKA_BOOTSTRAP_SERVERS is set (tests, local runs without Kafka).

Delivery is at-least-once with an idempotent producer (no duplicate from producer retries). The event_id is
deterministic (order id + status), so a consumer can drop the rare duplicate after a replay.
"""
import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone

log = logging.getLogger("events")
TOPIC = os.getenv("KAFKA_ORDERS_TOPIC", "market.orders")
_NAMESPACE = uuid.UUID("6f1c2f0e-8a0b-4c1f-9d57-3d0f5a3b7e11")

_producer = None
_lock = threading.Lock()


def _get_producer():
    global _producer
    if _producer is not None:
        return _producer
    servers = os.getenv("KAFKA_BOOTSTRAP_SERVERS")
    if not servers:
        return None
    with _lock:
        if _producer is None:
            try:
                from confluent_kafka import Producer
                _producer = Producer({
                    "bootstrap.servers": servers,
                    "enable.idempotence": True,   # no duplicate or reordering from producer retries
                    "acks": "all",
                    "linger.ms": 5,
                    "message.timeout.ms": 10000,  # give up after 10 s instead of piling up in memory
                    "client.id": "orders-svc",
                })
            except Exception as exc:  # missing library, bad config...
                log.warning("kafka disabled: %s", exc)
                return None
    return _producer


def set_producer(producer) -> None:
    """Inject a producer (tests)."""
    global _producer
    _producer = producer


def _on_delivery(err, msg) -> None:
    if err is not None:
        log.error("kafka delivery failed topic=%s key=%s: %s", msg.topic(), msg.key(), err)


def order_event(order_id: int, customer_id: int, status: str, total_cents: int) -> dict:
    return {
        "event_id": str(uuid.uuid5(_NAMESPACE, f"{order_id}:{status}")),
        "event_type": "order.status_changed",
        "order_id": order_id,
        "customer_id": customer_id,
        "status": status,  # pending | paid | payment_failed | cancelled | preparing | delivered
        "total_cents": total_cents,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    }


def publish_order_status(order_id: int, customer_id: int, status: str, total_cents: int) -> None:
    producer = _get_producer()
    if producer is None:
        return
    try:
        producer.produce(TOPIC, key=str(order_id), value=json.dumps(order_event(order_id, customer_id, status, total_cents)),
                         on_delivery=_on_delivery)
        producer.poll(0)  # serve delivery callbacks, never blocks
    except Exception as exc:  # queue full, broker down...
        log.error("kafka publish failed order=%s status=%s: %s", order_id, status, exc)


def flush(timeout: float = 3.0) -> None:
    if _producer is not None:
        try:
            _producer.flush(timeout)
        except Exception as exc:
            log.warning("kafka flush failed: %s", exc)
