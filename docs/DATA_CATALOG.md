# Catalogue de donnees - Data Lake Market

Genere automatiquement le 2026-11-17 20:17 UTC par `lake-consumption`. Ne pas modifier a la main.

## Zones

| Zone | Role |
|---|---|
| `raw` | Copie brute des sources (PostgreSQL, MongoDB), Parquet, rangee par date d'extraction. Contient des donnees personnelles (e-mail, nom) en clair : acces restreint. |
| `staging` | Donnees nettoyees et dedoublonnees. Lignes invalides ecartees dans `_rejected`. Contient encore e-mail et nom : acces restreint. |
| `curated` | Tables Delta de confiance. E-mail remplace par `email_hash`, nom supprime. Controles qualite bloquants avant ecriture. |
| `consumption` | Tables agregees pour l'analyse et la BI. Aucune donnee personnelle directe. |

## Zone `curated`

### `curated.customers` (2 lignes)

Clients (pseudonymises). Origine : staging.customers.

| Colonne | Type | Remarque |
|---|---|---|
| id | bigint |  |
| email_hash | string | pseudonymise (SHA-256 + sel) |
| email_domain | string | domaine seul, non identifiant |

### `curated.orders` (3 lignes)

Commandes, partitionnees par jour. Origine : staging.orders.

| Colonne | Type | Remarque |
|---|---|---|
| id | bigint |  |
| customer_id | bigint |  |
| status | string |  |
| total_cents | bigint |  |
| created_at | timestamp |  |
| idempotency_key | string |  |
| total_euros | double |  |
| order_date | date |  |

### `curated.order_items` (3 lignes)

Lignes de commande. Origine : staging.order_items.

| Colonne | Type | Remarque |
|---|---|---|
| id | bigint |  |
| order_id | bigint |  |
| product_id | bigint |  |
| quantity | bigint |  |
| unit_price_cents | bigint |  |
| line_total_cents | bigint |  |
| line_total_euros | double |  |

### `curated.products` (3 lignes)

Catalogue produits (MongoDB). Origine : staging.products.

| Colonne | Type | Remarque |
|---|---|---|
| tags | array<string> |  |
| product_id | bigint |  |
| name | string |  |
| category | string |  |
| price_cents | bigint |  |
| status | string |  |
| indexed_at | timestamp_ntz |  |
| attributes_json | string |  |
| price_euros | double |  |

### `curated.stock` (3 lignes)

Stock par produit et entrepot. Origine : staging.stock.

| Colonne | Type | Remarque |
|---|---|---|
| product_id | bigint |  |
| warehouse_id | bigint |  |
| quantity | bigint |  |

### `curated.warehouses` (2 lignes)

Entrepots. Origine : staging.warehouses.

| Colonne | Type | Remarque |
|---|---|---|
| id | bigint |  |
| name | string |  |
| capacity_units | bigint |  |

### `curated.payments` (2 lignes)

Paiements. Origine : staging.payments.

| Colonne | Type | Remarque |
|---|---|---|
| id | bigint |  |
| order_id | bigint |  |
| amount_cents | bigint |  |
| status | string |  |
| created_at | timestamp |  |
| amount_euros | double |  |

### `curated.reservations` (2 lignes)

Reservations de stock par commande. Origine : staging.reservations.

| Colonne | Type | Remarque |
|---|---|---|
| order_id | bigint |  |
| status | string |  |
| created_at | timestamp |  |
| updated_at | timestamp |  |

### `curated.reservation_items` (2 lignes)

Detail des reservations. Origine : staging.reservation_items.

| Colonne | Type | Remarque |
|---|---|---|
| order_id | bigint |  |
| product_id | bigint |  |
| warehouse_id | bigint |  |
| quantity | bigint |  |

## Zone `consumption`

### `consumption.sales_daily` (1 lignes)

Ventes par jour : commandes, chiffre d'affaires, panier moyen. Origine : curated.orders.

| Colonne | Type | Remarque |
|---|---|---|
| order_date | date |  |
| orders_total | bigint |  |
| orders_completed | bigint |  |
| orders_failed | bigint |  |
| revenue_euros | double |  |
| avg_basket_euros | double |  |

### `consumption.sales_by_category` (1 lignes)

Ventes par categorie de produit. Origine : curated.order_items + orders + products.

| Colonne | Type | Remarque |
|---|---|---|
| category | string |  |
| units_sold | bigint |  |
| orders_count | bigint |  |
| revenue_euros | double |  |

### `consumption.stock_by_warehouse` (2 lignes)

Stock et taux de remplissage par entrepot. Origine : curated.stock + warehouses.

| Colonne | Type | Remarque |
|---|---|---|
| warehouse_id | bigint |  |
| warehouse | string |  |
| capacity_units | bigint |  |
| units_in_stock | bigint |  |
| products_count | bigint |  |
| fill_rate_pct | double |  |

### `consumption.stock_alerts` (1 lignes)

Produits dont le stock est bas. Origine : curated.stock + warehouses + products.

| Colonne | Type | Remarque |
|---|---|---|
| product_id | bigint |  |
| product_name | string |  |
| warehouse | string |  |
| quantity | bigint |  |

### `consumption.customer_activity` (2 lignes)

Activite par client (pseudonymise). Origine : curated.customers + orders.

| Colonne | Type | Remarque |
|---|---|---|
| customer_id | bigint |  |
| email_hash | string | pseudonymise (SHA-256 + sel) |
| email_domain | string | domaine seul, non identifiant |
| orders_count | bigint |  |
| orders_completed | bigint |  |
| total_spent_euros | double |  |
| last_order_date | date |  |

## Donnees personnelles

- `raw` et `staging` : e-mail et nom en clair (acces restreint).
- `curated` : e-mail remplace par `email_hash`, nom supprime.
- `consumption` : uniquement des agregats et `email_hash`.
