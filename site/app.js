// The Menu Atlas: a static page over data/data.json (written by `menudb export-site`).
// Everything (search, filters, charts) runs in the browser. Data strings are only
// ever inserted with textContent, never as HTML.
"use strict";

const DATA_URL = window.MENU_ATLAS_DATA_URL || "data/data.json";
let D = null; // the loaded bundle
const byName = new Map();
const byRestaurant = new Map();

// --- split data files (export-site writes data.json, pairs.json, menus/NN.json) -----

const loaded = {};
function fetchJSON(rel) {
  return (loaded[rel] ||= fetch(new URL(rel, new URL(DATA_URL, location.href)), { cache: "no-cache" })
    .then((res) => { if (!res.ok) throw new Error(res.status + " " + res.statusText); return res.json(); }));
}
// Same as site.shard() in Python: FNV-1a 32-bit of the slug's UTF-8 bytes, mod 64.
function shard(slug) {
  let x = 0x811c9dc5;
  for (const b of new TextEncoder().encode(slug)) { x ^= b; x = Math.imul(x, 0x01000193) >>> 0; }
  return String(x % 64).padStart(2, "0");
}
async function ensurePairs() {
  if (D.pairs) return;
  try { Object.assign(D, await fetchJSON("pairs.json")); }
  catch (err) { D.pairs = []; D.pairs_by_accolade = {}; D.pairsError = String(err); }
}
async function ensureMenus(slug) {
  const r = byRestaurant.get(slug);
  if (!r || !D.has_menus || r.menu_list !== undefined || !r.menus) return;
  try {
    for (const [k, v] of Object.entries(await fetchJSON(`menus/${shard(slug)}.json`))) {
      const x = byRestaurant.get(k); if (x) x.menu_list = v;
    }
  } catch { /* the page still shows the ingredient list */ }
  if (r.menu_list === undefined) r.menu_list = [];
}
// Every restaurant's menus (all 64 shards, about 0.4 MB compressed), for the course lists.
let allMenus = null;
function ensureAllMenus() {
  if (!D.has_menus) return Promise.resolve();
  return (allMenus ||= Promise.all(Array.from({ length: 64 }, (_, i) => fetchJSON(`menus/${String(i).padStart(2, "0")}.json`)
    .then((part) => { for (const [k, v] of Object.entries(part)) { const x = byRestaurant.get(k); if (x) x.menu_list = v; } })
    .catch(() => {}))));
}
// Fill back the fields export-site drops at their default, and turn restaurant indexes into slugs.
const R_DEFAULTS = { approx: false, blocked: false, menus: 0, ingredients: 0, publishes_menu: "unknown",
  url: null, city: null, country: null, lat: null, lon: null };
function expand(D) {
  for (const r of D.restaurants) {
    for (const [k, v] of Object.entries(R_DEFAULTS)) if (!(k in r)) r[k] = v;
    r.accolades ||= {}; r.tiers ||= [];
  }
  for (const i of D.ingredients) i.restaurants = i.restaurants.map((n) => D.restaurants[n].slug);
}

// --- tiny DOM helpers ---------------------------------------------------------

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

const SVGNS = "http://www.w3.org/2000/svg";
function s(tag, attrs, ...kids) {
  const el = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) if (v != null) el.setAttribute(k, v);
  for (const kid of kids.flat()) if (kid != null) el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  return el;
}

// 2 significant digits below 10%, so small supports (0.04%) don't all read as 0.0%
const pct = (x) => (x == null ? "–" : (x * 100 >= 10 || x === 0 ? Math.round(x * 100) : x * 100 >= 1 ? (x * 100).toFixed(1) : Number((x * 100).toPrecision(2)).toString()) + "%");
const num = (x, d = 2) => (x == null ? "–" : Number(x).toFixed(d));
const ingLink = (name) => h("a", { href: "#/ingredient/" + encodeURIComponent(name) }, name);
// Only https://en.wikipedia.org links from the data file are rendered.
const wikiLink = (url, text, label) => (/^https:\/\/en\.wikipedia\.org\/wiki\//.test(url)
  ? h("a", { href: url, rel: "noopener", target: "_blank", "aria-label": label || null }, text) : null);
const restName = (slug) => (byRestaurant.get(slug) || { name: slug }).name;

// --- tooltip ------------------------------------------------------------------

const tip = () => document.getElementById("tip");
function showTip(ev, value, label) {
  const t = tip();
  t.replaceChildren(h("strong", {}, value), h("span", {}, label));
  t.hidden = false;
  const r = ev.target.getBoundingClientRect ? ev.target.getBoundingClientRect() : null;
  const x = ev.clientX ?? (r ? r.right : 0), y = ev.clientY ?? (r ? r.top : 0);
  const w = t.offsetWidth, hgt = t.offsetHeight;
  t.style.left = Math.min(window.innerWidth - w - 8, x + 12) + "px";
  t.style.top = Math.max(8, y - hgt - 10) + "px";
}
const hideTip = () => { tip().hidden = true; };
function hoverable(el, value, label) {
  el.addEventListener("pointermove", (e) => showTip(e, value, label));
  el.addEventListener("pointerleave", hideTip);
  el.addEventListener("focus", (e) => showTip(e, value, label));
  el.addEventListener("blur", hideTip);
  return el;
}

// --- routing and URL state ----------------------------------------------------

function parseHash() {
  const raw = location.hash.replace(/^#/, "") || "/";
  const [path, query] = raw.split("?");
  return { parts: path.split("/").filter(Boolean).map(decodeURIComponent), params: new URLSearchParams(query || "") };
}

// Keep filter state in the hash so views are linkable, without adding history entries.
function setParams(params) {
  const { parts } = parseHash();
  const q = params.toString();
  const hash = "#/" + parts.map(encodeURIComponent).join("/") + (q ? "?" + q : "");
  try { history.replaceState(null, "", hash); } catch { /* sandboxed frames may refuse; state just isn't linkable */ }
}

let routing = 0;
async function route() {
  hideTip();
  const { parts, params } = parseHash();
  const view = parts[0] || "home";
  const ticket = ++routing;
  const needs = view === "pairings" || view === "ingredient" ? ensurePairs()
    : view === "restaurant" && parts[1] ? ensureMenus(parts[1]) : view === "courses" ? ensureAllMenus() : null;
  if (needs) {
    const app = document.getElementById("app");
    const timer = setTimeout(() => { if (ticket === routing) app.replaceChildren(h("p", { class: "muted" }, "Loading…")); }, 150);
    await needs;
    clearTimeout(timer);
    if (ticket !== routing) return;
  }
  for (const a of document.querySelectorAll("[data-nav]")) {
    const pairView = view === "courses" && params.has("with");
    const on = (a.dataset.nav === view && !pairView) || (pairView && a.dataset.nav === "pairings")
      || ((view === "ingredient" || (view === "courses" && !pairView)) && a.dataset.nav === "ingredients")
      || (view === "restaurant" && a.dataset.nav === "restaurants");
    a.classList.toggle("on", on);
    if (on) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  }
  // The home page shows the title large, so the header's copy is hidden there.
  document.body.classList.toggle("home", view === "home");
  const app = document.getElementById("app");
  const views = { home, ingredients, ingredient, courses, pairings, restaurants, restaurant, coverage, about };
  const fn = views[view] || notFound;
  app.replaceChildren(...[].concat(fn(params, parts.slice(1))));
  document.title = (fn === home ? "" : (app.querySelector("h1")?.textContent + " · ")) + "The Menu Atlas";
}

// --- shared pieces ------------------------------------------------------------

function tiles(items) {
  return h("div", { class: "tiles" }, items.map(([v, k]) => h("div", { class: "tile" }, h("div", { class: "v" }, typeof v === "number" ? v.toLocaleString() : v), h("div", { class: "k" }, k))));
}

function shareBar(x, max = 1) {
  return h("div", { class: "sbar" },
    h("div", { class: "track" }, h("div", { class: "fill", style: `width:${Math.max(0, Math.min(1, x / max)) * 100}%` })),
    h("span", { class: "val" }, pct(x)));
}

// Horizontal bars: one series, direct value labels, hover for the full readout.
function barChart(entries, { fmt = pct, max, note } = {}) {
  if (!entries.length) return h("p", { class: "empty" }, "No data for this view.");
  const m = max ?? Math.max(...entries.map((e) => e[1]), 1e-9);
  return h("div", { class: "bars" }, entries.flatMap(([label, v, extra]) => {
    const track = h("div", { class: "track", tabindex: "0", "aria-label": `${label}: ${fmt(v)}` },
      h("div", { class: "fill", style: `width:${(v / m) * 100}%` }));
    hoverable(track, fmt(v), extra ? `${label} · ${extra}` : label);
    return [h("span", { class: "lab" }, label), track, h("span", { class: "val" }, fmt(v))];
  }), note ? h("p", { class: "muted", style: "grid-column:1/-1;margin:.25rem 0 0;font-size:.8rem" }, note) : null);
}

// Sortable table. cols: [{key, label, num, render(row), sort(row)}]
function table(rows, cols, state, onSort) {
  const sorted = [...rows];
  const col = cols.find((c) => c.key === state.sort) || cols[0];
  const val = col.sort || ((r) => r[col.key]);
  sorted.sort((a, b) => {
    const x = val(a), y = val(b);
    const c = typeof x === "string" ? x.localeCompare(y) : (x ?? -Infinity) - (y ?? -Infinity);
    return state.dir === "asc" ? c : -c;
  });
  const head = h("tr", {}, cols.map((c) => h("th", { class: c.num ? "num" : null, "aria-sort": c.key === col.key ? (state.dir === "asc" ? "ascending" : "descending") : null },
    c.nosort ? c.label : h("button", { onclick: () => onSort(c.key, c.key === col.key && state.dir === "desc" ? "asc" : (c.key === col.key ? "desc" : (c.first || (c.num ? "desc" : "asc")))) },
      c.label + (c.key === col.key ? (state.dir === "asc" ? " ↑" : " ↓") : "")))));
  // Long tables render in pages; "Show more" appends the next page in place.
  const page = state.page || 100;
  const tbody = h("tbody", {});
  const more = h("button", { type: "button", class: "more" });
  let shown = 0;
  const showMore = () => {
    const next = sorted.slice(shown, shown + page);
    tbody.append(...next.map((r) => h("tr", {}, cols.map((c) => h("td", { class: c.num ? "num" : null }, c.render ? c.render(r) : r[c.key])))));
    shown += next.length;
    more.textContent = `Show ${Math.min(page, sorted.length - shown)} more of ${sorted.length - shown}`;
    more.hidden = shown >= sorted.length;
  };
  more.addEventListener("click", showMore);
  if (sorted.length) showMore();
  else { tbody.append(h("tr", {}, h("td", { colspan: cols.length, class: "empty" }, "Nothing matches."))); more.hidden = true; }
  return h("div", {}, h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, head), tbody)), more);
}

