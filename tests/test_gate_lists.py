"""[GQ-132]: the write-gate prose's LISTS are compared to the gates they describe.

Two guards already compare a gate NUMBER to the code, and both record, at their own site, that
they never compare the enumeration the number summarises: ``test_prose_counters._GATE_SIZE_SCOPES``
and ``test_write_gate_contract.test_every_start_condition_count_matches_the_gate``. The gap is not
theoretical. ``CHANGELOG.md`` records the shipped operator guide naming four of the Olog gate's six
checks, and the figure beside that list was RIGHT while it happened, so a number guard would have
reported the passage as correct. This module closes that for the gate estate. The reading half is
``tests/gate_lists.py``.

THE EXPECTED COUNT IS BORROWED, NEVER RE-DERIVED, and that is the load-bearing decision here. A
second AST reader for "how wide is this gate" would be a second truth about the same gate, and the
day the two disagreed there would be no way to tell which was right. So:

* a gate's per-write checks come from ``test_prose_counters._gate_check_count``, which counts the
  module's audited deny call sites;
* the PV gate's refuse-to-start conditions come from ``test_write_gate_contract._start_conditions``,
  which counts its ``SafetyConfigError`` raises;
* the write-gate CONTRACT's six requirements come from nothing in the code, because they are a
  specification. Their count is the length of the specification's OWN ordinal run, and the other
  site that restates them is compared to that. The canonical row is therefore guarded by its
  dependants: delete a requirement and the run shortens, and the restatement in
  ``test_write_gate_contract``'s docstring goes red.

⛔ HONEST SCOPE. This covers the write-gate estate, the table ``gate_lists.SITES``, and NOT the
repository's counted lists at large. No figure is given for how big that table is, and the reason
is this module's own subject turned on itself: a hand-typed count of the list directly below,
which nothing compares, is exactly what this guard exists to catch. ``len(gl.SITES)`` answers it.

A repo-wide counter was rejected on a measurement rather than on taste: over hand-read sites a rule
pairing each list with the nearest number above it fired correctly 92 times against 42 false
alarms. ⚠️ Those two figures are a DERIVATION, not a measurement: they were classified by agents
reading the originals, and their accuracy is unverified. The decision does not rest on them being
exact, only on the false-alarm share being large, which two examples settle on their own and
neither can be tuned away. ``olog_safety.py`` numbers six checks
``0. 1. 2. 3. 3b. 4.``, where a counter reads four and the prose is right; and
``operator_guide.md`` names two tools above a three-item list whose third item opens "A THIRD
tool ... only its defaults keep it off that list", where both halves are right. How many counted
lists this leaves unguarded is deliberately NOT written here as a figure: this file would then
carry exactly the kind of hand-typed count it exists to catch, and a figure whose own sentence
joins the counted set cannot be written down at all. Re-derive it with the scripts recorded in
``analysis/gq132-listenzaehler-2026-08-21/`` in the workspace.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests import gate_lists as gl
from tests import test_prose_counters as pc
from tests import test_write_gate_contract as write_gate

_SRC = Path(__file__).resolve().parent.parent / "src" / "epics_mcp"

#: The one source that is a LIST rather than a measurement: the write-gate contract's requirements
#: are a specification, so no code holds their number.
_CONTRACT_POINTS = "contract-points"

#: The start-condition source, spelled as one string so a row cannot name the module without
#: saying WHICH of its two counts it means. ``safety.py`` alone is the gate's per-write width.
_START_CONDITIONS = "safety.py:start-conditions"


def _canonical_contract_points() -> int:
    """How many requirements the write-gate contract actually spells out."""
    canonical = [row for row in gl.SITES if row.canonical]
    assert len(canonical) == 1, (
        f"exactly one row may DEFINE the contract-point count, found {len(canonical)}. "
        "Two definitions of the same number is the drift this module exists to prevent."
    )
    return len(gl._read(canonical[0]).items)


def _expected(source: str) -> int:
    """What a list with this *source* must be as long as, measured now.

    Every branch delegates. Nothing here counts anything itself, see the module docstring.
    """
    if source == _CONTRACT_POINTS:
        return _canonical_contract_points()
    if source == _START_CONDITIONS:
        tree = ast.parse((_SRC / "safety.py").read_text(encoding="utf-8"))
        return write_gate._start_conditions("safety.py", tree)
    return pc._gate_check_count(source)


def test_every_registered_gate_list_matches_its_gate() -> None:
    """Every enumerated write-gate list is as long as the gate it describes.

    This is the half the two number guards leave open, and it is the half that shipped wrong:
    ``CHANGELOG.md`` records the operator guide listing four of six while the word "SIX" stood
    beside it.

    RED-PROOF: delete one bullet from the six-item Olog list in ``src/epics_mcp/operator_guide.md``
    and this reports ``the list has 5 items, olog_safety.py measures 6``, with every other test in
    the repository still green. Same for a numbered start condition in
    ``docs/write-gate-contract.md``, and for the CHANGELOG's own case, the list cut to four while
    the figure says six.
    """
    findings: list[str] = []
    for site in gl.read_all():
        if site.source is None:
            continue
        expected = _expected(site.source)
        if len(site.items) != expected:
            findings.append(
                f"{site.where()}: the list has {len(site.items)} items, "
                f"{site.source} measures {expected}"
            )
    assert not findings, (
        "a gate-size LIST disagrees with the gate it describes:\n  "
        + "\n  ".join(findings)
        + "\n  The FIGURE beside it may still be right; this guard counts the LIST. Repair the "
        "words, do not change the number to match a short list."
    )


def test_every_gate_size_scope_has_a_list_row() -> None:
    """Every PASSAGE the NUMBER guard watches for a gate size also has a LIST row here.

    Without this, the two halves drift apart in the one direction nothing else notices: a new
    gate-size sentence gets a number row, ships, and its enumeration is unwatched. That is exactly
    how ``_WATCHED`` grew a hole once, a file whose patterns matched perfectly and that nobody had
    listed.

    ⛔ THE COMPARISON IS BY (FILE, SCOPE), AND BY FILE ALONE IT WAS USELESS ON ITS FIRST RUN.
    That is not hypothetical, the post-build review measured it: ``_GATE_SIZE_SCOPES`` carries
    THREE ``operator_guide.md`` scopes and this table carried ONE row for that file, so a filename
    comparison was satisfied by the row that already existed and reported nothing, while two
    passages in the SHIPPED guide had no list row at all. One of them was a second enumeration of
    the very Olog list ``CHANGELOG.md`` records losing items from. Both have a row now.

    The two keyings agree because both name a markdown section by its heading and a Python site by
    its qualname; ``gate_lists`` derives the scope from the same reader the number guard uses, so
    matching them is not a coincidence somebody has to maintain.

    RED-PROOF: delete any row from ``gate_lists.SITES`` whose scope appears in
    ``_GATE_SIZE_SCOPES`` and this reports that file and scope.
    """
    watched = {(path, scope) for path, scope, _module in pc._GATE_SIZE_SCOPES}
    listed = {(Path(site.path).name, site.scope) for site in gl.read_all()}
    missing = sorted(f"{path} [{scope}]" for path, scope in watched - listed)
    assert not missing, (
        "a passage whose gate SIZE is watched has no gate LIST row:\n  "
        + "\n  ".join(missing)
        + "\n  A number without its enumeration is the half that shipped wrong once. Add a row "
        "to tests/gate_lists.SITES, or one with source=None and the reason it cannot be counted."
    )


def test_every_uncounted_row_still_has_the_length_it_claims() -> None:
    """A row exempt from the GATE comparison is not exempt from being counted at all.

    ``source=None`` says "no gate count is the right comparison here", never "this enumeration may
    change silently". Both of today's uncounted rows describe a PAIR and say so in the prose
    ("two of them", "its extra two are"), so the pair is what ``expect_items`` holds.

    ⚠️ This test did not exist for one commit while the reader's docstring already promised
    it, and the post-build review found the gap. The concrete hole it closes: widening
    ``update_log_entry``'s parenthesis to three checks left the word "two" standing and nothing
    compared anything.

    RED-PROOF: change either ``expect_items`` and this reports that row.
    """
    findings = [
        f"{site.where()}: the list has {len(site.items)} items, the row expects {row.expect_items}"
        for row, site in zip(gl.SITES, gl.read_all(), strict=True)
        if row.expect_items is not None and len(site.items) != row.expect_items
    ]
    assert not findings, (
        "an uncounted gate-list row changed length:\n  "
        + "\n  ".join(findings)
        + "\n  It is exempt from the gate comparison, not from this one. If the enumeration "
        "really grew, say so in the prose and move expect_items in the same change."
    )


def test_every_uncounted_row_declares_a_length() -> None:
    """``source=None`` without ``expect_items`` would be an exemption from both checks at once."""
    naked = [row.path for row in gl.SITES if row.source is None and row.expect_items is None]
    assert not naked, (
        "an uncounted row declares no expected length:\n  "
        + "\n  ".join(naked)
        + "\n  Set expect_items, so the row is still counted against something."
    )


def test_every_uncounted_row_says_why() -> None:
    """A row that is registered but not counted carries a reason, and it is a real one.

    ``source=None`` is the only way a site escapes the comparison, so it has to cost a sentence.
    Both of today's rows earn it: ``update_log_entry`` enumerates a subset and SAYS it does ("two
    of them"), and ``SECURITY.md`` names the arithmetic difference between the two gates, which no
    gate count is the right comparison for.
    """
    thin = [row.path for row in gl.SITES if row.source is None and len(row.reason.split()) < 12]
    assert not thin, (
        "an uncounted row has no real reason:\n  "
        + "\n  ".join(thin)
        + "\n  source=None is an exemption; say what makes counting it wrong."
    )


def test_every_row_still_finds_its_anchor() -> None:
    """Every row, counted or not, still matches the passage it names.

    An uncounted row is exempt from the COMPARISON, never from being read: a subset that quietly
    became the whole list, or a passage that vanished, must not pass unnoticed.

    RED-PROOF: reword any anchored first item and this reports that row's file.
    """
    broken: list[str] = []
    for row in gl.SITES:
        try:
            gl._read(row)
        except gl.AnchorNotFound as exc:
            broken.append(f"{row.path}: {exc}")
    assert not broken, (
        "a registered gate-list row no longer matches its passage:\n  "
        + "\n  ".join(broken)
        + "\n  The anchor is the row's identity. If the passage was restructured on purpose, read "
        "it again and move the anchor; do not delete the row."
    )


# ======================================================================================
# The readers themselves, on constructed input
# ======================================================================================
#
# The rows above prove the readers work on the tree as it stands TODAY. These prove the decisions
# inside them, which is a different question: a decision that happens to be inert on today's prose
# is exactly the one a later edit reverses without anything going red.


def test_an_adjacent_run_counts_a_letter_suffixed_and_zero_based_marker() -> None:
    """``0. 1. 2. 3. 3b. 4.`` is six items, not four.

    The live case is ``olog_safety.OlogWriteGate``, and it is the single most likely way a naive
    counter reports a correct passage as wrong. Both halves are load-bearing: the run may start at
    0, and a marker may carry a letter.
    """
    text = "\n".join(
        (
            "    0. first",
            "    1. second",
            "    2. third",
            "    3. fourth",
            "    3b. fifth, split out of the fourth",
            "       and wrapped onto a second line",
            "    4. sixth",
        )
    )
    _lineno, items = gl.adjacent_items(text, "0. first")
    assert len(items) == 6


def test_an_adjacent_run_does_not_count_a_sub_list() -> None:
    """A deeper indent belongs to the item above it. The write-gate contract nests two levels."""
    text = "\n".join(("- one", "  - not an item", "  - nor this", "- two"))
    _lineno, items = gl.adjacent_items(text, "- one")
    assert len(items) == 2


def test_an_adjacent_run_ends_at_unmarked_prose() -> None:
    """A blank line does not end a run; a paragraph at the same indentation does."""
    text = "\n".join(("- one", "", "- two", "", "And now a sentence.", "- three"))
    _lineno, items = gl.adjacent_items(text, "- one")
    assert len(items) == 2


def test_ordinal_sections_span_body_text_and_stop_at_a_heading() -> None:
    """The contract's six requirements are 131 lines apart; a heading starts a different list."""
    text = "\n".join(
        (
            "**1. First.** body",
            "more body",
            "",
            "**2. Second.** body",
            "",
            "## A new section",
            "**3. Third.** belongs to the section above, not to the run",
        )
    )
    _lineno, items = gl.ordinal_sections(text, "**1. First.**")
    assert len(items) == 2


