const SERVICES = ["orders", "inventory", "payments", "catalog", "auth"];
const SERVICE_NAMES = { orders: "Commandes", inventory: "Stocks", payments: "Paiements", catalog: "Catalogue", auth: "Authentification" };
const state = {
  token: sessionStorage.getItem("market-token") || "",
  refreshToken: sessionStorage.getItem("market-refresh") || "",
  email: sessionStorage.getItem("market-user") || "",
  page: "overview",
  orders: [],
  products: [],
  stock: new Map(),
  warehouses: [],
  health: {},
  previousMetrics: {},
  lastScrapeAt: 0,
  catalogLoadedAt: 0,
  loading: false
};

const $ = (selector) => document.querySelector(selector);
const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
})[char]);
const money = (cents) => new Intl.NumberFormat("fr-FR", { style: "currency", currency: "EUR" }).format((cents || 0) / 100);
const number = (value) => new Intl.NumberFormat("fr-FR").format(value || 0);
const orderStatuses = {
  paid: ["Payée", "paid"], preparing: ["En préparation", "preparing"],
  delivered: ["Livrée", "delivered"], payment_failed: ["Échec de paiement", "payment_failed"],
  cancelled: ["Annulée", "cancelled"], pending: ["En attente", "cancelled"]
};
const initials = (name) => String(name || "Client").trim().split(/\s+/).slice(0, 2).map((part) => part[0] || "").join("").toUpperCase();
const formatDate = (date) => date ? new Intl.DateTimeFormat("fr-FR", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }).format(new Date(date)) : "—";
const isToday = (value) => {
  const date = new Date(value), now = new Date();
  return date.getFullYear() === now.getFullYear() && date.getMonth() === now.getMonth() && date.getDate() === now.getDate();
};

async function request(path, options = {}, retried = false) {
  const headers = new Headers(options.headers || {});
  if (state.token) headers.set("Authorization", `Bearer ${state.token}`);
  if (options.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const response = await fetch(path, { ...options, headers });
  if (!response.ok) {
    let detail = `Erreur HTTP ${response.status}`;
    try {
      const body = await response.json();
      detail = typeof body.detail === "string" ? body.detail : detail;
    } catch { /* Response may be plain text. */ }
    if (response.status === 401 && !retried && await refreshAccess()) {
      return request(path, options, true);  // the access token had expired: retry once with the new one
    }
    if (response.status === 401) {
      clearSession();
      updateUser();
      showAuth("Votre session a expiré. Reconnectez-vous.");
    }
    throw new Error(detail);
  }
  return response;
}

function saveSession(data, email) {
  state.token = data.access_token;
  sessionStorage.setItem("market-token", state.token);
  if (data.refresh_token) {  // /refresh returns only a new access token
    state.refreshToken = data.refresh_token;
    sessionStorage.setItem("market-refresh", state.refreshToken);
  }
  if (email) {
    state.email = email;
    sessionStorage.setItem("market-user", email);
  }
}

function clearSession() {
  state.token = state.refreshToken = state.email = "";
  ["market-token", "market-refresh", "market-user"].forEach((key) => sessionStorage.removeItem(key));
}

let refreshing = null;  // parallel requests share one refresh call
function refreshAccess() {
  if (!state.refreshToken) return Promise.resolve(false);
  refreshing ??= fetch("/api/auth/refresh", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh_token: state.refreshToken })
  }).then(async (response) => {
    if (!response.ok) return false;
    saveSession(await response.json());
    return true;
  }).catch(() => false).finally(() => { refreshing = null; });
  return refreshing;
}

async function json(path, options) {
  return (await request(path, options)).json();
}

function decodeClaims(token) {
  const encoded = token.split(".")[1];
  if (!encoded) throw new Error("Format JWT invalide.");
  const normalized = encoded.replace(/-/g, "+").replace(/_/g, "/");
  const claims = JSON.parse(decodeURIComponent(Array.from(atob(normalized), (char) => `%${char.charCodeAt(0).toString(16).padStart(2, "0")}`).join("")));
  if (!claims.sub || !Array.isArray(claims.roles) || !claims.exp || claims.exp * 1000 <= Date.now()) {
    throw new Error("Le jeton est expiré ou ne contient pas les informations attendues.");
  }
  return claims;
}

