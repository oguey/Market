"""Volet 4 - Temps reel : Kafka (market.orders) -> Spark Structured Streaming -> Delta + metriques.

Ce que fait le job :
  * lit les evenements de commande dans Kafka (offsets suivis dans le checkpoint) ;
  * retire les doublons par event_id (dropDuplicatesWithinWatermark) ;
  * compte les commandes par fenetre d'1 minute et par statut, avec un watermark de 2 minutes ;
  * leve une alerte "pic d'echecs de paiement" quand une fenetre contient au moins ALERT_THRESHOLD echecs ;
  * ecrit stats et alertes dans Delta (consumption/streaming/...) par ajout idempotent (txnAppId/txnVersion) : rejouer un micro-lot ne duplique rien ;
  * expose des metriques Prometheus sur le port METRICS_PORT : debit, retard (lag) Kafka, alertes.

Garantie : exactly-once "en effet". Kafka -> Spark est au moins une fois ; le checkpoint rejoue le dernier lot
apres une panne ; le dedoublonnage et l'ecriture idempotente rendent ce rejeu sans effet sur le resultat.
"""
import json
import os
import time

from pyspark.sql import functions as F
from pyspark.sql.types import LongType, StringType, StructField, StructType

from staging import build_spark

KAFKA_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")
TOPIC = os.getenv("KAFKA_ORDERS_TOPIC", "market.orders")
CONSUMPTION = os.getenv("CONSUMPTION_URI", "s3a://consumption")
CHECKPOINT = os.getenv("STREAM_CHECKPOINT", "/checkpoints/orders")
WINDOW = os.getenv("STREAM_WINDOW", "1 minute")
WATERMARK = os.getenv("STREAM_WATERMARK", "2 minutes")
TRIGGER = os.getenv("STREAM_TRIGGER", "10 seconds")
MAX_PER_TRIGGER = int(os.getenv("STREAM_MAX_OFFSETS_PER_TRIGGER", "1000"))
ALERT_THRESHOLD = int(os.getenv("ALERT_THRESHOLD", "3"))
METRICS_PORT = int(os.getenv("METRICS_PORT", "8008"))
ALERT_TTL_S = 90  # une alerte reste "active" dans Grafana 90 s apres le dernier echec de la fenetre

EVENT_SCHEMA = StructType([
    StructField("event_id", StringType()),
    StructField("event_type", StringType()),
    StructField("order_id", LongType()),
    StructField("customer_id", LongType()),
    StructField("status", StringType()),
    StructField("total_cents", LongType()),
    StructField("occurred_at", StringType()),
])


# ------------------------------------------------------------------ logique pure (testee sans Kafka)
def parse_events(raw):
    """Colonne Kafka `value` (octets JSON) -> colonnes typees. Un message illisible est ecarte."""
    parsed = raw.select(F.from_json(F.col("value").cast("string"), EVENT_SCHEMA).alias("e")).select("e.*")
    return (parsed.withColumn("occurred_at", F.to_timestamp("occurred_at"))
            .filter(F.col("event_id").isNotNull() & F.col("occurred_at").isNotNull() & F.col("status").isNotNull()))


def deduplicate(events):
    """Watermark (retard maximum accepte) + un evenement n'est compte qu'une fois, meme rejoue."""
    return (events.withWatermark("occurred_at", WATERMARK)
            .dropDuplicatesWithinWatermark(["event_id"]))


def window_stats(events):
    return (events.groupBy(F.window("occurred_at", WINDOW).alias("w"), "status")
            .agg(F.count("*").alias("orders"), F.sum("total_cents").alias("amount_cents"))
            .select(F.col("w.start").alias("window_start"), F.col("w.end").alias("window_end"),
                    "status", "orders", "amount_cents"))


def detect_alerts(stats, threshold=ALERT_THRESHOLD):
    return (stats.filter((F.col("status") == "payment_failed") & (F.col("orders") >= threshold))
            .select("window_start", "window_end",
                    F.lit("payment_failure_spike").alias("alert_type"),
                    F.col("orders").alias("failed_orders"),
                    F.col("amount_cents").alias("failed_amount_cents"),
                    F.lit(threshold).alias("threshold"))
            .withColumn("detected_at", F.current_timestamp()))


# ------------------------------------------------------------------ ecriture idempotente (Delta)
def append_idempotent(df, path, app_id, batch_id):
    """Ajout rapide (sans relire la table). Delta memorise le dernier `txnVersion` ecrit pour `txnAppId` :
    si Spark rejoue le meme micro-lot apres une panne, l'ecriture est ignoree -> jamais de doublon.
    Une fenetre mise a jour plusieurs fois donne plusieurs lignes : la plus recente (updated_at) fait foi."""
    (df.withColumn("batch_id", F.lit(batch_id)).withColumn("updated_at", F.current_timestamp())
       .write.format("delta").mode("append")
       .option("mergeSchema", "true")  # tolere l'ajout de colonnes (tables creees par une version precedente)
       .option("mergeSchema", "true").option("txnAppId", app_id).option("txnVersion", batch_id).save(path))


