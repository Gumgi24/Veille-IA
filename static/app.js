/* Veille IA — single-page frontend (vanilla JS + ECharts). */
"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const app = $("#app");

// Visitor mode: the page was served from /share/<token> — read-only, no admin API.
const SHARE = (location.pathname.match(/^\/share\/([A-Za-z0-9_-]+)/) || [])[1] || null;
const campApi = (id, path) => (SHARE ? `/api/share/${SHARE}${path}` : `/api/campaigns/${id}${path}`);

const MODEL_LABELS = {
  openai: "OpenAI (GPT search)",
  gemini: "Gemini",
  anthropic: "Claude",
  xai: "Grok",
};

// ------------------------------------------------------------- utilities

async function api(path, options = {}) {
  if (options.json !== undefined) {
    options.body = JSON.stringify(options.json);
    options.headers = { "Content-Type": "application/json", ...options.headers };
    delete options.json;
  }
  const resp = await fetch(path, options);
  if (!resp.ok) {
    let detail = resp.statusText;
    try { detail = (await resp.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  const type = resp.headers.get("content-type") || "";
  return type.includes("json") ? resp.json() : resp.text();
}

function toast(message, isError = false) {
  const el = $("#toast");
  el.textContent = message;
  el.className = "toast show" + (isError ? " error" : "");
  clearTimeout(el._t);
  el._t = setTimeout(() => (el.className = "toast"), 3500);
}

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function fmtDate(iso) {
  if (!iso) return "—";
  const d = new Date(iso.includes("T") || iso.includes(" ") ? iso.replace(" ", "T") + (iso.endsWith("Z") || iso.includes("+") ? "" : "Z") : iso);
  if (isNaN(d)) return iso;
  return d.toLocaleString("fr-FR", { dateStyle: "short", timeStyle: "short" });
}

const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const SERIES = () => [1, 2, 3, 4, 5, 6, 7, 8].map((i) => cssVar(`--s${i}`));

/* lighten a hex color toward the chart surface (for sunburst children) */
function shade(hex, factor) {
  const surface = cssVar("--surface") || "#fcfcfb";
  const p = (h) => {
    h = h.replace("#", "");
    if (h.length === 3) h = h.split("").map((c) => c + c).join("");
    return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16));
  };
  const a = p(hex), b = p(surface);
  const mix = a.map((v, i) => Math.round(v + (b[i] - v) * factor));
  return "#" + mix.map((v) => v.toString(16).padStart(2, "0")).join("");
}

// ------------------------------------------------------------- state / router

const state = {
  view: "campaigns", campaignId: null, subtab: "dashboard",
  dashFilters: {}, resFilters: {}, resOffset: 0,
  sunMode: "source",   // sunburst hierarchy: "source" | "prompt"
  pivotMode: "abs",    // TCD values: "abs" | "pct"
  upSort: "categorie", // unique prompts sort column
};
let pollTimer = null;

$("#main-tabs").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-view]");
  if (!btn) return;
  $$("#main-tabs button").forEach((b) => b.classList.toggle("active", b === btn));
  state.view = btn.dataset.view;
  state.campaignId = null;
  render();
});

function syncHash() {
  const h = state.view === "campaigns" && state.campaignId
    ? `#c/${state.campaignId}/${state.subtab}` : `#${state.view}`;
  if (location.hash !== h) history.replaceState(null, "", h);
}

function render() {
  clearInterval(pollTimer);
  syncHash();
  $$("#main-tabs button").forEach((b) => b.classList.toggle("active", b.dataset.view === state.view));
  if (state.view === "campaigns" && state.campaignId) return renderCampaignDetail();
  if (state.view === "campaigns") return renderCampaigns();
  if (state.view === "categories") return renderCategories();
  if (state.view === "settings") return renderSettings();
}

// ============================================================= campaigns list

async function renderCampaigns() {
  app.innerHTML = `<div class="row between" style="margin-bottom:16px">
      <h2 style="margin:0">Campagnes</h2>
      <button class="primary" id="new-campaign">+ Nouvelle campagne</button>
    </div><div class="camp-grid" id="camp-grid"><div class="empty">Chargement…</div></div>`;
  $("#new-campaign").onclick = () => openCampaignDialog(null);

  const campaigns = await api("/api/campaigns");
  const grid = $("#camp-grid");
  if (!campaigns.length) {
    grid.innerHTML = `<div class="empty">Aucune campagne. Créez-en une pour commencer.</div>`;
    return;
  }
  grid.innerHTML = campaigns.map((c) => `
    <div class="camp" data-id="${c.id}">
      <div class="row between">
        <div class="name">${esc(c.name)}</div>
        <span class="badge ${c.running ? "running" : c.status}">
          <span class="dot"></span>${c.running ? "en cours" : c.status === "active" ? "active" : c.status === "paused" ? "en pause" : "archivée"}
        </span>
      </div>
      <div class="meta">
        <span>${c.n_prompts} prompts</span>
        <span>${c.n_results} résultats</span>
        <span>${(c.models || []).map((m) => MODEL_LABELS[m] || m).join(", ") || "aucun modèle"}</span>
      </div>
      <div class="meta">
        <span>Dernier run : ${fmtDate(c.last_run)}</span>
        <span>Prochain : ${c.next_run ? fmtDate(c.next_run) : "—"}</span>
      </div>
    </div>`).join("");
  $$(".camp", grid).forEach((el) =>
    el.addEventListener("click", () => { state.campaignId = +el.dataset.id; state.subtab = "dashboard"; render(); }));

  const anyRunning = campaigns.some((c) => c.running);
  if (anyRunning) pollTimer = setInterval(renderCampaigns, 5000);
}

// ------------------------------------------------------------- campaign dialog