function updateUser() {
  let claims = null;
  if (state.token) {
    try { claims = decodeClaims(state.token); } catch { /* The API will return the authoritative token error. */ }
  }
  const name = state.email || claims?.sub || "Équipe Market";
  const roles = claims?.roles?.join(", ") || "Non connecté";
  $("#user-name").textContent = name;
  $("#user-role").textContent = roles;
  $("#user-avatar").textContent = initials(name).slice(0, 1) || "M";
  $("#top-avatar").textContent = initials(name).slice(0, 1) || "M";
  $("#sidebar-status-text").textContent = state.token ? "Session active" : "Connexion en attente";
  $("#sidebar-status-dot").classList.toggle("offline", !state.token);
  $("#connection-notice").classList.toggle("hidden", Boolean(state.token));
}

function showAuth(error = "") {
  $("#auth-modal").classList.remove("hidden");
  $("#auth-error").textContent = error;
  $("#auth-error").classList.toggle("hidden", !error);
  $("#password-input").value = "";
  window.setTimeout(() => ($("#email-input").value ? $("#password-input") : $("#email-input")).focus(), 0);
}

function closeAuth() {
  $("#auth-modal").classList.add("hidden");
  $("#auth-error").classList.add("hidden");
}

function toast(message) {
  const element = $("#toast");
  element.textContent = message;
  element.classList.remove("hidden");
  window.clearTimeout(toast.timer);
  toast.timer = window.setTimeout(() => element.classList.add("hidden"), 3500);
}

function setPage(page) {
  state.page = page;
  document.querySelectorAll(".page-section").forEach((section) => section.classList.toggle("active", section.id === `page-${page}`));
  document.querySelectorAll(".nav-link").forEach((button) => button.classList.toggle("active", button.dataset.page === page));
  const titles = { overview: "Vue d’ensemble", orders: "Commandes", catalog: "Catalogue", logistics: "Logistique", observability: "Observabilité" };
  $("#breadcrumb-current").textContent = titles[page];
  $("#sidebar").classList.remove("open");
  if (!state.token) showAuth();
}

function statusBadge(status) {
  const [label, className] = orderStatuses[status] || [status || "Inconnu", "cancelled"];
  return `<span class="status-badge ${className}">${escapeHtml(label)}</span>`;
}

