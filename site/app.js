// Menu Atlas: a static page over data/data.json (written by `menudb export-site`).
// Everything (search, filters, charts) runs in the browser. Data strings are only
// ever inserted with textContent, never as HTML.
"use strict";

const DATA_URL = window.MENU_ATLAS_DATA_URL || "data/data.json";
let D = null; // the loaded bundle
const byName = new Map();
const byRestaurant = new Map();

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

const pct = (x) => (x == null ? "–" : (x * 100 >= 10 || x === 0 ? Math.round(x * 100) : (x * 100).toFixed(1)) + "%");
const num = (x, d = 2) => (x == null ? "–" : Number(x).toFixed(d));
const ingLink = (name) => h("a", { href: "#/ingredient/" + encodeURIComponent(name) }, name);
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

function route() {
  hideTip();
  const { parts, params } = parseHash();
  const view = parts[0] || "ingredients";
  for (const a of document.querySelectorAll("[data-nav]")) {
    const on = a.dataset.nav === view || (view === "ingredient" && a.dataset.nav === "ingredients")
      || (view === "restaurant" && a.dataset.nav === "restaurants");
    a.classList.toggle("on", on);
    if (on) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  }
  const app = document.getElementById("app");
  const views = { ingredients, ingredient, pairings, restaurants, restaurant, coverage, about };
  const fn = views[view] || notFound;
  app.replaceChildren(...[].concat(fn(params, parts.slice(1))));
  document.title = (fn === ingredients ? "" : (app.querySelector("h1")?.textContent + " · ")) + "Menu Atlas";
}

// --- shared pieces ------------------------------------------------------------

function tiles(items) {
  return h("div", { class: "tiles" }, items.map(([v, k]) => h("div", { class: "tile" }, h("div", { class: "v" }, v), h("div", { class: "k" }, k))));
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
    c.nosort ? c.label : h("button", { onclick: () => onSort(c.key, c.key === col.key && state.dir === "desc" ? "asc" : (c.key === col.key ? "desc" : (c.num ? "desc" : "asc"))) },
      c.label + (c.key === col.key ? (state.dir === "asc" ? " ↑" : " ↓") : "")))));
  const body = sorted.map((r) => h("tr", {}, cols.map((c) => h("td", { class: c.num ? "num" : null }, c.render ? c.render(r) : r[c.key]))));
  return h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, head), h("tbody", {}, body.length ? body : h("tr", {}, h("td", { colspan: cols.length, class: "empty" }, "Nothing matches.")))));
}

function groupOptions(selected) {
  const opt = (v, l) => h("option", { value: v, selected: v === selected }, l);
  return [opt("", "All restaurants"),
    h("optgroup", { label: "Region" }, D.groups.region.map((g) => opt("region:" + g, g))),
    D.groups.season.length ? h("optgroup", { label: "Season (hemisphere-adjusted)" }, D.groups.season.map((g) => opt("season:" + g, cap(g)))) : null];
}
const cap = (x) => x.charAt(0).toUpperCase() + x.slice(1);

function groupShare(ing, group) {
  if (!group) return ing.share;
  const [kind, g] = group.split(":");
  return (kind === "region" ? ing.by_region : ing.by_season)[g];
}

// --- views --------------------------------------------------------------------