function groupOptions(selected) {
  const opt = (v, l) => h("option", { value: v, selected: v === selected }, l);
  return [opt("", "All restaurants"),
    h("optgroup", { label: "Region" }, D.groups.region.map((g) => opt("region:" + g, g))),
    accoladeGroups().length ? h("optgroup", { label: "Accolade" }, accoladeGroups().map((g) => opt("accolade:" + g, g))) : null,
    seasonsReady() && D.groups.season.some((g) => g !== "unknown") ? h("optgroup", { label: "Season (hemisphere-adjusted)" }, D.groups.season.filter((g) => g !== "unknown").map((g) => opt("season:" + g, cap(g)))) : null];
}
// accolade groups with enough menus for shares and pairings
const accoladeGroups = () => Object.keys(D.groups.accolade_with_data || {});

// --- accolades ----------------------------------------------------------------

// "★★★ · 50 Best #12 · NYT NYC #48"; "" when the restaurant is on none of the lists.
function accoladeText(a = {}) {
  return [a.stars ? "★".repeat(a.stars) : null, a.w50b ? `50 Best #${a.w50b}` : null,
    a.nyt50 ? "NYT America 50" : null, a.nytnyc ? `NYT NYC #${a.nytnyc}` : null].filter(Boolean).join(" · ");
}
function accoladeTitle(a = {}) {
  return [a.stars ? `${a.stars} Michelin star${a.stars > 1 ? "s" : ""} (${a.stars_year} guide)` : null,
    a.w50b ? `No. ${a.w50b} on The World's 50 Best Restaurants ${a.w50b_year} (1–100)` : null,
    a.nyt50 ? `New York Times, The 50 Best Restaurants in America ${a.nyt50}` : null,
    a.nytnyc ? `No. ${a.nytnyc} on the New York Times 100 Best Restaurants in NYC ${a.nytnyc_year}` : null].filter(Boolean).join("\n");
}
// Higher is more decorated: stars first, then 50 Best rank, then the NYT lists.
function accoladeScore(a = {}) {
  return (a.stars || 0) * 1000 + (a.w50b ? 500 - a.w50b : 0) + (a.nyt50 ? 50 : 0) + (a.nytnyc ? (101 - a.nytnyc) / 10 : 0);
}
// n Michelin stars, drawn with the symbol in index.html (#mstar)
function stars(n) {
  return h("span", { class: "stars", role: "img", "aria-label": `${n} Michelin star${n > 1 ? "s" : ""}` },
    Array.from({ length: n }, () => s("svg", { "aria-hidden": "true" }, s("use", { href: "#mstar" }))));
}
// accoladeText as nodes, with the stars as symbols
function accoladeNodes(a = {}) {
  const rest = accoladeText({ ...a, stars: 0 });
  return [a.stars ? stars(a.stars) : null, a.stars && rest ? " · " : null, rest || null];
}
function accoladeCell(a) {
  return accoladeText(a) ? h("span", { class: "acc", title: accoladeTitle(a) }, accoladeNodes(a)) : h("span", { class: "muted" }, "–");
}
// Filter values: "" (all), a group name, "starred" (any Michelin star) or "none".
// Counts are over pool(); call sel.refresh() when the pool changes.
function accoladeSelect(selected, groups = D.groups.accolade || [], pool = () => D.restaurants) {
  const sel = h("select", {});
  sel.refresh = () => {
    const cur = sel.options.length ? sel.value : selected;
    const opt = (v, l) => h("option", { value: v, selected: v === cur }, l);
    const rs = pool(), n = (f) => rs.filter((r) => accoladeMatch(r, f)).length;
    sel.replaceChildren(opt("", "Any or none"),
      ...groups.map((g) => opt(g, `${g} (${n(g)})`)),
      ...(groups.some((g) => g.endsWith("star") || g.endsWith("stars")) ? [opt("starred", `Any Michelin star (${n("starred")})`)] : []),
      opt("none", `None of these lists (${n("none")})`));
  };
  sel.refresh();
  return sel;
}
function accoladeMatch(r, f) {
  if (!f) return true;
  if (f === "starred") return !!r.accolades?.stars;
  if (f === "none") return !(r.tiers || []).length;
  return (r.tiers || []).includes(f);
}
// Seasons only mean something once first-seen dates span most of a year; until then
// "autumn" vs "spring" is just which hemisphere a restaurant is in.
function seasonsReady() {
  const r = D.seen_range;
  return !r || (Date.parse(r[1]) - Date.parse(r[0])) / 864e5 >= 270;
}
const cap = (x) => x.charAt(0).toUpperCase() + x.slice(1);
// lowercase without accents, so "cepe" finds "cèpe"
const fold = (x) => (x || "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();

function groupShare(ing, group) {
  if (!group) return ing.share;
  const [kind, g] = group.split(":");
  return ({ region: ing.by_region, season: ing.by_season, accolade: ing.by_accolade }[kind] || {})[g];
}

// Staples (bread, butter, pasta...) pair with everything, so their pairings are hidden unless asked for.
const isStaple = (name) => byName.get(name)?.pairing === "staple";
const plain = (i) => !i.pairing; // neither generic nor a staple
const staplesNote = () => D.ingredients.filter((i) => i.pairing === "staple").slice(0, 3).map((i) => i.name).join(", ");

const REPO = "https://github.com/msh-yi/themenuatlas";
const OPT_OUT = REPO + "/issues/new?template=opt-out.yml";
const CORRECTION = REPO + "/issues/new?template=correction.yml";
const SUGGEST = (r) => REPO + "/issues/new?" + new URLSearchParams({ template: "website.yml", title: `Website: ${r.name}`, restaurant: r.slug });
const ext = (href, text) => h("a", { href, rel: "noopener", target: "_blank" }, text);

// --- views --------------------------------------------------------------------

// The landing page: one search box over ingredients and restaurants, then the ways in.
function home() {
  const t = D.totals;
  const withMenus = D.restaurants.filter((r) => r.menus > 0);
  const found = h("ul", { class: "found", id: "found", "aria-label": "Matches" });
  const search = h("input", { type: "search", placeholder: "An ingredient, restaurant or city", autocomplete: "off",
    "aria-label": "Search ingredients and restaurants", "aria-controls": "found" });
  let first = null;
  const rank = (name, needle) => (fold(name).startsWith(needle) ? 0 : 1);
  const live = () => {
    const needle = fold(search.value.trim());
    first = null;
    if (!needle) { found.replaceChildren(); return; }
    const ings = D.ingredients
      .map((i) => ({ i, via: fold(i.name).includes(needle) ? null : (i.aka || []).find((a) => fold(a).includes(needle)) }))
      .filter(({ i, via }) => via || fold(i.name).includes(needle))
      .sort((a, b) => rank(a.via || a.i.name, needle) - rank(b.via || b.i.name, needle) || b.i.restaurants.length - a.i.restaurants.length)
      .slice(0, 5)
      .map(({ i, via }) => ["#/ingredient/" + encodeURIComponent(i.name), via ? `${i.name} (as “${via}”)` : i.name,
        `ingredient · ${i.restaurants.length} restaurant${i.restaurants.length === 1 ? "" : "s"}`]);
    const rests = withMenus
      .filter((r) => fold(r.name).includes(needle) || fold(r.city).includes(needle))
      .sort((a, b) => rank(a.name, needle) - rank(b.name, needle) || accoladeScore(b.accolades) - accoladeScore(a.accolades))
      .slice(0, 5)
      .map((r) => ["#/restaurant/" + encodeURIComponent(r.slug), r.name, ["restaurant", r.city].filter(Boolean).join(" · ")]);
    const all = [...ings, ...rests];
    first = all[0]?.[0] || null;
    found.replaceChildren(...(all.length
      ? all.map(([href, label, kind]) => h("li", {}, h("a", { href }, h("span", {}, label), h("span", { class: "kind" }, kind))))
      : [h("li", { class: "none" }, "Nothing matches. Try another spelling.")]));
  };
  search.addEventListener("input", live);
  search.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && first) location.hash = first;
    else if (e.key === "ArrowDown") { const a = found.querySelector("a"); if (a) { e.preventDefault(); a.focus(); } }
  });
  found.addEventListener("keydown", (e) => {
    const links = [...found.querySelectorAll("a")], i = links.indexOf(document.activeElement);
    if (e.key === "ArrowDown" && i < links.length - 1) { e.preventDefault(); links[i + 1].focus(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); (i > 0 ? links[i - 1] : search).focus(); }
  });
  // Three random suggestions, new on each visit. Skip the most common ingredients (butter, beef...)
  // and the one-offs, so they're interesting but still lead somewhere.
  const byUse = [...D.ingredients].sort((a, b) => b.restaurants.length - a.restaurants.length);
  let pool = byUse.slice(30).filter((i) => i.restaurants.length >= 3 && plain(i));
  if (pool.length < 6) pool = byUse;
  const suggestions = [...pool].sort(() => Math.random() - 0.5).slice(0, 3);
  // Four colour blocks, numbered like the sections of a 1960s guidebook.
  let n = 0;
  const path = (href, title, text, go) => h("a", { class: `path path-${++n}`, href },
    h("span", { class: "kicker" }, String(n).padStart(2, "0")), h("h2", {}, title), h("p", {}, text), h("span", { class: "go" }, go + " →"));
  setTimeout(() => search.focus({ preventScroll: true }), 0);
  return h("div", {},
    h("section", { class: "hero" },
      h("div", { class: "hero-title" }, h("span", { class: "disc", "aria-hidden": "true" }), h("h1", {}, "The Menu Atlas")),
      h("div", { class: "hero-side" },
        h("p", { class: "lede" }, `What the world's best restaurants are cooking, read from the tasting menus ${t.restaurants_with_data.toLocaleString()} of them publish: every ingredient, how it's prepared, what it's paired with, and where.`),
        h("div", { class: "finder" }, search, found,
          h("p", { class: "hint" }, "Try: ", suggestions.flatMap((i, n) => [n ? " · " : "", ingLink(i.name)]))))),
    h("div", { class: "paths" },
      path("#/ingredients", "Ingredients", `Look up any of ${t.ingredients.toLocaleString()} ingredients: how often it's on the menu, by region and season, and how chefs prepare it.`, "Browse ingredients"),
      path("#/restaurants", "Restaurants", `Find a restaurant by name, city, region or accolade${D.has_menus ? ", and read its menus course by course" : ", and see what it cooks with"}.`, "Find a restaurant"),
      path("#/pairings", "Pairings", "Ingredients that share a plate more often than chance, from the classics to the surprising.", "Explore pairings"),
      path("#/coverage", "Map", "Every restaurant with a menu on a world map. Zoom into a country and open any of them.", "Open the map")),
    h("section", { class: "home-stats" },
      tiles([[t.restaurants_with_data.toLocaleString(), "restaurants with menus"], [t.menus.toLocaleString(), "menus read"],
        [t.ingredients.toLocaleString(), "ingredients"], [t.pairs.toLocaleString(), "pairings"]]),
      h("p", { class: "muted" }, "Only ingredient names and counts are shown; each restaurant links to its own site. ", h("a", { href: "#/about" }, "How it works →"))));
}

