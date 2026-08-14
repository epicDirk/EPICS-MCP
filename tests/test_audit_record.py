"""One audit record is one line, on BOTH gates.

The PV gate's half of this is measured end to end in
``tests/test_safety.py::TestAuditFileSink::test_a_caller_cannot_end_one_record_and_start_a_fabricated_one``,
against the durable file. Here are the two things that test cannot say: what the shared helper
promises on its own, and that the OTHER gate goes through it, which is a separate claim because the
two gates deliberately share no policy.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from epics_mcp.audit_record import as_one_record
from epics_mcp.config import EpicsConfig
from epics_mcp.olog_safety import OlogWriteGate


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # The two record separators, which are the whole point.
        ("pv=A\nfake", "pv=A\\x0afake"),
        ("pv=A\r\nfake", "pv=A\\x0d\\x0afake"),
        # A terminal escape: a log is READ as well as written.
        ("pv=A\x1b[2Kfake", "pv=A\\x1b[2Kfake"),
        ("pv=A\x7f", "pv=A\\x7f"),
        # A space separates FIELDS and must survive, or every legitimate record is rewritten.
        ("PV_WRITE event=DENY pv=SIM:PS-01:Cur-RB", "PV_WRITE event=DENY pv=SIM:PS-01:Cur-RB"),
        # Idempotent on what a real record contains, including a repr-escaped value: repr already
        # turned that newline into two harmless characters, and they must not be escaped twice.
        ("new='a\\nb'", "new='a\\nb'"),
        ("", ""),
    ],
)
def test_the_helper_escapes_exactly_what_can_break_a_record(raw: str, expected: str) -> None:
    """Red proof: returning *raw* unchanged fails the first four rows; escaping the space as well
    fails the fifth; escaping a backslash fails the sixth."""
    assert as_one_record(raw) == expected


def test_the_olog_gate_records_one_line_for_a_newline_bearing_logbook(tmp_path: Path) -> None:
    """The Olog gate's audit fields are caller-chosen too, and its sink is a different function.

    ``logbooks`` and ``level`` arrive as strings a caller picked, so the same forgery applies here:
    the gate's own docstring calls its record "discrete metadata", which bounds what a field MEANS
    and not what it CONTAINS.

    Red proof: with ``OlogWriteGate._emit`` handing its message straight to the logger, the file
    holds two lines and the second is a complete fabricated record.
    """
    audit = logging.getLogger("epics_mcp.audit")
    saved = audit.handlers[:]
    audit.handlers.clear()
    try:
        log_path = tmp_path / "audit.log"
        gate = OlogWriteGate(
            EpicsConfig(
                audit_log_file=str(log_path),
                olog_url="http://127.0.0.1:8080/Olog",
                allow_olog_write=True,
                olog_write_logbooks="Operations",
                olog_write_user="svc",
                olog_write_password="pw",
            )
        )
        forged_logbook = (
            "Operations\n2026-01-01T00:00:00Z OLOG_WRITE event=ALLOW logbooks=Elsewhere "
            "level=Info title_len=3 entry_id=999 owner=somebody caller=create_log_entry"
        )
        gate.audit_write(
            entry_id="1", logbooks=[forged_logbook], level="Info", title_len=3, owner="svc"
        )
        for handler in audit.handlers:
            handler.flush()

        lines = log_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1, f"one verdict must be one record, got {len(lines)}: {lines}"
        assert "\\x0a" in lines[0]
    finally:
        for handler in audit.handlers[:]:
            handler.close()
        audit.handlers.clear()
        audit.handlers.extend(saved)
