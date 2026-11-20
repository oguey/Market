/* Droits par role + actions du catalogue, du stock et des commandes. Charge apres app.js : redefinit quelques fonctions globales.
   Le serveur reste l'autorite (403 si le role n'a pas le droit) ; ici on evite juste d'afficher ce qui est interdit. */
const ACCESS = {
  admin:           { pages: ["overview", "orders", "catalog", "logistics", "observability", "access"], catalogWrite: true,  deleteProduct: true,  stockEdit: true, orderWrite: true, manageUsers: true },
  catalog_manager: { pages: ["overview", "catalog"],                                         catalogWrite: true,  deleteProduct: false, stockEdit: false },
  analyst:         { pages: ["overview", "orders", "catalog", "logistics", "observability"], catalogWrite: false, deleteProduct: false, stockEdit: false },
  logistics:       { pages: ["overview", "orders", "catalog", "logistics"],                  catalogWrite: false, deleteProduct: false, stockEdit: true }
};
const LOADERS = { orders: "loadOrders", logistics: "loadWarehouses", observability: "loadHealth" };

function userRoles() {
  try { return decodeClaims(state.token).roles; } catch { return []; }
}
function allowed(flag) {
  const roles = userRoles().filter((role) => ACCESS[role]);
  if (flag === "pages") return [...new Set(roles.flatMap((role) => ACCESS[role].pages))];
  return roles.some((role) => ACCESS[role][flag]);
}
const canPage = (page) => !state.token || allowed("pages").includes(page);

function applyAccess() {
  document.querySelectorAll("[data-page]").forEach((el) => el.classList.toggle("hidden", !canPage(el.dataset.page)));
  document.querySelectorAll(".page-section").forEach((el) => {
    if (!canPage(el.id.replace("page-", ""))) el.classList.remove("active");
  });
  if (!canPage(state.page)) setPage(allowed("pages")[0] || "overview");
  const th = $("#catalog-table")?.closest("table")?.querySelector("thead tr");
  if (th && !th.querySelector(".actions-th") && allowed("catalogWrite")) th.insertAdjacentHTML("beforeend", '<th class="actions-th">ACTIONS</th>');
  if (th && th.querySelector(".actions-th") && !allowed("catalogWrite")) th.querySelector(".actions-th").remove();
  const bar = $("#page-catalog .panel-toolbar");
  if (bar && !$("#new-product")) bar.insertAdjacentHTML("beforeend", '<button class="primary-button hidden" id="new-product" type="button">+ Nouveau produit</button>');
  $("#new-product")?.classList.toggle("hidden", !allowed("catalogWrite"));
  renderCatalog();
}

const _setPage = setPage;
setPage = function (page) {
  if (!canPage(page)) { toast("Votre rôle n'a pas accès à cette page."); page = allowed("pages")[0] || "overview"; }
  _setPage(page);
};
const _updateUser = updateUser;
updateUser = function () { _updateUser(); applyAccess(); };

refreshAll = async function (forceCatalog = false) {
  if (!state.token || state.loading) return;
  state.loading = true;
  $("#refresh-button").classList.add("is-spinning");
  $("#global-error").classList.add("hidden");
  const jobs = [loadCatalog(forceCatalog)];
  for (const [page, fn] of Object.entries(LOADERS)) if (canPage(page)) jobs.push(window[fn]());  // pas d'appel interdit : pas de 403 inutiles
  const results = await Promise.allSettled(jobs);
  const failures = results.filter((result) => result.status === "rejected");
  if (failures.length) showDataError(`Certaines données n’ont pas pu être chargées : ${failures.map((r) => r.reason.message).join(" · ")}`);
  $("#last-updated").textContent = `Mis à jour à ${new Intl.DateTimeFormat("fr-FR", { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(new Date())}`;
  state.loading = false;
  $("#refresh-button").classList.remove("is-spinning");
};