function ingredients(params) {
  const state = { sort: params.get("sort") || "gshare", dir: params.get("dir") || "desc" };
  const t = D.totals;
  const resultsHost = h("div", {});
  const search = h("input", { type: "search", placeholder: "uni, beetroot, fermented…", value: params.get("q") || "",
    "aria-label": "Search ingredients, other names for them, or preparations" });
  const sel = h("select", {}, groupOptions(params.get("g") || ""));
  const catSel = h("select", {}, h("option", { value: "" }, "All kinds"),
    (D.groups.category || []).map((c) => h("option", { value: c, selected: c === params.get("cat") }, cap(c))));

  const live = () => {
    const q = search.value, group = sel.value, cat = catSel.value;
    const p = new URLSearchParams();
    if (q) p.set("q", q); if (group) p.set("g", group); if (cat) p.set("cat", cat);
    if (state.sort !== "gshare") p.set("sort", state.sort); if (state.dir !== "desc") p.set("dir", state.dir);
    setParams(p);
    const needle = fold(q.trim());
    const rows = D.ingredients
      .map((i) => ({ ...i, gshare: groupShare(i, group), n: i.restaurants.length, prep: i.preparations[0]?.[0] || "",
        via: needle && !fold(i.name).includes(needle) ? (i.aka || []).find((a) => fold(a).includes(needle)) : null }))
      .filter((i) => i.gshare != null && (!cat || i.category === cat)
        && (!needle || fold(i.name).includes(needle) || i.via || i.preparations.some(([pr]) => fold(pr).includes(needle))));
    const max = Math.max(...rows.map((r) => r.gshare), 1e-9);
    resultsHost.replaceChildren(
      h("p", { class: "muted" }, `${rows.length} of ${D.ingredients.length} ingredients`),
      table(rows, [
        { key: "name", label: "Ingredient", render: (r) => r.via ? h("span", {}, ingLink(r.name), h("span", { class: "muted via" }, ` as “${r.via}”`)) : ingLink(r.name) },
        { key: "gshare", label: group ? "Share in group" : "Share of menus", render: (r) => shareBar(r.gshare, max) },
        { key: "n", label: "Restaurants", num: true },
        { key: "prep", label: "Top preparation", render: (r) => r.prep || h("span", { class: "muted" }, "–") },
        { key: "wikipedia", label: "Wikipedia", nosort: true,
          render: (r) => r.wikipedia ? wikiLink(r.wikipedia, "↗", `${r.name} on Wikipedia`) : h("span", { class: "muted" }, "–") },
      ], state, (sort, dir) => { state.sort = sort; state.dir = dir; live(); }));
  };
  search.addEventListener("input", live);
  sel.addEventListener("change", live);
  catSel.addEventListener("change", live);
  live();
  return h("div", {},
    h("h1", {}, "Ingredients"),
    h("p", { class: "sub" }, "How often each ingredient appears on published tasting menus. Each restaurant counts once, however many menus it publishes."),
    tiles([[t.restaurants_with_data, "restaurants with menus"], [t.menus, "distinct menus"], [t.ingredients, "ingredients"], [t.pairs, "pairings"]]),
    h("div", { class: "filters" }, h("label", {}, "Search, in any language", search), h("label", {}, "Kind", catSel), h("label", {}, "Group", sel)),
    resultsHost);
}

function ingredient(params, [name]) {
  const ing = byName.get(name);
  if (!ing) return notFound();
  const total = D.totals.restaurants_with_data;
  // Positive associations only, most widely shared first: a 40× lift seen at two
  // restaurants is one chef's dish, not a pattern.
  const allPairs = D.pairs.filter((p) => (p.a === name || p.b === name) && p.lift > 1)
    .map((p) => ({ ...p, other: p.a === name ? p.b : p.a }));
  const staples = params.get("staples") === "1";
  const pairs = staples ? allPairs : allPairs.filter((p) => !isStaple(p.other));
  const hiddenStaples = allPairs.length - pairs.length;
  const toggleStaples = () => { const p = new URLSearchParams(); if (!staples) p.set("staples", "1"); setParams(p); route(); };
  const regions = D.groups.region.filter((g) => ing.by_region[g] != null).map((g) => [g, ing.by_region[g]]);
  const seasons = D.groups.season.filter((g) => ing.by_season[g] != null).map((g) => [cap(g), ing.by_season[g]]);
  const accolades = accoladeGroups().map((g) => [g, (ing.by_accolade || {})[g] || 0]);
  const rs = ing.restaurants.map((slug) => byRestaurant.get(slug) || { slug, name: slug })
    .sort((a, b) => a.name.localeCompare(b.name));
  const LIST = 40;
  const children = D.ingredients.filter((i) => i.parent === name);

  return h("div", {},
    h("p", { class: "muted", style: "margin:0 0 .25rem" }, h("a", { href: "#/ingredients" }, "← Ingredients")),
    h("h1", {}, cap(name)),
    h("p", { class: "sub" }, ing.category ? `${cap(ing.category)} · ` : "", `Used by ${ing.restaurants.length} of ${total} restaurants with published menus.`,
      ing.wikipedia ? h("span", {}, " · ", wikiLink(ing.wikipedia, "Read about it on Wikipedia ↗")) : null),
    ing.parent || children.length ? h("p", { class: "muted", style: "margin-top:-.75rem" },
      ing.parent ? h("span", {}, "A kind of ", byName.has(ing.parent) ? ingLink(ing.parent) : ing.parent, ". ") : null,
      children.length ? h("span", {}, "Kinds: ", children.flatMap((c, i) => [i ? ", " : "", ingLink(c.name)]), ".") : null) : null,
    tiles([[pct(ing.share), "share of menus"], [ing.restaurants.length, "restaurants"], [pairs.length, "pairings"],
      [ing.preparations[0]?.[0] || "–", "most common preparation"]]),
    h("div", { class: "grid2" },
      h("section", { class: "card" }, h("h2", {}, "Share of menus by region"), barChart(regions, { max: 1 })),
      accolades.length ? h("section", { class: "card" }, h("h2", {}, "Share of menus by accolade"),
        barChart(accolades, { max: 1, note: "Among the restaurants with menus in each group. A restaurant can be in several." })) : null,
      h("section", { class: "card" }, h("h2", {}, "Share of menus by season"),
        seasons.every(([g]) => g === "Unknown")
          ? h("p", { class: "empty" }, "No menu dates in this build yet, so no seasons.")
          : !seasonsReady()
          ? h("p", { class: "empty" }, `Every menu so far was first seen ${D.seen_range[0] === D.seen_range[1] ? "on " + fmtDate(D.seen_range[0]) : `between ${fmtDate(D.seen_range[0])} and ${fmtDate(D.seen_range[1])}`}, so seasons would only show which hemisphere a restaurant is in. They'll appear once the menus span most of a year.`)
          : barChart(seasons.filter(([g]) => g !== "Unknown"), { max: 1, note: "Southern Hemisphere dates shift six months; tropical restaurants are their own group." }))),
    h("div", { class: "grid2", style: "margin-top:1.25rem" },
      h("section", { class: "card" }, h("h2", {}, "Pairs with"),
        pairs.length
          ? table(pairs, [
            { key: "other", label: "With", render: (p) => h("a", { href: pairHref(name, p.other), title: `Every dish with ${name} and ${p.other}` }, p.other) },
            { key: "score", label: "Strength", render: (p) => strength(p.score, pairs[0] ? Math.max(...pairs.map((x) => x.score)) : 1) },
            { key: "restaurants", label: "Restaurants", num: true },
          ], { sort: "score", dir: "desc", page: 15 }, () => {})
          : h("p", { class: "empty" }, ing.pairing === "generic" ? "Too general an ingredient to pair." : `No pairing seen at ${D.min_restaurants}+ restaurants yet.`),
        hiddenStaples || staples ? h("p", { class: "muted", style: "font-size:.85rem;margin:.5rem 0 0" },
          staples ? "Including staples. " : `${hiddenStaples} pairing${hiddenStaples === 1 ? "" : "s"} with staples (${staplesNote()}…) hidden. `,
          h("a", { href: "#", onclick: (e) => { e.preventDefault(); toggleStaples(); } }, staples ? "Hide them" : "Show them")) : null),
      h("section", { class: "card stack" },
        h("div", {}, h("h2", {}, "Preparations"),
          ing.preparations.length
            ? h("div", { class: "chips" }, ing.preparations.map(([p, n]) => D.has_menus
              ? h("a", { class: "chip", href: coursesHref(name, { prep: p }), title: `${cap(name)}, ${p}: every course` }, p, h("span", { class: "n" }, n))
              : h("span", { class: "chip" }, p, h("span", { class: "n" }, n))))
            : h("p", { class: "empty" }, "None recorded.")),
        ing.aka?.length ? h("div", {}, h("h2", {}, "Also on menus as"),
          h("div", { class: "chips" }, [...ing.aka].sort((a, b) => (ing.aka_n?.[b] || 0) - (ing.aka_n?.[a] || 0)).map((a) => ing.aka_n?.[a] && D.has_menus
            ? h("a", { class: "chip", href: coursesHref(name, { as: a }), title: `Every course where the menu said “${a}”` }, a, h("span", { class: "n" }, ing.aka_n[a]))
            : h("span", { class: "chip muted" }, a)))) : null,
        h("div", {}, h("h2", {}, "Restaurants"),
          h("div", { class: "chips" }, rs.slice(0, LIST).map((r) => h("a", { class: "chip", href: "#/restaurant/" + encodeURIComponent(r.slug) }, r.name)),
            rs.length > LIST ? h("span", { class: "muted" }, `and ${rs.length - LIST} more, all on the map below`) : null)))),
    h("h2", { style: "margin-top:1.5rem" }, "Where it's on the menu"),
    coverageMap(new URLSearchParams(), { only: new Set(ing.restaurants), sync: false, shadeLabel: "restaurants using it" }));
}