function orderRow(order, includeItems = false) {
  const amount = money(order.total_cents);
  const items = (order.items || []).reduce((total, item) => total + item.quantity, 0);
  return `<tr>
    <td><span class="table-ref">#MK-${String(order.id).padStart(5, "0")}</span></td>
    <td><div class="customer-cell"><span class="customer-avatar">${escapeHtml(initials(order.customer_name))}</span>${escapeHtml(order.customer_name || `Client #${order.customer_id}`)}</div></td>
    <td>${escapeHtml(formatDate(order.created_at))}</td>
    ${includeItems ? `<td>${items || "—"}</td>` : ""}
    <td>${escapeHtml(amount)}</td><td>${statusBadge(order.status)}</td>
    <td><button class="row-action" type="button" aria-label="Voir la commande ${order.id}" data-order-id="${order.id}">···</button></td>
  </tr>`;
}

function filteredOrders() {
  const term = ($("#order-search")?.value || "").trim().toLowerCase();
  const status = $("#order-filter")?.value || "";
  return state.orders.filter((order) => {
    const matchesText = !term || String(order.id).includes(term) || String(order.customer_name || "").toLowerCase().includes(term);
    return matchesText && (!status || order.status === status);
  });
}

function renderOrders() {
  const orders = filteredOrders();
  $("#orders-table").innerHTML = orders.length ? orders.map((order) => orderRow(order, true)).join("") : '<tr><td class="empty-row" colspan="7">Aucune commande ne correspond à ces critères.</td></tr>';
  $("#overview-orders").innerHTML = state.orders.length ? state.orders.slice(0, 5).map((order) => orderRow(order)).join("") : '<tr><td class="empty-row" colspan="6">Aucune commande récente à afficher.</td></tr>';
  $("#nav-order-count").textContent = number(state.orders.length);
  $("#orders-footer").textContent = `${number(orders.length)} commande${orders.length > 1 ? "s" : ""} affichée${orders.length > 1 ? "s" : ""} · maximum 200`;
}

function getTodayOrders() {
  return state.orders.filter((order) => isToday(order.created_at));
}

function renderOverview() {
  const orders = getTodayOrders();
  const successful = orders.filter((order) => ["paid", "preparing", "delivered"].includes(order.status));
  const revenue = successful.reduce((total, order) => total + order.total_cents, 0);
  $("#metric-orders").textContent = number(orders.length);
  $("#metric-revenue").textContent = money(revenue);
  $("#metric-basket").textContent = successful.length ? money(Math.round(revenue / successful.length)) : money(0);
  const full = state.warehouses.filter((warehouse) => warehouse.fill_rate >= .9).length;
  $("#metric-warehouses").textContent = state.warehouses.length ? `${full} / ${state.warehouses.length}` : "—";
  $("#chart-total").textContent = number(orders.length);

  const hours = Array.from({ length: 12 }, (_, index) => index + 8);
  const counts = hours.map((hour) => orders.filter((order) => new Date(order.created_at).getHours() === hour).length);
  const max = Math.max(1, ...counts);
  $("#orders-chart").innerHTML = hours.map((hour, index) => {
    const count = counts[index], height = Math.max(3, Math.round((count / max) * 86));
    return `<div class="bar-column" title="${hour} h : ${count} commande${count > 1 ? "s" : ""}"><span class="bar-value">${count || ""}</span><div class="bar-fill" style="height:${height}px;opacity:${count ? 1 : .24}"></div><span class="bar-label">${String(hour).padStart(2, "0")}h</span></div>`;
  }).join("");
  renderAlerts();
}

function renderAlerts() {
  const alerts = [];
  for (const warehouse of state.warehouses.filter((item) => item.fill_rate >= .9)) {
    alerts.push({ type: "danger", icon: "!", title: `Entrepôt proche de la saturation`, detail: `${warehouse.name} · ${Math.round(warehouse.fill_rate * 100)} % de remplissage` });
  }
  for (const product of state.products) {
    const stock = state.stock.get(product.productId);
    if (stock && stock.total <= 5) alerts.push({ type: stock.total === 0 ? "danger" : "warning", icon: stock.total === 0 ? "!" : "↓", title: stock.total === 0 ? "Produit en rupture de stock" : "Stock bientôt épuisé", detail: `${product.name} · ${stock.total} unité${stock.total === 1 ? "" : "s"} restante${stock.total === 1 ? "" : "s"}` });
  }
  for (const order of getTodayOrders().filter((item) => item.status === "payment_failed")) {
    alerts.push({ type: "warning", icon: "€", title: "Échec de paiement", detail: `Commande #MK-${String(order.id).padStart(5, "0")}` });
  }
  $("#alert-count").textContent = alerts.length;
  $("#alert-list").innerHTML = alerts.length
    ? alerts.slice(0, 5).map((alert) => `<div class="alert-item"><span class="alert-indicator ${alert.type}">${alert.icon}</span><div class="alert-copy"><strong>${escapeHtml(alert.title)}</strong><span>${escapeHtml(alert.detail)}</span></div></div>`).join("")
    : '<div class="empty-state compact">Aucune alerte détectée à partir des données disponibles.</div>';
}

function renderCatalog() {
  const term = ($("#product-search")?.value || "").trim().toLowerCase();
  const products = state.products.filter((product) => `${product.name} ${product.category} ${product.productId}`.toLowerCase().includes(term));
  $("#catalog-count").textContent = `${number(products.length)} produit${products.length > 1 ? "s" : ""}`;
  $("#catalog-table").innerHTML = products.length ? products.map((product) => {
    const stock = state.stock.get(product.productId);
    const stockValue = stock ? number(stock.total) : "—";
    const properties = product.attributes && Object.keys(product.attributes).length ? JSON.stringify(product.attributes) : "Aucune caractéristique";
    const icon = initials(product.category).slice(0, 1) || "P";
    return `<tr>
      <td><div class="product-name-cell"><span class="product-swatch">${escapeHtml(icon)}</span><div><div class="product-title">${escapeHtml(product.name)}</div><div class="product-subtitle">ID ${escapeHtml(product.productId)}</div></div></div></td>
      <td>${escapeHtml(product.category)}</td><td><span class="properties" title="${escapeHtml(properties)}">${escapeHtml(properties)}</span></td>
      <td>${escapeHtml(money(product.priceCents))}</td><td><span class="stock-number ${stock && stock.total <= 5 ? "low" : ""}">${escapeHtml(stockValue)}</span></td>
      <td><span class="status-badge ${product.status === "active" ? "active" : "inactive"}">${product.status === "active" ? "Actif" : "Archivé"}</span></td>
    </tr>`;
  }).join("") : '<tr><td class="empty-row" colspan="6">Aucun produit trouvé.</td></tr>';
}

