"""Tests for the allowlist projection (services/redact.py)."""

from epics_mcp.services.redact import project_allowlist


def test_project_allowlist_drops_non_allowed_keys() -> None:
    record = {"id": 7, "owner": "a.person", "title": "hi", "level": "Info"}
    result = project_allowlist(record, {"id", "level"})
    assert result == {"id": 7, "level": "Info"}
    assert "owner" not in result  # non-allowlisted field dropped


def test_project_allowlist_drops_unknown_new_field() -> None:
    """An allowlist means a NEW upstream field (not explicitly allowed) is dropped, not leaked."""
    record = {"id": 1, "newPersonField": "b.person"}
    assert project_allowlist(record, {"id"}) == {"id": 1}
