import contextlib
import logging
import os
import uuid
from pathlib import Path

import httpx
import typer
from sqlalchemy import select, text

from menudb.config import get_settings

app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.callback()
def main(verbose: bool = typer.Option(False, "--verbose", "-v")):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx logs full URLs at INFO; keep it quiet
    logging.getLogger("httpx").setLevel(logging.WARNING)


@app.command()
def seed(
    csv_path: Path = typer.Argument(Path("data/seed/restaurants.csv")),
    recipes_dir: Path = typer.Option(Path("data/recipes"), help="one <slug>.yaml recipe per restaurant"),
    auto_dir: Path = typer.Option(Path("data/recipes_auto"), help="generated recipes, used where no hand recipe exists"),
    full_csv: Path = typer.Option(Path("data/seed/restaurants_full.csv"),
                                  help="full list: restaurants only, seeded first so the curated CSV wins"),
):
    """Upsert restaurants, list memberships and recipes.

    CSV columns: slug,name,city,country,lat,lon,website,publishes_menu,lists
    where lists looks like "michelin:2026:3;w50b:2026:12".
    """
    from menudb.db import get_session
    from menudb.seed import seed as seed_db

    with get_session() as session:
        full = seed_db(session, full_csv, None) if full_csv.exists() else {"restaurants": 0}
        counts = seed_db(session, csv_path, recipes_dir, auto_dir)
        session.commit()
    typer.echo(f"seeded {full['restaurants']} full-list and {counts['restaurants']} curated "
               f"restaurants, {counts['sources']} sources")


@app.command()
def check(limit: int = typer.Option(None, help="max sources to check this run")):
    """Check due sources for new menus (tier 0/1). Prints counts only."""
    from menudb.db import get_session
    from menudb.db.models import Source
    from menudb.fetch.http import Fetcher
    from menudb.pipeline import due_sources, run_check

    s = get_settings()
    with get_session() as session, Fetcher(s.user_agent) as fetcher:
        from datetime import datetime, timezone

        due = due_sources(session, datetime.now(timezone.utc), limit)
        needs_js = any(src.recipe.get("render") == "js" for src in due)
        if needs_js:
            from menudb.fetch.browser import BrowserRenderer

            with BrowserRenderer(s.user_agent, s.chromium_path or None) as renderer:
                counts = run_check(session, fetcher, renderer, limit)
        else:
            counts = run_check(session, fetcher, None, limit)
        failing = session.scalars(
            select(Source).where(Source.consecutive_failures >= 2)).all()

    lines = [f"{k}: {counts.get(k, 0)}" for k in ("changed", "unchanged", "failed", "opted_out")]
    lines.append(f"sources failing 2+ runs: {[src.id for src in failing]}")
    summary = "\n".join(lines)
    typer.echo(summary)
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a") as f:
            f.write("### menudb check\n```\n" + summary + "\n```\n")


