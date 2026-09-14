"""Apply pending SQL migrations:  python scripts/migrate.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.api.config import get_settings  # noqa: E402
from apps.api.db import make_pool, run_migrations  # noqa: E402

s = get_settings()
if not s.database_url:
    sys.exit("DATABASE_URL is not set")
pool = make_pool(s.database_url)
applied = run_migrations(pool)
pool.close()
print("applied:", applied or "nothing (up to date)")
