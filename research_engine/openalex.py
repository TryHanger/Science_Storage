import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator
import httpx
import psycopg
from psycopg.types.json import Jsonb

from research_engine.config import OPENALEX_API_KEY, OPENALEX_MAILTO
from research_engine.db import get_connection
from research_engine.normalize import (
    extract_first_author,
    normalize_doi,
    normalize_openalex_id,
    reconstruct_abstract,
)


@dataclass
class ImportStats:
    search_run_id: uuid.UUID
    provider: str
    query: str
    target_limit: int
    fetched_count: int
    inserted_count: int
    duplicate_count: int
    status: str
    error_message: str | None = None


def fetch_openalex_works(
    query: str,
    limit: int = 500,
    year_from: int | None = None,
    api_key: str | None = None,
    mailto: str | None = None,
    max_retries: int = 5,
) -> Generator[tuple[dict[str, Any], str | None], None, None]:
    """Fetch works from OpenAlex API using cursor pagination with backoff.
    
    Yields (raw_work_dict, next_cursor).
    """
    key = api_key or OPENALEX_API_KEY
    contact_email = mailto or OPENALEX_MAILTO
    headers = {
        "User-Agent": f"ResearchEngine/0.1 (mailto:{contact_email})",
        "Accept": "application/json",
    }
    base_url = "https://api.openalex.org/works"

    cursor = "*"
    fetched = 0

    with httpx.Client(headers=headers, timeout=30.0) as client:
        while fetched < limit and cursor:
            per_page = min(100, limit - fetched)
            params: dict[str, Any] = {
                "search": query,
                "per_page": per_page,
                "cursor": cursor,
            }
            if year_from:
                params["filter"] = f"publication_year:{year_from}-"
            if key:
                params["api_key"] = key

            # Retry loop with exponential backoff and Retry-After handling
            retries = 0
            backoff = 2.0
            response = None

            while retries <= max_retries:
                try:
                    response = client.get(base_url, params=params)
                    if response.status_code == 200:
                        break
                    elif response.status_code == 429:
                        wait_seconds = backoff
                        retry_after_hdr = response.headers.get("Retry-After")
                        if retry_after_hdr and retry_after_hdr.isdigit():
                            wait_seconds = float(retry_after_hdr) + 1.0
                        else:
                            try:
                                body_json = response.json()
                                if "retryAfter" in body_json:
                                    wait_seconds = float(body_json["retryAfter"]) + 1.0
                            except Exception:
                                pass
                        print(
                            f"[OpenAlex Rate Limit] HTTP 429 received. Backing off for {wait_seconds:.1f}s "
                            f"(attempt {retries + 1}/{max_retries})...",
                            flush=True,
                        )
                        time.sleep(min(wait_seconds, 120.0))
                        backoff = max(backoff * 1.5, wait_seconds + 2.0)
                        retries += 1
                    elif response.status_code in (500, 502, 503, 504):
                        print(
                            f"[OpenAlex Server Error] HTTP {response.status_code}. Retrying in {backoff:.1f}s...",
                            flush=True,
                        )
                        time.sleep(backoff)
                        backoff *= 2.0
                        retries += 1
                    else:
                        response.raise_for_status()
                except (httpx.RequestError, httpx.TimeoutException) as exc:
                    retries += 1
                    if retries > max_retries:
                        raise exc
                    print(f"[OpenAlex Network Issue] {exc}. Retrying in {backoff:.1f}s...", flush=True)
                    time.sleep(backoff)
                    backoff *= 2.0

            if response is None or response.status_code != 200:
                raise RuntimeError(
                    f"OpenAlex API call failed after {max_retries} retries: "
                    f"HTTP {response.status_code if response else 'No Response'}"
                )

            data = response.json()
            results = data.get("results", [])
            meta = data.get("meta", {})
            next_cursor = meta.get("next_cursor")

            if not results:
                break

            for item in results:
                yield item, next_cursor
                fetched += 1
                if fetched >= limit:
                    break

            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor


def import_works_from_openalex(
    query: str,
    limit: int = 500,
    year_from: int | None = None,
    db_url: str | None = None,
    api_key: str | None = None,
    mailto: str | None = None,
) -> ImportStats:
    """Execute search run against OpenAlex and import/deduplicate records."""
    run_id = uuid.uuid4()
    provider = "openalex"
    params = {"limit": limit, "year_from": year_from}

    with get_connection(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO search_runs (id, provider, query, params, status, started_at)
                VALUES (%s, %s, %s, %s, 'running', now());
                """,
                (run_id, provider, query, Jsonb(params)),
            )
        conn.commit()

    fetched_count = 0
    inserted_count = 0
    duplicate_count = 0
    last_cursor = None
    error_msg = None
    final_status = "completed"

    try:
        with get_connection(db_url) as conn:
            with conn.cursor() as cur:
                for raw_work, next_cursor in fetch_openalex_works(
                    query=query,
                    limit=limit,
                    year_from=year_from,
                    api_key=api_key,
                    mailto=mailto,
                ):
                    fetched_count += 1
                    last_cursor = next_cursor

                    raw_id = raw_work.get("id")
                    raw_doi = raw_work.get("doi")
                    norm_doi = normalize_doi(raw_doi)
                    norm_openalex_id = normalize_openalex_id(raw_id)
                    title = raw_work.get("title") or raw_work.get("display_name") or "Untitled"
                    abstract = reconstruct_abstract(raw_work.get("abstract_inverted_index"))
                    year = raw_work.get("publication_year")
                    first_author = extract_first_author(raw_work)
                    provider_record_id = norm_openalex_id or norm_doi or str(raw_id or fetched_count)

                    # Deduplication check
                    existing_work_id = None
                    if norm_doi or norm_openalex_id:
                        cur.execute(
                            """
                            SELECT id FROM works
                            WHERE (doi IS NOT NULL AND doi = %s)
                               OR (openalex_id IS NOT NULL AND openalex_id = %s)
                            LIMIT 1;
                            """,
                            (norm_doi, norm_openalex_id),
                        )
                        row = cur.fetchone()
                        if row:
                            existing_work_id = row["id"]

                    if existing_work_id:
                        work_id = existing_work_id
                        duplicate_count += 1
                        # If existing record is missing abstract or doi, backfill it
                        if abstract or norm_doi:
                            cur.execute(
                                """
                                UPDATE works
                                SET abstract = COALESCE(abstract, %s),
                                    doi = COALESCE(doi, %s),
                                    updated_at = now()
                                WHERE id = %s;
                                """,
                                (abstract, norm_doi, work_id),
                            )
                    else:
                        work_id = uuid.uuid4()
                        cur.execute(
                            """
                            INSERT INTO works (
                                id, doi, openalex_id, title, abstract,
                                publication_year, first_author, created_at, updated_at
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, now(), now());
                            """,
                            (
                                work_id,
                                norm_doi,
                                norm_openalex_id,
                                title,
                                abstract,
                                year,
                                first_author,
                            ),
                        )
                        inserted_count += 1

                    # Record source provenance
                    cur.execute(
                        """
                        INSERT INTO source_records (
                            work_id, search_run_id, provider, provider_record_id, raw, created_at
                        ) VALUES (%s, %s, %s, %s, %s, now())
                        ON CONFLICT (search_run_id, provider, provider_record_id) DO NOTHING;
                        """,
                        (work_id, run_id, provider, provider_record_id, Jsonb(raw_work)),
                    )

            if fetched_count < limit:
                final_status = "completed"
            conn.commit()

    except Exception as exc:
        error_msg = str(exc)
        final_status = "incomplete" if fetched_count > 0 else "failed"

    # Update search_runs record with final status and statistics
    with get_connection(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE search_runs
                SET status = %s,
                    next_cursor = %s,
                    fetched_count = %s,
                    inserted_count = %s,
                    duplicate_count = %s,
                    error_message = %s,
                    finished_at = now()
                WHERE id = %s;
                """,
                (
                    final_status,
                    last_cursor,
                    fetched_count,
                    inserted_count,
                    duplicate_count,
                    error_msg,
                    run_id,
                ),
            )
        conn.commit()

    return ImportStats(
        search_run_id=run_id,
        provider=provider,
        query=query,
        target_limit=limit,
        fetched_count=fetched_count,
        inserted_count=inserted_count,
        duplicate_count=duplicate_count,
        status=final_status,
        error_message=error_msg,
    )