async function openCampaignDialog(campaign) {
  const dialog = $("#campaign-dialog");
  const form = $("#campaign-form");
  $("#campaign-dialog-title").textContent = campaign ? "Modifier la campagne" : "Nouvelle campagne";

  const sets = await api("/api/category_sets");
  const select = form.elements.category_set_id;
  select.innerHTML = `<option value="">— aucun —</option>` +
    sets.map((s) => `<option value="${s.id}">${esc(s.name)} (${s.n_domains} domaines)</option>`).join("");

  form.elements.name.value = campaign?.name || "";
  form.elements.schedule_time.value = campaign?.schedule_time || "13:40";
  form.elements.interval_days.value = campaign?.interval_days || 1;
  form.elements.start_date.value = campaign?.start_date || new Date().toISOString().slice(0, 10);
  form.elements.end_date.value = campaign?.end_date || "";
  select.value = campaign?.category_set_id || "";
  $$("input[name=models]", form).forEach((cb) =>
    (cb.checked = campaign ? campaign.models.includes(cb.value) : true));

  form.onsubmit = async (e) => {
    e.preventDefault();
    const body = {
      name: form.elements.name.value,
      models: $$("input[name=models]:checked", form).map((cb) => cb.value),
      schedule_time: form.elements.schedule_time.value || null,
      interval_days: +form.elements.interval_days.value || 1,
      start_date: form.elements.start_date.value || null,
      end_date: form.elements.end_date.value || null,
      category_set_id: select.value ? +select.value : null,
    };
    try {
      if (campaign) {
        await api(`/api/campaigns/${campaign.id}`, { method: "PUT", json: body });
        toast("Campagne mise à jour");
      } else {
        const { id } = await api("/api/campaigns", { method: "POST", json: body });
        toast("Campagne créée — importez maintenant vos prompts");
        state.campaignId = id;
        state.subtab = "prompts";
      }
      dialog.close();
      render();
    } catch (err) { toast(err.message, true); }
  };
  dialog.showModal();
}

// ============================================================= campaign detail

async function renderCampaignDetail() {
  const id = state.campaignId;
  let c;
  try { c = await api(`/api/campaigns/${id}`); }
  catch { state.campaignId = null; return render(); }

  app.innerHTML = `
    <div class="row between" style="margin-bottom:14px">
      <div class="row">
        <button id="back">← Campagnes</button>
        <h2 style="margin:0">${esc(c.name)}</h2>
        <span class="badge ${c.status}"><span class="dot"></span>${c.status === "active" ? "active" : c.status === "paused" ? "en pause" : "archivée"}</span>
      </div>
      <div class="row">
        <span class="small muted">Prochain run : ${c.next_run ? fmtDate(c.next_run) : "—"}</span>
        <button id="share-link" title="Copier l'URL de consultation publique (lecture seule)">🔗 Lien visiteur</button>
        <button id="share-rotate" class="small" title="Révoquer le lien actuel et en générer un nouveau">↻</button>
        <button id="edit-camp">Configurer</button>
        <button id="toggle-camp">${c.status === "active" ? "Mettre en pause" : "Réactiver"}</button>
        <a class="btn" href="/api/campaigns/${id}/export.csv" download>⬇ Export CSV</a>
        <button class="primary" id="run-now">▶ Lancer maintenant</button>
      </div>
    </div>
    <div class="subtabs" id="subtabs">
      <button data-t="dashboard">Dashboard</button>
      <button data-t="results">Résultats</button>
      <button data-t="prompts">Prompts (${c.n_prompts})</button>
      <button data-t="runs">Runs &amp; erreurs</button>
    </div>
    <div id="subview"></div>`;

  $("#back").onclick = () => { state.campaignId = null; render(); };
  const showShareLink = async (token) => {
    const url = `${location.origin}/share/${token}`;
    try { await navigator.clipboard.writeText(url); toast("Lien visiteur copié : " + url); }
    catch { prompt("Lien visiteur (lecture seule) :", url); }
  };
  $("#share-link").onclick = () => showShareLink(c.share_token);
  $("#share-rotate").onclick = async () => {
    if (!confirm("Révoquer le lien visiteur actuel ? L'ancien lien cessera de fonctionner.")) return;
    const r = await api(`/api/campaigns/${id}/share/rotate`, { method: "POST" });
    showShareLink(r.share_token);
    c.share_token = r.share_token;
  };
  $("#edit-camp").onclick = () => openCampaignDialog(c);
  $("#toggle-camp").onclick = async () => {
    await api(`/api/campaigns/${id}/status/${c.status === "active" ? "paused" : "active"}`, { method: "POST" });
    render();
  };
  $("#run-now").onclick = async () => {
    try {
      await api(`/api/campaigns/${id}/run`, { method: "POST" });
      toast("Run lancé");
      state.subtab = "runs";
      renderSubtab(id);
    } catch (err) { toast(err.message, true); }
  };
  $$("#subtabs button").forEach((b) => {
    b.classList.toggle("active", b.dataset.t === state.subtab);
    b.onclick = () => { state.subtab = b.dataset.t; state.resOffset = 0; renderCampaignDetail(); };
  });
  renderSubtab(id);
}

function renderSubtab(id) {
  const el = $("#subview");
  if (state.subtab === "dashboard") return renderDashboard(id, el);
  if (state.subtab === "results") return renderResults(id, el);
  if (state.subtab === "prompts") return renderPrompts(id, el);
  if (state.subtab === "runs") return renderRuns(id, el);
}

// ------------------------------------------------------------- dashboard