function pairings(params) {
  const out = h("div", {});
  const accSel = h("select", {}, h("option", { value: "" }, "All restaurants"),
    accoladeGroups().map((g) => h("option", { value: g, selected: g === params.get("acc") }, `${g} (${D.groups.accolade_with_data[g]} with menus)`)));
  const staplesBox = h("input", { type: "checkbox", checked: params.get("staples") === "1" });
  const pool = () => ((accSel.value && D.pairs_by_accolade?.[accSel.value]) || D.pairs)
    .filter((p) => staplesBox.checked || (!isStaple(p.a) && !isStaple(p.b)));
  const maxR = D.pairs.reduce((m, p) => Math.max(m, p.restaurants), D.min_restaurants);
  // With lots of data, start from pairs seen at more restaurants so one-off dishes don't lead.
  const defaultMin = () => { const n = pool().length; return n > 2000 ? Math.min(5, maxR) : n > 500 ? Math.min(3, maxR) : D.min_restaurants; };
  const state = { sort: params.get("sort") || "score", dir: params.get("dir") || "desc" };
  const search = h("input", { type: "search", placeholder: "filter by ingredient", value: params.get("q") || "", "aria-label": "Filter pairings by ingredient" });
  const steps = [...new Set([D.min_restaurants, 3, 5, 10, 20, 50, 100].filter((v) => v >= D.min_restaurants && v <= maxR))];
  const minSel = h("select", {}, steps.map((n, i) => {
    const v = String(n);
    return h("option", { value: v, selected: v === (params.get("min") || String(defaultMin())) }, v + "+");
  }));
  const chartHost = h("section", { class: "card" });
  const tableHost = h("div", {});

  const live = () => {
    const p = new URLSearchParams();
    const q = search.value.toLowerCase().trim(), min = Number(minSel.value);
    if (q) p.set("q", q); if (min !== defaultMin()) p.set("min", min); if (accSel.value) p.set("acc", accSel.value);
    if (staplesBox.checked) p.set("staples", "1");
    if (state.sort !== "score") p.set("sort", state.sort); if (state.dir !== "desc") p.set("dir", state.dir);
    setParams(p);
    const all = pool();
    const rows = all.filter((x) => x.restaurants >= min && (!q || x.a.includes(q) || x.b.includes(q)));
    chartHost.replaceChildren(h("h2", {}, "Every pairing at a glance"),
      h("p", { class: "muted", style: "margin-top:-.5rem;font-size:.85rem" }, "Hover a dot to see the pair; click it to see the dishes."),
      scatter(rows));
    const top = Math.max(...rows.map((r) => r.score), 1e-9);
    tableHost.replaceChildren(h("p", { class: "muted" }, `${rows.length} of ${all.length} pairings` +
      (accSel.value ? `, scored among ${accSel.value} restaurants only` : "") +
      (accSel.value && all.length >= 3000 ? " (the 3,000 seen at the most restaurants)" : "")),
      table(rows, [
        { key: "a", label: "Pair", sort: (r) => r.a + " " + r.b, render: (r) => h("a", { href: pairHref(r.a, r.b), title: `Every dish with ${r.a} and ${r.b}` }, `${r.a} + ${r.b}`) },
        { key: "score", label: "Strength", render: (r) => strength(r.score, top) },
        { key: "restaurants", label: "Restaurants", num: true },
      ], state, (sort, dir) => { state.sort = sort; state.dir = dir; live(); }));
  };
  search.addEventListener("input", live);
  minSel.addEventListener("change", live);
  accSel.addEventListener("change", () => { minSel.value = String(defaultMin()); live(); });
  staplesBox.addEventListener("change", live);
  out.append(h("h1", {}, "Pairings"),
    ...(D.pairsError ? [h("p", { class: "banner" }, `The pairings file didn't load (${D.pairsError}). Reload the page to try again.`)] : []),
    h("p", { class: "sub" }, "Ingredient pairings, ranked by how strongly two ingredients go together compared with chance, and by how many of the restaurants we surveyed put them on the same plate. Click any pair to see the dishes."),
    h("div", { class: "filters" }, h("label", {}, "Ingredient", search), h("label", {}, "Restaurants", minSel),
      accoladeGroups().length ? h("label", {}, "Accolade", accSel) : null,
      h("label", { class: "check" }, staplesBox, `Include staples (${staplesNote()}…)`)),
    chartHost, tableHost);
  live();
  return out;
}

// Scatter: x = support (log scale), y = lift (log scale). One series.
function scatter(all) {
  if (!all.length) return h("p", { class: "empty" }, "No pairings match.");
  // Thousands of SVG points get slow; plot the pairs seen at the most restaurants.
  const CAP = 600;
  const rows = all.length <= CAP ? all : [...all].sort((a, b) => b.restaurants - a.restaurants || b.lift - a.lift).slice(0, CAP);
  const W = 640, H = 310, m = { l: 18, r: 12, t: 22, b: 28 };
  const xs = rows.map((r) => r.support), ys = rows.map((r) => r.lift);
  const lx = (v) => Math.log10(v), pad = 0.08;
  let x0 = lx(Math.min(...xs)), x1 = lx(Math.max(...xs)), y0 = lx(Math.min(1, ...ys)), y1 = lx(Math.max(...ys));
  if (x1 - x0 < 0.1) { x0 -= 0.1; x1 += 0.1; } if (y1 - y0 < 0.1) { y1 += 0.1; }
  const dx = (x1 - x0) * pad, dy = (y1 - y0) * pad; x0 -= dx; x1 += dx; y1 += dy;
  const X = (v) => m.l + ((lx(v) - x0) / (x1 - x0)) * (W - m.l - m.r);
  const Y = (v) => H - m.b - ((lx(v) - y0) / (y1 - y0)) * (H - m.t - m.b);
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, width: "100%", role: "img", "aria-label": "Pairings: higher is a stronger association, further right a more popular one" });
  svg.append(s("line", { class: "axis", x1: m.l, x2: m.l, y1: m.t, y2: H - m.b }), s("line", { class: "axis", x1: m.l, x2: W - m.r, y1: H - m.b, y2: H - m.b }));
  // below this line, two ingredients share a dish less often than chance would have them
  if (y0 < 0) svg.append(s("line", { class: "grid", x1: m.l, x2: W - m.r, y1: Y(1), y2: Y(1) }), s("text", { x: W - m.r, y: Y(1) - 5, "text-anchor": "end" }, "as often as chance"));
  svg.append(s("text", { class: "axislab", x: m.l + 6, y: m.t - 8 }, "↑ stronger association"));
  svg.append(s("text", { class: "axislab", x: W - m.r, y: H - 8, "text-anchor": "end" }, "more popular →"));
  for (const r of [...rows].sort((a, b) => a.restaurants - b.restaurants)) {
    const cx = X(r.support), cy = Y(r.lift);
    // smaller dots when there are many, so dense regions stay readable
    const dense = rows.length > 150;
    const dot = s("circle", { class: "dot", cx, cy, r: dense ? 2.2 + Math.min(3, Math.log2(r.restaurants)) : 4 + Math.min(4, r.restaurants - 1),
      style: dense ? "stroke-width:1" : null });
    const hit = s("circle", { class: "hit", cx, cy, r: 12, tabindex: "0", role: "link", "aria-label": `${r.a} and ${r.b}` });
    hoverable(hit, `${r.a} + ${r.b}`, `at ${r.restaurants} restaurants · click for the dishes`);
    hit.addEventListener("pointerenter", () => dot.classList.add("hl"));
    hit.addEventListener("pointerleave", () => dot.classList.remove("hl"));
    const go = () => { location.hash = pairHref(r.a, r.b); };
    hit.addEventListener("click", go);
    hit.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
    svg.append(dot, hit);
  }
  if (rows.length < all.length) {
    return h("div", {}, svg, h("p", { class: "muted", style: "font-size:.8rem;margin:.25rem 0 0" },
      `Showing the ${rows.length} pairs seen at the most restaurants, out of ${all.length}. Raise “Restaurants” to narrow it.`));
  }
  return svg;
}

function restaurants(params) {
  const out = h("div", {});
  const state = { sort: params.get("sort") || "name", dir: params.get("dir") || "asc" };
  const opt = (v, l, sel) => h("option", { value: v, selected: v === sel }, l);
  const search = h("input", { type: "search", placeholder: "name or city", value: params.get("q") || "", "aria-label": "Search restaurants by name or city" });
  const regionSel = h("select", {}, opt("", "All regions", params.get("region") || ""), D.groups.region.map((g) => opt(g, g, params.get("region") || "")));
  // Restaurants without menus are hidden unless asked for ("menus=all" and the others).
  const menuSel = h("select", {}, [["", "With menus"], ["all", "All, with or without menus"], ["yes", "Publishes a menu"], ["blocked", "Opted out or blocked"]]
    .map(([v, l]) => opt(v, l, params.get("menus") || "")));
  const menuMatch = (r, f) => (!f ? r.menus > 0 : f === "all" ? true : f === "yes" ? r.publishes_menu === "yes" : r.blocked);
  const accSel = accoladeSelect(params.get("acc") || "", undefined, () => D.restaurants.filter((r) => menuMatch(r, menuSel.value)));
  const groupSel = h("select", {}, [["", "No grouping"], ["region", "Region"], ["accolade", "Accolade"]].map(([v, l]) => opt(v, l, params.get("by") || "")));
  const summaryHost = h("div", {});
  const host = h("div", {});
  const live = () => {
    const p = new URLSearchParams();
    const q = fold(search.value.trim());
    if (search.value) p.set("q", search.value);
    if (regionSel.value) p.set("region", regionSel.value);
    if (accSel.value) p.set("acc", accSel.value);
    if (menuSel.value) p.set("menus", menuSel.value);
    if (groupSel.value) p.set("by", groupSel.value);
    if (state.sort !== "name") p.set("sort", state.sort); if (state.dir !== "asc") p.set("dir", state.dir);
    setParams(p);
    const rows = D.restaurants.filter((r) => (!regionSel.value || r.region === regionSel.value) && accoladeMatch(r, accSel.value)
      && menuMatch(r, menuSel.value) && (!q || fold(r.name).includes(q) || fold(r.city).includes(q)))
      .map((r) => ({ ...r, acc: accoladeScore(r.accolades) }));
    summaryHost.replaceChildren(groupSel.value ? groupSummary(rows, groupSel.value) : "");
    host.replaceChildren(h("p", { class: "muted" }, `${rows.length.toLocaleString()} of ${D.restaurants.length.toLocaleString()} restaurants`,
      menuSel.value ? "" : h("span", {}, " · ", h("a", { href: "#/restaurants?menus=all", onclick: (e) => { e.preventDefault(); menuSel.value = "all"; accSel.refresh(); live(); } }, "show the ones without menus too"))),
      table(rows, [
        { key: "name", label: "Restaurant", render: (r) => h("a", { href: "#/restaurant/" + encodeURIComponent(r.slug) }, r.name) },
        { key: "city", label: "City", render: (r) => r.city || "–" },
        { key: "region", label: "Region" },
        { key: "acc", label: "Accolade", first: "desc", render: (r) => accoladeCell(r.accolades) },
        { key: "menus", label: "Menus", num: true },
        { key: "ingredients", label: "Ingredients", num: true },
        { key: "url", label: "Source", nosort: true, render: (r) => r.url ? h("a", { href: r.url, rel: "noopener", target: "_blank", style: "white-space:nowrap" }, "website ↗") : h("span", { class: "muted" }, "–") },
      ], state, (sort, dir) => { state.sort = sort; state.dir = dir; live(); }));
  };
  search.addEventListener("input", live);
  for (const el of [regionSel, accSel, menuSel, groupSel]) el.addEventListener("change", live);
  menuSel.addEventListener("change", () => { accSel.refresh(); live(); });
  out.append(h("h1", {}, "Restaurants"),
    h("p", { class: "sub" }, "Restaurants whose published menus we've read. Change “Menus” to see the rest of the list. Stars are from the most recent Michelin guide the list records."),
    h("div", { class: "filters" }, h("label", {}, "Search", search), h("label", {}, "Region", regionSel), h("label", {}, "Accolade", accSel),
      h("label", {}, "Menus", menuSel), h("label", {}, "Group by", groupSel)),
    summaryHost, host);
  live();
  return out;
}

