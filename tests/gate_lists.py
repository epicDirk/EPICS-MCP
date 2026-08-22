"""Read the ENUMERATIONS this repository's write-gate prose spells out, so a test can compare
them to the gates they describe.

[GQ-132]. Two guards already compare a gate NUMBER to the code: the ``checks`` family in
``tests/test_prose_counters.py`` and the start-condition sweep in
``tests/test_write_gate_contract.py``. Neither compares a LIST, and both say so at their own
site. That gap is not hypothetical: ``CHANGELOG.md`` records the shipped operator guide naming
four of the Olog gate's six checks, and the number beside that list was correct while it
happened. This module is the reading half of the missing check; the comparing half is
``tests/test_gate_lists.py``.

WHAT IT IS NOT, first, because the obvious generalisation is the wrong one and was measured
before it was rejected. It does NOT sweep the tree for lists. Over the tracked ``.py`` and
``.md`` files a sweep finds enumerations by the hundred, and a rule pairing each with the nearest
number was classified over 321 hand-read sites: 92 correct fires against 42 false alarms, i.e. a
precision under seventy percent. ⚠️ Those figures are a DERIVATION, not a measurement, and the
qualifier belongs beside them: agents read the originals and their accuracy is unverified. Nor was
that tree free of wrong numbers, and an earlier version of this sentence claimed it was: this
package's own post-build review answered with two, and ``SITES`` names one of them 300 lines below.
Two of the false alarms are worth naming because they are not fixable by tuning:
``olog_safety.py`` numbers six checks ``0. 1. 2. 3. 3b. 4.``, so a counter reads four where the
prose says six and the prose is RIGHT; and ``operator_guide.md`` names two tools above a
three-item list whose third item begins "A THIRD tool ... only its defaults keep it off that
list", where again both halves are right. So the population here is a TABLE, ``SITES``, not a
sweep. The measurement is written up in ``analysis/gq132-listenzaehler-2026-08-21/`` in the
workspace.

THE ROWS ARE ANCHORED ON A LITERAL, and a missing anchor is a LOUD failure rather than a skipped
row. An anchor stops matching exactly when the passage was restructured, which is when a human
should read it again; silently finding nothing would read identically to "this list was removed",
which is the event the whole module exists to catch. Same policy
``test_write_gate_contract._start_conditions`` states for a raise it cannot attribute.

SHAPES, plural and deliberately uncounted: this module exists to catch a hand-typed figure beside
a list, and the figure that stood here was one, and it was wrong, it said three while ``SITES``
carried five shape values and four readers. ``len({row.shape for row in SITES})`` answers it. The
gate estate writes its lists these ways, and a reader that knew only the first would cover a
fraction of it:

* **adjacent** - a run of sibling markers, each on its own line: the guide's six bullets, the
  contract's five numbered start conditions, the two gate docstrings. The marker accepts a LETTER
  SUFFIX (``3b.``) and does not require the run to start at 1 (``olog_safety`` starts at 0),
  because both are real spellings here and a reader that demanded ``1., 2., 3.`` would miscount
  the very list the CHANGELOG entry is about.
* **ordinal sections** - markers of the form ``**N.`` separated by paragraphs of body text: the
  contract's six requirements sit whole paragraphs apart, first head to last. An adjacency rule
  cannot see this list at all. ⚠️ The two line numbers that stood here were wrong within the hour,
  moved by an edit higher up the same file that this ticket itself made, and the SPAN that
  replaced them was a second hand-typed figure with the same fault. Both are gone, which is the
  argument for anchoring a row on a LITERAL rather than on a position, one paragraph later.
* **marker chain** - an enumeration inside one sentence whose items carry ``(1)``, ``(2)`` markers
  rather than a separator. It has its own reader for a measured reason, see
  :func:`numbered_chain`.
* **chain** - the enumeration inside one sentence, which is the MAJORITY shape in ``server.py``:
  "SIX checks in fixed order: A AND B AND ... AND F", "six checks (a + b + c + d + e + f)",
  "three gate checks (env gate, regex allowlist, rate-limit)". Read from the RUNTIME string, not
  from the file, because the source wraps these across adjacent string literals and the split
  lands wherever the line filled up: the server ``instructions`` chain has a literal boundary in
  the middle of "a test-server URL boundary". The runtime value is also what a model receives,
  so it is the honest subject.

A SUBSET IS NOT A LIST, and a row may exist to say so. ``update_log_entry`` says "all six checks
(two of them, the env gate and the test-server URL boundary, checked BEFORE the round-trip read)":
the parenthesis enumerates a deliberate subset and is marked as one in the prose. Counting it
against the gate would report a true sentence as wrong. Such rows carry ``source=None``, a reason,
and an ``expect_items`` of their own, so ``test_gate_lists`` holds both their anchor AND their
length: a subset that grew into the whole list goes red instead of passing unexamined. ⚠️ That
second half was missing for one commit, and this paragraph promised it anyway; the post-build
review measured the gap.

⛔ THIS SENTENCE COUNTED THOSE ROWS ("two rows exist to say so") AND THE COUNT DIED IN THE COMMIT
AFTER THE ONE THAT WROTE IT: ``461ec66`` gave the ``SECURITY.md`` row a source, leaving one. Two
further copies of the same claim stood in ``tests/test_gate_lists.py`` and are repaired with
[GQ-139], which at first missed this one, the copy in the module that DEFINES the rows. Nothing
counts them, here or there, so the wording no longer does either: ``len([r for r in SITES if
r.source is None])`` answers it, and a figure in prose beside a table it does not read is the
defect this whole module exists to catch.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

from tests.prose_numbers import ProseBlock, iter_blocks

REPO_ROOT = Path(__file__).resolve().parent.parent

#: A list marker at the start of a stripped line. Bullets, and ordinals that may carry a letter
#: suffix, which ``olog_safety.OlogWriteGate`` spells ``3b.`` for a check split out of another.
_MARKER = re.compile(r"^(?:[-*+]|\d{1,2}[a-z]?[.)])\s+\S")

#: An ordinal SECTION head: optionally bold, at the start of a line, body text may follow it for
#: any number of lines before the next head.
_ORDINAL_HEAD = re.compile(r"^(?:\*\*)?(\d{1,2})\.\s+\S")

#: A markdown ATX heading. It ends an ordinal-section run: two numbered lists under two headings
#: are two lists, however their numbering happens to continue.
_HEADING = re.compile(r"^#{1,6}\s")


class AnchorNotFound(AssertionError):
    """The literal a row is anchored on is no longer in the file it names.

    Raised rather than returning an empty list. An anchor stops matching when the passage was
    restructured, and a restructured passage is exactly what a human has to re-read; an empty
    result would be indistinguishable from "the list was deleted", the event this module exists
    to catch.
    """


@dataclass(frozen=True)
class GateList:
    """One enumeration that spells out what a write gate checks."""

    path: str
    lineno: int
    scope: str
    shape: str
    #: Which measurement this list must agree with, or ``None`` for a row that is registered so it
    #: cannot vanish unnoticed but is deliberately not counted. See ``reason``.
    source: str | None
    reason: str
    items: tuple[str, ...]

    def where(self) -> str:
        """``path:line [scope]``, what a failure message prints."""
        return f"{self.path}:{self.lineno} [{self.scope}]"


def _lines(text: str) -> list[str]:
    return text.splitlines()


def adjacent_items(text: str, anchor: str) -> tuple[int, tuple[str, ...]]:
    """The run of sibling markers starting at the line that begins with *anchor*.

    A sibling is a marker line at the SAME indentation. A more deeply indented line is a
    continuation or a sub-list and belongs to the item above it; a blank line does not end the
    run, because both markdown and the docstrings here space their items out. A line at the same
    or lower indentation that carries no marker ends the run.
    """
    lines = _lines(text)
    start = _anchor_line(lines, anchor)
    indent = len(lines[start]) - len(lines[start].lstrip())
    items: list[str] = [lines[start].strip()]
    for line in lines[start + 1 :]:
        if not line.strip():
            continue
        here = len(line) - len(line.lstrip())
        if here > indent:
            continue
        if here == indent and _MARKER.match(line.strip()):
            items.append(line.strip())
            continue
        break
    return start + 1, tuple(items)


def ordinal_sections(text: str, anchor: str) -> tuple[int, tuple[str, ...]]:
    """Ordinal heads ``**N.``/``N.`` at one indentation, however far apart, ascending by one.

    The run ends at the first missing successor or at a markdown heading. Distance is deliberately
    unbounded: the write-gate contract's six requirements span 131 lines, and every alternative
    rule that bounds the gap loses that list, which is one of the two the ticket named.
    """
    lines = _lines(text)
    start = _anchor_line(lines, anchor)
    first = _ORDINAL_HEAD.match(lines[start].strip())
    if first is None:
        raise AnchorNotFound(f"the anchor {anchor!r} is not an ordinal head: {lines[start]!r}")
    indent = len(lines[start]) - len(lines[start].lstrip())
    items: list[str] = [lines[start].strip()]
    expected = int(first.group(1)) + 1
    for line in lines[start + 1 :]:
        if _HEADING.match(line):
            break
        stripped = line.strip()
        head = _ORDINAL_HEAD.match(stripped)
        if head is None or len(line) - len(line.lstrip()) != indent:
            continue
        if int(head.group(1)) != expected:
            break
        items.append(stripped)
        expected += 1
    return start + 1, tuple(items)


def chain_items(text: str, anchor: str, terminator: str, separator: str) -> tuple[str, ...]:
    """The parts of a one-sentence enumeration, between *anchor* and *terminator*.

    *anchor* ends immediately before the first part and *terminator* begins immediately after the
    last one; both are literals and both must be present, so a rewording fails loudly instead of
    silently returning a shorter chain. The separator is a literal too rather than a pattern: the
    chains here are separated as differently as " AND ", " + ", ", ", " **and** " and ", and ",
    and a pattern loose enough to cover them all would also split the commas INSIDE a part.

    ⛔ A LITERAL SEPARATOR HAS THE SAME BLINDNESS ONE LAYER DOWN, and this is the honest limit of
    the shape. A row whose separator is ", " counts a comma INSIDE an item as a boundary, so an
    edit that both drops a check and adds a comma to a surviving item holds the length and passes.
    Named rather than closed: closing it means registering a fragment per item and comparing
    membership, which is a bigger contract than counting and is not what this ticket bought.
    :func:`numbered_chain` is the shape where the problem does not arise, and the two ", " rows in
    ``SITES`` say at their own site that they carry it.
    """
    flat = " ".join(text.split())
    begin = flat.find(anchor)
    if begin < 0:
        raise AnchorNotFound(f"the chain anchor {anchor!r} is gone")
    begin += len(anchor)
    end = flat.find(terminator, begin)
    if end < 0:
        raise AnchorNotFound(f"the chain terminator {terminator!r} is gone after {anchor!r}")
    return tuple(part.strip() for part in flat[begin:end].split(separator) if part.strip())


def _anchor_line(lines: list[str], anchor: str) -> int:
    """Index of the single line whose stripped form starts with *anchor*.

    Exactly one, asserted rather than assumed: a second match means the anchor stopped being an
    identity, and picking the first would silently move the row to a different list.
    """
    hits = [index for index, line in enumerate(lines) if line.strip().startswith(anchor)]
    if len(hits) != 1:
        raise AnchorNotFound(
            f"the anchor {anchor!r} matches {len(hits)} lines, expected exactly one"
            + (f" (lines {[hit + 1 for hit in hits]})" if hits else "")
        )
    return hits[0]


def qualname_lineno(text: str, qualname: str) -> int:
    """The line a class or function is defined on, for a row read from a runtime string.

    A chain read from ``__doc__`` or from ``build_instructions`` has no line of its own: the
    source wraps it across string literals, so the definition's line is the stable address to
    report. What ``test_every_gate_size_scope_has_a_list_row`` matches on is the QUALNAME, not this
    line, and that is what the two tables share. ⚠️ They share it and no more: since [GQ-139] the
    number family also keys on a row's ``lead``, so a scope can carry two of its rows and one of
    ours. The comparison is deliberately one-directional and stays correct under that.
    """
    wanted = qualname.split(".")[-1]
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) and (
            node.name == wanted
        ):
            return node.lineno
    raise AnchorNotFound(f"no class or function named {qualname!r}")


def literal_lineno(text: str, needle: str) -> int:
    """The line a literal fragment sits on, for a row that is a comment rather than a docstring."""
    for index, line in enumerate(_lines(text), start=1):
        if needle in line:
            return index
    raise AnchorNotFound(f"the locator {needle!r} is gone")


def numbered_chain(text: str, anchor: str, terminator: str, first: int = 1) -> tuple[str, ...]:
    """Parts of a one-sentence chain whose items are marked ``(1)``, ``(2)``, ....

    A marker-driven reader rather than a separator-driven one, and the difference is a false
    alarm avoided rather than a preference. The write-gate contract's six points are separated by
    ``"; ("``, and one point that ever contained that pair would split into seven and go red on a
    correct sentence. Ascending markers cannot: a missing marker SHORTENS the run, which is the
    finding, and a stray one is not read at all.
    """
    flat = " ".join(text.split())
    begin = flat.find(anchor)
    if begin < 0:
        raise AnchorNotFound(f"the chain anchor {anchor!r} is gone")
    begin += len(anchor)
    end = flat.find(terminator, begin)
    if end < 0:
        raise AnchorNotFound(f"the chain terminator {terminator!r} is gone after {anchor!r}")
    body = flat[begin:end]
    starts: list[int] = []
    number = first
    cursor = 0
    while True:
        found = body.find(f"({number})", cursor)
        if found < 0:
            break
        starts.append(found)
        cursor = found + 1
        number += 1
    if not starts:
        raise AnchorNotFound(f"no ``({first})`` marker between {anchor!r} and {terminator!r}")
    bounds = [*starts, len(body)]
    return tuple(body[bounds[i] : bounds[i + 1]].strip(" ;") for i in range(len(starts)))


@dataclass(frozen=True)
class _Row:
    """One registered site. ``source`` names what its length must equal, or is ``None``."""

    path: str
    shape: str
    source: str | None
    reason: str
    scope: str = ""
    #: Line-based shapes: the literal the first item's line must start with.
    anchor: str = ""
    #: Chain shapes: one ``(anchor, terminator, separator)`` per PART. More than one part where
    #: the prose splits its own list across two sentences, which ``docs/safety.md`` does: four
    #: checks chained with ``**and**``, then "Two further checks ... : X, and Y", then the total.
    #: Reading only the first part would report six as four, i.e. exactly the CHANGELOG defect,
    #: with the accusation pointing at the correct half.
    parts: tuple[tuple[str, str, str], ...] = ()
    #: For a ``source=None`` row: how many items its enumeration must still have. An uncounted
    #: row is exempt from the GATE comparison, never from being counted at all. Without this the
    #: subset row could grow into the whole list and pass, which the module docstring promised it
    #: could not, one commit before it was true.
    expect_items: int | None = None
    #: True for the ONE row that DEFINES a count no code holds. The write-gate contract's six
    #: requirements are a specification, so nothing derives them; the spec's own ordinal run is
    #: the definition, and the other contract-point sites are compared to it. Deleting a
    #: requirement shortens the run and reddens those sites, so the canonical row is guarded by
    #: its dependants rather than left unguarded.
    canonical: bool = False


#: Every enumeration in the write-gate estate that spells out what a gate checks, plus the ones
#: deliberately NOT counted, which are registered so they cannot quietly change shape.
#:
#: ⚠️ The list is a TABLE and not a sweep, and the reason is measured rather than aesthetic; the
#: module docstring carries it. What follows from that is stated here because it is the honest
#: limit: a NEW gate-size sentence with a new list is invisible to this table until somebody adds
#: a row. That is why ``test_gate_lists.test_every_gate_size_scope_has_a_list_row`` exists, and it
#: is why the partition is asserted against ``_GATE_SIZE_SCOPES`` rather than trusted.
SITES: tuple[_Row, ...] = (
    _Row(
        path="src/epics_mcp/operator_guide.md",
        shape="adjacent",
        source="olog_safety.py",
        reason="the shipped guide's Olog list, the one CHANGELOG.md records losing two items",
        scope="Olog write posture (all four write tools)",
        anchor="- **Non-empty target logbooks.**",
    ),
    _Row(
        path="src/epics_mcp/operator_guide.md",
        shape="chain",
        source="safety.py",
        reason="the guide's posture section, the PV gate's three checks as a comma chain",
        parts=(("passes **three** gate checks (", ") and needs an audit log", ", "),),
    ),
    _Row(
        path="src/epics_mcp/operator_guide.md",
        shape="chain",
        source="olog_safety.py",
        reason="the guide's error-signature section, the Olog gate's six checks as a comma chain",
        parts=(
            ("One code for all six checks of that gate (", "), because they are one gate", ", "),
        ),
    ),
    _Row(
        path="src/epics_mcp/olog_safety.py",
        shape="adjacent",
        source="olog_safety.py",
        reason="the gate class's own docstring, numbered 0/1/2/3/3b/4",
        scope="OlogWriteGate",
        anchor="0. Non-empty logbooks:",
    ),
    _Row(
        path="src/epics_mcp/safety.py",
        shape="adjacent",
        source="safety.py",
        reason="the PV gate class's own docstring",
        scope="SafetyLayer",
        anchor="1. Environment gate:",
    ),
    _Row(
        path="docs/write-gate-contract.md",
        shape="adjacent",
        source="safety.py:start-conditions",
        reason="the five start conditions, the list [GQ-130] made and left uncompared",
        scope="Write-gate contract",
        anchor="1. the PV-name allowlist pattern must COMPILE;",
    ),
    _Row(
        path="docs/write-gate-contract.md",
        shape="ordinal",
        source="contract-points",
        reason="the specification's six requirements, spanning 131 lines of body text",
        scope="Write-gate contract",
        anchor="**1. An environment on/off gate, default OFF.**",
        canonical=True,
    ),
    _Row(
        path="tests/test_write_gate_contract.py",
        shape="numbered",
        source="contract-points",
        reason="the contract's six points restated inline, the copy a reader of the test meets",
        parts=(("Its six points are: ", ".", ""),),
    ),
    _Row(
        path="src/epics_mcp/server.py",
        shape="chain",
        source="olog_safety.py",
        reason="create_log_entry's tool description, an AND-chain",
        parts=(("SIX checks in fixed order: ", "; ALLOW_PV_WRITE is untouched", " AND "),),
    ),
    _Row(
        path="src/epics_mcp/server.py",
        shape="chain",
        source="olog_safety.py",
        reason="add_log_attachment's tool description, a plus-chain",
        parts=(("the same six checks (", "): the env gate", " + "),),
    ),
    _Row(
        path="src/epics_mcp/server.py",
        shape="chain",
        source="safety.py",
        reason=(
            "the consent-hint comment beside set_pv_value's annotations. ⚠️ Separator ', ': a "
            "comma added INSIDE one item while another is dropped holds the length, see "
            "chain_items"
        ),
        parts=(("three gate checks (", ") plus the mandatory audit", ", "),),
    ),
    _Row(
        path="src/epics_mcp/server.py",
        shape="instructions",
        source="olog_safety.py",
        reason=(
            "the server instructions, the chain a model reads before it picks a tool. ⚠️ Separator "
            "', ', same blindness as the row above, see chain_items"
        ),
        scope="build_instructions",
        parts=(("behind its OWN gate (", "; the author is", ", "),),
    ),
    _Row(
        path="docs/safety.md",
        shape="chain",
        source="olog_safety.py",
        reason="the approval page's Olog list, split by its own prose into four plus two",
        parts=(
            ("(the last two MUTATE an existing entry), need ", ". Two further checks", " **and** "),
            ("because they are not configuration: ", ". **Six checks in all**", ", and "),
        ),
    ),
    _Row(
        path="src/epics_mcp/server.py",
        shape="chain",
        source=None,
        reason=(
            "update_log_entry names six and enumerates a SUBSET of two, and says so: 'two of "
            "them, ... checked BEFORE the round-trip read'. Counting it would report a true "
            "sentence as wrong. Its LENGTH is still held, by ``expect_items``, so the subset "
            "cannot grow into the whole list unexamined."
        ),
        parts=(("all six checks (two of them, ", ", checked BEFORE", " and "),),
        expect_items=2,
    ),
    _Row(
        path="SECURITY.md",
        shape="chain",
        source="olog_safety.py-over-safety.py",
        reason=(
            "the security page's statement of what the Olog gate has that the PV gate does not. "
            "This row was source=None until the ticket's own follow-up, on the argument that no "
            "gate count is the right comparison for a DIFFERENCE. That was half right: no single "
            "gate count is, but the difference between the two IS derivable, and the sentence "
            "here said 'extra two' while the gates differ by three. A difference nobody holds "
            "against its source is the class this module exists for, so it is held. "
            "⚠️ Separator ' and ', see chain_items"
        ),
        parts=(("What the Olog gate has on top is ", ".", " and "),),
    ),
)


def _block_for(path: str, anchor: str) -> ProseBlock:
    """The comment run or docstring paragraph in *path* that carries *anchor*.

    Reuses ``prose_numbers.iter_blocks`` rather than re-reading the file: it already flattens a
    wrapped comment run and a wrapped docstring paragraph, already strips the ``#``, and already
    keys the result by the enclosing qualname. A second reader for the same question would be a
    second thing to keep right, which is the reason the number guard gives for borrowing its own
    helper instead of re-deriving it.
    """
    hits = [
        block
        for block in iter_blocks([(path, REPO_ROOT / path)])
        if anchor in " ".join(block.text.split())
    ]
    if len(hits) != 1:
        raise AnchorNotFound(
            f"{path}: the chain anchor {anchor!r} sits in {len(hits)} blocks, expected exactly one"
        )
    return hits[0]


def _read(row: _Row) -> GateList:
    """One row, read from the tree."""
    if row.shape in {"adjacent", "ordinal"}:
        text = (REPO_ROOT / row.path).read_text(encoding="utf-8-sig")
        reader = adjacent_items if row.shape == "adjacent" else ordinal_sections
        lineno, items = reader(text, row.anchor)
        return GateList(row.path, lineno, row.scope, row.shape, row.source, row.reason, items)

    if row.shape == "instructions":
        # The one row read from a RUNTIME string. The chain is built by concatenating adjacent
        # string literals, and the source split lands mid-item ("a " + "test-server URL
        # boundary"), so no file-based reader can see it whole. It is also the text a model
        # receives, which makes the runtime value the honest subject rather than a convenience.
        from epics_mcp import server

        anchor, terminator, separator = row.parts[0]
        items = chain_items(server.build_instructions(True), anchor, terminator, separator)
        text = (REPO_ROOT / row.path).read_text(encoding="utf-8-sig")
        lineno = qualname_lineno(text, row.scope)
        return GateList(row.path, lineno, row.scope, row.shape, row.source, row.reason, items)

    block = _block_for(row.path, row.parts[0][0])
    chained: tuple[str, ...] = ()
    for anchor, terminator, separator in row.parts:
        part = _block_for(row.path, anchor) if anchor != row.parts[0][0] else block
        chained += (
            numbered_chain(part.text, anchor, terminator)
            if row.shape == "numbered"
            else chain_items(part.text, anchor, terminator, separator)
        )
    return GateList(
        row.path,
        block.lineno,
        row.scope or block.qualname,
        row.shape,
        row.source,
        row.reason,
        chained,
    )


def read_all() -> tuple[GateList, ...]:
    """Every registered site, read from the tree, in table order."""
    return tuple(_read(row) for row in SITES)
