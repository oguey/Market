"""Volet 2 - RAW -> STAGING (nettoyage) avec Spark.

Pour chaque table de `raw` :
  1. lit toutes les extractions (dossiers dt=...) ;
  2. garde, pour chaque cle primaire, la ligne de l'extraction la plus recente (dedoublonnage) ;
  3. normalise les types et les valeurs ;
  4. ecarte les lignes invalides vers `staging/_rejected/<table>` (jamais supprimees en silence) ;
  5. ecrit le resultat propre en Parquet dans `staging/<table>`.
Un rapport (lignes lues / gardees / rejetees) est affiche et ecrit dans `staging/_reports`.
"""
import os
from datetime import datetime, timezone

from pyspark.sql import SparkSession, Window
from pyspark.sql import functions as F

RAW = os.getenv("RAW_URI", "s3a://raw")
STAGING = os.getenv("STAGING_URI", "s3a://staging")
STATUSES = ["pending", "paid", "payment_failed", "cancelled", "preparing", "delivered"]
EMAIL_RE = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


def build_spark():
    builder = SparkSession.builder.appName("market-raw-to-staging")
    if RAW.startswith("s3a://") or STAGING.startswith("s3a://"):
        builder = (
            builder.config("spark.hadoop.fs.s3a.endpoint", os.getenv("S3_ENDPOINT", "http://rustfs:9000"))
            .config("spark.hadoop.fs.s3a.access.key", os.environ["S3_ACCESS_KEY"])
            .config("spark.hadoop.fs.s3a.secret.key", os.environ["S3_SECRET_KEY"])
            .config("spark.hadoop.fs.s3a.path.style.access", "true")
            .config("spark.hadoop.fs.s3a.connection.ssl.enabled", "false")
            .config("spark.hadoop.fs.s3a.impl", "org.apache.hadoop.fs.s3a.S3AFileSystem")
        )
    return builder.getOrCreate()


# ------------------------------------------------------------------ regles par table
def money(df, cents_col, euros_col):
    return df.withColumn(euros_col, F.round(F.col(cents_col) / 100.0, 2))


def t_customers(df):
    df = df.withColumn("name", F.trim("name")).withColumn("email", F.lower(F.trim("email")))
    return df, F.col("email").rlike(EMAIL_RE) & F.col("name").isNotNull() & (F.col("name") != "")


def t_orders(df):
    df = df.withColumn("status", F.lower(F.trim("status")))
    df = money(df, "total_cents", "total_euros")
    return df, F.col("status").isin(STATUSES) & (F.col("total_cents") >= 0) & F.col("customer_id").isNotNull()


def t_order_items(df):
    df = df.withColumn("line_total_cents", F.col("quantity") * F.col("unit_price_cents"))
    df = money(df, "line_total_cents", "line_total_euros")
    return df, (F.col("quantity") > 0) & (F.col("unit_price_cents") >= 0)


def t_warehouses(df):
    return df.withColumn("name", F.trim("name")), (F.col("capacity_units") >= 0)


def t_stock(df):
    return df, (F.col("quantity") >= 0)


def t_reservations(df):
    return df.withColumn("status", F.lower("status")), F.col("status").isin("reserved", "released", "confirmed")


def t_reservation_items(df):
    return df, (F.col("quantity") > 0)


def t_payments(df):
    df = df.withColumn("status", F.lower(F.trim("status")))
    df = money(df, "amount_cents", "amount_euros")
    return df, (F.col("amount_cents") >= 0) & F.col("order_id").isNotNull()


def t_catalog(df):
    df = (df.withColumn("name", F.trim("name")).withColumn("category", F.lower(F.trim("category"))))
    df = money(df, "price_cents", "price_euros")
    return df, F.col("name").isNotNull() & (F.col("name") != "") & (F.col("price_cents") >= 0)


# (dossier dans raw, nom dans staging, cle primaire, regle)
TABLES = [
    ("postgres/orders_customers", "customers", ["id"], t_customers),
    ("postgres/orders_orders", "orders", ["id"], t_orders),
    ("postgres/orders_order_items", "order_items", ["id"], t_order_items),
    ("postgres/inventory_warehouses", "warehouses", ["id"], t_warehouses),
    ("postgres/inventory_stock", "stock", ["product_id", "warehouse_id"], t_stock),
    ("postgres/inventory_reservations", "reservations", ["order_id"], t_reservations),
    ("postgres/inventory_reservation_items", "reservation_items", ["order_id", "product_id", "warehouse_id"], t_reservation_items),
    ("postgres/payments_payments", "payments", ["id"], t_payments),
    ("mongo/catalog", "products", ["product_id"], t_catalog),
]


def read_raw(spark, path):
    try:
        return spark.read.option("recursiveFileLookup", "false").parquet(f"{RAW}/{path}")
    except Exception as exc:  # table jamais extraite (ex. aucune reservation)
        if "PATH_NOT_FOUND" in str(exc) or "Path does not exist" in str(exc) or "UNABLE_TO_INFER_SCHEMA" in str(exc):
            return None
        raise


def latest_per_key(df, pk):
    window = Window.partitionBy(*pk).orderBy(F.col("_extracted_at").desc())
    return df.withColumn("_rn", F.row_number().over(window)).filter("_rn = 1").drop("_rn")


def main():
    spark = build_spark()
    spark.sparkContext.setLogLevel("WARN")
    run_at = datetime.now(timezone.utc)
    report = []
    for raw_path, name, pk, rule in TABLES:
        df = read_raw(spark, raw_path)
        if df is None:
            report.append((name, 0, 0, 0, 0))
            print(f"  {name}: rien dans raw, ignore")
            continue
        read_n = df.count()
        deduped = latest_per_key(df, pk)
        deduped_n = deduped.count()
        cleaned, is_valid = rule(deduped)
        # une condition NULL (donnee manquante) compte comme invalide
        flag = F.coalesce(is_valid, F.lit(False))
        good = cleaned.filter(flag).drop("dt")
        bad = cleaned.filter(~flag).drop("dt")
        good.write.mode("overwrite").parquet(f"{STAGING}/{name}")
        good_n = good.count()
        bad_n = deduped_n - good_n
        if bad_n:
            bad.write.mode("overwrite").parquet(f"{STAGING}/_rejected/{name}")
        report.append((name, read_n, deduped_n, good_n, bad_n))
        print(f"  {name}: {read_n} lues, {deduped_n} apres dedoublonnage, {good_n} gardees, {bad_n} rejetees")
    rep = spark.createDataFrame(
        [(run_at.isoformat(),) + r for r in report],
        "run_at string, table string, rows_read long, rows_deduplicated long, rows_kept long, rows_rejected long",
    )
    rep.coalesce(1).write.mode("append").json(f"{STAGING}/_reports")
    print("Rapport :")
    rep.drop("run_at").show(truncate=False)
    spark.stop()


if __name__ == "__main__":
    main()