function filterBar(options, filters, onChange) {
  const select = (name, label, values) => `
    <label class="field">${label}
      <select data-f="${name}">
        <option value="">Tous</option>
        ${values.map((v) => `<option value="${esc(v)}" ${filters[name] === v ? "selected" : ""}>${esc(String(v).slice(0, 80))}</option>`).join("")}
      </select>
    </label>`;
  const html = `
    ${select("modele", "Modèle", options.modeles || [])}
    ${select("langue", "Langue", options.langues || [])}
    ${select("prompt_categorie", "Cat. de prompt", options.prompt_categories || [])}
    ${select("prompt", "Prompt", options.prompts || [])}
    ${select("categorie", "Cat. de source", options.categories || [])}
    <button class="small" data-reset>Réinitialiser</button>`;
  return { html, wire(container) {
    $$("select[data-f]", container).forEach((s) =>
      s.addEventListener("change", () => { filters[s.dataset.f] = s.value || undefined; onChange(); }));
    $("[data-reset]", container)?.addEventListener("click", () => {
      Object.keys(filters).forEach((k) => delete filters[k]); onChange();
    });
  }};
}

const qs = (obj) => {
  const p = new URLSearchParams();
  Object.entries(obj).forEach(([k, v]) => v !== undefined && v !== "" && p.set(k, v));
  const s = p.toString();
  return s ? "?" + s : "";
};

async function renderDashboard(id, el) {
  el.innerHTML = `<div class="empty">Chargement…</div>`;
  const d = await api(campApi(id, `/dashboard${qs(state.dashFilters)}`));
  const palette = SERIES();
  // Stable color assignment: source categories keep their hue across filters.
  const categories = [...new Set(d.sunburst.map((r) => r.categorie))].sort((a, b) => a.localeCompare(b, "fr"));
  const promptCats = [...new Set(d.sunburst.map((r) => r.prompt_categorie))].sort((a, b) => a.localeCompare(b, "fr"));
  const colorOf = Object.fromEntries(categories.map((cat, i) => [cat, palette[i % palette.length]]));
  const colorOfPrompt = Object.fromEntries(promptCats.map((cat, i) => [cat, palette[i % palette.length]]));

  const options = { ...d.options, categories: [...new Set(d.by_category.map((r) => r.categorie))] };
  const fb = filterBar(options, state.dashFilters, () => renderDashboard(id, el));
  const pivotExportQs = (mode) => qs({ ...state.dashFilters, mode });

  el.innerHTML = `
    <div class="filters">${fb.html}</div>
    <div class="tiles" style="margin-bottom:16px">
      <div class="tile"><div class="v">${d.total_sources}</div><div class="l">sources citées</div></div>
      <div class="tile"><div class="v">${d.by_domain.length}</div><div class="l">domaines distincts</div></div>
      <div class="tile"><div class="v">${d.by_category.length}</div><div class="l">catégories</div></div>
      <div class="tile"><div class="v">${d.by_url.length}</div><div class="l">URLs distinctes</div></div>
    </div>
    <div class="card">
      <div class="row between" style="margin-bottom:4px">
        <h2 style="margin:0">Répartition des sources</h2>
        <label class="field" style="flex-direction:row;align-items:center;gap:8px">Hiérarchie
          <select id="sun-mode">
            <option value="source" ${state.sunMode === "source" ? "selected" : ""}>Catégorie de source → Domaine → URL</option>
            <option value="prompt" ${state.sunMode === "prompt" ? "selected" : ""}>Catégorie de prompt → Catégorie de source → Domaine</option>
          </select>
        </label>
      </div>
      <div id="sunburst"></div>
    </div>
    <div class="dash-grid">
      <div class="card">
        <h2>Catégories de sources</h2>
        <div class="table-wrap scroll-table">
          <table><thead><tr><th>Catégorie</th><th class="num">Citations</th></tr></thead>
          <tbody>${d.by_category.map((r) => `
            <tr><td><span class="chip"><span class="sw" style="background:${colorOf[r.categorie] || "#888"}"></span>${esc(r.categorie)}</span></td>
            <td class="num">${r.n}</td></tr>`).join("")}</tbody></table>
        </div>
        <h3 id="uncat-title"></h3>
        <div id="uncat"></div>
      </div>
      <div class="card">
        <h2>Domaines</h2>
        <div class="table-wrap scroll-table">
          <table><thead><tr><th>#</th><th>Domaine</th><th class="num">Citations</th></tr></thead>
          <tbody>${d.by_domain.map((r, i) => `
            <tr><td class="muted">${i + 1}</td><td>${esc(r.domaine)}</td><td class="num">${r.n}</td></tr>`).join("")}</tbody></table>
        </div>
      </div>
    </div>
    <div class="card">
      <h2>URLs les plus citées</h2>
      <div class="table-wrap scroll-table">
        <table><thead><tr><th>URL</th><th>Domaine</th><th class="num">Citations</th></tr></thead>
        <tbody>${d.by_url.slice(0, 150).map((r) => `
          <tr><td class="clip"><a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.url)}</a></td>
          <td class="small">${esc(r.domaine || "")}</td>
          <td class="num">${r.n}</td></tr>`).join("")}</tbody></table>
      </div>
    </div>
    <div class="card">
      <div class="row between">
        <h2 style="margin:0">TCD — Catégorie de source × Prompt</h2>
        <div class="row" style="gap:8px">
          <select id="pivot-mode">
            <option value="abs" ${state.pivotMode === "abs" ? "selected" : ""}>Valeurs absolues</option>
            <option value="pct" ${state.pivotMode === "pct" ? "selected" : ""}>% du prompt</option>
          </select>
          <a class="btn small" href="${campApi(id, "/pivot.csv" + pivotExportQs("abs"))}" download>⬇ CSV absolu</a>
          <a class="btn small" href="${campApi(id, "/pivot.csv" + pivotExportQs("pct"))}" download>⬇ CSV %</a>
        </div>
      </div>
      <div class="table-wrap" id="pivot" style="margin-top:10px"></div>
    </div>`;

  fb.wire(el);
  $("#sun-mode").onchange = (e) => {
    state.sunMode = e.target.value;
    drawSunburst(d.sunburst, state.sunMode === "source" ? colorOf : colorOfPrompt);
  };
  $("#pivot-mode").onchange = (e) => { state.pivotMode = e.target.value; drawPivot(d.pivot, colorOf); };
  drawSunburst(d.sunburst, state.sunMode === "source" ? colorOf : colorOfPrompt);
  drawPivot(d.pivot, colorOf);
  loadUncategorized(id);
}

