
if (!db.getCollectionNames().includes("catalog")) {
  db.createCollection("catalog", {
    validator: {
      $jsonSchema: {
        bsonType: "object",
        required: ["product_id", "name", "category", "price_cents"],
        properties: {
          product_id:  { bsonType: "int" },
          name:        { bsonType: "string" },
          category:    { bsonType: "string" },
          price_cents: { bsonType: "int", minimum: 0 },
          tags:        { bsonType: "array", items: { bsonType: "string" } },
          description: { bsonType: "string" },
          attributes:  { bsonType: "object" },
          indexed_at:  { bsonType: "date" },
          status:      { enum: ["active", "inactive"] }
        }
      }
    }
  });
}

db.catalog.createIndex({ product_id: 1 }, { unique: true });
db.catalog.createIndex({ category: 1 });
db.catalog.createIndex({ tags: 1 });