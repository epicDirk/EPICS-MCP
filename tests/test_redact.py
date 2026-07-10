"""Tests for the DS-PRIVACY redaction utilities (services/redact.py)."""

from epics_pv_mcp.services.redact import (
    FREETEXT_WITHHELD,
    project_allowlist,
    redact_record,
    withhold_freetext,
)


def test_project_allowlist_drops_non_allowed_keys() -> None:
    record = {"id": 7, "owner": "a.person", "title": "hi", "level": "Info"}
    result = project_allowlist(record, {"id", "level"})
    assert result == {"id": 7, "level": "Info"}
    assert "owner" not in result  # person field dropped


def test_project_allowlist_drops_unknown_new_field() -> None:
    """An allowlist means a NEW upstream field (not explicitly allowed) is dropped, not leaked."""
    record = {"id": 1, "newPersonField": "b.person"}
    assert project_allowlist(record, {"id"}) == {"id": 1}


def test_withhold_freetext_replaces_value_keeps_key() -> None:
    record = {"id": 1, "title": "seen by a.person", "description": "written by b.person"}
    result = withhold_freetext(record, {"title", "description"})
    assert result["id"] == 1
    assert result["title"] == FREETEXT_WITHHELD
    assert result["description"] == FREETEXT_WITHHELD
    # the free-text content (which named people) is gone; the field's presence remains
    assert "a.person" not in str(result)
    assert "b.person" not in str(result)


def test_redact_record_projects_then_withholds() -> None:
    """A person named in a free-text field (not just in an owner key) must not survive."""
    record = {
        "id": 42,
        "createdDate": "2026-06-01",
        "owner": "a.person",  # person key -> dropped by allowlist
        "logbooks": ["Operations"],
        "title": "Vacuum trip investigated by b.person",  # person in free text -> withheld
        "description": "c.person restarted the IOC",  # person in free text -> withheld
    }
    result = redact_record(
        record,
        allowed={"id", "createdDate", "logbooks", "title", "description"},
        freetext={"title", "description"},
    )
    assert result == {
        "id": 42,
        "createdDate": "2026-06-01",
        "logbooks": ["Operations"],
        "title": FREETEXT_WITHHELD,
        "description": FREETEXT_WITHHELD,
    }
    # No person name — neither from the owner key nor from any free-text field — leaks.
    for name in ("a.person", "b.person", "c.person"):
        assert name not in str(result)


def test_redact_record_freetext_outside_allowlist_is_dropped() -> None:
    """A free-text key not in the allowlist is dropped entirely (stricter than withholding)."""
    record = {"id": 1, "comment": "d.person said ..."}
    result = redact_record(record, allowed={"id"}, freetext={"comment"})
    assert result == {"id": 1}
    assert "d.person" not in str(result)