function renderLogistics() {
  const warehouses = state.warehouses;
  const totalCapacity = warehouses.reduce((sum, item) => sum + item.capacity_units, 0);
  const totalStored = warehouses.reduce((sum, item) => sum + item.stored_units, 0);
  const fill = totalCapacity ? Math.round(totalStored / totalCapacity * 100) : 0;
  const saturated = warehouses.filter((item) => item.fill_rate >= .9).length;
  $("#logistics-summary").innerHTML = [
    ["Entrepôts actifs", warehouses.length ? number(warehouses.length) : "—", "Données du service Inventory"],
    ["Unités stockées", number(totalStored), `sur ${number(totalCapacity)} unités de capacité`],
    ["Remplissage global", warehouses.length ? `${fill} %` : "—", `${saturated} entrepôt${saturated > 1 ? "s" : ""} à surveiller`]
  ].map(([label, value, caption]) => `<article class="summary-card"><span>${label}</span><strong>${value}</strong><small>${caption}</small></article>`).join("");
  $("#warehouse-grid").innerHTML = warehouses.length ? warehouses.map((item) => {
    const percent = Math.min(100, Math.round(item.fill_rate * 100));
    const severity = percent >= 95 ? "danger" : percent >= 90 ? "warning" : "";
    const status = percent >= 90 ? "near_saturation" : "normal";
    return `<article class="warehouse-card"><div class="warehouse-head"><div><div class="warehouse-id">ENTREPÔT #${item.id}</div><h3>${escapeHtml(item.name)}</h3></div><span class="status-badge ${status}">${status === "normal" ? "Normal" : "Saturation proche"}</span></div><div class="warehouse-stats"><strong>${percent} %</strong><span>${number(item.stored_units)} / ${number(item.capacity_units)} unités</span></div><div class="progress-track"><div class="progress-fill ${severity}" style="width:${percent}%"></div></div><div class="warehouse-foot"><span>Capacité utilisée</span><span>${number(Math.max(0, item.capacity_units - item.stored_units))} unités disponibles</span></div></article>`;
  }).join("") : '<div class="panel empty-state">Aucun entrepôt à afficher. Vérifiez le service Inventory et votre rôle analyst ou logistics.</div>';
}

function parsePrometheus(text) {
  const metrics = { requests: 0, errors: 0, latencySum: 0, latencyCount: 0 };
  for (const line of text.split("\n")) {
    let match = line.match(/^http_requests_total\{([^}]*)\}\s+([\d.eE+-]+)/);
    if (match) {
      const labels = Object.fromEntries([...match[1].matchAll(/(\w+)="([^"]*)"/g)].map((item) => [item[1], item[2]]));
      const value = Number(match[2]);
      if (labels.service && labels.status) {
        metrics.requests += value;
        if (Number(labels.status) >= 500) metrics.errors += value;
      }
      continue;
    }
    match = line.match(/^http_request_duration_seconds_(sum|count)\{([^}]*)\}\s+([\d.eE+-]+)/);
    if (!match) continue;
    const labels = Object.fromEntries([...match[2].matchAll(/(\w+)="([^"]*)"/g)].map((item) => [item[1], item[2]]));
    if (!labels.service) continue;
    if (match[1] === "sum") metrics.latencySum += Number(match[3]);
    else metrics.latencyCount += Number(match[3]);
  }
  return metrics;
}

