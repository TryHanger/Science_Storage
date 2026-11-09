import json
import uuid
import zipfile
from pathlib import Path
import psycopg
import pytest

from research_engine.db import get_connection
from research_engine.queue import create_next_job
from research_engine.validator import accept_response


def setup_exported_job(test_db, protocol_path, tmp_path, count=50):
    """Helper to populate works and export a job."""
    from tests.test_queue import populate_dummy_works

    populate_dummy_works(test_db, count=count)
    res = create_next_job(
        study_key="buildingpulse_fdd",
        protocol_path=protocol_path,
        count=count,
        exchange_dir=tmp_path,
        db_url=test_db,
    )
    with zipfile.ZipFile(res.package_path, "r") as zf:
        manifest = json.loads(zf.read("manifest.json"))
    return res, manifest["work_ids"]


def test_valid_response_yields_50_reviews_and_committed_status(test_db, protocol_path, tmp_path):
    """6. Тестовый ответ с 50 валидными ID даёт ровно 50 reviews/committed."""
    job_res, work_ids = setup_exported_job(test_db, protocol_path, tmp_path, count=50)

    results_payload = [
        {
            "work_id": wid,
            "decision": "include" if idx % 2 == 0 else "exclude",
            "evidence_level": "abstract_only",
            "needs_fulltext": True if idx % 2 == 0 else False,
            "reason": f"Detailed reason explaining decision for publication {wid}",
        }
        for idx, wid in enumerate(work_ids)
    ]
    response_data = {
        "schema_version": "1.0",
        "job_id": str(job_res.job_id),
        "protocol_sha256": job_res.protocol_sha256,
        "results": results_payload,
        "additional_references": [
            {
                "title": "Supplementary foundational paper",
                "doi": "10.1000/additional.1",
                "why_relevant": "Discovered during analysis of domain concepts",
            }
        ],
    }

    resp_file = tmp_path / "valid_response.json"
    resp_file.write_text(json.dumps(response_data), encoding="utf-8")

    accept_res = accept_response(resp_file, db_url=test_db, exchange_dir=tmp_path)
    assert accept_res.status == "committed"
    assert accept_res.reviews_count == 50

    with get_connection(test_db) as conn:
        with conn.cursor() as cur:
            # Check review_jobs status
            cur.execute("SELECT status, accepted_response_sha256 FROM review_jobs WHERE id = %s;", (job_res.job_id,))
            job_row = cur.fetchone()
            assert job_row["status"] == "committed"
            assert job_row["accepted_response_sha256"] is not None

            # Check paper_reviews count
            cur.execute("SELECT COUNT(*) AS count FROM paper_reviews WHERE job_id = %s;", (job_res.job_id,))
            assert cur.fetchone()["count"] == 50


def test_repeat_identical_response_is_idempotent_no_op(test_db, protocol_path, tmp_path):
    """7. Повтор того же ответа не меняет количество reviews."""
    job_res, work_ids = setup_exported_job(test_db, protocol_path, tmp_path, count=50)

    response_data = {
        "schema_version": "1.0",
        "job_id": str(job_res.job_id),
        "protocol_sha256": job_res.protocol_sha256,
        "results": [
            {
                "work_id": wid,
                "decision": "uncertain",
                "evidence_level": "abstract_only",
                "needs_fulltext": True,
                "reason": "Uncertain evidence from abstract alone, requires fulltext.",
            }
            for wid in work_ids
        ],
    }
    resp_file = tmp_path / "results.json"
    resp_file.write_text(json.dumps(response_data), encoding="utf-8")

    # First accept
    res1 = accept_response(resp_file, db_url=test_db, exchange_dir=tmp_path)
    assert res1.status == "committed"

    # Second accept (identical file)
    res2 = accept_response(resp_file, db_url=test_db, exchange_dir=tmp_path)
    assert res2.status == "already_accepted"

    with get_connection(test_db) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS count FROM paper_reviews WHERE job_id = %s;", (job_res.job_id,))
            assert cur.fetchone()["count"] == 50


