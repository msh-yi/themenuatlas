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
