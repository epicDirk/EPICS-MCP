"""One audit record is one line, on BOTH gates.

The PV gate's half of this is measured end to end in
``tests/test_safety.py::TestAuditSink::test_a_caller_cannot_end_one_record_and_start_a_fabricated_one``,
against the durable file. Here are the three things that test cannot say: what the shared helper
promises on its own, that the OTHER gate goes through it (a separate claim, because the two gates
deliberately share no policy), and that making a record safe did not cost the audit sink the
totality a shipped specification page promises it.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from epics_mcp.audit_record import as_one_record
from epics_mcp.config import EpicsConfig
from epics_mcp.olog_safety import OlogWriteGate
from epics_mcp.safety import SafetyLayer

#: Both gate loggers, because a test that isolates the wrong one passes by collection-order luck.
#: Measured on the first version of this file, which saved and restored ``epics_mcp.audit`` while
#: the gate under test attaches to ``epics_mcp.olog_audit``: the teardown closed a logger the test
#: never used, left the gate's FileHandler open on the other one, and a SECOND run in the same
#: process then read an empty file, because both gates dedup with ``if not audit.handlers``.
_AUDIT_LOGGERS = ("epics_mcp.audit", "epics_mcp.olog_audit")


@contextlib.contextmanager
def _isolated_audit_loggers() -> Iterator[None]:
    """Build a real gate without leaving a handler on a process-global logger.

    The same dance as ``tests/test_doctor.py::_isolated_audit_loggers``, spelled out here rather
    than imported so this module stands on its own; the mechanism it guards against is written up
    there.
    """
    saved = {
        name: (
            logging.getLogger(name).handlers[:],
            logging.getLogger(name).level,
            logging.getLogger(name).propagate,
        )
        for name in _AUDIT_LOGGERS
    }
    for name in saved:
        logging.getLogger(name).handlers.clear()
    try:
        yield
    finally:
        for name, (handlers, level, propagate) in saved.items():
            logger = logging.getLogger(name)
            for handler in logger.handlers[:]:
                handler.close()
            logger.handlers.clear()
            logger.handlers.extend(handlers)
            logger.setLevel(level)
            # ``propagate`` too: one test below switches it off to measure the gate's own sink
            # without pytest's re-raising capture handler, and a leaked False would silence every
            # later test's caplog on this logger.
            logger.propagate = propagate


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # The two byte-oriented record separators, which is where this started.
        ("pv=A\nfake", "pv=A\\x0afake"),
        ("pv=A\r\nfake", "pv=A\\x0d\\x0afake"),
        # The three a byte reader does NOT break on and ``str.splitlines`` does. The first version
        # of the module stopped at C0 plus DEL and left exactly these open, which is why this block
        # exists: ``splitlines`` is the instrument the guards below count with, so a gap here was a
        # gap in the measurement as well as in the record.
        ("pv=A\x85fake", "pv=A\\x85fake"),
        ("pv=A\u2028fake", "pv=A\\u2028fake"),
        ("pv=A\u2029fake", "pv=A\\u2029fake"),
        # A terminal escape: a log is READ as well as written.
        ("pv=A\x1b[2Kfake", "pv=A\\x1b[2Kfake"),
        ("pv=A\x7f", "pv=A\\x7f"),
        # A space separates FIELDS and must survive, or every legitimate record is rewritten.
        ("PV_WRITE event=DENY pv=SIM:PS-01:Cur-RB", "PV_WRITE event=DENY pv=SIM:PS-01:Cur-RB"),
        # Above C1 nothing is touched: real audit lines carry units and accented names.
        ("new=21.5 units=Ω who=josé", "new=21.5 units=Ω who=josé"),
        # Idempotent on what a real record contains, including a repr-escaped value: repr already
        # turned that newline into two harmless characters, and they must not be escaped twice.
        ("new='a\\nb'", "new='a\\nb'"),
        ("", ""),
    ],
)
def test_the_helper_escapes_exactly_what_can_break_a_record(raw: str, expected: str) -> None:
    """Red proof: returning *raw* unchanged fails the first seven rows; narrowing the class back to
    ``[\\x00-\\x1f\\x7f]`` fails the three Unicode-separator rows; escaping the space as well fails
    the field-separator row; widening past C1 fails the units row."""
    assert as_one_record(raw) == expected


def test_no_reader_of_either_family_sees_two_records() -> None:
    """The property behind the table, stated once over both definitions of a line.

    ``splitlines`` is the Unicode-aware family and ``split`` on a newline the byte-oriented one; a
    separator only one of them honours is exactly how the first version of this module passed its
    own tests while the hole was open.
    """
    separators = ("\n", "\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029")
    for separator in separators:
        record = as_one_record(f"pv=A{separator}2026-01-01T00:00:00Z PV_WRITE event=ALLOW pv=B")
        assert len(record.splitlines()) == 1, f"{separator!r} still splits for splitlines()"
        assert len(record.split("\n")) == 1, f"{separator!r} still splits for a byte reader"


def test_the_olog_gate_records_one_line_for_a_newline_bearing_logbook(tmp_path: Path) -> None:
    """The Olog gate's audit fields are caller-chosen too, and its sink is a different function.

    ``logbooks``, ``level`` and the reply target arrive as strings a caller picked, so the same
    forgery applies here: the gate's own docstring calls its record "discrete metadata", which
    bounds what a field MEANS and not what it CONTAINS.

    Red proof: drop ``OneRecordFormatter`` from ``olog_safety._setup_audit_logger`` and the file
    holds two lines, the second a complete fabricated record.
    """
    with _isolated_audit_loggers():
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
        for handler in logging.getLogger("epics_mcp.olog_audit").handlers:
            handler.flush()

        lines = log_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1, f"one verdict must be one record, got {len(lines)}: {lines}"
        assert "\\x0a" in lines[0]


def test_a_value_that_cannot_be_rendered_does_not_replace_the_outcome(tmp_path: Path) -> None:
    """The audit sink is a TOTAL function, and making records safe must not cost that.

    ``docs/write-gate-contract.md`` requires that an audit emission never turns a denial or a
    failure into a crash, and the call sites depend on it: ``tools/write.py`` audits a FAILED put
    and then re-raises the ORIGINAL error, so an exception escaping the sink there would hand the
    caller the audit's problem instead of the write's, and on the ALLOW path it would turn a write
    that DID land into a tool error.

    Measured, and it is why the escaping lives in the formatter rather than at ``_emit``: rendering
    ``message % args`` inside ``_emit`` moved the ``%`` out of ``Handler.emit``, where ``logging``
    absorbs it through ``Handler.handleError``, and this test went red.

    ⚠️ Propagation is switched off for the measurement, and the reason is worth stating because it
    bounds the claim. The record otherwise reaches pytest's own capture handler on the root logger,
    whose ``handleError`` deliberately RE-RAISES so that a broken log call fails a test. That is
    pytest being a strict embedder, not this gate crashing: measured outside pytest, with only the
    gate's own FileHandler attached, the same call returns normally and logging prints its
    "--- Logging error ---" block to stderr. So what is pinned here is the property this repository
    can keep, that the gate's OWN sink absorbs a formatting failure. An embedder that installs a
    re-raising root handler is outside it.

    Red proof: render eagerly in ``SafetyLayer._emit`` and the call below raises ``RuntimeError``
    instead of returning, propagation or no propagation, because the ``%`` then happens before
    ``logging`` is involved at all.
    """

    class Unrenderable:
        def __repr__(self) -> str:
            raise RuntimeError("repr blew up")

    with _isolated_audit_loggers():
        audit = logging.getLogger("epics_mcp.audit")
        audit.propagate = False
        layer = SafetyLayer(EpicsConfig(audit_log_file=str(tmp_path / "audit.log")))
        # Returns rather than raises: the audit line is lost, and logging says so on stderr, but
        # the caller's own outcome is not replaced.
        layer.audit_write_failed("SIM:PS-01:Cur-SP", Unrenderable(), "42", "INTERNAL")