// One row per region or accolade group for the filtered restaurants.
function groupSummary(rows, by) {
  const keys = by === "region" ? D.groups.region : [...(D.groups.accolade || []), "None of these lists"];
  const of = (r) => by === "region" ? [r.region] : (r.tiers?.length ? r.tiers : ["None of these lists"]);
  const acc = new Map(keys.map((k) => [k, { group: k, restaurants: 0, yes: 0, data: 0, menus: 0 }]));
  for (const r of rows) for (const k of of(r)) {
    const g = acc.get(k); if (!g) continue;
    g.restaurants++; if (r.publishes_menu === "yes") g.yes++; if (r.menus) g.data++; g.menus += r.menus;
  }
  const out = [...acc.values()].filter((g) => g.restaurants).map((g) => ({ ...g, rate: g.data / g.restaurants }));
  return h("section", { class: "card" }, h("h2", {}, by === "region" ? "By region" : "By accolade"),
    by === "accolade" ? h("p", { class: "muted", style: "margin-top:-.5rem;font-size:.85rem" }, "A restaurant can be in several groups.") : null,
    table(out, [
      { key: "group", label: by === "region" ? "Region" : "Accolade" },
      { key: "restaurants", label: "Restaurants", num: true },
      { key: "yes", label: "Publish a menu", num: true },
      { key: "data", label: "With menus", num: true },
      { key: "rate", label: "Covered", num: true, render: (g) => pct(g.rate) },
    ], { sort: "restaurants", dir: "desc" }, () => {}));
}

function restaurant(_params, [slug]) {
  const r = byRestaurant.get(slug);
  if (!r) return notFound();
  const used = D.ingredients.filter((i) => i.restaurants.includes(slug));
  // Distinctive: used here, and by few others.
  const distinct = [...used].sort((a, b) => a.restaurants.length - b.restaurants.length || b.share - a.share);
  return h("div", {},
    h("p", { class: "muted", style: "margin:0 0 .25rem" }, h("a", { href: "#/restaurants" }, "← Restaurants")),
    h("h1", {}, r.name),
    accoladeText(r.accolades) ? h("p", { class: "acc", style: "margin:-.1rem 0 .4rem;font-size:1.1rem", title: accoladeTitle(r.accolades) }, accoladeNodes(r.accolades)) : null,
    h("p", { class: "sub" }, [r.city, r.country].filter(Boolean).join(", ") + (r.region ? ` · ${r.region}` : ""),
      r.url ? h("span", {}, " · ", h("a", { href: r.url, rel: "noopener", target: "_blank" }, "restaurant website ↗")) : null),
    tiles([[r.menus, "distinct menus"], [r.ingredients, "ingredients"], [r.publishes_menu === "yes" ? "Yes" : r.publishes_menu === "no" ? "No" : "?", "publishes a menu"]]),
    r.menu_list?.length ? h("section", { class: "stack", style: "margin-bottom:1.25rem" },
      h("h2", { style: "margin:0" }, r.menu_list.length > 1 ? `Menus (${r.menu_list.length})` : "Menu"),
      r.menu_list.map((m, i) => menuCard(m, i === 0, r))) : null,
    h("section", { class: "card" }, h("h2", {}, "Ingredients, most distinctive first"),
      used.length
        ? h("div", { class: "chips" }, distinct.map((i) => h("a", { class: "chip", href: "#/ingredient/" + encodeURIComponent(i.name) }, i.name,
          h("span", { class: "n", title: "restaurants using it" }, i.restaurants.length))))
        : h("p", { class: "empty" }, "No menu data for this restaurant.")),
    r.menu_list?.length ? null : h("p", { class: "muted", style: "font-size:.85rem" }, "Only ingredient names are shown here. For the dishes themselves, see the restaurant's own site."),
    !r.url || !r.menus ? h("p", { class: "card suggest" }, r.url ? "We haven't found a menu on its website. Know the page it's on? " : "We don't know this restaurant's website. Know it? ",
      ext(SUGGEST(r), r.url ? "Suggest the menu page ↗" : "Suggest it ↗")) : null,
    h("p", { class: "muted", style: "font-size:.85rem" }, "Is this your restaurant? ", ext(OPT_OUT, "Ask to be removed ↗"), " · Something wrong? ", ext(CORRECTION, "Report it ↗")));
}

// One menu as a manifest: courses in order, each with its ingredients and how they're
// prepared. Ingredient names only; the restaurant's own wording stays on its site.
const MENU_TYPES = { tasting: "Tasting menu", a_la_carte: "À la carte", vegetarian: "Vegetarian menu", lunch: "Lunch menu",
  prix_fixe_lunch: "Set lunch", prix_fixe_dinner: "Set dinner", kaiseki: "Kaiseki", event: "Event menu" };
const LANGS = { en: "English", fr: "French", de: "German", it: "Italian", es: "Spanish", nl: "Dutch", da: "Danish", sv: "Swedish",
  no: "Norwegian", pt: "Portuguese", ja: "Japanese", zh: "Chinese", ko: "Korean", ca: "Catalan", fi: "Finnish" };
function fmtDate(d) {
  try { return new Date(d + "T00:00:00Z").toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric", timeZone: "UTC" }); }
  catch { return d; }
}
// One course's ingredients with their preparations. `hit` marks the item a course list is
// about; that one also shows the menu's own name for it when it used another ("beef (wagyu)").
function courseItems(c, hit) {
  return h("span", { class: "mc-items" }, c.i.flatMap(([name, preps, term], j) => {
    const on = hit && hit(name, preps, term);
    return [j ? h("span", { class: "sep" }, " · ") : null,
    h("span", { class: "mi" + (on ? " hit" : "") }, byName.has(name) ? ingLink(name) : name,
      on && term ? h("span", { class: "muted mt" }, ` (${term})`) : null,
      preps.length ? h("span", { class: "muted mp" }, " " + preps.join(", ")) : null)];
  }));
}
// Every dish with both ingredients; without menus in this build, the first ingredient's page.
const pairHref = (a, b) => (D.has_menus ? "#/courses?" + new URLSearchParams({ i: a, with: b }) : "#/ingredient/" + encodeURIComponent(a));
const strength = (v, max) => h("div", { class: "sbar", title: "How much more often than chance the two share a dish" },
  h("div", { class: "track" }, h("div", { class: "fill", style: `width:${Math.max(0, Math.min(1, v / max)) * 100}%` })),
  h("span", { class: "val" }, num(v, 1)));
const coursesHref = (name, { prep, as } = {}) => "#/courses?" + new URLSearchParams({ i: name, ...(prep ? { prep } : {}), ...(as ? { as } : {}) });

// Every course, across all restaurants, where an ingredient appears prepared one way
// (?i=beef&prep=braised) or under one of its other names (?i=beef&as=wagyu).
function courses(params) {
  const name = params.get("i") || "", prep = params.get("prep"), as = params.get("as"), partner = params.get("with");
  const ing = byName.get(name);
  if (!ing || !(prep || as || byName.has(partner))) return notFound();
  // `match` marks the items the list is about; `has` picks the courses
  const match = (n, preps, term) => (partner ? n === name || n === partner : n === name && (prep ? preps.includes(prep) : term === as));
  const has = (c) => (partner ? c.i.some(([n]) => n === name) && c.i.some(([n]) => n === partner) : c.i.some(([n, p, t]) => match(n, p, t)));
  const found = [];
  for (const r of D.restaurants) {
    for (const m of r.menu_list || []) {
      let k = 0;
      for (const c of m.courses) {
        if (c.k === "section") continue; // a whole section the extractor couldn't split into dishes
        if (c.k !== "drink") k++;
        if (has(c)) found.push({ r, m, c, n: c.k === "drink" ? null : k });
      }
    }
  }
  const state = { sort: params.get("sort") || "acc", dir: params.get("dir") || "desc", page: 50 };
  const regionSel = h("select", {}, h("option", { value: "" }, "All regions"),
    D.groups.region.filter((g) => found.some((f) => f.r.region === g)).map((g) => h("option", { value: g, selected: g === params.get("region") }, g)));
  const host = h("div", {}), withHost = h("div", {});
  const live = () => {
    const p = new URLSearchParams({ i: name, ...(prep ? { prep } : {}), ...(as ? { as } : {}), ...(partner ? { with: partner } : {}) });
    if (regionSel.value) p.set("region", regionSel.value);
    if (state.sort !== "acc") p.set("sort", state.sort); if (state.dir !== "desc") p.set("dir", state.dir);
    setParams(p);
    const rows = found.filter((f) => !regionSel.value || f.r.region === regionSel.value)
      .map((f) => ({ ...f, name: f.r.name, region: f.r.region, acc: accoladeScore(f.r.accolades) }));
    const places = new Set(rows.map((f) => f.r.slug));
    // what else is on the plate, counted by restaurant
    const other = new Map();
    const shown = (n) => n !== name && n !== partner && byName.get(n)?.pairing !== "generic" && (isStaple(name) || !isStaple(n));
    for (const f of rows) for (const [n] of f.c.i) if (shown(n)) (other.get(n) || other.set(n, new Set()).get(n)).add(f.r.slug);
    const top = [...other].map(([n, s]) => [n, s.size]).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).slice(0, 16);
    withHost.replaceChildren(top.length ? h("section", { class: "card" }, h("h2", {}, partner ? "Most often on the plate with them" : "Most often on the plate with it"),
      h("div", { class: "chips" }, top.map(([n, c]) => h("a", { class: "chip", href: "#/ingredient/" + encodeURIComponent(n) }, n, h("span", { class: "n", title: "restaurants" }, c))))) : "");
    host.replaceChildren(h("p", { class: "muted" }, `${rows.length.toLocaleString()} course${rows.length === 1 ? "" : "s"} at ${places.size.toLocaleString()} restaurant${places.size === 1 ? "" : "s"}`),
      h("div", { class: "stacked" }, table(rows, [
        // "acc" isn't a column: it falls back to this one, sorted by accolades
        { key: "name", label: "Restaurant", sort: (f) => (state.sort === "acc" ? f.acc : f.name), render: (f) => h("span", {}, h("a", { href: "#/restaurant/" + encodeURIComponent(f.r.slug) }, f.r.name),
          accoladeText(f.r.accolades) ? h("span", { class: "acc", title: accoladeTitle(f.r.accolades), style: "margin-left:.4rem" }, accoladeNodes(f.r.accolades)) : null,
          h("div", { class: "muted", style: "font-size:.85rem" }, [f.r.city, f.r.country].filter(Boolean).join(", "))) },
        { key: "region", label: "Region" },
        { key: "course", label: "Course", nosort: true, render: (f) => h("div", {},
          h("div", { class: "muted", style: "font-size:.78rem" }, [MENU_TYPES[f.m.type] || "Menu", f.n ? `course ${f.n}` : f.c.k].filter(Boolean).join(" · ")),
          courseItems(f.c, match)) },
      ], state, (sort, dir) => { state.sort = sort; state.dir = dir; live(); })));
  };
  regionSel.addEventListener("change", live);
  live();
  return h("div", {},
    h("p", { class: "muted", style: "margin:0 0 .25rem" }, partner
      ? h("span", {}, h("a", { href: "#/pairings" }, "← Pairings"), " · ", ingLink(name), " · ", ingLink(partner))
      : h("a", { href: "#/ingredient/" + encodeURIComponent(name) }, "← " + cap(name))),
    h("h1", {}, partner ? `${cap(name)} + ${partner}` : prep ? `${cap(name)}, ${prep}` : `${cap(name)} as “${as}”`),
    h("p", { class: "sub" }, partner
      ? `Every dish with both ${name} and ${partner}, on the menus of every restaurant we read. Each is shown as its ingredients; for the dish itself, see the restaurant's site.`
      : prep
      ? `Every course where ${name} is ${prep}, on the menus of every restaurant. Each course is shown as its ingredients; for the dish itself, see the restaurant's site.`
      : `Every course where the menu calls ${name} “${as}”. Each course is shown as its ingredients; for the dish itself, see the restaurant's site.`),
    h("div", { class: "filters" }, h("label", {}, "Region", regionSel),
      h("label", {}, "Sort", (() => {
        const sel = h("select", {}, [["acc", "Most decorated first"], ["name", "Restaurant A–Z"], ["region", "Region"]].map(([v, l]) => h("option", { value: v, selected: v === state.sort }, l)));
        sel.addEventListener("change", () => { state.sort = sel.value; state.dir = sel.value === "acc" ? "desc" : "asc"; live(); });
        return sel;
      })())),
    withHost, host);
}