function renderHealth() {
  const healthy = Object.values(state.health).filter((item) => item.status === "ok").length;
  const total = SERVICES.length;
  const metricsAvailable = Object.values(state.health).filter((item) => item.metrics).length;
  $("#health-summary").innerHTML = [
    [`${healthy} / ${total}`, "Services opérationnels", "Vérification /health"],
    [`${metricsAvailable} / ${total}`, "Sources de métriques", "Endpoint Prometheus disponible"],
    ["Non connecté", "Pipeline Kafka", "Aucun service de streaming configuré"],
    ["Non connecté", "Grafana & traces", "Non configurés dans Docker Compose"]
  ].map(([value, title, caption]) => `<article class="summary-card"><strong>${value}</strong><span>${title}</span><small>${caption}</small></article>`).join("");
  $("#health-table").innerHTML = SERVICES.map((service) => {
    const item = state.health[service] || {};
    const metric = item.metrics;
    const average = metric?.latencyCount ? `${(metric.latencySum / metric.latencyCount * 1000).toFixed(1)} ms` : "—";
    const rate = metric?.rate == null ? "—" : `${metric.rate.toFixed(2)} / s`;
    const errors = metric?.errorRate == null ? "—" : `${metric.errorRate.toFixed(1)} %`;
    const status = item.status === "ok" ? ["Opérationnel", "normal"] : item.status === "degraded" ? ["Dégradé", "near_saturation"] : ["Indisponible", "payment_failed"];
    return `<tr><td><div class="health-service"><span class="service-icon">${SERVICE_NAMES[service][0]}</span><div><strong>${SERVICE_NAMES[service]}</strong><small>${service}</small></div></div></td><td><span class="status-badge ${status[1]}">${status[0]}</span></td><td>${average}</td><td>${rate}</td><td>${errors}</td><td>${item.error ? `<span class="metric-muted" title="${escapeHtml(item.error)}">Détail</span>` : ""}</td></tr>`;
  }).join("");
}

function showDataError(message) {
  $("#global-error").textContent = message;
  $("#global-error").classList.remove("hidden");
}

async function loadOrders() {
  state.orders = await json("/api/orders/orders?limit=200");
  renderOrders();
  renderOverview();
}

async function loadWarehouses() {
  state.warehouses = await json("/api/inventory/warehouses");
  renderLogistics();
  renderOverview();
}

async function loadCatalog(force = false) {
  if (!force && state.products.length && Date.now() - state.catalogLoadedAt < 120000) return;
  const result = await json("/api/catalog/graphql", {
    method: "POST",
    body: JSON.stringify({ query: "{ products(limit: 40) { productId name category priceCents attributes status tags } }" })
  });
  if (result.errors?.length) throw new Error(result.errors[0].message || "Erreur de lecture du catalogue.");
  state.products = result.data?.products || [];
  state.catalogLoadedAt = Date.now();
  const stocks = await Promise.allSettled(state.products.map((product) => json(`/api/inventory/inventory/${product.productId}`)));
  state.products.forEach((product, index) => {
    const outcome = stocks[index];
    if (outcome.status === "fulfilled") state.stock.set(product.productId, outcome.value);
    else state.stock.delete(product.productId);
  });
  renderCatalog();
  renderAlerts();
}

async function loadHealth() {
  const now = Date.now(), elapsed = state.lastScrapeAt ? Math.max((now - state.lastScrapeAt) / 1000, .001) : 0;
  const responses = await Promise.all(SERVICES.map(async (service) => {
    try {
      const [healthResponse, metricsResponse] = await Promise.all([
        fetch(`/health/${service}`),
        fetch(`/metrics/${service}`)
      ]);
      if (!healthResponse.ok) throw new Error(`health HTTP ${healthResponse.status}`);
      const health = await healthResponse.json();
      let metric = null;
      if (metricsResponse.ok) {
        const current = parsePrometheus(await metricsResponse.text());
        const previous = state.previousMetrics[service];
        metric = { ...current, rate: previous && elapsed ? Math.max(0, (current.requests - previous.requests) / elapsed) : null,
          errorRate: previous && current.requests > previous.requests ? Math.max(0, (current.errors - previous.errors) / (current.requests - previous.requests) * 100) : null };
        state.previousMetrics[service] = current;
      }
      return [service, { status: health.status, metrics: metric, error: health.error || null }];
    } catch (error) {
      return [service, { status: "unavailable", metrics: null, error: error.message }];
    }
  }));
  state.lastScrapeAt = now;
  state.health = Object.fromEntries(responses);
  renderHealth();
  const allHealthy = SERVICES.every((service) => state.health[service].status === "ok");
  $("#sidebar-status-dot").classList.toggle("offline", !allHealthy);
  $("#sidebar-status-text").textContent = state.token ? (allHealthy ? "Services opérationnels" : "Service dégradé") : "Connexion en attente";
}

