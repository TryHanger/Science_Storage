from pathlib import Path
from research_engine.gold import evaluate_gold_set
from research_engine.validator import accept_response
from tests.test_validator import setup_exported_job
import json


def test_gold_empty_template_yields_not_run(test_db, contracts_dir):
    template_path = contracts_dir / "gold_set_template.csv"
    res = evaluate_gold_set(file_path=template_path, study_key="buildingpulse_fdd", db_url=test_db)
    assert res.status == "NOT RUN"
    assert "empty or contains no labeled entries" in res.message


def test_gold_evaluation_with_labels(test_db, protocol_path, tmp_path):
    job_res, work_ids = setup_exported_job(test_db, protocol_path, tmp_path, count=50)

    # Commit reviews where work_ids[0] is 'include', work_ids[1] is 'exclude'
    resp_data = {
        "schema_version": "1.0",
        "job_id": str(job_res.job_id),
        "protocol_sha256": job_res.protocol_sha256,
        "results": [
            {
                "work_id": wid,
                "decision": "include" if idx == 0 else "exclude",
                "evidence_level": "abstract_only",
                "needs_fulltext": False,
                "reason": "Clear methodological focus meeting screening protocol.",
            }
            for idx, wid in enumerate(work_ids)
        ],
    }
    resp_file = tmp_path / "resp.json"
    resp_file.write_text(json.dumps(resp_data), encoding="utf-8")
    accept_response(resp_file, db_url=test_db, exchange_dir=tmp_path)

    # Create gold csv: work_ids[0] expected 'include', work_ids[1] expected 'include' (will be a false exclusion!)
    gold_csv = tmp_path / "gold.csv"
    gold_csv.write_text(
        f"work_id_or_doi,expected_decision,reason,labeler\n"
        f"{work_ids[0]},include,Pioneering FDD algorithm,Expert1\n"
        f"{work_ids[1]},include,Crucial sensor fault methodology,Expert1\n",
        encoding="utf-8",
    )

    res = evaluate_gold_set(file_path=gold_csv, study_key="buildingpulse_fdd", db_url=test_db)
    assert res.status == "COMPLETED"
    assert res.total_gold == 2
    assert res.total_relevant == 2
    assert res.true_positives == 1
    assert res.recall == 0.5
    assert len(res.false_exclusions) == 1
    assert res.false_exclusions[0]["target"] == str(work_ids[1])