let sunburstChart = null;

function drawSunburst(rows, colorOf) {
  const container = $("#sunburst");
  if (sunburstChart) { sunburstChart.dispose(); sunburstChart = null; }
  if (!rows.length) { container.innerHTML = `<div class="empty">Aucune donnée</div>`; return; }
  container.innerHTML = "";

  // Hierarchy: 3 keys picked from the aggregated rows depending on the mode.
  const keys = state.sunMode === "prompt"
    ? ["prompt_categorie", "categorie", "domaine"]
    : ["categorie", "domaine", "url"];
  const urlOuterRing = keys[2] === "url";

  // rows -> nested {name -> {name -> {name -> value}}}
  const tree = {};
  rows.forEach((r) => {
    const l1 = (tree[r[keys[0]]] ??= {});
    const l2 = (l1[r[keys[1]]] ??= {});
    l2[r[keys[2]]] = (l2[r[keys[2]]] || 0) + r.n;
  });
  const sumL2 = (l2) => Object.values(l2).reduce((s, v) => s + v, 0);
  const sumL1 = (l1) => Object.values(l1).reduce((s, l2) => s + sumL2(l2), 0);

  const data = Object.entries(tree)
    .sort((a, b) => sumL1(b[1]) - sumL1(a[1]))
    .map(([top, l1]) => ({
      name: top,
      itemStyle: { color: colorOf[top] || "#888" },
      children: Object.entries(l1)
        .sort((a, b) => sumL2(b[1]) - sumL2(a[1]))
        .map(([mid, l2], i, arr) => {
          const midColor = shade(colorOf[top] || "#888", 0.18 + 0.35 * (i / Math.max(arr.length - 1, 1)));
          return {
            name: mid,
            itemStyle: { color: midColor },
            children: Object.entries(l2)
              .sort((a, b) => b[1] - a[1])
              .map(([leaf, value]) => ({
                name: leaf, value,
                itemStyle: { color: shade(midColor, 0.35) },
              })),
          };
        }),
    }));

  sunburstChart = echarts.init(container, null, { renderer: "canvas" });
  const ink = cssVar("--ink"), surface = cssVar("--surface");
  sunburstChart.setOption({
    tooltip: {
      backgroundColor: surface, borderColor: cssVar("--grid"),
      textStyle: { color: ink }, confine: true,
      extraCssText: "max-width:520px;white-space:normal;word-break:break-all",
      formatter: (p) => {
        const path = (p.treePathInfo || []).slice(1).map((x) => esc(x.name)).join(" › ");
        return `${path}<br><b>${p.value}</b> citation(s)`;
      },
    },
    series: [{
      type: "sunburst",
      data,
      radius: ["12%", "96%"],
      sort: null,
      emphasis: { focus: "ancestor" },
      itemStyle: { borderColor: surface, borderWidth: 2, borderRadius: 3 },
      levels: [
        {},
        { r0: "12%", r: "42%", label: { rotate: "tangential", color: "#fff", minAngle: 6, fontSize: 12 } },
        { r0: "42%", r: urlOuterRing ? "72%" : "78%",
          label: { rotate: "tangential", minAngle: 5, color: "#fff", fontSize: 10.5 } },
        urlOuterRing
          ? { r0: "72%", r: "96%", label: { show: false } }          // URLs : identité via tooltip/clic
          : { r0: "78%", r: "96%", label: { rotate: "tangential", minAngle: 6, color: "#fff", fontSize: 10 } },
      ],
    }],
  });
  if (urlOuterRing) {
    sunburstChart.on("click", (p) => {
      if (p.treePathInfo && p.treePathInfo.length === 4) window.open(p.name, "_blank", "noopener");
    });
  }
  new ResizeObserver(() => sunburstChart && sunburstChart.resize()).observe(container);
}

function drawPivot(rows, colorOf) {
  const container = $("#pivot");
  if (!rows.length) { container.innerHTML = `<div class="empty">Aucune donnée</div>`; return; }
  const pct = state.pivotMode === "pct";
  const prompts = [...new Set(rows.map((r) => r.prompt))];
  const cats = [...new Set(rows.map((r) => r.categorie))];
  const value = {}, catTotal = {}, colTotal = {};
  rows.forEach((r) => {
    value[r.categorie + "||" + r.prompt] = r.n;
    catTotal[r.categorie] = (catTotal[r.categorie] || 0) + r.n;
    colTotal[r.prompt] = (colTotal[r.prompt] || 0) + r.n;
  });
  cats.sort((a, b) => catTotal[b] - catTotal[a]);
  const cell = (cat, p) => {
    const n = value[cat + "||" + p];
    if (n === undefined) return "–";
    return pct ? (100 * n / colTotal[p]).toFixed(1) + " %" : n;
  };
  container.innerHTML = `
    <table>
      <thead><tr><th>Catégorie</th>${prompts.map((p) => `<th class="num" title="${esc(p)}">${esc(p.length > 48 ? p.slice(0, 48) + "…" : p)}</th>`).join("")}${pct ? "" : `<th class="num">Total</th>`}</tr></thead>
      <tbody>${cats.map((cat) => `
        <tr>
          <td><span class="chip"><span class="sw" style="background:${colorOf[cat] || "#888"}"></span>${esc(cat)}</span></td>
          ${prompts.map((p) => `<td class="num">${cell(cat, p)}</td>`).join("")}
          ${pct ? "" : `<td class="num"><b>${catTotal[cat]}</b></td>`}
        </tr>`).join("")}
        <tr>
          <td><b>Total</b></td>
          ${prompts.map((p) => `<td class="num"><b>${pct ? "100 %" : colTotal[p]}</b></td>`).join("")}
          ${pct ? "" : `<td class="num"><b>${Object.values(catTotal).reduce((s, v) => s + v, 0)}</b></td>`}
        </tr>
      </tbody>
    </table>`;
}

