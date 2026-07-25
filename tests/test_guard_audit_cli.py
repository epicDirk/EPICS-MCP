"""S33: the audit tool's ``--check`` mode is exercised, not just described.

``scripts/guard_audit.py`` produces the figures that ``tests/test_client_edge_guards.py`` records.
Its AST half is imported and therefore covered; its CLI half was not run by anything, so a new
mode there would have shipped on the strength of a commit message. ``CONTRIBUTING.md`` asks for a
test with new behaviour, and this is it.

Both cases run WITHOUT a coverage database, which is the point: recording one costs a full suite
run under ``COVERAGE_CORE=ctrace``, so the cheap half has to be checkable on its own or nobody
will ever check it. They also run IN-PROCESS rather than through a subprocess — a second
interpreter would import a second copy of the module and the substitution below would apply to an
object the code under test never reads, which is precisely the sham this audit exists to find.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "scripts"))

import guard_audit  # noqa: E402 - needs sys.path above

_ARGV = ["guard_audit.py", "sham", "--check"]


def test_check_without_a_database_agrees_and_names_what_it_could_not_reach(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Exit 0 on agreement — and the unreachable pins are named on the CLEAN run too.

    A checker that prints "OK" while half its pins were out of reach reads as a verdict about all
    of them. Naming the gap on every run, not only on failure, is what keeps the clean case from
    being over-read."""
    assert guard_audit.main(_ARGV) == 0
    reported = capsys.readouterr().err
    assert "NOT checked (needs --coverage-db)" in reported
    for pin in guard_audit.PINNED_COVERAGE:
        assert pin in reported, f"a coverage-dependent pin was silently omitted: {pin}"


def test_check_reports_a_deviating_pin_by_name_and_exits_one(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exit 1 means "a pin deviates" — and says WHICH, so the code is never the thing to hunt.

    The exit code alone would be ambiguous: 1 is also what an uncaught exception produces. The
    tool answers that by reserving 3 for a crash; this test pins the other half, that a 1 arrives
    with the deviating pin's name and both figures on stderr."""
    wrong = guard_audit.population()[guard_audit.DOUBLES] + 1
    monkeypatch.setitem(guard_audit.PINNED, guard_audit.DOUBLES, wrong)

    assert guard_audit.main(_ARGV) == 1
    reported = capsys.readouterr().err
    assert "PIN DEVIATION" in reported
    assert guard_audit.DOUBLES in reported
    assert str(wrong) in reported, "the failure must show the pinned figure it disagreed with"
    assert "COVERAGE_CORE=ctrace" in reported, "and how to re-record it"


def test_reporting_mode_still_demands_a_database(capsys: pytest.CaptureFixture[str]) -> None:
    """Without ``--check`` the old contract is untouched, message and exit code included.

    ``--coverage-db`` had to lose ``required=True`` so ``--check`` could run without one. That
    silently turns a clean argparse refusal into a crash unless the refusal is restated by hand,
    so the restatement is pinned here rather than assumed."""
    with pytest.raises(SystemExit) as exit_info:
        guard_audit.main(["guard_audit.py", "sham"])
    assert exit_info.value.code == 2
    assert "the following arguments are required: --coverage-db" in capsys.readouterr().err