# ------------------------------------------------------------------ metriques Prometheus
class Metrics:
    def __init__(self):
        from prometheus_client import Counter, Gauge
        self.input_rate = Gauge("market_stream_input_rows_per_second", "Messages lus par seconde")
        self.processed_rate = Gauge("market_stream_processed_rows_per_second", "Messages traites par seconde")
        self.lag = Gauge("market_stream_kafka_lag_messages", "Messages Kafka disponibles mais pas encore traites")
        self.batch_ms = Gauge("market_stream_batch_duration_ms", "Duree du dernier micro-lot")
        self.batches = Counter("market_stream_batches_total", "Micro-lots traites")
        self.errors = Counter("market_stream_errors_total", "Erreurs de traitement")
        self.alerts = Counter("market_stream_alerts_total", "Alertes levees (pic d'echecs de paiement)")
        self.failed_window = Gauge("market_stream_payment_failures_window",
                                   "Echecs de paiement dans la fenetre la plus recente (0 si calme depuis 90 s)")
        self._failed, self._seen = 0, 0.0
        self.failed_window.set_function(lambda: self._failed if time.time() - self._seen < ALERT_TTL_S else 0)

    def failures(self, count: int):
        self._failed, self._seen = count, time.time()


def kafka_lag(progress) -> int:
    """Somme, sur les partitions, de (dernier offset Kafka - offset traite)."""
    try:
        src = progress.sources[0]
        end, latest = json.loads(src.endOffset), json.loads(src.latestOffset)
        return sum(int(latest[t][p]) - int(end[t].get(p, 0)) for t in latest for p in latest[t])
    except Exception:
        return 0


def make_listener(metrics):
    from pyspark.sql.streaming import StreamingQueryListener

    class Listener(StreamingQueryListener):
        def onQueryStarted(self, event):
            pass

        def onQueryProgress(self, event):
            p = event.progress
            metrics.input_rate.set(p.inputRowsPerSecond or 0)
            metrics.processed_rate.set(p.processedRowsPerSecond or 0)
            metrics.lag.set(kafka_lag(p))
            metrics.batch_ms.set((p.durationMs or {}).get("triggerExecution", 0))
            metrics.batches.inc()

        def onQueryTerminated(self, event):
            if event.exception:
                metrics.errors.inc()

    return Listener()


# ------------------------------------------------------------------ programme principal
def main():
    from prometheus_client import start_http_server

    spark = build_spark("market-stream-orders", extra_conf={
        "spark.sql.extensions": "io.delta.sql.DeltaSparkSessionExtension",
        "spark.sql.catalog.spark_catalog": "org.apache.spark.sql.delta.catalog.DeltaCatalog",
        "spark.sql.shuffle.partitions": "2",
        "spark.databricks.delta.snapshotPartitions": "2",
        "spark.databricks.delta.merge.repartitionBeforeWrite.enabled": "false",
    }, uris=[CONSUMPTION])
    spark.sparkContext.setLogLevel("WARN")

    metrics = Metrics()
    start_http_server(METRICS_PORT)
    spark.streams.addListener(make_listener(metrics))

    raw = (spark.readStream.format("kafka")
           .option("kafka.bootstrap.servers", KAFKA_SERVERS)
           .option("subscribe", TOPIC)
           .option("startingOffsets", "earliest")      # premier demarrage : tout l'historique ; ensuite : le checkpoint
           .option("maxOffsetsPerTrigger", MAX_PER_TRIGGER)
           .option("failOnDataLoss", "false")
           .load())
    stats = window_stats(deduplicate(parse_events(raw)))

    def write_batch(batch_df, batch_id):
        rows = batch_df.collect()  # une poignee de fenetres : on les lit une seule fois
        if not rows:
            return  # rien de nouveau : pas d'ecriture inutile sur le stockage
        t0 = time.time()
        try:
            # 1) Metriques d'abord : l'alerte apparait dans Grafana sans attendre les ecritures Delta
            failed = max((r["orders"] for r in rows if r["status"] == "payment_failed"), default=0)
            n_alerts = sum(1 for r in rows if r["status"] == "payment_failed" and r["orders"] >= ALERT_THRESHOLD)
            if failed:
                metrics.failures(int(failed))
            if n_alerts:
                metrics.alerts.inc(n_alerts)
            # 2) Ecritures idempotentes : un micro-lot rejoue apres une panne ne duplique rien
            append_idempotent(batch_df, f"{CONSUMPTION}/streaming/order_stats", "orders-stats", batch_id)
            if n_alerts:
                append_idempotent(detect_alerts(batch_df), f"{CONSUMPTION}/streaming/alerts", "orders-alerts", batch_id)
            print(f"batch {batch_id}: {len(rows)} fenetres mises a jour, {n_alerts} alerte(s), "
                  f"ecriture {time.time() - t0:.1f}s", flush=True)
        except Exception:
            metrics.errors.inc()
            raise

    query = (stats.writeStream.outputMode("update")
             .foreachBatch(write_batch)
             .option("checkpointLocation", CHECKPOINT)
             .trigger(processingTime=TRIGGER)
             .start())
    query.awaitTermination()


if __name__ == "__main__":
    main()
