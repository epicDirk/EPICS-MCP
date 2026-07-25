"""S31: the client-edge guard population is pinned, and the audit's findings live in code.

The clients validate what a foreign service ANSWERED, so an unreadable payload becomes a loud
error instead of a fabricated empty result. ``scripts/guard_audit.py`` audits whether anything
actually observes those checks, in two directions: which tests claim a guard they never execute
(the sham guards of CLAUDE.md point 8), and which guards no test notices the removal of.

An audit is a measurement, and a measurement rots. This module is the part that does not: it
pins the POPULATION the audit covered, so adding, removing or reshaping a client-edge check goes
red with a pointer to re-run the audit, instead of quietly inheriting a verdict that was reached
about different code. It deliberately does NOT re-run the mutation sweep — that needs a coverage
map recorded with ``COVERAGE_CORE=ctrace`` and some ten minutes, which is not a unit test.

Findings of the 2026-07-25 run, kept here rather than in a document nobody reads again:

* Sham guards (direction B): ZERO. 107 tests install a client class double in their own body and
  102 of those never execute a guard line — but that is the double used legitimately, to keep a
  service-layer test off the network. Only two also claimed something about the answered payload,
  and reading both showed they claim service-layer behaviour, not a client-edge check. The
  client-edge guards have their own tests at the transport seam.
* Unobserved polarities (direction A): 19 of 93 targets, plus one where neither polarity is
  noticed and two that no test executes at all. They are declared below.

Honest scope, because the numbers invite over-reading: measured WITHOUT the live lane (the eight
``*_live`` modules, 65 skipped), which is exactly where a guard meets a real payload. And a
surviving mutant is not by itself a defect — it can equally be an equivalent mutant or a guard
masked by its neighbour. ``channelfinder_client.py:91`` is the measured example of the latter:
disable the list check and the loop iterates a dict, whose keys the item check at :98 rejects
anyway, so the whole suite stays green.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "scripts"))

from guard_audit import enumerate_targets  # noqa: E402 - needs the sys.path line above

# (isinstance calls, whole-condition targets) per client module, as the AST sees them. Two
# separate numbers on purpose: a composite condition needs its own mutant, because splicing one
# conjunct leaves the rest of the guard standing.
_GUARD_POPULATION: dict[str, tuple[int, int]] = {
    "alarm_client.py": (6, 1),
    "archiver_client.py": (15, 6),
    "channelfinder_client.py": (16, 5),
    "epics_client.py": (16, 2),
    "naming_client.py": (2, 1),
    "olog_client.py": (19, 4),
}

# Targets whose removal no test noticed, from the 2026-07-25 sweep (COVERAGE_CORE=ctrace map,
# 1444 passed / 65 skipped). Key is ``module:line`` — the finer offset moves with any edit to the
# line, which would make this table rot for a reason that is not a change in the finding.
_UNOBSERVED: dict[str, str] = {
    "alarm_client.py:247": "empty-list fallback; disabling it is not noticed",
    "alarm_client.py:250": "any() filter over the config records",
    "archiver_client.py:152": "sample check, masked by the following 'secs'/'val' membership test",
    "archiver_client.py:497": "history block shape",
    "channelfinder_client.py:91": "list check, masked by the item check at :98",
    "channelfinder_client.py:98": "the dict half of the item check: no NON-dict item is tested",
    "channelfinder_client.py:335": "find_channels list check, masked by its item check at :343",
    "channelfinder_client.py:473": "property name/value pair check (both halves)",
    "channelfinder_client.py:479": "equivalent mutant: a pure mypy narrowing, provably always true",
    "epics_client.py:131": "PVNotFoundError branch of the gather dispatch",
    "epics_client.py:451": "NTNDArray element_count",
    "epics_client.py:461": "int-or-none column coercion",
    "epics_client.py:490": "NTMatrix dim entries",
    "epics_client.py:692": "NaN alarm field",
    "olog_client.py:211": "lenient name filter inside an already-anchored entry",
    "olog_client.py:419": "attachment filename check",
    "olog_client.py:1109": "source default before concatenation",
}

# Neither polarity is noticed, and lines no test executes at all. Both are TEST GAPS at the
# refusal path, not evidence that the check is removable: production input is not test input.
_UNOBSERVED_EITHER_WAY: tuple[str, ...] = ("epics_client.py:133",)
_NEVER_EXECUTED: tuple[str, ...] = ("epics_client.py:135", "epics_client.py:384")

_RERUN = (
    "re-run the audit: COVERAGE_CORE=ctrace COVERAGE_FILE=<scratch>/cov uv run pytest "
    "--cov=src --cov-branch --cov-context=test, then uv run python scripts/guard_audit.py "
    "sweep --coverage-db <scratch>/cov"
)


def test_client_edge_guard_population_is_pinned() -> None:
    """The audited population must still be the population that exists.

    Relational in both directions: a new client module, a new ``isinstance`` check, or a
    condition that gains a conjunct all change these counts, and the verdicts below were reached
    about the old shape. Going red here is the signal to re-measure, not to edit the numbers."""
    actual: dict[str, tuple[int, int]] = {}
    for target in enumerate_targets():
        calls, whole = actual.get(target.module, (0, 0))
        if target.form == "WHOLE-CONDITION":
            actual[target.module] = (calls, whole + 1)
        else:
            actual[target.module] = (calls + 1, whole)
    assert actual == _GUARD_POPULATION, (
        f"the client-edge guard population changed — the recorded audit verdicts were reached "
        f"about a different shape. {_RERUN}"
    )


def test_recorded_audit_findings_still_point_at_a_guard() -> None:
    """Every recorded finding must still name a line that carries a guard.

    Without this the table would quietly become a list of stale line numbers: a refactor moves a
    check, the entry keeps pointing at whatever now sits there, and a future reader treats a
    coincidence as a measurement."""
    live = {f"{target.module}:{target.lineno}" for target in enumerate_targets()}
    recorded = set(_UNOBSERVED) | set(_UNOBSERVED_EITHER_WAY) | set(_NEVER_EXECUTED)
    assert recorded <= live, (
        f"recorded audit findings no longer sit on a guard line: {sorted(recorded - live)}. "
        f"They were measured on an older revision — {_RERUN}"
    )