async function loadUncategorized(id) {
  if (SHARE) return; // visitor: read-only, no categorization
  const rows = await api(`/api/campaigns/${id}/uncategorized`);
  const title = $("#uncat-title"), box = $("#uncat");
  if (!title || !box) return;
  if (!rows.length) { title.textContent = ""; box.innerHTML = ""; return; }
  const campaign = await api(`/api/campaigns/${id}`);
  title.textContent = `Domaines non catégorisés (${rows.length})`;
  if (!campaign.category_set_id) {
    box.innerHTML = `<div class="small muted">Associez un jeu de catégories à la campagne pour catégoriser ces domaines.</div>`;
    return;
  }
  box.innerHTML = `
    <div class="table-wrap" style="max-height:260px;overflow-y:auto">
      <table><tbody>
      ${rows.map((r) => `
        <tr><td>${esc(r.domaine)}</td><td class="num muted">${r.n}</td>
        <td><input class="small" data-d="${esc(r.domaine)}" placeholder="Catégorie…" style="padding:3px 8px;width:150px"></td>
        <td><button class="small" data-save="${esc(r.domaine)}">OK</button></td></tr>`).join("")}
      </tbody></table>
    </div>`;
  $$("button[data-save]", box).forEach((btn) => btn.onclick = async () => {
    const domaine = btn.dataset.save;
    const input = $(`input[data-d="${CSS.escape(domaine)}"]`, box);
    if (!input.value.trim()) return;
    await api(`/api/category_sets/${campaign.category_set_id}/mappings`,
      { method: "POST", json: { domaine, categorie: input.value.trim() } });
    toast(`${domaine} → ${input.value.trim()}`);
    renderDashboard(id, $("#subview"));
  });
}

// ------------------------------------------------------------- results

async function renderResults(id, el) {
  el.innerHTML = `<div class="empty">Chargement…</div>`;
  const limit = 50;
  const d = await api(campApi(id, `/results${qs({ ...state.resFilters, offset: state.resOffset, limit })}`));
  const dash = await api(campApi(id, `/dashboard${qs({})}`));
  const options = { ...dash.options, categories: dash.by_category.map((r) => r.categorie) };
  const fb = filterBar(options, state.resFilters, () => { state.resOffset = 0; renderResults(id, el); });

  const page = Math.floor(state.resOffset / limit) + 1;
  const pages = Math.max(1, Math.ceil(d.total / limit));
  el.innerHTML = `
    <div class="filters">${fb.html}
      <span class="small muted" style="margin-left:auto">${d.total} lignes</span>
      <a class="btn small" href="${campApi(id, "/export.csv" + qs(state.resFilters))}" download>⬇ Exporter (filtré)</a>
    </div>
    <div class="card">
      <div class="table-wrap">
        <table>
          <thead><tr><th>Date</th><th>Modèle</th><th>Prompt</th><th>Langue</th><th>URL</th><th>Domaine</th><th>Catégorie</th><th>Réponse</th></tr></thead>
          <tbody>${d.rows.map((r) => `
            <tr>
              <td class="small" style="white-space:nowrap">${fmtDate(r.date)}</td>
              <td class="small">${esc(r.modele)}</td>
              <td class="clip-sm" title="${esc(r.prompt)}">${esc(r.prompt)}</td>
              <td class="small">${esc(r.langue || "")}</td>
              <td class="clip-sm">${r.url ? `<a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.url)}</a>` : "—"}</td>
              <td class="small">${esc(r.domaine || "")}</td>
              <td class="small">${esc(r.categorie || "")}</td>
              <td class="answer-cell"><details><summary class="small muted">voir</summary><div class="full-text small">${esc(r.reponse || "")}</div></details></td>
            </tr>`).join("")}</tbody>
        </table>
      </div>
      <div class="row between" style="margin-top:12px">
        <button class="small" id="prev" ${page <= 1 ? "disabled" : ""}>← Précédent</button>
        <span class="small muted">Page ${page} / ${pages}</span>
        <button class="small" id="next" ${page >= pages ? "disabled" : ""}>Suivant →</button>
      </div>
    </div>`;
  fb.wire(el);
  $("#prev").onclick = () => { state.resOffset = Math.max(0, state.resOffset - limit); renderResults(id, el); };
  $("#next").onclick = () => { state.resOffset += limit; renderResults(id, el); };
}

// ------------------------------------------------------------- prompts

