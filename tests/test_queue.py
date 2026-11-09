import json
import zipfile
from pathlib import Path
import pytest

from research_engine.normalize import compute_sha256
from research_engine.openalex import import_works_from_fixture
from research_engine.queue import cancel_job, create_next_job
from research_engine.validator import accept_response


def populate_dummy_works(test_db, count=120):
    """Helper to insert N works into test DB via fixture."""
    items = []
    for i in range(1, count + 1):
        items.append(
            {
                "title": f"Paper Title {i}",
                "doi": f"10.1000/paper.{i}",
                "openalex_id": f"W{100000 + i}",
                "abstract": f"Abstract for paper {i}",
                "publication_year": 2020 + (i % 5),
                "first_author": f"Author {i}",
            }
        )
    import tempfile
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json", encoding="utf-8") as f:
        json.dump(items, f)
        temp_name = Path(f.name)
    try:
        import_works_from_fixture(temp_name, db_url=test_db)
    finally:
        temp_name.unlink(missing_ok=True)


def test_next_creates_valid_zip_with_50_distinct_ids_and_correct_hash(test_db, protocol_path, tmp_path):
    """4. next из 50 создаёт ZIP с 50 разными ID и корректным хешем papers.jsonl."""
    populate_dummy_works(test_db, count=60)

    res = create_next_job(
        study_key="buildingpulse_fdd",
        protocol_path=protocol_path,
        count=50,
        exchange_dir=tmp_path,
        db_url=test_db,
    )

    assert not res.is_reused
    assert res.paper_count == 50
    assert res.package_path.is_file()

    # Inspect zip contents
    with zipfile.ZipFile(res.package_path, "r") as zf:
        file_list = zf.namelist()
        assert set(file_list) == {
            "manifest.json",
            "papers.jsonl",
            "research_protocol.md",
            "instructions.md",
            "response_schema.json",
        }

        manifest_data = json.loads(zf.read("manifest.json"))
        papers_jsonl_bytes = zf.read("papers.jsonl")

        assert manifest_data["paper_count"] == 50
        work_ids = manifest_data["work_ids"]
        assert len(work_ids) == 50
        assert len(set(work_ids)) == 50  # All 50 IDs are distinct

        # Manifest papers_sha256 must match the exact sha256 of papers.jsonl
        expected_hash = compute_sha256(papers_jsonl_bytes)
        assert manifest_data["papers_sha256"] == expected_hash


def test_next_second_time_reuses_existing_package(test_db, protocol_path, tmp_path):
    """5. next второй раз до accept не резервирует новые 50."""
    populate_dummy_works(test_db, count=100)

    # First call to next
    res1 = create_next_job(
        study_key="buildingpulse_fdd",
        protocol_path=protocol_path,
        count=50,
        exchange_dir=tmp_path,
        db_url=test_db,
    )
    assert not res1.is_reused

    # Second call to next before accept
    res2 = create_next_job(
        study_key="buildingpulse_fdd",
        protocol_path=protocol_path,
        count=50,
        exchange_dir=tmp_path,
        db_url=test_db,
    )
    assert res2.is_reused
    assert res2.job_id == res1.job_id
    assert res2.package_path == res1.package_path


def test_next_subsequent_batch_after_accept(test_db, protocol_path, tmp_path):
    """11. Обработав первую партию, следующий next берёт следующие 50."""
    populate_dummy_works(test_db, count=100)

    # Batch 1
    res1 = create_next_job(
        study_key="buildingpulse_fdd",
        protocol_path=protocol_path,
        count=50,
        exchange_dir=tmp_path,
        db_url=test_db,
    )

    with zipfile.ZipFile(res1.package_path, "r") as zf:
        manifest1 = json.loads(zf.read("manifest.json"))
        work_ids1 = manifest1["work_ids"]

    # Generate synthetic response for Batch 1
    resp1_data = {
        "schema_version": "1.0",
        "job_id": str(res1.job_id),
        "protocol_sha256": res1.protocol_sha256,
        "results": [
            {
                "work_id": wid,
                "decision": "include",
                "evidence_level": "abstract_only",
                "needs_fulltext": False,
                "reason": "Valid reason for screening acceptance criteria.",
            }
            for wid in work_ids1
        ],
    }
    resp1_file = tmp_path / "resp1.json"
    resp1_file.write_text(json.dumps(resp1_data), encoding="utf-8")

    accept_res = accept_response(resp1_file, db_url=test_db, exchange_dir=tmp_path)
    assert accept_res.status == "committed"

    # Batch 2
    res2 = create_next_job(
        study_key="buildingpulse_fdd",
        protocol_path=protocol_path,
        count=50,
        exchange_dir=tmp_path,
        db_url=test_db,
    )
    assert not res2.is_reused
    assert res2.job_id != res1.job_id

    with zipfile.ZipFile(res2.package_path, "r") as zf:
        manifest2 = json.loads(zf.read("manifest.json"))
        work_ids2 = manifest2["work_ids"]

    assert len(work_ids2) == 50
    # Batches must have zero intersection
    assert set(work_ids1).isdisjoint(set(work_ids2))


def test_cancel_job_releases_reserved_works(test_db, protocol_path, tmp_path):
    """12. Отмена/повтор после отказа не приводит к потере записей."""
    populate_dummy_works(test_db, count=50)

    res = create_next_job(
        study_key="buildingpulse_fdd",
        protocol_path=protocol_path,
        count=50,
        exchange_dir=tmp_path,
        db_url=test_db,
    )

    # Cancel the active job
    cancel_msg = cancel_job(res.job_id, db_url=test_db)
    assert "successfully cancelled" in cancel_msg

    # Next job should now be able to pick up those same candidate works again
    res2 = create_next_job(
        study_key="buildingpulse_fdd",
        protocol_path=protocol_path,
        count=50,
        exchange_dir=tmp_path,
        db_url=test_db,
    )
    assert not res2.is_reused
    assert res2.paper_count == 50
    assert res2.job_id != res.job_id