/* ---- Catalogue : tableau avec actions ---- */
renderCatalog = function () {
  const term = ($("#product-search")?.value || "").trim().toLowerCase();
  const products = state.products.filter((p) => `${p.name} ${p.category} ${p.productId}`.toLowerCase().includes(term));
  const write = allowed("catalogWrite"), withActions = write;
  $("#catalog-count").textContent = `${number(products.length)} produit${products.length > 1 ? "s" : ""}`;
  $("#catalog-table").innerHTML = products.length ? products.map((p) => {
    const stock = state.stock.get(p.productId);
    const properties = p.attributes && Object.keys(p.attributes).length ? JSON.stringify(p.attributes) : "Aucune caractéristique";
    const buttons = [
      write && `<button class="table-action" data-prod="edit" data-id="${p.productId}">Modifier</button>`,
      write && p.status === "active" && `<button class="table-action" data-prod="archive" data-id="${p.productId}">Archiver</button>`,
      allowed("deleteProduct") && `<button class="table-action danger-text" data-prod="delete" data-id="${p.productId}">Supprimer</button>`
    ].filter(Boolean).join(" ");
    return `<tr>
      <td><div class="product-name-cell"><span class="product-swatch">${escapeHtml(initials(p.category).slice(0, 1) || "P")}</span><div><div class="product-title">${escapeHtml(p.name)}</div><div class="product-subtitle">ID ${escapeHtml(p.productId)}</div></div></div></td>
      <td>${escapeHtml(p.category)}</td><td><span class="properties" title="${escapeHtml(properties)}">${escapeHtml(properties)}</span></td>
      <td>${escapeHtml(money(p.priceCents))}</td><td><span class="stock-number ${stock && stock.total <= 5 ? "low" : ""}">${escapeHtml(stock ? number(stock.total) : "—")}</span></td>
      <td><span class="status-badge ${p.status === "active" ? "active" : "inactive"}">${p.status === "active" ? "Actif" : "Archivé"}</span></td>
      ${withActions ? `<td class="row-actions">${buttons}</td>` : ""}
    </tr>`;
  }).join("") : `<tr><td class="empty-row" colspan="7">Aucun produit trouvé.</td></tr>`;
  renderStockPanel();
};

async function gql(query, variables) {
  const result = await json("/api/catalog/graphql", { method: "POST", body: JSON.stringify({ query, variables }) });
  if (result.errors?.length) throw new Error(result.errors[0].message);
  return result.data;
}

/* ---- Fenetre de saisie (produit / stock / commande) ---- */
function openDialog(title, fieldsHtml, onSubmit) {
  closeDialog();
  document.body.insertAdjacentHTML("beforeend", `<div class="modal-backdrop" id="act-modal"><form class="auth-dialog" id="act-form"><button class="modal-close" type="button" data-close>×</button><h2>${title}</h2>${fieldsHtml}<p class="form-error hidden" id="act-error"></p><button class="primary-button auth-submit" type="submit">Enregistrer <span>→</span></button></form></div>`);
  $("#act-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!onSubmit) return;
    const data = Object.fromEntries(new FormData(event.target));
    try { await onSubmit(data); closeDialog(); }
    catch (error) { $("#act-error").textContent = error.message; $("#act-error").classList.remove("hidden"); }
  });
  $("#act-form input, #act-form select")?.focus();
}
function closeDialog() { $("#act-modal")?.remove(); }

const field = (label, name, value = "", extra = "") => `<label class="field-label" for="a-${name}">${label}</label><input id="a-${name}" name="${name}" value="${escapeHtml(value)}" ${extra}>`;
const attrsToText = (attrs) => Object.entries(attrs || {}).map(([k, v]) => `${k}: ${v}`).join("\n");
function textToAttrs(text) {
  const out = {};
  for (const line of String(text || "").split("\n")) {
    const i = line.indexOf(":");
    if (i < 1) continue;
    const key = line.slice(0, i).trim(), raw = line.slice(i + 1).trim();
    out[key] = raw !== "" && !Number.isNaN(Number(raw)) ? Number(raw) : raw;
  }
  return out;
}
const parsePrice = (text) => {
  const euros = Number(String(text).replace(",", ".").replace(/\s/g, ""));
  if (!(euros >= 0) || !Number.isFinite(euros)) throw new Error("Prix invalide (exemple : 89,90).");
  return Math.round(euros * 100);
};
const productFields = (p = {}) =>
  field("Nom", "name", p.name, "required minlength=\"2\"") + field("Catégorie", "category", p.category, "required") +
  field("Prix (€)", "price", p.priceCents != null ? (p.priceCents / 100).toFixed(2).replace(".", ",") : "", "required inputmode=\"decimal\"") +
  `<label class="field-label" for="a-attrs">Caractéristiques (une par ligne, « clé: valeur »)</label><textarea id="a-attrs" name="attrs" rows="3">${escapeHtml(attrsToText(p.attributes))}</textarea>`;

