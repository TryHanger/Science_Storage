from research_engine.normalize import (
    compute_sha256,
    extract_first_author,
    normalize_doi,
    normalize_openalex_id,
    reconstruct_abstract,
)


def test_normalize_doi():
    assert normalize_doi("https://doi.org/10.1016/j.apenergy.2023.121030") == "10.1016/j.apenergy.2023.121030"
    assert normalize_doi("http://dx.doi.org/10.1016/J.APENERGY.2023.121030") == "10.1016/j.apenergy.2023.121030"
    assert normalize_doi("doi:10.1016/test") == "10.1016/test"
    assert normalize_doi("  10.1016/test  ") == "10.1016/test"
    assert normalize_doi("") is None
    assert normalize_doi(None) is None


def test_normalize_openalex_id():
    assert normalize_openalex_id("https://openalex.org/W4361026914") == "W4361026914"
    assert normalize_openalex_id("w4361026914") == "W4361026914"
    assert normalize_openalex_id("  W4361026914  ") == "W4361026914"
    assert normalize_openalex_id("") is None
    assert normalize_openalex_id(None) is None


def test_reconstruct_abstract():
    inv_index = {
        "This": [0],
        "is": [1],
        "a": [2],
        "test": [3],
        "abstract": [4],
    }
    assert reconstruct_abstract(inv_index) == "This is a test abstract"
    assert reconstruct_abstract(None) is None
    assert reconstruct_abstract({}) is None


def test_extract_first_author():
    work_with_display = {
        "authorships": [
            {"author": {"display_name": "Alice Smith"}},
            {"author": {"display_name": "Bob Jones"}},
        ]
    }
    assert extract_first_author(work_with_display) == "Alice Smith"

    work_with_raw = {
        "authorships": [
            {"raw_author_name": "Charlie Brown"}
        ]
    }
    assert extract_first_author(work_with_raw) == "Charlie Brown"

    assert extract_first_author({}) is None
    assert extract_first_author({"authorships": []}) is None


def test_compute_sha256():
    # sha256 of empty bytes
    assert compute_sha256("") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
