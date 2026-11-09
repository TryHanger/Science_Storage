import json
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import jsonschema
from pydantic import ValidationError

from research_engine.config import CONTRACTS_DIR, EXCHANGE_DIR
from research_engine.contracts import ReviewResponse
from research_engine.db import get_connection
from research_engine.normalize import compute_sha256


@dataclass
class ValidationIssue:
    kind: str
    path: str
    message: str


@dataclass
class ValidationOutcome:
    is_valid: bool
    is_already_accepted: bool = False
    job_id: uuid.UUID | None = None
    study_key: str | None = None
    protocol_sha256: str | None = None
    issues: list[ValidationIssue] | None = None
    validated_response: ReviewResponse | None = None
    job_db_row: dict[str, Any] | None = None


@dataclass
class AcceptResult:
    status: str  # 'committed', 'already_accepted', 'rejected'
    job_id: uuid.UUID | None
    reviews_count: int = 0
    message: str = ""
    issues: list[ValidationIssue] | None = None
    rejected_dir: Path | None = None
    accepted_dir: Path | None = None


def load_response_schema() -> dict[str, Any]:
    """Load JSON Schema from contracts/response_schema.json."""
    schema_file = CONTRACTS_DIR / "response_schema.json"
    if not schema_file.is_file():
        raise FileNotFoundError(f"Schema file missing: {schema_file}")
    return json.loads(schema_file.read_text(encoding="utf-8"))