async function newProduct() {
  const all = (await gql("{ products(limit: 200) { productId } }")).products;
  const nextId = Math.max(0, ...all.map((p) => p.productId)) + 1;
  openDialog("Nouveau produit", productFields(), async (d) => {
    await gql("mutation($in: ProductInput!) { createProduct(input: $in) { productId } }",
      { in: { productId: nextId, name: d.name.trim(), category: d.category.trim(), priceCents: parsePrice(d.price), attributes: textToAttrs(d.attrs) } });
    toast(`Produit #${nextId} créé. Le stock se règle depuis le bouton « Stock » (rôles logistique/admin).`);
    await loadCatalog(true);
  });
}
function editProduct(id) {
  const p = state.products.find((item) => item.productId === id);
  openDialog(`Modifier le produit #${id}`, productFields(p), async (d) => {
    await gql("mutation($id: Int!, $p: ProductPatch!) { updateProduct(productId: $id, patch: $p) { productId } }",
      { id, p: { name: d.name.trim(), category: d.category.trim(), priceCents: parsePrice(d.price), attributes: textToAttrs(d.attrs) } });
    toast("Produit modifié.");
    await loadCatalog(true);
  });
}
async function archiveProduct(id) {
  if (!confirm(`Archiver le produit #${id} ? Il reste en base (des commandes peuvent le référencer).`)) return;
  try { await gql("mutation($id: Int!) { archiveProduct(productId: $id) { productId } }", { id }); toast("Produit archivé."); await loadCatalog(true); }
  catch (error) { toast(error.message); }
}
async function deleteProduct(id) {
  if (!confirm(`Supprimer définitivement le produit #${id} ? Action irréversible (admin).`)) return;
  try { await gql("mutation($id: Int!) { deleteProduct(productId: $id) }", { id }); toast("Produit supprimé."); await loadCatalog(true); }
  catch (error) { toast(error.message); }
}
function editStock(id) {
  const stock = state.stock.get(id);
  const options = (state.warehouses.length ? state.warehouses : [{ id: 1, name: "Entrepôt 1" }])
    .map((w) => `<option value="${w.id}">${escapeHtml(w.name)}</option>`).join("");
  openDialog(`Stock du produit #${id}`,
    `<p class="auth-intro">Stock total actuel : ${stock ? number(stock.total) : "—"}. Indiquez la quantité disponible dans un entrepôt.</p>
     <label class="field-label" for="a-wh">Entrepôt</label><select id="a-wh" name="warehouse">${options}</select>` +
    field("Quantité", "quantity", "", 'required inputmode="numeric"'),
    async (d) => {
      const quantity = Number(d.quantity);
      if (!Number.isInteger(quantity) || quantity < 0) throw new Error("Quantité invalide (entier ≥ 0).");
      await json(`/api/inventory/inventory/${id}`, { method: "PUT", body: JSON.stringify({ warehouse_id: Number(d.warehouse), quantity }) });
      toast("Stock mis à jour.");
      state.catalogLoadedAt = 0;
      await Promise.allSettled([loadCatalog(true), loadWarehouses()]);
    });
}

document.addEventListener("click", (event) => {
  if (event.target.closest("[data-close]") || event.target.id === "act-modal") return closeDialog();
  if (event.target.closest("#new-product")) return newProduct().catch((error) => toast(error.message));
  const b = event.target.closest("[data-prod]");
  if (!b) return;
  const id = Number(b.dataset.id);
  ({ edit: editProduct, stock: editStock, archive: archiveProduct, delete: deleteProduct })[b.dataset.prod](id);
});
document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeDialog(); });

