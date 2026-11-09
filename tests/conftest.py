import os
import uuid
from pathlib import Path
import psycopg
from psycopg import sql
import pytest

from research_engine.config import CONTRACTS_DIR
from research_engine.db import init_db

# Base URL to postgres server
POSTGRES_SERVER_URL = os.getenv(
    "TEST_POSTGRES_URL",
    "postgresql://postgres:postgrespassword@127.0.0.1:5434/postgres",
)


@pytest.fixture(scope="session")
def contracts_dir() -> Path:
    return CONTRACTS_DIR


@pytest.fixture(scope="session")
def protocol_path(contracts_dir: Path) -> Path:
    return contracts_dir / "research_protocol.md"


@pytest.fixture
def test_db():
    """Create a fresh temporary database for each test and drop it afterwards."""
    test_db_name = f"test_{uuid.uuid4().hex[:12]}"
    admin_conn = psycopg.connect(POSTGRES_SERVER_URL, autocommit=True)
    with admin_conn.cursor() as cur:
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(test_db_name)))
    admin_conn.close()

    # Construct test database URL
    test_db_url = f"postgresql://postgres:postgrespassword@127.0.0.1:5434/{test_db_name}"

    # Initialize schema
    init_db(db_url=test_db_url)

    yield test_db_url

    # Drop database
    admin_conn = psycopg.connect(POSTGRES_SERVER_URL, autocommit=True)
    with admin_conn.cursor() as cur:
        # Terminate active connections before dropping
        cur.execute(
            sql.SQL(
                """
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE datname = {} AND pid <> pg_backend_pid();
                """
            ).format(sql.Literal(test_db_name))
        )
        cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(test_db_name)))
    admin_conn.close()
