from pathlib import Path

from app.config import MIGRATIONS_DATABASE_URL, PAYMENTS_DB_CONNECT_TIMEOUT

try:
    import psycopg2
except Exception:
    psycopg2 = None


MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db" / "migrations"


def _connect():
    if psycopg2 is None:
        raise RuntimeError("psycopg2 is required for migrations.")
    if not MIGRATIONS_DATABASE_URL:
        raise RuntimeError("MIGRATIONS_DATABASE_URL is required for migrations.")
    return psycopg2.connect(MIGRATIONS_DATABASE_URL, connect_timeout=int(PAYMENTS_DB_CONNECT_TIMEOUT))


def _ensure_schema_migrations(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )


def _load_sql_migrations():
    if not MIGRATIONS_DIR.exists():
        raise RuntimeError(f"Migrations directory is missing: {MIGRATIONS_DIR}")
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def apply_migrations():
    migrations = _load_sql_migrations()
    with _connect() as conn:
        with conn.cursor() as cur:
            _ensure_schema_migrations(cur)
            cur.execute("SELECT version FROM schema_migrations")
            applied = {row[0] for row in cur.fetchall()}
            for path in migrations:
                version = path.name
                if version in applied:
                    continue
                sql = path.read_text(encoding="utf-8")
                cur.execute(sql)
                cur.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (%s, NOW())",
                    (version,),
                )


def main():
    apply_migrations()


if __name__ == "__main__":
    main()