def validate_screening_response(
    raw_content: str,
    raw_sha256: str,
    db_url: str | None = None,
) -> ValidationOutcome:
    """Strictly validate incoming LLM screening response against schema and database."""
    issues: list[ValidationIssue] = []

    # 1. JSON Syntax validation
    try:
        parsed_json = json.loads(raw_content)
    except json.JSONDecodeError as exc:
        issues.append(
            ValidationIssue(
                kind="json_syntax",
                path=f"line {exc.lineno}, col {exc.colno} (char {exc.pos})",
                message=f"Invalid JSON syntax: {exc.msg}",
            )
        )
        return ValidationOutcome(is_valid=False, issues=issues)

    if not isinstance(parsed_json, dict):
        issues.append(
            ValidationIssue(
                kind="schema_violation",
                path="$",
                message="Root of response must be a JSON object.",
            )
        )
        return ValidationOutcome(is_valid=False, issues=issues)

    # Attempt to extract job_id early for error reporting and job state tracking
    extracted_job_id = None
    if "job_id" in parsed_json:
        try:
            extracted_job_id = uuid.UUID(str(parsed_json["job_id"]))
        except Exception:
            extracted_job_id = None

    # 2. JSON Schema Draft 2020-12 validation
    schema = load_response_schema()
    validator_cls = jsonschema.validators.validator_for(schema)
    validator = validator_cls(schema)

    for err in sorted(validator.iter_errors(parsed_json), key=lambda e: list(e.path)):
        path = "$." + ".".join(str(p) for p in err.path) if err.path else "$"
        issues.append(
            ValidationIssue(
                kind="schema_violation",
                path=path,
                message=err.message,
            )
        )

    if issues:
        # Check if job exists in DB to populate job_db_row for attempt_count tracking
        job_db_row = None
        if extracted_job_id:
            try:
                with get_connection(db_url) as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT id, status FROM review_jobs WHERE id = %s;", (extracted_job_id,))
                        job_db_row = cur.fetchone()
            except Exception:
                pass
        return ValidationOutcome(is_valid=False, job_id=extracted_job_id, issues=issues, job_db_row=job_db_row)

    # 3. Pydantic model validation
    try:
        response_model = ReviewResponse.model_validate(parsed_json)
    except ValidationError as pydantic_err:
        for p_err in pydantic_err.errors():
            loc = "$." + ".".join(str(x) for x in p_err["loc"])
            issues.append(
                ValidationIssue(
                    kind="model_violation",
                    path=loc,
                    message=p_err["msg"],
                )
            )
        return ValidationOutcome(is_valid=False, issues=issues)

    job_id = response_model.job_id

    # 4. Database integrity checks
    with get_connection(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, study_key, protocol_sha256, schema_version, status,
                       accepted_response_sha256, attempt_count
                FROM review_jobs
                WHERE id = %s;
                """,
                (job_id,),
            )
            job_row = cur.fetchone()

            if not job_row:
                issues.append(
                    ValidationIssue(
                        kind="job_not_found",
                        path="$.job_id",
                        message=f"Job ID {job_id} does not exist in review_jobs table.",
                    )
                )
                return ValidationOutcome(is_valid=False, job_id=job_id, issues=issues)

            study_key = job_row["study_key"]
            expected_protocol_sha256 = job_row["protocol_sha256"].strip()
            expected_schema_version = job_row["schema_version"]
            current_status = job_row["status"]

            # Schema version check
            if response_model.schema_version != expected_schema_version:
                issues.append(
                    ValidationIssue(
                        kind="version_mismatch",
                        path="$.schema_version",
                        message=f"Schema version mismatch: expected '{expected_schema_version}', got '{response_model.schema_version}'.",
                    )
                )

            # Protocol hash check
            if response_model.protocol_sha256 != expected_protocol_sha256:
                issues.append(
                    ValidationIssue(
                        kind="protocol_hash_mismatch",
                        path="$.protocol_sha256",
                        message=f"Protocol SHA-256 mismatch: expected '{expected_protocol_sha256}', got '{response_model.protocol_sha256}'.",
                    )
                )

            # Job status checks
            if current_status == "cancelled":
                issues.append(
                    ValidationIssue(
                        kind="job_cancelled",
                        path="$.job_id",
                        message=f"Job {job_id} has been cancelled and cannot accept reviews.",
                    )
                )

            if current_status == "committed":
                # Check for idempotent resubmission
                accepted_sha = (job_row["accepted_response_sha256"] or "").strip()
                if raw_sha256 == accepted_sha:
                    return ValidationOutcome(
                        is_valid=True,
                        is_already_accepted=True,
                        job_id=job_id,
                        study_key=study_key,
                        protocol_sha256=expected_protocol_sha256,
                        job_db_row=job_row,
                    )
                else:
                    issues.append(
                        ValidationIssue(
                            kind="job_already_committed",
                            path="$.job_id",
                            message=f"Job {job_id} is already committed with a different response. Overwrites are forbidden.",
                        )
                    )

            # Fetch expected work items for this job
            cur.execute(
                """
                SELECT work_id
                FROM review_job_items
                WHERE job_id = %s
                ORDER BY ordinal ASC;
                """,
                (job_id,),
            )
            rows = cur.fetchall()
            expected_work_ids = [r["work_id"] for r in rows]
            expected_set = set(expected_work_ids)

            # Check cardinality, duplicates, missing and extra IDs
            results_work_ids = [r.work_id for r in response_model.results]
            seen_ids: set[uuid.UUID] = set()
            duplicate_ids: set[uuid.UUID] = set()
            for w_id in results_work_ids:
                if w_id in seen_ids:
                    duplicate_ids.add(w_id)
                seen_ids.add(w_id)

            if duplicate_ids:
                for d_id in duplicate_ids:
                    issues.append(
                        ValidationIssue(
                            kind="duplicate_work_id",
                            path="$.results",
                            message=f"Duplicate work_id in results: {d_id}",
                        )
                    )

            missing_ids = expected_set - seen_ids
            if missing_ids:
                for m_id in sorted(missing_ids, key=str):
                    issues.append(
                        ValidationIssue(
                            kind="missing_work_id",
                            path="$.results",
                            message=f"Missing expected work_id from job: {m_id}",
                        )
                    )

            extra_ids = seen_ids - expected_set
            if extra_ids:
                for e_id in sorted(extra_ids, key=str):
                    issues.append(
                        ValidationIssue(
                            kind="extra_work_id",
                            path="$.results",
                            message=f"Unexpected work_id not in job: {e_id}",
                        )
                    )

            if len(response_model.results) != len(expected_work_ids):
                issues.append(
                    ValidationIssue(
                        kind="count_mismatch",
                        path="$.results",
                        message=f"Expected {len(expected_work_ids)} items, got {len(response_model.results)}.",
                    )
                )

    if issues:
        return ValidationOutcome(
            is_valid=False,
            job_id=job_id,
            study_key=study_key,
            protocol_sha256=expected_protocol_sha256,
            issues=issues,
            job_db_row=job_row,
        )

    return ValidationOutcome(
        is_valid=True,
        is_already_accepted=False,
        job_id=job_id,
        study_key=study_key,
        protocol_sha256=expected_protocol_sha256,
        validated_response=response_model,
        job_db_row=job_row,
    )


def generate_repair_prompt(
    job_id: uuid.UUID | None,
    issues: list[ValidationIssue],
) -> str:
    """Generate clear, actionable repair prompt for ChatGPT."""
    issue_lines = [f"- [{issue.kind}] at {issue.path}: {issue.message}" for issue in issues]
    return (
        f"Ваш ответ results.json для Job ID {job_id or 'UNKNOWN'} содержит ошибки валидации:\n\n"
        + "\n".join(issue_lines)
        + "\n\n"
        "Инструкция по исправлению:\n"
        "1. Перегенерируйте и верните ПОЛНЫЙ файл results.json строго по response_schema.json.\n"
        "2. Убедитесь, что ВСЕ work_id из papers.jsonl присутствуют ровно один раз.\n"
        "3. Не добавляйте лишних полей, не меняйте UUID статей и job_id.\n"
        "4. Длина поля 'reason' для каждой статьи должна быть от 12 до 800 символов.\n"
        "5. Поле 'additional_references' должно содержать 'title' и хотя бы одно из ('doi', 'url')."
    )


def accept_response(
    file_path: Path,
    db_url: str | None = None,
    exchange_dir: Path | None = None,
) -> AcceptResult:
    """Validate and atomically commit screening results from response file."""
    if not file_path.is_file():
        raise FileNotFoundError(f"Response file not found: {file_path}")

    raw_bytes = file_path.read_bytes()
    raw_content = raw_bytes.decode("utf-8", errors="replace")
    raw_sha256 = compute_sha256(raw_bytes)

    outcome = validate_screening_response(
        raw_content=raw_content,
        raw_sha256=raw_sha256,
        db_url=db_url,
    )

    base_exchange = exchange_dir or EXCHANGE_DIR

    # Case 1: Idempotent resubmission of already committed file
    if outcome.is_already_accepted and outcome.job_id:
        return AcceptResult(
            status="already_accepted",
            job_id=outcome.job_id,
            reviews_count=0,
            message=f"Job {outcome.job_id} was already accepted with this exact file (safe no-op).",
        )

    # Case 2: Validation failed
    if not outcome.is_valid:
        job_id = outcome.job_id
        timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        subfolder_name = f"{job_id}_{timestamp_str}" if job_id else f"unknown_{timestamp_str}"
        rejected_dir = base_exchange / "rejected" / subfolder_name
        rejected_dir.mkdir(parents=True, exist_ok=True)

        # Save raw response
        (rejected_dir / "results.json").write_bytes(raw_bytes)

        # Save validation errors JSON
        errors_data = [asdict(issue) for issue in (outcome.issues or [])]
        (rejected_dir / "validation_errors.json").write_text(
            json.dumps(errors_data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        # Save repair prompt
        repair_text = generate_repair_prompt(job_id, outcome.issues or [])
        (rejected_dir / "repair_prompt.txt").write_text(repair_text, encoding="utf-8")

        # Update job in DB if known
        if job_id and outcome.job_db_row:
            with get_connection(db_url) as conn:
                with conn.cursor() as cur:
                    summary_msg = "; ".join(i.message for i in (outcome.issues or [])[:3])
                    cur.execute(
                        """
                        UPDATE review_jobs
                        SET status = 'repair_required',
                            attempt_count = attempt_count + 1,
                            last_error = %s
                        WHERE id = %s;
                        """,
                        (summary_msg[:500], job_id),
                    )
                conn.commit()

        return AcceptResult(
            status="rejected",
            job_id=job_id,
            message="Response failed strict validation. Stored in exchange/rejected/.",
            issues=outcome.issues,
            rejected_dir=rejected_dir,
        )

    # Case 3: Validation succeeded -> commit in a single transaction
    assert outcome.validated_response is not None
    assert outcome.job_id is not None
    assert outcome.study_key is not None
    assert outcome.protocol_sha256 is not None

    job_id = outcome.job_id
    study_key = outcome.study_key
    protocol_sha256 = outcome.protocol_sha256
    results = outcome.validated_response.results

    with get_connection(db_url) as conn:
        with conn.cursor() as cur:
            # Insert paper_reviews
            for item in results:
                cur.execute(
                    """
                    INSERT INTO paper_reviews (
                        job_id, work_id, study_key, protocol_sha256,
                        decision, evidence_level, needs_fulltext, reason, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now());
                    """,
                    (
                        job_id,
                        item.work_id,
                        study_key,
                        protocol_sha256,
                        item.decision,
                        item.evidence_level,
                        item.needs_fulltext,
                        item.reason,
                    ),
                )

            # Update review_jobs to committed
            cur.execute(
                """
                UPDATE review_jobs
                SET status = 'committed',
                    accepted_response_sha256 = %s,
                    committed_at = now(),
                    last_error = NULL
                WHERE id = %s;
                """,
                (raw_sha256, job_id),
            )
        conn.commit()

    # Save raw accepted file
    accepted_dir = base_exchange / "accepted" / str(job_id)
    accepted_dir.mkdir(parents=True, exist_ok=True)
    (accepted_dir / "results.json").write_bytes(raw_bytes)

    return AcceptResult(
        status="committed",
        job_id=job_id,
        reviews_count=len(results),
        message=f"Successfully committed {len(results)} paper reviews for job {job_id}.",
        accepted_dir=accepted_dir,
    )
