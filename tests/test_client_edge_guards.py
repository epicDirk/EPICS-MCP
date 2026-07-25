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

* Sham guards (direction B): **none found — which is not the same as none there.** 107 tests
  install a client class double in their own body and 102 never execute a guard line; that is the
  double used legitimately, to keep a service-layer test off the network. 20 of those also carry
  payload vocabulary, and the ones read claim SERVICE-layer behaviour (an already-constructed
  exception must not be relabelled "unreachable"), not a client-edge check. ⚠️ Not all 20 were
  read, and the vocabulary filter itself decides who gets read — a first, narrower filter surfaced
  only 2 and a review showed it missed a test whose docstring states the edge claim in words the
  regex did not know. Treat this as "no sham guard found by this filter", and widen the filter
  before treating it as a stronger statement.

  S33, and the distinction matters for what can be checked cheaply: 23 of those 107 carry payload
  vocabulary before any coverage map is consulted, and 20 remain once the tests that DO execute a
  guard line are removed. The 23 follows from this repository's AST alone and is therefore pinned
  by a test in the ordinary gate; the 102 and the 20 are decided by the coverage map and are
  checked only by ``scripts/guard_audit.py sham --check --coverage-db …``.
* Unobserved polarities (direction A): 19 of 93 targets, plus one where neither polarity is
  noticed and two that no test executes at all. They are declared below.

  The sweep counted 19 such targets and the table below has 17 rows, which is not a discrepancy:
  the key is ``module:line``, and one line can carry several targets — ``channelfinder_client.py``
  line 473 carries three (two ``isinstance`` calls plus the whole condition). Measured, those 17
  keys sit on 28 targets in total. A key is not a target, and reading the table as if it were is
  how a reader would conclude that two findings had been lost.
  ⚠️ Two caveats on the counterpart number. First, "observed in both polarities" is weaker than it
  sounds for the 21 RAISE guards: their enabling polarity fires the guard on every input, so every
  covering test dies by construction and only the disabling half carries information. Second,
  three entries below (`alarm_client.py:250`, `epics_client.py:490`, `olog_client.py:211`) sit in
  comprehension filters, where the tool builds no whole-condition target — for those "unobserved"
  means "this CONJUNCT is unobserved", the rest of the condition still stood during the mutant.

Honest scope, because the numbers invite over-reading: measured WITHOUT the live lane (the nine
``*_live`` modules, 65 skipped), which is exactly where a guard meets a real payload. And a
surviving mutant is not by itself a defect — it can equally be an equivalent mutant or a guard
masked by its neighbour. ``channelfinder_client.py:91`` is the measured example of the latter:
disable the list check and the loop iterates a dict, whose keys the item check at :98 rejects
anyway, so the whole suite stays green.
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from collections.abc import Callable
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "scripts"))

from guard_audit import (  # noqa: E402 - needs sys.path above
    DOUBLES,
    EDGE_VOCABULARY,
    PINNED_AST,
    RERUN,
    client_modules,
    enumerate_targets,
    population,
)

from tests.prose_numbers import parse_count  # noqa: E402 - after the sys.path splice above

_TESTS_DIR = Path(__file__).resolve().parent


def _targets_behind_recorded_keys() -> int:
    """How many AST targets the recorded ``module:line`` keys actually cover.

    The number the docstring uses to explain why 19 findings occupy 17 rows. Derived, so the
    explanation cannot become a story: if a line stops carrying several targets, this moves.
    """
    per_key = Counter(f"{target.module}:{target.lineno}" for target in enumerate_targets())
    return sum(per_key[key] for key in _UNOBSERVED)


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
# 1446 passed / 65 skipped). Key is ``module:line`` — the finer offset moves with any edit to the
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

# S33: the figures the docstring above states, each next to the thing that decides it. Only the
# figures a SOURCE-LEVEL measurement can settle are here; 102 and 20 are decided by the coverage
# map and live in ``guard_audit.PINNED_COVERAGE``, checked by ``sham --check`` instead.
_PROSE_FIGURES: tuple[tuple[str, str, Callable[[], int]], ...] = (
    (
        "tests with a client class double",
        r"(\w+) tests\s+install a client class double",
        lambda: population()[DOUBLES],
    ),
    (
        "of those, carrying payload vocabulary",
        r"(\w+) of those 107 carry payload\s+vocabulary",
        lambda: population()[EDGE_VOCABULARY],
    ),
    (
        "audited targets",
        r"of (\w+) targets",
        lambda: len(enumerate_targets()),
    ),
    (
        "rows in the unobserved table",
        r"the table below has (\w+) rows",
        lambda: len(_UNOBSERVED),
    ),
    (
        "targets those rows sit on",
        r"keys sit on (\w+) targets",
        _targets_behind_recorded_keys,
    ),
    (
        "live-lane modules",
        r"the (\w+) ``\*_live`` modules",
        lambda: len(list(_TESTS_DIR.glob("*_live.py"))),
    ),
)


def test_client_edge_guard_population_is_pinned() -> None:
    """The audited population must still be the population that exists.

    Relational in both directions: a new client module, a new ``isinstance`` check, or a
    condition that gains a conjunct all change these counts, and the verdicts below were reached
    about the old shape. Going red here is the signal to re-measure, not to edit the numbers."""
    # Pre-seeded from the module list, not built up from the targets: a NEW client module whose
    # checks are written without ``isinstance`` produces no target at all, so building the dict
    # from targets alone would leave it out of the comparison entirely — the test would stay green
    # while the audited population grew. Measured on a scratch copy before this line existed.
    actual: dict[str, tuple[int, int]] = {path.name: (0, 0) for path in client_modules()}
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


def test_the_ast_derivable_audit_figures_are_pinned() -> None:
    """S33: the half of direction B that costs no coverage run is watched by the ordinary gate.

    This closes the trigger the roadmap names: a NEW test that installs a client class double and
    carries payload vocabulary in its name or docstring moves both figures, and until now moved
    them silently. Going red here is the signal to re-read the audit's verdict, not to edit the
    number — the verdict "no sham guard found" was reached by a human reading a specific list, and
    a longer list has not been read.

    The other two figures (102, 20) are decided by which tests EXECUTED a guard line. That needs a
    coverage map, so they are pinned in the tool and checked deliberately, not here.
    """
    assert population() == PINNED_AST, (
        f"the client-double population changed: measured {population()}, recorded {PINNED_AST}. "
        f"A test was added or its wording changed. {RERUN}"
    )


def test_the_recorded_figures_match_the_prose_that_states_them() -> None:
    """The numbers in this module's own docstring are compared to what produces them.

    An audit is a measurement and a measurement rots; a WRITE-UP of a measurement rots faster,
    because nothing runs it. Every figure below is re-derived here, so the paragraph a future
    reader trusts cannot drift away from the code it describes while every test stays green.
    """
    prose = " ".join((__doc__ or "").split())
    wrong: list[str] = []
    for label, pattern, measure in _PROSE_FIGURES:
        match = re.search(pattern, prose, re.IGNORECASE)
        if match is None:
            wrong.append(
                f"{label}: the sentence stating it is gone; reword the pattern or restore it"
            )
            continue
        stated = parse_count(match.group(1))
        if stated != measure():
            wrong.append(f"{label}: the docstring says {stated}, the code says {measure()}")
    assert not wrong, "this module's own docstring no longer describes the code:\n  " + "\n  ".join(
        wrong
    )