@app.command()
def probe(
    slugs: list[str] = typer.Argument(None, help="restaurant slugs; default: every recipe"),
    recipes_dir: Path = typer.Option(Path("data/recipes")),
    to_bucket: bool = typer.Option(False, help="store raw documents in the snapshots bucket"),
):
    """Walk recipes without a database, e.g. to test a recipe or from a cloud session.

    Prints kind, size and hash per document, never menu text. With --to-bucket the
    raw bytes go to the private bucket under raw/<sha256>, the same keys `check` uses,
    so a later `check` finds them already stored.
    """
    from menudb import storage
    from menudb.extract.text import document_kind, document_text
    from menudb.fetch.http import Fetcher, RobotsDisallowed
    from menudb.normalize import content_hash
    from menudb.pipeline import _MIME, fetch_documents, html_getter
    from menudb.recipes import load_recipes

    s = get_settings()
    files = sorted(recipes_dir.glob("*.yaml"))
    if slugs:
        files = [f for f in files if f.stem in slugs]
    sources = [(f.stem, label, r) for f in files for label, r in load_recipes(f).items()]
    counts = {"ok": 0, "failed": 0, "opted_out": 0}

    renderer = None
    with Fetcher(s.user_agent) as fetcher, contextlib.ExitStack() as stack:
        if any(r.render == "js" for *_, r in sources):
            from menudb.fetch.browser import BrowserRenderer

            renderer = stack.enter_context(BrowserRenderer(s.user_agent, s.chromium_path or None))
        for slug, label, recipe in sources:
            name = f"{slug}/{label}"
            try:
                docs = list(fetch_documents(recipe, fetcher, html_getter(recipe, fetcher, renderer), {}))
            except RobotsDisallowed:
                counts["failed"] += 1
                typer.echo(f"{name}: failed robots.txt disallows")
                continue
            except Exception as e:
                counts["failed"] += 1
                typer.echo(f"{name}: failed {type(e).__name__}: {e}"[:300])
                continue
            if any(d.tdm_reserved for d in docs):
                counts["opted_out"] += 1
                typer.echo(f"{name}: opted out (tdm-reservation)")
                continue
            counts["ok"] += 1
            typer.echo(f"{name}: ok, {len(docs)} document(s)")
            for d in docs:
                kind = document_kind(d.content_type, d.url, d.data)
                text = document_text(kind, d.data)
                h = content_hash(d.data)
                stored = ""
                if to_bucket:
                    storage.put_raw(h, d.data, _MIME[kind])
                    stored = ", stored"
                chars = f"{len(text)} text chars" if text else "no text layer"
                typer.echo(f"  {kind} {len(d.data)} bytes, {chars}, sha256 {h[:12]}{stored}")
    typer.echo(" ".join(f"{k}: {v}" for k, v in counts.items()))


