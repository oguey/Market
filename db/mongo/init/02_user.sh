#!/bin/bash
set -e

mongosh --quiet \
  -u "$MONGO_INITDB_ROOT_USERNAME" -p "$MONGO_INITDB_ROOT_PASSWORD" \
  --authenticationDatabase admin "$MONGO_INITDB_DATABASE" <<EOF
db.createUser({
  user: "catalog_svc",
  pwd: "${CATALOG_DB_PASSWORD}",
  roles: [{ role: "readWrite", db: "${MONGO_INITDB_DATABASE}" }]
});
EOF