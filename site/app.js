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
    D.groups.season.some((g) => g !== "unknown") ? h("optgroup", { label: "Season (hemisphere-adjusted)" }, D.groups.season.filter((g) => g !== "unknown").map((g) => opt("season:" + g, cap(g)))) : null];
}
const cap = (x) => x.charAt(0).toUpperCase() + x.slice(1);
// lowercase without accents, so "cepe" finds "cèpe"
const fold = (x) => (x || "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();

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

function ingredient(_params, [name]) {
  const ing = byName.get(name);
  if (!ing) return notFound();
  const total = D.totals.restaurants_with_data;
  // Positive associations only, most widely shared first: a 40× lift seen at two
  // restaurants is one chef's dish, not a pattern.
  const pairs = D.pairs.filter((p) => (p.a === name || p.b === name) && p.lift > 1)
    .map((p) => ({ ...p, other: p.a === name ? p.b : p.a }));
  const regions = D.groups.region.filter((g) => ing.by_region[g] != null).map((g) => [g, ing.by_region[g]]);
  const seasons = D.groups.season.filter((g) => ing.by_season[g] != null).map((g) => [cap(g), ing.by_season[g]]);
  const rs = ing.restaurants.map((slug) => byRestaurant.get(slug) || { slug, name: slug })
    .sort((a, b) => a.name.localeCompare(b.name));
  const LIST = 40;
  const children = D.ingredients.filter((i) => i.parent === name);

  return h("div", {},
    h("p", { class: "muted", style: "margin:0 0 .25rem" }, h("a", { href: "#/" }, "← Ingredients")),
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
      h("section", { class: "card" }, h("h2", {}, "Share of menus by season"),
        seasons.every(([g]) => g === "Unknown")
          ? h("p", { class: "empty" }, "No menu dates in this build yet, so no seasons.")
          : barChart(seasons.filter(([g]) => g !== "Unknown"), { max: 1, note: "Southern Hemisphere dates shift six months; tropical restaurants are their own group." }))),
    h("div", { class: "grid2", style: "margin-top:1.25rem" },
      h("section", { class: "card" }, h("h2", {}, "Pairs with"),
        pairs.length
          ? table(pairs, [
            { key: "other", label: "Ingredient", render: (p) => ingLink(p.other) },
            { key: "lift", label: "Lift", num: true, render: (p) => num(p.lift, 1) + "×" },
            { key: "restaurants", label: "Restaurants", num: true },
          ], { sort: "restaurants", dir: "desc", page: 15 }, () => {})
          : h("p", { class: "empty" }, `No pairing seen at ${D.min_restaurants}+ restaurants yet.`)),
      h("section", { class: "card stack" },
        h("div", {}, h("h2", {}, "Preparations"),
          ing.preparations.length
            ? h("div", { class: "chips" }, ing.preparations.map(([p, n]) => h("span", { class: "chip" }, p, h("span", { class: "n" }, n))))
            : h("p", { class: "empty" }, "None recorded.")),
        ing.aka?.length ? h("div", {}, h("h2", {}, "Also on menus as"),
          h("div", { class: "chips" }, ing.aka.map((a) => h("span", { class: "chip" }, a)))) : null,
        h("div", {}, h("h2", {}, "Restaurants"),
          h("div", { class: "chips" }, rs.slice(0, LIST).map((r) => h("a", { class: "chip", href: "#/restaurant/" + encodeURIComponent(r.slug) }, r.name)),
            rs.length > LIST ? h("span", { class: "muted" }, `and ${rs.length - LIST} more, all on the map below`) : null)))),
    h("h2", { style: "margin-top:1.5rem" }, "Where it's on the menu"),
    coverageMap(new URLSearchParams(), { only: new Set(ing.restaurants), sync: false, shadeLabel: "restaurants using it" }));
}

