"""Tests for retrieval path hint heuristics."""

from brain.search.path_hints import derive_path_hints, expand_keywords


def test_health_task_hints_include_main_and_tests():
    hints = derive_path_hints("Fix health check endpoint when database is unavailable")
    assert "test_health" in hints
    assert "apps/api/main" in hints


def test_dto_task_hints_include_models():
    hints = derive_path_hints("Change API response DTO for status endpoints")
    assert "brain/database/models" in hints
    assert "models.py" in hints


def test_branding_task_hints_include_base_template():
    hints = derive_path_hints("Update dashboard HTML branding and base template styling")
    assert "base.html" in hints
    assert "apps/api/templates" in hints


def test_dead_code_task_hints_include_api_main():
    hints = derive_path_hints("Find dead code in the API layer")
    assert "apps/api/main" in hints


def test_expand_keywords_adds_path_hints():
    merged = expand_keywords(["api"], "Improve hybrid context retrieval in context pack builder")
    assert "code_search" in merged
