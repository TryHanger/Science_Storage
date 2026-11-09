import json
import zipfile
from pathlib import Path
from research_engine.db import get_connection
from research_engine.openalex import import_works_from_fixture
from research_engine.queue import create_next_job, get_study_status
from research_engine.validator import accept_response


def test_full_e2e_cycle_with_synthetic_fixture(test_db, protocol_path, contracts_dir, tmp_path):
    """Full end-to-end screening cycle demonstration with synthetic fixture."""
    # Step 1: Import 50 synthetic papers from contracts/papers.jsonl
    papers_fixture = contracts_dir / "papers.jsonl"
    import_stats = import_works_from_fixture(
        fixture_path=papers_fixture,
        db_url=test_db,
        query="e2e_synthetic_test",
    )
    assert import_stats.status == "completed"
    assert import_stats.inserted_count == 50

    # Step 2: Next job exports 50 publications into ZIP
    job_result = create_next_job(
        study_key="buildingpulse_fdd",
        protocol_path=protocol_path,
        count=50,
        exchange_dir=tmp_path,
        db_url=test_db,
    )
    assert not job_result.is_reused
    assert job_result.paper_count == 50
    assert job_result.package_path.is_file()

    # Step 3: Accept the example synthetic response from contracts/example_results.json
    # Notice: example_results.json in contracts has a preset job_id ("11111111-1111-4111-8111-111111111111").
    # We update job_id in synthetic payload to match the newly exported job_id:
    raw_example = json.loads((contracts_dir / "example_results.json").read_text(encoding="utf-8"))
    raw_example["job_id"] = str(job_result.job_id)
    raw_example["protocol_sha256"] = job_result.protocol_sha256

    test_response_file = tmp_path / "incoming_synthetic_results.json"
    test_response_file.write_text(json.dumps(raw_example), encoding="utf-8")

    # Step 4: Strict validation & commit
    accept_result = accept_response(
        file_path=test_response_file,
        db_url=test_db,
        exchange_dir=tmp_path,
    )
    assert accept_result.status == "committed"
    assert accept_result.reviews_count == 50

    # Step 5: Verify status
    status = get_study_status(study_key="buildingpulse_fdd", protocol_path=protocol_path, db_url=test_db)
    assert status["total_works"] == 50
    assert status["committed_reviews"] == 50
    assert status["pending_works"] == 0
    assert status["jobs"]["committed"] == 1
    assert status["active_job"] is None