function ingredients(params) {
  const state = { sort: params.get("sort") || "gshare", dir: params.get("dir") || "desc" };
  const t = D.totals;
  const resultsHost = h("div", {});
  const search = h("input", { type: "search", placeholder: "uni, beetroot, fermented…", value: params.get("q") || "",
    "aria-label": "Search ingredients or preparations" });
  const sel = h("select", {}, groupOptions(params.get("g") || ""));

  const live = () => {
    const q = search.value, group = sel.value;
    const p = new URLSearchParams();
    if (q) p.set("q", q); if (group) p.set("g", group);
    if (state.sort !== "gshare") p.set("sort", state.sort); if (state.dir !== "desc") p.set("dir", state.dir);
    setParams(p);
    const needle = q.toLowerCase().trim();
    const rows = D.ingredients
      .map((i) => ({ ...i, gshare: groupShare(i, group), n: i.restaurants.length, prep: i.preparations[0]?.[0] || "" }))
      .filter((i) => i.gshare != null && (!needle || i.name.includes(needle) || i.preparations.some(([pr]) => pr.includes(needle))));
    const max = Math.max(...rows.map((r) => r.gshare), 1e-9);
    resultsHost.replaceChildren(
      h("p", { class: "muted" }, `${rows.length} of ${D.ingredients.length} ingredients`),
      table(rows, [
        { key: "name", label: "Ingredient", render: (r) => ingLink(r.name) },
        { key: "gshare", label: group ? "Share in group" : "Share of menus", render: (r) => shareBar(r.gshare, max) },
        { key: "n", label: "Restaurants", num: true },
        { key: "prep", label: "Top preparation", render: (r) => r.prep || h("span", { class: "muted" }, "–") },
      ], state, (sort, dir) => { state.sort = sort; state.dir = dir; live(); }));
  };
  search.addEventListener("input", live);
  sel.addEventListener("change", live);
  live();
  return h("div", {},
    h("h1", {}, "Ingredients"),
    h("p", { class: "sub" }, "How often each ingredient appears on published tasting menus. Each restaurant counts once, however many menus it publishes."),
    tiles([[t.restaurants_with_data, "restaurants with menus"], [t.menus, "distinct menus"], [t.ingredients, "ingredients"], [t.pairs, "pairings"]]),
    h("div", { class: "filters" }, h("label", {}, "Search", search), h("label", {}, "Group", sel)),
    resultsHost);
}

function ingredient(_params, [name]) {
  const ing = byName.get(name);
  if (!ing) return notFound();
  const total = D.totals.restaurants_with_data;
  const pairs = D.pairs.filter((p) => p.a === name || p.b === name)
    .map((p) => ({ ...p, other: p.a === name ? p.b : p.a }))
    .sort((x, y) => y.lift - x.lift);
  const regions = D.groups.region.filter((g) => ing.by_region[g] != null).map((g) => [g, ing.by_region[g]]);
  const seasons = D.groups.season.filter((g) => ing.by_season[g] != null).map((g) => [cap(g), ing.by_season[g]]);
  const rs = ing.restaurants.map((slug) => byRestaurant.get(slug) || { slug, name: slug });

  return h("div", {},
    h("p", { class: "muted", style: "margin:0 0 .25rem" }, h("a", { href: "#/" }, "← Ingredients")),
    h("h1", {}, cap(name)),
    h("p", { class: "sub" }, `Used by ${ing.restaurants.length} of ${total} restaurants with published menus.`),
    tiles([[pct(ing.share), "share of menus"], [ing.restaurants.length, "restaurants"], [pairs.length, "pairings"],
      [ing.preparations[0]?.[0] || "–", "most common preparation"]]),
    h("div", { class: "grid2" },
      h("section", { class: "card" }, h("h2", {}, "Share of menus by region"), barChart(regions, { max: 1 })),
      h("section", { class: "card" }, h("h2", {}, "Share of menus by season"),
        barChart(seasons, { max: 1, note: "Southern Hemisphere dates shift six months; tropical restaurants are their own group." }))),
    h("div", { class: "grid2", style: "margin-top:1.25rem" },
      h("section", { class: "card" }, h("h2", {}, "Pairs with"),
        pairs.length
          ? table(pairs, [
            { key: "other", label: "Ingredient", render: (p) => ingLink(p.other) },
            { key: "lift", label: "Lift", num: true, render: (p) => num(p.lift, 1) + "×" },
            { key: "restaurants", label: "Restaurants", num: true },
          ], { sort: "lift", dir: "desc" }, () => {})
          : h("p", { class: "empty" }, `No pairing seen at ${D.min_restaurants}+ restaurants yet.`)),
      h("section", { class: "card stack" },
        h("div", {}, h("h2", {}, "Preparations"),
          ing.preparations.length
            ? h("div", { class: "chips" }, ing.preparations.map(([p, n]) => h("span", { class: "chip" }, p, h("span", { class: "n" }, n))))
            : h("p", { class: "empty" }, "None recorded.")),
        h("div", {}, h("h2", {}, "Restaurants"),
          h("div", { class: "chips" }, rs.map((r) => h("a", { class: "chip", href: "#/restaurant/" + encodeURIComponent(r.slug) }, r.name)))))));
}

