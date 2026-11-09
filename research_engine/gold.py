import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import psycopg

from research_engine.db import get_connection
from research_engine.normalize import normalize_doi


@dataclass
class GoldEvaluationResult:
    status: str  # 'COMPLETED' or 'NOT RUN'
    message: str
    total_gold: int = 0
    total_relevant: int = 0
    total_matched_in_reviews: int = 0
    true_positives: int = 0
    false_exclusions: list[dict[str, Any]] | None = None
    recall: float = 0.0


def evaluate_gold_set(
    file_path: Path,
    study_key: str,
    db_url: str | None = None,
) -> GoldEvaluationResult:
    """Evaluate screening reviews against a human-annotated gold standard set."""
    if not file_path.is_file():
        return GoldEvaluationResult(
            status="NOT RUN",
            message=f"Gold set file not found: {file_path}",
        )

    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    # Filter out empty or whitespace rows
    valid_rows = [r for r in rows if r.get("work_id_or_doi") and r["work_id_or_doi"].strip()]

    if not valid_rows:
        return GoldEvaluationResult(
            status="NOT RUN",
            message="Gold standard set is empty or contains no labeled entries.",
        )

    # Query all reviews and their corresponding works for this study
    query = """
        SELECT pr.work_id, pr.decision, pr.reason AS actual_reason,
               w.doi, w.title
        FROM paper_reviews pr
        JOIN works w ON w.id = pr.work_id
        WHERE pr.study_key = %s;
    """
    reviews_by_work_id: dict[str, dict[str, Any]] = {}
    reviews_by_doi: dict[str, dict[str, Any]] = {}

    with get_connection(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(query, (study_key,))
            for r in cur.fetchall():
                w_id = str(r["work_id"])
                doi = normalize_doi(r["doi"])
                reviews_by_work_id[w_id] = r
                if doi:
                    reviews_by_doi[doi] = r

    total_gold = len(valid_rows)
    total_relevant = 0
    total_matched = 0
    true_positives = 0
    false_exclusions: list[dict[str, Any]] = []

    for row in valid_rows:
        target = row.get("work_id_or_doi", "").strip()
        expected = row.get("expected_decision", "").strip().lower()
        reason = row.get("reason", "").strip()
        labeler = row.get("labeler", "").strip()

        is_relevant = (expected == "include")
        if is_relevant:
            total_relevant += 1

        # Match against reviews by UUID or normalized DOI
        matched_review = reviews_by_work_id.get(target)
        if not matched_review:
            norm_target_doi = normalize_doi(target)
            if norm_target_doi:
                matched_review = reviews_by_doi.get(norm_target_doi)

        if matched_review:
            total_matched += 1
            actual = matched_review["decision"].lower()
            if is_relevant:
                if actual in ("include", "uncertain"):
                    true_positives += 1
                elif actual == "exclude":
                    false_exclusions.append(
                        {
                            "target": target,
                            "title": matched_review["title"],
                            "expected": expected,
                            "actual": actual,
                            "gold_reason": reason,
                            "system_reason": matched_review["actual_reason"],
                            "labeler": labeler,
                        }
                    )

    recall = (true_positives / total_relevant) if total_relevant > 0 else 0.0

    return GoldEvaluationResult(
        status="COMPLETED",
        message=f"Evaluated {total_matched}/{total_gold} gold items against study '{study_key}'.",
        total_gold=total_gold,
        total_relevant=total_relevant,
        total_matched_in_reviews=total_matched,
        true_positives=true_positives,
        false_exclusions=false_exclusions,
        recall=recall,
    )
