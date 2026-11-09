-- Research Engine v0.1: exactly six project tables. UUIDs generated in Python.
CREATE TABLE IF NOT EXISTS works (
    id UUID PRIMARY KEY,
    doi TEXT UNIQUE,
    openalex_id TEXT UNIQUE,
    title TEXT NOT NULL,
    abstract TEXT,
    publication_year INTEGER CHECK (publication_year BETWEEN 1500 AND 2200),
    first_author TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS search_runs (
    id UUID PRIMARY KEY,
    provider TEXT NOT NULL,
    query TEXT NOT NULL,
    params JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed', 'incomplete')),
    next_cursor TEXT,
    fetched_count INTEGER NOT NULL DEFAULT 0 CHECK (fetched_count >= 0),
    inserted_count INTEGER NOT NULL DEFAULT 0 CHECK (inserted_count >= 0),
    duplicate_count INTEGER NOT NULL DEFAULT 0 CHECK (duplicate_count >= 0),
    error_message TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS source_records (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    work_id UUID NOT NULL REFERENCES works(id),
    search_run_id UUID NOT NULL REFERENCES search_runs(id),
    provider TEXT NOT NULL,
    provider_record_id TEXT NOT NULL,
    raw JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (search_run_id, provider, provider_record_id)
);
CREATE INDEX IF NOT EXISTS source_records_work_idx ON source_records(work_id);
CREATE TABLE IF NOT EXISTS review_jobs (
    id UUID PRIMARY KEY,
    study_key TEXT NOT NULL,
    protocol_text TEXT NOT NULL,
    protocol_sha256 CHAR(64) NOT NULL,
    schema_version TEXT NOT NULL DEFAULT '1.0',
    status TEXT NOT NULL CHECK (status IN ('exported', 'repair_required', 'committed', 'cancelled')),
    package_path TEXT NOT NULL,
    accepted_response_sha256 CHAR(64),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    committed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS review_jobs_study_idx ON review_jobs(study_key, protocol_sha256, status);
CREATE TABLE IF NOT EXISTS review_job_items (
    job_id UUID NOT NULL REFERENCES review_jobs(id),
    work_id UUID NOT NULL REFERENCES works(id),
    ordinal INTEGER NOT NULL CHECK (ordinal > 0),
    paper_snapshot JSONB NOT NULL,
    PRIMARY KEY (job_id, work_id),
    UNIQUE (job_id, ordinal)
);
CREATE TABLE IF NOT EXISTS paper_reviews (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id UUID NOT NULL,
    work_id UUID NOT NULL,
    study_key TEXT NOT NULL,
    protocol_sha256 CHAR(64) NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('include', 'exclude', 'uncertain')),
    evidence_level TEXT NOT NULL CHECK (evidence_level IN ('title_only', 'abstract_only')),
    needs_fulltext BOOLEAN NOT NULL,
    reason TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (job_id, work_id),
    FOREIGN KEY (job_id, work_id) REFERENCES review_job_items(job_id, work_id)
);
CREATE INDEX IF NOT EXISTS paper_reviews_study_idx ON paper_reviews(study_key, protocol_sha256);
CREATE INDEX IF NOT EXISTS paper_reviews_work_idx ON paper_reviews(work_id);
