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

* Sham guards (direction B): **none found, which is not the same as none there.** 286 tests
  install a client class double, in their own body or, since GQ-403 on 2026-09-14, through a
  helper, a fixture or an autouse fixture; until then only the body counted. A class-level double
  takes the real client off the path, which is what it is FOR: the double used legitimately, to
  keep a service-layer test off the network. A few of those tests still execute a client-edge guard
  line through module-level code of the doubled client's module; ``guard_audit.PINNED_COVERAGE``
  records the figures that follow from it. ⚠️ Two groups arrived AFTER the 2026-07-25 sweep and
  were held against the criterion by reading and by the coverage map rather than by the sweep: the
  GQ-153 test of 2026-08-23, which replaces the whole ``ChannelFinderClient`` class so a coverage
  audit can be joined against a known registry offline, and every test the GQ-403 widening added.
  They stay in that position until the sweep is re-run, which is what the guard below exists to
  keep visible. 91 of those also carry payload vocabulary, and every one that executes no guard
  line was read (the first set on 2026-07-25, the two BG-DTHR tests on 2026-08-19, the GQ-403
  additions on 2026-09-14; each group's reading is a comment in ``PINNED_CANDIDATES``): they claim
  SERVICE-layer or doctor-level behaviour (an already-constructed exception must not be relabelled
  "unreachable"; an unknown level is refused before any request is built; a plane refused by this
  command's own read throttle is reported as unmeasured rather than as unreachable; a doctor
  identity probe that met an unreadable body stays unverified), not a client-edge check.
  ⚠️ The vocabulary filter itself decides who gets read, a first, narrower filter surfaced only 2
  and a review showed it
  missed a test whose docstring states the edge claim in words the regex did not know. Treat this
  as "no sham guard found by this filter", and widen the filter before treating it as a stronger
  statement.

  S33, and the distinction matters for what can be checked cheaply: 91 of those carry payload
  vocabulary before any coverage map is consulted, and 85 remain once the tests that DO execute a
  guard line are removed. The vocabulary figure follows from this repository's AST alone and is
  therefore pinned by a test in the ordinary gate; the two coverage figures are decided by the
  coverage map and are checked only by ``scripts/guard_audit.py sham --check --coverage-db ...``.
  ⚠️ The figures this bullet carried moved on 2026-07-26, and again with GQ-403 on 2026-09-14
  for the reason given above (the history is in the comments of ``guard_audit.PINNED_AST``). The
  uniformity they showed after the first move, the same figure before and after the map, was the
  RESULT of
  three separate measurement defects being removed, not a change in the code under audit: the
  population read the function's SOURCE TEXT (a docstring quoting the idiom counted, and so did a
  method patch on a helper-installed double), and the coverage matcher compared node ids with
  ``endswith("::" + name)``: file-blind, and unable to match a parametrised id at all. The old
  107 / 102 / 20 said five of the population reached a guard line; four of those five were
  miscounted into the population, and the fifth was credited with a SAME-NAMED test's execution in
  another file.
