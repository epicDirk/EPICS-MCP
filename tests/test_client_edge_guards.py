"""S31: the client-edge guard population is pinned, and the audit's findings live in code.

The clients validate what a foreign service ANSWERED, so an unreadable payload becomes a loud
error instead of a fabricated empty result. ``scripts/guard_audit.py`` audits whether anything
actually observes those checks, in two directions: which tests claim a guard they never execute
(the sham guards of CLAUDE.md point 8), and which guards no test notices the removal of.

An audit is a measurement, and a measurement rots. This module is the part that does not: it
pins the POPULATION the audit covered, so adding, removing or reshaping a client-edge check goes
red with a pointer to re-run the audit, instead of quietly inheriting a verdict that was reached
about different code. It deliberately does NOT re-run the mutation sweep, that needs a coverage
map recorded with ``COVERAGE_CORE=ctrace`` and some ten minutes, which is not a unit test.

Findings of the 2026-07-25 run, kept here rather than in a document nobody reads again:

* Sham guards (direction B): **none found, which is not the same as none there.** 102 tests
  install a client class double in their own body and NOT ONE of them executes a client-edge guard
  line, which is what a class-level double is FOR: it takes the real client off the path. That is
  the double used legitimately, to keep a service-layer test off the network. ⚠️ The 102nd arrived
  with GQ-153 on 2026-08-23, AFTER that sweep, and was held against the same criterion by hand
  rather than by the sweep: it replaces the whole ``ChannelFinderClient`` class so a coverage
  audit can be joined against a known registry offline, and reaches no client-edge line. Every
  further one is in the same position until the sweep is re-run, which is what the guard below
  exists to keep visible. 21 of those also
  carry payload vocabulary, and every one of them was read: they claim SERVICE-layer behaviour (an
  already-constructed exception must not be relabelled "unreachable"; an unknown level is refused
  before any request is built; a plane refused by this command's own read throttle is reported as
  unmeasured rather than as unreachable), not a client-edge check. ⚠️ The vocabulary filter
  itself decides who gets read, a first, narrower filter surfaced only 2 and a review showed it
  missed a test whose docstring states the edge claim in words the regex did not know. Treat this
  as "no sham guard found by this filter", and widen the filter before treating it as a stronger
  statement.

  S33, and the distinction matters for what can be checked cheaply: 21 of those carry payload
  vocabulary before any coverage map is consulted, and 21 remain once the tests that DO execute a
  guard line are removed. The vocabulary figure follows from this repository's AST alone and is
  therefore pinned by a test in the ordinary gate; the two coverage figures are decided by the
  coverage map and are checked only by ``scripts/guard_audit.py sham --check --coverage-db ...``.
  ⚠️ Every figure in this bullet moved on 2026-07-26, and the uniformity above is the RESULT of
  three separate measurement defects being removed, not a change in the code under audit: the
  population read the function's SOURCE TEXT (a docstring quoting the idiom counted, and so did a
  method patch on a helper-installed double), and the coverage matcher compared node ids with
  ``endswith("::" + name)``: file-blind, and unable to match a parametrised id at all. The old
  107 / 102 / 20 said five of the population reached a guard line; four of those five were
  miscounted into the population, and the fifth was credited with a SAME-NAMED test's execution in
  another file.
* Unobserved polarities (direction A): 19 of 96 targets, plus one where neither polarity is
  noticed and two that no test executes at all. They are declared below. (The denominator rose
  from 93 with QA-31, which added three guard sites to ``epics_client``; the findings themselves
  were only RE-LOCATED to their moved lines, so the numerator is unchanged and unreviewed.)

  The sweep counted 19 such targets and the table below has 17 rows, which is not a discrepancy:
  the key is ``module:line``, and one line can carry several targets, eight of those keys do.
  ``channelfinder_client.py`` line 473 carries three: the two ``isinstance`` calls its row calls
  "both halves", plus the whole condition. Measured, those 17 keys sit on 28 targets in total. A
  key is not a target, and reading the table as if it were is how a reader concludes that two
  findings have been lost.
  ⚠️ What is DERIVED here is the 28 and the eight, not the 19. 28 counts every target on those
  lines, observed and unobserved alike, so it shows only that a key CAN carry several findings;
  which two of them share a key is a fact about the sweep, and the sweep's per-key breakdown was
  not recorded. Re-deriving the 19 needs the coverage run.
  ⚠️ Two caveats on the counterpart number. First, "observed in both polarities" is weaker than it
  sounds for the 21 RAISE guards: their enabling polarity fires the guard on every input, so every
  covering test dies by construction and only the disabling half carries information. Second,
  three entries below (`alarm_client.py:247`, `epics_client.py:490`, `olog_client.py:181`) sit in
  comprehension filters, where the tool builds no whole-condition target, for those "unobserved"
  means "this CONJUNCT is unobserved", the rest of the condition still stood during the mutant.

Honest scope, because the numbers invite over-reading: measured WITHOUT the live lane (the twelve
``*_live`` modules; 66 tests skipped at the time of the sweep, when that lane still had nine
modules and no read probe, the remote-https probe was rebuilt into it on 2026-08-29, and the Olog
write pin got its own module on 2026-09-04), which is
exactly where a guard meets a
real payload. And a
surviving mutant is not by itself a defect, it can equally be an equivalent mutant or a guard
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
    PINNED_COVERAGE,
    SHAM_CANDIDATES,
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
    return sum(_targets_per_recorded_key().values())


def _targets_per_recorded_key() -> dict[str, int]:
    per_key = Counter(f"{target.module}:{target.lineno}" for target in enumerate_targets())
    return {key: per_key[key] for key in _UNOBSERVED}


def _keys_with_several_targets() -> int:
    """How many recorded keys carry more than one target, the reason 19 findings fit in 17 rows.

    A sum alone would be invariant: split one line into two and merge another, and the total holds
    while the named example goes false. This counts the keys, so the shape of the explanation is
    watched and not only its arithmetic."""
    return sum(1 for count in _targets_per_recorded_key().values() if count > 1)


# (isinstance calls, whole-condition targets) per client module, as the AST sees them. Two
# separate numbers on purpose: a composite condition needs its own mutant, because splicing one
# conjunct leaves the rest of the guard standing.
_GUARD_POPULATION: dict[str, tuple[int, int]] = {
    "alarm_client.py": (6, 1),
    "archiver_client.py": (15, 6),
    "channelfinder_client.py": (16, 5),
    "epics_client.py": (19, 2),
    "naming_client.py": (2, 1),
    "olog_client.py": (19, 4),
}

# Targets whose removal no test noticed, from the 2026-07-25 sweep (COVERAGE_CORE=ctrace map,
# 1446 passed / 65 skipped). Key is ``module:line``, the finer offset moves with any edit to the
# line, which would make this table rot for a reason that is not a change in the finding.
_UNOBSERVED: dict[str, str] = {
    "alarm_client.py:244": "empty-list fallback; disabling it is not noticed",
    "alarm_client.py:247": "any() filter over the config records",
    "archiver_client.py:152": "sample check, masked by the following 'secs'/'val' membership test",
    "archiver_client.py:497": "history block shape",
    "channelfinder_client.py:96": "list check, masked by the item check at :98",
    "channelfinder_client.py:103": "the dict half of the item check: no NON-dict item is tested",
    "channelfinder_client.py:340": "find_channels list check, masked by its item check at :343",
    "channelfinder_client.py:478": "property name/value pair check (both halves)",
    "channelfinder_client.py:484": "equivalent mutant: a pure mypy narrowing, provably always true",
    # The five epics_client rows moved with the QA-31 edit (+1 above pv_monitor, +100 below it)
    # and were RE-LOCATED, not re-judged: each still names the same guard, verified against the
    # surrounding code. This is the offset rot the comment above predicts, not a new finding.
    # Then +33 more, from ``available_providers``/``effective_provider`` being added above them.
    # RE-LOCATED again, and this time the verification is mechanical rather than by eye: every
    # recorded epics_client line, including the two tuples below, was compared against
    # ``git show HEAD:src/epics_mcp/services/epics_client.py`` at its OLD number, and all eight
    # are byte-identical at old+33. A uniform offset over every row is what an insertion ABOVE
    # all of them produces; a re-judged finding would not move as a block.
    "epics_client.py:165": "PVNotFoundError branch of the gather dispatch",
    "epics_client.py:584": "NTNDArray element_count",
    "epics_client.py:594": "int-or-none column coercion",
    "epics_client.py:623": "NTMatrix dim entries",
    "epics_client.py:825": "NaN alarm field",
    "olog_client.py:183": "lenient name filter inside an already-anchored entry",
    "olog_client.py:391": "attachment filename check",
    # Moved +14 by the OQ11 docstring on ``_expand_log_entry``, then +24 more by OQ12 (the union
    # check in ``add_attachment`` plus its widened docstring, both ABOVE this line), and each time
    # RE-LOCATED, not re-judged: the line still carries the same guard
    # (``source if isinstance(source, str) else ""`` feeding the inline-markup concatenation),
    # verified against the surrounding code AND against ``git show HEAD:...`` at the old number
    # (991 in HEAD and 1015 in the tree are byte-identical). Same offset rot as the epics_client
    # rows above, and the same reason it is a relocation rather than a finding. ⚠ The test below
    # only checks that the key names SOME guard line, so it cannot tell a correct relocation from a
    # coincidence: that comparison is this comment, and it has to be redone by hand whenever the
    # number moves.
    "olog_client.py:1020": "source default before concatenation",
}

# Neither polarity is noticed, and lines no test executes at all. Both are TEST GAPS at the
# refusal path, not evidence that the check is removable: production input is not test input.
_UNOBSERVED_EITHER_WAY: tuple[str, ...] = ("epics_client.py:167",)
_NEVER_EXECUTED: tuple[str, ...] = ("epics_client.py:169", "epics_client.py:517")

_RERUN = (
    "re-run the audit: COVERAGE_CORE=ctrace COVERAGE_FILE=<scratch>/cov uv run pytest "
    "--cov=src --cov-branch --cov-context=test, then uv run python scripts/guard_audit.py "
    "sweep --coverage-db <scratch>/cov"
)

# S33: the figures the docstring above states, each next to the thing that decides it. Only the
# figures a SOURCE-LEVEL measurement can settle are here; the coverage-decided pair lives in
# ``guard_audit.PINNED_COVERAGE`` and is checked by ``sham --check --coverage-db`` instead.
#
# This comment used to name that pair as "102 and 20". They became 103 and 21 in ``9253fc9``, and
# three sentences in this file went on stating the old values in the present tense, inside the
# module whose job is to stop exactly that. They are not named here any more: a figure the tool pins
# does not need a second, unguarded copy in a comment beside it.
_PROSE_FIGURES: tuple[tuple[str, str, Callable[[], int]], ...] = (
    (
        "tests with a client class double",
        r"(\w+) tests\s+install a client class double",
        lambda: population()[DOUBLES],
    ),
    # The pattern must NOT name the population figure. It used to say "of those 107", which is a
    # figure the row above derives: correcting the docstring after the population moved made this
    # pattern MISS and reddened with "the sentence stating it is gone", while leaving the stale
    # number in place kept the file green. The guard rewarded the false prose. Same defect 72e3250
    # removed from "(\w+) of the 17 keys do", two rows down, and left standing here.
    (
        "of those, carrying payload vocabulary",
        r"(\w+) of those carry payload\s+vocabulary",
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
    # The SECOND statement of the row count, and it was unwatched. The row above catches "the table
    # below has N rows"; this sentence restates the same N as "those N keys sit on 28 targets", and
    # nothing compared it, which is why the recipe 72e3250 recorded for exactly this defect ran
    # green again on the shipped code. Two sentences stating one figure need two patterns; a guard
    # over one of them says nothing about the other.
    (
        "keys the targets sit on (restated)",
        r"those (\w+) keys sit on",
        lambda: len(_UNOBSERVED),
    ),
    (
        "targets those rows sit on",
        r"keys sit on (\w+) targets",
        _targets_behind_recorded_keys,
    ),
    (
        "keys carrying more than one target",
        r"(\w+) of those keys do",
        _keys_with_several_targets,
    ),
    # ``re.search`` takes the FIRST occurrence per pattern, so one row per WORDING and not per
    # figure. The caveat two lines below the sentences above restates the 28 twice and the eight
    # once, and the sham bullet restates the vocabulary count, measured, all four could be set to
    # absurd values with the whole suite green. One pattern per wording is the only shape that
    # covers them, which is why these look repetitive: they are not duplicates, they are the other
    # sentences.
    (
        "targets behind the keys (restated in the caveat)",
        r"DERIVED here is the (\w+) and",
        _targets_behind_recorded_keys,
    ),
    (
        "targets behind the keys (restated once more)",
        r"(\w+) counts every target on those lines",
        _targets_behind_recorded_keys,
    ),
    # Anchored on "DERIVED here is", not on the bare ``and the X, not the`` fragment it started as.
    # That fragment is five common tokens with no domain word in it, so any earlier sentence of the
    # same shape, and this docstring is dense in them, would capture the row and leave the caveat
    # watched by nothing. A pattern per WORDING only works if the wording identifies its sentence.
    (
        "keys with several targets (restated in the caveat)",
        r"DERIVED here is the \w+ and the (\w+)",
        _keys_with_several_targets,
    ),
    # The FIFTH restatement, and it is a coverage-decided figure stated in the present tense, the
    # class that put a superseded pair in this file for a day. Compared to the PIN rather than to a
    # fresh sweep, which is all that can be done without a ctrace run, and is what the docstring
    # below says it does.
    (
        "sham candidates (restated in the sham bullet)",
        r"and (\w+) remain once the tests",
        lambda: PINNED_COVERAGE[SHAM_CANDIDATES],
    ),
    (
        "payload vocabulary (restated in the sham bullet)",
        r"(\w+) of those also carry payload",
        lambda: population()[EDGE_VOCABULARY],
    ),
    (
        "either-way findings",
        r"plus (\w+) where neither polarity",
        lambda: len(_UNOBSERVED_EITHER_WAY),
    ),
    ("never-executed findings", r"and (\w+) that no test executes", lambda: len(_NEVER_EXECUTED)),
    (
        "raise guards",
        r"the (\w+) RAISE guards",
        lambda: sum(1 for t in enumerate_targets() if t.form == "RAISE-GUARD"),
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
    # from targets alone would leave it out of the comparison entirely, the test would stay green
    # while the audited population grew. Measured on a scratch copy before this line existed.
    actual: dict[str, tuple[int, int]] = {path.name: (0, 0) for path in client_modules()}
    for target in enumerate_targets():
        calls, whole = actual.get(target.module, (0, 0))
        if target.form == "WHOLE-CONDITION":
            actual[target.module] = (calls, whole + 1)
        else:
            actual[target.module] = (calls + 1, whole)
    assert actual == _GUARD_POPULATION, (
        f"the client-edge guard population changed, the recorded audit verdicts were reached "
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
        f"They were measured on an older revision, {_RERUN}"
    )


def test_the_recorded_figures_match_the_prose_that_states_them() -> None:
    """The numbers in this module's own docstring are compared to what produces them.

    An audit is a measurement and a measurement rots; a WRITE-UP of a measurement rots faster,
    because nothing runs it. Each figure in ``_PROSE_FIGURES`` is re-derived from what produces it.

    NOT everything in that docstring, and the difference is the honest part, stated in tiers
    because merging them is how this docstring came to claim coverage it did not have. It said
    "three tiers" above four of them from the commit that introduced it, so the figure was wrong at
    birth rather than by drift; the count is gone rather than corrected, because nothing here reads
    the list it summarised:

    * RE-MEASURED from the code: the population, the target count, the row count, the eight, the
      RAISE guards, the live modules, and each of their restatements, one row per wording.
    * COMPARED TO A PIN, which proves the prose matches the recorded audit and NOT a fresh sweep:
      the two coverage-decided figures in ``guard_audit.PINNED_COVERAGE``. Reaching them for real
      costs a ``COVERAGE_CORE=ctrace`` run and is ``sham --check --coverage-db``'s job.
    * COMPARED TO A HAND-TYPED TABLE, which proves only that the prose matches the table: the "one"
      and the "two", against ``len(_UNOBSERVED_EITHER_WAY)`` and ``len(_NEVER_EXECUTED)``.
    * COMPARED TO NOTHING AT ALL: the 19 and the "only 2". They are sweep results with no table and
      no pin, written here and nowhere else. Saying otherwise is the failure this tier list exists
      to prevent, and an earlier version of this paragraph did say otherwise.

    Nor is there a completeness pin here: a figure added to that docstring next month ships
    unwatched, exactly as five restatements already did until each got its own row. S32's
    ``test_prose_counters`` has that mechanism; wiring this file into it is separate work.
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