/* =====================  COMMANDES : detail, creation, changement de statut  ===================== */
const STATUS_NEXT = { paid: ["preparing", "Passer en préparation"], preparing: ["delivered", "Marquer comme livrée"] };
const uuid = () => (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`);
const productName = (id) => state.products.find((p) => p.productId === id)?.name || `Produit #${id}`;

async function showOrder(id) {
  const order = await json(`/api/orders/orders/${id}`);
  let payment = null;
  try { payment = await json(`/api/payments/payments/${id}`); } catch { /* role sans acces aux paiements ou pas de paiement */ }
  const lines = (order.items || []).map((i) => `<tr><td>${escapeHtml(productName(i.product_id))}</td><td>${i.quantity}</td><td>${escapeHtml(money(i.unit_price_cents))}</td><td>${escapeHtml(money(i.unit_price_cents * i.quantity))}</td></tr>`).join("");
  const next = STATUS_NEXT[order.status];
  const action = allowed("orderWrite") && next ? `<button class="primary-button auth-submit" type="button" id="order-advance">${next[1]} <span>→</span></button>` : "";
  openDialog(`Commande #MK-${String(order.id).padStart(5, "0")}`,
    `<p class="auth-intro">${escapeHtml(order.customer_name)} · ${escapeHtml(formatDate(order.created_at))} · ${statusBadge(order.status)}${payment ? ` · Paiement : ${payment.status === "succeeded" ? "réussi" : "échoué"}` : ""}</p>
     <div class="table-wrap"><table><thead><tr><th>ARTICLE</th><th>QTÉ</th><th>PRIX</th><th>TOTAL</th></tr></thead><tbody>${lines}</tbody></table></div>
     <p class="auth-intro"><strong>Total : ${escapeHtml(money(order.total_cents))}</strong></p>${action}`, null);
  $("#act-form button[type=submit]")?.remove();
  $("#order-advance")?.addEventListener("click", async () => {
    try { await json(`/api/orders/orders/${id}/status`, { method: "PATCH", body: JSON.stringify({ status: next[0] }) }); closeDialog(); toast(`Commande passée à « ${statusLabel(next[0])} ».`); await loadOrders(); }
    catch (error) { $("#act-error").textContent = error.message; $("#act-error").classList.remove("hidden"); }
  });
}

function newOrder() {
  const customers = [...new Map(state.orders.map((o) => [o.customer_id, o.customer_name])).entries()];
  const products = state.products.filter((p) => p.status === "active");
  const productOptions = '<option value="">— produit —</option>' + products.map((p) => `<option value="${p.productId}">${escapeHtml(p.name)} · ${escapeHtml(money(p.priceCents))}</option>`).join("");
  const rows = [1, 2, 3].map((n) => `<div class="order-line"><select name="p${n}">${productOptions}</select><input name="q${n}" type="number" min="1" max="1000" value="1" aria-label="Quantité ${n}"></div>`).join("");
  openDialog("Nouvelle commande",
    `<label class="field-label" for="a-customer">Client</label>
     <select id="a-customer" name="customer"><option value="">Choisir un client existant…</option>${customers.map(([id, name]) => `<option value="${id}">${escapeHtml(name)} (#${id})</option>`).join("")}<option value="new">+ Nouveau client…</option></select>
     <div id="new-customer" class="hidden">${field("Nom du client", "cname", "")}${field("Email du client", "cemail", "", 'type="email"')}</div>
     <label class="field-label">Articles (1 à 3 lignes)</label>${rows}`,
    async (d) => {
      let customerId = Number(d.customer);
      if (d.customer === "new") {
        const c = await json("/api/orders/customers", { method: "POST", body: JSON.stringify({ name: d.cname.trim(), email: d.cemail.trim() }) });
        customerId = c.id;
      }
      if (!customerId) throw new Error("Choisissez ou créez un client.");
      const items = [1, 2, 3].filter((n) => d[`p${n}`]).map((n) => ({ product_id: Number(d[`p${n}`]), quantity: Number(d[`q${n}`]) || 1 }));
      if (!items.length) throw new Error("Ajoutez au moins un article.");
      const order = await json("/api/orders/orders", { method: "POST", headers: { "Idempotency-Key": uuid() }, body: JSON.stringify({ customer_id: customerId, items }) });
      toast(`Commande #MK-${String(order.id).padStart(5, "0")} : ${statusLabel(order.status)} (${money(order.total_cents)}).`);
      await Promise.allSettled([loadOrders(), loadCatalog(true), loadWarehouses()]);
    });
  $("#a-customer").addEventListener("change", (event) => $("#new-customer").classList.toggle("hidden", event.target.value !== "new"));
}

/* intercepter le clic « ··· » avant le gestionnaire de app.js (qui n'affichait qu'un message) */
document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-order-id]");
  if (!button) return;
  event.stopImmediatePropagation();
  showOrder(button.dataset.orderId).catch((error) => toast(`Impossible de charger la commande : ${error.message}`));
}, true);