function pairings(params) {
  const out = h("div", {});
  const maxR = Math.max(D.min_restaurants, ...D.pairs.map((p) => p.restaurants));
  const state = { sort: params.get("sort") || "lift", dir: params.get("dir") || "desc" };
  const search = h("input", { type: "search", placeholder: "filter by ingredient", value: params.get("q") || "", "aria-label": "Filter pairings by ingredient" });
  const minSel = h("select", {}, Array.from({ length: maxR - D.min_restaurants + 1 }, (_, i) => {
    const v = String(D.min_restaurants + i);
    return h("option", { value: v, selected: v === (params.get("min") || String(D.min_restaurants)) }, v + "+");
  }));
  const chartHost = h("section", { class: "card" });
  const tableHost = h("div", {});

  const live = () => {
    const p = new URLSearchParams();
    const q = search.value.toLowerCase().trim(), min = Number(minSel.value);
    if (q) p.set("q", q); if (min !== D.min_restaurants) p.set("min", min);
    if (state.sort !== "lift") p.set("sort", state.sort); if (state.dir !== "desc") p.set("dir", state.dir);
    setParams(p);
    const rows = D.pairs.filter((x) => x.restaurants >= min && (!q || x.a.includes(q) || x.b.includes(q)));
    chartHost.replaceChildren(h("h2", {}, "Lift against support"),
      h("p", { class: "muted", style: "margin-top:-.5rem;font-size:.85rem" }, "Up is a stronger-than-chance pairing; right is a more common one. Hover a point; click to open its first ingredient."),
      scatter(rows));
    tableHost.replaceChildren(h("p", { class: "muted" }, `${rows.length} of ${D.pairs.length} pairings`),
      table(rows, [
        { key: "a", label: "Pair", sort: (r) => r.a + " " + r.b, render: (r) => h("span", {}, ingLink(r.a), " + ", ingLink(r.b)) },
        { key: "lift", label: "Lift", num: true, render: (r) => num(r.lift, 1) + "×" },
        { key: "pmi", label: "PMI (bits)", num: true, render: (r) => num(r.pmi, 2) },
        { key: "support", label: "Support", num: true, render: (r) => pct(r.support) },
        { key: "restaurants", label: "Restaurants", num: true },
      ], state, (sort, dir) => { state.sort = sort; state.dir = dir; live(); }));
  };
  search.addEventListener("input", live);
  minSel.addEventListener("change", live);
  out.append(h("h1", {}, "Pairings"),
    h("p", { class: "sub" }, `Ingredients that share a course more often than chance, seen at ${D.min_restaurants} or more restaurants. Lift 2× means twice as often as if they were independent.`),
    h("div", { class: "filters" }, h("label", {}, "Ingredient", search), h("label", {}, "Seen at", minSel)),
    chartHost, tableHost);
  live();
  return out;
}

