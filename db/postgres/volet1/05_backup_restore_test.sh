#!/bin/bash
# Volet 1 : sauvegarde PostgreSQL + restauration TESTÉE.
# Se lance DANS le conteneur postgres (voir VOLET1_PREUVES.md). Ne touche pas à la base d'origine :
# on restaure dans une base temporaire, on compare, puis on la supprime.
set -euo pipefail

DB="${POSTGRES_DB:-market}"
USR="${POSTGRES_USER:-market_admin}"
DIR="${BACKUP_DIR:-/tmp/backups}"
STAMP="$(date +%Y%m%d_%H%M%S)"
FILE="$DIR/${DB}_${STAMP}.dump"
RESTORE_DB="${DB}_restore_test"
mkdir -p "$DIR"

# Empreinte d'une base : nombre de lignes par table (hors partitions) + somme de contrôle des commandes
fingerprint() {
  psql -U "$USR" -d "$1" -At -c "
    SELECT n.nspname || '.' || c.relname || '|' ||
           (xpath('/row/n/text()', query_to_xml(
              format('SELECT count(*) AS n FROM %I.%I', n.nspname, c.relname), false, true, '')))[1]::text
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind IN ('r', 'p') AND NOT c.relispartition
      AND n.nspname IN ('orders', 'inventory', 'payments', 'auth')
    ORDER BY 1;"
  psql -U "$USR" -d "$1" -At -c "
    SELECT 'orders.orders checksum|' || coalesce(md5(string_agg(id || ':' || status || ':' || total_cents, ',' ORDER BY id)), 'empty')
    FROM orders.orders;"
}

echo "== 1/5 Empreinte de la base d'origine ($DB)"
fingerprint "$DB" > "$DIR/before_${STAMP}.txt"
cat "$DIR/before_${STAMP}.txt"

echo "== 2/5 Sauvegarde (pg_dump, format custom compressé)"
pg_dump -U "$USR" -d "$DB" -Fc -f "$FILE"
# Les rôles (un par service) n'appartiennent pas à une base : on les sauvegarde à part
# (le fichier contient des empreintes de mots de passe : à protéger).
pg_dumpall -U "$USR" --roles-only -f "$DIR/roles_${STAMP}.sql"
chmod 600 "$DIR/roles_${STAMP}.sql"
echo "   $FILE ($(du -h "$FILE" | cut -f1))"

echo "== 3/5 Restauration dans une base temporaire ($RESTORE_DB)"
dropdb -U "$USR" --if-exists "$RESTORE_DB"
createdb -U "$USR" "$RESTORE_DB"
pg_restore -U "$USR" -d "$RESTORE_DB" --exit-on-error "$FILE"

echo "== 4/5 Comparaison"
fingerprint "$RESTORE_DB" > "$DIR/after_${STAMP}.txt"
if diff -q "$DIR/before_${STAMP}.txt" "$DIR/after_${STAMP}.txt" >/dev/null; then
  echo "   OK : toutes les tables ont le même nombre de lignes et les commandes la même somme de contrôle."
else
  echo "   ECHEC : différences entre l'original et la restauration :"
  diff "$DIR/before_${STAMP}.txt" "$DIR/after_${STAMP}.txt" || true
  exit 1
fi
# Les droits (isolation entre services) doivent survivre à la restauration
LEAK=$(psql -U "$USR" -d "$RESTORE_DB" -At -c \
  "SELECT has_schema_privilege('orders_svc', 'orders', 'USAGE') AND NOT has_schema_privilege('orders_svc', 'inventory', 'USAGE');")
[ "$LEAK" = "t" ] && echo "   OK : les droits par service sont restaurés (orders_svc lit orders, pas inventory)." \
                  || { echo "   ECHEC : les droits par service ne sont pas restaurés."; exit 1; }

echo "== 5/5 Nettoyage de la base temporaire (KEEP_RESTORE=1 pour la garder)"
[ "${KEEP_RESTORE:-0}" = "1" ] || dropdb -U "$USR" "$RESTORE_DB"

echo "Terminé. Fichiers dans $DIR : $(basename "$FILE"), roles_${STAMP}.sql, before/after_${STAMP}.txt"