async function renderPrompts(id, el) {
  const [prompts, unique] = await Promise.all([
    api(`/api/campaigns/${id}/prompts`),
    api(`/api/campaigns/${id}/unique_prompts?sort=${state.upSort}`),
  ]);
  const sortLink = (key, label) =>
    `<th><a href="#" data-sort="${key}">${label}${state.upSort === key ? " ▾" : ""}</a></th>`;

  el.innerHTML = `
    <div class="card">
      <div class="row between">
        <h2 style="margin:0">Import des prompts (${prompts.length} importés)</h2>
        <div class="row">
          <label class="check small"><input type="checkbox" id="replace-cb" checked> Remplacer les prompts existants</label>
          <input type="file" id="prompt-file" accept=".csv" style="display:none">
          <button class="primary" id="upload-btn">⬆ Importer un CSV</button>
        </div>
      </div>
      <div class="small muted" style="margin:8px 0 4px">
        Colonnes attendues : <span class="mono">Catégorie, Prompt, Langue, LOC</span> (LOC = URL de proxy optionnelle, ex <span class="mono">http://user:pass@ip:port</span>)
      </div>
      <details style="margin-top:8px">
        <summary class="small" style="cursor:pointer;color:var(--accent)">Voir les prompts importés (avec proxy)</summary>
        <div class="table-wrap" style="margin-top:8px;max-height:360px;overflow-y:auto">
          <table>
            <thead><tr><th>#</th><th>Catégorie</th><th>Prompt</th><th>Langue</th><th>Proxy</th></tr></thead>
            <tbody>${prompts.map((p, i) => `
              <tr><td class="muted">${i + 1}</td><td class="small">${esc(p.categorie || "")}</td>
              <td>${esc(p.prompt)}</td><td class="small">${esc(p.langue || "")}</td>
              <td class="mono">${p.proxy ? esc(p.proxy.replace(/:\/\/[^@]+@/, "://•••@")) : ""}</td></tr>`).join("") ||
              `<tr><td colspan="5" class="empty">Aucun prompt — importez un CSV.</td></tr>`}</tbody>
          </table>
        </div>
      </details>
    </div>
    <div class="card">
      <div class="row between">
        <h2 style="margin:0">Prompts uniques (${unique.length})</h2>
        <div class="row" style="gap:8px">
          <a class="btn small" href="/api/campaigns/${id}/unique_prompts.csv?sort=${state.upSort}" download>⬇ CSV</a>
          <a class="btn small" href="/api/campaigns/${id}/unique_prompts.txt?sort=${state.upSort}" download>⬇ Texte</a>
        </div>
      </div>
      <div class="small muted" style="margin:6px 0 10px">
        Combinaisons uniques observées dans les résultats (une ligne par modèle) + prompts importés jamais exécutés.
        Cliquez sur un en-tête pour trier.
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr>
            ${sortLink("categorie", "Catégorie")}
            ${sortLink("prompt", "Prompt")}
            ${sortLink("langue", "Langue")}
            ${sortLink("modele", "Modèle")}
            <th class="num">Citations</th>
          </tr></thead>
          <tbody>${unique.map((u) => `
            <tr>
              <td class="small">${esc(u.categorie)}</td>
              <td>${esc(u.prompt)}</td>
              <td class="small">${esc(u.langue)}</td>
              <td class="small">${u.modele ? esc(u.modele) : `<span class="muted">jamais exécuté</span>`}</td>
              <td class="num">${u.citations}</td>
            </tr>`).join("") || `<tr><td colspan="5" class="empty">Aucun prompt.</td></tr>`}</tbody>
        </table>
      </div>
    </div>`;

  $$("a[data-sort]", el).forEach((a) => a.onclick = (e) => {
    e.preventDefault();
    state.upSort = a.dataset.sort;
    renderPrompts(id, el);
  });
  const fileInput = $("#prompt-file");
  $("#upload-btn").onclick = () => fileInput.click();
  fileInput.onchange = async () => {
    if (!fileInput.files.length) return;
    const fd = new FormData();
    fd.append("file", fileInput.files[0]);
    const replace = $("#replace-cb").checked;
    try {
      const r = await api(`/api/campaigns/${id}/prompts/upload?replace=${replace}`, { method: "POST", body: fd });
      toast(`${r.imported} prompts importés`);
      renderCampaignDetail();
    } catch (err) { toast(err.message, true); }
  };
}

// ------------------------------------------------------------- runs & errors

async function renderRuns(id, el) {
  const [runs, events] = await Promise.all([
    api(`/api/campaigns/${id}/runs`),
    api(`/api/campaigns/${id}/events?limit=200`),
  ]);
  const running = runs.some((r) => r.status === "running");

  el.innerHTML = `
    <div class="card">
      <h2>Runs</h2>
      <div class="table-wrap">
        <table>
          <thead><tr><th>#</th><th>Démarré</th><th>Terminé</th><th>Déclencheur</th><th>Statut</th><th>Progression</th><th class="num">OK</th><th class="num">Erreurs</th><th></th></tr></thead>
          <tbody>${runs.map((r) => {
            const done = r.ok_tasks + r.err_tasks;
            return `<tr>
              <td class="muted">${r.id}</td>
              <td class="small">${fmtDate(r.started_at)}</td>
              <td class="small">${fmtDate(r.finished_at)}</td>
              <td class="small">${r.trigger === "schedule" ? "planifié" : "manuel"}</td>
              <td><span class="badge ${r.status}"><span class="dot"></span>${r.status}</span></td>
              <td><progress max="${r.total_tasks || 1}" value="${done}"></progress> <span class="small muted">${done}/${r.total_tasks}</span></td>
              <td class="num" style="color:var(--good-text)">${r.ok_tasks}</td>
              <td class="num" style="color:${r.err_tasks ? "var(--critical)" : "inherit"}">${r.err_tasks}</td>
              <td>${r.status === "running" ? `<button class="small danger" data-cancel="${r.id}">Annuler</button>` : ""}</td>
            </tr>`;
          }).join("") || `<tr><td colspan="9" class="empty">Aucun run.</td></tr>`}</tbody>
        </table>
      </div>
    </div>
    <div class="card">
      <div class="row between">
        <h2 style="margin:0">Journal d'événements</h2>
        <select id="level-filter">
          <option value="">Tous les niveaux</option>
          <option value="error">Erreurs</option>
          <option value="warning">Avertissements</option>
          <option value="info">Infos</option>
        </select>
      </div>
      <div class="table-wrap" style="margin-top:10px">
        <table id="events-table">
          <thead><tr><th>Date</th><th>Niveau</th><th>Source</th><th>Run</th><th>Message</th></tr></thead>
          <tbody>${eventRows(events)}</tbody>
        </table>
      </div>
    </div>`;

  $$("button[data-cancel]", el).forEach((btn) => btn.onclick = async () => {
    await api(`/api/runs/${btn.dataset.cancel}/cancel`, { method: "POST" });
    toast("Annulation demandée");
    renderRuns(id, el);
  });
  $("#level-filter").onchange = async (e) => {
    const rows = await api(`/api/campaigns/${id}/events?limit=200${e.target.value ? "&level=" + e.target.value : ""}`);
    $("#events-table tbody").innerHTML = eventRows(rows);
  };

  if (running) pollTimer = setInterval(() => {
    if (state.subtab === "runs" && state.campaignId === id) renderRuns(id, el);
    else clearInterval(pollTimer);
  }, 3000);
}

