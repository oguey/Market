"""Catalog service — products in MongoDB, exposed through a schema-first GraphQL API."""
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from urllib.parse import quote_plus

from ariadne import MutationType, ObjectType, QueryType, ScalarType, graphql_sync, make_executable_schema
from fastapi import Body, Depends, FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse
from graphql import GraphQLError, value_from_ast_untyped
from pymongo import ASCENDING, TEXT, MongoClient, ReturnDocument
from pymongo.collection import Collection
from pymongo.errors import DuplicateKeyError

from common import observability
from common.auth import current_user, has_role

ALL_ROLES = ("admin", "catalog_manager", "analyst", "logistics", "service_account")
WRITERS = ("admin", "catalog_manager")
SDL = (Path(__file__).parent / "schema.graphql").read_text()
PROJECTION = {"_id": 0}


def mongo_collection() -> Collection:
    db = os.getenv("MONGO_INITDB_DATABASE", "market")
    uri = os.getenv("CATALOG_MONGO_URI") or (
        f"mongodb://catalog_svc:{quote_plus(os.getenv('CATALOG_DB_PASSWORD', ''))}"
        f"@{os.getenv('MONGO_HOST', 'localhost')}:27017/{db}?authSource={db}")
    return MongoClient(uri, serverSelectionTimeoutMS=2000)[db]["catalog"]


def ensure_indexes(col: Collection) -> None:
    col.create_index([("product_id", ASCENDING)], unique=True)
    col.create_index([("category", ASCENDING)])
    col.create_index([("tags", ASCENDING)])
    col.create_index([("name", TEXT), ("description", TEXT)], default_language="french")


def _error(message: str, code: str) -> GraphQLError:
    return GraphQLError(message, extensions={"code": code})


def roles(*allowed: str):
    """Per-resolver authorization (the HTTP layer only checks that the token is valid)."""
    def decorator(resolver):
        @wraps(resolver)
        def wrapper(obj, info, **kwargs):
            if not has_role(info.context["user"], allowed):
                raise _error("forbidden: insufficient role", "FORBIDDEN")
            return resolver(obj, info, **kwargs)
        return wrapper
    return decorator


def _validate(fields: dict) -> None:
    if fields.get("price_cents") is not None and fields["price_cents"] < 0:
        raise _error("priceCents must be >= 0", "BAD_USER_INPUT")
    for key in ("name", "category"):
        if key in fields and not (fields[key] or "").strip():
            raise _error(f"{key} must not be empty", "BAD_USER_INPUT")


def build_schema(col: Collection):
    query, mutation = QueryType(), MutationType()
    json_scalar = ScalarType("JSON", serializer=lambda v: v, value_parser=lambda v: v,
                             literal_parser=lambda ast, variables=None: value_from_ast_untyped(ast, variables))

    @query.field("product")
    @roles(*ALL_ROLES)
    def product(*_, product_id):
        return col.find_one({"product_id": product_id}, PROJECTION)

    @query.field("products")
    @roles(*ALL_ROLES)
    def products(*_, ids=None, category=None, search=None, status=None, limit=50):
        flt = {}
        if ids is not None:
            flt["product_id"] = {"$in": ids}
        if category:
            flt["category"] = category
        if status:
            flt["status"] = status
        if search:
            flt["$text"] = {"$search": search}
        return list(col.find(flt, PROJECTION).sort("product_id", ASCENDING).limit(max(1, min(limit, 200))))

    @mutation.field("createProduct")
    @roles(*WRITERS)
    def create_product(*_, input):
        if input["product_id"] <= 0:
            raise _error("productId must be > 0", "BAD_USER_INPUT")
        _validate(input)
        doc = {"tags": [], "description": None, "attributes": {}, **{k: v for k, v in input.items() if v is not None},
               "status": "active", "indexed_at": datetime.now(timezone.utc)}
        if doc["description"] is None:
            del doc["description"]  # the Mongo validator wants a string when present
        try:
            col.insert_one(doc)
        except DuplicateKeyError:
            raise _error(f"product {input['product_id']} already exists", "CONFLICT")
        return col.find_one({"product_id": input["product_id"]}, PROJECTION)

    def _update(product_id: int, changes: dict):
        changes["indexed_at"] = datetime.now(timezone.utc)
        doc = col.find_one_and_update({"product_id": product_id}, {"$set": changes},
                                      projection=PROJECTION, return_document=ReturnDocument.AFTER)
        if not doc:
            raise _error(f"product {product_id} not found", "NOT_FOUND")
        return doc

    @mutation.field("updateProduct")
    @roles(*WRITERS)
    def update_product(*_, product_id, patch):
        changes = {k: v for k, v in patch.items() if v is not None}
        _validate(changes)
        return _update(product_id, changes)

    @mutation.field("archiveProduct")
    @roles(*WRITERS)
    def archive_product(*_, product_id):
        return _update(product_id, {"status": "inactive"})

    @mutation.field("deleteProduct")
    @roles("admin")
    def delete_product(*_, product_id):
        if col.delete_one({"product_id": product_id}).deleted_count == 0:
            raise _error(f"product {product_id} not found", "NOT_FOUND")
        return True

    product_type = ObjectType("Product")
    product_type.set_field("indexedAt", lambda doc, _: doc["indexed_at"].isoformat() if doc.get("indexed_at") else None)

    return make_executable_schema(SDL, query, mutation, product_type, json_scalar, convert_names_case=True)


def create_app(collection: Collection | None = None) -> FastAPI:
    col = collection if collection is not None else mongo_collection()
    schema = build_schema(col)

    @asynccontextmanager
    async def lifespan(_):
        ensure_indexes(col)
        yield

    app = FastAPI(title="Market - Catalog", version="1.0.0", lifespan=lifespan)
    observability.setup(app, "catalog", lambda: col.database.command("ping"))

    @app.post("/v1/graphql", tags=["graphql"])
    def graphql(payload: dict = Body(...), user: dict = Depends(current_user)):
        """GraphQL endpoint. Contract: GET /v1/graphql/schema."""
        ok, result = graphql_sync(schema, payload, context_value={"user": user})
        return JSONResponse(result, status_code=200 if ok else 400)

    @app.get("/v1/graphql/schema", response_class=PlainTextResponse, tags=["graphql"])
    def sdl():
        return SDL

    return app

