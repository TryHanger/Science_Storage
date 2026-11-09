import hashlib
import re
from pathlib import Path
from typing import Any


def normalize_doi(doi: str | None) -> str | None:
    """Normalize DOI to standard format (lowercase, without resolver URL prefix).
    
    Examples:
        'https://doi.org/10.1016/j.apenergy.2023.121030' -> '10.1016/j.apenergy.2023.121030'
        '10.1016/J.APENERGY.2023.121030' -> '10.1016/j.apenergy.2023.121030'
        None or '' -> None
    """
    if not doi or not isinstance(doi, str):
        return None
    val = doi.strip()
    if not val:
        return None
    # Strip resolver prefixes
    prefixes = [
        "https://doi.org/",
        "http://doi.org/",
        "https://dx.doi.org/",
        "http://dx.doi.org/",
        "doi:",
    ]
    for prefix in prefixes:
        if val.lower().startswith(prefix):
            val = val[len(prefix) :].strip()
            break
    val = val.lstrip("/").strip().lower()
    return val if val else None


def normalize_openalex_id(openalex_id: str | None) -> str | None:
    """Normalize OpenAlex ID to canonical short form (e.g., 'W4361026914').
    
    Examples:
        'https://openalex.org/W4361026914' -> 'W4361026914'
        'w4361026914' -> 'W4361026914'
        None or '' -> None
    """
    if not openalex_id or not isinstance(openalex_id, str):
        return None
    val = openalex_id.strip()
    if not val:
        return None
    prefixes = [
        "https://openalex.org/",
        "http://openalex.org/",
        "openalex.org/",
    ]
    for prefix in prefixes:
        if val.lower().startswith(prefix):
            val = val[len(prefix) :].strip()
            break
    val = val.lstrip("/").strip().upper()
    return val if val else None


def reconstruct_abstract(inverted_index: dict[str, list[int]] | None) -> str | None:
    """Reconstruct plain-text abstract from OpenAlex abstract_inverted_index.
    
    If inverted_index is missing or empty, returns None without inventing text.
    """
    if not inverted_index or not isinstance(inverted_index, dict):
        return None
    position_word: list[tuple[int, str]] = []
    for word, positions in inverted_index.items():
        if isinstance(positions, list):
            for pos in positions:
                if isinstance(pos, int):
                    position_word.append((pos, str(word)))
    if not position_word:
        return None
    position_word.sort(key=lambda x: x[0])
    text = " ".join(word for _, word in position_word).strip()
    return text if text else None


def extract_first_author(work_data: dict[str, Any]) -> str | None:
    """Extract display name or raw name of first author."""
    authorships = work_data.get("authorships")
    if not isinstance(authorships, list) or not authorships:
        return None
    first = authorships[0]
    if not isinstance(first, dict):
        return None
    author_obj = first.get("author")
    if isinstance(author_obj, dict) and author_obj.get("display_name"):
        return str(author_obj["display_name"]).strip() or None
    if first.get("raw_author_name"):
        return str(first["raw_author_name"]).strip() or None
    return None


def compute_sha256(data: str | bytes) -> str:
    """Compute SHA-256 hex digest of string or bytes."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def compute_file_sha256(file_path: Path) -> str:
    """Compute SHA-256 hex digest of file contents (binary mode)."""
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()
