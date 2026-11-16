#!/bin/bash
# Crée les rôles applicatifs et un utilisateur de test par rôle.
# Les mots de passe viennent du .env (jamais écrits dans le dépôt) et sont
# hachés en bcrypt par pgcrypto avant d'être stockés.
set -e

for v in SEED_ADMIN_PASSWORD SEED_CATALOG_PASSWORD SEED_ANALYST_PASSWORD SEED_LOGISTICS_PASSWORD; do
  if [ -z "${!v}" ]; then
    echo "ERREUR : $v est vide ou absent du .env" >&2
    exit 1
  fi
done

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v admin_pw="$SEED_ADMIN_PASSWORD" \
  -v catalog_pw="$SEED_CATALOG_PASSWORD" \
  -v analyst_pw="$SEED_ANALYST_PASSWORD" \
  -v logistics_pw="$SEED_LOGISTICS_PASSWORD" <<'EOSQL'
CREATE EXTENSION IF NOT EXISTS pgcrypto;

INSERT INTO auth.roles (name, description) VALUES
  ('admin',           'Responsable de la plateforme : tout lire et écrire'),
  ('catalog_manager', 'Gère le catalogue produits'),
  ('analyst',         'Lecture des commandes et du stock'),
  ('logistics',       'Lecture des commandes, mise à jour du stock'),
  ('service_account', 'Compte technique entre services')
ON CONFLICT (name) DO NOTHING;

-- bcrypt (coût 12) ; rejouable : on met à jour le haché si l'email existe déjà
INSERT INTO auth.users (email, password_hash) VALUES
  ('admin@market.test',     crypt(:'admin_pw',     gen_salt('bf', 12))),
  ('catalog@market.test',   crypt(:'catalog_pw',   gen_salt('bf', 12))),
  ('lea@market.test',       crypt(:'analyst_pw',   gen_salt('bf', 12))),
  ('karim@market.test',     crypt(:'logistics_pw', gen_salt('bf', 12)))
ON CONFLICT (email) DO UPDATE SET password_hash = EXCLUDED.password_hash;

INSERT INTO auth.user_roles (user_id, role_id)
SELECT u.id, r.id
FROM (VALUES
  ('admin@market.test',   'admin'),
  ('catalog@market.test', 'catalog_manager'),
  ('lea@market.test',     'analyst'),
  ('karim@market.test',   'logistics')
) AS m(email, role)
JOIN auth.users u ON u.email = m.email
JOIN auth.roles r ON r.name  = m.role
ON CONFLICT DO NOTHING;
EOSQL