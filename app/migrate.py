"""Apply SQL migrations.  Usage:  python -m app.migrate [--status]"""
import sys
from pathlib import Path

from . import db
from .config import database_path

MIGRATIONS = Path(__file__).parent / "migrations"


def migrate(path=None, verbose=True):
    conn = db.connect(path or database_path())
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at INTEGER NOT NULL)")
    done = {r["name"] for r in conn.execute("SELECT name FROM schema_migrations")}
    applied = []
    for f in sorted(MIGRATIONS.glob("*.sql")):
        if f.name in done:
            continue
        script = "BEGIN;\n%s\nINSERT INTO schema_migrations(name, applied_at) VALUES ('%s', strftime('%%s','now'));\nCOMMIT;" % (
            f.read_text(encoding="utf-8"), f.name)
        try:
            conn.executescript(script)
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        applied.append(f.name)
        if verbose:
            print("applied", f.name)
    if verbose and not applied:
        print("database is up to date")
    conn.close()
    return applied


def status(path=None):
    conn = db.connect(path or database_path())
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at INTEGER NOT NULL)")
    done = {r["name"] for r in conn.execute("SELECT name FROM schema_migrations")}
    for f in sorted(MIGRATIONS.glob("*.sql")):
        print(("[x] " if f.name in done else "[ ] ") + f.name)


if __name__ == "__main__":
    if "--status" in sys.argv:
        status()
    else:
        migrate()
