from conftest import token

MANAGER, ANALYST, ADMIN = token("catalog_manager"), token("analyst"), token("admin")

CREATE = """mutation($in: ProductInput!) { createProduct(input: $in) { productId name priceCents attributes status } }"""
HEADSET = {"productId": 1, "name": "Casque sans fil", "category": "audio", "priceCents": 7990,
           "tags": ["bluetooth"], "description": "Casque à réduction de bruit", "attributes": {"battery_hours": 30}}
SHOES = {"productId": 2, "name": "Chaussures de course", "category": "sport", "priceCents": 5990,
         "attributes": {"size": 42, "color": "blue"}}


def gql(client, query, variables=None, headers=MANAGER):
    return client.post("/v1/graphql", json={"query": query, "variables": variables or {}}, headers=headers)


def code(resp):
    return resp.json()["errors"][0]["extensions"]["code"]


def test_create_products_with_different_attributes(catalog):
    a = gql(catalog, CREATE, {"in": HEADSET}).json()["data"]["createProduct"]
    b = gql(catalog, CREATE, {"in": SHOES}).json()["data"]["createProduct"]
    assert a["attributes"] == {"battery_hours": 30} and a["status"] == "active"
    assert b["attributes"] == {"size": 42, "color": "blue"}


def test_read_only_requested_fields(catalog):
    gql(catalog, CREATE, {"in": HEADSET})
    body = gql(catalog, "{ product(productId: 1) { name priceCents indexedAt } }", headers=ANALYST).json()
    assert set(body["data"]["product"]) == {"name", "priceCents", "indexedAt"}


def test_filters_and_text_search(catalog):
    gql(catalog, CREATE, {"in": HEADSET})
    gql(catalog, CREATE, {"in": SHOES})
    q = "query($s: String, $c: String, $ids: [Int!]) { products(search: $s, category: $c, ids: $ids) { productId } }"
    ids = lambda **v: [p["productId"] for p in gql(catalog, q, v, ANALYST).json()["data"]["products"]]
    assert ids(s="réduction") == [1]
    assert ids(c="sport") == [2]
    assert ids(ids=[2, 1, 99]) == [1, 2]


def test_update_archive_delete(catalog):
    gql(catalog, CREATE, {"in": HEADSET})
    upd = gql(catalog, "mutation { updateProduct(productId: 1, patch: {priceCents: 6990}) { priceCents name } }")
    assert upd.json()["data"]["updateProduct"] == {"priceCents": 6990, "name": "Casque sans fil"}
    arch = gql(catalog, "mutation { archiveProduct(productId: 1) { status } }")
    assert arch.json()["data"]["archiveProduct"]["status"] == "inactive"
    assert code(gql(catalog, "mutation { deleteProduct(productId: 1) }")) == "FORBIDDEN"  # manager can't hard-delete
    assert gql(catalog, "mutation { deleteProduct(productId: 1) }", headers=ADMIN).json()["data"]["deleteProduct"]
    assert code(gql(catalog, "mutation { archiveProduct(productId: 1) { status } }")) == "NOT_FOUND"


def test_errors(catalog):
    gql(catalog, CREATE, {"in": HEADSET})
    assert code(gql(catalog, CREATE, {"in": HEADSET})) == "CONFLICT"
    assert code(gql(catalog, CREATE, {"in": {**SHOES, "priceCents": -1}})) == "BAD_USER_INPUT"


def test_authorization_is_checked_per_operation(catalog):
    assert catalog.post("/v1/graphql", json={"query": "{ products { name } }"}).status_code == 401
    assert gql(catalog, "{ products { name } }", headers=ANALYST).status_code == 200
    assert code(gql(catalog, CREATE, {"in": HEADSET}, headers=ANALYST)) == "FORBIDDEN"


def test_schema_contract_is_served(catalog):
    assert "type Product" in catalog.get("/v1/graphql/schema").text