* Unobserved polarities (direction A): 19 of 98 targets, plus one where neither polarity is
  noticed and two that no test executes at all. They are declared below. (The denominator rose
  from 93 with QA-31, which added three guard sites to ``epics_client``; the findings themselves
  were only RE-LOCATED to their moved lines, so the numerator is unchanged and unreviewed. It rose
  again to 98 with GQ-290, which added the bare-``[]`` branch to ``archiver_client``: that site is
  observed in both polarities, so it lands in the denominator only.)

  The sweep counted 19 such targets and the table below has 16 rows, which is not a discrepancy:
  the key is ``module:line``, and one line can carry several targets, seven of those keys do.
  ``channelfinder_client.py`` line 473 carries three: the two ``isinstance`` calls its row calls
  "both halves", plus the whole condition. Measured, those 16 keys sit on 25 targets in total. A
  key is not a target, and reading the table as if it were is how a reader concludes that two
  findings have been lost.
  ⚠️ What is DERIVED here is the 25 and the seven, not the 19. 25 counts every target on those
  lines, observed and unobserved alike, so it shows only that a key CAN carry several findings;
  which two of them share a key is a fact about the sweep, and the sweep's per-key breakdown was
  not recorded. Re-deriving the 19 needs the coverage run.
  ⚠️ The rows went 17 -> 16 and the derived pair 28 -> 25 and eight -> seven with GQ-290, which
  RE-JUDGED the ``archiver_client`` history-block row out of the table (its reasoning sits at that
  row's former place below). The 19 does NOT move with it: it is the sweep's own number, and this
  file cannot re-derive it without the coverage run. So the numerator now names one finding whose
  row is gone, which is honest and unavoidable until the next sweep, and better than silently
  decrementing a figure this file did not measure.
  ⚠️ Two caveats on the counterpart number. First, "observed in both polarities" is weaker than it
  sounds for the 21 RAISE guards: their enabling polarity fires the guard on every input, so every
  covering test dies by construction and only the disabling half carries information. Second,
  three entries below (`alarm_client.py:247`, `epics_client.py:661`, `olog_client.py:183`) sit in
  comprehension filters, where the tool builds no whole-condition target, for those "unobserved"
  means "this CONJUNCT is unobserved", the rest of the condition still stood during the mutant.
  ⚠️ Those two numbers were `490` and `181` until GQ-276 and had rotted where the table below had
  not: prose carries no key a guard can check, so nothing went red while `490` drifted onto a
  blank line. They are the table's own rows, spelled again here, and they move with it.

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
from dataclasses import dataclass
from functools import cache
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

from tests.prose_numbers import (  # noqa: E402 - after the sys.path splice above
    ProseBlock,
    ProseSite,
    iter_blocks,
    iter_sites,
    parse_count,
)

_TESTS_DIR = Path(__file__).resolve().parent
_THIS_FILE = Path(__file__).resolve()
_THIS_LABEL = "tests/test_client_edge_guards.py"


def _targets_behind_recorded_keys() -> int:
    """How many AST targets the recorded ``module:line`` keys actually cover.

    The number the docstring uses to explain why 19 findings occupy 16 rows. Derived, so the
    explanation cannot become a story: if a line stops carrying several targets, this moves.
    """
    return sum(_targets_per_recorded_key().values())


def _targets_per_recorded_key() -> dict[str, int]:
    per_key = Counter(f"{target.module}:{target.lineno}" for target in enumerate_targets())
    return {key: per_key[key] for key in _UNOBSERVED}


def _keys_with_several_targets() -> int:
    """How many recorded keys carry more than one target, the reason 19 findings fit in 16 rows.

    A sum alone would be invariant: split one line into two and merge another, and the total holds
    while the named example goes false. This counts the keys, so the shape of the explanation is
    watched and not only its arithmetic."""
    return sum(1 for count in _targets_per_recorded_key().values() if count > 1)


# (isinstance calls, whole-condition targets) per client module, as the AST sees them. Two
# separate numbers on purpose: a composite condition needs its own mutant, because splicing one
# conjunct leaves the rest of the guard standing.
_GUARD_POPULATION: dict[str, tuple[int, int]] = {
    "alarm_client.py": (6, 1),
    # 15/6 -> 16/7 with GQ-290: the bare-[] branch in get_pv_history adds one isinstance call and
    # one whole condition. A FORM change, not a verdict change: the new site is observed in both
    # polarities, so it belongs in no _UNOBSERVED row. Measured rather than argued, branch
    # coverage of tests/test_archiver.py over this module on 2026-09-06 takes both arcs off that
    # line and leaves the file at 0 missing branches.
    "archiver_client.py": (16, 7),
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
    # RE-LOCATED +5 by the widened HistoryResult docstring in GQ-290 (byte-identical against
    # ``git show 39c276d:...`` at its old number 152), then +3 more by that change's post-build
    # review, which qualified the empty-branch comment and the status docstring
    # (``git show f8cdaa2:...`` at 157).
    "archiver_client.py:160": "sample check, masked by the following 'secs'/'val' membership test",
    # ⛔ ``archiver_client.py:497`` ("history block shape") is GONE from this table, and it is the
    # one row here that was RE-JUDGED rather than re-located. GQ-290 split its middle conjunct out
    # (the bare ``[]`` now means empty, not unreadable), so the old line has NO byte-identical
    # counterpart in the tree: the guard itself changed, and a verdict measured about the
    # three-conjunct version says nothing about the two-conjunct one.
    # Re-measured with the same instrument the 2026-07-25 sweep used, a mutant: removing the whole
    # condition now takes FOUR tests down (the four unexpected_payload rows), so the guard is
    # observed and no longer belongs in a table of findings nobody noticed. Two of those four rows
    # are new, added in the same change precisely because a mutant survived without them.
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
    # recorded epics_client line, including the two tuples below, was compared against the OLD
    # blob at its OLD number, and all eight were byte-identical at old+33.
    # Then +30 more with GQ-276, which put ``reset_context`` and a widened ``get_context``
    # docstring ABOVE every one of them, and +8 more when that change's own post-build review
    # wrapped the close in a try/finally and explained it. Verified the same mechanical way,
    # against the blob and not against a branch:
    #     git show 5bd6b7f:src/epics_mcp/services/epics_client.py   (for the +30 step)
    #     git show f8cdaa2:src/epics_mcp/services/epics_client.py   (for the +8 step)
    # all eight are byte-identical at each step.
    # ⚠ TWO steps inside one working session, which is this row's recurring lesson stated once
    # more: the offset is measured at the END of a change, and a review that touches the file
    # again starts a new change. A uniform offset over every row is what an insertion
    # ABOVE all of them produces; a re-judged finding would not move as a block.
    # ⭐ And this time the coincidence the note below warns about was RULED OUT rather than
    # assumed: +30 is the ONLY offset in -5..+59 under which all eight lines match their old
    # bytes, so no other relocation is consistent with the evidence. Re-running that sweep is the
    # cheapest way to redo this check after the next move.
    "epics_client.py:203": "PVNotFoundError branch of the gather dispatch",
    "epics_client.py:622": "NTNDArray element_count",
    "epics_client.py:632": "int-or-none column coercion",
    "epics_client.py:661": "NTMatrix dim entries",
    "epics_client.py:863": "NaN alarm field",
    "olog_client.py:183": "lenient name filter inside an already-anchored entry",
    "olog_client.py:391": "attachment filename check",
    # Moved +14 by the OQ11 docstring on ``_expand_log_entry``, then +24 more by OQ12 (the union
    # check in ``add_attachment`` plus its widened docstring), then +55 by GQ-297 (the hit-count
    # ceiling constant, ``hit_count_is_capped`` and two widened docstrings) - every one of them
    # ABOVE this line - and each time RE-LOCATED, not re-judged: the line still carries the same
    # guard (``source if isinstance(source, str) else ""`` feeding the inline-markup
    # concatenation), verified against the surrounding code AND against the blob at the old number:
    # measured 2026-09-05, ``git show 0d62eb5:...`` line 1020 and the tree's line 1075 are
    # byte-identical, and the new line carries the same SINGLE target the old one did, so
    # ``_targets_behind_recorded_keys`` and ``_keys_with_several_targets`` both hold.
    # ⛔ The COMMIT ID is deliberate and it is the second half of this row's own lesson. The pair
    # named here before GQ-297 read "991 in HEAD and 1015 in the tree", and both halves had gone
    # stale: 991 names a different statement entirely, 1015 a string fragment. It rotted because
    # ``HEAD`` MOVES: the recipe stops reproducing the moment the commit that fixed the row lands,
    # so the next reader runs it, gets a mismatch, and reads a correct relocation as a defect.
    # Pin the blob, never the branch. ⛔ And the offset itself moved TWICE inside GQ-297, +40 at
    # the build and +55 after its own QA restored a source pointer above this line, which is why
    # this number is re-measured at the END of a change and not in the middle of one.
    # ⚠ The test below only checks that the key names SOME guard
    # line, so it cannot tell a correct relocation from a coincidence: that comparison is this
    # comment, and it has to be redone by hand.
    "olog_client.py:1075": "source default before concatenation",
}

# Neither polarity is noticed, and lines no test executes at all. Both are TEST GAPS at the
# refusal path, not evidence that the check is removable: production input is not test input.
_UNOBSERVED_EITHER_WAY: tuple[str, ...] = ("epics_client.py:205",)
_NEVER_EXECUTED: tuple[str, ...] = ("epics_client.py:207", "epics_client.py:555")

_RERUN = (
    "re-run the audit: COVERAGE_CORE=ctrace COVERAGE_FILE=<scratch>/cov uv run pytest "
    "--cov=src --cov-branch --cov-context=test, then uv run python scripts/guard_audit.py "
    "sweep --coverage-db <scratch>/cov"
)


@dataclass(frozen=True)
class _Figure:
    """A number a docstring of this file states, the measure that decides it, and WHERE it is read.

    ``scope`` names the docstring: ``<module>`` for the module docstring, otherwise the name of a
    function in this module, which is how the two helper docstrings above and the docstring of the
    figures test got their own rows instead of staying unread. A figure is read in the docstring
    paragraphs of its scope only, never in a comment: comments quote retired patterns and retired
    values on purpose, and a figure that matched there would either alarm on a quotation or, worse,
    declare a comment's number derived when nothing compares it.
    """

    label: str
    pattern: str
    measure: Callable[[], int]
    scope: str = "<module>"


# S33: the figures the docstring above states, each next to the thing that decides it. Only the
# figures a SOURCE-LEVEL measurement can settle are here; the coverage-decided pair lives in
# ``guard_audit.PINNED_COVERAGE`` and is checked by ``sham --check --coverage-db`` instead.
#
# This comment used to name that pair as "102 and 20". They became 103 and 21 in ``9253fc9``, and
# three sentences in this file went on stating the old values in the present tense, inside the
# module whose job is to stop exactly that. They are not named here any more: a figure the tool pins
# does not need a second, unguarded copy in a comment beside it.
_PROSE_FIGURES: tuple[_Figure, ...] = (
    _Figure(
        "tests with a client class double",
        r"(\w+) tests\s+install a client class double",
        lambda: population()[DOUBLES],
    ),
    # The pattern must NOT name the population figure. It used to say "of those 107", which is a
    # figure the row above derives: correcting the docstring after the population moved made this
    # pattern MISS and reddened with "the sentence stating it is gone", while leaving the stale
    # number in place kept the file green. The guard rewarded the false prose. Same defect 72e3250
    # removed from "(\w+) of the 17 keys do", the row for keys carrying more than one target,
    # and left standing here.
    _Figure(
        "of those, carrying payload vocabulary",
        r"(\w+) of those carry payload\s+vocabulary",
        lambda: population()[EDGE_VOCABULARY],
    ),
    _Figure(
        "audited targets",
        r"of (\w+) targets",
        lambda: len(enumerate_targets()),
    ),
    _Figure(
        "rows in the unobserved table",
        r"the table below has (\w+) rows",
        lambda: len(_UNOBSERVED),
    ),
    # The SECOND statement of the row count, and it was unwatched. The row above catches "the table
    # below has N rows"; this sentence restates the same N as "those N keys sit on M targets", and
    # nothing compared it, which is why the recipe 72e3250 recorded for exactly this defect ran
    # green again on the shipped code. Two sentences stating one figure need two patterns; a guard
    # over one of them says nothing about the other.
    _Figure(
        "keys the targets sit on (restated)",
        r"those (\w+) keys sit on",
        lambda: len(_UNOBSERVED),
    ),
    _Figure(
        "targets those rows sit on",
        r"keys sit on (\w+) targets",
        _targets_behind_recorded_keys,
    ),
    _Figure(
        "keys carrying more than one target",
        r"(\w+) of those keys do",
        _keys_with_several_targets,
    ),
    # Every occurrence of a pattern is read (``_figure_hits`` uses ``finditer``), so a sentence
    # repeated verbatim is compared as often as it appears; a DIFFERENT wording of the same figure
    # still needs its own row. The caveat below the sentences above restates the target total
    # twice and the multi-key count once, and the sham bullet restates the vocabulary count;
    # measured, all four could be set to absurd values with the whole suite green until each got
    # a row. One pattern per wording is the only shape that covers them, which is why these look
    # repetitive: they are not duplicates, they are the other sentences.
    _Figure(
        "targets behind the keys (restated in the caveat)",
        r"DERIVED here is the (\w+) and",
        _targets_behind_recorded_keys,
    ),
    _Figure(
        "targets behind the keys (restated once more)",
        r"(\w+) counts every target on those lines",
        _targets_behind_recorded_keys,
    ),
    # Anchored on "DERIVED here is", not on the bare ``and the X, not the`` fragment it started as.
    # That fragment is five common tokens with no domain word in it, so any earlier sentence of the
    # same shape, and this docstring is dense in them, would capture the row and leave the caveat
    # watched by nothing. A pattern per WORDING only works if the wording identifies its sentence.
    _Figure(
        "keys with several targets (restated in the caveat)",
        r"DERIVED here is the \w+ and the (\w+)",
        _keys_with_several_targets,
    ),
    # The FIFTH restatement, and it is a coverage-decided figure stated in the present tense, the
    # class that put a superseded pair in this file for a day. Compared to the PIN rather than to a
    # fresh sweep, which is all that can be done without a ctrace run, and is what the docstring
    # below says it does.
    _Figure(
        "sham candidates (restated in the sham bullet)",
        r"and (\w+) remain once the tests",
        lambda: PINNED_COVERAGE[SHAM_CANDIDATES],
    ),
    _Figure(
        "payload vocabulary (restated in the sham bullet)",
        r"(\w+) of those also carry payload",
        lambda: population()[EDGE_VOCABULARY],
    ),
    _Figure(
        "either-way findings",
        r"plus (\w+) where neither polarity",
        lambda: len(_UNOBSERVED_EITHER_WAY),
    ),
    _Figure(
        "never-executed findings",
        r"and (\w+) that no test executes",
        lambda: len(_NEVER_EXECUTED),
    ),
    _Figure(
        "raise guards",
        r"the (\w+) RAISE guards",
        lambda: sum(1 for t in enumerate_targets() if t.form == "RAISE-GUARD"),
    ),
    _Figure(
        "live-lane modules",
        r"the (\w+) ``\*_live`` modules",
        lambda: len(list(_TESTS_DIR.glob("*_live.py"))),
    ),
    # The rows below read docstrings OTHER than the module's. They exist because GQ-290 dropped a
    # row from the table and the pair of helper docstrings, plus the docstring of the figures test,
    # went on stating the old figures for ten days: the value test read only the module docstring,
    # so nothing could go red there.
    _Figure(
        "rows the findings occupy (helper docstring)",
        r"occupy (\w+) rows",
        lambda: len(_UNOBSERVED),
        scope="_targets_behind_recorded_keys",
    ),
    _Figure(
        "rows the findings fit in (helper docstring)",
        r"fit in (\w+) rows",
        lambda: len(_UNOBSERVED),
        scope="_keys_with_several_targets",
    ),
    _Figure(
        "keys with several targets (figures test docstring)",
        r"the row count, the (\w+), the RAISE",
        _keys_with_several_targets,
        scope="test_the_recorded_figures_match_the_prose_that_states_them",
    ),
)


# --- the completeness pin: what the S32 detector finds in THIS file, and what reads it ---


@cache
def _own_blocks() -> tuple[ProseBlock, ...]:
    """Every docstring paragraph and comment run of this file, as the S32 detector cuts them."""
    return iter_blocks(((_THIS_LABEL, _THIS_FILE),))


@cache
def _own_sites() -> tuple[ProseSite, ...]:
    """Every number inside a size-naming phrase of this file, by the S32 detector's own rules."""
    return iter_sites(_own_blocks())


@cache
def _flattened_doc(scope: str) -> str:
    """The docstring of *scope*, whitespace-normalised the way the detector flattens a paragraph."""
    if scope == "<module>":
        doc = __doc__
    else:
        owner = globals().get(scope)
        assert owner is not None, (
            f"a prose block sits in scope {scope!r}, which this module cannot resolve to a "
            "function; nested or class-level docstrings need the lookup widened before they can "
            "be read"
        )
        doc = owner.__doc__
    return " ".join((doc or "").split())


def _is_docstring_block(block: ProseBlock) -> bool:
    """A block is a docstring paragraph when its text sits inside the docstring of its scope.

    The detector keys comment runs and docstring paragraphs by the same qualname, so the block
    alone cannot say which it is; the docstring can. Normalised on both sides, because the detector
    keeps a double space inside a line while ``str.split`` does not."""
    return " ".join(block.text.split()) in _flattened_doc(block.qualname)


def _figure_hits(figure: _Figure) -> list[tuple[ProseBlock, re.Match[str]]]:
    """Every match of *figure* in the docstring paragraphs of its scope, comments excluded."""
    hits: list[tuple[ProseBlock, re.Match[str]]] = []
    for block in _own_blocks():
        if block.qualname != figure.scope or not _is_docstring_block(block):
            continue
        hits.extend(
            (block, match) for match in re.finditer(figure.pattern, block.text, re.IGNORECASE)
        )
    return hits


def _site_kind(site: ProseSite) -> str:
    return "docstring" if _is_docstring_block(site.block) else "comment"


def _site_key(site: ProseSite) -> tuple[str, str, str, int]:
    """Block kind, scope, phrase, value: the identity a row in ``_UNDERIVED`` is keyed by.

    The kind is part of the key on purpose. Without it a sentence that leaves the module docstring
    for a module-level comment keeps its key, because the detector gives both the qualname
    ``<module>``, and the move is exactly the kind of silent demotion the pin exists to notice."""
    return (_site_kind(site), site.block.qualname, site.snippet, site.value)


def _is_derived(site: ProseSite) -> bool:
    """Derived means the number sits in the CAPTURE GROUP of a figure read in that very block.

    The group, not the match: the S32 guard measured that a number inside a claim's span but outside
    its group is as unwatched as one nobody matched. And the same block: a figure is read in the
    docstring paragraphs of its scope, so a comment's number is never derived, whatever it says."""
    if not _is_docstring_block(site.block):
        return False
    return any(
        block == site.block and match.start(1) <= site.offset < match.end(1)
        for figure in _PROSE_FIGURES
        if figure.scope == site.block.qualname
        for block, match in _figure_hits(figure)
    )


def _figures_without_site() -> tuple[frozenset[str], list[str]]:
    """The figures none of whose hits carries a detector site, and the figures with mixed hits.

    A figure with no hit at all is left to the value test, which reports it as gone."""
    without: set[str] = set()
    mixed: list[str] = []
    for figure in _PROSE_FIGURES:
        hits = _figure_hits(figure)
        if not hits:
            continue
        carries_site = [
            any(
                site.block == block and match.start(1) <= site.offset < match.end(1)
                for site in _own_sites()
            )
            for block, match in hits
        ]
        if all(carries_site):
            continue
        if any(carries_site):
            mixed.append(figure.label)
        else:
            without.add(figure.label)
    return frozenset(without), mixed


# Numbers the detector finds in this file that NO figure derives, each with the reason. Keyed by
# (block kind, scope, phrase, value), never by line number, which moves with any edit above it. The
# rule for a row is the S32 guard's: it pins that the phrase EXISTS, never that its value is right,
# and a reason has to say why no measure can settle the value.
_UNDERIVED: dict[tuple[str, str, str, int], str] = {
    # --- the module docstring ---------------------------------------------------------------------
    ("docstring", "<module>", "8), and which guards", 8): (
        "the marker of CLAUDE.md point 8, paired by the detector with the noun of the next clause; "
        "not a count"
    ),
    ("docstring", "<module>", "two bg-dthr tests", 2): (
        "which tests were read by hand on 2026-08-19; a record of that reading, no constant "
        "holds it"
    ),
    ("docstring", "<module>", "four of those five", 4): (
        "the history of the retired population pair: how many of its five were miscounted; past "
        "tense about a measurement that no longer exists"
    ),
    ("docstring", "<module>", "four of those five", 5): (
        "the same retired history, its other number"
    ),
    ("docstring", "<module>", "three entries", 3): (
        "the recorded keys sitting in comprehension filters, named inline right after the "
        "sentence; the audit records no form that would let a measure count them"
    ),
    ("docstring", "<module>", "66 tests", 66): (
        "the live lane's skip count at the time of the 2026-07-25 sweep; historical, the current "
        "count needs a pytest run and is no figure of this file"
    ),
    ("docstring", "<module>", "nine modules", 9): (
        "how many live modules the lane had at the time of that sweep; historical, the current "
        "count is the live-lane modules figure"
    ),
    # --- the comments above the recorded findings -------------------------------------------------
    ("comment", "<module>", "four tests", 4): (
        "how many tests the GQ-290 mutant took down on 2026-09-06; a recorded run, a comment, and "
        "figures read docstrings only"
    ),
    ("comment", "<module>", "four unexpected_payload rows", 4): (
        "the same recorded run, naming the rows that went red"
    ),
    ("comment", "<module>", "two of those four", 2): (
        "the same recorded run, how many of those rows were new"
    ),
    ("comment", "<module>", "four rows", 4): (
        "the same recorded run, restated with the noun the detector pairs first"
    ),
    ("comment", "<module>", "five epics_client rows", 5): (
        "derivable, the epics_client keys of _UNOBSERVED, but a comment: figures read docstrings, "
        "so the phrase is pinned to exist and its value is not compared"
    ),
    ("comment", "<module>", "17 keys", 17): (
        "quotes the retired pattern of 72e3250 with the value the table had then; past tense"
    ),
}

# The figures whose captured number the S32 detector does not see as a site, so the site counts
# below say nothing about them and a stale value there is caught by the value test alone. Measured,
# the reasons are the detector's own limits: a noun outside prose_numbers.COLLECTION_NOUNS
# (targets, vocabulary, polarity), a bare number with no noun (the caveat), a singular where the
# list holds the plural (test), and a noun further than the detector's gap allows (remain once the
# tests). A figure listed here that gains a site, or one missing here that has none, goes red.
_FIGURES_WITHOUT_SITE: frozenset[str] = frozenset(
    {
        "of those, carrying payload vocabulary",
        "audited targets",
        "targets those rows sit on",
        "targets behind the keys (restated in the caveat)",
        "targets behind the keys (restated once more)",
        "keys with several targets (restated in the caveat)",
        "sham candidates (restated in the sham bullet)",
        "payload vocabulary (restated in the sham bullet)",
        "either-way findings",
        "never-executed findings",
    }
)

# The hand-kept residual, per block kind. Per kind and not one total, for the reason the S32 guard
# gives for its per-file table: a total lets a removal in one place cancel an addition elsewhere,
# and a sentence demoted from the docstring to a comment would otherwise keep the total unchanged.
# What escapes the tests above is a second site with the SAME key in the same block; this is
# the only thing that notices it. Measured on 2026-09-16 after the build, on this file.
_DOCSTRING_SITES = 16
_COMMENT_SITES = 6


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

    * RE-MEASURED from the code: the population, the target count, the row count, the seven, the
      RAISE guards, the live modules, and each of their restatements, one row per wording.
    * COMPARED TO A PIN, which proves the prose matches the recorded audit and NOT a fresh sweep:
      the coverage-decided candidate figure in ``guard_audit.PINNED_COVERAGE``; the other figure
      there, the tests never executing a guard line, is stated in no sentence of that docstring
      since GQ-403. Reaching them for real
      costs a ``COVERAGE_CORE=ctrace`` run and is ``sham --check --coverage-db``'s job.
    * COMPARED TO A HAND-TYPED TABLE, which proves only that the prose matches the table: the "one"
      and the "two", against ``len(_UNOBSERVED_EITHER_WAY)`` and ``len(_NEVER_EXECUTED)``.
    * COMPARED TO NOTHING AT ALL: the 19 and the "only 2". They are sweep results with no table and
      no pin, written here and nowhere else. Saying otherwise is the failure this tier list exists
      to prevent, and an earlier version of this paragraph did say otherwise.

    Every figure is read wherever its wording occurs in the docstring of its scope, the module by
    default and a named function for the helper docstrings, and every occurrence is compared, so a
    sentence repeated verbatim cannot restate a stale value behind the first one.

    What keeps the list above complete is the group of tests below rather than a promise: every
    number the S32 detector finds in this file is either inside a figure's capture group or listed
    in ``_UNDERIVED`` with its reason; the figures whose number the detector cannot see are named in
    ``_FIGURES_WITHOUT_SITE``; and the number of detector sites per block kind is pinned. What NONE
    of that sees, stated so the pin is not read as more than it is: a number whose noun is outside
    ``prose_numbers.COLLECTION_NOUNS`` ("19 of 98 targets", "55 findings"), a number word above
    twenty, a hyphenated number, a number further than the detector's gap from its noun ("85 remain
    once the tests"), and a number with no noun at all. Such a sentence ships unwatched here,
    exactly as the restatements did until each got its own row; widening the noun list re-opens
    the whole watched estate and is not done from this file.
    """
    wrong: list[str] = []
    for figure in _PROSE_FIGURES:
        hits = _figure_hits(figure)
        if not hits:
            wrong.append(
                f"{figure.label}: the sentence stating it is gone; reword the pattern or restore it"
            )
            continue
        expected = figure.measure()
        for block, match in hits:
            stated = parse_count(match.group(1))
            if stated != expected:
                wrong.append(
                    f"{figure.label} at {block.where()}: the docstring says {stated}, "
                    f"the code says {expected}"
                )
    assert not wrong, "this module's own docstring no longer describes the code:\n  " + "\n  ".join(
        wrong
    )


def test_every_number_the_detector_finds_is_derived_or_inventoried() -> None:
    """Every size-naming number here is inside a figure's capture group or in ``_UNDERIVED``.

    This is the completeness pin the docstring above used to say was missing. A new number lands
    here, not silently in the tree: it gets a figure that reads it, or a row that says in words why
    nothing can. Measured before this test existed, the docstring of the figures test had stated a
    retired value for ten days with the whole suite green."""
    unclaimed = [
        f"{site.block.where()} {site.value} in {site.snippet!r}\n        key: {_site_key(site)!r}"
        for site in _own_sites()
        if not _is_derived(site) and _site_key(site) not in _UNDERIVED
    ]
    assert not unclaimed, (
        "these numbers are neither derived by a figure nor inventoried; add a _Figure row if the "
        "number follows from the code, or an _UNDERIVED row with the reason it cannot:\n  "
        + "\n  ".join(unclaimed)
    )


def test_every_underived_row_still_names_a_number_in_this_file() -> None:
    """A row whose phrase is gone, or whose number a figure now derives, is a table that stopped
    describing the file."""
    live = {_site_key(site) for site in _own_sites()}
    derived = {_site_key(site) for site in _own_sites() if _is_derived(site)}
    stale = sorted(key for key in _UNDERIVED if key not in live)
    redundant = sorted(key for key in _UNDERIVED if key in derived)
    assert not stale, f"_UNDERIVED rows no longer match any number in this file: {stale}"
    assert not redundant, (
        f"_UNDERIVED rows for numbers a figure now derives, drop them: {redundant}"
    )


def test_the_figures_the_detector_cannot_see_are_named() -> None:
    """The figures whose number is no detector site are listed, in both directions.

    Those figures are exactly the ones the site counts below cannot protect, which is why they are
    named rather than counted. A figure with mixed hits, one carrying a site and one not, is neither
    and needs a second pattern or a row."""
    without, mixed = _figures_without_site()
    assert not mixed, f"figures with hits both with and without a detector site: {mixed}"
    assert without == _FIGURES_WITHOUT_SITE, (
        "the set of figures the detector cannot see changed (measured vs listed): "
        f"unlisted {sorted(without - _FIGURES_WITHOUT_SITE)}, "
        f"listed but now seen {sorted(_FIGURES_WITHOUT_SITE - without)}"
    )


def test_the_number_of_detector_sites_is_pinned() -> None:
    """The hand-kept residual: how many size-naming numbers this file holds, per block kind.

    Narrow on purpose, like the S32 guard's per-file pin: what escapes the tests above is a
    second occurrence of a phrase whose key is already inventoried, and a sentence demoted from the
    docstring to a comment moves both counts at once."""
    counts = Counter(_site_kind(site) for site in _own_sites())
    found = (counts["docstring"], counts["comment"])
    assert found == (_DOCSTRING_SITES, _COMMENT_SITES), (
        f"the number of size-naming phrases changed (docstring, comment): found {found}, "
        f"pinned {(_DOCSTRING_SITES, _COMMENT_SITES)}. A phrase was added, removed or moved; "
        "update the pin together with _PROSE_FIGURES and _UNDERIVED"
    )