async function refreshAll(forceCatalog = false) {
  if (!state.token || state.loading) return;
  state.loading = true;
  $("#refresh-button").classList.add("is-spinning");
  $("#global-error").classList.add("hidden");
  const results = await Promise.allSettled([loadOrders(), loadWarehouses(), loadCatalog(forceCatalog), loadHealth()]);
  const failures = results.filter((result) => result.status === "rejected");
  if (failures.length) {
    const message = failures.map((result) => result.reason.message).join(" · ");
    showDataError(`Certaines données n’ont pas pu être chargées : ${message}`);
  }
  $("#last-updated").textContent = `Mis à jour à ${new Intl.DateTimeFormat("fr-FR", { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(new Date())}`;
  state.loading = false;
  $("#refresh-button").classList.remove("is-spinning");
}

function logout() {
  clearSession();
  state.orders = [];
  state.products = [];
  state.stock.clear();
  state.warehouses = [];
  state.catalogLoadedAt = 0;
  updateUser();
  renderOrders();
  renderCatalog();
  renderLogistics();
  renderOverview();
  showAuth();
}

document.querySelectorAll("[data-page]").forEach((button) => button.addEventListener("click", () => setPage(button.dataset.page)));
document.querySelectorAll("[data-refresh]").forEach((button) => button.addEventListener("click", () => refreshAll(true)));
document.querySelectorAll("[data-open-auth]").forEach((button) => button.addEventListener("click", () => showAuth()));
$("#refresh-button").addEventListener("click", () => refreshAll(true));
$("#user-menu").addEventListener("click", logout);
$("#top-user").addEventListener("click", logout);
$("#mobile-menu").addEventListener("click", () => $("#sidebar").classList.toggle("open"));
$("#close-auth").addEventListener("click", closeAuth);
$("#order-search").addEventListener("input", renderOrders);
$("#order-filter").addEventListener("change", renderOrders);
$("#product-search").addEventListener("input", renderCatalog);
$("#auth-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const email = $("#email-input").value.trim().toLowerCase();
  const submit = $("#auth-submit");
  submit.disabled = true;
  try {
    const response = await fetch("/api/auth/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password: $("#password-input").value })
    });
    if (response.status === 401) throw new Error("Email ou mot de passe incorrect.");
    if (response.status === 422) throw new Error("Email ou mot de passe invalide.");
    if (!response.ok) throw new Error("Le service d’authentification est indisponible.");
    const data = await response.json();
    decodeClaims(data.access_token);
    saveSession(data, email);
    $("#password-input").value = "";
    updateUser();
    closeAuth();
    refreshAll(true);
  } catch (error) {
    $("#auth-error").textContent = error instanceof TypeError ? "Le service d’authentification est injoignable." : error.message;
    $("#auth-error").classList.remove("hidden");
  } finally {
    submit.disabled = false;
  }
});
$("#auth-modal").addEventListener("click", (event) => {
  if (event.target === $("#auth-modal") && state.token) closeAuth();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && state.token) closeAuth();
});

document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-order-id]");
  if (!button || !state.token) return;
  try {
    const order = await json(`/api/orders/orders/${button.dataset.orderId}`);
    const lines = (order.items || []).map((item) => `${item.quantity} × produit ${item.product_id} · ${money(item.unit_price_cents)}`).join("\n");
    toast(`Commande #MK-${String(order.id).padStart(5, "0")} · ${statusLabel(order.status)} · ${lines || "Détail indisponible"}`);
  } catch (error) {
    toast(`Impossible de charger la commande : ${error.message}`);
  }
});

function statusLabel(status) {
  return orderStatuses[status]?.[0] || status;
}

$("#today-label").textContent = new Intl.DateTimeFormat("fr-FR", { weekday: "long", day: "numeric", month: "long" }).format(new Date()).toLocaleUpperCase("fr-FR");
updateUser();
renderOrders();
renderCatalog();
renderLogistics();
renderOverview();
renderHealth();
if (state.token) refreshAll(true);
else showAuth();
window.setInterval(() => refreshAll(false), 30000);
