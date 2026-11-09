from contextlib import contextmanager
from pathlib import Path
from typing import Generator
import psycopg
from psycopg.rows import dict_row

from research_engine.config import CONTRACTS_DIR, DATABASE_URL

EXPECTED_TABLES = {
    "works",
    "search_runs",
    "source_records",
    "review_jobs",
    "review_job_items",
    "paper_reviews",
}


def get_connection(db_url: str | None = None) -> psycopg.Connection:
    """Create a psycopg 3 connection to PostgreSQL."""
    url = db_url or DATABASE_URL
    return psycopg.connect(url, row_factory=dict_row)


@contextmanager
def get_db_cursor(db_url: str | None = None) -> Generator[psycopg.Cursor, None, None]:
    """Context manager for database cursor with automatic commit/rollback."""
    with get_connection(db_url) as conn:
        with conn.cursor() as cur:
            yield cur
        conn.commit()


def init_db(db_url: str | None = None, schema_file: Path | None = None) -> None:
    """Execute schema.sql to initialize or verify project tables."""
    schema_path = schema_file or (CONTRACTS_DIR / "schema.sql")
    if not schema_path.is_file():
        raise FileNotFoundError(f"Schema file not found at: {schema_path}")

    sql = schema_path.read_text(encoding="utf-8")
    with get_connection(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()


def verify_tables(db_url: str | None = None) -> set[str]:
    """Return set of project tables present in public schema."""
    query = """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_type = 'BASE TABLE';
    """
    with get_connection(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(query)
            rows = cur.fetchall()
            found = {row["table_name"] for row in rows}
            return found.intersection(EXPECTED_TABLES)
