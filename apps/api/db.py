"""PostgreSQL access: connection pool + SQL-file migration runner."""
from __future__ import annotations

import logging
import re
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from apps.api.config import REPO_ROOT

log = logging.getLogger(__name__)
MIGRATIONS_DIR = REPO_ROOT / "database" / "migrations"
_VERSION_RE = re.compile(r"^(\d{3}_[a-z0-9_]+)\.sql$")


def make_pool(database_url: str, *, min_size: int = 1, max_size: int = 8) -> ConnectionPool:
    pool = ConnectionPool(database_url, min_size=min_size, max_size=max_size, kwargs={"row_factory": dict_row}, open=False)
    pool.open(wait=True, timeout=10)
    return pool


def run_migrations(pool: ConnectionPool, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply database/migrations/NNN_name.sql in order; each file records its own version."""
    applied: list[str] = []
    with pool.connection() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
        done = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()}
        for f in sorted(migrations_dir.glob("*.sql")):
            m = _VERSION_RE.match(f.name)
            if not m or m.group(1) in done:
                continue
            log.info("applying migration %s", f.name)
            with conn.transaction():
                conn.execute(f.read_text(encoding="utf-8"))
                conn.execute("INSERT INTO schema_migrations (version) VALUES (%s) ON CONFLICT DO NOTHING", (m.group(1),))
            applied.append(m.group(1))
    return applied


def ping(database_url: str) -> bool:
    try:
        with psycopg.connect(database_url, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
        return True
    except psycopg.Error as exc:
        log.warning("database not reachable (%s): %s", database_url.split("@")[-1], exc)
        return False