function menuCard(m, open, r) {
  const n = new Set(m.courses.flatMap((c) => c.i.map(([name]) => name))).size;
  const when = m.first_seen
    ? (m.last_seen && m.last_seen !== m.first_seen ? `seen ${fmtDate(m.first_seen)} to ${fmtDate(m.last_seen)}` : `seen ${fmtDate(m.first_seen)}`)
    : "date not recorded in this build";
  let price = null;
  if (m.price != null) {
    try { price = new Intl.NumberFormat(undefined, { style: "currency", currency: m.currency || "EUR", maximumFractionDigits: 0 }).format(m.price); }
    catch { price = `${m.price} ${m.currency || ""}`.trim(); }
  }
  const title = MENU_TYPES[m.type] || "Menu";
  const dishes = m.courses.filter((c) => c.k !== "section").length;
  const meta = [`${dishes} courses`, `${n} ingredients`, price, LANGS[m.lang] || m.lang, m.period, when].filter(Boolean).join(" · ");
  let k = 0;
  return h("details", { class: "card menu", open: open || null },
    h("summary", {}, h("span", {}, title), h("span", { class: "muted menu-meta" }, meta)),
    h("ol", { class: "courses" }, m.courses.map((c) => {
      const plain = c.k === "drink" || c.k === "section";
      if (!plain) k++;
      return h("li", { class: "mc" + (c.k === "section" ? " mc-section" : "") },
        h("span", { class: "mc-n" }, plain ? "" : String(k)),
        h("div", { class: "mc-body" },
          c.k === "section"
            ? h("span", { class: "mc-kind", title: "Several dishes the extractor couldn't tell apart; left out of pairings and course lists" }, "section, not split into dishes")
            : c.k && c.k !== "course" ? h("span", { class: "mc-kind" }, c.k) : null,
          courseItems(c)));
    })),
    r.url ? h("p", { class: "muted", style: "font-size:.82rem;margin:.6rem 0 0" }, "Ingredients and preparations as read by the extractor. For the dishes as the chef wrote them, see ",
      h("a", { href: r.url, rel: "noopener", target: "_blank" }, "the restaurant's site ↗"), ".") : null);
}

// --- coverage map -------------------------------------------------------------

// Equal Earth projection (lon/lat in degrees -> unit plane, y up).
function equalEarth(lon, lat) {
  const A1 = 1.340264, A2 = -0.081106, A3 = 0.000893, A4 = 0.003796, M = Math.sqrt(3) / 2;
  const l = (lon * Math.PI) / 180, th = Math.asin(M * Math.sin((lat * Math.PI) / 180));
  const t2 = th * th, t6 = t2 * t2 * t2;
  return [(l * Math.cos(th)) / (M * (A1 + 3 * A2 * t2 + t6 * (7 * A3 + 9 * A4 * t2))),
    th * (A1 + A2 * t2 + t6 * (A3 + A4 * t2))];
}

// [west, south, east, north] in degrees
const VIEWS = {
  World: [-180, -56, 180, 78], Europe: [-11, 35, 32, 64], "North America": [-128, 14, -60, 52],
  "Latin America": [-118, -45, -34, 33], Asia: [68, -10, 146, 46], Oceania: [110, -48, 180, -8],
};

const STATUS = [
  // drawn in this order, so the categories that matter most end up on top
  { key: "unknown", label: "Not checked yet", color: "--dot-unknown", hollow: true },
  { key: "no", label: "No menu on its site", color: "--dot-none", hollow: true },
  { key: "blocked", label: "Site opts out of crawling", color: "--dot-blocked", hollow: true },
  { key: "yes", label: "Publishes, not collected yet", color: "--dot-pending" },
  { key: "data", label: "Menus collected", color: "--accent" },
];
const statusOf = (r) => (r.menus > 0 ? "data" : r.blocked ? "blocked" : r.publishes_menu === "yes" ? "yes"
  : r.publishes_menu === "no" ? "no" : "unknown");

// Seed-list country names and ISO codes -> Natural Earth names. Anything else is
// matched by name, then by which outline contains the restaurant.
const COUNTRY_ALIASES = {
  us: "United States of America", usa: "United States of America", "united states": "United States of America",
  gb: "United Kingdom", uk: "United Kingdom", england: "United Kingdom", scotland: "United Kingdom", wales: "United Kingdom",
  dk: "Denmark", se: "Sweden", no: "Norway", fi: "Finland", is: "Iceland", fr: "France", es: "Spain", it: "Italy",
  de: "Germany", be: "Belgium", nl: "Netherlands", ch: "Switzerland", at: "Austria", pt: "Portugal", ie: "Ireland",
  mx: "Mexico", pe: "Peru", br: "Brazil", ca: "Canada", jp: "Japan", kr: "South Korea", cn: "China", tw: "Taiwan",
  th: "Thailand", in: "India", ae: "United Arab Emirates", au: "Australia", nz: "New Zealand",
  "czech republic": "Czechia", "republic of korea": "South Korea", korea: "South Korea", "türkiye": "Turkey",
  "north macedonia": "Macedonia", "bosnia and herzegovina": "Bosnia and Herz.", "dominican republic": "Dominican Rep.",
};
// Too small for 1:110m outlines; their dots still show, but no country is shaded for them.
const NO_OUTLINE = new Set(["hong kong", "hk", "macau", "macao", "mo", "singapore", "sg", "monaco", "mc", "luxembourg city"]);

function pointInRings(rings, x, y) {
  let inside = false;
  for (const r of rings) {
    for (let i = 0, j = r.length - 2; i < r.length; j = i, i += 2) {
      const xi = r[i], yi = r[i + 1], xj = r[j], yj = r[j + 1];
      if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
    }
  }
  return inside;
}

let WORLD = null; // countries projected once: {name, rings, bbox:[x0,y0,x1,y1]}
function world() {
  if (WORLD) return WORLD;
  WORLD = (window.MENU_ATLAS_WORLD || []).map((c) => {
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    const rings = c.r.map((ring) => {
      const out = new Float64Array(ring.length);
      for (let i = 0; i < ring.length; i += 2) {
        const [x, y] = equalEarth(ring[i], ring[i + 1]);
        out[i] = x; out[i + 1] = y;
        x0 = Math.min(x0, x); x1 = Math.max(x1, x); y0 = Math.min(y0, y); y1 = Math.max(y1, y);
      }
      return out;
    });
    return { name: c.n, rings, bbox: [x0, y0, x1, y1] };
  });
  return WORLD;
}

function countryOf(r, x, y, byName) {
  const key = (r.country || "").trim().toLowerCase();
  if (NO_OUTLINE.has(key) || NO_OUTLINE.has((r.city || "").trim().toLowerCase())) return null;
  const named = byName.get((COUNTRY_ALIASES[key] || key).toLowerCase());
  if (named) return named;
  return world().find((c) => x >= c.bbox[0] && x <= c.bbox[2] && y >= c.bbox[1] && y <= c.bbox[3] && pointInRings(c.rings, x, y)) || null;
}