// Scatter: x = support (log scale), y = lift (log scale). One series.
function scatter(rows) {
  if (!rows.length) return h("p", { class: "empty" }, "No pairings match.");
  const W = 640, H = 310, m = { l: 44, r: 12, t: 10, b: 44 };
  const xs = rows.map((r) => r.support), ys = rows.map((r) => r.lift);
  const lx = (v) => Math.log10(v), pad = 0.08;
  let x0 = lx(Math.min(...xs)), x1 = lx(Math.max(...xs)), y0 = lx(Math.min(1, ...ys)), y1 = lx(Math.max(...ys));
  if (x1 - x0 < 0.1) { x0 -= 0.1; x1 += 0.1; } if (y1 - y0 < 0.1) { y1 += 0.1; }
  const dx = (x1 - x0) * pad, dy = (y1 - y0) * pad; x0 -= dx; x1 += dx; y1 += dy;
  const X = (v) => m.l + ((lx(v) - x0) / (x1 - x0)) * (W - m.l - m.r);
  const Y = (v) => H - m.b - ((lx(v) - y0) / (y1 - y0)) * (H - m.t - m.b);
  const ticks = (a, b) => { const out = []; for (let e = Math.floor(a); e <= Math.ceil(b); e++) for (const k of [1, 2, 5]) { const v = k * 10 ** e; if (lx(v) >= a && lx(v) <= b) out.push(v); } return out; };
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, width: "100%", role: "img", "aria-label": "Scatter of pairings: support against lift" });
  for (const v of ticks(y0, y1)) svg.append(s("line", { class: "grid", x1: m.l, x2: W - m.r, y1: Y(v), y2: Y(v) }), s("text", { x: m.l - 6, y: Y(v) + 4, "text-anchor": "end" }, v + "×"));
  for (const v of ticks(x0, x1)) svg.append(s("text", { x: X(v), y: H - m.b + 16, "text-anchor": "middle" }, pct(v)));
  svg.append(s("text", { x: W - m.r, y: H - 6, "text-anchor": "end" }, "support: share of courses (log scale)"));
  svg.append(s("text", { x: 4, y: m.t + 4 }, "lift"));
  for (const r of [...rows].sort((a, b) => a.restaurants - b.restaurants)) {
    const cx = X(r.support), cy = Y(r.lift);
    const dot = s("circle", { class: "dot", cx, cy, r: 4 + Math.min(4, r.restaurants - 1) });
    const hit = s("circle", { class: "hit", cx, cy, r: 12, tabindex: "0", role: "link", "aria-label": `${r.a} and ${r.b}` });
    const label = `${r.a} + ${r.b} · ${pct(r.support)} support · ${r.restaurants} restaurants`;
    hoverable(hit, num(r.lift, 1) + "× lift", label);
    hit.addEventListener("pointerenter", () => dot.classList.add("hl"));
    hit.addEventListener("pointerleave", () => dot.classList.remove("hl"));
    const go = () => { location.hash = "#/ingredient/" + encodeURIComponent(r.a); };
    hit.addEventListener("click", go);
    hit.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
    svg.append(dot, hit);
  }
  return svg;
}

function restaurants(params) {
  const out = h("div", {});
  const state = { sort: params.get("sort") || "name", dir: params.get("dir") || "asc" };
  const opt = (v, l, sel) => h("option", { value: v, selected: v === sel }, l);
  const regionSel = h("select", {}, opt("", "All regions", params.get("region") || ""), D.groups.region.map((g) => opt(g, g, params.get("region") || "")));
  const host = h("div", {});
  const live = () => {
    const p = new URLSearchParams();
    if (regionSel.value) p.set("region", regionSel.value);
    if (state.sort !== "name") p.set("sort", state.sort); if (state.dir !== "asc") p.set("dir", state.dir);
    setParams(p);
    const rows = D.restaurants.filter((r) => !regionSel.value || r.region === regionSel.value);
    host.replaceChildren(table(rows, [
      { key: "name", label: "Restaurant", render: (r) => h("a", { href: "#/restaurant/" + encodeURIComponent(r.slug) }, r.name) },
      { key: "city", label: "City", render: (r) => r.city || "–" },
      { key: "region", label: "Region" },
      { key: "menus", label: "Menus", num: true },
      { key: "ingredients", label: "Ingredients", num: true },
      { key: "url", label: "Source", nosort: true, render: (r) => r.url ? h("a", { href: r.url, rel: "noopener", target: "_blank" }, "website ↗") : h("span", { class: "muted" }, "–") },
    ], state, (sort, dir) => { state.sort = sort; state.dir = dir; live(); }));
  };
  regionSel.addEventListener("change", live);
  out.append(h("h1", {}, "Restaurants"),
    h("p", { class: "sub" }, "Every restaurant in the seed list, including those that don't publish a menu."),
    h("div", { class: "filters" }, h("label", {}, "Region", regionSel)), host);
  live();
  return out;
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
    h("p", { class: "sub" }, [r.city, r.country].filter(Boolean).join(", ") + (r.region ? ` · ${r.region}` : ""),
      r.url ? h("span", {}, " · ", h("a", { href: r.url, rel: "noopener", target: "_blank" }, "restaurant website ↗")) : null),
    tiles([[r.menus, "distinct menus"], [r.ingredients, "ingredients"], [r.publishes_menu === "yes" ? "Yes" : r.publishes_menu === "no" ? "No" : "?", "publishes a menu"]]),
    h("section", { class: "card" }, h("h2", {}, "Ingredients, most distinctive first"),
      used.length
        ? h("div", { class: "chips" }, distinct.map((i) => h("a", { class: "chip", href: "#/ingredient/" + encodeURIComponent(i.name) }, i.name,
          h("span", { class: "n", title: "restaurants using it" }, i.restaurants.length))))
        : h("p", { class: "empty" }, "No menu data for this restaurant.")),
    h("p", { class: "muted", style: "font-size:.85rem" }, "Only ingredient names are shown here. For the dishes themselves, see the restaurant's own site."));
}

