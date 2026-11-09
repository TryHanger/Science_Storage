from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, model_validator


class PaperItem(BaseModel):
    """Publication item for papers.jsonl in review package."""
    model_config = ConfigDict(extra="forbid")

    work_id: UUID
    title: str
    doi: str | None = None
    publication_year: int | None = None
    abstract: str | None = None
    openalex_id: str | None = None

    def to_jsonl_dict(self) -> dict[str, Any]:
        return {
            "work_id": str(self.work_id),
            "title": self.title,
            "doi": self.doi,
            "publication_year": self.publication_year,
            "abstract": self.abstract,
            "openalex_id": self.openalex_id,
        }


class Manifest(BaseModel):
    """Manifest for review package zip."""
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    job_id: UUID
    study_key: str
    protocol_sha256: str = Field(..., pattern=r"^[a-f0-9]{64}$")
    paper_count: int = Field(..., ge=1)
    work_ids: list[UUID]
    papers_sha256: str = Field(..., pattern=r"^[a-f0-9]{64}$")
    generated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )


class ReviewResult(BaseModel):
    """Single paper screening result from LLM."""
    model_config = ConfigDict(extra="forbid")

    work_id: UUID
    decision: Literal["include", "exclude", "uncertain"]
    evidence_level: Literal["title_only", "abstract_only"]
    needs_fulltext: bool
    reason: str = Field(..., min_length=12, max_length=800)


class AdditionalReference(BaseModel):
    """Discovered additional reference proposed by LLM."""
    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=5)
    doi: str | None = None
    url: str | None = None
    why_relevant: str = Field(..., min_length=8)

    @model_validator(mode="after")
    def check_doi_or_url(self) -> "AdditionalReference":
        if not self.doi and not self.url:
            raise ValueError("additional_references item must provide at least one of 'doi' or 'url'")
        return self


class ReviewResponse(BaseModel):
    """Screening response matching response_schema.json."""
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    job_id: UUID
    protocol_sha256: str = Field(..., pattern=r"^[a-f0-9]{64}$")
    results: list[ReviewResult] = Field(..., min_length=1)
    additional_references: list[AdditionalReference] = Field(default_factory=list, max_length=30)
