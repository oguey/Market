"""Volet 2 - STAGING -> CURATED : pseudonymisation, controles qualite, tables Delta.

1. Lit les tables propres de `staging`.
2. Pseudonymise : l'e-mail est remplace par un hash SHA-256 sale (`email_hash`) ; le nom complet est retire.
   On garde le domaine (`email_domain`) pour les analyses. Le sel vient de PSEUDO_SALT (secret, dans .env).
3. Controles qualite : cles uniques, references valides entre tables, coherence des montants.
   Un controle de gravite "error" en echec ARRETE le traitement : rien n'est ecrit dans curated.
   Un controle de gravite "warn" est seulement signale.
4. Ecrit chaque table en Delta dans `curated/<table>` et le rapport qualite dans `curated/_quality`.
"""
import os
import sys
from datetime import datetime, timezone

from pyspark.sql import functions as F

from staging import build_spark

STAGING = os.getenv("STAGING_URI", "s3a://staging")
CURATED = os.getenv("CURATED_URI", "s3a://curated")
FORMAT = os.getenv("CURATED_FORMAT", "delta")  # "parquet" seulement pour les tests sans Delta
TABLES = ["customers", "orders", "order_items", "products", "stock", "warehouses",
          "payments", "reservations", "reservation_items"]


def delta_conf():
    if FORMAT != "delta":
        return {}
    return {
        "spark.sql.extensions": "io.delta.sql.DeltaSparkSessionExtension",
        "spark.sql.catalog.spark_catalog": "org.apache.spark.sql.delta.catalog.DeltaCatalog",
    }


def load(spark):
    data = {}
    for name in TABLES:
        try:
            data[name] = spark.read.parquet(f"{STAGING}/{name}")
        except Exception as exc:
            if "PATH_NOT_FOUND" in str(exc) or "Path does not exist" in str(exc) or "UNABLE_TO_INFER_SCHEMA" in str(exc):
                continue
            raise
    return data


def pseudonymise(customers):
    salt = os.environ["PSEUDO_SALT"]
    return (
        customers
        .withColumn("email_hash", F.sha2(F.concat(F.lower(F.col("email")), F.lit(salt)), 256))
        .withColumn("email_domain", F.split(F.col("email"), "@").getItem(1))
        .drop("email", "name")
    )


def duplicates(df, cols):
    return df.groupBy(*cols).count().filter("count > 1").count()


def orphans(child, child_col, parent, parent_col):
    parents = parent.select(F.col(parent_col).alias("_k")).distinct()
    return child.join(parents, child[child_col] == parents["_k"], "left_anti").count()


def quality_checks(d):
    """Renvoie une liste (nom, gravite, nb_violations)."""
    checks = []

    def add(name, severity, fn, *needed):
        if all(n in d for n in needed):
            checks.append((name, severity, fn()))

    add("customers: id unique", "error", lambda: duplicates(d["customers"], ["id"]), "customers")
    add("orders: id unique", "error", lambda: duplicates(d["orders"], ["id"]), "orders")
    add("order_items: id unique", "error", lambda: duplicates(d["order_items"], ["id"]), "order_items")
    add("products: product_id unique", "error", lambda: duplicates(d["products"], ["product_id"]), "products")
    add("customers: email unique", "error", lambda: duplicates(d["customers"], ["email"]), "customers")
    add("orders -> customers", "error", lambda: orphans(d["orders"], "customer_id", d["customers"], "id"), "orders", "customers")
    add("order_items -> orders", "error", lambda: orphans(d["order_items"], "order_id", d["orders"], "id"), "order_items", "orders")
    add("payments -> orders", "error", lambda: orphans(d["payments"], "order_id", d["orders"], "id"), "payments", "orders")
    add("stock -> warehouses", "error", lambda: orphans(d["stock"], "warehouse_id", d["warehouses"], "id"), "stock", "warehouses")
    add("order_items -> products", "warn", lambda: orphans(d["order_items"], "product_id", d["products"], "product_id"), "order_items", "products")

    def total_mismatch():
        sums = d["order_items"].groupBy("order_id").agg(F.sum("line_total_cents").alias("items_total"))
        return (d["orders"].join(sums, d["orders"]["id"] == sums["order_id"], "left")
                .filter(F.coalesce(F.col("items_total"), F.lit(0)) != F.col("total_cents")).count())

    add("orders.total = somme des lignes", "warn", total_mismatch, "orders", "order_items")
    return checks


def main():
    spark = build_spark("market-staging-to-curated", delta_conf(), uris=(STAGING, CURATED))
    spark.sparkContext.setLogLevel("WARN")
    data = load(spark)
    print(f"Tables lues dans staging : {', '.join(data)}")

    # Les controles portent sur les donnees de staging (e-mail encore present pour verifier son unicite).
    checks = quality_checks(data)
    run_at = datetime.now(timezone.utc).isoformat()
    report = spark.createDataFrame(
        [(run_at, n, s, v, "OK" if v == 0 else ("ECHEC" if s == "error" else "AVERTISSEMENT")) for n, s, v in checks],
        "run_at string, check_name string, severity string, violations long, result string",
    )
    print("Controles qualite :")
    report.drop("run_at").show(truncate=False)
    failed = [n for n, s, v in checks if s == "error" and v > 0]

    if failed:
        print(f"ARRET : {len(failed)} controle(s) bloquant(s) en echec : {failed}. Rien n'est ecrit dans curated.")
        report.coalesce(1).write.mode("append").json(f"{CURATED}/_quality")
        spark.stop()
        sys.exit(1)

    if "customers" in data:
        data["customers"] = pseudonymise(data["customers"])
    if "orders" in data:
        data["orders"] = data["orders"].withColumn("order_date", F.to_date("created_at"))

    for name, df in data.items():
        writer = df.write.format(FORMAT).mode("overwrite").option("overwriteSchema", "true")
        if name == "orders":
            writer = writer.partitionBy("order_date")
        writer.save(f"{CURATED}/{name}")
        print(f"  curated/{name}: {df.count()} lignes ({FORMAT})")
    report.coalesce(1).write.mode("append").json(f"{CURATED}/_quality")
    print("Termine.")
    spark.stop()


if __name__ == "__main__":
    main()
