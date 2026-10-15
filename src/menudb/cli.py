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
):
    """Upsert restaurants, list memberships and recipes.

    CSV columns: slug,name,city,country,lat,lon,website,publishes_menu,lists
    where lists looks like "michelin:2026:3;w50b:2026:12".
    """
    from menudb.db import get_session
    from menudb.seed import seed as seed_db

    with get_session() as session:
        counts = seed_db(session, csv_path, recipes_dir)
        session.commit()
    typer.echo(f"seeded {counts['restaurants']} restaurants, {counts['sources']} sources")


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
    keep_pending: bool = typer.Option(False, help="don't delete the pending/ item"),
):
    """Validate extraction JSON, upload to extracted/, and delete the pending/ item."""
    from menudb import handoff
    from menudb.extract.schema import ExtractedMenu

    for f in files:
        ex = ExtractedMenu.model_validate_json(f.read_text())
        handoff.put_json(handoff.key(handoff.EXTRACTED, ex.slug, ex.normalized_hash), ex)
        pk = handoff.key(handoff.PENDING, ex.slug, ex.normalized_hash)
        if not keep_pending and handoff.storage.exists(pk):
            handoff.storage.delete(pk)
        n = sum(len(c.components) for c in ex.courses)
        typer.echo(f"{ex.slug}/{ex.normalized_hash[:12]}: {len(ex.courses)} courses, {n} components")


@extract_io.command("schema")
def extract_io_schema():
    """Print the JSON schema an extraction must match."""
    import json

    from menudb.extract.schema import ExtractedMenu

    typer.echo(json.dumps(ExtractedMenu.model_json_schema(), indent=2))
