import csv
import logging
import os
import uuid
from pathlib import Path

import httpx
import typer
import yaml
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
    from menudb.db.models import ListMembership, Restaurant, Source
    from menudb.recipes import Recipe

    with get_session() as session, csv_path.open() as f:
        for row in csv.DictReader(f):
            r = session.scalar(select(Restaurant).where(Restaurant.slug == row["slug"]))
            if r is None:
                r = Restaurant(slug=row["slug"])
                session.add(r)
            r.name, r.city, r.country = row["name"], row["city"] or None, row["country"] or None
            r.lat = float(row["lat"]) if row["lat"] else None
            r.lon = float(row["lon"]) if row["lon"] else None
            r.website = row["website"] or None
            r.publishes_menu = row.get("publishes_menu") or "unknown"
            session.flush()

            for entry in filter(None, (row.get("lists") or "").split(";")):
                list_name, year, value = entry.split(":")
                m = session.scalar(select(ListMembership).where(
                    ListMembership.restaurant_id == r.id,
                    ListMembership.list_name == list_name, ListMembership.year == int(year)))
                if m is None:
                    session.add(ListMembership(restaurant_id=r.id, list_name=list_name,
                                               year=int(year), value=value))
                else:
                    m.value = value

            recipe_file = recipes_dir / f"{r.slug}.yaml"
            if recipe_file.exists():
                docs = yaml.safe_load(recipe_file.read_text())
                for label, body in (docs if "start" not in docs else {"main": docs}).items():
                    recipe = Recipe.model_validate(body).model_dump(mode="json")
                    src = session.scalar(select(Source).where(
                        Source.restaurant_id == r.id, Source.label == label))
                    if src is None:
                        session.add(Source(restaurant_id=r.id, label=label, recipe=recipe, validators={}))
                    elif src.recipe != recipe:
                        src.recipe, src.consecutive_failures, src.last_error = recipe, 0, None
        session.commit()
    typer.echo("seeded")


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

            with BrowserRenderer(s.user_agent) as renderer:
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
