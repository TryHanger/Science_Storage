import io
import json
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import psycopg
from psycopg.types.json import Jsonb

from research_engine.config import CONTRACTS_DIR, EXCHANGE_DIR
from research_engine.contracts import Manifest, PaperItem
from research_engine.db import get_connection
from research_engine.normalize import compute_file_sha256, compute_sha256


@dataclass
class JobExportResult:
    job_id: uuid.UUID
    study_key: str
    protocol_sha256: str
    paper_count: int
    package_path: Path
    is_reused: bool
    chatgpt_prompt: str


def get_active_job(
    study_key: str, protocol_sha256: str, db_url: str | None = None
) -> dict[str, Any] | None:
    """Find currently active (uncommitted) review job for study and protocol."""
    query = """
        SELECT id, study_key, protocol_sha256, status, package_path, attempt_count, last_error, created_at
        FROM review_jobs
        WHERE study_key = %s
          AND protocol_sha256 = %s
          AND status IN ('exported', 'repair_required')
        ORDER BY created_at DESC
        LIMIT 1;
    """
    with get_connection(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(query, (study_key, protocol_sha256))
            return cur.fetchone()


def build_chatgpt_prompt(job_id: uuid.UUID, paper_count: int) -> str:
    """Generate ready-to-paste prompt for ChatGPT."""
    return (
        f"Выполни screening статей по приложенному заданию Research Engine (Job ID: {job_id}).\n"
        f"1. Открой ZIP-пакет (файлы manifest.json, papers.jsonl, research_protocol.md, instructions.md, response_schema.json).\n"
        f"2. Оцени строго каждую из {paper_count} статей по research_protocol.md, используя исключительно title и abstract. Не придумывай facts/fulltext.\n"
        f"3. Обязательно обработай ровно все {paper_count} идентификаторов work_id без пропусков, изменений и дубликатов.\n"
        f"4. Сформируй и верни один скачиваемый файл results.json, строго валидный по response_schema.json."
    )


def create_next_job(
    study_key: str,
    protocol_path: Path,
    count: int = 50,
    exchange_dir: Path | None = None,
    db_url: str | None = None,
) -> JobExportResult:
    """Create next review job batch of publications and export package.
    
    If an uncommitted active job already exists, reuses it without allocating a new batch.
    """
    if not protocol_path.is_file():
        raise FileNotFoundError(f"Protocol file not found: {protocol_path}")

    protocol_sha256 = compute_file_sha256(protocol_path)
    protocol_text = protocol_path.read_text(encoding="utf-8")

    # Check for existing uncommitted job
    active = get_active_job(study_key, protocol_sha256, db_url=db_url)
    if active:
        pkg_path = Path(active["package_path"])
        job_id = active["id"]

        # Count items in existing job
        with get_connection(db_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) AS count FROM review_job_items WHERE job_id = %s;",
                    (job_id,),
                )
                row = cur.fetchone()
                item_count = row["count"] if row else 0

        prompt = build_chatgpt_prompt(job_id, item_count)
        return JobExportResult(
            job_id=job_id,
            study_key=study_key,
            protocol_sha256=protocol_sha256,
            paper_count=item_count,
            package_path=pkg_path,
            is_reused=True,
            chatgpt_prompt=prompt,
        )

    # Allocate next batch of candidate works
    out_dir = (exchange_dir or EXCHANGE_DIR) / "outgoing"
    out_dir.mkdir(parents=True, exist_ok=True)

    job_id = uuid.uuid4()
    zip_path = out_dir / f"{job_id}.zip"

    with get_connection(db_url) as conn:
        with conn.cursor() as cur:
            # Query candidate works not yet reviewed and not reserved in any uncommitted job
            cur.execute(
                """
                SELECT w.id, w.doi, w.openalex_id, w.title, w.abstract, w.publication_year, w.first_author
                FROM works w
                WHERE NOT EXISTS (
                    SELECT 1 FROM paper_reviews pr
                    WHERE pr.work_id = w.id
                      AND pr.study_key = %s
                      AND pr.protocol_sha256 = %s
                )
                AND NOT EXISTS (
                    SELECT 1 FROM review_job_items rji
                    JOIN review_jobs rj ON rj.id = rji.job_id
                    WHERE rji.work_id = w.id
                      AND rj.study_key = %s
                      AND rj.protocol_sha256 = %s
                      AND rj.status IN ('exported', 'repair_required')
                )
                ORDER BY w.created_at ASC, w.id ASC
                LIMIT %s
                FOR UPDATE SKIP LOCKED;
                """,
                (study_key, protocol_sha256, study_key, protocol_sha256, count),
            )
            rows = cur.fetchall()

            if not rows:
                raise RuntimeError("No candidate works available for new screening job.")

            # Insert review_job row
            cur.execute(
                """
                INSERT INTO review_jobs (
                    id, study_key, protocol_text, protocol_sha256,
                    schema_version, status, package_path, created_at
                ) VALUES (%s, %s, %s, %s, '1.0', 'exported', %s, now());
                """,
                (job_id, study_key, protocol_text, protocol_sha256, str(zip_path)),
            )

            # Insert review_job_items rows
            paper_items: list[PaperItem] = []
            work_ids: list[uuid.UUID] = []

            for idx, r in enumerate(rows, start=1):
                p_item = PaperItem(
                    work_id=r["id"],
                    title=r["title"],
                    doi=r["doi"],
                    publication_year=r["publication_year"],
                    abstract=r["abstract"],
                    openalex_id=r["openalex_id"],
                )
                paper_items.append(p_item)
                work_ids.append(r["id"])

                cur.execute(
                    """
                    INSERT INTO review_job_items (job_id, work_id, ordinal, paper_snapshot)
                    VALUES (%s, %s, %s, %s);
                    """,
                    (job_id, r["id"], idx, Jsonb(p_item.to_jsonl_dict())),
                )

        conn.commit()

    # Generate papers.jsonl content
    jsonl_lines = [json.dumps(p.to_jsonl_dict(), ensure_ascii=False) for p in paper_items]
    papers_jsonl_bytes = ("\n".join(jsonl_lines) + "\n").encode("utf-8")
    papers_sha256 = compute_sha256(papers_jsonl_bytes)

    # Generate manifest.json
    manifest = Manifest(
        schema_version="1.0",
        job_id=job_id,
        study_key=study_key,
        protocol_sha256=protocol_sha256,
        paper_count=len(paper_items),
        work_ids=work_ids,
        papers_sha256=papers_sha256,
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    manifest_bytes = json.dumps(manifest.model_dump(mode="json"), indent=2, ensure_ascii=False).encode("utf-8")

    # Load instructions.md and response_schema.json
    instructions_file = CONTRACTS_DIR / "instructions.md"
    instructions_bytes = instructions_file.read_bytes() if instructions_file.is_file() else b"Follow protocol."

    schema_file = CONTRACTS_DIR / "response_schema.json"
    schema_bytes = schema_file.read_bytes() if schema_file.is_file() else b"{}"

    protocol_bytes = protocol_path.read_bytes()

    # Build ZIP archive
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", manifest_bytes)
        zf.writestr("papers.jsonl", papers_jsonl_bytes)
        zf.writestr("research_protocol.md", protocol_bytes)
        zf.writestr("instructions.md", instructions_bytes)
        zf.writestr("response_schema.json", schema_bytes)

    prompt = build_chatgpt_prompt(job_id, len(paper_items))
    return JobExportResult(
        job_id=job_id,
        study_key=study_key,
        protocol_sha256=protocol_sha256,
        paper_count=len(paper_items),
        package_path=zip_path,
        is_reused=False,
        chatgpt_prompt=prompt,
    )


def cancel_job(job_id: uuid.UUID, db_url: str | None = None) -> str:
    """Cancel an active uncommitted job to release its works."""
    with get_connection(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, status FROM review_jobs WHERE id = %s;",
                (job_id,),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError(f"Job {job_id} not found.")

            status = row["status"]
            if status == "committed":
                raise ValueError(f"Job {job_id} is already committed; cannot cancel.")
            if status == "cancelled":
                return "Job was already cancelled."

            cur.execute(
                """
                UPDATE review_jobs
                SET status = 'cancelled'
                WHERE id = %s;
                """,
                (job_id,),
            )
        conn.commit()
    return f"Job {job_id} successfully cancelled."


def get_study_status(
    study_key: str, protocol_path: Path, db_url: str | None = None
) -> dict[str, Any]:
    """Retrieve aggregate status for a study and protocol."""
    if not protocol_path.is_file():
        raise FileNotFoundError(f"Protocol file not found: {protocol_path}")

    protocol_sha256 = compute_file_sha256(protocol_path)

    with get_connection(db_url) as conn:
        with conn.cursor() as cur:
            # Total works in database
            cur.execute("SELECT COUNT(*) AS count FROM works;")
            total_works = cur.fetchone()["count"]

            # Reviews for this study and protocol
            cur.execute(
                """
                SELECT COUNT(*) AS count
                FROM paper_reviews
                WHERE study_key = %s AND protocol_sha256 = %s;
                """,
                (study_key, protocol_sha256),
            )
            committed_reviews = cur.fetchone()["count"]

            # Jobs breakdown
            cur.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM review_jobs
                WHERE study_key = %s AND protocol_sha256 = %s
                GROUP BY status;
                """,
                (study_key, protocol_sha256),
            )
            jobs_by_status = {row["status"]: row["count"] for row in cur.fetchall()}

            # Currently active job
            active = get_active_job(study_key, protocol_sha256, db_url=db_url)

            # Pending works available for new jobs
            cur.execute(
                """
                SELECT COUNT(*) AS count
                FROM works w
                WHERE NOT EXISTS (
                    SELECT 1 FROM paper_reviews pr
                    WHERE pr.work_id = w.id
                      AND pr.study_key = %s
                      AND pr.protocol_sha256 = %s
                )
                AND NOT EXISTS (
                    SELECT 1 FROM review_job_items rji
                    JOIN review_jobs rj ON rj.id = rji.job_id
                    WHERE rji.work_id = w.id
                      AND rj.study_key = %s
                      AND rj.protocol_sha256 = %s
                      AND rj.status IN ('exported', 'repair_required')
                );
                """,
                (study_key, protocol_sha256, study_key, protocol_sha256),
            )
            pending_works = cur.fetchone()["count"]

    return {
        "study_key": study_key,
        "protocol_sha256": protocol_sha256,
        "total_works": total_works,
        "pending_works": pending_works,
        "committed_reviews": committed_reviews,
        "jobs": {
            "exported": jobs_by_status.get("exported", 0),
            "repair_required": jobs_by_status.get("repair_required", 0),
            "committed": jobs_by_status.get("committed", 0),
            "cancelled": jobs_by_status.get("cancelled", 0),
        },
        "active_job": str(active["id"]) if active else None,
        "active_job_status": active["status"] if active else None,
        "active_job_package": active["package_path"] if active else None,
    }