function eventRows(events) {
  if (!events.length) return `<tr><td colspan="5" class="empty">Aucun événement.</td></tr>`;
  return events.map((ev) => `
    <tr>
      <td class="small" style="white-space:nowrap">${fmtDate(ev.ts)}</td>
      <td><span class="badge ${ev.level}"><span class="dot"></span>${ev.level}</span></td>
      <td class="small">${esc(ev.source || "")}</td>
      <td class="small muted">${ev.run_id ?? ""}</td>
      <td>
        ${esc(ev.message)}
        ${ev.detail ? `<details class="detail-json"><summary>détail</summary><pre>${esc(ev.detail)}</pre></details>` : ""}
      </td>
    </tr>`).join("");
}

// ============================================================= categories view

async function renderCategories() {
  const sets = await api("/api/category_sets");
  app.innerHTML = `
    <div class="row between" style="margin-bottom:16px">
      <h2 style="margin:0">Jeux de catégories</h2>
      <div class="row">
        <input id="new-set-name" placeholder="Nom du nouveau jeu…">
        <button class="primary" id="new-set">+ Créer</button>
      </div>
    </div>
    <div class="grid-2">
      <div class="card">
        <h2>Jeux</h2>
        <div class="table-wrap">
          <table><thead><tr><th>Nom</th><th class="num">Domaines</th><th></th></tr></thead>
          <tbody>${sets.map((s) => `
            <tr>
              <td><a href="#" data-open="${s.id}">${esc(s.name)}</a></td>
              <td class="num">${s.n_domains}</td>
              <td class="row" style="gap:6px;justify-content:flex-end">
                <button class="small" data-clone="${s.id}">Dupliquer</button>
                <button class="small danger" data-del="${s.id}">Supprimer</button>
              </td>
            </tr>`).join("") || `<tr><td colspan="3" class="empty">Aucun jeu de catégories.</td></tr>`}</tbody></table>
        </div>
      </div>
      <div class="card" id="set-detail"><div class="empty">Sélectionnez un jeu pour voir / modifier ses domaines.</div></div>
    </div>`;

  $("#new-set").onclick = async () => {
    const name = $("#new-set-name").value.trim();
    if (!name) return;
    try { await api("/api/category_sets", { method: "POST", json: { name } }); renderCategories(); }
    catch (err) { toast(err.message, true); }
  };
  $$("a[data-open]").forEach((a) => a.onclick = (e) => { e.preventDefault(); openSet(+a.dataset.open, sets); });
  $$("button[data-del]").forEach((b) => b.onclick = async () => {
    if (!confirm("Supprimer ce jeu de catégories ? Les campagnes qui l'utilisent perdront leur catégorisation.")) return;
    await api(`/api/category_sets/${b.dataset.del}`, { method: "DELETE" });
    renderCategories();
  });
  $$("button[data-clone]").forEach((b) => b.onclick = async () => {
    const name = prompt("Nom du nouveau jeu (copie) :");
    if (!name) return;
    try { await api(`/api/category_sets/${b.dataset.clone}/clone`, { method: "POST", json: { name } }); renderCategories(); }
    catch (err) { toast(err.message, true); }
  });
  if (sets.length) openSet(sets[0].id, sets);
}

async function openSet(setId, sets) {
  const set = sets.find((s) => s.id === setId);
  const mappings = await api(`/api/category_sets/${setId}/mappings`);
  const detail = $("#set-detail");
  detail.innerHTML = `
    <div class="row between">
      <h2 style="margin:0">${esc(set?.name || "")} <span class="muted small">(${mappings.length} domaines)</span></h2>
      <div class="row">
        <input type="file" id="cat-file" accept=".csv" style="display:none">
        <button id="cat-upload">⬆ Importer CSV</button>
      </div>
    </div>
    <div class="small muted" style="margin:6px 0 12px">Colonnes attendues : <span class="mono">Catégorie, Domaine</span></div>
    <div class="row" style="margin-bottom:12px">
      <input id="map-domain" placeholder="domaine.com" style="flex:1">
      <input id="map-cat" placeholder="Catégorie" list="cat-list" style="flex:1">
      <datalist id="cat-list">${[...new Set(mappings.map((m) => m.categorie))].map((c) => `<option value="${esc(c)}">`).join("")}</datalist>
      <button class="primary" id="map-add">Ajouter</button>
    </div>
    <div class="table-wrap" style="max-height:480px;overflow-y:auto">
      <table><thead><tr><th>Catégorie</th><th>Domaine</th><th></th></tr></thead>
      <tbody>${mappings.map((m) => `
        <tr><td>${esc(m.categorie)}</td><td>${esc(m.domaine)}</td>
        <td style="text-align:right"><button class="small danger" data-delm="${m.id}">✕</button></td></tr>`).join("")}</tbody></table>
    </div>`;

  $("#cat-upload").onclick = () => $("#cat-file").click();
  $("#cat-file").onchange = async (e) => {
    if (!e.target.files.length) return;
    const fd = new FormData();
    fd.append("file", e.target.files[0]);
    const r = await api(`/api/category_sets/${setId}/upload`, { method: "POST", body: fd });
    toast(`${r.imported} associations importées`);
    renderCategories();
  };
  $("#map-add").onclick = async () => {
    const domaine = $("#map-domain").value.trim(), categorie = $("#map-cat").value.trim();
    if (!domaine || !categorie) return;
    await api(`/api/category_sets/${setId}/mappings`, { method: "POST", json: { domaine, categorie } });
    openSet(setId, sets);
  };
  $$("button[data-delm]", detail).forEach((b) => b.onclick = async () => {
    await api(`/api/category_sets/${setId}/mappings/${b.dataset.delm}`, { method: "DELETE" });
    openSet(setId, sets);
  });
}

