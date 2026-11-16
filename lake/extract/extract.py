"""Volet 2 - extraction vers la zone RAW du data lake.

Copie telles quelles les tables PostgreSQL (orders, inventory, payments) et la collection MongoDB (catalog)
dans le bucket `raw`, en Parquet, rangees par date d'extraction :

    s3://raw/<source>/<table>/dt=YYYY-MM-DD/<table>_HHMMSS.parquet

Aucun nettoyage ici (c'est le role de `staging`). Le schema `auth` n'est JAMAIS extrait :
il contient les hash de mots de passe.
"""
import io
import json
import os
import sys
from datetime import datetime, timezone

import boto3
import psycopg
import pyarrow as pa
import pyarrow.parquet as pq
from psycopg.rows import dict_row
from pymongo import MongoClient

# (schema, table) copiees depuis PostgreSQL. Pas de schema auth.
PG_TABLES = [
    ("orders", "customers"),
    ("orders", "orders"),
    ("orders", "order_items"),
    ("inventory", "warehouses"),
    ("inventory", "stock"),
    ("inventory", "reservations"),
    ("inventory", "reservation_items"),
    ("payments", "payments"),
]

NOW = datetime.now(timezone.utc)
DT = NOW.strftime("%Y-%m-%d")
TS = NOW.strftime("%H%M%S")


def env(name, default=None):
    value = os.getenv(name, default)
    if value is None:
        sys.exit(f"Variable d'environnement manquante : {name}")
    return value


def s3_client():
    return boto3.client(
        "s3",
        endpoint_url=env("S3_ENDPOINT", "http://rustfs:9000"),
        aws_access_key_id=env("S3_ACCESS_KEY"),
        aws_secret_access_key=env("S3_SECRET_KEY"),
        region_name="us-east-1",
    )


def upload(s3, bucket, source, table, rows):
    """Ecrit `rows` (liste de dict) en Parquet. Renvoie le nombre de lignes ecrites."""
    if not rows:
        print(f"  {source}.{table}: 0 ligne, rien a ecrire")
        return 0
    for row in rows:
        row["_extracted_at"] = NOW
    buf = io.BytesIO()
    data = pa.Table.from_pylist(rows)
    # Une colonne entierement NULL aurait le type "null" : on la force en texte pour que le schema reste stable.
    for i, field in enumerate(data.schema):
        if pa.types.is_null(field.type):
            data = data.set_column(i, field.name, data[field.name].cast(pa.string()))
    pq.write_table(data, buf, compression="snappy")
    key = f"{source}/{table}/dt={DT}/{table}_{TS}.parquet"
    s3.put_object(Bucket=bucket, Key=key, Body=buf.getvalue())
    print(f"  {source}.{table}: {len(rows)} lignes -> s3://{bucket}/{key}")
    return len(rows)


def extract_postgres(s3, bucket):
    conninfo = psycopg.conninfo.make_conninfo(
        host=env("POSTGRES_HOST", "postgres"),
        port=env("POSTGRES_PORT", "5432"),
        dbname=env("POSTGRES_DB"),
        user=env("POSTGRES_USER"),
        password=env("POSTGRES_PASSWORD"),
    )
    total = 0
    with psycopg.connect(conninfo, row_factory=dict_row) as conn:
        for schema, table in PG_TABLES:
            rows = conn.execute(f'SELECT * FROM "{schema}"."{table}"').fetchall()
            total += upload(s3, bucket, "postgres", f"{schema}_{table}", rows)
    return total


def extract_mongo(s3, bucket):
    host, port = env("MONGO_HOST", "mongo"), env("MONGO_PORT", "27017")
    client = MongoClient(
        f"mongodb://{host}:{port}",
        username=env("MONGO_INITDB_ROOT_USERNAME"),
        password=env("MONGO_INITDB_ROOT_PASSWORD"),
        authSource="admin",
        serverSelectionTimeoutMS=5000,
    )
    docs = list(client[env("MONGO_INITDB_DATABASE")]["catalog"].find({}))
    rows = []
    for doc in docs:
        doc.pop("_id", None)  # ObjectId interne, sans interet
        # `attributes` a une forme libre : on le garde tel quel en JSON texte.
        doc["attributes_json"] = json.dumps(doc.pop("attributes", {}) or {}, ensure_ascii=False, default=str)
        rows.append(doc)
    return upload(s3, bucket, "mongo", "catalog", rows)


def main():
    bucket = env("RAW_BUCKET", "raw")
    s3 = s3_client()
    print(f"Extraction vers s3://{bucket}/ (dt={DT})")
    n = extract_postgres(s3, bucket) + extract_mongo(s3, bucket)
    print(f"Termine : {n} lignes copiees dans raw.")


if __name__ == "__main__":
    main()