def test_invalid_job_id_and_protocol_hash_rejected(test_db, protocol_path, tmp_path):
    """8. Неправильный job_id/хеш протокола отклоняется."""
    job_res, work_ids = setup_exported_job(test_db, protocol_path, tmp_path, count=50)

    # 1. Non-existent job_id
    fake_job_id = str(uuid.uuid4())
    resp_bad_job = {
        "schema_version": "1.0",
        "job_id": fake_job_id,
        "protocol_sha256": job_res.protocol_sha256,
        "results": [
            {
                "work_id": work_ids[0],
                "decision": "include",
                "evidence_level": "abstract_only",
                "needs_fulltext": False,
                "reason": "Valid reason length greater than twelve characters",
            }
        ],
    }
    f1 = tmp_path / "bad_job.json"
    f1.write_text(json.dumps(resp_bad_job), encoding="utf-8")
    res1 = accept_response(f1, db_url=test_db, exchange_dir=tmp_path)
    assert res1.status == "rejected"
    assert any(i.kind == "job_not_found" for i in res1.issues)

    # 2. Wrong protocol SHA-256
    resp_bad_hash = {
        "schema_version": "1.0",
        "job_id": str(job_res.job_id),
        "protocol_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
        "results": [
            {
                "work_id": wid,
                "decision": "include",
                "evidence_level": "abstract_only",
                "needs_fulltext": False,
                "reason": "Valid reason length greater than twelve characters",
            }
            for wid in work_ids
        ],
    }
    f2 = tmp_path / "bad_hash.json"
    f2.write_text(json.dumps(resp_bad_hash), encoding="utf-8")
    res2 = accept_response(f2, db_url=test_db, exchange_dir=tmp_path)
    assert res2.status == "rejected"
    assert any(i.kind == "protocol_hash_mismatch" for i in res2.issues)


def test_missing_extra_duplicate_ids_rejected(test_db, protocol_path, tmp_path):
    """9. Missing, extra, duplicate IDs отклоняются с точными сообщениями."""
    job_res, work_ids = setup_exported_job(test_db, protocol_path, tmp_path, count=50)

    # Construct response with:
    # - 1 duplicate ID (work_ids[0] repeated)
    # - 1 missing ID (work_ids[1] omitted)
    # - 1 extra unexpected ID (random UUID)
    tampered_ids = [work_ids[0], work_ids[0]] + work_ids[2:] + [str(uuid.uuid4())]

    resp_data = {
        "schema_version": "1.0",
        "job_id": str(job_res.job_id),
        "protocol_sha256": job_res.protocol_sha256,
        "results": [
            {
                "work_id": wid,
                "decision": "include",
                "evidence_level": "abstract_only",
                "needs_fulltext": False,
                "reason": "Valid reason length greater than twelve characters",
            }
            for wid in tampered_ids
        ],
    }
    resp_file = tmp_path / "tampered.json"
    resp_file.write_text(json.dumps(resp_data), encoding="utf-8")

    res = accept_response(resp_file, db_url=test_db, exchange_dir=tmp_path)
    assert res.status == "rejected"
    issue_kinds = {i.kind for i in res.issues}
    assert "duplicate_work_id" in issue_kinds
    assert "missing_work_id" in issue_kinds
    assert "extra_work_id" in issue_kinds


def test_malformed_json_and_extra_fields_zero_partial_writes(test_db, protocol_path, tmp_path):
    """10. Сломанный JSON/неизвестные поля/несоответствие типов отклоняются, 0 частичных записей."""
    job_res, work_ids = setup_exported_job(test_db, protocol_path, tmp_path, count=50)

    # Broken JSON syntax
    bad_json_file = tmp_path / "broken.json"
    bad_json_file.write_text("{\n  \"schema_version\": \"1.0\",\n  invalid syntax here\n}", encoding="utf-8")

    res_broken = accept_response(bad_json_file, db_url=test_db, exchange_dir=tmp_path)
    assert res_broken.status == "rejected"
    assert any(i.kind == "json_syntax" for i in res_broken.issues)

    # Disallowed extra field at root
    extra_field_data = {
        "schema_version": "1.0",
        "job_id": str(job_res.job_id),
        "protocol_sha256": job_res.protocol_sha256,
        "forbidden_extra_field": "disallowed",
        "results": [
            {
                "work_id": wid,
                "decision": "include",
                "evidence_level": "abstract_only",
                "needs_fulltext": False,
                "reason": "Valid reason length greater than twelve characters",
            }
            for wid in work_ids
        ],
    }
    extra_field_file = tmp_path / "extra_field.json"
    extra_field_file.write_text(json.dumps(extra_field_data), encoding="utf-8")

    res_extra = accept_response(extra_field_file, db_url=test_db, exchange_dir=tmp_path)
    assert res_extra.status == "rejected"
    assert any(i.kind == "schema_violation" for i in res_extra.issues)

    # Verify zero paper_reviews were written
    with get_connection(test_db) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS count FROM paper_reviews;")
            assert cur.fetchone()["count"] == 0