def test_ordinal_sections_stop_at_a_gap_in_the_numbering() -> None:
    """A missing successor SHORTENS the run, which is the finding rather than a reason to skip."""
    text = "\n".join(("**1. First.** body", "**2. Second.** body", "**4. Fourth.** body"))
    _lineno, items = gl.ordinal_sections(text, "**1. First.**")
    assert len(items) == 2


def test_a_numbered_chain_counts_markers_and_not_separators() -> None:
    """``(1) ... ; (2) ...`` is read by its markers, so a ``"; ("`` inside an item is harmless.

    The live case is the write-gate contract's six points restated in
    ``test_write_gate_contract``'s docstring. A separator-driven reader would split a point that
    ever contained that pair into two and go red on a correct sentence, which is the false-alarm
    class this whole ticket measured and refused to ship.
    """
    text = "points are: (1) alpha; (2) beta, which itself reads '; (' literally; (3) gamma."
    items = gl.numbered_chain(text, "points are: ", ".")
    assert len(items) == 3


def test_a_chain_fails_loudly_when_its_anchor_is_reworded() -> None:
    """A vanished anchor raises. Returning nothing would read like "the list was deleted"."""
    with pytest.raises(gl.AnchorNotFound):
        gl.chain_items("nothing like the anchor here", "six checks (", ")", " + ")


def test_a_chain_fails_loudly_when_its_terminator_is_reworded() -> None:
    """Half a chain is worse than none: it would report a correct list as short."""
    with pytest.raises(gl.AnchorNotFound):
        gl.chain_items("six checks (a + b + c", "six checks (", ")", " + ")


def test_an_anchor_that_matches_twice_is_refused() -> None:
    """An anchor is an identity. Two matches means it stopped being one, so it fails rather than
    silently binding the row to whichever list came first."""
    text = "\n".join(("- one", "- two", "", "- one", "- two"))
    with pytest.raises(gl.AnchorNotFound):
        gl.adjacent_items(text, "- one")