function coverage() {
  const rows = D.coverage.map((c) => ({ ...c, rate: c.restaurants ? c.with_data / c.restaurants : 0 }));
  return h("div", {},
    h("h1", {}, "Coverage"),
    h("p", { class: "sub" }, "Which regions the data actually covers. Read every chart on this site against this table: a region with two restaurants says little."),
    h("section", { class: "card" }, h("h2", {}, "Share of seed restaurants with menu data"),
      barChart(rows.map((c) => [c.region, c.rate, `${c.with_data} of ${c.restaurants} restaurants`]), { max: 1 })),
    h("section", { class: "card" }, table(rows, [
      { key: "region", label: "Region" },
      { key: "restaurants", label: "In seed list", num: true },
      { key: "publishes_menu", label: "Publish a menu", num: true },
      { key: "with_data", label: "With data", num: true },
      { key: "rate", label: "Covered", num: true, render: (c) => pct(c.rate) },
    ], { sort: "region", dir: "asc" }, () => {})));
}

function about() {
  return h("div", { class: "prose" },
    h("h1", {}, "About"),
    h("p", {}, "Menu Atlas reads the tasting menus fine-dining restaurants publish on their own websites and breaks each dish into ingredients and preparations. It shows which ingredients are common, how they vary by region and by season, and which ones chefs put together more often than chance."),
    h("h2", {}, "Method"),
    h("ul", {},
      h("li", {}, "Each distinct menu counts once. Seeing the same menu again only extends its date range."),
      h("li", {}, "Every restaurant carries the same weight, split across its menus, so a restaurant that publishes five menus doesn't count five times."),
      h("li", {}, "Seasons are hemisphere-adjusted: October in Melbourne compares with April in Paris. Restaurants inside the tropics are grouped separately."),
      h("li", {}, `Pairings are scored per course by lift (how many times more often than chance) and PMI, and only shown when ${D.min_restaurants} or more restaurants use them.`)),
    h("h2", {}, "What's shown and what isn't"),
    h("ul", {},
      h("li", {}, "Only derived facts: ingredient names, preparations, counts and scores, plus a link to each restaurant's site."),
      h("li", {}, "No menu text, dish descriptions, PDFs or photos."),
      h("li", {}, "Sites that opt out through robots.txt or a TDM reservation are skipped.")),
    h("p", { class: "muted" }, `Data built ${new Date(D.generated_at).toLocaleString()} from source “${D.source}”.`));
}

function notFound() {
  return h("div", {}, h("h1", {}, "Not found"), h("p", {}, h("a", { href: "#/" }, "Back to ingredients")));
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