// ============================================================= settings view

async function renderSettings() {
  const s = await api("/api/settings");
  const secret = (key, label) => `
    <label class="field">${label}
      <input name="${key}" type="password" autocomplete="off"
        placeholder="${s[key].set ? "configurée (" + esc(s[key].hint) + ") — laisser vide pour conserver" : "non configurée"}">
    </label>`;
  const plain = (key, label, type = "text") => `
    <label class="field">${label}
      <input name="${key}" type="${type}" value="${esc(s[key])}">
    </label>`;

  app.innerHTML = `
    <div class="card">
      <h2>Sécurité (Basic auth)</h2>
      <div class="small muted" style="margin-bottom:12px">
        Identifiants demandés à l'ouverture de l'application (sauf liens visiteur).
        Après modification, le navigateur redemandera de se connecter.
        ${s.admin_password_is_default ? `<br><b style="color:var(--serious)">⚠ Le mot de passe par défaut (admin / admin) est toujours actif — changez-le !</b>` : ""}
      </div>
      <form id="security-form">
        <div class="form-grid">
          <label class="field">Utilisateur
            <input name="admin_user" value="${esc(s.admin_user)}">
          </label>
          <label class="field">Mot de passe
            <input name="admin_password" type="password" autocomplete="new-password"
              placeholder="${s.admin_password.set ? "configuré — laisser vide pour conserver" : "non configuré"}">
          </label>
        </div>
        <button class="primary" type="submit">Enregistrer la sécurité</button>
      </form>
    </div>
    <div class="card">
      <h2>Clés API</h2>
      <div class="small muted" style="margin-bottom:12px">
        Les clés sont stockées localement (SQLite) et ne sont jamais renvoyées au navigateur.
      </div>
      <form id="settings-form">
        <div class="form-grid">
          ${secret("openai_api_key", "OpenAI")}
          ${secret("gemini_api_key", "Google Gemini")}
          ${secret("anthropic_api_key", "Anthropic (Claude)")}
          ${secret("xai_api_key", "xAI (Grok)")}
        </div>
        <h2>Modèles</h2>
        <div class="form-grid">
          ${plain("openai_model", "Modèle OpenAI")}
          ${plain("gemini_model", "Modèle Gemini")}
          ${plain("anthropic_model", "Modèle Claude")}
          ${plain("xai_model", "Modèle Grok")}
        </div>
        <h2>Exécution</h2>
        <div class="form-grid">
          ${plain("concurrency", "Requêtes parallèles par fournisseur", "number")}
          ${plain("request_timeout", "Timeout API (secondes)", "number")}
          ${plain("max_retries", "Nombre de retries", "number")}
          ${plain("resolve_timeout", "Timeout résolution d'URL (secondes)", "number")}
        </div>
        <button class="primary" type="submit">Enregistrer</button>
      </form>
    </div>`;

  $("#settings-form").onsubmit = async (e) => {
    e.preventDefault();
    const values = {};
    $$("#settings-form input").forEach((input) => (values[input.name] = input.value));
    await api("/api/settings", { method: "POST", json: { values } });
    toast("Réglages enregistrés");
    renderSettings();
  };
  $("#security-form").onsubmit = async (e) => {
    e.preventDefault();
    const values = {};
    $$("#security-form input").forEach((input) => (values[input.name] = input.value));
    await api("/api/settings", { method: "POST", json: { values } });
    toast("Sécurité mise à jour — le navigateur va redemander les identifiants");
    setTimeout(() => location.reload(), 1500);
  };
}

// ============================================================= visitor panel

async function renderVisitor() {
  $("#main-tabs").style.display = "none";
  let info;
  try { info = await api(`/api/share/${SHARE}/info`); }
  catch {
    app.innerHTML = `<div class="empty">Lien visiteur invalide ou révoqué.</div>`;
    return;
  }
  document.title = `${info.name} — Veille IA`;
  if (!["dashboard", "results"].includes(state.subtab)) state.subtab = "dashboard";

  app.innerHTML = `
    <div class="row between" style="margin-bottom:14px">
      <div class="row">
        <h2 style="margin:0">${esc(info.name)}</h2>
        <span class="badge active"><span class="dot"></span>consultation</span>
      </div>
      <div class="row">
        <span class="small muted">${info.n_results} résultats — dernier run : ${fmtDate(info.last_run)}</span>
        <a class="btn" href="/api/share/${SHARE}/export.csv" download>⬇ Export CSV</a>
      </div>
    </div>
    <div class="subtabs" id="subtabs">
      <button data-t="dashboard">Dashboard</button>
      <button data-t="results">Résultats</button>
    </div>
    <div id="subview"></div>`;

  $$("#subtabs button").forEach((b) => {
    b.classList.toggle("active", b.dataset.t === state.subtab);
    b.onclick = () => { state.subtab = b.dataset.t; state.resOffset = 0; renderVisitor(); };
  });
  const el = $("#subview");
  if (state.subtab === "dashboard") renderDashboard(0, el);
  else renderResults(0, el);
}

// ============================================================= boot

(function initFromHash() {
  const q = new URLSearchParams(location.search);
  if (q.get("sun") === "prompt") state.sunMode = "prompt";
  const h = location.hash.slice(1);
  if (h.startsWith("c/")) {
    const [, id, sub] = h.split("/");
    state.view = "campaigns";
    state.campaignId = +id || null;
    state.subtab = sub || "dashboard";
  } else if (["campaigns", "categories", "settings"].includes(h)) {
    state.view = h;
  }
})();
if (SHARE) renderVisitor();
else render();
