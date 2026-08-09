"""The four display tools must count the inventory's DIAGNOSTICS TAIL identically (GB-71).

The tail is ``PvDiagnostics.context_capped`` (which targets the context cap cut short) and
``PvDiagnostics.glob_capped`` (which globbed ``<file>`` references the glob cap cut short). All four
display tools report it, so that an answer is never read as complete when it is a lower bound:
``crossplane_check``, ``coverage_audit``, ``validate_pvs``, ``find_device``.

Until GB-71 three separate places read those two fields off the inventory, and nothing held them to
the same reading. That is not a hypothetical failure mode: ``find_device`` was one of the three and
had simply forgotten the tail entirely until GB-65 put it back. The only assertion anywhere near
these values was ``isinstance(..., int)``.

Two halves, and they secure different things:

* **Equality.** One walk, one tail, four consumers, same numbers. Parametrised over two vectors
  whose numbers differ, because a single vector cannot tell a value that is passed through from one
  that happens to be hard-coded.
* **Uniqueness.** Source guards that keep both the READ and the CALL in one place, so a fifth
  consumer cannot grow its own copy and drift again. Equality alone would not survive the next tool.

⚠ The uniqueness half needs BOTH guards, and the second was missing from the first version of this
file (found by GB-71's own post-build review). A read guard catches a second reading drifting from
the first. It is blind to the failure that actually happened: a consumer that runs the walk itself
and reports no tail AT ALL leaves nothing for a read guard to see. That was ``find_device`` until
GB-65, and a drift guard would have passed it every day it was broken.

⚠ ``glob_capped`` is a tuple of ``(source display, raw <file> target)`` PAIRS, and every tool
counts PAIRS. Counting distinct SOURCES is the plausible-looking alternative that reports a smaller
number for the same walk, and it is exactly the mutant this file exists to catch. The fixture
vectors below therefore carry more pairs than sources, and a third test asserts that property of
the fixtures themselves: a blind fixture (one pair per source) would leave the mutant green while
looking like a test.

**A third half, added by GB-72: the WORDING.** Equality and uniqueness both concern the numbers,
and the numbers were only ever half the promise. The same cap was NAMED three different ways
across the four tools (``per-display context cap``, ``per-instance context cap``, and the bare
``the context cap``), which breaks a reader rather than a computation: an assistant that has read
one tool's description searches the notes for the phrase that description uses, and does not find
it. Worse, one of the three was wrong. ``per-instance`` is what ``services/crossplane.py`` calls
the INVENTORY, while the cap counts reachability contexts per FILE, so that note named the thing
the cap shortens instead of the cap. The guard below fixes ``per-display context cap`` as the one
wording and holds every place that names the cap to it.

⚠ Three properties of that guard are load-bearing, and each was measured rather than assumed.
It reads the AST and not the lines, because two of the seven mentions straddle a line break and a
line-wise ``grep`` misses both, INCLUDING a fourth wording that lived inside ``crossplane_check``
beside its own second one, so that one service named the cap two ways. It renders an f-string
WHOLE, with a placeholder per insertion, so that a qualifier assembled at runtime
(``f"hit the {qual} context cap"``) reads as ``hit the {} context cap`` and is rejected rather
than passing unseen between two constant segments. And it anchors on ``hit the ... context cap``,
the construction with which a note REPORTS the cap, so the advice ``re-run with a higher context
cap`` at the end of two of those same notes stays untouched: that is the argument, not a second
name for the cap.

⚠ Named blind spots, in the same spirit as the one on :func:`_tail_reads`. A note that announces
the cap some other way ("was cut short by the context cap") is not this construction and is
invisible here. COMMENTS are invisible too, because an AST carries none: the two ``#:`` field
comments that also name the cap were brought along by hand and nothing holds them there. And the
guard is about the WORD, never about whether a tool emits it at all. That each tool really does
emit it is pinned per tool, beside the behaviour it belongs to, in ``test_validate.py``,
``test_coverage.py``, ``test_crossplane.py`` and ``test_device_lookup.py``.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple
from unittest.mock import patch

import pytest
from opi_navigation.pv_analysis import analyze_pv_inventory
from opi_navigation.pv_analysis.models import PvDiagnostics, PvInventory

from epics_mcp.services.inventory_adapter import (
    DEFAULT_PV_CONTEXT_CAP,
    analyze_display_index,
    analyze_display_pvs,
)
from epics_mcp.tools.find_device import _run_lookup
from epics_mcp.tools.validate import _run_validate

#: The one seam every consumer now goes through. Patching it feeds a known tail to all four at once,
#: which is what makes this an equality test rather than four independent ones.
_ENGINE_SEAM = "epics_mcp.services.inventory_adapter.analyze_pv_inventory"

#: The display the fixture dataset holds, and the one ``validate_pvs`` is asked about.
_PANEL = "panel.bob"

#: A device prefix ``find_device`` can match. Synthetic, per the facility-agnostic guardrail.
_DEVICE = "DEV-TEST01:Ctrl-EVR-01"


class _Tail(NamedTuple):
    """One test vector: the tail an inventory is given, plus what every tool must report for it."""

    #: Goes into ``diagnostics.context_capped`` verbatim. Contains :data:`_PANEL`, so the verdict
    #: ``validate_pvs`` derives from it can be observed as well as the raw tuple the others pass on.
    context_capped: tuple[str, ...]
    #: Goes into ``diagnostics.glob_capped``, engine-shaped as ``(source, raw_target)``.
    glob_capped: tuple[tuple[str, str], ...]

    @property
    def expected_count(self) -> int:
        """What every tool must report: the number of PAIRS."""
        return len(self.glob_capped)

    @property
    def source_count(self) -> int:
        """What the mutant would report instead: the number of distinct SOURCE displays."""
        return len({source for source, _ in self.glob_capped})


#: Three pairs over two sources, three capped contexts.
_VECTOR_A = _Tail(
    context_capped=(_PANEL, "alpha.bob", "beta.bob"),
    glob_capped=(("p.bob", "a/*.bob"), ("p.bob", "b/*.bob"), ("q.bob", "c/*.bob")),
)

#: Five pairs over three sources, one capped context. Every number differs from vector A, so a
#: hard-coded return value cannot satisfy both.
_VECTOR_B = _Tail(
    context_capped=(_PANEL,),
    glob_capped=(
        ("r.bob", "d/*.bob"),
        ("r.bob", "e/*.bob"),
        ("r.bob", "f/*.bob"),
        ("s.bob", "g/*.bob"),
        ("t.bob", "h/*.bob"),
    ),
)

_VECTORS = [pytest.param(_VECTOR_A, id="A"), pytest.param(_VECTOR_B, id="B")]


def _dataset(tmp_path: Path) -> Path:
    """A minimal real dataset: one display with a concrete channel AND a macro-templated one.

    The macro half is load-bearing for ``validate_pvs``: ``_file_view_is_capped`` answers False for
    a file with no macro-templated occurrence whatever the tail says, and its docstring explains why
    that is a statement about the engine rather than a shortcut. Without it the context-cap verdict
    could not be observed here at all.
    """
    root = tmp_path / "ds"
    root.mkdir()
    (root / _PANEL).write_text(
        '<display version="2.0.0"><name>Panel</name>'
        f'<widget type="textupdate"><name>a</name><pv_name>{_DEVICE}:status</pv_name></widget>'
        f'<widget type="textupdate"><name>b</name><pv_name>$(P){_DEVICE}:extra</pv_name></widget>'
        "</display>",
        encoding="utf-8",
    )
    return root


def _engine_with_tail(tail: _Tail) -> Callable[..., PvInventory]:
    """Run the REAL engine and replace only its diagnostics tail with *tail*.

    Faking the whole inventory would let the tools see displays that no walk produced, and two of
    the four consumers (``validate_pvs``, ``find_device``) read real display events beside the tail.
    So the walk stays real and only the two fields under test are substituted.
    """

    def _engine(repo_root: Path, *, context_cap: int, windows_paths: bool) -> PvInventory:
        real = analyze_pv_inventory(repo_root, context_cap=context_cap, windows_paths=windows_paths)
        return real.model_copy(
            update={
                "diagnostics": PvDiagnostics(
                    context_capped=tail.context_capped, glob_capped=tail.glob_capped
                )
            }
        )

    return _engine


@pytest.mark.parametrize("tail", _VECTORS)
def test_the_three_raw_consumers_report_the_identical_tail(tail: _Tail, tmp_path: Path) -> None:
    """``crossplane_check``, ``coverage_audit`` and ``find_device`` pass the tail on unchanged.

    These three receive both values as they are: the first two through the adapters
    ``analyze_display_pvs`` / ``analyze_display_index``, which ``services/orchestration.py`` hands
    straight to the pure cores, the third through ``_run_lookup``. So all three can be compared
    field for field, and against the vector, in one assertion.

    ``validate_pvs`` is the one consumer that does NOT pass ``context_capped`` on (it collapses the
    tuple into two per-file verdicts), which is why it has its own two tests below rather than a
    row here.
    """
    root = _dataset(tmp_path)
    with patch(_ENGINE_SEAM, _engine_with_tail(tail)):
        _, crossplane_context, crossplane_globs = analyze_display_pvs(root)
        _, coverage_context, coverage_globs = analyze_display_index(root)
        _, _, device_context, device_globs = _run_lookup(
            str(root), _DEVICE, "prefix", DEFAULT_PV_CONTEXT_CAP, False
        )

    reported = {
        "crossplane_check": (crossplane_context, crossplane_globs),
        "coverage_audit": (coverage_context, coverage_globs),
        "find_device": (device_context, device_globs),
    }
    expected = (tail.context_capped, tail.expected_count)
    assert reported == dict.fromkeys(reported, expected), (
        f"the three tools disagree about the tail of ONE walk: {reported}, expected {expected}"
    )


@pytest.mark.parametrize("tail", _VECTORS)
def test_validate_counts_the_same_glob_pairs_as_its_three_siblings(
    tail: _Tail, tmp_path: Path
) -> None:
    """The fourth tool counts globbed references the same way: PAIRS, not source displays.

    ``validate_pvs`` reports the glob cap as a plain number in its answer, so this half is directly
    comparable with the other three. Both vectors carry more pairs than sources, so a consumer that
    counted sources would report 2 instead of 3 here, and 3 instead of 5 in vector B.
    """
    root = _dataset(tmp_path)
    with patch(_ENGINE_SEAM, _engine_with_tail(tail)):
        extraction = _run_validate(str(root / _PANEL), str(root))

    assert extraction.glob_capped_count == tail.expected_count
    assert extraction.glob_capped_count != tail.source_count, (
        "the vector cannot distinguish a pair count from a source count, see the fixture guard"
    )


def test_validate_sees_the_same_context_capped_as_its_siblings(tmp_path: Path) -> None:
    """The fourth tool receives ``context_capped`` too, shown through the verdict it derives.

    ``validate_pvs`` is the only consumer that consumes the tuple as a membership test rather than
    passing it on, so equality with the other three cannot be asserted on a value here. What can be
    asserted is that the tuple reaches it AT ALL, and BOTH directions are needed for that: a tool
    that ignored the tail and always answered True would satisfy the positive case alone.
    """
    root = _dataset(tmp_path)
    naming_it = _Tail(context_capped=(_PANEL,), glob_capped=())
    not_naming_it = _Tail(context_capped=("somewhere-else.bob",), glob_capped=())

    with patch(_ENGINE_SEAM, _engine_with_tail(naming_it)):
        flagged = _run_validate(str(root / _PANEL), str(root))
    with patch(_ENGINE_SEAM, _engine_with_tail(not_naming_it)):
        unflagged = _run_validate(str(root / _PANEL), str(root))

    assert flagged.capped is True, "the capped target named this very file and was not seen"
    assert unflagged.capped is False, "a tail naming another file must not flag this one"


@pytest.mark.parametrize("tail", _VECTORS)
def test_the_fixture_vectors_can_tell_a_pair_count_from_a_source_count(tail: _Tail) -> None:
    """Guard on the FIXTURES, not on the code, and it is the reason the tests above bite.

    A sabotage of the production code cannot be caught by a blind fixture: with one pair per source
    the two counts coincide, the pair/source mutant reports the same number, and every assertion
    above stays green while measuring nothing. Same for an empty tail against a consumer that
    returns a constant.

    So this asserts the properties the vectors must keep, and it fails the day someone tidies them
    into something that looks equivalent and is not.
    """
    assert tail.glob_capped, "an empty glob tail cannot separate any counting rule from a constant"
    assert tail.context_capped, "an empty context tail is indistinguishable from a dropped field"
    assert tail.expected_count != tail.source_count, (
        f"vector has one pair per source ({tail.expected_count}), so the pair/source mutant "
        "would stay green"
    )


def test_the_two_fixture_vectors_differ_in_every_number() -> None:
    """A value that is passed through must be told apart from one that is hard-coded.

    One vector cannot do that: a consumer returning the literal 3 satisfies vector A perfectly. So
    both counts differ between the vectors, and this asserts that they keep differing.
    """
    assert _VECTOR_A.expected_count != _VECTOR_B.expected_count
    assert len(_VECTOR_A.context_capped) != len(_VECTOR_B.context_capped)


# --------------------------------------------------------------------------------------------
# The uniqueness half: one place reads the tail, and no consumer grows a second copy.
# --------------------------------------------------------------------------------------------

_SRC = Path(__file__).resolve().parent.parent / "src" / "epics_mcp"

#: The one module allowed to read the tail, relative to :data:`_SRC`.
_COLLECTION_POINT = Path("services") / "inventory_adapter.py"

#: The two fields that make up the tail. Read anywhere else, they are a second reading free to
#: drift from the first, which is the defect GB-71 removed.
_TAIL_FIELDS = frozenset({"context_capped", "glob_capped"})

#: The engine entry point that produces a tail. Called anywhere else, a consumer holds an inventory
#: the collection point never saw, and may report no tail at all.
_ENGINE_CALL = "analyze_pv_inventory"


def _tail_reads(source: str) -> list[str]:
    """Return the ``<something>.diagnostics.<tail field>`` reads in *source*.

    Anchored on the ``diagnostics`` hop rather than on the field names alone, because both names
    also occur as keyword arguments all over the reporting path
    (``crossplane_check(context_capped=...)``), and those are the tail being PASSED ON, which is
    the whole point, not read again.

    ⚠ Named blind spot: a dynamic read (``getattr(inv.diagnostics, name)``) is not an
    ``ast.Attribute`` and is invisible here. That is a hole in a guard against accident, not
    against evasion, and no source-level guard closes it.
    """
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Attribute) or node.attr not in _TAIL_FIELDS:
            continue
        parent = node.value
        if isinstance(parent, ast.Attribute) and parent.attr == "diagnostics":
            found.append(f"{node.attr}")
    return found


def _engine_calls(source: str) -> list[str]:
    """Return the ``analyze_pv_inventory(...)`` CALL sites in *source*, plain and dotted.

    The call rather than the import: an import with no call is dead code that ruff already reports,
    while a call is a consumer holding an inventory of its own.
    """
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (
            func.id
            if isinstance(func, ast.Name)
            else func.attr
            if isinstance(func, ast.Attribute)
            else None
        )
        if name == _ENGINE_CALL:
            found.append(name)
    return found


def test_only_the_collection_point_runs_the_engine() -> None:
    """No module outside the collection point calls the engine, so no tail can go unreported.

    This is the OTHER half of the defect class, and the one that actually happened. The read guard
    below catches a second reading DRIFTING from the first; it is blind to a consumer that runs the
    walk and reports no tail at all, because there is nothing to see. That is precisely what
    ``find_device`` did until GB-65: it called the engine directly and simply never mentioned either
    cap. A guard against drift would have passed it every single day.

    Found by the post-build review of GB-71 itself, in the guard built to close GB-71.
    """
    offenders = {
        str(path.relative_to(_SRC)): calls
        for path in sorted(_SRC.rglob("*.py"))
        if path.relative_to(_SRC) != _COLLECTION_POINT
        and (calls := _engine_calls(path.read_text(encoding="utf-8")))
    }
    assert not offenders, (
        f"{_ENGINE_CALL} is called outside the collection point "
        f"({_COLLECTION_POINT.as_posix()}): {offenders}. A consumer holding its own inventory owes "
        "the caller the diagnostics tail and nothing checks that it pays. Project through "
        "inventory_adapter.analyze_inventory instead, see GB-71."
    )


def test_the_collection_point_really_does_run_the_engine() -> None:
    """The non-vacuity floor for the call guard, same reasoning as for the read guard."""
    calls = _engine_calls((_SRC / _COLLECTION_POINT).read_text(encoding="utf-8"))
    assert calls == [_ENGINE_CALL], (
        f"the collection point makes {len(calls)} engine calls, expected 1"
    )


def test_the_call_detector_separates_a_call_from_an_import() -> None:
    """The detector itself, on synthetic sources."""
    assert _engine_calls("inv = analyze_pv_inventory(root, context_cap=1)") == [_ENGINE_CALL]
    assert _engine_calls("inv = pv_analysis.analyze_pv_inventory(root)") == [_ENGINE_CALL]
    # An import alone is not a consumer; ruff reports it if nothing uses it.
    assert _engine_calls("from opi_navigation.pv_analysis import analyze_pv_inventory") == []
    # A different engine entry point, which produces no tail and is free to be called anywhere.
    assert _engine_calls("x = find_displays(inv, q)") == []


def test_only_the_collection_point_reads_the_diagnostics_tail() -> None:
    """No module outside ``services/inventory_adapter.py`` reads the tail off an inventory.

    This is what keeps the equality tests above true for the NEXT consumer. Equality is a property
    of today's four call sites; uniqueness is a property of the design, and only the second one
    survives a fifth tool being added by somebody who never read this file.
    """
    offenders = {
        str(path.relative_to(_SRC)): reads
        for path in sorted(_SRC.rglob("*.py"))
        if path.relative_to(_SRC) != _COLLECTION_POINT
        and (reads := _tail_reads(path.read_text(encoding="utf-8")))
    }
    assert not offenders, (
        "the diagnostics tail is read outside the collection point "
        f"({_COLLECTION_POINT.as_posix()}): {offenders}. Route the walk through "
        "inventory_adapter.analyze_inventory instead, see GB-71."
    )


def test_the_collection_point_really_does_read_it() -> None:
    """The non-vacuity floor: the guard above would also pass if NOBODY read the tail.

    Every guard in this repository has to be shown able to go red. For a guard phrased as "only X
    may do this", the failure mode that costs nothing to build is X quietly stopping, at which
    point the guard measures an empty population and reports success forever.
    """
    reads = _tail_reads((_SRC / _COLLECTION_POINT).read_text(encoding="utf-8"))
    assert sorted(set(reads)) == sorted(_TAIL_FIELDS), (
        f"the collection point reads {sorted(set(reads))}, expected both tail fields"
    )


def test_the_detector_recognises_a_read_and_ignores_a_pass_on() -> None:
    """The detector itself, on synthetic sources: it must separate a READ from a PASS-ON.

    Without this, "no offenders" is a claim about the detector as much as about the tree, and a
    detector that matched nothing at all would look exactly like a clean repository.
    """
    assert _tail_reads("x = inventory.diagnostics.glob_capped") == ["glob_capped"]
    assert _tail_reads("x = self.inv.diagnostics.context_capped") == ["context_capped"]
    assert _tail_reads("n = len(report.diagnostics.glob_capped)") == ["glob_capped"]
    # A pass-on, which is the normal and desired shape everywhere downstream.
    assert _tail_reads("audit(rows, context_capped=capped, glob_capped_count=n)") == []
    # The field name on something that is not a diagnostics object.
    assert _tail_reads("x = other.context_capped") == []
    # The diagnostics object itself, without reaching into the tail.
    assert _tail_reads("d = inventory.diagnostics") == []


# --------------------------------------------------------------------------------------------
# The wording half (GB-72): one name for the cap, everywhere a tool names it.
# --------------------------------------------------------------------------------------------

#: The one qualifier a note may carry when it REPORTS the context cap. It is the engine's own
#: unit of measure (``opi_navigation`` budgets reachability contexts per file), and it is already
#: what all four ``context_cap`` argument descriptions, both CLI help texts and the shipped
#: operator guide say, so this pins the majority wording rather than inventing a new one.
_CAP_QUALIFIER = "per-display "

#: The construction a note uses to REPORT the cap. Deliberately not "any mention of the cap":
#: ``re-run with a higher context cap`` is advice about the ARGUMENT and is left alone.
#:
#: A qualifier is an attribute, never a sentence, so it may not contain sentence punctuation. That
#: is not cosmetic: a bare ``.*?`` spans anything between the two anchors, and the ``view``
#: description of ``validate_pvs`` says "hit the per-display context cap" early and "reports the
#: glob cap" some 400 characters later, so the glob pattern below matched that whole paragraph and
#: reported a description as a malformed note. Measured after narrowing: the context pattern keeps
#: exactly the same seven matches, and the glob one drops to the four notes it is about.
_CAP_MENTION = re.compile(r"hit the ([^.,:;]*?)context cap", re.S)

#: What an f-string insertion renders as, so a qualifier computed at runtime shows up as a
#: qualifier that is not :data:`_CAP_QUALIFIER`, rather than vanishing between two segments.
_INSERTION = "{}"

#: The modules that must each name the cap. Not a count: a count says nothing about WHICH tool
#: fell out of the population, and a tool dropping its note entirely is how this defect class
#: began (see ``find_device`` before GB-65).
_CAP_NAMING_MODULES = frozenset(
    {
        Path("display_tools.py"),
        Path("services") / "coverage.py",
        Path("services") / "crossplane.py",
        Path("services") / "device_lookup.py",
        Path("tools") / "validate.py",
    }
)


def _rendered_strings(tree: ast.AST) -> list[str]:
    """Return every string literal in *tree*, with an f-string rendered WHOLE.

    An f-string is one :class:`ast.JoinedStr` holding constant segments and insertions. Walking
    for :class:`ast.Constant` alone would see the segments separately, so a phrase split by an
    insertion would be invisible; each insertion therefore renders as :data:`_INSERTION` and the
    JoinedStr is not descended into. Implicit concatenation needs no handling: Python already
    joins ``"a" "b"`` into one node at parse time, which is why the two mentions that straddle a
    line break are seen here and are missed by a line-wise search.
    """
    found: list[str] = []

    class _Visitor(ast.NodeVisitor):
        def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
            parts = [
                part.value
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
                else _INSERTION
                for part in node.values
            ]
            found.append("".join(parts))
            # No generic_visit: its constant segments are already accounted for above, and
            # descending would report the same phrase twice and split ones not at all.

        def visit_Constant(self, node: ast.Constant) -> None:
            if isinstance(node.value, str):
                found.append(node.value)

    _Visitor().visit(tree)
    return found


def _mentions(source: str, mention: re.Pattern[str]) -> list[tuple[str, str]]:
    """Return ``(whole literal, qualifier)`` for every match of *mention* in *source*.

    One detector for both caps rather than a copy per cap, which is the same build-once rule the
    collection point above follows: the two guards ask different questions of the result (one
    reads the qualifier, the other the literal that carries it) and share the AST walk.
    """
    return [
        (text, match.group(1))
        for text in _rendered_strings(ast.parse(source))
        for match in mention.finditer(text)
    ]


def _cap_qualifiers(source: str) -> list[str]:
    """Return the qualifier of every "hit the ... context cap" in *source*, in source order."""
    return [qualifier for _, qualifier in _mentions(source, _CAP_MENTION)]


def test_every_place_that_names_the_context_cap_uses_the_same_words() -> None:
    """One cap, one name, so a phrase from any tool's description finds the matching note.

    This is a guard about READERS, not about numbers, and it is the half the equality tests above
    cannot express: four tools can agree perfectly on what they counted and still call it three
    different things, which is exactly the state GB-72 found. An assistant that reads
    ``crossplane_check``'s description ("per-display reachability contexts") and then greps the
    notes for that wording used to come up empty on the very tool it had just read.

    Provably red: put ``per-instance`` back into either crossplane note, or drop the qualifier
    from the coverage one.
    """
    offenders = {
        f"{path.relative_to(_SRC).as_posix()}: {qualifier!r}"
        for path in sorted(_SRC.rglob("*.py"))
        for qualifier in _cap_qualifiers(path.read_text(encoding="utf-8"))
        if qualifier != _CAP_QUALIFIER
    }
    assert not offenders, (
        f"the context cap is named in more than one way: {sorted(offenders)}. Every note that "
        f"reports it says 'hit the {_CAP_QUALIFIER}context cap', because that is the engine's "
        "own unit (contexts per file) and what every argument description already says. See "
        "GB-72."
    )


def test_every_display_tool_actually_names_the_cap() -> None:
    """The non-vacuity floor: a guard phrased as "all of them agree" also passes on silence.

    Sameness over an empty population is free, and the cheapest way to satisfy the test above is
    for a tool to stop mentioning the cap at all, which is the WORSE defect and the one this
    repository has really had. So the population is named per module rather than counted: a
    module dropping out is reported by name.
    """
    naming = {
        path.relative_to(_SRC)
        for path in sorted(_SRC.rglob("*.py"))
        if _cap_qualifiers(path.read_text(encoding="utf-8"))
    }
    assert naming == _CAP_NAMING_MODULES, (
        f"modules naming the context cap changed: missing {_CAP_NAMING_MODULES - naming}, "
        f"unexpected {naming - _CAP_NAMING_MODULES}. A tool that stopped naming the cap is a "
        "silent lower bound, not a tidy-up; a NEW one belongs in this set."
    )


def test_the_wording_detector_sees_a_split_phrase_and_ignores_the_advice() -> None:
    """The detector itself, on synthetic sources, including the two shapes it exists for.

    Without this, "no offenders" is as much a claim about the detector as about the tree, and the
    two hard cases are precisely the ones a naive detector gets wrong in the SAFE-looking
    direction: it reports nothing and reads as a clean repository.
    """
    # The plain case, and the qualifier is returned rather than merely matched.
    assert _cap_qualifiers('x = "hit the per-display context cap"') == ["per-display "]
    assert _cap_qualifiers('x = "hit the per-instance context cap"') == ["per-instance "]
    # Implicit concatenation across a line break: one node at parse time, one match here. This is
    # the shape a line-wise search misses, and two of the real mentions have it.
    assert _cap_qualifiers('x = ("hit the per-display context " "cap, so on")') == ["per-display "]
    # An f-string: the count in front must not hide the phrase behind it.
    assert _cap_qualifiers('x = f"{n} display(s) hit the per-display context cap"') == [
        "per-display "
    ]
    # A qualifier assembled at runtime is a qualifier nobody pinned, so it must NOT read as clean.
    assert _cap_qualifiers('x = f"hit the {qual} context cap"') == [f"{_INSERTION} "]
    # The advice at the end of two real notes talks about the argument, not about the cap's name.
    assert _cap_qualifiers('x = "(re-run with a higher context cap)."') == []
    # A mention that is not this construction: out of reach by design, see the file docstring.
    assert _cap_qualifiers('x = "was cut short by the context cap"') == []


#: Every display tool has to name BOTH limits of the walk on the wire, in the words its notes use.
#: Two phrases rather than one: naming one limit while staying silent about the other is exactly
#: the state GB-72 found, and it reads as "there is one limit" to a caller.
#:
#: The context-cap phrase is the full one, deliberately identical to :data:`_CAP_QUALIFIER` plus
#: the noun, because matching the notes is the whole point. Measured: describing the cap without
#: naming it is its own failure, and two tools were in it. Their descriptions said "per-display
#: reachability contexts the PV-inventory explores", which explains the cap perfectly and cannot
#: be found by anyone searching for the phrase the notes and the sibling tools use. The argument
#: is called ``context_cap`` with an underscore, so it does not match either.
_WIRE_CAP_PHRASES = (f"{_CAP_QUALIFIER}context cap", "glob cap")

#: The tools whose descriptions carry that duty, i.e. the four fed by the inventory walk.
_DISPLAY_TOOLS = frozenset({"validate_pvs", "crossplane_check", "coverage_audit", "find_device"})


async def test_every_display_tool_names_both_walk_limits_on_the_wire() -> None:
    """The DESCRIPTION side of the same promise, checked where a caller actually reads it.

    The guards above hold the notes to one wording; this one holds the descriptions to naming the
    thing at all. It is the other half of the same defect and it needs its own test, measured
    rather than assumed: removing either sentence added by GB-72 leaves the entire suite green,
    so nothing but this stops the descriptions from falling silent again. Both
    ``crossplane_check`` and ``coverage_audit`` emitted a glob-cap note while their descriptions
    named only the context cap, which is the worse direction of the two, because a caller who
    reads a description and sees one limit has been told there is one.

    Read off the wire (``mcp.list_tools()``) rather than out of the source, because the wire is
    what an assistant sees: a sentence in a docstring nobody serialises would satisfy a source
    check and help no one. The whole serialised tool is searched, not one argument, so moving the
    sentence between the docstring and a field description is free, as it should be.

    Provably red: drop the glob-cap sentence from either tool's ``context_cap`` description.
    """
    from mcp.types import ListToolsResult

    from epics_mcp.server import mcp

    tools = {t.name: t for t in await mcp.list_tools() if t.name in _DISPLAY_TOOLS}
    assert set(tools) == _DISPLAY_TOOLS, (
        f"expected the four display tools on the wire, found {sorted(tools)}. Running without the "
        "displays group? Then this test cannot make its statement and must not pass quietly."
    )

    silent = {
        name: [phrase for phrase in _WIRE_CAP_PHRASES if phrase not in serialised]
        for name, tool in tools.items()
        if (
            serialised := ListToolsResult(tools=[tool.to_mcp_tool()]).model_dump_json(
                by_alias=True, exclude_none=True
            )
        )
        and any(phrase not in serialised for phrase in _WIRE_CAP_PHRASES)
    }
    assert not silent, (
        f"display tools that do not name both limits of the inventory walk: {silent}. A tool that "
        "emits a note about a cap and never mentions it in its description leaves the reader to "
        "discover the limit from an answer, see GB-72."
    )


#: The names this repository has really given the context cap besides the agreed one, each of them
#: removed by GB-72. A denylist alongside the phrase guard above, because these three sat OUTSIDE
#: the "hit the ... context cap" construction it anchors on: in ``find_device``'s ``context_cap``
#: field, in the shipped operator guide, and in a table row of ``docs/tools.md``. Honest about what
#: it is: a guard against a REPEAT, not against a name nobody has thought of yet. Its warrant is
#: the same as every other guard here, that this is the wording history the repository actually
#: has, and the three would otherwise be the only reworded places with nothing holding them.
_RETIRED_CAP_NAMES = ("per-instance context cap", "inventory context cap", "macro-context cap")

#: Where the cap can be named at a READER: the served surfaces. Comments are out of scope by the
#: same rule as the phrase guard (an AST has none), and so are ``tests/``, where these names are
#: quoted on purpose to say what was wrong.
_SERVED_MARKDOWN = (Path("src") / "epics_mcp", Path("docs"))


def test_no_served_surface_uses_a_retired_name_for_the_cap() -> None:
    """The three places the phrase guard cannot see, held by name.

    ``hit the ... context cap`` is how a NOTE reports the cap; a description or a guide names it in
    running prose ("Per-display macro-context cap", "resolved under the macro-context cap"), and
    those are exactly where the last two wordings survived the first pass of GB-72. The guide is
    the worse of the two, because it ships as the ``epics-pv://guide`` resource: an assistant reads
    a name there and then greps the notes for it.

    Provably red: put "macro-context cap" back into either the ``find_device`` field description or
    the guide.
    """
    root = _SRC.parent.parent
    offenders: dict[str, list[str]] = {}

    for path in sorted(_SRC.rglob("*.py")):
        hits = [
            name
            for text in _rendered_strings(ast.parse(path.read_text(encoding="utf-8")))
            for name in _RETIRED_CAP_NAMES
            if name in text
        ]
        if hits:
            offenders[path.relative_to(root).as_posix()] = sorted(set(hits))

    for base in _SERVED_MARKDOWN:
        for path in sorted((root / base).rglob("*.md")):
            text = path.read_text(encoding="utf-8")
            if hits := [name for name in _RETIRED_CAP_NAMES if name in text]:
                offenders[path.relative_to(root).as_posix()] = sorted(set(hits))

    assert not offenders, (
        f"retired names for the context cap are back: {offenders}. The cap is called "
        f"'{_CAP_QUALIFIER}context cap' everywhere a reader meets it, see GB-72."
    )


# --------------------------------------------------------------------------------------------
# The same for the OTHER cap (GB-78): the glob cap, whose wording was uniform and held by nothing.
# --------------------------------------------------------------------------------------------

#: How a note REPORTS the glob cap. Same construction as :data:`_CAP_MENTION`, same reason for
#: anchoring on ``hit the``: the tool descriptions mention the glob cap too (GB-72 put it there),
#: but never with this verb, and they are not notes.
_GLOB_MENTION = re.compile(r"hit the ([^.,:;]*?)glob cap", re.S)

#: The opening every glob-cap note carries, measured byte-identical across all four tools. Pinning
#: the whole opening rather than a qualifier covers BOTH failures this repository has had here in
#: ONE assertion: a wrong qualifier cannot appear (the phrase fixes it as empty), and the word
#: ``globbed`` cannot silently revert.
#:
#: That word is the reason this guard exists. ``05b5fc2`` had to replace "template <file>
#: reference(s)" with "globbed" in three tools, because the engine fills ``glob_capped`` from
#: glob-resolved references and skips template edges, so the old word named the wrong thing. Those
#: three fixes were pinned per file, and ``test_device_lookup.py`` says in its own docstring that a
#: FOURTH note carrying the old word would be caught by none of them. It was right, and it stayed
#: right until this guard.
_GLOB_NOTE_OPENING = "globbed <file> reference(s) hit the glob cap"

#: The modules that must each report the glob cap, named rather than counted, for the same reason
#: as :data:`_CAP_NAMING_MODULES`. Shorter than that set by exactly one: ``display_tools.py``
#: DESCRIBES both caps but reports neither, because a description is not a note.
_GLOB_NAMING_MODULES = frozenset(
    {
        Path("services") / "coverage.py",
        Path("services") / "crossplane.py",
        Path("services") / "device_lookup.py",
        Path("tools") / "validate.py",
    }
)


def test_every_glob_cap_note_opens_with_the_same_words() -> None:
    """The second cap, held to one opening, so the twin of GB-72 cannot drift either.

    The four notes are byte-identical today and were held by nothing central: three tools pinned
    the wording in their own test file and two pinned only the substring "glob cap", which
    survives any rewording that keeps those two words. That is the same shape, and the same two
    files, as the gap GB-72 found on the context cap.

    Provably red in both directions the repository has really seen: put "template" back in place
    of "globbed" in any note, or slip a qualifier into "hit the ... glob cap".
    """
    offenders = {
        f"{path.relative_to(_SRC).as_posix()}: {qualifier!r}"
        for path in sorted(_SRC.rglob("*.py"))
        for literal, qualifier in _mentions(path.read_text(encoding="utf-8"), _GLOB_MENTION)
        if _GLOB_NOTE_OPENING not in literal
    }
    assert not offenders, (
        f"glob-cap notes that do not open with the agreed words: {sorted(offenders)}. Every one of "
        f"them reads '{{n}} {_GLOB_NOTE_OPENING}'. The count is of globbed <file> references, not "
        "of template edges, which the engine does not put in glob_capped, see GB-78 and 05b5fc2."
    )


def test_every_tool_that_walks_the_inventory_reports_the_glob_cap() -> None:
    """The non-vacuity floor for the guard above, and the failure it is really about.

    A tool silently dropping its glob-cap note satisfies "all remaining notes agree" perfectly,
    and it is the worse defect: the caller then reads a shortened answer as a complete one. That
    is not hypothetical either, it is what ``find_device`` did until GB-65.
    """
    reporting = {
        path.relative_to(_SRC)
        for path in sorted(_SRC.rglob("*.py"))
        if _mentions(path.read_text(encoding="utf-8"), _GLOB_MENTION)
    }
    assert reporting == _GLOB_NAMING_MODULES, (
        f"modules reporting the glob cap changed: missing {_GLOB_NAMING_MODULES - reporting}, "
        f"unexpected {reporting - _GLOB_NAMING_MODULES}. A tool that walks the inventory and stays "
        "quiet about the glob cap returns a lower bound that reads as complete."
    )


def test_the_glob_detector_ignores_a_description_that_merely_mentions_the_cap() -> None:
    """The detector itself, and this one is a correction rather than a formality.

    A bare ``.*?`` between the two anchors spans whole paragraphs. The ``view`` description of
    ``validate_pvs`` names the context cap early and the glob cap some 400 characters later, so
    the first draft of this pattern matched that entire description and reported it as a
    malformed note. Excluding sentence punctuation from the qualifier fixes it, because a
    qualifier is an attribute and never a sentence.
    """
    note = 'x = f"{n} globbed <file> reference(s) hit the glob cap, so some screens were left out"'
    assert _mentions(note, _GLOB_MENTION) and all(
        _GLOB_NOTE_OPENING in literal for literal, _ in _mentions(note, _GLOB_MENTION)
    )
    # The two real failure shapes.
    assert _mentions('x = "{} template <file> reference(s) hit the glob cap"', _GLOB_MENTION)
    assert _mentions('x = "hit the file-glob cap"', _GLOB_MENTION) == [
        ("hit the file-glob cap", "file-")
    ]
    # A description that names both caps in one paragraph is NOT a note and must not be read as
    # one. This is the case that made the narrowing necessary.
    assert not _mentions(
        'x = "a note fires when you hit the per-display context cap, and a SEPARATE one '
        'reports the glob cap"',
        _GLOB_MENTION,
    )