function coverageMap(params, opts = {}) {
  const source = opts.only ? D.restaurants.filter((r) => opts.only.has(r.slug)) : D.restaurants;
  const countries = world();
  const byName = new Map(countries.map((c) => [c.name.toLowerCase(), c]));
  const stats = new Map(); // country -> {total, yes, data}
  const dots = [];
  for (const r of source) {
    if (r.lat == null || r.lon == null) continue;
    const [x, y] = equalEarth(r.lon, r.lat);
    const st = statusOf(r), c = countryOf(r, x, y, byName);
    dots.push({ r, x, y, st, c });
    if (c) {
      const s = stats.get(c) || stats.set(c, { total: 0, yes: 0, data: 0 }).get(c);
      s.total++; if (r.publishes_menu === "yes") s.yes++; if (st === "data") s.data++;
    }
  }
  const maxTotal = Math.max(1, ...[...stats.values()].map((s) => s.total));
  // Only restaurants with menus show until the legend turns the others on ("hide=none" shows all).
  const DEFAULT_HIDDEN = STATUS.map((st) => st.key).filter((k) => k !== "data").join(",");
  const hidden = new Set((params.get("hide") ?? DEFAULT_HIDDEN).split(",").filter((k) => k && k !== "none"));
  let preset = VIEWS[params.get("view")] ? params.get("view") : "World";

  const canvas = h("canvas", { class: "map", role: "img", tabindex: "0",
    "aria-label": `Map of ${dots.length} restaurants by menu status. Use plus and minus to zoom and the arrow keys to move. A table by region follows.` });
  const wrap = h("div", { class: "mapwrap" }, canvas);
  const counts = Object.fromEntries(STATUS.map((st) => [st.key, 0]));
  for (const d of dots) counts[d.st]++;

  // camera: screen = (ox + k*x, oy - k*y)
  let W = 0, H = 0, k = 1, ox = 0, oy = 0, minK = 1, hover = null, hoverCountry = null, frame = 0, anim = 0;
  const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

  const sync = () => {
    if (opts.sync === false) return;
    const p = new URLSearchParams(opts.extra || {});
    if (preset && preset !== "World") p.set("view", preset);
    const key = STATUS.map((st) => st.key).filter((k) => hidden.has(k)).join(",");
    if (key !== DEFAULT_HIDDEN) p.set("hide", key || "none");
    setParams(p);
  };

  function boxCamera(x0, y0, x1, y1, pad = 16) {
    const kk = Math.min((W - 2 * pad) / (x1 - x0), (H - 2 * pad) / (y1 - y0));
    return [kk, (W - kk * (x1 - x0)) / 2 - kk * x0, (H - kk * (y1 - y0)) / 2 + kk * y1];
  }
  function presetBox(name) {
    const [w, s, e, n] = VIEWS[name];
    let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
    for (let i = 0; i <= 16; i++) { // sample the edges, since meridians curve
      for (const [lo, la] of [[w + ((e - w) * i) / 16, s], [w + ((e - w) * i) / 16, n], [w, s + ((n - s) * i) / 16], [e, s + ((n - s) * i) / 16]]) {
        const [x, y] = equalEarth(lo, la);
        x0 = Math.min(x0, x); x1 = Math.max(x1, x); y0 = Math.min(y0, y); y1 = Math.max(y1, y);
      }
    }
    return [x0, y0, x1, y1];
  }
  function flyTo(target) {
    cancelAnimationFrame(anim);
    if (reduced) { [k, ox, oy] = target; return redraw(); }
    const from = [k, ox, oy], t0 = performance.now();
    const step = (now) => {
      const t = Math.min(1, (now - t0) / 350), e = 1 - (1 - t) ** 3;
      // interpolate scale in log space so zooms feel even
      k = Math.exp(Math.log(from[0]) + (Math.log(target[0]) - Math.log(from[0])) * e);
      ox = from[1] + (target[1] - from[1]) * e; oy = from[2] + (target[2] - from[2]) * e;
      draw();
      if (t < 1) anim = requestAnimationFrame(step);
    };
    anim = requestAnimationFrame(step);
  }
  function zoomAt(sx, sy, f) {
    const k2 = Math.max(minK * 0.8, Math.min(minK * 60, k * f));
    ox = sx + ((ox - sx) * k2) / k; oy = sy + ((oy - sy) * k2) / k; k = k2;
    preset = null; setPressed(); redraw();
  }
  const redraw = () => { cancelAnimationFrame(frame); frame = requestAnimationFrame(draw); };

  function resize() {
    const w = wrap.clientWidth || 640, hh = Math.round(Math.min(600, Math.max(280, w * 0.55)));
    const first = !W, oldW = W, oldH = H;
    W = w; H = hh;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = W * dpr; canvas.height = H * dpr;
    canvas.style.width = W + "px"; canvas.style.height = H + "px";
    minK = boxCamera(...presetBox("World"))[0];
    if (first || preset) [k, ox, oy] = boxCamera(...presetBox(preset || "World"));
    else { ox += (W - oldW) / 2; oy += (H - oldH) / 2; }
    draw();
  }

  function draw() {
    if (!W) return;
    const css = getComputedStyle(document.documentElement);
    const col = (v) => css.getPropertyValue(v).trim();
    const ctx = canvas.getContext("2d"), dpr = window.devicePixelRatio || 1;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = col("--map-sea"); ctx.fillRect(0, 0, W, H);
    const path = (c) => {
      ctx.beginPath();
      for (const r of c.rings) {
        for (let i = 0; i < r.length; i += 2) {
          const X = ox + k * r[i], Y = oy - k * r[i + 1];
          if (i) ctx.lineTo(X, Y); else ctx.moveTo(X, Y);
        }
        ctx.closePath();
      }
    };
    const visible = (c) => ox + k * c.bbox[2] >= 0 && ox + k * c.bbox[0] <= W && oy - k * c.bbox[1] >= 0 && oy - k * c.bbox[3] <= H;
    const land = col("--map-land"), accent = col("--accent"), border = col("--map-sea");
    const shadeMin = parseFloat(col("--map-shade-min")) || 0.1, shadeMax = parseFloat(col("--map-shade-max")) || 0.42;
    for (const c of countries) {
      if (!visible(c)) continue;
      path(c);
      ctx.fillStyle = land; ctx.fill("evenodd");
      const s = stats.get(c);
      if (s) { // shade by restaurants in the list, log scale
        ctx.globalAlpha = shadeMin + ((shadeMax - shadeMin) * Math.log1p(s.total)) / Math.log1p(maxTotal);
        ctx.fillStyle = accent; ctx.fill("evenodd"); ctx.globalAlpha = 1;
      }
      ctx.strokeStyle = border; ctx.lineWidth = 0.75; ctx.stroke();
    }
    if (hoverCountry && visible(hoverCountry)) {
      path(hoverCountry); ctx.strokeStyle = accent; ctx.lineWidth = 2; ctx.stroke();
    }
    const grow = Math.min(1.8, (k / minK) ** 0.35);
    for (const st of STATUS) {
      if (hidden.has(st.key)) continue;
      const c = col(st.color);
      for (const d of dots) {
        if (d.st !== st.key) continue;
        const X = ox + k * d.x, Y = oy - k * d.y;
        if (X < -10 || Y < -10 || X > W + 10 || Y > H + 10) continue;
        const rad = (st.key === "data" ? 3.5 : 2.75) * grow;
        ctx.globalAlpha = d.r.approx ? 0.55 : 1;
        ctx.beginPath(); ctx.arc(X, Y, rad, 0, 2 * Math.PI);
        if (st.hollow) { ctx.strokeStyle = c; ctx.lineWidth = 1.5; ctx.stroke(); }
        else { ctx.fillStyle = c; ctx.fill(); ctx.strokeStyle = border; ctx.lineWidth = 1; ctx.stroke(); }
      }
    }
    ctx.globalAlpha = 1;
    if (hover) {
      ctx.beginPath(); ctx.arc(ox + k * hover.x, oy - k * hover.y, 7 * grow, 0, 2 * Math.PI);
      ctx.strokeStyle = col("--text"); ctx.lineWidth = 2; ctx.stroke();
    }
  }

  // --- what's under the pointer
  const local = (ev) => { const b = canvas.getBoundingClientRect(); return [ev.clientX - b.left, ev.clientY - b.top]; };
  function dotAt(sx, sy) {
    const grow = Math.min(1.8, (k / minK) ** 0.35);
    let best = null, bd = Infinity;
    for (const d of dots) {
      if (hidden.has(d.st)) continue;
      const dist = Math.hypot(ox + k * d.x - sx, oy - k * d.y - sy);
      // ties go to the category drawn on top
      const reach = Math.max(9, 5 * grow), rank = STATUS.findIndex((s) => s.key === d.st);
      if (dist <= reach && dist - rank * 0.01 < bd) { best = d; bd = dist - rank * 0.01; }
    }
    return best;
  }
  function countryAt(sx, sy) {
    const x = (sx - ox) / k, y = (oy - sy) / k;
    return countries.find((c) => x >= c.bbox[0] && x <= c.bbox[2] && y >= c.bbox[1] && y <= c.bbox[3] && pointInRings(c.rings, x, y)) || null;
  }
  function describe(ev, sx, sy) {
    const d = dotAt(sx, sy), c = d ? null : countryAt(sx, sy);
    if (d !== hover || c !== hoverCountry) { hover = d; hoverCountry = c; redraw(); }
    canvas.style.cursor = d || c ? "pointer" : "grab";
    if (d) {
      const r = d.r, st = STATUS.find((x) => x.key === d.st);
      showTip(ev, r.name, [r.city, r.country].filter(Boolean).join(", ") + " · " + st.label
        + (r.menus ? ` (${r.menus} menu${r.menus > 1 ? "s" : ""})` : "") + (r.approx ? " · location approximate (city)" : ""));
    } else if (c) {
      const s = stats.get(c);
      showTip(ev, c.name, s ? `${s.total} restaurant${s.total > 1 ? "s" : ""} · ${s.yes} publish a menu · ${s.data} with menus collected` : "No restaurants in the list");
    } else hideTip();
  }

  // --- pan, zoom, pinch, click
  const pointers = new Map();
  let drag = null; // {x, y, moved, pinch}
  canvas.addEventListener("pointerdown", (ev) => {
    canvas.setPointerCapture(ev.pointerId);
    pointers.set(ev.pointerId, local(ev));
    cancelAnimationFrame(anim);
    if (pointers.size === 1) drag = { start: local(ev), last: local(ev), moved: false };
    else if (pointers.size === 2) {
      const [a, b] = [...pointers.values()];
      drag = { pinch: Math.hypot(a[0] - b[0], a[1] - b[1]), moved: true };
    }
  });
  canvas.addEventListener("pointermove", (ev) => {
    const p = local(ev);
    if (!pointers.has(ev.pointerId)) return describe(ev, ...p);
    pointers.set(ev.pointerId, p);
    if (pointers.size === 2 && drag?.pinch) {
      const [a, b] = [...pointers.values()];
      const dist = Math.hypot(a[0] - b[0], a[1] - b[1]);
      zoomAt((a[0] + b[0]) / 2, (a[1] + b[1]) / 2, dist / drag.pinch);
      drag.pinch = dist;
    } else if (drag && !drag.pinch) {
      if (!drag.moved && Math.hypot(p[0] - drag.start[0], p[1] - drag.start[1]) > 4) { drag.moved = true; hideTip(); canvas.style.cursor = "grabbing"; }
      if (drag.moved) {
        ox += p[0] - drag.last[0]; oy += p[1] - drag.last[1];
        if (preset) { preset = null; setPressed(); }
        redraw();
      }
      drag.last = p;
    }
  });
  const release = (ev) => {
    const wasClick = drag && !drag.moved && pointers.size === 1;
    pointers.delete(ev.pointerId);
    if (pointers.size === 0) {
      if (wasClick && ev.type === "pointerup") {
        const [sx, sy] = local(ev), d = dotAt(sx, sy);
        if (d) location.hash = "#/restaurant/" + encodeURIComponent(d.r.slug);
        else {
          const c = countryAt(sx, sy);
          if (c) zoomToCountry(c, (sx - ox) / k, (oy - sy) / k);
        }
      }
      drag = null; canvas.style.cursor = "grab";
    } else if (pointers.size === 1) {
      const p = [...pointers.values()][0];
      drag = { start: p, last: p, moved: true };
    }
  };
  canvas.addEventListener("pointerup", release);
  canvas.addEventListener("pointercancel", release);
  canvas.addEventListener("pointerleave", () => { if (!pointers.size) { hideTip(); if (hover || hoverCountry) { hover = hoverCountry = null; redraw(); } } });
  canvas.addEventListener("wheel", (ev) => {
    ev.preventDefault();
    const [sx, sy] = local(ev);
    zoomAt(sx, sy, Math.exp(-ev.deltaY * (ev.deltaMode === 1 ? 0.05 : 0.0015)));
    describe(ev, sx, sy);
  }, { passive: false });
  canvas.addEventListener("dblclick", (ev) => { ev.preventDefault(); zoomAt(...local(ev), 2); });
  canvas.addEventListener("keydown", (ev) => {
    const step = 60, moves = { ArrowLeft: [step, 0], ArrowRight: [-step, 0], ArrowUp: [0, step], ArrowDown: [0, -step] };
    if (ev.key === "+" || ev.key === "=") zoomAt(W / 2, H / 2, 1.5);
    else if (ev.key === "-" || ev.key === "_") zoomAt(W / 2, H / 2, 1 / 1.5);
    else if (ev.key === "0") goPreset("World");
    else if (moves[ev.key]) { ox += moves[ev.key][0]; oy += moves[ev.key][1]; preset = null; setPressed(); redraw(); }
    else return;
    ev.preventDefault();
  });

  // Zoom to the part of a country that was clicked (France, not France plus Guiana).
  function zoomToCountry(c, x, y) {
    const ring = c.rings.find((r) => pointInRings([r], x, y)) || c.rings[0];
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (let i = 0; i < ring.length; i += 2) {
      x0 = Math.min(x0, ring[i]); x1 = Math.max(x1, ring[i]); y0 = Math.min(y0, ring[i + 1]); y1 = Math.max(y1, ring[i + 1]);
    }
    const minSpan = 0.06; // don't zoom tighter than roughly a small country
    const cx = (x0 + x1) / 2, cy = (y0 + y1) / 2, hw = Math.max(minSpan, x1 - x0) / 2, hh = Math.max(minSpan, y1 - y0) / 2;
    const target = boxCamera(cx - hw, cy - hh, cx + hw, cy + hh, 32);
    target[0] = Math.min(target[0], minK * 60);
    preset = null; setPressed(); flyTo(target);
  }
  function goPreset(name) { preset = name; setPressed(); sync(); flyTo(boxCamera(...presetBox(name))); }

  const viewButtons = h("div", { class: "seg", role: "group", "aria-label": "Zoom to" },
    Object.keys(VIEWS).map((v) => h("button", { type: "button", "data-view": v, "aria-pressed": String(v === preset), onclick: () => goPreset(v) }, v)));
  function setPressed() { for (const b of viewButtons.children) b.setAttribute("aria-pressed", String(b.dataset.view === preset)); }
  const zoomButtons = h("div", { class: "zoom" },
    h("button", { type: "button", "aria-label": "Zoom in", onclick: () => zoomAt(W / 2, H / 2, 1.6) }, "+"),
    h("button", { type: "button", "aria-label": "Zoom out", onclick: () => zoomAt(W / 2, H / 2, 1 / 1.6) }, "−"),
    h("button", { type: "button", "aria-label": "Show the whole world", onclick: () => goPreset("World") }, "⟲"));
  wrap.append(zoomButtons);

  const legend = h("div", { class: "legend" }, [...STATUS].reverse().filter((st) => counts[st.key]).map((st) => h("button", {
    type: "button", class: "key", "aria-pressed": String(!hidden.has(st.key)), title: "Show or hide",
    onclick: (e) => { hidden.has(st.key) ? hidden.delete(st.key) : hidden.add(st.key); e.currentTarget.setAttribute("aria-pressed", String(!hidden.has(st.key))); offNote(); sync(); redraw(); },
  }, h("span", { class: "swatch" + (st.hollow ? " hollow" : ""), style: `--c:var(${st.color})` }), st.label, h("span", { class: "n" }, counts[st.key]))),
  h("span", { class: "shade" }, h("span", { class: "ramp" }), `Country shade: ${opts.shadeLabel || "restaurants in the list"} (1 to ${maxTotal})`));

  const unplaced = source.length - dots.length;
  const approx = dots.filter((d) => d.r.approx).length;
  const offHost = h("span", {});
  const offNote = () => {
    const off = dots.filter((d) => hidden.has(d.st)).length;
    offHost.textContent = off ? `${off} restaurant${off === 1 ? " is" : "s are"} hidden; click their keys above to show them. ` : "";
  };
  offNote();
  const notes = ["Scroll or pinch to zoom, drag to move. Click a country to zoom in, or a dot to open the restaurant.",
    approx ? `${approx} faded dots are placed at their city, not their address.` : null,
    unplaced ? `${unplaced} restaurants have no location yet and aren't on the map.` : null].filter(Boolean);

  if (window.ResizeObserver) new ResizeObserver(() => resize()).observe(wrap);
  else requestAnimationFrame(resize);
  window.matchMedia?.("(prefers-color-scheme: dark)").addEventListener?.("change", redraw);
  return h("section", { class: "card" }, h("div", { class: "maphead" }, viewButtons), wrap, legend,
    h("p", { class: "muted", style: "margin:.5rem 0 0;font-size:.85rem" }, offHost, notes.join(" ")));
}