def import_works_from_fixture(
    fixture_path: Path,
    db_url: str | None = None,
    query: str = "fixture_import",
) -> ImportStats:
    """Import works from an offline fixture file (JSON or JSONL)."""
    run_id = uuid.uuid4()
    provider = "offline-fixture"
    params = {"fixture": str(fixture_path)}

    with get_connection(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO search_runs (id, provider, query, params, status, started_at)
                VALUES (%s, %s, %s, %s, 'running', now());
                """,
                (run_id, provider, query, Jsonb(params)),
            )
        conn.commit()

    fetched_count = 0
    inserted_count = 0
    duplicate_count = 0

    items: list[dict[str, Any]] = []
    text = fixture_path.read_text(encoding="utf-8").strip()
    if text.startswith("["):
        items = json.loads(text)
    else:
        for line in text.splitlines():
            line = line.strip()
            if line:
                items.append(json.loads(line))

    with get_connection(db_url) as conn:
        with conn.cursor() as cur:
            for item in items:
                fetched_count += 1
                work_id_str = item.get("work_id")
                work_id = uuid.UUID(work_id_str) if work_id_str else uuid.uuid4()
                doi = normalize_doi(item.get("doi"))
                openalex_id = normalize_openalex_id(item.get("openalex_id"))
                title = item.get("title", "Synthetic title")
                abstract = item.get("abstract")
                year = item.get("publication_year")
                first_author = item.get("first_author")
                provider_record_id = openalex_id or doi or str(work_id)

                # Check existing
                existing_work_id = None
                cur.execute(
                    """
                    SELECT id FROM works
                    WHERE id = %s
                       OR (doi IS NOT NULL AND doi = %s)
                       OR (openalex_id IS NOT NULL AND openalex_id = %s)
                    LIMIT 1;
                    """,
                    (work_id, doi, openalex_id),
                )
                row = cur.fetchone()
                if row:
                    existing_work_id = row["id"]

                if existing_work_id:
                    duplicate_count += 1
                else:
                    cur.execute(
                        """
                        INSERT INTO works (
                            id, doi, openalex_id, title, abstract,
                            publication_year, first_author, created_at, updated_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, now(), now());
                        """,
                        (work_id, doi, openalex_id, title, abstract, year, first_author),
                    )
                    inserted_count += 1

                cur.execute(
                    """
                    INSERT INTO source_records (
                        work_id, search_run_id, provider, provider_record_id, raw, created_at
                    ) VALUES (%s, %s, %s, %s, %s, now())
                    ON CONFLICT (search_run_id, provider, provider_record_id) DO NOTHING;
                    """,
                    (existing_work_id or work_id, run_id, provider, provider_record_id, Jsonb(item)),
                )
            conn.commit()

    with get_connection(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE search_runs
                SET status = 'completed',
                    fetched_count = %s,
                    inserted_count = %s,
                    duplicate_count = %s,
                    finished_at = now()
                WHERE id = %s;
                """,
                (fetched_count, inserted_count, duplicate_count, run_id),
            )
        conn.commit()

    return ImportStats(
        search_run_id=run_id,
        provider=provider,
        query=query,
        target_limit=len(items),
        fetched_count=fetched_count,
        inserted_count=inserted_count,
        duplicate_count=duplicate_count,
        status="completed",
    )