@app.command()
def discover(
    csv_path: Path = typer.Argument(Path("data/seed/restaurants_full.csv")),
    slugs: list[str] = typer.Option(None, "--slug", help="only these restaurants"),
    out_dir: Path = typer.Option(Path("data/recipes_auto"), help="where generated recipes go"),
    hand_dir: Path = typer.Option(Path("data/recipes"), help="restaurants with a hand recipe are skipped"),
    status_path: Path = typer.Option(Path("data/seed/discovery.csv")),
    workers: int = typer.Option(8, help="sites checked in parallel (one host per worker)"),
    js: bool = typer.Option(False, help="retry sites that need JavaScript in headless Chromium"),
    redo: bool = typer.Option(False, help="re-check restaurants that already have a status"),
    limit: int = typer.Option(None, help="max restaurants this run"),
    to_bucket: bool = typer.Option(False, help="store found menus in the bucket and queue them in pending/"),
):
    """Find menus on restaurant websites with generic heuristics and write recipes for them.

    No database. Prints and records slug, status, food-word score and URL, never menu text.
    Results are merged into the status CSV, so an interrupted run resumes where it stopped.
    """
    import csv
    import datetime as dt
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from urllib.parse import urlsplit

    import yaml

    from menudb import handoff, storage
    from menudb.discover import Discovery, discover as find_menu
    from menudb.fetch.http import Fetcher
    from menudb.normalize import content_hash
    from menudb.extract.text import document_kind
    from menudb.pipeline import _MIME, fetch_documents, html_getter
    from menudb.recipes import Recipe

    s = get_settings()
    rows = [r for r in csv.DictReader(csv_path.open()) if r.get("website")]
    if slugs:
        rows = [r for r in rows if r["slug"] in slugs]
    fields = ["slug", "status", "score", "kind", "menu_url", "stored", "note", "checked"]
    done: dict[str, dict] = {}
    if status_path.exists():
        done = {r["slug"]: r for r in csv.DictReader(status_path.open())}
    todo = []
    for r in rows:
        if (hand_dir / f"{r['slug']}.yaml").exists():
            done[r["slug"]] = {"slug": r["slug"], "status": "hand_recipe"}
        elif redo or r["slug"] not in done or (js and done[r["slug"]].get("status") == "needs_js"):
            todo.append(r)
    todo = todo[:limit] if limit else todo
    out_dir.mkdir(parents=True, exist_ok=True)

    def save_status():
        with status_path.open("w", newline="") as f:
            w = csv.DictWriter(f, fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(sorted(done.values(), key=lambda r: r["slug"]))

    def store(slug: str, recipe: Recipe, fetcher: Fetcher, renderer=None) -> int:
        docs = list(fetch_documents(recipe, fetcher, html_getter(recipe, fetcher, renderer), {}))
        if any(d.tdm_reserved for d in docs):
            return -1
        items = []
        for d in docs:
            kind = document_kind(d.content_type, d.url, d.data)
            storage.put_raw(content_hash(d.data), d.data, _MIME[kind])
            items.append(handoff.item_from_bytes(slug, "main", d.url, d.data, d.content_type))
        handoff.queue_items(items)
        return len(docs)

    def record(slug: str, d: Discovery, stored: int | str = ""):
        if d.recipe:
            (out_dir / f"{slug}.yaml").write_text(yaml.safe_dump(d.recipe, sort_keys=False, allow_unicode=True))
        else:
            (out_dir / f"{slug}.yaml").unlink(missing_ok=True)
        if stored == -1:
            d.status, d.note, stored = "opted_out", "tdm-reservation", ""
        done[slug] = {"slug": slug, "status": d.status, "score": d.score, "kind": d.kind,
                      "menu_url": d.menu_url, "stored": stored, "note": " ".join(d.note.split())[:200],
                      "checked": dt.date.today().isoformat()}
        typer.echo(f"{slug}: {d.status} score={d.score} {d.kind} stored={stored}")

    def run_host(group: list[dict]) -> list[tuple[str, Discovery, int | str]]:
        out = []
        with Fetcher(s.user_agent) as fetcher:
            for r in group:
                try:
                    d = find_menu(r["website"], fetcher)
                except Exception as e:  # one odd site must not stop the batch
                    d = Discovery("failed", note=f"{type(e).__name__}: {e}"[:200])
                stored: int | str = ""
                if to_bucket and d.recipe and d.status.startswith("menu_"):
                    try:
                        stored = store(r["slug"], Recipe.model_validate(d.recipe), fetcher)
                    except Exception as e:
                        d.note = f"store failed: {type(e).__name__}"
                out.append((r["slug"], d, stored))
        return out

    static = [r for r in todo if not (js and done.get(r["slug"], {}).get("status") == "needs_js")]
    retry_js = [r for r in todo if r not in static]
    by_host: dict[str, list[dict]] = {}
    for r in static:
        by_host.setdefault(urlsplit(r["website"]).netloc.lower().removeprefix("www."), []).append(r)
    with ThreadPoolExecutor(workers) as ex:
        futures = [ex.submit(run_host, g) for g in by_host.values()]
        for n, fut in enumerate(as_completed(futures), 1):
            for slug, d, stored in fut.result():
                record(slug, d, stored)
            if n % 20 == 0:
                save_status()
    save_status()

    if js:
        from menudb.fetch.browser import BrowserRenderer

        retry_js += [r for r in static if done[r["slug"]]["status"] == "needs_js"]
        with Fetcher(s.user_agent) as fetcher, BrowserRenderer(s.user_agent, s.chromium_path or None) as renderer:
            def rendered(url: str) -> tuple[str, str]:
                if not fetcher.allowed(url):
                    from menudb.fetch.http import RobotsDisallowed
                    raise RobotsDisallowed(url)
                return renderer.render(url)

            for r in retry_js:
                try:
                    d = find_menu(r["website"], fetcher, rendered)
                except Exception as e:
                    d = Discovery("failed", note=f"{type(e).__name__}: {e}"[:200])
                if d.recipe:
                    d.recipe["render"] = "js"
                if d.status == "needs_js":
                    d.status = "no_menu_found"
                stored: int | str = ""
                if to_bucket and d.recipe and d.status.startswith("menu_"):
                    try:
                        stored = store(r["slug"], Recipe.model_validate(d.recipe), fetcher, renderer)
                    except Exception as e:
                        d.note = f"store failed: {type(e).__name__}"
                record(r["slug"], d, stored)
        save_status()

    from collections import Counter

    counts = Counter(v["status"] for v in done.values())
    typer.echo(" ".join(f"{k}: {v}" for k, v in sorted(counts.items())))


@app.command()
def backfill(
    slugs: list[str] = typer.Argument(None, help="restaurant slugs; default: every source"),
    since: str = typer.Option(None, help="earliest capture, YYYY or YYYYMMDD"),
    max_captures: int = typer.Option(24, help="most recent N monthly captures per source"),
):
    """Record menu history from the Wayback Machine (origin=wayback snapshots).

    Needs Postgres, and web.archive.org isn't reachable from cloud sessions, so this
    runs in Actions (backfill.yml) or locally. Prints counts only.
    """
    from menudb.db import get_session
    from menudb.db.models import Restaurant, Source
    from menudb.fetch.http import Fetcher
    from menudb.wayback import WAYBACK_INTERVAL, WaybackUnavailable, backfill_source

    s = get_settings()
    totals: dict[str, int] = {}
    with get_session() as session, Fetcher(s.user_agent) as live, \
            Fetcher(s.user_agent, min_interval=WAYBACK_INTERVAL) as wb:
        q = select(Source).join(Restaurant).order_by(Source.id)
        if slugs:
            q = q.where(Restaurant.slug.in_(slugs))
        for source in session.scalars(q).all():
            try:
                counts = backfill_source(session, source, wb, live, since=since,
                                         max_captures=max_captures)
            except WaybackUnavailable as e:
                # Menus recorded so far are committed; rerun later to continue.
                typer.echo(f"web.archive.org refused requests ({e}) at source {source.id}; stopping")
                totals["stopped_at_source"] = source.id
                break
            for k, v in counts.items():
                totals[k] = totals.get(k, 0) + v
    _summary("menudb backfill", totals)


@app.command()
def smoke():
    """End-to-end check of each Neon service: Postgres, Object Storage, Auth."""
    from menudb import storage
    from menudb.db import get_session
    from menudb.db.models import Restaurant

    s = get_settings()
    ok = True

    # Postgres (pooled URL): write a row inside a transaction, read it back, roll back.
    try:
        with get_session() as session:
            version = session.execute(text("show server_version")).scalar()
            slug = f"smoke-{uuid.uuid4().hex[:8]}"
            session.add(Restaurant(slug=slug, name="Smoke Test"))
            session.flush()
            found = session.scalar(select(Restaurant.name).where(Restaurant.slug == slug))
            session.rollback()
        assert found == "Smoke Test"
        typer.echo(f"postgres: ok (server {version}, insert/select/rollback on restaurant)")
    except Exception as e:
        ok = False
        typer.echo(f"postgres: FAILED {type(e).__name__}: {e}")

    # Object Storage: put, head, get, delete in the snapshots bucket.
    try:
        payload = uuid.uuid4().bytes
        key = f"smoke/{uuid.uuid4().hex}"
        storage._client().put_object(Bucket=s.snapshots_bucket, Key=key, Body=payload)
        assert storage.exists(key)
        assert storage.get_raw(key) == payload
        storage.delete(key)
        assert not storage.exists(key)
        typer.echo(f"object storage: ok (put/head/get/delete in bucket '{s.snapshots_bucket}')")
    except Exception as e:
        ok = False
        typer.echo(f"object storage: FAILED {type(e).__name__}: {e}")

    # Neon Auth: the service is up and publishes signing keys. Nothing logs in yet;
    # login belongs to the future web app.
    try:
        base = s.neon_auth_base_url.rstrip("/")
        health = httpx.get(f"{base}/ok", timeout=15)
        jwks = httpx.get(s.neon_auth_jwks_url, timeout=15)
        keys = jwks.json().get("keys", [])
        assert health.status_code == 200 and jwks.status_code == 200
        typer.echo(f"neon auth: ok (/ok {health.status_code}, /jwks {len(keys)} key(s))")
    except Exception as e:
        ok = False
        typer.echo(f"neon auth: FAILED {type(e).__name__}: {e}")

    raise typer.Exit(0 if ok else 1)


def _summary(title: str, counts: dict) -> None:
    line = " ".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "nothing to do"
    typer.echo(line)
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a") as f:
            f.write(f"### {title}\n```\n{line}\n```\n")


@app.command()
def pending(
    to_bucket: bool = typer.Option(False, help="write pending/ items to the snapshots bucket"),
    from_recipes: bool = typer.Option(
        False, help="no database: walk the recipes and queue what they fetch (cloud sessions)"),
    slugs: list[str] = typer.Argument(None, help="with --from-recipes: restaurant slugs"),
    recipes_dir: Path = typer.Option(Path("data/recipes")),
    limit: int = typer.Option(None, help="max items to queue"),
    shard: str = typer.Option(None, help="with --from-recipes: 'i/n' walks only shard i of n (stable by slug)"),
):
    """Queue unextracted menus in the bucket's pending/ for a session to extract.

    Default: menus in Postgres with no extraction yet (runs in Actions). With
    --from-recipes, walks recipes instead and needs no database; import then waits
    until `check` has recorded the same documents.
    """
    from menudb import handoff

    if not to_bucket:
        typer.echo("pass --to-bucket to write pending/ items")
        raise typer.Exit(2)
    if not from_recipes:
        from menudb.db import get_session

        with get_session() as session:
            counts = handoff.pending_from_db(session, limit)
        _summary("menudb pending", counts)
        return

    from menudb.fetch.http import Fetcher
    from menudb.pipeline import fetch_documents, html_getter
    from menudb.recipes import load_recipes

    s = get_settings()
    files = sorted(recipes_dir.glob("*.yaml"))
    if slugs:
        files = [f for f in files if f.stem in slugs]
    if shard:
        import zlib

        i, n = (int(x) for x in shard.split("/"))
        files = [f for f in files if zlib.crc32(f.stem.encode()) % n == i]
    sources = [(f.stem, label, r) for f in files for label, r in load_recipes(f).items()]
    items, failed = [], 0
    with Fetcher(s.user_agent) as fetcher, contextlib.ExitStack() as stack:
        renderer = None
        if any(r.render == "js" for *_, r in sources):
            from menudb.fetch.browser import BrowserRenderer

            renderer = stack.enter_context(BrowserRenderer(s.user_agent, s.chromium_path or None))
        for slug, label, recipe in sources:
            try:
                docs = list(fetch_documents(recipe, fetcher, html_getter(recipe, fetcher, renderer), {}))
            except Exception as e:
                failed += 1
                typer.echo(f"{slug}/{label}: failed {type(e).__name__}")
                continue
            if any(d.tdm_reserved for d in docs):
                typer.echo(f"{slug}/{label}: opted out (tdm-reservation)")
                continue
            for d in docs:
                items.append(handoff.item_from_bytes(slug, label, d.url, d.data, d.content_type))
    if limit is not None:
        items = items[:limit]
    counts = handoff.queue_items(items)
    counts["failed_sources"] = failed
    _summary("menudb pending", counts)


@app.command("import")
def import_(
    files: list[Path] = typer.Argument(None, help="local extracted JSON files"),
    from_bucket: bool = typer.Option(False, help="import every object in the bucket's extracted/"),
):
    """Load extractions into Postgres (courses, components, ingredient matches)."""
    from collections import Counter

    from menudb import handoff
    from menudb.db import get_session
    from menudb.extract.schema import ExtractedMenu

    if not files and not from_bucket:
        typer.echo("pass JSON files or --from-bucket")
        raise typer.Exit(2)
    with get_session() as session:
        if from_bucket:
            counts = handoff.import_from_bucket(session)
        else:
            counts = Counter()
            for f in files:
                ex = ExtractedMenu.model_validate_json(f.read_text())
                counts[handoff.import_menu(session, ex)] += 1
                session.commit()
    _summary("menudb import", counts)


extract_io = typer.Typer(no_args_is_help=True, help=(
    "Bucket reads and writes for extraction in a session. Needs no database."))
app.add_typer(extract_io, name="extract-io")


@extract_io.command("list")
def extract_io_list():
    """List pending/ items: key, kind and text size (never the text)."""
    from menudb import handoff

    keys = sorted(handoff.storage.list_keys(handoff.PENDING))
    for k in keys:
        item = handoff.PendingItem.model_validate(handoff.get_json(k))
        size = f"{len(item.text)} chars" if item.text else "no text layer"
        typer.echo(f"{k}  {item.kind}  {size}")
    typer.echo(f"{len(keys)} pending")


@extract_io.command("get")
def extract_io_get(
    key: str,
    out: Path = typer.Option(..., help="local directory outside the repo, e.g. a scratchpad"),
):
    """Download one pending item (and its raw file when it has no text layer)."""
    from menudb import handoff

    item = handoff.PendingItem.model_validate(handoff.get_json(key))
    out.mkdir(parents=True, exist_ok=True)
    stem = f"{item.slug}--{item.normalized_hash[:16]}"
    (out / f"{stem}.pending.json").write_text(item.model_dump_json(indent=2))
    typer.echo(str(out / f"{stem}.pending.json"))
    if not item.text:
        ext = {"pdf": ".pdf", "image": ".img", "html": ".html"}[item.kind]
        (out / f"{stem}{ext}").write_bytes(handoff.storage.get_raw(item.storage_key))
        typer.echo(str(out / f"{stem}{ext}"))


@extract_io.command("put")
def extract_io_put(
    files: list[Path],
    keep_pending: bool = typer.Option(False, help="leave the pending/ item where it is"),
):
    """Validate extraction JSON, upload to extracted/, and move the pending/ item to processed/.

    import needs the processed/ item to record menus that no `check` run has seen."""
    from menudb import handoff
    from menudb.extract.schema import ExtractedMenu

    for f in files:
        ex = ExtractedMenu.model_validate_json(f.read_text())
        handoff.put_json(handoff.key(handoff.EXTRACTED, ex.slug, ex.normalized_hash), ex)
        pk = handoff.key(handoff.PENDING, ex.slug, ex.normalized_hash)
        if not keep_pending and handoff.storage.exists(pk):
            handoff.storage.move(pk, handoff.PROCESSED + pk.removeprefix(handoff.PENDING))
        n = sum(len(c.components) for c in ex.courses)
        typer.echo(f"{ex.slug}/{ex.normalized_hash[:12]}: {len(ex.courses)} courses, {n} components")


@extract_io.command("schema")
def extract_io_schema():
    """Print the JSON schema an extraction must match."""
    import json

    from menudb.extract.schema import ExtractedMenu

    typer.echo(json.dumps(ExtractedMenu.model_json_schema(), indent=2))


@app.command("extract-auto")
def extract_auto(
    slugs: list[str] = typer.Argument(None, help="only these restaurants"),
    min_stars: int = typer.Option(0, help="only restaurants with at least this many Michelin stars (latest year)"),
    rerun: bool = typer.Option(False, help="also re-extract processed/ items done by an older lexicon run"),
    limit: int = typer.Option(None, help="stop after this many documents"),
    dry_run: bool = typer.Option(False, help="extract and count, but write nothing"),
    full_csv: Path = typer.Option(Path("data/seed/restaurants_full.csv")),
):
    """Extract pending/ menus with the rule-based lexicon extractor (no LLM, bucket only).

    Writes extracted/ for `import --from-bucket`; prints counts only, never menu text.
    """
    import csv
    import re

    from menudb import handoff

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    rows = list(csv.DictReader(full_csv.open())) if full_csv.exists() else []
    countries = {r["slug"]: r["country"] for r in rows}
    wanted = set(slugs) if slugs else None
    if min_stars:
        def stars(lists: str) -> int:
            found = [(int(y), int(n)) for y, n in re.findall(r"michelin:(\d{4}):(\d)", lists)]
            latest = max((y for y, _ in found), default=None)
            return max((n for y, n in found if y == latest), default=0)
        starred = {r["slug"] for r in rows if stars(r.get("lists", "")) >= min_stars}
        wanted = starred if wanted is None else wanted & starred
    counts = handoff.extract_auto(wanted, countries, rerun=rerun, limit=limit, dry_run=dry_run)
    typer.echo(", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "nothing to do")


review_app = typer.Typer(no_args_is_help=True, help=(
    "Audit and fix ingredient matches. export/apply need Postgres (Actions); "
    "pull/push only need the bucket (sessions). See docs/EXTRACTION.md."))
app.add_typer(review_app, name="review")


@review_app.command("export")
def review_export(to_bucket: bool = typer.Option(False, help="write review/queue.json to the bucket")):
    """Export the review queue with each term's current match."""
    import json

    from menudb import review
    from menudb.db import get_session

    with get_session() as session:
        if to_bucket:
            _summary("menudb review export", review.export_to_bucket(session))
        else:
            typer.echo(json.dumps(review.export_queue(session), ensure_ascii=False, indent=1))


@review_app.command("apply")
def review_apply(
    files: list[Path] = typer.Argument(None, help="local decision files (JSON lists)"),
    from_bucket: bool = typer.Option(False, help="apply review/decisions/ from the bucket"),
):
    """Apply map / accept / ignore decisions to aliases, ingredients and components."""
    from collections import Counter

    from menudb import review
    from menudb.db import get_session

    if not files and not from_bucket:
        typer.echo("pass decision files or --from-bucket")
        raise typer.Exit(2)
    with get_session() as session:
        if from_bucket:
            counts = review.apply_from_bucket(session)
        else:
            counts = Counter()
            for f in files:
                counts.update(review.apply_all(session, review.Decisions.validate_json(f.read_text())))
            session.commit()
    _summary("menudb review apply", counts)


@review_app.command("pull")
def review_pull(out: Path = typer.Option(..., help="local directory outside the repo")):
    """Download review/queue.json. No database needed."""
    from menudb import review, storage

    out.mkdir(parents=True, exist_ok=True)
    (out / "queue.json").write_bytes(storage.get_raw(review.QUEUE_KEY))
    typer.echo(str(out / "queue.json"))


@review_app.command("push")
def review_push(files: list[Path]):
    """Validate decision files and upload them to review/decisions/. No database needed."""
    from datetime import datetime, timezone

    from menudb import review, storage

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    for f in files:
        decisions = review.Decisions.validate_json(f.read_text())
        key = f"{review.DECISIONS}{stamp}-{f.stem}.json"
        storage.put_bytes(key, review.Decisions.dump_json(decisions), "application/json")
        typer.echo(f"{key}: {len(decisions)} decisions")


SITE_KEY = "site/data.json"  # private: real-data builds stay in the bucket until publishing is decided


@app.command("export-site")
def export_site(
    source: str = typer.Option("synthetic", help="synthetic | bucket | db"),
    out: Path = typer.Option(Path("site/data"), help="directory for data.json (gitignored)"),
    min_restaurants: int = typer.Option(2, help="drop pairs seen at fewer restaurants"),
    to_bucket: bool = typer.Option(False, help=f"also store it privately in the bucket as {SITE_KEY}"),
):
    """Write aggregated JSON for the static site: no course text, derived data only."""
    from menudb import site

    root = Path(__file__).resolve().parents[2]
    rows, restaurants, details, preps = site.load(source, root)
    from menudb import wiki

    data = site.build(rows, restaurants, source=source, details=details, preparations=preps,
                      min_restaurants=min_restaurants, wikipedia=wiki.urls(),
                      lexicon=site.lexicon_info(root / "data/lexicon/ingredients.txt"))
    path = site.write(data, out)
    if to_bucket:
        from menudb import storage

        storage.put_bytes(SITE_KEY, path.read_bytes(), "application/json")
    _summary("menudb export-site", {"source": source, **data["totals"], "bytes": path.stat().st_size})
    if source != "synthetic":
        typer.echo("real data: keep it out of the repo and don't publish it (docs/PLAN.md, Website)")


@app.command("wiki-links")
def wiki_links(
    source: str = typer.Option("bucket", help="where ingredient names come from: synthetic | bucket | db"),
    names_file: list[Path] = typer.Option(None, help="extra names, one per line (e.g. a lexicon)"),
    retry: bool = typer.Option(False, help="also redo check, ambiguous and missing rows"),
):
    """Look up Wikipedia articles for ingredients into data/ingredients/wikipedia.csv."""
    from menudb import site, wiki

    root = Path(__file__).resolve().parents[2]
    rows, *_ = site.load(source, root)
    names = {r.ingredient for r in rows}
    for f in names_file or []:
        names |= {line.strip().lower() for line in f.read_text().splitlines()
                  if line.strip() and not line.startswith("#")}
    client = wiki.Client(get_settings().user_agent)
    _summary("menudb wiki-links", {"names": len(names), **wiki.update(names, client, retry=retry)})