function pairings(params) {
  const out = h("div", {});
  const maxR = D.pairs.reduce((m, p) => Math.max(m, p.restaurants), D.min_restaurants);
  // With lots of data, start from pairs seen at 5+ restaurants so one-off dishes don't lead.
  const defaultMin = D.pairs.length > 2000 ? Math.min(5, maxR) : D.min_restaurants;
  const state = { sort: params.get("sort") || "lift", dir: params.get("dir") || "desc" };
  const search = h("input", { type: "search", placeholder: "filter by ingredient", value: params.get("q") || "", "aria-label": "Filter pairings by ingredient" });
  const steps = [...new Set([D.min_restaurants, 3, 5, 10, 20, 50, 100].filter((v) => v >= D.min_restaurants && v <= maxR))];
  const minSel = h("select", {}, steps.map((n, i) => {
    const v = String(n);
    return h("option", { value: v, selected: v === (params.get("min") || String(defaultMin)) }, v + "+");
  }));
  const chartHost = h("section", { class: "card" });
  const tableHost = h("div", {});

  const live = () => {
    const p = new URLSearchParams();
    const q = search.value.toLowerCase().trim(), min = Number(minSel.value);
    if (q) p.set("q", q); if (min !== defaultMin) p.set("min", min);
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
function scatter(all) {
  if (!all.length) return h("p", { class: "empty" }, "No pairings match.");
  // Thousands of SVG points get slow; plot the pairs seen at the most restaurants.
  const CAP = 600;
  const rows = all.length <= CAP ? all : [...all].sort((a, b) => b.restaurants - a.restaurants || b.lift - a.lift).slice(0, CAP);
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
  if (rows.length < all.length) {
    return h("div", {}, svg, h("p", { class: "muted", style: "font-size:.8rem;margin:.25rem 0 0" },
      `Showing the ${rows.length} pairs seen at the most restaurants, out of ${all.length}. Raise “Seen at” to narrow it.`));
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
  const hidden = new Set((params.get("hide") || "").split(",").filter(Boolean));
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
    const p = new URLSearchParams();
    if (preset && preset !== "World") p.set("view", preset);
    if (hidden.size) p.set("hide", [...hidden].join(","));
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
    onclick: (e) => { hidden.has(st.key) ? hidden.delete(st.key) : hidden.add(st.key); e.currentTarget.setAttribute("aria-pressed", String(!hidden.has(st.key))); sync(); redraw(); },
  }, h("span", { class: "swatch" + (st.hollow ? " hollow" : ""), style: `--c:var(${st.color})` }), st.label, h("span", { class: "n" }, counts[st.key]))),
  h("span", { class: "shade" }, h("span", { class: "ramp" }), `Country shade: ${opts.shadeLabel || "restaurants in the list"} (1 to ${maxTotal})`));

  const unplaced = source.length - dots.length;
  const approx = dots.filter((d) => d.r.approx).length;
  const notes = ["Scroll or pinch to zoom, drag to move. Click a country to zoom in, or a dot to open the restaurant.",
    approx ? `${approx} faded dots are placed at their city, not their address.` : null,
    unplaced ? `${unplaced} restaurants have no location yet and aren't on the map.` : null].filter(Boolean);

  if (window.ResizeObserver) new ResizeObserver(() => resize()).observe(wrap);
  else requestAnimationFrame(resize);
  window.matchMedia?.("(prefers-color-scheme: dark)").addEventListener?.("change", redraw);
  return h("section", { class: "card" }, h("div", { class: "maphead" }, viewButtons), wrap, legend,
    h("p", { class: "muted", style: "margin:.5rem 0 0;font-size:.85rem" }, notes.join(" ")));
}

function coverage(params) {
  const rows = D.coverage.map((c) => ({ ...c, rate: c.restaurants ? c.with_data / c.restaurants : 0 }));
  const t = D.totals;
  return h("div", {},
    h("h1", {}, "Coverage"),
    h("p", { class: "sub" }, "Where the menus come from. Read every chart on this site against this map: a region with two restaurants says little."),
    tiles([[t.restaurants, "restaurants in the list"], [D.restaurants.filter((r) => r.publishes_menu === "yes").length, "publish a menu"],
      [t.restaurants_with_data, "with menus collected"], [pct(t.restaurants ? t.restaurants_with_data / t.restaurants : 0), "covered"]]),
    coverageMap(params),
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