/* =====================  ACCES & ROLES (admin) : creation d'utilisateurs  ===================== */
const ROLE_MATRIX = [
  ["Lire le catalogue", "oui", "oui", "oui", "oui"], ["Créer / modifier un produit", "oui", "oui", "—", "—"], ["Supprimer un produit", "oui", "—", "—", "—"],
  ["Lire les commandes", "oui", "—", "oui", "oui"], ["Créer une commande, changer son statut", "oui", "—", "—", "—"],
  ["Lire les entrepôts", "oui", "—", "oui", "oui"], ["Modifier le stock", "oui", "—", "—", "oui"],
  ["Observabilité (métriques, traces)", "oui", "—", "oui", "—"], ["Créer des utilisateurs", "oui", "—", "—", "—"]
];
function buildExtraPages() {
  if (!$("#page-access")) {
    $("#page-observability").insertAdjacentHTML("afterend", `<section class="page-section" id="page-access">
      <div class="page-heading"><div><p class="eyebrow">SÉCURITÉ</p><h1>Accès &amp; rôles</h1><p class="page-subtitle">Qui peut faire quoi, et création de comptes (service Auth).</p></div></div>
      <article class="panel table-panel full-panel"><div class="panel-heading"><div><h2>Matrice des droits</h2><p>Appliquée par chaque service (le serveur répond 403 sinon)</p></div></div>
        <div class="table-wrap"><table><thead><tr><th>ACTION</th><th>ADMIN</th><th>CATALOG_MANAGER</th><th>ANALYST</th><th>LOGISTICS</th></tr></thead><tbody>
        ${ROLE_MATRIX.map((r) => `<tr><td>${r[0]}</td>${r.slice(1).map((c) => `<td>${c === "oui" ? '<span class="status-badge active">oui</span>' : '<span class="subtle-label">—</span>'}</td>`).join("")}</tr>`).join("")}
        </tbody></table></div></article>
      <article class="panel full-panel"><div class="panel-heading"><div><h2>Créer un utilisateur</h2><p>Mot de passe de 8 caractères minimum, stocké haché (bcrypt)</p></div></div>
        <form id="user-form" class="inline-form"><input name="email" type="email" placeholder="prenom@market.test" required><input name="password" type="password" minlength="8" maxlength="72" placeholder="Mot de passe" required>
        <select name="role"><option>analyst</option><option>logistics</option><option>catalog_manager</option><option>admin</option></select><button class="primary-button" type="submit">Créer</button></form>
        <p class="form-error hidden" id="user-error"></p></article></section>`);
    $("#page-access").querySelector("#user-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const d = Object.fromEntries(new FormData(event.target));
      try {
        const u = await json("/api/auth/users", { method: "POST", body: JSON.stringify({ email: d.email.trim(), password: d.password, roles: [d.role] }) });
        toast(`Utilisateur ${u.email} créé (${u.roles.join(", ")}).`); event.target.reset(); $("#user-error").classList.add("hidden");
      } catch (error) { $("#user-error").textContent = error.message; $("#user-error").classList.remove("hidden"); }
    });
  }
  if (!document.querySelector('.nav-link[data-page="access"]')) {
    $('.nav-link[data-page="observability"]').insertAdjacentHTML("afterend", '<button class="nav-link" data-page="access"><span class="nav-icon">⚿</span>Accès &amp; rôles</button>');
    $('.nav-link[data-page="access"]').addEventListener("click", () => setPage("access"));
  }
  if (!$("#new-order") && $("#page-orders .panel-toolbar")) $("#page-orders .panel-toolbar").insertAdjacentHTML("beforeend", '<button class="primary-button hidden" id="new-order" type="button">+ Nouvelle commande</button>');
  if (!$("#tool-links")) {
    const note = $("#page-observability .observability-note");
    const host = location.hostname;
    const links = [["Grafana", 3001, "Tableaux de bord (dont « Temps réel »)"], ["Prometheus", 9090, "Métriques brutes"], ["Jaeger", 16686, "Traces d'une commande"],
      ["Kafka UI", 8090, "Topics et messages"], ["RustFS", 9001, "Lac de données (S3)"]];
    const html = `<article class="panel full-panel" id="tool-links"><div class="panel-heading"><div><h2>Outils d'observabilité</h2><p>Ouverts dans un nouvel onglet</p></div></div><div class="tool-grid">${links.map(([n, port, d]) => `<a class="tool-card" href="http://${host}:${port}" target="_blank" rel="noopener"><strong>${n}</strong><span>${d}</span><small>:${port}</small></a>`).join("")}</div></article>`;
    if (note) { note.insertAdjacentHTML("beforebegin", html); note.remove(); } else $("#page-observability").insertAdjacentHTML("beforeend", html);
  }
  const lnote = $("#page-logistics .logistics-note span:last-child");
  if (lnote) lnote.textContent = "Le taux de remplissage et le statut sont calculés par le service Inventory. Les données sont relues toutes les 30 secondes ; le flux temps réel des commandes (Kafka + Spark) est visible dans Grafana.";
}
const _applyAccess = applyAccess;
applyAccess = function () {
  buildExtraPages();
  _applyAccess();
  $("#new-order")?.classList.toggle("hidden", !allowed("orderWrite"));
  $("#overview-orders")?.closest("article")?.classList.toggle("hidden", !canPage("orders"));
};
const _setPage2 = setPage;
setPage = function (page) { _setPage2(page); if (page === "access") $("#breadcrumb-current").textContent = "Accès & rôles"; };
document.addEventListener("click", (event) => { if (event.target.closest("#new-order")) newOrder(); });

