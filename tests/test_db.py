from research_engine.db import EXPECTED_TABLES, init_db, verify_tables


def test_init_db_creates_exactly_six_tables_and_is_idempotent(test_db):
    """1. Созданы ровно 6 проектных таблиц; повторный db init безопасен."""
    tables = verify_tables(db_url=test_db)
    assert tables == EXPECTED_TABLES
    assert len(tables) == 6

    # Re-run init_db (must be safe and idempotent)
    init_db(db_url=test_db)
    tables_after = verify_tables(db_url=test_db)
    assert tables_after == EXPECTED_TABLES
