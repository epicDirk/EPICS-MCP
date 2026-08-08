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
* **Uniqueness.** A source guard that keeps the read in one place, so a fifth consumer cannot grow
  its own copy and drift again. Equality alone would not survive the next tool.

⚠ ``glob_capped`` is a tuple of ``(source display, raw <file> target)`` PAIRS, and every tool
counts PAIRS. Counting distinct SOURCES is the plausible-looking alternative that reports a smaller
number for the same walk, and it is exactly the mutant this file exists to catch. The fixture
vectors below therefore carry more pairs than sources, and a third test asserts that property of
the fixtures themselves: a blind fixture (one pair per source) would leave the mutant green while
looking like a test.
"""

from __future__ import annotations

import ast
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