function coverage(params) {
  const rows = D.coverage.map((c) => ({ ...c, rate: c.restaurants ? c.with_data / c.restaurants : 0 }));
  const accSel = accoladeSelect(params.get("acc") || "");
  const tilesHost = h("div", {}), mapHost = h("div", {});
  const render = () => {
    const f = accSel.value;
    const rs = D.restaurants.filter((r) => accoladeMatch(r, f));
    const withData = rs.filter((r) => r.menus).length;
    tilesHost.replaceChildren(tiles([[rs.length, f ? "restaurants in this group" : "restaurants in the list"],
      [rs.filter((r) => r.publishes_menu === "yes").length, "publish a menu"],
      [withData, "with menus collected"], [pct(rs.length ? withData / rs.length : 0), "covered"]]));
    const p = new URLSearchParams(location.hash.split("?")[1] || "");
    mapHost.replaceChildren(coverageMap(p, { only: f ? new Set(rs.map((r) => r.slug)) : null, extra: f ? { acc: f } : {},
      shadeLabel: f ? "restaurants in this group" : undefined }));
  };
  accSel.addEventListener("change", () => {
    const p = new URLSearchParams(location.hash.split("?")[1] || "");
    accSel.value ? p.set("acc", accSel.value) : p.delete("acc");
    setParams(p); render();
  });
  render();
  return h("div", {},
    h("h1", {}, "Map"),
    h("p", { class: "sub" }, "Where the menus come from. Read every chart on this site against this map: a region with two restaurants says little. Only restaurants with menus show at first; the legend under the map adds the rest of the list."),
    h("div", { class: "filters" }, h("label", {}, "Accolade", accSel)),
    tilesHost, mapHost,
    h("details", { class: "card" }, h("summary", {}, "Coverage by region, as a table"),
      table(rows, [
        { key: "region", label: "Region" },
        { key: "restaurants", label: "In list", num: true },
        { key: "publishes_menu", label: "Publish a menu", num: true },
        { key: "with_data", label: "With menus", num: true },
        { key: "rate", label: "Covered", num: true, render: (c) => pct(c.rate) },
      ], { sort: "region", dir: "asc" }, () => {})));
}

function about() {
  return h("div", { class: "prose" },
    h("h1", {}, "About"),
    h("p", { class: "lede" }, "I started The Menu Atlas because I love flavor combinations. Say I have lamb to cook and want to do something more creative with it: what are the best restaurants in the world putting it with? This site answers that, from the menus they publish."),
    h("h2", {}, "How it works"),
    h("ul", {},
      h("li", {}, "Menus come only from restaurants' own websites, read slowly by a crawler that names itself. Sites that reserve text and data mining rights are skipped, and bot protection is never bypassed."),
      h("li", {}, "A program breaks each dish into its ingredients and how they're prepared, so expect the odd mistake. Only that is shown, never the menu's own wording."),
      h("li", {}, `A pairing is two ingredients that share a dish more often than chance, at ${D.min_restaurants} or more restaurants. Each restaurant counts once, however many menus it publishes. Staples like bread and butter are hidden unless you ask for them.`)),
    h("h2", {}, "Restaurants"),
    h("p", {}, "Know a restaurant's website, or where its menu is? ", ext(REPO + "/issues/new?template=website.yml", "Suggest it ↗"),
      ". Run one of these restaurants and would rather not be here? ", ext(OPT_OUT, "Ask to be removed ↗"),
      " and we'll take out the restaurant and everything built from its menus. Spotted a mistake? ", ext(CORRECTION, "Report it ↗"),
      ". Each opens a public GitHub issue."),
    h("h2", {}, "Credits"),
    h("p", { class: "muted" }, "Restaurant lists from the MICHELIN Guide, The World's 50 Best Restaurants and The New York Times; this site isn't affiliated with any of them, and the MICHELIN star is Michelin's trademark. Country outlines from Natural Earth; ingredient articles from Wikipedia. ",
      ext(REPO, "The code is open source ↗"), ` · Data built ${new Date(D.generated_at).toLocaleString()} from “${D.source}”.`));
}

function notFound() {
  return h("div", {}, h("h1", {}, "Not found"), h("p", {}, h("a", { href: "#/" }, "Back to the start")));
}

// --- boot ---------------------------------------------------------------------

async function boot() {
  // An inlined bundle (window.MENU_ATLAS_DATA) skips the fetch, for single-file previews.
  D = window.MENU_ATLAS_DATA || null;
  if (!D) {
    try {
      const res = await fetch(DATA_URL, { cache: "no-cache" });
      if (!res.ok) throw new Error(res.status + " " + res.statusText);
      D = await res.json();
    } catch (err) {
      document.getElementById("app").replaceChildren(h("h1", {}, "No data yet"),
        h("p", {}, "Run ", h("code", {}, "uv run menudb export-site"), " to write data/data.json, then serve this folder."),
        h("p", { class: "muted" }, String(err)));
      return;
    }
  }
  expand(D);
  for (const i of D.ingredients) byName.set(i.name, i);
  for (const r of D.restaurants) byRestaurant.set(r.slug, r);
  if (D.source === "synthetic") {
    const b = document.getElementById("banner");
    b.textContent = "Preview built from invented test data. None of these restaurants or menus are real.";
    b.hidden = false;
  }
  document.getElementById("stamp").textContent = `Data built ${D.generated_at.slice(0, 10)}`;
  window.addEventListener("hashchange", route);
  route();
}

boot();
