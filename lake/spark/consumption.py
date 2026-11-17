"""Volet 2 - CURATED -> CONSUMPTION : tables d'analyse + catalogue de donnees.

Construit des tables agregees, pretes pour la BI, a partir de `curated`, puis genere le catalogue
de donnees (zones, tables, colonnes, sensibilite, origine) dans `consumption/_catalog` et
dans un fichier Markdown (CATALOG_OUT, par defaut /docs/DATA_CATALOG.md).
Les ventes "realisees" sont les commandes payees, en preparation ou livrees.
"""
import json
import os
from datetime import datetime, timezone

from pyspark.sql import functions as F

from staging import build_spark

STAGING = os.getenv("STAGING_URI", "s3a://staging")
CURATED = os.getenv("CURATED_URI", "s3a://curated")
CONSUMPTION = os.getenv("CONSUMPTION_URI", "s3a://consumption")
FORMAT = os.getenv("CURATED_FORMAT", "delta")
CATALOG_OUT = os.getenv("CATALOG_OUT", "/docs/DATA_CATALOG.md")
DONE = ["paid", "preparing", "delivered"]
LOW_STOCK = int(os.getenv("LOW_STOCK_THRESHOLD", "5"))


def delta_conf():
    if FORMAT != "delta":
        return {}
    return {
        "spark.sql.extensions": "io.delta.sql.DeltaSparkSessionExtension",
        "spark.sql.catalog.spark_catalog": "org.apache.spark.sql.delta.catalog.DeltaCatalog",
    }


def read(spark, name):
    return spark.read.format(FORMAT).load(f"{CURATED}/{name}")


# ------------------------------------------------------------------ tables d'analyse
def sales_daily(orders):
    done = F.col("status").isin(DONE)
    return (orders.groupBy("order_date").agg(
        F.count("*").alias("orders_total"),
        F.sum(F.when(done, 1).otherwise(0)).alias("orders_completed"),
        F.sum(F.when(F.col("status").isin("payment_failed", "cancelled"), 1).otherwise(0)).alias("orders_failed"),
        F.round(F.sum(F.when(done, F.col("total_euros")).otherwise(0)), 2).alias("revenue_euros"),
        F.round(F.avg(F.when(done, F.col("total_euros"))), 2).alias("avg_basket_euros"),
    ).orderBy("order_date"))


def sales_by_category(orders, items, products):
    done = orders.filter(F.col("status").isin(DONE)).select(F.col("id").alias("order_id"))
    return (items.join(done, "order_id")
            .join(products.select("product_id", "category"), "product_id", "left")
            .withColumn("category", F.coalesce("category", F.lit("inconnu")))
            .groupBy("category").agg(
                F.sum("quantity").alias("units_sold"),
                F.countDistinct("order_id").alias("orders_count"),
                F.round(F.sum("line_total_euros"), 2).alias("revenue_euros"))
            .orderBy(F.desc("revenue_euros")))


def stock_by_warehouse(stock, warehouses):
    units = stock.groupBy("warehouse_id").agg(
        F.sum("quantity").alias("units_in_stock"), F.countDistinct("product_id").alias("products_count"))
    return (warehouses.select(F.col("id").alias("warehouse_id"), F.col("name").alias("warehouse"), "capacity_units")
            .join(units, "warehouse_id", "left")
            .fillna(0, ["units_in_stock", "products_count"])
            .withColumn("fill_rate_pct", F.round(F.col("units_in_stock") / F.col("capacity_units") * 100, 1))
            .orderBy("warehouse_id"))


def stock_alerts(stock, warehouses, products):
    return (stock.filter(F.col("quantity") <= LOW_STOCK)
            .join(warehouses.select(F.col("id").alias("warehouse_id"), F.col("name").alias("warehouse")), "warehouse_id")
            .join(products.select("product_id", F.col("name").alias("product_name")), "product_id", "left")
            .select("product_id", "product_name", "warehouse", "quantity")
            .orderBy("quantity"))


def customer_activity(customers, orders):
    agg = orders.groupBy("customer_id").agg(
        F.count("*").alias("orders_count"),
        F.sum(F.when(F.col("status").isin(DONE), 1).otherwise(0)).alias("orders_completed"),
        F.round(F.sum(F.when(F.col("status").isin(DONE), F.col("total_euros")).otherwise(0)), 2).alias("total_spent_euros"),
        F.max("order_date").alias("last_order_date"))
    return (customers.select(F.col("id").alias("customer_id"), "email_hash", "email_domain")
            .join(agg, "customer_id", "left")
            .fillna(0, ["orders_count", "orders_completed", "total_spent_euros"]))