applyAccess();

if (state.token) refreshAll(true);
/* Logistique : stocks par produit et par entrepot. L'ajustement est reserve aux roles logistics et admin. */
function renderStockPanel() {
  if (!$("#warehouse-grid")) return;
  if (!$("#stock-panel")) {
    $("#warehouse-grid").insertAdjacentHTML("afterend", `<article class="panel table-panel full-panel" id="stock-panel" style="margin-top:17px"><div class="panel-heading"><div><h2>Stocks par produit</h2><p>Quantités par entrepôt (service Inventory)</p></div></div><div class="table-wrap"><table><thead><tr><th>PRODUIT</th><th>TOTAL</th><th>PAR ENTREPÔT</th><th></th></tr></thead><tbody id="stock-table"></tbody></table></div></article>`);
  }
  const edit = allowed("stockEdit");
  const names = new Map(state.warehouses.map((w) => [w.id, w.name]));
  $("#stock-table").innerHTML = state.products.length ? state.products.map((p) => {
    const s = state.stock.get(p.productId);
    const detail = s && s.warehouses.length ? s.warehouses.map((w) => `${escapeHtml(names.get(w.warehouse_id) || `Entrepôt ${w.warehouse_id}`)} : ${number(w.quantity)}`).join(" · ") : "Aucun stock";
    return `<tr><td><div class="product-title">${escapeHtml(p.name)}</div><div class="product-subtitle">ID ${escapeHtml(p.productId)}</div></td><td><span class="stock-number ${s && s.total <= 5 ? "low" : ""}">${s ? number(s.total) : "—"}</span></td><td>${detail}</td><td class="row-actions">${edit ? `<button class="table-action" data-prod="stock" data-id="${p.productId}">Ajuster</button>` : ""}</td></tr>`;
  }).join("") : '<tr><td class="empty-row" colspan="4">Aucun produit.</td></tr>';
}
const _renderLogistics = renderLogistics;
renderLogistics = function () { _renderLogistics(); renderStockPanel(); };