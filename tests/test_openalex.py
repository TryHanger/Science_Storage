import json
from pathlib import Path
import psycopg
from research_engine.db import get_connection
from research_engine.openalex import import_works_from_fixture


def test_import_fixture_without_abstract_and_provenance(test_db, tmp_path):
    """3. Без abstract импорт работает."""
    fixture_data = [
        {
            "title": "Paper Without Abstract",
            "doi": "10.1234/no-abstract",
            "openalex_id": "W111",
            "abstract": None,
            "publication_year": 2021,
            "first_author": "Dr. NoAbstract",
        }
    ]
    f_path = tmp_path / "fixture.json"
    f_path.write_text(json.dumps(fixture_data), encoding="utf-8")

    stats = import_works_from_fixture(fixture_path=f_path, db_url=test_db)
    assert stats.inserted_count == 1
    assert stats.fetched_count == 1
    assert stats.status == "completed"

    with get_connection(test_db) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT title, abstract, doi, openalex_id FROM works WHERE openalex_id = 'W111';")
            row = cur.fetchone()
            assert row is not None
            assert row["title"] == "Paper Without Abstract"
            assert row["abstract"] is None
            assert row["doi"] == "10.1234/no-abstract"


def test_import_deduplication_preserves_provenance(test_db, tmp_path):
    """2. Дубликат OpenAlex/DOI не создаёт новую works, source_records сохраняет источник."""
    fixture_batch_1 = [
        {
            "title": "HVAC Fault Detection Part 1",
            "doi": "10.1016/j.fdd.2023.01",
            "openalex_id": "W999",
            "abstract": "Abstract text",
            "publication_year": 2023,
            "first_author": "First Author",
        }
    ]
    # Second search run with the exact same publication (e.g. matching DOI/OpenAlex ID)
    fixture_batch_2 = [
        {
            "title": "HVAC Fault Detection Part 1 (Updated Metadata)",
            "doi": "https://doi.org/10.1016/J.FDD.2023.01",
            "openalex_id": "https://openalex.org/w999",
            "abstract": "Abstract text",
            "publication_year": 2023,
            "first_author": "First Author",
        }
    ]

    f1 = tmp_path / "batch1.json"
    f2 = tmp_path / "batch2.json"
    f1.write_text(json.dumps(fixture_batch_1), encoding="utf-8")
    f2.write_text(json.dumps(fixture_batch_2), encoding="utf-8")

    # Run 1
    stats1 = import_works_from_fixture(fixture_path=f1, db_url=test_db, query="query_1")
    assert stats1.inserted_count == 1
    assert stats1.duplicate_count == 0

    # Run 2
    stats2 = import_works_from_fixture(fixture_path=f2, db_url=test_db, query="query_2")
    assert stats2.inserted_count == 0
    assert stats2.duplicate_count == 1

    with get_connection(test_db) as conn:
        with conn.cursor() as cur:
            # Check works table has exactly 1 row
            cur.execute("SELECT COUNT(*) AS count FROM works;")
            assert cur.fetchone()["count"] == 1

            # Check source_records table has 2 records linked to the same work_id
            cur.execute("SELECT id, work_id, search_run_id, provider FROM source_records;")
            sources = cur.fetchall()
            assert len(sources) == 2
            assert sources[0]["work_id"] == sources[1]["work_id"]
            assert sources[0]["search_run_id"] != sources[1]["search_run_id"]