# ------------------------------------------------------------------ catalogue
DESCRIPTIONS = {
    "curated": {
        "customers": ("Clients (pseudonymises)", "staging.customers"),
        "orders": ("Commandes, partitionnees par jour", "staging.orders"),
        "order_items": ("Lignes de commande", "staging.order_items"),
        "products": ("Catalogue produits (MongoDB)", "staging.products"),
        "stock": ("Stock par produit et entrepot", "staging.stock"),
        "warehouses": ("Entrepots", "staging.warehouses"),
        "payments": ("Paiements", "staging.payments"),
        "reservations": ("Reservations de stock par commande", "staging.reservations"),
        "reservation_items": ("Detail des reservations", "staging.reservation_items"),
    },
    "consumption": {
        "sales_daily": ("Ventes par jour : commandes, chiffre d'affaires, panier moyen", "curated.orders"),
        "sales_by_category": ("Ventes par categorie de produit", "curated.order_items + orders + products"),
        "stock_by_warehouse": ("Stock et taux de remplissage par entrepot", "curated.stock + warehouses"),
        "stock_alerts": ("Produits dont le stock est bas", "curated.stock + warehouses + products"),
        "customer_activity": ("Activite par client (pseudonymise)", "curated.customers + orders"),
    },
}
SENSITIVE = {
    "email_hash": "pseudonymise (SHA-256 + sel)",
    "email_domain": "domaine seul, non identifiant",
}
ZONES = [
    ("raw", "Copie brute des sources (PostgreSQL, MongoDB), Parquet, rangee par date d'extraction. "
            "Contient des donnees personnelles (e-mail, nom) en clair : acces restreint."),
    ("staging", "Donnees nettoyees et dedoublonnees. Lignes invalides ecartees dans `_rejected`. "
                "Contient encore e-mail et nom : acces restreint."),
    ("curated", f"Tables {FORMAT.capitalize()} de confiance. E-mail remplace par `email_hash`, nom supprime. "
                "Controles qualite bloquants avant ecriture."),
    ("consumption", "Tables agregees pour l'analyse et la BI. Aucune donnee personnelle directe."),
]


def describe(spark, zone, base, tables, fmt):
    out = []
    for name, (desc, source) in tables.items():
        try:
            df = spark.read.format(fmt).load(f"{base}/{name}")
        except Exception:
            continue
        cols = [{"name": f.name, "type": f.dataType.simpleString(), "note": SENSITIVE.get(f.name, "")} for f in df.schema.fields
                if not f.name.startswith("_")]
        out.append({"zone": zone, "table": name, "description": desc, "source": source, "rows": df.count(), "columns": cols})
    return out


def to_markdown(entries, generated_at):
    lines = ["# Catalogue de donnees - Data Lake Market", "",
             f"Genere automatiquement le {generated_at} par `lake-consumption`. Ne pas modifier a la main.", "",
             "## Zones", "", "| Zone | Role |", "|---|---|"]
    lines += [f"| `{z}` | {d} |" for z, d in ZONES]
    for zone in ("curated", "consumption"):
        lines += ["", f"## Zone `{zone}`"]
        for e in (x for x in entries if x["zone"] == zone):
            lines += ["", f"### `{zone}.{e['table']}` ({e['rows']} lignes)", "", f"{e['description']}. Origine : {e['source']}.", "",
                      "| Colonne | Type | Remarque |", "|---|---|---|"]
            lines += [f"| {c['name']} | {c['type']} | {c['note']} |" for c in e["columns"]]
    lines += ["", "## Donnees personnelles", "",
              "- `raw` et `staging` : e-mail et nom en clair (acces restreint).",
              "- `curated` : e-mail remplace par `email_hash`, nom supprime.",
              "- `consumption` : uniquement des agregats et `email_hash`.", ""]
    return "\n".join(lines)


def main():
    spark = build_spark("market-curated-to-consumption", delta_conf(), uris=(CURATED, CONSUMPTION))
    spark.sparkContext.setLogLevel("WARN")
    d = {n: read(spark, n) for n in ["customers", "orders", "order_items", "products", "stock", "warehouses"]}
    tables = {
        "sales_daily": sales_daily(d["orders"]),
        "sales_by_category": sales_by_category(d["orders"], d["order_items"], d["products"]),
        "stock_by_warehouse": stock_by_warehouse(d["stock"], d["warehouses"]),
        "stock_alerts": stock_alerts(d["stock"], d["warehouses"], d["products"]),
        "customer_activity": customer_activity(d["customers"], d["orders"]),
    }
    for name, df in tables.items():
        df.write.format(FORMAT).mode("overwrite").option("overwriteSchema", "true").save(f"{CONSUMPTION}/{name}")
        print(f"\n--- consumption/{name}")
        spark.read.format(FORMAT).load(f"{CONSUMPTION}/{name}").show(truncate=False)

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    entries = (describe(spark, "curated", CURATED, DESCRIPTIONS["curated"], FORMAT)
               + describe(spark, "consumption", CONSUMPTION, DESCRIPTIONS["consumption"], FORMAT))
    spark.createDataFrame([(json.dumps(entries, ensure_ascii=False),)], "catalog_json string") \
        .coalesce(1).write.mode("overwrite").json(f"{CONSUMPTION}/_catalog")
    try:
        os.makedirs(os.path.dirname(CATALOG_OUT), exist_ok=True)
        with open(CATALOG_OUT, "w", encoding="utf-8") as fh:
            fh.write(to_markdown(entries, generated_at))
        print(f"Catalogue ecrit : {CATALOG_OUT}")
    except OSError as exc:
        print(f"Catalogue Markdown non ecrit ({exc}) ; la version JSON est dans consumption/_catalog")
    spark.stop()


if __name__ == "__main__":
    main()
