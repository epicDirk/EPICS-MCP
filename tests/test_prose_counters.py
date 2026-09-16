"""S32: the numbers this repository's prose NAMES are compared to the sets they describe.

Comments, docstrings and the SHIPPED operator guide state sizes: "all 22 schemas", "7 (resp. 9)
rows", "TEN of the twelve", and until now nothing checked a single one of them. The cost was
measured, not feared: the S29 11th typing target pulled 18 counters over by hand, two adversarial
QA rounds found 8 of them still wrong, and one had already been wrong before that build began.
The reading half of this guard is ``tests/prose_numbers.py``; this module is the comparing half.

WHAT IS PROMISED, in four parts, because a vaguer promise would be the same defect one layer up:

* **Derived**: every claim in ``_CLAIMS`` is compared to a set that is computed here and now. A
  wrong number goes red with its file, line and enclosing scope. Nothing in ``_CLAIMS`` may carry a
  typed-in expectation; ``test_no_claim_hard_codes_its_expectation`` enforces that.
* **Inventoried**: every OTHER phrase the detector finds is listed in ``_FROZEN`` with a reason.
  Those are not verified: a new one, or a vanished one, goes red (``test_inventory_is_partitioned``
  and ``test_inventory_size_is_pinned``), but their VALUE is nobody's promise. Most are historical
  anchors, runtime measurements, or claims about test bodies, which no constant can settle.
* **Out of scope**: the detector pairs a number with one of a closed list of collection nouns, or
  reads the ``N of the M`` shape. It does not see every statement about a size, and it does not see
  numbers inside f-string assertion messages at all. The closed list lives in
  ``prose_numbers.COLLECTION_NOUNS`` and is the whole of the coverage claim.
* **Out of REACH, which is a different thing and was the larger one**: a file this module does not
  name in ``_WATCHED`` is invisible however well the patterns fit it. Measured for [GQ-123]: of the
  seventeen wrong numbers [GQ-117] repaired in one day, NOT ONE sat where this guard was looking,
  and three sat at a place its patterns match exactly, in a file nobody had listed. The criterion
  that decides the list, and the arithmetic that keeps two large test modules OUT of it, is written
  out above ``_WATCHED``.

Which claims are DERIVED and which are merely inventoried was decided by measurement, not taste: a
``git`` pickaxe over the real S29 commits shows the drift lives in five families, the typed-tool
cardinality, the untyped remainder, the explicit rows of ``_ALWAYS_PRESENT_BY_TOOL``, the element
schema split, and the declared arrays, plus the per-tool "EXACTLY the N mapped fields" line every
typing step adds a new one of. Lane counts and the "four paths" family have not moved once in the
recorded history of the file.

Every measurement here reads a module constant or AST-scans a source file. None of them takes a
length from ``mcp.list_tools()``: that answers with FEWER tools in the core-only lane than in the
full one, so a count taken there would pass locally and break the core-only CI, the trap
``tests/test_server.py`` warns about at its own ``_TYPED_OUTPUT_TOOLS``. (This paragraph
deliberately names no figure. A module that derives the lane counts for five other files must not
hand-type them in its own docstring, where nothing would ever check them.)

⚠️ One TEST does read the wire, since [GQ-405]: the union reader is held against the schema each
declared array row advertises. It takes no LENGTH, so the sentence above stands; what changes is
the cost, because reaching the wire pulls the server stack and through it p4p, numpy and OpenBLAS.
Measured on 2026-09-16: the module alone runs in about 36 s, 25 s of which is the provenance
tracer.
"""

from __future__ import annotations

import ast
import functools
import inspect
import json
import re
import sys
import textwrap
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from tests import prose_numbers as pn
from tests import test_server as ts
from tests import test_write_gate_contract as write_gate
from tests.prose_numbers import ProseBlock, ProseSite, iter_blocks, iter_sites, parse_count
from tests.wire_tools import wire_tools_by_name

_TESTS = Path(__file__).resolve().parent
_SRC = _TESTS.parent / "src" / "epics_mcp"

# The files whose prose is watched.
#
# THE CRITERION, written down with [GQ-123] because until then it was implicit and read as grown.
# A file is watched when all three hold:
#
#   1. it carries at least one sentence whose number names a set THIS repository computes, so a
#      derivation can be written at all. Reach without derivation buys nothing: an _FROZEN row
#      guards the EXISTENCE of a phrase, never its value, and every drift measured on this guard so
#      far happened while the phrase stood still and the world moved underneath it;
#   2. somebody reads it who cannot check it, a model, an operator, or the next author of the code,
#      as opposed to the release history, whose figures MUST stay as they were written;
#   3. its site set moves rarely enough that the bookkeeping does not eat the check. Measured per
#      file as the share of its commits that add or remove a size-naming phrase, because that is
#      what turns test_inventory_is_partitioned red at all. ⚠️ That share is an UPPER bound on the
#      false-alarm rate and not the rate itself: some of those commits move the set BECAUSE a
#      number was wrong, and there the red is the guard working. Nothing here measures the split.
#
# Applied rather than asserted. Every share below was MEASURED on 2026-08-21 over that file's
# commits since 2026-07-01, and is written in the past tense on purpose: a churn share is a
# reading of a history, not a property of the file, and a figure in the present tense here would
# be the very defect this module exists to catch. Re-derive them, do not trust them:
# `analysis/gq123-waechter-population-2026-08-21/skripte/kandidaten.py` in the workspace.
#
#   * `checkers_olog.py` came in from a roadmap entry, and `checkers.py`, `archiver.py` and
#     `server.py` because they carry sentences that DUPLICATE a number watched in test_server.py.
#     A duplicate nobody compares is how the canonical side drifts while the test side is dutifully
#     corrected. That reason still holds and is condition 1 in its earliest form.
#   * `operator_guide.md` joins with [GQ-123]. It meets the SAME duplicate rule three times over,
#     "four display-aware tools" is the figure `_display_tools` already checks in three other
#     places, and it is the text a model reads. 17 sites when it joined, and its site set moved on
#     13 of 82 commits. What had kept it out was that `iter_blocks` called `ast.parse` and could
#     open nothing but `.py`. ([GQ-126] took it to 21, see `_INVENTORY_SIZES`.)
#   * `display_tools.py` joins in the same package, and it is here because [GQ-123]'s own QA
#     refuted the sentence that used to stand above: "the only thing that had ever kept the guide
#     out was the reader". This file is `.py`, the reader could always open it, it names the
#     display-tool count THREE times, and its site set moved on 0 of 18 commits. It was simply
#     never listed. For the Python half of the tree the criterion had not been applied at all, and
#     that is the honest answer to "was this list grown": the markdown half was blocked by the
#     reader, the Python half by nobody having looked.
#   * `SECURITY.md` joins with [GQ-139], the cheaper of the two MARKDOWN files the criterion has
#     admitted (`display_tools.py` above is cheaper than either). Measured on the tree as it stood
#     BEFORE the joining commit: 4 sites, and a site set that had moved on 2 of the 17 commits then
#     in range. Both figures are moved BY that commit, to 5 and 3 of 18, because it gave the Olog
#     width its noun; the viewpoint is named here because the first draft of this entry mixed the
#     two and read as if the file were cheaper than it is. ⚠️ Two of the three figures above are
#     also not measured the way `display_tools.py`'s are: its denominator excludes the commit that
#     created it, these include it. Re-derive rather than compare across the entries.
#     Condition 2 is why it matters more than the arithmetic suggests: it is the page a security
#     reviewer reads before approving a deployment, and it states BOTH write-gate widths.
#     ⚠️ Condition 1 has NOT held all along, and the first draft of this entry said it had: the
#     file was created on 2026-07-25 with no size-naming phrase at all, and its first DERIVABLE one
#     arrived on 2026-08-15. What is true is the second half, that nobody applied the criterion
#     once it did hold, through three tickets that each looked at this family.
#     ⚠️ What kept it out was not only that: both gate widths sit in ONE
#     markdown section, and until this ticket the gate-size family could key only one module per
#     (file, scope), so a row for this file would have accused the true Olog sentence against the
#     PV gate's three. The `lead` field at `_GATE_SIZE_SCOPES` is what made the file admissible.
#     ⚠️ AND IT CONCENTRATES THE MARKDOWN KEY'S WEAKNESS: all five sites hang off the single
#     heading "Security posture", so inserting one `###` inside that section, with no word of prose
#     changed, re-keys all five at once and turns three tests red with about ten messages. They do
#     print the NEW heading, but none of them says that a heading moved, and the claim message
#     advises rewording the prose, which would be the wrong repair. That is a property of the
#     markdown key rather than of this file, and it is written here because this is where it bites
#     hardest.
#   * `tests/test_guide_matches_code.py` is REFUSED: 47 table rows to sort by hand, its site set
#     moved on 15 of 40 commits, and the forward yield is about ONE derivable number, because
#     [GQ-117] repaired two of its three wrong numbers by DE-NUMBERING and those sites are gone.
#     ⚠️ Two honest weaknesses in that trade, both named rather than smoothed over. The yield is
#     extrapolated from a sample of THREE, observed on one day, and nobody has read the other 44
#     rows to see how many are derivable. And "moved on 15 of 40" is a bookkeeping rate, not a
#     false-alarm rate: at least two of those 15 were [GQ-117]'s own repair commits, where going
#     red would have been the guard working. So this is a judgement on weak evidence, not
#     arithmetic. `tests/test_doctor.py` (44 rows, 27 of 65 commits) is refused the same way.
#   * `CHANGELOG.md` is refused on condition 2: a release entry that said "22 schemas" must keep
#     saying it. It is also the one file in the tree whose section titles repeat, which the
#     markdown key rests on not doing (`prose_numbers.ambiguous_headings`).
#   * `docs/known-limits.md` is refused on condition 3: 24 sites, and its site set moved on 28 of
#     52 commits, better than one commit in two. Entry 1 of that same page argues the point at
#     length and its own figures are the evidence for it. (No superlative is claimed here, and the
#     first draft did claim one and was wrong: this module's own file moved on 10 of 17.)
#
# This guard's own two files are NOT watched, and the honest reason is a trade-off, not a triumph.
# Watching them surfaces dozens of phrases, the overwhelming majority QUOTATIONS of the estate's
# prose used to explain the design ("all 22 schemas", "TEN of the twelve"); inventorying those
# would add rows saying "this is an example, not a claim", a blanket exemption wearing a table's
# clothes. The worst offender, a core-lane tool count in the docstring of the module that computes
# it, was removed rather than inventoried.
#
# What is NOT claimed: that nothing here can rot. These files still name derived values in prose,
# and several of them use nouns the detector does not know ("constants", "words", "pairings"), so
# watching the files would not catch those anyway. Closing that properly is its own piece of work
# and is recorded as such, it is not silently finished.
_WATCHED: tuple[tuple[str, Path], ...] = (
    ("tests/test_server.py", _TESTS / "test_server.py"),
    ("services/checkers_olog.py", _SRC / "services" / "checkers_olog.py"),
    ("services/checkers.py", _SRC / "services" / "checkers.py"),
    ("tools/archiver.py", _SRC / "tools" / "archiver.py"),
    ("server.py", _SRC / "server.py"),
    ("operator_guide.md", _SRC / "operator_guide.md"),
    ("display_tools.py", _SRC / "display_tools.py"),
    ("SECURITY.md", _TESTS.parent / "SECURITY.md"),
)


@dataclass(frozen=True)
class _Claim:
    """A phrase that names a size, the set that decides the size, and WHICH SOURCES that set is.

    ``reads`` is the field this guard was missing, and its absence was the root of every stand-in
    measure an outside QA found here: a claim held ``(label, pattern, callable)``, the measure was
    an opaque thunk returning a number, and the only structural check on it searched the derivation
    for THE ANSWER, the one property that says nothing about which set was read. The cheapest
    expression that happens to answer correctly today was therefore always admissible.

    THE VOCABULARY, in full, because a declaration nobody can read is a comment:

    * ``".../name.py"``: a source file the measure must hand to ``_parsed``. Matched on a
      path-separator boundary, never as a bare suffix: ``"server.py"`` would otherwise be satisfied
      by parsing ``tests/test_server.py``. Spelled the way ``_WATCHED`` spells its labels.
    * ``"_SOME_CONSTANT"``: a name the measure must read off the ``test_server`` module.
    * ``"__dict__"``: the measure scans that module's NAMESPACE rather than naming a constant,
      which is what a suffix scan over ``vars(ts)`` does. A deliberately weaker declaration, and it
      says so by being a different word.

    WHAT ``reads`` DOES AND DOES NOT PROVE. Traced, it establishes that the measure touched the
    object the claim names. It does not establish that the SENTENCE means that object, that stays a
    human reading, and it does not establish that the answer DEPENDS on what was touched: a measure
    could read a constant and ignore it. Both limits are stated here because no construct closes
    them.

    ``path`` narrows a claim to ONE watched file, and it closes an asymmetry rather than adding a
    convenience: ``_FROZEN`` identifies a phrase by (file, scope, phrase, value) while a claim
    identified itself by scope alone. For a scope that exists once that is the same thing; for
    ``<module>`` it is not, it is the scope of every watched PYTHON file, so a claim about the PV
    gate's size would also judge a sentence about the LOGBOOK gate in another file's module
    header. Measured with [GQ-126]: no such collision exists today, which is why the field is
    optional and every older claim keeps matching across files, several of them deliberately (the
    duplicate rule that put ``checkers.py`` and ``archiver.py`` in ``_WATCHED`` depends on it).
    ⚠️ ``path`` is not the finest key there is, and [GQ-139] is where that stopped being an
    academic point: a scope may state TWO gate widths, which (file, scope) cannot express at all.
    The gate-size family answers that with its own ``lead``; nothing here does.
    """

    label: str
    phrase: re.Pattern[str]
    measure: Callable[[], int]
    reads: tuple[str, ...]
    scope: str = ""
    path: str = ""

    def applies_to(self, block: ProseBlock) -> bool:
        return (not self.scope or block.qualname == self.scope) and (
            not self.path or block.path == self.path
        )


def _claim(
    label: str,
    pattern: str,
    measure: Callable[[], int],
    *,
    reads: tuple[str, ...],
    scope: str = "",
    path: str = "",
) -> _Claim:
    """*reads* is keyword-only and REQUIRED, so a new claim cannot be added without declaring it."""
    return _Claim(label, re.compile(pattern, re.IGNORECASE), measure, reads, scope, path)


def _derivation_source(measure: Callable[[], int]) -> str:
    """A claim's derivation as source text, so a typed-in answer THERE is visible too.

    Parsed, never split on text. A NAMED function yields its body WITHOUT the docstring: those
    docstrings explain which set they count, and accusing them would punish the explanation the
    rest of this module asks for. A LAMBDA yields its body, its enclosing statement carries the
    claim's own pattern, which legitimately contains digits. Only executable code is searched.

    THE ORDER OF THOSE TWO IS LOAD-BEARING and it used to be the other way round. The lambda walk
    ran first over the WHOLE parsed statement, so a named measure containing a lambda anywhere:
    a ``key=lambda ...`` in a sort, say, had its entire body discarded and only that lambda's body
    inspected. Latent on the delivered table, measured: NONE of the named measures contains one,
    and a trap the moment one does, because the discarded body is exactly where a typed-in answer
    would sit. A named function is now matched before anything is walked.
    ⚠️ That sentence used to read "0 of 79 named measures", and the 79 had rotted: it was never the
    number of NAMED measures in the first place, and the table has grown twice since. Only the zero
    was ever load-bearing, and it is the half nothing can rot silently, because a lambda appearing
    in a named measure is what the reordering above exists to survive. [GQ-126] removed the
    denominator rather than refreshing it; re-derive it by walking ``_CLAIMS`` if you need it.

    An unreadable source is an ERROR, not an empty string. ``functools.partial``, a callable
    object and a dynamically compiled function all raise here, and returning "" for them made the
    anti-hard-coding check silently inspect nothing, an acquittal indistinguishable from a
    verdict. ``partial(_paths_rows, _DISCOVER_CONFORMANCE)`` is a straight-faced refactor of nine
    claims in this file, and it would have taken all nine out of the guard's reach at once.
    """
    try:
        source = textwrap.dedent(inspect.getsource(measure))
    except (OSError, TypeError) as unreadable:
        raise AssertionError(
            f"the derivation of {getattr(measure, '__name__', measure)!r} cannot be read, so "
            "test_no_claim_hard_codes_its_expectation would inspect nothing and pass. A measure "
            "must be a plain function or lambda, not a functools.partial, not a callable object."
        ) from unreadable
    try:
        node: ast.AST = ast.parse(source).body[0]
    except SyntaxError:
        # ``inspect.getsource`` on a lambda hands back its enclosing statement cut at the first
        # NEWLINE, which is a FRAGMENT. Most such fragments parse anyway,
        # ``_claim(..., lambda: x),`` is a one-element tuple, so the branch below is NOT the common
        # case the old comment claimed. It is reached by one layout: arguments on a single wrapped
        # line together with a
        # ``scope=`` keyword, where the fragment is a tuple containing a keyword argument.
        # ``test_the_derivation_reader_handles_a_fragment`` drives exactly that.
        node = ast.parse(f"_({source.strip().rstrip(',')})").body[0]
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        body = node.body[1:] if ast.get_docstring(node) else node.body
        return "\n".join(ast.unparse(statement) for statement in body)
    for inner in ast.walk(node):
        if isinstance(inner, ast.Lambda):
            return ast.unparse(inner.body)
    return source


# --- the provenance tracer --------------------------------------------------------------------


class _SourceRecorder:
    """A DELEGATING stand-in for the ``test_server`` module that records every name read off it.

    ``__getattribute__`` and not ``__getattr__``, and that is a measurement rather than a
    preference: ``vars(obj)`` reads ``obj.__dict__`` directly and never reaches ``__getattr__``, so
    a ``__getattr__`` proxy handed ``_always_present_constants`` its own instance attributes instead
    of the module namespace. Measured, the twelve ``*_ALWAYS_PRESENT`` constants then counted as 0
    and the subtraction derived from them as -2, a tracer that changes the answer it is watching is
    worse than no tracer, because its verdict looks like a finding.

    It delegates rather than stubs for a related reason: ``_none_valued`` iterates the mapping it is
    handed, so a stub would raise where the real module measures.
    """

    def __init__(self, module: ModuleType, seen: set[str]) -> None:
        object.__setattr__(self, "_module", module)
        object.__setattr__(self, "_seen", seen)

    def __getattribute__(self, name: str) -> Any:
        module = object.__getattribute__(self, "_module")
        object.__getattribute__(self, "_seen").add(name)
        return getattr(module, name)


def _memoized_helpers() -> tuple[Any, ...]:
    """Every memoized helper in this module, DISCOVERED rather than listed.

    A hand-kept list would blind the tracer the day someone adds a helper and forgets to extend it:
    and noticing a measure that reads nothing is the tracer's entire job. Discovery also keeps the
    count out of the prose, which matters here: this file is deliberately unwatched, so a number
    written into it rots exactly the way the numbers it guards do.

    WHICH helpers this actually protects, measured rather than assumed: the ones BETWEEN a claim and
    the two seams. ``_parsed`` itself is not among them, the tracer replaces it with a wrapper that
    records the path BEFORE delegating, so a warm ``_parsed`` still reports its file. Hiding
    ``_parsed`` from this scan therefore changes nothing (probed), while hiding an intermediate such
    as ``_tools_declaring_parameter`` makes the second claim that uses it return a cached answer
    having touched nothing at all, and the tracer says so.
    """
    return tuple(
        value
        for value in vars(sys.modules[__name__]).values()
        if callable(value) and hasattr(value, "cache_clear")
    )


#: Names a measure reads only as a side effect of AST-parsing a module, never as a source itself.
_TRACE_ARTEFACTS: frozenset[str] = frozenset({"__file__"})


def _trace_measure(measure: Callable[[], int]) -> tuple[int, frozenset[str], frozenset[str]]:
    """Run *measure* with this module's two bottlenecks recorded; report what it touched.

    TWO SEAMS ARE ENOUGH, and that is a measured property of this module rather than a hope:
    ``_parsed`` is the only file reader in it (``test_the_tracer_can_see_every_source`` asserts
    exactly that, so the precondition travels with the tracer), and every constant arrives through
    the ``test_server`` module object. Should either stop holding, the tracer goes blind, which is
    why the assertion is a test and not a comment.

    Every memoized helper is cleared first and afterwards. A measure whose answer is already cached
    touches nothing at all, so a tracer reading warm caches would report every claim as reading
    nothing, green, and about as informative as a coin.
    """
    module = sys.modules[__name__]
    paths: set[str] = set()
    attributes: set[str] = set()
    real_parsed = _parsed

    def recording_parsed(path: Path) -> ast.Module:
        paths.add(Path(path).as_posix())
        return real_parsed(path)

    helpers = _memoized_helpers()
    try:
        for helper in helpers:
            helper.cache_clear()
        with pytest.MonkeyPatch.context() as patcher:
            patcher.setattr(module, "_parsed", recording_parsed)
            patcher.setattr(module, "ts", _SourceRecorder(ts, attributes))
            answer = measure()
    finally:
        for helper in helpers:
            helper.cache_clear()
    return answer, frozenset(paths), frozenset(attributes)


def _provenance_faults(claim: _Claim) -> list[str]:
    """Where *claim*'s declared sources and the sources it actually touches disagree.

    BOTH DIRECTIONS for the module attributes, and the second one is what keeps ``reads`` a
    declaration rather than a lower bound: a measure free to read an extra constant without saying
    so is precisely the "cheapest expression that answers correctly today" this field exists to
    stop.

    Asymmetric on purpose, because the two seams differ in what they can resolve:

    * ``test_server`` names are compared for EQUALITY, the recorder sees each name separately.
    * FILES are checked in one direction only, and since [GQ-400] that half is nearly empty. Every
      measure that reaches its file through ``_typed_dict_fields`` parses the WHOLE package, so
      "this file was parsed" cannot be told apart from "some file under ``src`` was parsed", and
      that now covers the enrichment claims as well as the array and ChannelInfo ones: for them any
      ``src`` path in ``reads`` is satisfied by the walk. A post-build QA measured it. The
      declarations stay because they are the honest answer to "which file carries this set", not
      because they can go red; tightening that needs a third recorder over the index's keys.

    FIRST it checks that the recorder did not change the measurement, and that check is here
    because it was needed: the first recorder written for this used ``__getattr__``, ``vars(ts)``
    never reached it, and the twelve ``*_ALWAYS_PRESENT`` constants traced as 0. A trace of a
    different computation than the guard checks is not evidence about the guard, and it fails
    towards accusing innocent claims, which is the worse direction.
    """
    try:
        untraced = claim.measure()
        traced, paths, attributes = _trace_measure(claim.measure)
    except AssertionError as refusal:
        # A measure that refuses is reported as a fault of THIS claim rather than thrown through
        # the loop: the refusals are preconditions of the module, and letting one end the sweep
        # leaves every claim after it untraced without saying so.
        return [f"the measure refused to answer, so nothing about it is traced: {refusal}"]
    declared_files = {entry for entry in claim.reads if entry.endswith(".py")}
    declared_names = set(claim.reads) - declared_files
    observed_names = attributes - _TRACE_ARTEFACTS

    faults = [
        f"the recorders changed the measurement ({traced} traced versus {untraced} untraced), so "
        "the trace describes a different computation than the one the guard checks"
    ] * (traced != untraced)
    faults += [
        f"declares the source file {entry!r}, which the measure never parsed"
        for entry in sorted(declared_files)
        if not any(path == entry or path.endswith(f"/{entry}") for path in paths)
    ]
    faults += [
        f"reads ts.{name} without declaring it" for name in sorted(observed_names - declared_names)
    ]
    faults += [
        f"declares ts.{name} but never reads it" for name in sorted(declared_names - observed_names)
    ]
    return faults


# --- the measurements -------------------------------------------------------------------------


@cache
def _tool_pinned_by(constant: str) -> str:
    """The tool whose ADVERTISED schema the per-tool map *constant* is held against.

    Read from the test that holds it rather than typed a second time: the row of
    ``_MAPPED_FIELD_CLAIMS`` names that test, and the test names exactly one tool, in its
    ``tools[...]`` lookup. Loud when the row, the test or the lookup does not say exactly one
    thing, because a row pointing at the wrong test is a silent swap. Measured by an outside QA on
    2026-07-26, on the tree of that day: two maps of the same size traded places, its full lane
    stayed green, and the next ordinary change to one of those maps then accused the OTHER tool's
    correct sentence. That QA's own verdict was that this is no defect in the delivered tree, where
    no row is mis-wired and most swaps are loud; what stays is the silent pair, and this is what
    makes it loud. Re-measured on 2026-09-16 for this module alone: under the same swap its tests
    were green.

    ⚠️ It reads the SPELLING of a foreign test, one ``tools[...]`` lookup and one ``*_BASE_TYPE``
    name. A second lookup in that test, or a renamed local, stops this and with it every claim that
    reads a per-tool map, with a message about ``_MAPPED_FIELD_CLAIMS`` while the edit was
    elsewhere. That is the price of deriving the binding rather than typing it a second time: a
    post-build QA is right that this module rejects derivations from spelling, and here the spelling
    IS the binding, the alternative being the hand-typed pair this replaced.
    """
    rows = [test for test, named in _MAPPED_FIELD_CLAIMS if named == constant]
    if len(rows) != 1:
        raise AssertionError(
            f"{constant} is named by {len(rows)} rows of _MAPPED_FIELD_CLAIMS, and a map that is "
            "held against a tool needs exactly one"
        )
    node = next(
        (
            item
            for item in _module_ast(ts).body
            if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef) and item.name == rows[0]
        ),
        None,
    )
    if node is None:
        raise AssertionError(
            f"the row for {constant} names {rows[0]}, which tests/test_server.py no longer defines"
        )
    tools: set[str] = {
        item.slice.value
        for item in ast.walk(node)
        if isinstance(item, ast.Subscript)
        and isinstance(item.value, ast.Name)
        and item.value.id == "tools"
        and isinstance(item.slice, ast.Constant)
        and isinstance(item.slice.value, str)
    }
    maps = {
        item.id
        for item in ast.walk(node)
        if isinstance(item, ast.Name) and item.id.endswith("_BASE_TYPE")
    }
    if len(tools) != 1 or maps != {constant}:
        raise AssertionError(
            f"the row for {constant} names {rows[0]}, which looks up the tools {sorted(tools)} and "
            f"reads the maps {sorted(maps)}; a row must name the test that reads its own map, for "
            "exactly one tool"
        )
    return next(iter(tools))


def _none_valued(constant: str) -> int:
    """Fields the per-tool map *constant* declares with NO advertised base type.

    ``None`` means specifically ``object | None``. An ``X | None`` with a concrete ``X`` still
    carries its non-null type (``archived: bool | None`` maps to ``"boolean"``), so the estate's
    "enrichment fields" are not simply its nullable fields. Every claim below that counts an
    enrichment, topology or type-info subset rests on that distinction.

    ⚠️ UNTIL [GQ-400] THAT DISTINCTION WAS A SENTENCE, NOT A PROPERTY. The map carries what
    ``tests.test_server._base_type`` reads off the advertised schema, and that is ``None`` for every
    property without a non-null type, a plain non-nullable ``object`` included. Measured on
    2026-09-16 in a throwaway copy: one such field added to the is_archived result made four TRUE
    enrichment sentences go red, each with an instruction to write a false number into it. So every
    ``None`` entry is looked up in the result type of the tool the map is held against
    (:func:`_tool_pinned_by`), and anything but ``object | None`` there, in any spelling
    :func:`_union_members` reads, stops the measure instead of being counted.

    ⚠️ BOTH DIRECTIONS, and the second one a post-build QA asked for: a field the result annotates
    ``object | None`` while its map row carries a concrete base type would go uncounted and unseen,
    so the two sides are compared as SETS rather than the map's alone.
    """
    mapping: Mapping[str, str | None] = getattr(ts, constant)
    tool = _tool_pinned_by(constant)
    fields = _typed_dict_fields().get(_tool_result_type().get(tool, ""))
    if fields is None:
        raise AssertionError(
            f"{constant} is held against {tool!r}, whose result type is not among the package's "
            "TypedDicts"
        )
    unnamed = sorted(field for field, base in mapping.items() if base is None)
    nullable_opaque = sorted(
        field
        for field, annotation in fields.items()
        if _union_members(annotation) == {"object", "None"}
    )
    if unnamed != nullable_opaque:
        raise AssertionError(
            f"{constant} advertises {unnamed} without a base type while the {tool} result "
            f"annotates {nullable_opaque} as object | None; the sentences count those fields, so "
            "the two sides must name the same set. Decide which set the sentence means before "
            "touching a number."
        )
    return len(unnamed)


@cache
def _parsed(path: Path) -> ast.Module:
    """Parse once per process. Nothing here mutates a source file, so caching is safe, and the
    uncached version re-read and re-parsed the 177 KB test module up to 44 times per run."""
    return ast.parse(path.read_text(encoding="utf-8-sig"))


def _module_ast(module: Any) -> ast.Module:
    return _parsed(Path(module.__file__))


@cache
def _always_present_constants() -> int:
    """The ``*_ALWAYS_PRESENT`` constants, the "twelve" the prose counts."""
    return sum(1 for name in vars(ts) if name.endswith("_ALWAYS_PRESENT"))


@cache
def _explicit_always_present_rows() -> int:
    """Rows of ``_ALWAYS_PRESENT_BY_TOOL`` written out one by one, counted in the literal itself.

    The sentence is about the EXPLICIT rows, the ones that "point straight at the constant its
    sibling conformance test declares", as opposed to the Olog rows a ``**`` splat contributes. What
    was measured instead was the dict's length minus the Olog table's, which is the same number only
    while no explicit key repeats a splatted one. Measured on 2026-09-16 in a throwaway copy: an
    explicit row for a tool the splat already carries is a twelfth row in the source, Python keeps
    the later value under the one key, and the subtraction went on answering the old number with the
    whole module green.

    Loud when the literal is gone, and when an explicit value is anything but a bare name ending in
    ``_ALWAYS_PRESENT``: that much of "point straight at the constant" a syntax tree can check, and
    a post-build QA asked for the name shape rather than the node kind alone. Whether the constant
    is the one that tool's OWN sibling test declares stays a human reading.
    """
    for node in _module_ast(ts).body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "_ALWAYS_PRESENT_BY_TOOL"
            and isinstance(node.value, ast.Dict)
        ):
            explicit = [
                value
                for key, value in zip(node.value.keys, node.value.values, strict=True)
                if key is not None
            ]
            indirect = [
                ast.unparse(value)
                for value in explicit
                if not (isinstance(value, ast.Name) and value.id.endswith("_ALWAYS_PRESENT"))
            ]
            if indirect:
                raise AssertionError(
                    "these explicit rows of _ALWAYS_PRESENT_BY_TOOL do not point straight at an "
                    "always-present constant, so the sentence about them is no longer about this "
                    f"set: {indirect}"
                )
            return len(explicit)
    raise AssertionError(
        "_ALWAYS_PRESENT_BY_TOOL is no longer an annotated dict literal at the module level of "
        "tests/test_server.py, so the explicit rows cannot be counted where they are written"
    )


def _named_tests(suffix: str) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        node
        for node in _module_ast(ts).body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.endswith(suffix)
    ]


@cache
def _conformance_tests() -> int:
    """The per-tool ``*_conforms_to_its_schema`` tests, the other "twelve"."""
    return len(_named_tests("_conforms_to_its_schema"))


def _drives_real_client(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """The test constructs a ``Client``, i.e. it goes over the wire."""
    return any(
        isinstance(inner, ast.Call)
        and isinstance(inner.func, ast.Name)
        and inner.func.id == "Client"
        for inner in ast.walk(node)
    )


def _drives_in_process(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """The test calls ``mcp.call_tool``, the in-process path, receiver included.

    THE RECEIVER IS THE WHOLE DISCRIMINATOR, not decoration. Measured: all twelve conformance
    tests contain some ``.call_tool``, because the two wire tests call it on their ``Client``.
    Matching the attribute name alone would answer twelve where the sentence says ten.
    """
    return any(
        isinstance(inner, ast.Call)
        and isinstance(inner.func, ast.Attribute)
        and inner.func.attr == "call_tool"
        and isinstance(inner.func.value, ast.Name)
        and inner.func.value.id == "mcp"
        for inner in ast.walk(node)
    )


@cache
def _real_client_conformance_tests() -> int:
    """Conformance tests that drive a REAL client.

    The ``paths``-table half of the old conjunction is gone. It carried no information, every
    test with a table also builds a ``Client``, and it was the half that made this a proxy: a
    conformance test converted to ``async with Client(mcp)`` WITHOUT a table (the direction this
    estate's own docstrings advocate) stayed uncounted, so "TEN of the twelve drive
    FastMCP.call_tool" would have gone on reading ten while the truth was nine.
    """
    return sum(1 for node in _named_tests("_conforms_to_its_schema") if _drives_real_client(node))


@cache
def _in_process_conformance_tests() -> int:
    """Conformance tests that call ``mcp.call_tool``, measured, not "the twelve minus the two".

    Deriving this by subtraction guaranteed the two figures summed to twelve, which is what the
    sentence asserts, but it could not notice a test that drives NEITHER path. The guarantee is
    restored explicitly by ``test_the_conformance_tests_partition_into_two_kinds``.
    """
    return sum(1 for node in _named_tests("_conforms_to_its_schema") if _drives_in_process(node))


@cache
def _runtime_bound_constants() -> int:
    """``*_ALWAYS_PRESENT`` constants a REAL-CLIENT conformance test reads.

    ⚠️ Honest scope, because this is the one measure in the family that remains a stand-in. The
    sentence it guards says those constants are "ALREADY runtime-bound ... and go red on the same
    mutation", a MUTATION property, which no constant can settle. What is measured instead is
    which constants the wire-driving tests read, and that is nameable, derivable and moves with
    the thing the sentence is about. It is not the same claim, and pretending otherwise is what
    the previous version did by counting TESTS and calling the answer a count of constants.
    """
    return len(
        {
            inner.id
            for node in _named_tests("_conforms_to_its_schema")
            if _drives_real_client(node)
            for inner in ast.walk(node)
            if isinstance(inner, ast.Name) and inner.id.endswith("_ALWAYS_PRESENT")
        }
    )


@cache
def _tools_named_by_in_process_tests() -> frozenset[str]:
    """The typed tools the in-process conformance tests actually drive, by name.

    Replaces ``len(_TYPED_OUTPUT_TOOLS) - <a count of tests>``, which subtracted a number of TESTS
    from a number of TOOLS and was right only because the olog test happens to drive eleven tools
    from its own table. Deleting a row from that table left the arithmetic untouched and the
    sentence false.
    """
    return frozenset(
        inner.value
        for node in _named_tests("_conforms_to_its_schema")
        if _drives_in_process(node)
        for inner in ast.walk(node)
        if isinstance(inner, ast.Constant) and inner.value in ts._TYPED_OUTPUT_TOOLS
    )


def _is_mcp_tool(node: ast.expr) -> bool:
    """``mcp.tool`` or ``mcp.tool(...)``, in decorator or call position."""
    target = node.func if isinstance(node, ast.Call) else node
    return (
        isinstance(target, ast.Attribute)
        and target.attr == "tool"
        and isinstance(target.value, ast.Name)
        and target.value.id == "mcp"
    )


#: The attributes through which a FastMCP server takes a tool or drops one, read off the installed
#: FastMCP 3.4.4 on 2026-09-16 (``server.py``: ``tool``, ``add_tool``, ``remove_tool``,
#: ``add_tool_transformation``, ``remove_tool_transformation``, ``mount``, ``import_server``).
#: ``tool`` is the one this package writes; the others are listed so that a registrar module
#: reaching for them is REFUSED by :func:`_tool_registrations` rather than silently left uncounted.
#: Measured the same day: ``mcp.remove_tool("find_device")`` beside the display registrations left
#: every lane claim green while the registry had one tool fewer.
#: ⚠️ Hand-kept, like every list of a foreign library's surface, and nothing proves it complete: a
#: surface it misses is a tool this count does not see. The list is checked against the library the
#: day somebody reads this, not by a test.
_REGISTRAR_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "tool",
        "add_tool",
        "remove_tool",
        "add_tool_transformation",
        "remove_tool_transformation",
        "mount",
        "import_server",
    }
)

#: What a module OUTSIDE the two registrars may not reach for. Narrower than the set above on
#: purpose: ``mount`` is also a ``requests`` session method and is used as one in
#: ``services/_http.py`` (measured 2026-09-16), so the wide set would accuse a line that registers
#: nothing. What stays is the pair that only a FastMCP server carries.
_REGISTRAR_ATTRIBUTES_ELSEWHERE: frozenset[str] = frozenset({"tool", "add_tool"})


@dataclass(frozen=True)
class _Registration:
    """One tool registration in a module: its line, and the function it names if it names one."""

    lineno: int
    function: str | None


def _direct_registration_target(call: ast.Call) -> ast.expr | None:
    """The function a DIRECT ``mcp.tool(...)`` registers on the spot, or ``None`` for a factory.

    FastMCP's first parameter is ``name_or_fn``: a function in that slot is registered immediately,
    while a string or ``None`` there is a NAME and the call hands back a decorator instead. Both
    spellings of the slot count, positional and keyword, because FastMCP 3.4.4 registers both
    (probed with a scratch server on 2026-09-16).
    """
    slot = call.args[0] if call.args else None
    if slot is None:
        slot = next((item.value for item in call.keywords if item.arg == "name_or_fn"), None)
    if slot is None or (
        isinstance(slot, ast.Constant) and (slot.value is None or isinstance(slot.value, str))
    ):
        return None
    return slot


def _tool_registrations(tree: ast.Module, label: str) -> tuple[_Registration, ...]:
    """Every tool registration in *tree*, in each form FastMCP takes one, or a loud refusal.

    The forms: a decorator, bare or called (``@mcp.tool``, ``@mcp.tool(...)``); the call position
    ``mcp.tool(...)(fn)``; and the direct call ``mcp.tool(fn, ...)``, see
    :func:`_direct_registration_target`. One reading serves :func:`_registered_tools` and
    :func:`_registered_tool_functions`, so the count and the functions behind it cannot disagree
    about which forms exist.

    REFUSED rather than skipped: every other reach for a registrar attribute in *tree*, whatever
    object it hangs off. That covers an alias (``registrar = mcp``), a registrar kept in a variable
    (``register = mcp.tool``), a factory applied somewhere else, and ``add_tool``. Skipping was the
    defect: each of those can register a tool that no lane count sees. ⚠️ What a syntax tree cannot
    name stays out of reach, ``getattr(mcp, "tool")`` above all; and this reads ONE module, so a
    registrar reached from another module is the job of
    ``test_no_other_module_reaches_the_registrar``.
    """
    registrations: list[_Registration] = []
    accounted: set[int] = set()
    applied: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for decorator in node.decorator_list:
                if _is_mcp_tool(decorator):
                    target = decorator.func if isinstance(decorator, ast.Call) else decorator
                    accounted.add(id(target))
                    applied.add(id(decorator))
                    registrations.append(_Registration(decorator.lineno, node.name))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Call)
            and _is_mcp_tool(node.func)
        ):
            # ``mcp.tool(...)(fn)``: requiring the OUTER call's func to be a call is what keeps a
            # decorator, which ``ast.walk`` also visits on its own, from being counted twice.
            accounted.add(id(node.func.func))
            applied.add(id(node.func))
            argument = node.args[0] if node.args else None
            named = argument.id if isinstance(argument, ast.Name) else None
            registrations.append(_Registration(node.lineno, named))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and id(node) not in applied and _is_mcp_tool(node):
            function = _direct_registration_target(node)
            if function is not None:
                accounted.add(id(node.func))
                named = function.id if isinstance(function, ast.Name) else None
                registrations.append(_Registration(node.lineno, named))
    unreadable = sorted(
        f"{label}:{node.lineno}: {ast.unparse(node)}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr in _REGISTRAR_ATTRIBUTES
        and id(node) not in accounted
    )
    if unreadable:
        raise AssertionError(
            "a tool registrar is reached in a form this reader does not count, so a tool "
            f"registered that way would leave every lane claim green: {unreadable}"
        )
    return tuple(registrations)


def _registered_tools(path: Path) -> int:
    """Tools a module REGISTERS, in every form FastMCP takes one.

    Counting ``async def`` instead would miss the difference that matters: dropping one
    ``mcp.tool(...)(fn)`` line takes a tool off the wire while leaving the coroutine defined, so
    every count stays green and a tool disappears silently.

    ⚠️ "Both forms are counted" is what this said until [GQ-400], and it was true of the HOUSE, not
    of FastMCP: the decorator and the call position are what this package writes, while the direct
    call ``mcp.tool(fn, ...)``, which FastMCP registers just the same, was counted by nobody.
    Measured on 2026-09-16 in a throwaway copy of the tree: a display tool registered that way, or
    through ``registrar = mcp``, left this whole module green. The reading is
    :func:`_tool_registrations` now, and it refuses what it cannot classify.
    """
    return len(_tool_registrations(_parsed(path), path.name))


@cache
def _core_lane_tools() -> int:
    """Tools ``server.py`` registers, the core lane, AST-scanned, never taken from the wire."""
    return _registered_tools(_SRC / "server.py")


@cache
def _display_tools() -> int:
    """The display-extra tools. AST-scanned, never imported: importing ``display_tools.py`` pulls
    ``opi_navigation``, which the core-only lane does not have."""
    return _registered_tools(_SRC / "display_tools.py")


@cache
def _full_lane_tools() -> int:
    return _core_lane_tools() + _display_tools()


def _rows_sharing(element_schema: dict[str, object]) -> int:
    """Rows of ``_OUTPUT_ARRAY_ITEMS`` that hold *element_schema* ITSELF, identity, deliberately.

    The sentence this serves is about ALIASING: "they are module dicts, so assigning into one
    would silently move N (resp. M) rows". Exactly the rows pointing at that object would move,
    so identity is the property the sentence names, and a row spelled out as an equal literal is
    correctly not one of them.
    """
    return sum(1 for value in ts._OUTPUT_ARRAY_ITEMS.values() if value is element_schema)


def _rows_advertising(element_schema: dict[str, object]) -> int:
    """Rows whose advertised element schema EQUALS *element_schema*, value, deliberately.

    The sentences this serves say what the rows CARRY ("the N rows carrying ``{"type": "string"}``",
    "the other M rows are ``{"type": "object", ...}``"). Identity answered a different question
    there, and the difference is reachable rather than theoretical: spelling one row out as an
    equal literal changes nothing about what that row advertises, yet made both sentences go red
    while both were still true. Which set a sentence is about may not depend on whether the author
    reused the module dict or repeated its contents.
    """
    return sum(1 for value in ts._OUTPUT_ARRAY_ITEMS.values() if value == element_schema)


@cache
def _distinct_element_schemas() -> int:
    """How many DIFFERENT element schemas the array rows advertise between them.

    By value, for the same reason as :func:`_rows_advertising`: the sentence is about the schemas
    the estate advertises, and two equal dicts are one advertised schema however many objects
    happen to hold it. Normalized through ``json.dumps(sort_keys=True)`` because the schemas are
    dicts and a set needs a hashable, order-independent key.
    """
    return len({json.dumps(schema, sort_keys=True) for schema in ts._OUTPUT_ARRAY_ITEMS.values()})


def _is_typed_dict_base(base: ast.expr) -> bool:
    """``TypedDict`` itself, in either spelling, as opposed to another TypedDict being inherited."""
    return (isinstance(base, ast.Name) and base.id == "TypedDict") or (
        isinstance(base, ast.Attribute) and base.attr == "TypedDict"
    )


def _base_name(base: ast.expr) -> str:
    """The name a base expression carries, in the two shapes a base is written in here."""
    if isinstance(base, ast.Name):
        return base.id
    if isinstance(base, ast.Attribute):
        return base.attr
    return ""


def _typed_dict_index(trees: Iterable[tuple[str, ast.Module]]) -> dict[str, dict[str, str]]:
    """Every ``TypedDict`` in *trees* by name, with its fields' normalized annotations.

    BOTH spellings, and the functional one is not hypothetical: ``ArchiverHistoryResult`` in
    ``tools/archiver.py`` HAS to use it, because ``from`` is a Python keyword and cannot be a
    class-syntax field name. A class-syntax-only index did not see that tool's result shape at
    all, latent while none of its fields is nullable, and silent the moment one becomes so.

    Only class-body annotations count, never any annotated assignment: a function LOCAL would
    otherwise stand in for a field (``warnings: list[str] = []`` already exists inside a function
    body in this package).

    Base resolution is deliberately ABSENT, and since [GQ-400] its absence is REFUSED rather than
    merely stated. Measured on 2026-09-16 in a throwaway copy: a second base beside ``TypedDict``
    kept ``ChannelInfo`` in the index with its own body only, so the sentence counting its fields
    stayed green while the type had gained one; a single inherited base dropped the class out of the
    index altogether and reached the reader as "ChannelInfo is gone", which sends the next author
    looking for a deletion that never happened. Neither shape exists in this package today, which is
    why ``test_the_typed_dict_index_refuses_an_inherited_field`` holds the refusal on constructed
    input rather than on the tree.

    ⚠️ The refusal reaches exactly as far as this index sees. A base defined OUTSIDE the package, or
    a TypedDict imported under another name (``from typing import TypedDict as _TD``), is neither
    indexed nor refused: the class falls out of the index and the reader that misses it says so.
    Measured on 2026-09-16 in a throwaway copy, that case still reaches the reader as "ChannelInfo
    is gone", which is why :func:`_channel_info_fields` now names both of its causes.

    Two TypedDicts of the same NAME are refused as well, and that one a post-build QA found. This
    index is keyed on the bare name over a package walk in path order, so the file read later
    answered for both without a word. Measured on 2026-09-16: a second ``ChannelInfo`` in
    ``tools/validate.py``, which sorts after ``services/channelfinder_client.py``, made the count
    answer one and accuse the correct sentence of saying six.
    """
    index: dict[str, dict[str, str]] = {}
    classes: list[tuple[str, ast.ClassDef]] = []
    written: dict[str, str] = {}
    twice: list[str] = []
    for label, tree in tuple(trees):
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                classes.append((label, node))
                if any(_is_typed_dict_base(base) for base in node.bases):
                    if node.name in written:
                        twice.append(f"{node.name}: {written[node.name]} and {label}")
                    written[node.name] = label
                    index[node.name] = {
                        item.target.id: ast.unparse(item.annotation).replace(" ", "")
                        for item in node.body
                        if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
                    }
            elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                called = node.value.func
                functional = (isinstance(called, ast.Name) and called.id == "TypedDict") or (
                    isinstance(called, ast.Attribute) and called.attr == "TypedDict"
                )
                target = node.targets[0] if node.targets else None
                fields = node.value.args[1] if len(node.value.args) > 1 else None
                if not functional or not isinstance(target, ast.Name):
                    continue
                if not isinstance(fields, ast.Dict):
                    raise AssertionError(
                        f"{label}: {target.id} uses the functional TypedDict form without a "
                        "literal field mapping, so its fields cannot be read from the syntax tree"
                    )
                if target.id in written:
                    twice.append(f"{target.id}: {written[target.id]} and {label}")
                written[target.id] = label
                index[target.id] = {
                    key.value: ast.unparse(annotation).replace(" ", "")
                    for key, annotation in zip(fields.keys, fields.values, strict=True)
                    if isinstance(key, ast.Constant) and isinstance(key.value, str)
                }
    if twice:
        raise AssertionError(
            "two TypedDicts in this package carry the same name, and this index is keyed on the "
            "name alone, so whichever file is read later answers for both and a count that names "
            f"one of them reads the other: {sorted(twice)}"
        )
    inheriting = sorted(
        f"{label}: {node.name}({', '.join(ast.unparse(base) for base in node.bases)})"
        for label, node in classes
        if (node.name in index and len(node.bases) > 1)
        or any(_base_name(base) in index for base in node.bases)
    )
    if inheriting:
        raise AssertionError(
            "these classes inherit TypedDict fields, and this index carries no base resolution, so "
            f"every count that reads it would miss them without a word: {inheriting}"
        )
    return index


@cache
def _typed_dict_fields() -> dict[str, dict[str, str]]:
    """The package's TypedDicts, read through this module's one file seam.

    The reading itself, and what it refuses, is :func:`_typed_dict_index`; only the walk over the
    shipped package lives here, so the index can be driven on constructed trees by a test.
    """
    return _typed_dict_index((path.name, _parsed(path)) for path in sorted(_SRC.rglob("*.py")))


@cache
def _registered_tool_functions() -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """Registered tool name to the function that implements it, from the two registrar modules.

    Every registration form, through the same :func:`_tool_registrations` reading that
    :func:`_registered_tools` counts with: server.py decorates, display_tools.py calls
    ``mcp.tool(...)(fn)`` after the fact, and a decorator-only reader would know nothing about the
    display tools at all.

    One discovery, several readers, the return annotation and the parameter names are two
    questions about the same set, and asking each of them its own way is how the two answers start
    disagreeing.

    ⚠️ The resolution is LOUD, because until a post-build QA measured it this half skipped in
    silence exactly what the reading above refuses out loud: a registration whose target is not a
    bare name defined in the same module. Measured on 2026-09-16 in a throwaway copy, with the
    display registration rewritten as ``mcp.tool(...)(globals()["find_device"])``: the count stayed
    at four and every display sentence with it, while the name set lost one, and the shipped guide's
    sentence about the display tools that take a ``context_cap`` was accused of saying three where
    the set had two. A correct sentence, accused in place of the registration nobody could read.
    """
    found: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    unresolved: list[str] = []
    for path in (_SRC / "server.py", _SRC / "display_tools.py"):
        tree = _parsed(path)
        defined = {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        for registration in _tool_registrations(tree, path.name):
            if registration.function is None or registration.function not in defined:
                unresolved.append(f"{path.name}:{registration.lineno}")
                continue
            found[registration.function] = defined[registration.function]
    if unresolved:
        raise AssertionError(
            "these registrations do not resolve to a function defined in their own registrar "
            "module, so the tools behind them are counted and then lost, and every set built from "
            f"the functions is short of them without a word: {unresolved}"
        )
    return found


@cache
def _tool_result_type() -> dict[str, str]:
    """Registered tool name to the type annotation its function declares as its return."""
    return {
        name: ast.unparse(node.returns)
        for name, node in _registered_tool_functions().items()
        if node.returns is not None
    }


@cache
def _tools_declaring_parameter(parameter: str) -> frozenset[str]:
    """Registered tools whose SIGNATURE declares a parameter literally named *parameter*.

    This is the set the title sentences are about: a parameter name is what reaches the wire as a
    schema property, and the trap those sentences warn about, a strip pass that deletes every
    ``title`` key, bites exactly there. They were measured against a hand-typed four-element
    tuple instead, which is not a reading of anything: giving a fifth tool a ``title`` parameter
    left both sentences false and the whole lane green.

    All three parameter kinds count (positional-only, positional-or-keyword, keyword-only) because
    all three arrive on the wire as a property name.

    AST over the registrar modules rather than ``mcp.list_tools()``, for the reason this module
    states elsewhere: the wire answers with fewer tools in the core-only lane. Measured, that makes
    no difference to THIS answer, no display tool declares a ``title`` parameter, but a count
    taken from the wire would be lane-dependent by construction, which is the trap
    ``tests/test_server.py`` warns about at its own ``_TYPED_OUTPUT_TOOLS``.
    """
    return frozenset(
        name
        for name, node in _registered_tool_functions().items()
        if any(
            argument.arg == parameter
            for group in (node.args.posonlyargs, node.args.args, node.args.kwonlyargs)
            for argument in group
        )
    )


#: Spellings of a union that are the SAME TYPE as ``X | None`` and must therefore read alike.
#: ``Annotated[X, ...]`` is unwrapped rather than treated as a member: the wrapper carries metadata,
#: never a type, and it is already house style on the input side of this package.
#: ⚠️ Hand-kept, and nothing proves it complete. What a missing spelling costs is measured where it
#: costs something: on the rows the nullable-array count reads, against the schema they advertise
#: ([GQ-405], ``test_the_union_reader_agrees_with_the_wire_on_every_array_row``).
_UNION_ALIASES: frozenset[str] = frozenset({"Union", "Optional"})


def _union_members(annotation: str) -> frozenset[str]:
    """The top-level members of a union annotation, in every spelling of the same type.

    PARSED, never split on the ``|`` character: a union nested inside a subscript
    (``dict[str, int | None]``) is ONE member, and a text split would read it as two.

    Normalized, because these are all one type and a guard that answers differently for them is
    testing the author's habit rather than the type: ``X | None`` and ``None | X`` (order),
    ``Optional[X]`` and ``Union[X, None]`` (alias), the same two written ``typing.Optional[X]`` /
    ``t.Union[...]`` (dotted), ``Annotated[X, Field(...)]`` (wrapper), and a quoted forward
    reference ``"X | None"`` (string). Measured: reading only ``X | None`` and ``Optional[X]``
    left five of those seven spellings answering wrongly, in both directions.

    Anything the walk does not recognize comes back as a single unparsed member, so a non-union
    annotation answers as a one-element set. ⚠️ An unknown SPELLING of a union answers the same
    way, silently and as NOT nullable: measured on 2026-09-16, a module-level type alias and a
    ``NotRequired`` qualifier both did, while the schema each of them rendered to said the
    opposite. On the array rows that reading is held against the advertised schema ([GQ-405]);
    everywhere else it stays this walk's honest limit.
    """

    def members(node: ast.expr) -> Iterator[str]:
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            yield from members(node.left)
            yield from members(node.right)
            return
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            # A quoted forward reference is a type written as text; read the text.
            yield from members(ast.parse(node.value, mode="eval").body)
            return
        if isinstance(node, ast.Subscript):
            head = node.value
            name = head.attr if isinstance(head, ast.Attribute) else getattr(head, "id", "")
            if name == "Annotated":
                inner = node.slice
                first = inner.elts[0] if isinstance(inner, ast.Tuple) and inner.elts else inner
                yield from members(first)
                return
            if name in _UNION_ALIASES:
                inner = node.slice
                for element in inner.elts if isinstance(inner, ast.Tuple) else [inner]:
                    yield from members(element)
                if name == "Optional":
                    yield "None"
                return
        yield ast.unparse(node).replace(" ", "")

    return frozenset(members(ast.parse(annotation, mode="eval").body))


def _row_admits_null(annotation: str) -> bool:
    """Whether an ARRAY row's field admits ``None``, i.e. renders as ``anyOf[array, null]``.

    THE QUESTION IS ONLY NULLABILITY, and getting that wrong is what two rounds of QA found here.
    Whether the field is an array is already settled elsewhere and better: ``_OUTPUT_ARRAY_ITEMS``
    is pinned EQUAL to the set of array-typed properties the live schemas advertise
    (``test_typed_output_schema_arrays_declare_their_element_schema``), so every row is an array on
    the wire by construction. A predicate that also asked "does the annotation start with
    ``list[``" was therefore re-deciding a settled question, and deciding it from the spelling:

    * first it asked ``startswith("list[") and endswith("|None")``. ``None | list[str]``, the
      identical type, the identical wire schema, answered False, so a genuinely nullable new row
      shipped green with the prose false, and re-spelling an existing field reddened a true
      sentence. Both directions measured.
    * then it asked the same question of the union members, which fixed the ORDER and left
      ``Sequence[str] | None``, ``tuple[str, ...] | None``, ``Union[list[str], None]``,
      ``typing.Optional[list[str]]``, ``Annotated[list[str] | None, Field(...)]`` and the quoted
      forward reference each answering wrongly, five of them false-RED on correct prose. A fix
      that moves the boundary of a wrong question is still the wrong question.

    Asking only about ``None`` removes the container from the criterion altogether, which is why
    that whole family of spellings stops mattering.
    """
    return "None" in _union_members(annotation)


def _array_row_annotation(tool: str, field: str) -> str:
    """The annotation an ``_OUTPUT_ARRAY_ITEMS`` row's field carries in its TOOL'S OWN result type.

    One resolution for the two readers of these rows, the count below and the wire comparison in
    ``test_the_union_reader_agrees_with_the_wire_on_every_array_row``, so the two cannot disagree
    about which annotation a row is about.

    Loud rather than lenient when a row stops resolving: a silent skip would let the count fall
    while the prose stayed green, which is the whole failure mode this module exists for.
    """
    results = _tool_result_type()
    fields = _typed_dict_fields().get(results.get(tool, ""))
    if fields is None or field not in fields:
        raise AssertionError(
            f"the array row ({tool!r}, {field!r}) no longer resolves to a TypedDict field "
            f"(the tool's return annotation reads {results.get(tool)!r}), the row, the "
            "return annotation or the result shape moved"
        )
    return fields[field]


@cache
def _nullable_array_rows() -> int:
    """Array rows whose field is an ``X | None``, the ``anyOf[array, null]`` subset.

    Bound to the (tool, field) PAIR the row is keyed on, resolved through the TOOL'S OWN result
    type by :func:`_array_row_annotation`. Matching by field NAME across the package read a set the
    sentence does not name, and the collisions are real rather than imagined: ``pvs``, ``tags``,
    ``properties`` and ``samples`` each carry two different annotations here, so one internal
    TypedDict declaring any of them nullable would have moved this count without a single array row
    changing.

    ⚠️ What this reads is the ANNOTATION, so it is exactly as right as the union walk is. Since
    [GQ-405] that reading is compared to the schema each row advertises, by
    ``test_the_union_reader_agrees_with_the_wire_on_every_array_row``.
    """
    return sum(
        _row_admits_null(_array_row_annotation(tool, field))
        for tool, field in ts._OUTPUT_ARRAY_ITEMS
    )


@cache
def _channel_info_fields() -> int:
    """Fields of ``ChannelInfo``, a NAMESAKE of the find_channels key count, not the same set.

    DECIDED WITH [GQ-400], and it is the rest an outside QA left here: a field ``ChannelInfo``
    INHERITS was invisible to this count. :func:`_typed_dict_index` refuses both shapes of that as
    far as it can see them; where it cannot, because the base is imported under another name or
    lives outside the package, a post-build QA measured that the reader gets the message below, so
    the message names that cause as well. ⚠️ "all required", the other half of the sentence this
    count serves, names no size and stays a human reading: ``total=False`` or a ``NotRequired``
    field would make it false with nothing here going red.
    """
    fields = _typed_dict_fields().get("ChannelInfo")
    if fields is None:
        raise AssertionError(
            "ChannelInfo is gone from the package's TypedDicts: deleted, renamed, or inheriting "
            "from a base this index cannot see, such as a TypedDict imported under another name "
            "or declared outside the package"
        )
    return len(fields)


@cache
def _destructive_golden_rows() -> int:
    """Golden-map rows whose ``destructiveHint`` is True.

    The index comes from ``_ANNOTATION_HINTS``, not from a literal: the hint ORDER is a convention
    that a comment records, and a comment is the thing this module distrusts.
    """
    index = list(ts._ANNOTATION_HINTS).index("destructiveHint")
    return sum(1 for hints in ts._ANNOTATION_GOLDEN.values() if hints[index] is True)


@cache
def _paths_rows(test_name: str) -> int:
    """Rows of the ``paths`` table inside *test_name*, the return paths it drives."""
    for node in _module_ast(ts).body:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) or node.name != test_name:
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.AnnAssign)
                and isinstance(inner.target, ast.Name)
                and inner.target.id == "paths"
                and isinstance(inner.value, ast.List)
            ):
                return len(inner.value.elts)
    raise AssertionError(f"{test_name} no longer builds a ``paths`` table")


_DISCOVER_CONFORMANCE = "test_discover_pvs_structured_output_conforms_to_its_schema"
_FIND_CHANNELS_CONFORMANCE = "test_find_channels_structured_output_conforms_to_its_schema"
#: The test whose docstring states how the array rows split between the two element schemas. Both
#: rows of that family are keyed on it, because their phrases are otherwise plain enough to bind a
#: neighbouring sentence about a different set (see the claims themselves).
_ELEMENT_SCHEMA_TEST = "test_typed_output_schema_arrays_declare_their_element_schema"

#: Which conformance test drives which tool's return paths, by the NAME of the constant a claim's
#: derivation hands to its measure and by the test that name resolves to. Both halves are needed:
#: ``test_every_return_path_claim_reads_the_table_of_its_own_tool`` reads the name out of the
#: derivation and compares the test against the claim's scope.
_CONFORMANCE_BY_TOOL: dict[str, tuple[str, str]] = {
    "discover_pvs": ("_DISCOVER_CONFORMANCE", _DISCOVER_CONFORMANCE),
    "find_channels": ("_FIND_CHANNELS_CONFORMANCE", _FIND_CHANNELS_CONFORMANCE),
}


def _return_paths_as_driven(test_name: str) -> int:
    """The return paths of a TOOL, as far as the conformance test *test_name* drives them.

    ⛔ HONEST SCOPE, decided with [GQ-400] rather than repaired, and the decision is measured. Three
    sentences describe the TOOL rather than a table: find_channels' own description, and the two
    module comments on the keys each tool emits on every one of its return paths. The set they name
    is the tool's return paths; what this counts is the rows of the ``paths`` table the conformance
    test drives, and the two agree only while that table is complete. Measured on 2026-09-16 in a
    throwaway copy: a genuine further return in ``_find_channels`` left this whole module green.

    The derivation a reader reaches for was measured and rejected. Counted on 2026-09-16 over each
    delegation chain, nested functions included: discover_pvs carries six ``return`` statements and
    find_channels seven, of which four each are TERMINAL and the rest hand on to the next function
    in the chain. Four is the table's number for both, so the derivation agrees today. But it counts
    a code STYLE rather than a path: merging two returns into one, or splitting the concrete-miss
    path into its three statuses, moves the answer while the tool's paths stand still, and it would
    redden a correct sentence. A derivation that depends on how the author spelled the control flow
    is what this module rejects at :func:`_olog_round_trip_tools`. ⚠️ The six and the seven are what
    a reader re-running that count sees; the four is what is left after subtracting the delegations,
    ``server.py`` to ``tools``, ``tools`` to ``services``, and the wildcard branch of discover_pvs.

    What binds tool and table instead, partly: both conformance tests assert that the paths they
    drive together emit every advertised field, so a new path is noticed when it emits a field no
    driven path does, and not otherwise. The claims scoped INSIDE those tests describe the table
    itself and keep reading :func:`_paths_rows` directly.
    """
    return _paths_rows(test_name)


@cache
def _olog_query_functions() -> int:
    """``query_olog_*`` coroutines in checkers_olog.py, the "ten" its own header claims."""
    return sum(
        1
        for node in _parsed(_SRC / "services" / "checkers_olog.py").body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name.startswith("query_olog_")
    )


@cache
def _olog_reexport_lines() -> int:
    """``query_olog_*`` names ``services/checkers.py`` re-exports from ``checkers_olog``.

    The sentence this serves is about the RE-EXPORT, "for backward compatibility :mod:`~.checkers`
    re-exports the ... functions and ``_olog_error_code``", not about the definitions. Counting the
    definitions instead, which is what this claim used to do, read a set the sentence does not
    name: dropping a re-export leaves every definition where it is, so the count stayed right while
    the sentence became false.

    ``name as name`` is the whole criterion, and not a stylistic detail: under
    ``no_implicit_reexport`` the bare form does not re-export at all, which is what the comment
    above those lines in ``checkers.py`` says itself. The source module is part of the criterion
    too, because the sentence names it.
    """
    return sum(
        1
        for node in ast.walk(_parsed(_SRC / "services" / "checkers.py"))
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("checkers_olog")
        for alias in node.names
        if alias.name.startswith("query_olog_") and alias.asname == alias.name
    )


@cache
def _return_path_rows() -> int:
    """The return-path count both real-client conformance tests drive.

    Loud rather than lenient if they ever differ: prose that says "four paths" without naming a
    tool is only meaningful while the two agree, so a divergence must stop the guard, not be
    averaged away."""
    sizes = {_paths_rows(_DISCOVER_CONFORMANCE), _paths_rows(_FIND_CHANNELS_CONFORMANCE)}
    if len(sizes) != 1:
        raise AssertionError(f"the two conformance path tables no longer agree: {sorted(sizes)}")
    return sizes.pop()


# --- the measurements the SHIPPED GUIDE needs -----------------------------------------------------
#
# Added with [GQ-123], when the reader learned to open markdown. Every one of them reads the CODE
# the guide's sentence is about, never a table in a test that would have to be remembered: the
# guide is the text a model and an operator read, and a derivation that rests on somebody keeping a
# list up to date puts the same maintenance obligation one file further away.


@cache
def _write_result_fields() -> int:
    """Fields ``WriteResult`` answers a PV write with, from its class body.

    AST rather than ``model_fields`` on the imported class, for the reason the whole module reads
    sources instead of importing them: a claim is about what the file DECLARES, and an import also
    answers for whatever a base class contributes.
    """
    return sum(
        1
        for node in ast.walk(_parsed(_SRC / "readback.py"))
        if isinstance(node, ast.ClassDef) and node.name == "WriteResult"
        for statement in node.body
        if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name)
    )


@cache
def _package_modules() -> tuple[Path, ...]:
    """Every module of the shipped package, discovered rather than listed.

    ⛔ A LISTED SET OF MODULES WAS THE FIRST VERSION AND IT WAS SILENTLY BLIND. It named the three
    modules a write request travels through today (``server.py`` -> ``tools/olog.py`` ->
    ``services/checkers_olog.py``), which answered correctly and would have gone on answering 4
    while a FIFTH write tool took the gate through a fourth module. An adversarial coverage pass
    proved it with exactly that mutant: the count did not move and no test went red.

    Discovery costs a parse of the package, which ``_typed_dict_fields`` already pays for its own
    question, and it removes the failure mode entirely rather than documenting it.
    """
    return tuple(sorted(path for path in _SRC.rglob("*.py") if "__pycache__" not in path.parts))


def _called_names(node: ast.AST) -> set[str]:
    """Every name called inside *node*, taken at the call site, attribute or bare name alike."""
    return {
        call.func.attr if isinstance(call.func, ast.Attribute) else call.func.id
        for call in ast.walk(node)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute | ast.Name)
    }


def _reaches_any(seed: frozenset[str]) -> frozenset[str]:
    """*seed* plus every function that reaches one of them, transitively, across the package."""
    calls: dict[str, set[str]] = {}
    for path in _package_modules():
        for node in ast.walk(_parsed(path)):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                calls.setdefault(node.name, set()).update(_called_names(node))
    reaching = set(seed)
    while True:
        grown = reaching | {name for name, callees in calls.items() if callees & reaching}
        if grown == reaching:
            return frozenset(reaching)
        reaching = grown


def _reaches(marker: str) -> frozenset[str]:
    """Every function along the Olog write path that reaches a call to *marker*, transitively.

    A call GRAPH rather than a single hop, and that is a measured requirement rather than
    thoroughness for its own sake: the tool a caller sees is three modules away from the gate it is
    gated by, so a reader that looks one hop deep answers zero and a reader that looks two answers
    zero as well. Both were measured before this was written.

    The closure is the honest shape of the question the guide's sentences ask, "which tools are
    behind this gate", and it keeps answering it when somebody inserts another delegation layer,
    which is exactly what happened between the gate and the tools already.
    """
    calls: dict[str, set[str]] = {}
    for path in _package_modules():
        for node in ast.walk(_parsed(path)):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                calls.setdefault(node.name, set()).update(_called_names(node))
    reaching = {name for name, callees in calls.items() if marker in callees}
    while True:
        grown = reaching | {name for name, callees in calls.items() if callees & reaching}
        if grown == reaching:
            return frozenset(reaching)
        reaching = grown


@cache
def _olog_write_tools() -> int:
    """Registered tools that reach the Olog write gate.

    The marker is the gate itself (``get_olog_safety``), never a name prefix and never a hand-kept
    tuple: "which tools are behind the write gate" is the question the guide's heading asks, and
    asking it of the CALL rather than of a spelling means a fifth write tool is counted the day it
    is written rather than the day somebody remembers a list.
    """
    return len(_reaches("get_olog_safety") & frozenset(_registered_tool_functions()))


@cache
def _olog_round_trip_tools() -> int:
    """Gated write tools whose gate is SPLIT around a pre-write read.

    The guide's sentence names a MECHANISM, not two tools: the env gate and the URL boundary run
    BEFORE the target entry is read, the logbook allowlist after it. A path that is split calls
    BOTH halves itself; a path that is not calls only ``check_write_preconditions``, which runs the
    early half internally.

    ⛔ "ONLY A PATH THAT READS BEFORE IT WRITES CALLS check_write_env_and_url" IS FALSE, and this
    docstring asserted it for one commit. Measured at ``olog_safety.py``: ``check_write_-
    preconditions`` calls it too, on EVERY write path, and says so in its own docstring. The first
    version answered 2 only because its call graph stopped one module short of the gate; widening
    the graph, which is an obvious tidy-up, turned it into 5 and would have made a CORRECT sentence
    red. A derivation that depends on where the reader stops looking is not a derivation.

    Requiring both calls in the same function is the sentence's own criterion and survives the
    wider graph.
    """
    split = frozenset(
        node.name
        for path in _package_modules()
        for node in ast.walk(_parsed(path))
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and {"check_write_env_and_url", "check_write_preconditions"} <= _called_names(node)
    )
    return len(_reaches_any(split) & frozenset(_registered_tool_functions()))


@cache
def _blank_refused_filters() -> int:
    """Olog search filters refused client-side when blank, counted at the refusal itself.

    ``_reject_blank_filter`` is called once per guarded filter, so the call sites ARE the set. The
    alternative, a list of field names, is the construction this module rejects everywhere else:
    it answers correctly until somebody guards a third filter.
    """
    return sum(
        1
        for node in ast.walk(_parsed(_SRC / "services" / "olog_client.py"))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_reject_blank_filter"
    )


@cache
def _display_tools_taking_a_context_cap() -> int:
    """Display tools that expose ``context_cap``; the guide's "the other three".

    Deliberately the INTERSECTION and not just the parameter scan: the sentence says "the other
    three DISPLAY tools", so a core tool growing a ``context_cap`` must not move it.

    ⛔ LOUD RATHER THAN LENIENT ON THE COMPLEMENT, and that is a repair rather than a flourish.
    The sentence means "all display tools except the one that exposes neither cap", and this
    measures "the display tools that have one". The two coincide only while the complement is
    exactly one. An adversarial pass proved the gap with a fifth display tool that takes no cap:
    ``_display_tools`` reddened LOUDLY at the larger count, an author would dutifully re-number
    that sentence, and the "other display tools" sentence beside it would then be wrong by one
    with nothing left to say so. Raising here means the loud alarm cannot lead past the silent one.
    """
    with_cap = _tools_declaring_parameter("context_cap") & frozenset(_display_tool_names())
    without_cap = frozenset(_display_tool_names()) - with_cap
    if len(without_cap) != 1:
        raise AssertionError(
            "the guide's sentence rests on exactly ONE display tool exposing neither cap, with "
            f"the rest taking a context_cap; measured, {sorted(without_cap)} take none. Re-read "
            "the sentence about the other display tools before touching this number."
        )
    return len(with_cap)


@cache
def _untyped_tools_full_lane() -> int:
    """Registered tools with no typed output schema, in the lane that has the display tools.

    The figure ``[GQ-117]`` re-measured by hand and could not register, because the sentence around
    it elided its noun ("ten today") and the detector is blind to that shape by construction. It is
    a claim in the PRESENT about a set this repository computes, so the honest treatment is a
    derivation and not a better adjective.
    """
    return _full_lane_tools() - len(ts._TYPED_OUTPUT_TOOLS)


@cache
def _untyped_tools_core_lane() -> int:
    """The same, in the lane CI runs, i.e. without the display tools.

    Subtracting the typed tools that are NOT display tools, rather than subtracting all of them: on
    today's tree every display tool is untyped and the two arithmetics agree, which is exactly when
    the difference is cheap to write down. The day one display tool grows an output schema the lazy
    form answers one too low, and nothing would say so.
    """
    return _core_lane_tools() - len(ts._TYPED_OUTPUT_TOOLS - _display_tool_names())


@cache
def _file_mode_fields() -> int:
    """The fields a file-mode display answer travels with, from the dict literal that builds them.

    ⛔ THIS WAS AN _FROZEN ROW WITH A REASON THAT WAS SIMPLY WRONG. It said the fields "are
    produced by the ``opi_navigation`` engine, an optional dependency CI does not install, so any
    derivation would be lane-dependent by construction". Measured, they are three string keys of a
    dict literal in ``tools/validate.py``, and an AST count over that literal imports nothing, so
    it is exactly as lane-independent as ``_display_tools``, which this module already AST-scans
    for that very reason. An adversarial pass read the file and said so.
    """
    return max(
        (
            len(node.value.keys)
            for node in ast.walk(_parsed(_SRC / "tools" / "validate.py"))
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Dict)
            and any(
                isinstance(target, ast.Name) and target.id == "file_mode_fields"
                for target in node.targets
            )
        ),
        default=0,
    )


@cache
def _display_tool_names() -> frozenset[str]:
    """The registered tools that ``display_tools.py`` defines, by name."""
    defined = {
        node.name
        for node in ast.walk(_parsed(_SRC / "display_tools.py"))
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    return frozenset(defined & set(_registered_tool_functions()))


# --- the write gates: how many checks each one really has ---------------------------------------

# In from [GQ-126], the commit that put ``checks`` into ``prose_numbers.COLLECTION_NOUNS``. Seven
# sentences across ``server.py`` and the shipped guide state a gate's width, five of them the same
# number, and until that commit the detector could not see one of them. The class is the expensive
# one: a figure repeated five times and compared nowhere is five times as likely to be wrong and
# still never noticed. ``CHANGELOG.md`` records it happening, the guide listed four of the Olog
# gate's six checks, twice in a row.
#
# THE WIDTH IS NOT RE-DERIVED HERE. ``tests/test_write_gate_contract.py`` already counts it off the
# audited deny call sites, resolving the ``error_code`` parameter position from each module's own
# ``_audit_deny`` signature because the two gates disagree about it, and failing LOUDLY on a call
# site it cannot read. A second AST reader for the same question is a second thing to keep right.
#
# ⚠️ WHAT THAT MEANS FOR ``reads``, because an import is invisible to the tracer and would otherwise
# be the undeclared stand-in that field exists to stop: the borrowed helper is a PURE FUNCTION OF A
# TREE and opens nothing. The SOURCE is the gate module; this measure parses it ITSELF through
# ``_parsed``, which is what ``reads`` declares and what the tracer confirms.


def _gate_check_count(module: str) -> int:
    """How wide a write gate is, from the audited deny call sites its module really has."""
    return sum(write_gate._audit_deny_error_codes(module, _parsed(_SRC / module)).values())


def _gate_size_of(module: str) -> Callable[[], int]:
    """A closure per row, a loop variable captured directly would measure the LAST module."""

    def measure() -> int:
        return _gate_check_count(module)

    return measure


#: Which write gate a size-naming SCOPE speaks about. File AND scope, because ``server.py`` and the
#: shipped guide each talk about BOTH gates. That is exactly why ``test_write_gate_contract.py``
#: keeps them out of its own sweep: it reads a FILE at a time and says at ``_SINGLE_GATE_FILES``
#: that a mixed file "would need sentence-level attribution". This guard reads a BLOCK at a time, so
#: that attribution is the one thing it has, and the rows below are it. ⚠️ A BLOCK is not always
#: fine enough either: ``SECURITY.md`` states both widths in one sentence, which is what ``lead``
#: answers, and the paragraph on it further down is the one to read before adding a row.
#:
#: ⛔ HONEST SCOPE, and it has to be read before this family is taken for coverage of the gate
#: sizes, because the thing it does NOT do is the thing that historically went wrong. It compares a
#: NUMBER to the code. It never compares a LIST to the code. That is verbatim the defect
#: ``CHANGELOG.md`` records, "the shipped operator guide listed four of the Olog gate's six
#: checks", and this family would have reported the number as correct while it happened.
#:
#: ⭐ SINCE [GQ-132] THE LIST HALF IS SOMEBODY'S JOB: ``tests/test_gate_lists.py`` counts the
#: enumerations of the write-gate estate and compares each to the measurement that OWNS it,
#: this family's ``_gate_check_count`` among them, so the guide's Olog bullets, the two gate
#: docstrings and the ``AND``/``+``/comma chains in ``server.py`` are covered. ⚠️ Not every
#: row borrows from HERE: the refuse-to-start conditions of the contract page go to
#: ``test_write_gate_contract._start_conditions``, which this family never uses, and
#: ``update_log_entry``'s parenthesis is registered as a deliberate SUBSET and compared to no
#: gate at all. ⚠️ This family is
#: still a NUMBER guard and gains nothing from that: the two halves are separate tests over
#: separately keyed populations, and a passage can be in one and not the other. What remains
#: uncovered by BOTH is stated where it can be acted on rather than here: the sub-counts ("the
#: first four are refused before any I/O") and the ordinals ("the first and the fifth"), which
#: name no size at all, and every counted list outside the gate estate, which
#: ``tests/test_gate_lists.py`` refuses on a measured false-alarm rate rather than on taste.
#:
#: ⚠️ Two further limits, both measured rather than feared. A sentence about the write-gate
#: CONTRACT's six requirements landing in an Olog scope would be permanently green, because only
#: the value is compared and never which of the four questions the sentence asks; the contract page
#: says itself that "the two sixes are unrelated and land on the same value by accident". And a
#: row that carries NO ``lead`` separates the two gates ACROSS files, not WITHIN one: a correct
#: Olog sentence written into ``server.py``'s module scope would be accused against the PV gate's
#: three, with a message telling the reader to break a true sentence.
#: ⭐ THAT SECOND LIMIT IS WHY ``lead`` EXISTS, and it stopped being hypothetical with [GQ-139]:
#: ``SECURITY.md`` states BOTH widths inside one markdown section, so the two rows that judge it
#: cannot be told apart by (file, scope) at all. What tells them apart is a required PREFIX, and
#: what keeps a leadless row from silently swallowing a second gate's sentence is
#: ``test_no_gate_size_row_judges_two_different_sizes``, which reports the collision as a keying
#: problem IN ADDITION TO the value comparison reporting it as a prose problem. ⚠️ Not "instead
#: of": measured on the mutant, both reds appear, and the test's own docstring says so.
#:
#: ⛔ WHAT THIS FAMILY STILL DOES NOT REACH, named here because reach is what it failed at three
#: times running and because a guard that does not say where it stops reads as if it stopped
#: nowhere. FOUR further documents a human reads state a write-gate width that no NUMBER guard
#: holds. The price of admitting each was measured on 2026-08-22, as sites in that file and as the
#: share of ITS commits since 2026-07-01 that moved its site set: ``docs/safety.md`` (5 sites,
#: 3 of 28) and ``README.md`` (2 sites, 16 of 108) and ``ARCHITECTURE.md`` (4 sites, 3 of 13) and
#: ``docs/deployment.md`` (7 sites, 6 of 48).
#: ⚠️ WHICH READING, because condition 3 above admits two and they disagree here. These count a
#: commit as moving the set when the KEY SET changes, value included, which is what a ``_FROZEN``
#: row is keyed by and therefore the closer measure of the bookkeeping. Counting only a change in
#: the NUMBER of sites, the site half of what ``test_inventory_size_is_pinned`` compares, gives
#: 4, 10, 3 and 6 for the same four files: neither reading dominates the other, because a file can
#: hold the same phrase twice under one heading. Both readings agree on ``SECURITY.md``.
#: Only ``docs/safety.md`` already has the gate LIST row
#: that ``test_every_gate_size_scope_has_a_list_row`` would demand; the other three need one
#: written first. ``CHANGELOG.md`` states the width too and stays out on condition 2, a release
#: entry must keep saying what it said. The two gate MODULES are covered elsewhere, by
#: ``test_write_gate_contract._SINGLE_GATE_FILES``.
#:
#: ⛔ NO TREE-WIDE TOTAL IS GIVEN HERE, for the reason the ``_GATE_CHECKS`` paragraph below gives
#: at length and for a second one measured on this very comment. [GQ-139] wrote such a total into
#: this spot and re-measured after the edit: it had already moved, because the SAME commit added one
#: matching phrase to ``SECURITY.md`` and another to the docstring of
#: ``test_no_gate_size_row_judges_two_different_sizes``, which quotes the shape as its example.
#: ⚠️ WHAT JOINED THE COUNT WAS THE QUOTATION, NOT THE FIGURE, and a first draft of this very
#: paragraph got that wrong: measured, neither "43 occurrences" nor "the tree holds 45 occurrences"
#: matches ``_GATE_CHECKS``, because the pattern wants the counted NOUN behind the number. A total
#: is therefore not unwritable, it is merely unmaintainable, and it stays out on the second ground:
#: nothing here would ever compare it. The four per-file prices above survive precisely because
#: they are properties of OTHER files, which a later edit of THIS file cannot move.
#: Re-derive a total by running ``_GATE_CHECKS`` over ``git ls-files "*.py" "*.md"`` and
#: reading every hit; the run behind the four figures is in
#: ``analysis/gq139-torgroessen-2026-08-22/`` in the workspace. Most of what such a sweep finds
#: outside a watched file is this estate's own guards QUOTING the sentences they watch, which is
#: also why no tree-wide sweep is proposed: those files are deliberately unwatched, and a sweep
#: would report their explanations as findings.
@dataclass(frozen=True)
class _GateScope:
    """One passage that names a write gate's width, and which gate it is about.

    ``path`` AND ``scope``, because ``server.py`` and the shipped guide each talk about BOTH gates.

    ``lead`` is a LITERAL prefix that narrows a row WITHIN its scope, for the case (file, scope)
    cannot express: a section that states both widths. It is empty for every row that is alone in
    its scope, so those rows keep exactly the pattern they had.

    ⛔ LITERAL, NOT A PATTERN, and it is escaped where the pattern is built. The first version took
    it as a raw regular expression and guarded only that it opened no capturing group, which an
    adversarial pass answered with ``r"the .* gate "``: no group, so the guard let it through, and
    the greedy gap then binds the row to the LAST matching phrase in the block instead of the
    meant one. Where value and module then disagree the comparison still catches it; where they
    happen to agree it is silent. Escaping removes the class instead of testing for it, and it
    gives ``lead`` the semantics ``gate_lists`` already gives its anchors: when the passage is
    reworded the anchor stops matching, the claim goes orphaned, and a human reads the passage
    again. That is the right failure, not a limitation.

    The second rule has no such structural answer and keeps its test: a row may not end up owning
    phrases that name different sizes, which is what happens when a leadless row is left standing
    in a scope that grew a second gate's sentence.
    """

    path: str
    scope: str
    module: str
    lead: str = ""


_GATE_SIZE_SCOPES: tuple[_GateScope, ...] = (
    _GateScope("server.py", "<module>", "safety.py"),
    _GateScope("operator_guide.md", "Posture (read this first)", "safety.py"),
    _GateScope("operator_guide.md", "Olog write posture (all four write tools)", "olog_safety.py"),
    _GateScope(
        "operator_guide.md",
        "The write gates, the read throttle, and a server that declines to start",
        "olog_safety.py",
    ),
    _GateScope("server.py", "create_log_entry", "olog_safety.py"),
    _GateScope("server.py", "add_log_attachment", "olog_safety.py"),
    _GateScope("server.py", "update_log_entry", "olog_safety.py"),
    # The security page, in from [GQ-139]. Both widths in ONE section, so both rows need a lead;
    # the LIST half of the same passage has been held since [GQ-132] on the very same key.
    _GateScope("SECURITY.md", "Security posture", "safety.py", lead=r"the PV gate is "),
    _GateScope("SECURITY.md", "Security posture", "olog_safety.py", lead=r"the Olog gate "),
)

#: A gate-size sentence, in both shapes this estate writes it: "three gate checks" and the bare
#: "six checks", the guide bolding either word.
#:
#: ⚠️ ``gate`` is OPTIONAL here and REQUIRED in ``test_write_gate_contract._GATE_SIZE_RE``, which
#: excludes the bare form on purpose because "the two checks split out of this one" is a real
#: sentence in ``olog_safety.py``. Several of the sentences the rows above cover carry ONLY the
#: bare form, so the word cannot be required; what replaces it is the row itself, file plus scope
#: plus, where one section states both widths, the row's ``lead``.
#: Measured over the tracked tree the loose shape also pairs with phrases that name no gate at all,
#: and NOT ONE of them sits in a watched file: the narrowness is carried entirely by
#: ``_GATE_SIZE_SCOPES``, which is also why ``path`` had to exist before this family could.
#:
#: ⛔ NO FIGURE IS GIVEN for how many such phrases there are, and the reason is worth the space
#: because it is this package's own subject turned on itself. This comment named one, "nine", and
#: an adversarial pass called it wrong. It was not miscounted: nine was EXACT against the tree it
#: was measured on. It became ten when the comment was committed, because the sentence you are
#: reading quotes "the two checks split out of this one" as its counter-example, and the sweep
#: counts that quotation. ⚠️ A figure that counts a population its own sentence then joins cannot
#: be written down here at all, and THIS FILE IS UNWATCHED, so nothing would have gone red either
#: way. The rule matters too and nobody had stated it: "the value is neither gate size" and "the
#: sentence is not about a gate" give different answers, because one file states a release-gate
#: size that coincidentally equals the PV gate's. Re-derive with the rule you mean: run this
#: pattern over ``git ls-files "*.py" "*.md"`` and read every hit.
#:
#: ⛔ THE CAPTURE IS THE DETECTOR'S OWN NUMBER GRAMMAR, never ``(\w+)`` and never a second list of
#: number words. Both were tried and both are wrong. ``(\w+)`` reads the ``s`` of "that one test's
#: checks", ``parse_count`` answers ``None`` and the comparison reports "prose says None". A
#: hand-typed list of number words has no lookbehind, so "twenty-three gate checks", "SEC-3 checks"
#: and "forty-three checks" all read as 3 and pass GREEN, while ``_PAIRED`` refuses all three and
#: leaves no site for ``test_inventory_is_partitioned`` to catch them either. Borrowing ``_NUMBER``
#: makes "a phrase this claim can cover" and "a phrase the detector can see" the same set by
#: construction.
_GATE_CHECKS = rf"({pn._NUMBER})\*{{0,2}}\s+\*{{0,2}}(?:gate\*{{0,2}}\s+)?checks\b"


def _gate_scope_pattern(row: _GateScope) -> str:
    """A row's phrase pattern: the shared shape, optionally behind the row's own literal prefix.

    ``_GATE_CHECKS`` stays the TAIL of every pattern, so the capture group is the detector's own
    number grammar for a leadless and a led row alike. A lead only decides WHICH of a scope's
    phrases the row owns; it never decides what counts as a number, and ``re.escape`` is what makes
    that structural rather than a rule somebody has to keep. See ``_GateScope``.
    """
    return f"{re.escape(row.lead)}{_GATE_CHECKS}" if row.lead else _GATE_CHECKS


def _gate_size_claims() -> tuple[_Claim, ...]:
    """One claim per sentence family, generated so each row is authored exactly once.

    No figure for how many rows that is: this module is deliberately unwatched, so a count written
    here rots exactly the way the counts it guards do. ``len(_GATE_SIZE_SCOPES)`` answers it.
    """
    return tuple(
        _claim(
            f"{row.module} width in {row.path} [{row.scope}]",
            _gate_scope_pattern(row),
            _gate_size_of(row.module),
            reads=(row.module,),
            scope=row.scope,
            path=row.path,
        )
        for row in _GATE_SIZE_SCOPES
    )


# --- the claims -------------------------------------------------------------------------------

_TYPED = "_TYPED_OUTPUT_TOOLS"
_MAPPED = r"EXACTLY the (\w+) mapped fields"

# The per-tool "EXACTLY the N mapped fields" line: one row per typed tool, bound by the test it
# lives in. Every typing step adds one, which is why the family is derived rather than inventoried.
# The binding is hand-kept, so it is held against the tests themselves by
# test_every_mapped_field_row_names_the_test_that_reads_its_map ([GQ-400]), and _tool_pinned_by
# reads the same rows to find the tool a per-tool map is held against.
_MAPPED_FIELD_CLAIMS: tuple[tuple[str, str], ...] = (
    ("test_archiver_history_exposes_typed_output_schema", "_ARCHIVER_HISTORY_BASE_TYPE"),
    ("test_alarm_configured_exposes_typed_output_schema", "_ALARM_CONFIGURED_BASE_TYPE"),
    ("test_name_lookup_exposes_typed_output_schema", "_NAME_LOOKUP_BASE_TYPE"),
    ("test_is_archived_exposes_typed_output_schema", "_ARCHIVE_STATUS_BASE_TYPE"),
    ("test_list_archived_pvs_exposes_typed_output_schema", "_LIST_ARCHIVED_PVS_BASE_TYPE"),
    ("test_get_appliance_info_exposes_typed_output_schema", "_GET_APPLIANCE_INFO_BASE_TYPE"),
    ("test_get_archive_info_exposes_typed_output_schema", "_GET_ARCHIVE_INFO_BASE_TYPE"),
    (
        "test_list_channel_vocabulary_exposes_typed_output_schema",
        "_LIST_CHANNEL_VOCABULARY_BASE_TYPE",
    ),
    ("test_get_alarm_history_exposes_typed_output_schema", "_GET_ALARM_HISTORY_BASE_TYPE"),
    ("test_discover_pvs_exposes_typed_output_schema", "_DISCOVER_PVS_BASE_TYPE"),
    ("test_find_channels_exposes_typed_output_schema", "_FIND_CHANNELS_BASE_TYPE"),
)


def _size_of(constant: str) -> Callable[[], int]:
    """A closure per row, a loop variable captured directly would measure the LAST constant."""

    def measure() -> int:
        return len(getattr(ts, constant))

    return measure


def _mapped_field_claims() -> tuple[_Claim, ...]:
    """One claim per row, each DECLARING the constant it measures, generated, not typed out.

    The declaration comes from the same row as the measure, so the two cannot drift apart. That is
    the one place in this table where generating ``reads`` is right rather than circular: the row
    IS the authored statement, and it is authored once instead of eleven times.
    """
    return tuple(
        _claim(f"{constant} size", _MAPPED, _size_of(constant), reads=(constant,), scope=test)
        for test, constant in _MAPPED_FIELD_CLAIMS
    )


_CLAIMS: tuple[_Claim, ...] = (
    # --- family 1: the typed-tool cardinality, re-stated on every typing step -------------------
    _claim(
        "typed tools (recursive traversal)",
        r"traversal of all (\w+) schemas",
        lambda: len(ts._TYPED_OUTPUT_TOOLS),
        reads=("_TYPED_OUTPUT_TOOLS",),
    ),
    _claim(
        "typed tools (recursive walk)",
        r"walk of all (\w+) typed schemas",
        lambda: len(ts._TYPED_OUTPUT_TOOLS),
        reads=("_TYPED_OUTPUT_TOOLS",),
    ),
    _claim(
        "typed tools (scope note)",
        rf"properties of the (\w+) tools in {_TYPED}",
        lambda: len(ts._TYPED_OUTPUT_TOOLS),
        reads=("_TYPED_OUTPUT_TOOLS",),
    ),
    _claim(
        "typed tools (scan note)",
        r"a scan of all (\w+) schemas",
        lambda: len(ts._TYPED_OUTPUT_TOOLS),
        reads=("_TYPED_OUTPUT_TOOLS",),
    ),
    # --- family 1b: what the displays group GATES, named in the header builder's own docstring ---
    # Added because the phrase arrived UNGUARDED and left this module red on the commit that wrote
    # it: c5d4ad7 put "gates FOUR tools" into build_instructions' docstring and touched neither
    # _CLAIMS nor _FROZEN nor _INVENTORY_SIZES, so server.py carried three size-naming phrases
    # against a pin of two. Derived rather than frozen, because the number does follow from a set:
    # it is exactly what display_tools.py registers, which is the side that would move if a fifth
    # display tool were ever added.
    _claim(
        "display-gated tools (header docstring)",
        r"gates (\w+) tools",
        _display_tools,
        reads=("display_tools.py",),
        scope="build_instructions",
    ),
    # --- family 2: the untyped remainder --------------------------------------------------------
    _claim(
        "untyped remainder",
        r"list fields of the (\w+) tools that are not typed yet",
        lambda: _full_lane_tools() - len(ts._TYPED_OUTPUT_TOOLS),
        reads=("_TYPED_OUTPUT_TOOLS", "server.py", "display_tools.py"),
    ),
    # --- family 3: the rows of _ALWAYS_PRESENT_BY_TOOL and the twelve constants ------------------
    # ⚠️ ``reads`` cannot go red here, and a post-build QA is right to say so: the measure only
    # PARSES, so the tracer's attribute half is empty on both sides and its file half is
    # one-directional. What holds this claim to its set is the measure's own refusals, not the
    # declaration. ``path`` keeps the module-level phrase inside the file that carries it.
    _claim(
        "explicit rows",
        r"the (\w+) explicit rows point straight at",
        _explicit_always_present_rows,
        reads=("tests/test_server.py",),
        path="tests/test_server.py",
    ),
    _claim(
        "static-check constants",
        r"(\w+) of the \w+ are consulted only to",
        lambda: _always_present_constants() - _runtime_bound_constants(),
        reads=("__dict__", "tests/test_server.py"),
    ),
    _claim(
        "always-present constants",
        r"\w+ of the (\w+) are consulted only to",
        _always_present_constants,
        reads=("__dict__",),
    ),
    _claim(
        "runtime-bound constants",
        r"The remaining (\w+), _DISCOVER_PVS_ALWAYS_PRESENT",
        _runtime_bound_constants,
        reads=("tests/test_server.py",),
    ),
    _claim(
        "in-process conformance tests",
        r"(\w+) of the \w+ drive ``FastMCP.call_tool``",
        _in_process_conformance_tests,
        reads=("tests/test_server.py",),
    ),
    _claim(
        "conformance tests",
        r"\w+ of the (\w+) drive ``FastMCP.call_tool``",
        _conformance_tests,
        reads=("tests/test_server.py",),
    ),
    _claim(
        "real-client conformance tests",
        r"The other (\w+), test_discover_pvs",
        _real_client_conformance_tests,
        reads=("tests/test_server.py",),
    ),
    _claim(
        "tools those ten cover",
        r"of the (\w+) tools THOSE TEN cover",
        lambda: len(_tools_named_by_in_process_tests()),
        reads=("_TYPED_OUTPUT_TOOLS", "tests/test_server.py"),
    ),
    # --- family 4: the element-schema split ------------------------------------------------------
    # ALIASING versus ADVERTISED, and the two halves genuinely differ. The shared-dict note is
    # about what assigning into a module dict would move, so it reads IDENTITY; the two sentences
    # below it say what the rows carry, so they read the VALUE. One measure served all four and was
    # therefore wrong for two of them.
    _claim(
        "string-item rows (shared-dict note)",
        r"silently move (\w+) \(resp",
        lambda: _rows_sharing(ts._STRING_ITEMS),
        reads=("_OUTPUT_ARRAY_ITEMS", "_STRING_ITEMS"),
    ),
    _claim(
        "opaque-item rows (shared-dict note)",
        r"silently move \w+ \(resp\. (\w+)\) rows",
        lambda: _rows_sharing(ts._OPAQUE_OBJECT_ITEMS),
        reads=("_OUTPUT_ARRAY_ITEMS", "_OPAQUE_OBJECT_ITEMS"),
    ),
    # These two carried the plainest phrases in the table, "most for the N rows carrying" and "the
    # other N rows are", with no scope, no path and no mention of the schema they count. Measured on
    # 2026-09-16: a TRUE neighbouring sentence about a different set, written in the estate's own
    # voice above _OUTPUT_ARRAY_ITEMS, was accused by the second one with an instruction to break
    # it. Each is keyed on the test whose docstring states the split AND anchored on the element
    # schema it counts, so the phrase binds what it is about rather than what it sounds like.
    _claim(
        "string-item rows",
        r'most for the (\w+) rows carrying ``\{"type": "string"\}``',
        lambda: _rows_advertising(ts._STRING_ITEMS),
        reads=("_OUTPUT_ARRAY_ITEMS", "_STRING_ITEMS"),
        scope=_ELEMENT_SCHEMA_TEST,
        path="tests/test_server.py",
    ),
    _claim(
        "opaque-item rows",
        r'the other (\w+) rows are ``\{"type": "object", "additionalProperties": true\}``',
        lambda: _rows_advertising(ts._OPAQUE_OBJECT_ITEMS),
        reads=("_OUTPUT_ARRAY_ITEMS", "_OPAQUE_OBJECT_ITEMS"),
        scope=_ELEMENT_SCHEMA_TEST,
        path="tests/test_server.py",
    ),
    # --- family 5: the declared arrays -----------------------------------------------------------
    _claim(
        "declared arrays",
        r"of the (\w+) declared arrays",
        lambda: len(ts._OUTPUT_ARRAY_ITEMS),
        reads=("_OUTPUT_ARRAY_ITEMS",),
    ),
    _claim(
        "nullable arrays",
        r"(\w+) of the \w+ declared arrays",
        _nullable_array_rows,
        reads=("_OUTPUT_ARRAY_ITEMS", "server.py", "display_tools.py", "tools/archiver.py"),
    ),
    _claim(
        "nullable arrays (schema-test note)",
        r"the (\w+) rows happen to match",
        _nullable_array_rows,
        reads=("_OUTPUT_ARRAY_ITEMS", "server.py", "display_tools.py", "tools/archiver.py"),
    ),
    _claim(
        "shared element schemas",
        r"The (\w+) element schemas the estate",
        _distinct_element_schemas,
        reads=("_OUTPUT_ARRAY_ITEMS",),
    ),
    # --- the return-path tables the real-client conformance tests drive ---------------------------
    # These two and the tool-description claim below describe the TOOL rather than the table they
    # are measured against, so they read ``_return_paths_as_driven``, whose docstring carries what
    # that costs and why no derivation from the code replaces it ([GQ-400]).
    _claim(
        "discover_pvs return paths",
        r"discover_pvs emits on EVERY one of its (\w+) return paths",
        lambda: _return_paths_as_driven(_DISCOVER_CONFORMANCE),
        scope="<module>",
        reads=("tests/test_server.py",),
    ),
    # Anchored on the TOOL NAME. One pattern matched BOTH module-level sentences, so the
    # find_channels claim was verified against discover_pvs' table. Equal today, which means the
    # day either tool gains a path, the guard would demand the CORRECT sentence be changed back.
    _claim(
        "find_channels return paths",
        r"find_channels emits on EVERY one of its (\w+) return paths",
        lambda: _return_paths_as_driven(_FIND_CHANNELS_CONFORMANCE),
        scope="<module>",
        reads=("tests/test_server.py",),
    ),
    _claim(
        "discover_pvs paths (part A)",
        r"drives ALL (\w+) return paths",
        lambda: _paths_rows(_DISCOVER_CONFORMANCE),
        scope=_DISCOVER_CONFORMANCE,
        reads=("tests/test_server.py",),
    ),
    _claim(
        "discover_pvs paths (matter)",
        r"ALL (\w+) paths matter",
        lambda: _paths_rows(_DISCOVER_CONFORMANCE),
        scope=_DISCOVER_CONFORMANCE,
        reads=("tests/test_server.py",),
    ),
    _claim(
        "discover_pvs paths (union)",
        r"The (\w+) paths together",
        lambda: _paths_rows(_DISCOVER_CONFORMANCE),
        scope=_DISCOVER_CONFORMANCE,
        reads=("tests/test_server.py",),
    ),
    _claim(
        "find_channels paths (modes)",
        r"(\w+) paths, and here the \w+ are two MODES",
        lambda: _paths_rows(_FIND_CHANNELS_CONFORMANCE),
        scope=_FIND_CHANNELS_CONFORMANCE,
        reads=("tests/test_server.py",),
    ),
    _claim(
        "find_channels paths (rows)",
        r"makes all (\w+) rows load-bearing",
        lambda: _paths_rows(_FIND_CHANNELS_CONFORMANCE),
        scope=_FIND_CHANNELS_CONFORMANCE,
        reads=("tests/test_server.py",),
    ),
    _claim(
        "find_channels paths (load-bearing)",
        r"the reason all (\w+) paths are load-bearing",
        lambda: _paths_rows(_FIND_CHANNELS_CONFORMANCE),
        scope=_FIND_CHANNELS_CONFORMANCE,
        reads=("tests/test_server.py",),
    ),
    _claim(
        "find_channels paths (coverage)",
        r"satisfiable by two of the (\w+)",
        lambda: _paths_rows(_FIND_CHANNELS_CONFORMANCE),
        scope=_FIND_CHANNELS_CONFORMANCE,
        reads=("tests/test_server.py",),
    ),
    _claim(
        "real-client paths (wire note)",
        r"drive a real client over (\w+) paths",
        _return_path_rows,
        reads=("tests/test_server.py",),
    ),
    # find_channels' OWN table, not the union: _return_path_rows raises when the two conformance
    # tables disagree, so binding this find_channels sentence to it would turn a discover_pvs
    # change into an error across all six tests, naming neither the file nor the prose.
    _claim(
        "find_channels return paths (tool description)",
        r"the (\w+) paths differ further",
        lambda: _return_paths_as_driven(_FIND_CHANNELS_CONFORMANCE),
        scope="find_channels",
        reads=("tests/test_server.py",),
    ),
    _claim(
        "find_channels keys",
        r"permits all (\w+) keys",
        lambda: len(ts._FIND_CHANNELS_BASE_TYPE),
        reads=("_FIND_CHANNELS_BASE_TYPE",),
    ),
    # --- the per-tool mapped-field lines ---------------------------------------------------------
    *_mapped_field_claims(),
    # --- the ``X | None`` subsets of the per-tool maps --------------------------------------------
    # The claims below read their map through ``_none_valued``, which is why each declares the
    # sources that reading touches: its own map, the test that holds the map against the wire, the
    # registrar the tool's result type is resolved through, and the module that declares it. No
    # figure for how many rows that is: this module is deliberately unwatched, so a count written
    # here rots exactly the way the counts it guards do.
    _claim(
        "archive-status enrichment fields",
        r"The (\w+) DS-4A/AR-D enrichment fields",
        lambda: _none_valued("_ARCHIVE_STATUS_BASE_TYPE"),
        reads=(
            "_ARCHIVE_STATUS_BASE_TYPE",
            "tests/test_server.py",
            "server.py",
            "services/checkers.py",
        ),
    ),
    # The detector cannot see this one: four words sit between the number and its noun and the gap
    # allows two. A claim is matched against the block text directly, so it guards the sentence
    # anyway, and it must, because its twin thirty lines away IS derived. Which of two sentences
    # about the same set is watched may not depend on how the author spelled the type.
    _claim(
        "archive-status enrichment fields (exposes note)",
        r"The (\w+) ``object \| None`` enrichment fields",
        lambda: _none_valued("_ARCHIVE_STATUS_BASE_TYPE"),
        scope="test_is_archived_exposes_typed_output_schema",
        reads=(
            "_ARCHIVE_STATUS_BASE_TYPE",
            "tests/test_server.py",
            "server.py",
            "services/checkers.py",
        ),
    ),
    _claim(
        "archive-status enrichment fields (runtime note)",
        r"status \+ the (\w+) enrichment fields",
        lambda: _none_valued("_ARCHIVE_STATUS_BASE_TYPE"),
        reads=(
            "_ARCHIVE_STATUS_BASE_TYPE",
            "tests/test_server.py",
            "server.py",
            "services/checkers.py",
        ),
    ),
    _claim(
        "appliance topology fields",
        r"the (\w+) object\|None topology fields",
        lambda: _none_valued("_GET_APPLIANCE_INFO_BASE_TYPE"),
        reads=(
            "_GET_APPLIANCE_INFO_BASE_TYPE",
            "tests/test_server.py",
            "server.py",
            "tools/archiver.py",
        ),
    ),
    _claim(
        "archive-info projection fields",
        r"The (\w+) getPVTypeInfo projection fields",
        lambda: _none_valued("_GET_ARCHIVE_INFO_BASE_TYPE"),
        reads=(
            "_GET_ARCHIVE_INFO_BASE_TYPE",
            "tests/test_server.py",
            "server.py",
            "tools/archiver.py",
        ),
    ),
    _claim(
        "archive-info type-info fields",
        r"the (\w+) object\|None type-info fields",
        lambda: _none_valued("_GET_ARCHIVE_INFO_BASE_TYPE"),
        reads=(
            "_GET_ARCHIVE_INFO_BASE_TYPE",
            "tests/test_server.py",
            "server.py",
            "tools/archiver.py",
        ),
    ),
    # --- the same subsets, re-stated in src/ where the values are produced -----------------------
    _claim(
        "enrichment fields (validator note)",
        r"bite on these (\w+) fields",
        lambda: _none_valued("_ARCHIVE_STATUS_BASE_TYPE"),
        reads=(
            "_ARCHIVE_STATUS_BASE_TYPE",
            "tests/test_server.py",
            "server.py",
            "services/checkers.py",
        ),
    ),
    _claim(
        "type-info fields (builder note)",
        r"the (\w+) type-info fields appear only",
        lambda: _none_valued("_GET_ARCHIVE_INFO_BASE_TYPE"),
        reads=(
            "_GET_ARCHIVE_INFO_BASE_TYPE",
            "tests/test_server.py",
            "server.py",
            "tools/archiver.py",
        ),
    ),
    _claim(
        "topology fields (builder note)",
        r"the (\w+) projected topology fields",
        lambda: _none_valued("_GET_APPLIANCE_INFO_BASE_TYPE"),
        reads=(
            "_GET_APPLIANCE_INFO_BASE_TYPE",
            "tests/test_server.py",
            "server.py",
            "tools/archiver.py",
        ),
    ),
    _claim(
        "topology fields (copy note)",
        r"The (\w+) fields are COPIED UNCONVERTED",
        lambda: _none_valued("_GET_APPLIANCE_INFO_BASE_TYPE"),
        reads=(
            "_GET_APPLIANCE_INFO_BASE_TYPE",
            "tests/test_server.py",
            "server.py",
            "tools/archiver.py",
        ),
    ),
    _claim(
        "ChannelInfo fields",
        r"``ChannelInfo`` \((\w+) fields, all required\)",
        _channel_info_fields,
        reads=("services/channelfinder_client.py",),
    ),
    # --- the per-tool tables the Olog and archiver-history claims point at ------------------------
    _claim(
        "olog always-present tools",
        r"uniform over all (\w+) tools",
        lambda: len(ts._OLOG_ALWAYS_PRESENT),
        reads=("_OLOG_ALWAYS_PRESENT",),
    ),
    # "Pre-fix this tripped on 8/11 tools": the numerator is a frozen historical measurement, but
    # the DENOMINATOR is the live Olog tool count and drifts with every Olog tool added.
    _claim(
        "olog tools (historical ratio)",
        r"tripped on \d+/(\w+) tools",
        lambda: len(ts._OLOG_ALWAYS_PRESENT),
        reads=("_OLOG_ALWAYS_PRESENT",),
    ),
    _claim(
        "archiver-history fields",
        r"ArchiverHistoryResult's (\w+) fields",
        lambda: len(ts._ARCHIVER_HISTORY_BASE_TYPE),
        reads=("_ARCHIVER_HISTORY_BASE_TYPE",),
    ),
    # --- the annotation tables -------------------------------------------------------------------
    # Read from the SIGNATURES, not from the tuple that sits right under the first of these two
    # sentences. The tuple binds nothing to the wire on its own; that binding is now an assertion
    # in ``test_server.py::test_title_parameter_tools_keep_their_title_property``.
    _claim(
        "title-parameter tools",
        r"The (\w+) tools that carry a parameter",
        lambda: len(_tools_declaring_parameter("title")),
        reads=("server.py", "display_tools.py"),
    ),
    _claim(
        "title-parameter tools (trap note)",
        r"the (\w+) tools with a ``title`` PARAMETER",
        lambda: len(_tools_declaring_parameter("title")),
        reads=("server.py", "display_tools.py"),
    ),
    _claim(
        "annotation hint fields",
        r"The (\w+) boolean hint fields",
        lambda: len(ts._ANNOTATION_HINTS),
        reads=("_ANNOTATION_HINTS",),
    ),
    _claim(
        "annotation hint fields (law)",
        r"with all (\w+) hint fields explicitly set",
        lambda: len(ts._ANNOTATION_HINTS),
        reads=("_ANNOTATION_HINTS",),
    ),
    _claim(
        "annotation hint fields (client note)",
        r"The (\w+) hints drive real client behaviour",
        lambda: len(ts._ANNOTATION_HINTS),
        reads=("_ANNOTATION_HINTS",),
    ),
    _claim(
        "annotation hint fields (default note)",
        r"leaves all (\w+) fields None",
        lambda: len(ts._ANNOTATION_HINTS),
        reads=("_ANNOTATION_HINTS",),
    ),
    _claim(
        "destructive golden rows",
        r"antecedent holds for exactly (\w+) tools today",
        _destructive_golden_rows,
        reads=("_ANNOTATION_GOLDEN", "_ANNOTATION_HINTS"),
    ),
    # --- the two lanes ---------------------------------------------------------------------------
    _claim(
        "core lane (displays absent)",
        r"\[displays\] absent, (\w+) tools",
        _core_lane_tools,
        reads=("server.py",),
    ),
    _claim(
        "full lane (install note)",
        r"a full \((\w+)\) install",
        _full_lane_tools,
        reads=("server.py", "display_tools.py"),
    ),
    _claim(
        "core lane (budget)",
        r"core-only lane \((\w+) tools\)",
        _core_lane_tools,
        reads=("server.py",),
    ),
    _claim(
        "full lane (budget)",
        r"the full lane \((\w+)\) pass",
        _full_lane_tools,
        reads=("server.py", "display_tools.py"),
    ),
    _claim(
        "full lane (golden map)",
        r"Covers all (\w+) full-lane tools",
        _full_lane_tools,
        reads=("server.py", "display_tools.py"),
    ),
    _claim(
        "display tools (golden map)",
        r"the (\w+) display-extra tools are absent in",
        _display_tools,
        reads=("display_tools.py",),
    ),
    _claim(
        "core lane (golden map)",
        r"core-only CI \((\w+) tools\)",
        _core_lane_tools,
        reads=("server.py",),
    ),
    _claim(
        "full lane (golden map presence)",
        r"PRESENT in the full lane \((\w+)\)",
        _full_lane_tools,
        reads=("server.py", "display_tools.py"),
    ),
    _claim(
        "core lane (drift logic)",
        r"Core-only lane \((\w+)\)",
        _core_lane_tools,
        reads=("server.py",),
    ),
    _claim(
        "core lane (annotations)",
        r"core-only CI = (\w+) tools",
        _core_lane_tools,
        reads=("server.py",),
    ),
    _claim(
        "full lane (annotations)",
        r"core-only CI = \w+ tools, full = (\w+)",
        _full_lane_tools,
        reads=("server.py", "display_tools.py"),
    ),
    _claim(
        "display tools (registrar)",
        r"registers exactly the (\w+) display tools",
        _display_tools,
        reads=("display_tools.py",),
    ),
    # The two figures [GQ-117] measured by hand and left unregistered, because the sentence around
    # them elided its noun. The sentence names its noun now, so they are derived like the rest.
    _claim(
        "untyped tools (full lane)",
        r"the full lane carries (\w+) untyped tools",
        _untyped_tools_full_lane,
        reads=("_TYPED_OUTPUT_TOOLS", "server.py", "display_tools.py"),
    ),
    _claim(
        "untyped tools (core lane)",
        r"the core-only lane (\w+) untyped tools",
        _untyped_tools_core_lane,
        reads=("_TYPED_OUTPUT_TOOLS", "server.py", "display_tools.py"),
    ),
    # --- the canonical Olog block ----------------------------------------------------------------
    _claim(
        "olog query functions",
        r"The (\w+) ``query_olog_\*`` functions",
        _olog_query_functions,
        reads=("services/checkers_olog.py",),
    ),
    # The re-export sentence is about checkers.py's import block, not about checkers_olog.py's
    # definitions, equal today, and the day a re-export is dropped they are not.
    _claim(
        "olog query functions (re-export)",
        r"re-exports the (\w+) functions",
        _olog_reexport_lines,
        reads=("services/checkers.py",),
    ),
    # --- the SHIPPED guide -----------------------------------------------------------------------
    #
    # Every row here compares a sentence a MODEL and an OPERATOR read against the code it is about.
    # Until [GQ-123] not one number in this file was compared to anything, for a reason that turns
    # out to be embarrassingly small: the reader called ``ast.parse``.
    #
    # ``scope`` is not optional decoration on this side. A markdown block is keyed by its nearest
    # heading, and the guide says "two fields" in two different sections about two different pairs;
    # an unscoped pattern would hold one section's claim against the other's set.
    _claim(
        "guide: display-aware tools (palette)",
        r"The (\w+) \*\*display-aware\*\* tools register",
        _display_tools,
        reads=("display_tools.py",),
        scope="Tool palette",
    ),
    _claim(
        "guide: display tools (glob cap)",
        r"All \*\*(\w+)\*\* display tools report it",
        _display_tools,
        reads=("display_tools.py",),
    ),
    _claim(
        "guide: display tools taking a context cap",
        r"the other (\w+) display tools take a `context_cap`",
        _display_tools_taking_a_context_cap,
        reads=("display_tools.py", "server.py"),
    ),
    _claim(
        "guide: fields of a write answer",
        r"All (\w+) fields a write answers with",
        _write_result_fields,
        reads=("readback.py",),
    ),
    _claim(
        "guide: olog write tools",
        r"Olog write posture \(all (\w+) write tools\)",
        _olog_write_tools,
        reads=("server.py", "tools/olog.py", "services/checkers_olog.py"),
    ),
    _claim(
        "guide: olog round-tripping tools",
        r"On the (\w+) round-tripping tools",
        _olog_round_trip_tools,
        reads=("server.py", "tools/olog.py", "services/checkers_olog.py"),
    ),
    _claim(
        "guide: file-mode fields",
        r"Those (\w+) fields are the FILE-MODE fields",
        _file_mode_fields,
        reads=("tools/validate.py",),
    ),
    # ⚠️ The SAME three fields are named a second time, in `tools/validate.py`, and that duplicate
    # stays unwatched on the criterion's third condition rather than on an oversight: measured
    # 2026-08-21, that file carries 9 sites, of which six are live measurements against a real
    # display tree ("1 of the 11", "41 of the 42", "257 entries"), and its site set moved on 6 of
    # 16 commits. Six frozen rows and a 38 % bookkeeping rate to compare ONE number is the trade
    # this list exists to refuse. Written down because the first draft of [GQ-123] added the file
    # without costing it, which is precisely what its own report criticises elsewhere.
    # --- display_tools.py: three sentences, one number, and the reader could always open it -----
    _claim(
        "display tools (module head)",
        r"These (\w+) tools",
        _display_tools,
        reads=("display_tools.py",),
    ),
    _claim(
        "display tools (posture note)",
        r"All (\w+) display tools share",
        _display_tools,
        reads=("display_tools.py",),
    ),
    _claim(
        "display tools (registrar docstring)",
        r"Register the (\w+) display-aware tools",
        _display_tools,
        reads=("display_tools.py",),
    ),
    _claim(
        "guide: olog filters refused when blank",
        r"the (\w+) fields disagree about what it means",
        _blank_refused_filters,
        reads=("services/olog_client.py",),
        scope="Olog search filters: what the server does with a value it does not like",
    ),
    # --- family 8: the two write-gate widths, one claim per row, see _GATE_SIZE_SCOPES ----------
    *_gate_size_claims(),
)


# --- the inventory ------------------------------------------------------------------------------

# Phrases the detector finds and NOBODY verifies, each with the reason it cannot be derived. Keyed
# by (file, enclosing scope, phrase, value), never by line number, which moves with any edit above
# it and would rot this table for a reason that is not a change in the finding.
_FROZEN: dict[tuple[str, str, str, int], str] = {
    # --- not a count at all: the detector paired a number with a noun it does not govern ---------
    ("services/checkers.py", "<module>", "1. **injected protocol checkers", 1): (
        "the marker of a numbered list, not a size"
    ),
    ("tests/test_server.py", _FIND_CHANNELS_CONFORMANCE, "one mode's fields", 1): (
        "the article in 'one mode's fields', not a count of modes"
    ),
    (
        "tests/test_server.py",
        "test_every_typed_tool_conforms_to_its_schema_over_the_wire",
        "one test's checks",
        1,
    ): (
        "the possessive in 'that one test's checks', not a count of checks. In from [GQ-126] "
        "together with the noun: every other phrase that noun opened is a write-gate width and is "
        "derived, this is the only one no set can settle"
    ),
    (
        "tests/test_server.py",
        "test_every_typed_tool_conforms_to_its_schema_over_the_wire",
        "one detects new tools",
        1,
    ): "the pronoun in 'claiming this one detects new tools would be false'",
    (
        "tests/test_server.py",
        "test_search_logbook_payload_path_is_guarded_below_the_client",
        "1``: 19 other tests",
        1,
    ): "the VALUE of EPICS_MCP_READ_RATE_LIMIT=1, not a size",
    # --- runtime measurements: no constant can settle them --------------------------------------
    (
        "tests/test_server.py",
        "test_every_typed_tool_conforms_to_its_schema_over_the_wire",
        "13 of the 20",
        13,
    ): "how many tools emit no null on the driven path, observed at runtime, not declared",
    (
        "tests/test_server.py",
        "test_search_logbook_payload_path_is_guarded_below_the_client",
        "1``: 19 other tests",
        19,
    ): "how many tests fall under a mutated env var, a suite measurement, and the sentence itself "
    "records that it had already drifted once",
    # --- claims about TEST BODIES: about code shape, not about a collection ----------------------
    (
        "tests/test_server.py",
        "test_every_typed_tool_conforms_to_its_schema_over_the_wire",
        "three per-tool assertions",
        3,
    ): "assertions inside a test body",
    ("tests/test_server.py", "test_array_items_reads_both_array_shapes", "five assertions", 5): (
        "assertions inside a test body, and about a counterfactual run of it"
    ),
    ("tests/test_server.py", "test_array_items_reads_both_array_shapes", "two paths", 2): (
        "the two branches of a helper, not a collection"
    ),
    (
        "tests/test_server.py",
        "test_output_schema_typed_only_for_typed_tools",
        "two sibling tests",
        2,
    ): ("test functions that react to one mutation, no table declares them"),
    ("tests/test_server.py", "test_output_schema_typed_only_for_typed_tools", "three tests", 3): (
        "test functions a red proof turns red, measured by running it, not declared"
    ),
    # --- structural claims about code that no constant enumerates -------------------------------
    ("server.py", "main", "three olog write paths", 3): (
        "Olog write paths through the server; no table lists them"
    ),
    ("services/checkers.py", "<module>", "three generic schema guards", 3): (
        "guards in the JSON-Schema walker; no table lists them"
    ),
    ("services/checkers_olog.py", "<module>", "four rest-plane checkers", 4): (
        "REST planes, not a Python collection, there is no plane enum to count"
    ),
    ("services/checkers_olog.py", "<module>", "two validators", 2): (
        "a fact about the fastmcp SDK, not about this repository"
    ),
    ("tools/archiver.py", "<module>", "two non-nullable fields", 2): (
        "fields present on every path of one schema, a shape claim, not a table size"
    ),
    # --- a DIFFERENT set that happens to have the same size --------------------------------------
    ("tests/test_server.py", _FIND_CHANNELS_CONFORMANCE, "two enabled paths", 2): (
        "the enabled half of the return paths, not the return-path table itself"
    ),
    ("tests/test_server.py", _FIND_CHANNELS_CONFORMANCE, "four client-edge error paths", 4): (
        "error paths inside the ChannelFinder client, a different four from the return paths"
    ),
    ("tests/test_server.py", _FIND_CHANNELS_CONFORMANCE, "two of the four", 2): (
        "how few paths satisfy the union, a coverage property, measured, not declared"
    ),
    # --- the shipped guide: what it says about a REMOTE service, which no local set decides -------
    #
    # Seven of these ten are statements about somebody else's server. That is the honest shape of a
    # guide whose job is to describe what the estate TALKS TO, and it is the reason the guide's
    # coverage stops where it does rather than a gap somebody could close by trying harder.
    ("operator_guide.md", "The planes", "two** context paths", 2): (
        "context paths of the Archiver Appliance's retrieval root, a fact about that service's URL "
        "layout; nothing in this repository enumerates them"
    ),
    (
        "operator_guide.md",
        "List archived PVs: `list_archived_pvs` (or the MGMT API directly)",
        "one of those two",
        1,
    ): "the article in 'works on ONE of those two endpoints', not a count",
    (
        "operator_guide.md",
        "List archived PVs: `list_archived_pvs` (or the MGMT API directly)",
        "one of those two",
        2,
    ): "MGMT endpoints of the Archiver Appliance, a remote API's surface, not a local collection",
    (
        "operator_guide.md",
        "Olog search filters: what the server does with a value it does not like",
        'two "blank" rows',
        2,
    ): "rows of the table above it, describing how a remote Olog reads two spellings of blank",
    (
        "operator_guide.md",
        "Discover the alarm config-tree names",
        "one of the three",
        1,
    ): "the article in 'blind to one of the three kinds', not a count",
    (
        "operator_guide.md",
        "Discover the alarm config-tree names",
        "one of the three",
        3,
    ): "document kinds the Phoebus alarm logger ORs over, a property of that server",
    # --- the shipped guide: a set that IS local but that no single declaration spans --------------
    ("operator_guide.md", "Discover the alarm config-tree names", "two tools", 2): (
        "tools that spend a config-tree name, and they spell the parameter differently "
        "(`config_name` on is_alarm_configured, `alarm_config` on coverage_audit), so the only "
        "derivation available is a hand-typed pair of names, which is the construction this module "
        "rejects everywhere else"
    ),
    ("operator_guide.md", "Posture (read this first)", "two multi-get tools", 2): (
        "tools whose REST cost scales with the input, a property measured against a live service "
        "on 2026-07-31 and stated as of that date; no declaration carries it"
    ),
    (
        "operator_guide.md",
        "The cross-plane reports: what each bucket means, and which verdicts are withheld",
        "two fields",
        2,
    ): (
        "the two coverage fields that say how far an answer can be trusted. `CoverageReport` "
        "declares many more and marks none of them as the trust pair, so the set exists only in "
        "the sentence"
    ),
    # --- the security page: one historical measurement, stated three times ------------------------
    # In with [GQ-139]. All three sit in the same bullet, the one about the ANSWER channel. TWO of
    # them describe a state of the tree on two named days in August 2026 rather than a set that
    # exists now, which is what makes them inventory rather than claims: the sentence is a record of
    # a repair, and a derivation from today's code would compare it to the wrong world. The THIRD
    # is not a size at all, it is the article in "one of the two ways"; a post-build review caught
    # this comment describing all three the same way.
    #
    # ⚠️ A bullet that RECORDS A CLOSED DISCLOSURE is watched here while `CHANGELOG.md` is refused
    # on condition 2 for looking like the same thing, and the distinction is worth stating because
    # it is not obvious. A release entry is immutable by convention: correcting it would falsify
    # the record. This bullet is current prose about the current posture that happens to narrate
    # history, and it is edited normally. The cost of that is real and was measured rather than
    # waved away: rewording "in one of the two ways" to "in either way" turns two rows stale and
    # moves the pin, for an edit that changes no claim about the world. It is inside the file's
    # measured bookkeeping share, not outside it.
    ("SECURITY.md", "Security posture", "two tools", 2): (
        "tools that once put a REST failure message into a `note` of a payload they returned "
        "successfully, named in the same sentence (`diagnose_connection`, `lookup_device_name`). "
        "The behaviour is GONE, which is the point of the sentence, so no live set can count it"
    ),
    ("SECURITY.md", "Security posture", "one of the two", 1): (
        "the article in 'in one of the two ways', not a count of anything"
    ),
    ("SECURITY.md", "Security posture", "one of the two", 2): (
        "the two ways a configured credential used to leave through the ANSWER channel, a failure "
        "message and a successful note. Both were closed on 2026-08-14; the pair exists in the "
        "record of that repair and in no declaration"
    ),
}

# Per FILE, not one total. A single number lets an addition cancel a deletion out: measured, a real
# size-naming sentence could be removed and every test stayed green because an unrelated one
# appeared elsewhere. Per file narrows that to "added and removed in the SAME file in the same
# commit". This table and ``_SITELESS_CLAIM_HITS`` below are the only hand-kept numbers outside
# ``_FROZEN``'s keys.
_INVENTORY_SIZES: dict[str, int] = {
    # 79 -> 81 with [GQ-123]: the untyped-tool figures [GQ-117] had to leave unregistered now name
    # their noun, so the detector sees them and both are compared to the registrations.
    # 81 -> 82 with [GQ-126]: the noun ``checks`` opened exactly one phrase here, and it is a
    # possessive rather than a size, so it is inventoried rather than derived.
    "tests/test_server.py": 82,
    "services/checkers_olog.py": 4,
    "services/checkers.py": 5,
    "tools/archiver.py": 5,
    # 2 -> 3 as the repair of the same defect the "display-gated tools" claim above records: the
    # third phrase has been in build_instructions' docstring since c5d4ad7 with no pin to match it.
    # 3 -> 7 with [GQ-126]: four gate-width sentences became visible at once, the PV gate's in the
    # set_pv_value registration comment and the Olog gate's in three tool docstrings. All four are
    # derived, none of them was wrong, and none of them had ever been compared to anything.
    "server.py": 7,
    # The shipped guide, in from [GQ-123]. Eight of its seventeen were compared to a set, nine were
    # inventoried, and seven of those nine are statements about a remote service.
    # 17 -> 21 with [GQ-126]: three gate-width sentences the noun ``checks`` opened, plus a fourth
    # that had to be given its noun to become visible at all. "all six are below" restated the Olog
    # gate's width in the SAME paragraph as the sentence now derived, with no noun and therefore no
    # site; a half-repair would have corrected the guarded half and left it standing, which is the
    # failure the surrounding paragraph itself records having happened once. As of this commit,
    # twelve of the twenty-one were compared to a set and nine were inventoried.
    "operator_guide.md": 21,
    # display_tools.py, in from [GQ-123]'s own QA. Three sentences, one number, all three derived
    # from `_display_tools`. It is the cheapest entry in the list (its site set moved on 0 of 18
    # commits since 2026-07-01) and it was missed by the first draft, which is the reason the
    # paragraph above no longer claims the reader was the only thing keeping files out.
    "display_tools.py": 3,
    # The security page, in from [GQ-139]. Two of the five are the write-gate widths and are
    # derived; the other three sit in one bullet, the record of a closed disclosure, and are
    # inventoried. It joined at 5 rather than 4 because the Olog width carried no noun and was
    # therefore invisible to the detector, the same half-repair the operator_guide entry above
    # records, found again in the file a security reviewer reads first.
    "SECURITY.md": 5,
}

# The second hand-kept table, and it exists because the first one is structurally blind to a whole
# class of sentence. ``_INVENTORY_SIZES`` counts detector SITES; a claim hit whose captured number
# is no site (the noun is outside ``COLLECTION_NOUNS``, or further away than the detector's gap, or
# the number stands in a parenthesis) adds nothing to it, so deleting such a sentence moves no
# per-file count. Measured on 2026-07-26 (QA finding [45]) and again on 2026-09-16: 8 of 108 claim
# hits carry no site, all of them in tests/test_server.py, six claims match twice, and no claim does
# both, so no LIVE escape exists today. The escape needs a site-less sentence whose claim also
# matches somewhere else: deleting it then left every test in this module green, measured. This
# table counts exactly that class, per file, with a row for EVERY watched file even at zero,
# because the comparison below reads a missing row as drift. Same residual as the table above: a
# site-less hit removed and another one added in the SAME file in the same commit cancel out.
_SITELESS_CLAIM_HITS: dict[str, int] = {
    "tests/test_server.py": 8,
    "services/checkers_olog.py": 0,
    "services/checkers.py": 0,
    "tools/archiver.py": 0,
    "server.py": 0,
    "operator_guide.md": 0,
    "display_tools.py": 0,
    "SECURITY.md": 0,
}


@cache
def _watched_blocks() -> tuple[ProseBlock, ...]:
    return iter_blocks(_WATCHED)


def _claim_matches(block: ProseBlock) -> list[tuple[re.Match[str], _Claim]]:
    """Every claim hit in *block*, with the match so the captured number is available."""
    hits: list[tuple[re.Match[str], _Claim]] = []
    for claim in _CLAIMS:
        if not claim.applies_to(block):
            continue
        hits.extend((match, claim) for match in claim.phrase.finditer(block.text))
    return hits


def _is_covered(block: ProseBlock, site: ProseSite) -> bool:
    """Covered means the number sits in a claim's CAPTURE GROUP, not merely in its match.

    The distinction is load-bearing and was found by measurement: "satisfiable by two of the four"
    is one match whose group holds only the four, so a span-wide test counted the *two* as guarded
    while nothing ever compared it. A number inside a claim's span but outside its group is exactly
    as unwatched as one nobody matched at all, and must fall through to ``_FROZEN``.
    """
    return any(match.start(1) <= site.offset < match.end(1) for match, _ in _claim_matches(block))


def _describe(site: ProseSite) -> str:
    return f"{site.block.where()} {site.value} in {site.snippet!r}"


def test_every_claimed_counter_matches_its_set() -> None:
    """A number the prose names must equal the size of the set it is naming.

    This is the whole point: the estate re-typed these by hand on every S29 step and got 8 of 18
    wrong in one build. Going red here means the prose is stale, not that the guard is.

    A measure that REFUSES is collected like a mismatch rather than thrown through the loop. Those
    refusals are this module's own preconditions and [GQ-400] added several; a post-build QA
    measured what that costs, because one refusal at the first block ended the run and the rest of
    the estate went unread. Collected, the report names the refusal AND every drift beside it.
    """
    wrong: list[str] = []
    refused: dict[str, str] = {}
    for block in _watched_blocks():
        for match, claim in _claim_matches(block):
            named = parse_count(match.group(1))
            try:
                expected = claim.measure()
            except AssertionError as refusal:
                refused.setdefault(claim.label, f"{claim.label} refused to answer: {refusal}")
                continue
            if named != expected:
                wrong.append(
                    f"{block.where()} {claim.label}: prose says {named}, the set has {expected}"
                )
    assert not wrong and not refused, (
        "prose counters no longer match their sets, or a measure refused to answer:\n  "
        + "\n  ".join([*wrong, *sorted(refused.values())])
    )


def test_every_claim_still_matches_prose() -> None:
    """Every claim must still find its phrase, a claim that matches nothing verifies nothing.

    Without this the table decays into a sham guard the moment a sentence is reworded: it would
    keep passing while watching a phrase that no longer exists."""
    blocks = _watched_blocks()
    orphaned = [
        claim.label
        for claim in _CLAIMS
        if not any(claim.applies_to(b) and claim.phrase.search(b.text) for b in blocks)
    ]
    assert not orphaned, (
        f"these claims match no prose any more, so they guard nothing: {sorted(orphaned)}. "
        "Reword the claim to follow the prose, or drop it."
    )


def test_inventory_is_partitioned() -> None:
    """Every number the detector finds is either derived or explicitly inventoried.

    A new size-naming sentence lands here, not silently in the tree: either it gets a claim that
    checks it, or an ``_FROZEN`` row that says in words why it cannot be checked."""
    unclaimed: list[str] = []
    for block in _watched_blocks():
        for site in iter_sites([block]):
            if _is_covered(block, site) or site.key in _FROZEN:
                continue
            unclaimed.append(f"{_describe(site)}\n        key: {site.key!r}")
    assert not unclaimed, (
        "these prose numbers are neither derived nor inventoried, add a claim in _CLAIMS if the "
        "number follows from a set, or an _FROZEN row with the reason it cannot:\n  "
        + "\n  ".join(unclaimed)
    )


def test_every_frozen_entry_still_exists() -> None:
    """A row whose phrase is gone is an inventory that has stopped describing the tree."""
    live = {site.key for site in iter_sites(_watched_blocks())}
    stale = sorted(key for key in _FROZEN if key not in live)
    assert not stale, f"_FROZEN rows no longer match any prose: {stale}"


def _siteless_claim_hits() -> Counter[str]:
    """Claim hits per file whose captured number is no detector site, the class a site count is
    blind to. Counted per (block, match, claim) exactly as ``_claim_matches`` yields them."""
    hits: Counter[str] = Counter()
    for block in _watched_blocks():
        sites = iter_sites([block])
        for match, _claim in _claim_matches(block):
            if not any(match.start(1) <= site.offset < match.end(1) for site in sites):
                hits[block.path] += 1
    return hits


def test_inventory_size_is_pinned() -> None:
    """The two hand-kept tables in this module, and both are deliberate.

    Narrow on purpose, because the honest residual is narrow: a deleted DERIVED phrase is normally
    caught by ``test_every_claim_still_matches_prose`` and a deleted INVENTORIED one by
    ``test_every_frozen_entry_still_exists``. What escapes both is ONE occurrence of a phrase whose
    claim or frozen row still matches somewhere else, and several claims do match twice. The site
    count catches that residual only for a phrase the detector SEES; a claim hit whose number is no
    site is invisible to it, which is what the second table is for. On 2026-09-16, 8 of 108 claim
    hits carried no detector site, six claims matched twice, and no claim did both; a deleted
    site-less hit whose claim still matched elsewhere had escaped every test here until that count
    was pinned. What still escapes, for either table: a phrase of the same class removed and
    another one added in the SAME file in the same commit."""
    tables: tuple[tuple[str, dict[str, int], Counter[str]], ...] = (
        (
            "_INVENTORY_SIZES",
            _INVENTORY_SIZES,
            Counter(s.block.path for s in iter_sites(_watched_blocks())),
        ),
        ("_SITELESS_CLAIM_HITS", _SITELESS_CLAIM_HITS, _siteless_claim_hits()),
    )
    drift = [
        f"{label}: {path} {found.get(path, 0)} vs {pinned.get(path)}"
        for label, pinned, found in tables
        for path in sorted({*pinned, *found})
        if found.get(path, 0) != pinned.get(path)
    ]
    assert not drift, (
        "the number of size-naming phrases or of site-less claim hits changed (table: file found "
        "vs pinned): " + ", ".join(drift) + ". A phrase was added or removed, update the pin "
        "together with _CLAIMS/_FROZEN."
    )


def _named_measure_containing_a_lambda() -> int:
    """A measure of the shape that used to blind the reader. Its answer is in the BODY."""
    ordered = sorted([3, 1, 2], key=lambda item: item)
    return len(ordered) + 19


_FRAGMENT_PROBE = _claim("fragment probe", r"(\w+) probes", lambda: 22, scope="nowhere", reads=())


def test_every_claim_declares_which_source_it_reads() -> None:
    """S32's ROOT repair: a claim says which set its measure reads, and the reading is traced.

    Until now the only structural check on a measure searched its derivation for THE ANSWER, which
    is the one property that says nothing about the set. An outside QA measured the consequence:
    at least twelve of the derivations answered correctly while reading a DIFFERENT set from the one
    their sentence names, in both failure directions, stale-green (the wrong set happened to have
    the right size) and false-red (a correct sentence reddened because the measure watched a
    neighbour). Neither direction is visible to a spelling check.

    ``reads`` turns that into something decidable. Provenance IS decidable where "this derives it"
    is not: which symbol did the measure touch, which file did it parse. Both directions of the
    attribute half are enforced, so ``reads`` is a declaration and not a lower bound.

    HOW FAR THIS REACHES, stated here because the field name invites over-reading:

    * It proves the measure touched the object the claim names. It does NOT prove the sentence
      means that object, that stays a human reading, and no construct can replace it.
    * It does not prove the ANSWER depends on what was touched. A measure could read a constant and
      ignore it.
    * The FILE half is one-directional and, for every measure that reaches its file through
      ``_typed_dict_fields``, nearly free: that index parses the whole package, so "this file was
      parsed" does not distinguish it from any other file under ``src``. Since [GQ-400] that covers
      most of the table, see :func:`_provenance_faults`.

    All three are stated here rather than left to be discovered.

    Cost, measured rather than estimated, and re-measured on 2026-09-16 after [GQ-400] gave the
    enrichment claims a measure that parses the package: 25 s for this test, in a module that runs
    in about 36 s. ⚠️ The figure here read "about six seconds" until that day and had rotted, in a
    file that is deliberately unwatched; it carries its date now. That cost is two cold measurement
    passes per claim, one traced, one not, because the recorder is compared against the untraced
    answer, with every memoized helper cleared on both sides of the traced one. The clearing is what
    the seconds buy: against a warm cache a measure touches nothing at all and the test would pass
    while proving nothing. Halving it by leaving the caches warm afterwards is possible and
    deliberately not done, it would let a value computed under the recorder outlive the trace, and a
    cheap test that can poison the rest of the suite is the wrong trade.
    """
    faults = [f"{claim.label}: {fault}" for claim in _CLAIMS for fault in _provenance_faults(claim)]
    assert not faults, (
        "these claims do not read what they declare, correct the declaration if the measure is "
        "right, or the measure if the declaration is:\n  " + "\n  ".join(faults)
    )


def test_no_claim_leaves_its_sources_undeclared() -> None:
    """An empty ``reads`` is the state this repair removes, so it may not come back.

    ``_claim`` makes the field keyword-only and required, which stops it being FORGOTTEN; nothing
    stops it being passed empty, and an empty declaration traces to nothing and proves nothing."""
    undeclared = [claim.label for claim in _CLAIMS if not claim.reads]
    assert not undeclared, (
        f"these claims declare no source at all, so nothing about them is traced: {undeclared}"
    )


def test_a_gate_scope_lead_is_taken_literally() -> None:
    """``_gate_scope_pattern`` escapes the lead, on constructed input rather than on today's rows.

    Both of today's leads are plain words, so this decision is INERT on the real table and would
    survive its own removal unnoticed: exactly the shape of decision that has to be held on
    constructed input, the way ``tests/test_gate_lists.py`` holds its readers. Two properties, both
    load-bearing and each with its own failure:

    * a metacharacter in a lead must not act as one. ``r"the .* gate "`` compiles fine and opens no
      capturing group, so the earlier guard passed it, and the greedy gap then bound the row to the
      LAST phrase in the block instead of the meant one.
    * a group in a lead must not shift ``match.group(1)`` off the number. That was a separate test
      until the escape made it unreachable; a guard that can no longer go red is the thing this
      module exists to remove, so it is folded in here as a property of the BUILDER instead.

    RED-PROOF: drop the ``re.escape`` in ``_gate_scope_pattern`` and BOTH halves report. They are
    collected rather than asserted one by one for exactly that reason: a bare ``assert`` on the
    first would abort before the second was ever tried, and a red-proof that can only ever show one
    of two properties is half a proof.
    """
    both_widths = "the PV gate is three checks and the Olog gate six checks"
    findings: list[str] = []

    greedy = _gate_scope_pattern(_GateScope("f", "s", "m", lead="the .* gate "))
    if re.search(greedy, both_widths):
        findings.append(
            "a lead's metacharacter still acts as a pattern, so a loose lead binds a row to the "
            f"wrong phrase in its own scope: {greedy!r}"
        )

    grouped = _gate_scope_pattern(_GateScope("f", "s", "m", lead="the (PV|Olog) gate is "))
    if re.compile(grouped).groups != 1:
        findings.append(
            "a lead opened a capturing group, so group(1) is no longer the number the comparison "
            f"reads and every claim on that row would report 'prose says None': {grouped!r}"
        )

    plain = _gate_scope_pattern(_GateScope("f", "s", "m", lead="the Olog gate "))
    match = re.search(plain, "the Olog gate six checks", re.IGNORECASE)
    if match is None or match.group(1) != "six":
        findings.append(f"escaping broke an ordinary lead, which must still match: {plain!r}")

    assert not findings, (
        "_gate_scope_pattern no longer takes a lead literally:\n  "
        + "\n  ".join(findings)
        + "\n  A lead is a literal prefix; the builder must re.escape it."
    )


def test_no_gate_size_row_judges_two_different_sizes() -> None:
    """One row, one gate. A row that owns phrases naming DIFFERENT sizes is keyed too coarsely.

    This is the failure ``lead`` exists to prevent, stated as a property so that the NEXT scope to
    grow a second gate's sentence reports a keying problem rather than a prose problem. Without it
    the symptom arrives through ``test_every_claimed_counter_matches_its_set`` as
    ``prose says 6, the set has 3``, which tells the reader to break a true sentence; the family's
    own comment records that trap and this is the answer to it.

    ⛔ IT NEVER FIRES ALONE, and saying so is the point rather than an apology. A row owning two
    different sizes means one of them disagrees with the row's module, so
    ``test_every_claimed_counter_matches_its_set`` goes red in the same run, every time. What this
    test adds is not detection, it is the DIAGNOSIS: the other message names a block and a value
    and reads as an instruction to correct the prose, which in this exact case would mean breaking
    a true sentence. Measured on the mutant below, both reds appear together and only this one
    names the row and the cause. It is also the only place the rule "one row, one gate" exists as
    something that can go red; it lived in a comment before, and this family's comments are the
    ones that rotted.

    ⚠️ WHAT IT IS NOT: a partition check. "No row owns this phrase" is already
    ``test_inventory_is_partitioned``'s job, and a second guard for the same question would be the
    second thing to keep right that this module argues against everywhere else.

    ⚠️ AND IT IS BLIND TO THE ACCIDENTAL AGREEMENT, which is the case the comment at
    ``_GATE_SIZE_SCOPES`` records: two sentences about DIFFERENT sets that name the SAME number,
    the contract's six requirements and the Olog gate's six checks, would be one value here and
    pass. Nothing in this family closes that; only a human reading the sentence does.

    RED-PROOF: drop the ``lead`` from either ``SECURITY.md`` row and this reports that row owning
    the values ``[3, 6]``.
    """
    blocks = _watched_blocks()
    findings: list[str] = []
    for row in _GATE_SIZE_SCOPES:
        phrase = re.compile(_gate_scope_pattern(row), re.IGNORECASE)
        owned = {
            value
            for block in blocks
            if block.path == row.path and block.qualname == row.scope
            for match in phrase.finditer(block.text)
            if (value := parse_count(match.group(1))) is not None
        }
        if len(owned) > 1:
            findings.append(
                f"{row.path} [{row.scope}] -> {row.module}: owns phrases naming {sorted(owned)}"
            )
    assert not findings, (
        "a gate-size row judges phrases that name different sizes, so its key cannot be the right "
        "one:\n  "
        + "\n  ".join(findings)
        + "\n  This scope states more than one gate width. Give each row a `lead` that selects "
        "its own sentence; do NOT change the prose to agree with one module."
    )


def test_no_watched_markdown_file_repeats_a_section_title() -> None:
    """The PRECONDITION the markdown key rests on, asserted rather than assumed.

    A markdown block is addressed by its nearest heading instead of the whole chain, which is a
    readability decision (the shipped guide's longest chain is 169 characters against 70 for its
    deepest heading, and these keys are typed into a table a human has to read) with a soundness
    condition attached: two sections may not share a title, or one section's frozen row could be
    satisfied by the other section's phrase and a real drift would pass unseen.

    Same shape as ``test_the_tracer_can_see_every_source``: the property a mechanism depends on is
    a test, not a comment, because a mechanism that silently stops working is the defect this
    module exists to find. ``tests/test_prose_numbers.py`` carries the positive control, proving
    the check can see a repeated title at all, so an empty answer here is evidence and not a shrug.
    """
    repeated = {
        label: pn.ambiguous_headings(path) for label, path in _WATCHED if path.suffix == ".md"
    }
    offenders = {label: found for label, found in repeated.items() if found}
    assert not offenders, (
        "a watched markdown file repeats a section title, so its blocks no longer have distinct "
        f"keys: {offenders}. Rename one of the sections, or key on the full chain."
    )


def test_the_tracer_can_see_every_source() -> None:
    """The tracer's PRECONDITION, asserted instead of assumed: ``_parsed`` is the only file reader.

    The tracer watches two seams. If a measure ever reads a file some other way, that read becomes
    invisible and every claim resting on it silently stops being traced, a guard that cannot go red
    is the defect it was built to remove. So the property the tracer depends on is a test, and it
    names no count: the reader has to sit inside ``_parsed``, however many there are.

    ⚠️ HOW FAR THIS REACHES, stated because an adversarial pass over [GQ-126] showed it reaches
    less far than the sentence above suggests. The scan is SYNTACTIC and covers THIS FILE. A
    measure that calls into an IMPORTED module can read files the scan never sees: constructed, a
    measure delegating to ``write_gate._discover_gate_modules`` parsed both gate modules, answered
    9, and ``_provenance_faults`` reported nothing at all. Closing that in general needs a third
    recorder over imports and is not built. What IS closed is the one such dependency that exists:
    :func:`test_the_borrowed_gate_counter_opens_nothing` asserts the borrowed counter opens
    nothing, so it stays the pure function of a tree that ``_gate_check_count``'s ``reads``
    declaration rests on."""
    tree = _parsed(Path(__file__))
    spans = pn._scope_spans(tree)
    elsewhere = [
        f"line {node.lineno} in {pn._qualname_at(spans, node.lineno)}: {ast.unparse(node)}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Attribute) and node.func.attr in {"read_text", "read_bytes"})
            or (isinstance(node.func, ast.Name) and node.func.id == "open")
        )
        and pn._qualname_at(spans, node.lineno) != "_parsed"
    ]
    assert not elsewhere, (
        "a file is read outside ``_parsed``, so the provenance tracer cannot see it and every "
        f"claim that declares that file is untraced: {elsewhere}"
    )


def test_the_borrowed_gate_counter_opens_nothing() -> None:
    """The other half of that precondition: the one function this module BORROWS may not read.

    ``_gate_check_count`` declares the gate module as its source and parses it here, through
    ``_parsed``, so the tracer sees it. That declaration is only true while
    ``write_gate._audit_deny_error_codes`` stays a pure function of the tree it is handed. Nothing
    else in this file could notice if it started opening the file itself, because the scan above
    reads THIS module and not that one.

    RED-PROOF: put a ``path.read_text(...)`` into that function and this fails naming the line.
    """
    source = textwrap.dedent(inspect.getsource(write_gate._audit_deny_error_codes))
    reads = [
        f"line {node.lineno}: {ast.unparse(node)}"
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Attribute) and node.func.attr in {"read_text", "read_bytes"})
            or (isinstance(node.func, ast.Name) and node.func.id == "open")
        )
    ]
    assert not reads, (
        "write_gate._audit_deny_error_codes now opens a file, so the source it reads is invisible "
        f"to the tracer and _gate_check_count's reads declaration is no longer true: {reads}"
    )


def test_the_tracer_reports_a_declared_but_unread_source() -> None:
    """The tracer's own red proof, in the file rather than in a commit body.

    A tracer that fails to notice a declared-and-untouched source traces nothing while looking
    green, the exact shape this whole guard exists to find, one layer up. The subject is therefore
    a synthetic claim whose declaration is deliberately false in every direction the checker
    claims to cover: a constant it never reads, a file it never parses, and a constant it reads
    without declaring.
    """
    lying = _claim(
        "tracer self-test",
        r"(\w+) probes",
        lambda: len(ts._ANNOTATION_HINTS),
        reads=("_OLOG_ALWAYS_PRESENT", "services/checkers_olog.py"),
        scope="nowhere",
    )
    faults = _provenance_faults(lying)
    assert any("services/checkers_olog.py" in fault for fault in faults), (
        f"a declared-but-unparsed FILE went unreported, so the file half traces nothing: {faults}"
    )
    assert any(
        "declares ts._OLOG_ALWAYS_PRESENT but never reads it" in fault for fault in faults
    ), f"a declared-but-unread CONSTANT went unreported: {faults}"
    assert any("reads ts._ANNOTATION_HINTS without declaring it" in fault for fault in faults), (
        f"an UNDECLARED read went unreported, so ``reads`` is a lower bound after all: {faults}"
    )

    honest = _claim(
        "tracer self-test (honest)",
        r"(\w+) probes",
        lambda: len(ts._ANNOTATION_HINTS),
        reads=("_ANNOTATION_HINTS",),
        scope="nowhere",
    )
    assert _provenance_faults(honest) == [], (
        "a truthful declaration must produce no fault, or the tracer accuses everything and "
        "therefore discriminates nothing"
    )


def test_the_union_walk_reads_a_type_and_not_a_spelling() -> None:
    """``_union_members`` is asserted directly, because its own contract is what the guard rests on.

    Every spelling below is the SAME TYPE and the same wire schema, so all must yield the same
    members. Two rounds of QA found this predicate answering by spelling: first the order
    (``None | X``), then the alias, the dotted alias, the ``Annotated`` wrapper, the non-``list``
    container and the quoted forward reference, five of the seven false-RED on correct prose.

    The nested-union assertion carries the PARSE-not-split property, and it is asserted on
    ``_union_members`` rather than through the nullability predicate on purpose: routed through the
    predicate it was UNFALSIFIABLE, because a member unparsed from a sub-node of
    ``dict[str, int | None]`` can never satisfy the predicate whatever the walk does. An assertion
    that cannot go red is the defect this module exists to remove, so it was moved to where it can.

    Red proof, executed: replace the final ``yield`` with ``.split("|")``, the strawman the
    ``_union_members`` docstring names, and this test reddens naming ``dict[str,int|None]``, while
    ``test_every_claimed_counter_matches_its_set`` stays GREEN under the same mutation. That green
    is the measurement that this property has no other guard in the module.
    """
    for spelling in (
        "list[str]|None",
        "None|list[str]",
        "Optional[list[str]]",
        "Union[list[str],None]",
        "typing.Optional[list[str]]",
        "Annotated[list[str]|None,Field(description='x')]",
        "'list[str] | None'",
    ):
        assert _union_members(spelling) == frozenset({"list[str]", "None"}), spelling

    # PARSED, not split: a nested union is one member, and "None" must NOT surface at top level.
    assert _union_members("dict[str,int|None]") == frozenset({"dict[str,int|None]"})
    assert _union_members("list[str]") == frozenset({"list[str]"})


def test_an_array_row_is_nullable_by_its_type_not_by_its_container() -> None:
    """An array row admits null or it does not; which array container it uses is not the question.

    The row set is pinned equal to the wire's array-typed properties elsewhere, so asking about the
    container here re-decided a settled question from the spelling, and got it wrong for
    ``Sequence[str] | None`` and ``tuple[str, ...] | None``, both of which render as
    ``anyOf[array, null]`` exactly like ``list[str] | None``.

    Red proof, executed: restore the ``list[``-prefix requirement and the THIRD assertion fails:
    named precisely, because pytest stops at the first failing assert, so "the third and fourth
    fail" would be a claim about a run nobody has seen. The fourth reddens the same way once the
    third is removed.
    """
    assert _row_admits_null("list[str]|None")
    assert _row_admits_null("None|list[str]")
    assert _row_admits_null("Sequence[str]|None")
    assert _row_admits_null("tuple[str,...]|None")
    assert not _row_admits_null("list[str]")
    assert not _row_admits_null("dict[str,int|None]")


def test_the_conformance_tests_partition_into_two_kinds() -> None:
    """The guarantee that the old subtraction gave away for free, restored as an assertion.

    "TEN of the twelve drive ``FastMCP.call_tool``. The other TWO drive a real client" is only
    meaningful while the two kinds are disjoint and together are all of them. Measuring each side
    independently, which is what stops them being proxies, drops that guarantee: a thirteenth
    conformance test driving NEITHER, or one converted to drive BOTH, would leave both figures
    describing a set that is no longer a partition, and every claim above would stay green.

    The tools half is the same shape: the union of what the two kinds drive must be exactly
    ``_TYPED_OUTPUT_TOOLS``, or "the 20 tools THOSE TEN cover" is a fragment of a bigger set and
    the sentence quietly stops being about the estate.
    """
    conformance = _named_tests("_conforms_to_its_schema")
    in_process = {node.name for node in conformance if _drives_in_process(node)}
    real_client = {node.name for node in conformance if _drives_real_client(node)}

    assert not in_process & real_client, (
        f"these conformance tests drive BOTH paths, so the two figures double-count them: "
        f"{sorted(in_process & real_client)}"
    )
    neither = {node.name for node in conformance} - in_process - real_client
    assert not neither, (
        f"these conformance tests drive neither ``mcp.call_tool`` nor a real ``Client``, so they "
        f"are in the twelve and in neither figure: {sorted(neither)}"
    )

    named_by_wire = {
        inner.value
        for node in conformance
        if _drives_real_client(node)
        for inner in ast.walk(node)
        if isinstance(inner, ast.Constant) and inner.value in ts._TYPED_OUTPUT_TOOLS
    }
    assert _tools_named_by_in_process_tests() | named_by_wire == set(ts._TYPED_OUTPUT_TOOLS), (
        "the conformance tests together must name every typed tool; otherwise 'the tools THOSE "
        "TEN cover' is measured against an incomplete reading of the test bodies"
    )


def test_the_derivation_reader_reads_the_body_it_claims_to() -> None:
    """The reader's three promises, each driven rather than described.

    (1) A named measure yields its own body, docstring excluded, EVEN when it contains a lambda.
    The old order walked for a lambda first and returned that lambda's body, discarding the very
    statements a typed-in answer would sit in. (2) A fragment, the shape ``inspect.getsource``
    hands back for a lambda on a wrapped argument line carrying a ``scope=`` keyword, is still
    read; that repair branch had never been executed by anything. (3) An unreadable measure is an
    error rather than an empty string, so the check cannot silently inspect nothing.
    """
    body = _derivation_source(_named_measure_containing_a_lambda)
    assert "19" in body, "the named measure's own body must be inspected"
    assert body.strip() != "item", "not merely the lambda it happens to contain"
    assert "A measure of the shape" not in body, "and not its docstring"

    assert _derivation_source(_FRAGMENT_PROBE.measure) == "22", (
        "a lambda whose enclosing statement is an unparseable fragment must still be read"
    )

    with pytest.raises(AssertionError, match="cannot be read"):
        _derivation_source(functools.partial(len, [1] * 16))


def test_no_claim_hard_codes_its_expectation() -> None:
    """No claim may spell out the answer it is supposed to derive, in EITHER half.

    Checking only the pattern is not enough, and that was settled by execution rather than by
    argument: replacing one claim's derivation with a typed-in answer left every test in this
    module green. A typed-in integer in the MEASURE is the same defect as one in the PATTERN, so
    the derivation's source is inspected too, in digits AND in words, this prose spells its
    numbers as words far more often than as digits, so a digits-only check guards the rarer half.

    HOW FAR THIS ACTUALLY REACHES, because the promise above is easy to over-read. It is a
    check on the SPELLING of the answer, one frame deep, and an outside QA measured all three of
    its blind spots: ``lambda: 20 + 2`` passes for an expected 22 (the answer is never spelled),
    ``lambda: _helper()`` passes whatever ``_helper`` returns (only the immediate frame is
    inspected, and MOST of the delivered derivations call a module helper), and a regex may split
    the literal (``(1[1])`` captures an 11 the skeleton search cannot see). What it does catch is
    the defect it was built for: a bare typed-in answer, in either half. Closing the rest needs a
    different mechanism, not a wider regex, a claim declaring what it READS, which is separate
    work recorded as such.

    A FOURTH limit joined with [GQ-126], and it is the first one this check imposes on its subject
    rather than suffers: it reads the pattern as TEXT, so it cannot tell "spells the answer" from
    "spells every number there is". Both look like the string "six". The gate-width claims capture
    with ``prose_numbers._NUMBER``, the detector's own grammar, for reasons measured at
    ``_GATE_CHECKS``, and that grammar carries the whole number vocabulary. It is therefore removed
    from the pattern before the search, once, by exact string, with the argument for that written
    at the line that does it.

    ⚠️ No SHARE is printed here, deliberately, and the previous wording is why: it said 41 and had
    drifted to 51 unnoticed, because nothing in this repository re-runs a figure written in prose.
    Re-derive it by walking ``_CLAIMS``, parsing each ``measure``'s source and asking whether it
    calls a module-level function of this module; the arithmetic blind spot is the same walk asking
    for an ``ast.BinOp``.

    Doubles as the precondition for the rest of the module: a pattern that captures nothing would
    raise ``IndexError`` deep in the coverage scan, naming neither ``_CLAIMS`` nor the row.
    """
    groupless = [claim.label for claim in _CLAIMS if claim.phrase.groups < 1]
    assert not groupless, (
        f"a claim pattern must CAPTURE the number it checks; these capture nothing: {groupless}"
    )

    offenders: list[str] = []
    for claim in _CLAIMS:
        try:
            expected = claim.measure()
        except AssertionError:
            # A refusing measure has no answer to spell, and this check needs the answer. The
            # refusal itself is reported where refusals belong, by
            # test_every_claimed_counter_matches_its_set; letting it end this loop would leave
            # every claim after it unchecked for a typed-in answer, without a word.
            continue
        forms = {str(expected)} | {
            word for word, value in pn._WORD_VALUES.items() if value == expected
        }
        # The BORROWED number grammar is removed before the search, and that is not an escape
        # hatch: a claim capturing with ``prose_numbers._NUMBER`` spells EVERY number the detector
        # can read, so it matches a wrong figure exactly as willingly as the right one, which is
        # the property this check is really asking about. Only that one shared string is removed,
        # so a pattern that borrows the grammar AND separately names its own answer is still
        # caught. Added with [GQ-126], whose gate-width claims are the first to borrow it, and
        # they borrow it because the alternatives were measured worse: see ``_GATE_CHECKS``.
        skeleton = re.sub(
            r"\\[dwsSWbAZ]|\{\d+(?:,\d+)?\}", "", claim.phrase.pattern.replace(pn._NUMBER, "")
        )
        derivation = _derivation_source(claim.measure)
        for form in forms:
            if re.search(rf"(?<!\w){re.escape(form)}(?!\w)", skeleton, re.IGNORECASE):
                offenders.append(f"{claim.label}: the pattern spells {form!r}")
            if re.search(rf"(?<!\w){re.escape(form)}(?!\w)", derivation, re.IGNORECASE):
                offenders.append(f"{claim.label}: the derivation spells {form!r}")
    assert not offenders, (
        "these claims state the answer instead of deriving it, so they would pass by "
        f"construction: {sorted(offenders)}"
    )


def test_the_registration_reader_counts_every_form_and_refuses_the_rest() -> None:
    """:func:`_tool_registrations` on constructed input, because the real tree holds two forms only.

    Every counted form below registers a tool in FastMCP 3.4.4 (probed with a scratch server for
    the direct call, in both spellings of its slot), so each must be counted and named. Every
    refused shape reaches a registrar in a way the reader cannot follow, so a tool registered
    through it would be on the wire and in no count. Neither the direct call nor any refused shape
    occurs in ``server.py`` or ``display_tools.py`` (measured 2026-09-16), which is exactly why they
    are held here and not on the tree: a decision that is inert on the real table survives its own
    removal unnoticed.

    RED-PROOF: make :func:`_direct_registration_target` answer ``None`` and the counted module is
    refused at its direct calls; switch the refusal off and every refused shape is reported.
    """
    counted = textwrap.dedent(
        """
        @mcp.tool
        async def bare() -> None: ...

        @mcp.tool(annotations=READONLY)
        async def called() -> None: ...

        def register(mcp: object) -> None:
            mcp.tool(annotations=READONLY)(later)
            mcp.tool("named")(renamed)
            mcp.tool(direct, name="direct")
            mcp.tool(name_or_fn=keyword)
        """
    )
    registrations = _tool_registrations(ast.parse(counted), "probe.py")
    named = sorted(str(item.function) for item in registrations)
    assert named == ["bare", "called", "direct", "keyword", "later", "renamed"], registrations

    findings: list[str] = []
    for shape, source in (
        ("an aliased registrar", "registrar = mcp\nregistrar.tool(annotations=READONLY)(later)\n"),
        ("a registrar kept in a variable", "register = mcp.tool\nregister(later)\n"),
        ("a factory applied elsewhere", "decorate = mcp.tool(annotations=RO)\ndecorate(later)\n"),
        ("a factory that names a tool and is never applied", 'mcp.tool("named")\n'),
        ("a ready Tool object", "mcp.add_tool(prepared)\n"),
        ("a tool taken off the wire again", 'mcp.remove_tool("later")\n'),
    ):
        try:
            _tool_registrations(ast.parse(source), "probe.py")
        except AssertionError as refusal:
            if "probe.py:" not in str(refusal):
                findings.append(f"{shape}: refused without naming where: {refusal}")
            continue
        findings.append(f"{shape}: not refused, so a tool registered that way is counted by nobody")
    assert not findings, "_tool_registrations misreads a registrar:\n  " + "\n  ".join(findings)


def test_no_other_module_reaches_the_registrar() -> None:
    """A tool registered anywhere but ``server.py`` and ``display_tools.py`` is in no lane count.

    Every lane claim reads exactly those two modules, so a registration in a third one would put a
    tool on the wire that the core, the display and the full count all leave out, with every claim
    green. This asks the whole shipped package for a registrar attribute outside those two. Syntax
    only, like :func:`_tool_registrations`, so ``getattr(mcp, "tool")`` stays out of reach.

    ⚠️ It reads the ATTRIBUTE NAME, never the object it hangs off, so an ordinary ``record.tool``
    somewhere under ``src`` reports as a registrar reach. That is why the set it asks for is the
    narrow ``_REGISTRAR_ATTRIBUTES_ELSEWHERE`` and not the wide one: ``mount`` is a ``requests``
    session method in this package (measured 2026-09-16). The repair for a false report is to
    rename the attribute or to narrow that set with the reason, never to drop this test.

    RED-PROOF: a ``registrar.tool(name=...)(fn)`` line in any other module of the package, even in
    code that never runs, is reported with its file and line.
    """
    registrars = {_SRC / "server.py", _SRC / "display_tools.py"}
    elsewhere = [
        f"{path.relative_to(_SRC).as_posix()}:{node.lineno}: {ast.unparse(node)}"
        for path in _package_modules()
        if path not in registrars
        for node in ast.walk(_parsed(path))
        if isinstance(node, ast.Attribute) and node.attr in _REGISTRAR_ATTRIBUTES_ELSEWHERE
    ]
    assert not elsewhere, (
        "a tool registrar is reached outside the two registrar modules the lane counts read, so a "
        f"tool registered there is counted by nobody: {elsewhere}"
    )


def test_every_mapped_field_row_names_the_test_that_reads_its_map() -> None:
    """``_MAPPED_FIELD_CLAIMS`` is hand-kept, and a swapped pair of rows was invisible.

    The table binds a test scope to a per-tool map by hand, which is the construction this module
    rejects everywhere else. An outside QA measured what that costs on the tree of 2026-07-26:
    swapping two rows whose maps have the same size left its full lane green, and the next ordinary
    change to one of those maps then accused the OTHER tool's correct sentence, with an instruction
    to falsify it. Its verdict was that the delivered tree carries no mis-wired row and that most
    swaps are loud; the silent pair is what this holds.

    DECIDED WITH [GQ-400]: the table stays authored, because the row IS the authored statement, and
    the binding is held against the tests themselves. That is the same reading :func:`_none_valued`
    needs to find the tool a map is measured against, so it is one mechanism, not a second one to
    keep right. The ANSWER is used and not only the absence of a refusal, which a post-build QA
    asked for: two rows may not be held against the same tool.

    RED-PROOF: swap the maps of two rows whose maps have the same size, discover_pvs and
    find_channels say, and this reports both rows; this module was green under that swap.
    """
    faults: list[str] = []
    held: dict[str, str] = {}
    for _test, constant in _MAPPED_FIELD_CLAIMS:
        try:
            tool = _tool_pinned_by(constant)
        except AssertionError as fault:
            faults.append(str(fault))
            continue
        if tool in held:
            faults.append(
                f"{constant} and {held[tool]} are both held against {tool!r}, so one of them reads "
                "a test that pins somebody else's map"
            )
        held[tool] = constant
    assert not faults, (
        "these rows of _MAPPED_FIELD_CLAIMS do not name the test that reads their map:\n  "
        + "\n  ".join(faults)
    )


def test_the_element_schema_claims_bind_only_their_own_sentence() -> None:
    """The two element-schema row claims bind the sentence they were written for, and nothing else.

    Both patterns were plain phrases with no scope, no path and no mention of the schema they count,
    so they judged every block of every watched file. Measured on 2026-09-16 in a throwaway copy: a
    TRUE sentence in the estate's own voice above ``_OUTPUT_ARRAY_ITEMS``, "the other 5 rows are
    ``anyOf[array, null]``", was accused as "opaque-item rows: prose says 5, the set has 9", which
    reads as an instruction to break it.

    Each claim matches exactly one block today, so the narrowing is INERT on the real tree and would
    survive its own removal unnoticed there. That is the shape of decision this module holds on
    constructed input, the way ``test_a_gate_scope_lead_is_taken_literally`` holds its own.

    RED-PROOF: drop ``scope`` from either claim and the neighbour finding reports it; drop the
    element schema from either pattern and the other-set finding does.
    """
    claims = {claim.label: claim for claim in _CLAIMS}
    findings: list[str] = []
    for label, sentence, other_set in (
        (
            "string-item rows",
            'That matters most for the 7 rows carrying ``{"type": "string"}``: over the wire',
            "That matters most for the 5 rows carrying ``anyOf[array, null]``",
        ),
        (
            "opaque-item rows",
            'Stated honestly, the other 9 rows are ``{"type": "object", "additionalProperties": '
            "true}``, a deliberately opaque element",
            "Eleven rows carry a plain element schema; the other 5 rows are "
            "``anyOf[array, null]``.",
        ),
    ):
        claim = claims[label]
        home = ProseBlock("tests/test_server.py", 1, _ELEMENT_SCHEMA_TEST, sentence)
        neighbour = ProseBlock("tests/test_server.py", 1, "<module>", sentence)
        stranger = ProseBlock("server.py", 1, _ELEMENT_SCHEMA_TEST, sentence)
        if not (claim.applies_to(home) and claim.phrase.search(home.text)):
            findings.append(f"{label}: no longer binds the sentence it was written for")
        if claim.applies_to(neighbour):
            findings.append(f"{label}: binds the same sentence outside its scope")
        if claim.applies_to(stranger):
            findings.append(f"{label}: binds the same sentence in another file")
        if claim.phrase.search(other_set):
            findings.append(f"{label}: binds a sentence about a different set")
    assert not findings, (
        "an element-schema claim no longer binds its own sentence, and only that one:\n  "
        + "\n  ".join(findings)
    )


def test_the_typed_dict_index_refuses_what_it_cannot_key() -> None:
    """The index stops on an inherited field and on two TypedDicts of one name, constructed here.

    It reads class bodies, resolves no bases and keys on the bare class name. Both are right while
    nothing inherits and no name repeats, and silently wrong the moment one of them changes.
    Measured on 2026-09-16 in a throwaway copy: a second base beside ``TypedDict`` left the count of
    ``ChannelInfo``'s fields where it was while the type had gained one, with the whole module
    green; a single inherited base dropped the class out of the index and reached the reader as
    "ChannelInfo is gone"; and a second ``ChannelInfo`` in a file that sorts later made the count
    answer one and accuse the correct sentence of saying six. None of the three exists in the
    package, so all three are held here rather than there.

    RED-PROOF: switch either refusal off and the shapes below are reported as unrefused.
    """
    base = "class _Provenance(TypedDict):\n    source_url: str\n\n\n"
    findings: list[str] = []
    for shape, trees, named in (
        (
            "a second base beside TypedDict",
            [("probe.py", base + "class ChannelInfo(_Provenance, TypedDict):\n    name: str\n")],
            "ChannelInfo",
        ),
        (
            "an inherited TypedDict as the only base",
            [("probe.py", base + "class ChannelInfo(_Provenance):\n    name: str\n")],
            "ChannelInfo",
        ),
        (
            "the same name in two files",
            [
                ("early.py", "class Twin(TypedDict):\n    a: str\n"),
                ("later.py", "class Twin(TypedDict):\n    b: str\n"),
            ],
            "Twin",
        ),
    ):
        try:
            _typed_dict_index([(label, ast.parse(source)) for label, source in trees])
        except AssertionError as refusal:
            if named not in str(refusal):
                findings.append(f"{shape}: refused without naming {named}: {refusal}")
            continue
        findings.append(f"{shape}: went unrefused, so a count would read a set nobody named")
    plain = _typed_dict_index([("probe.py", ast.parse(base))])
    if plain != {"_Provenance": {"source_url": "str"}}:
        findings.append(f"a TypedDict that inherits nothing is no longer read as written: {plain}")
    assert not findings, (
        "the TypedDict index no longer refuses what it cannot key:\n  " + "\n  ".join(findings)
    )


async def test_the_union_reader_agrees_with_the_wire_on_every_array_row() -> None:
    """[GQ-405]: what the union reader decides about nullability, held against what the wire says.

    ``_union_members`` recognizes a closed set of spellings, ``_UNION_ALIASES`` among them, and
    anything else comes back as one unparsed member, which reads as NOT nullable. One direction of
    that is loud already: a nullable field the reader misreads makes a correct count go red. The
    other is silent, and it is the one measured on 2026-09-16 in a throwaway copy: ``levels`` of
    ``list_log_levels`` re-annotated through a module-level type alias, and again through a
    ``NotRequired`` qualifier, rendered as ``anyOf[array, null]`` on the wire while the reader
    answered not-nullable, the nullable-array count stayed where it was, and this module together
    with ``tests/test_server.py`` stayed green.

    So every row that count reads is read twice, once through its annotation and once through the
    schema it advertises, and a disagreement names the row, the annotation and the schema. The
    repair it asks for is teaching the walk the new spelling, never editing a row.

    A row that does not exist YET is reached through a chain rather than by this test:
    ``_OUTPUT_ARRAY_ITEMS`` is pinned equal to the array-typed properties the wire advertises, by
    ``test_typed_output_schema_arrays_declare_their_element_schema``, so a new nullable array field
    forces a row there first and is compared here second. A post-build QA asked for that chain to be
    named where the reader of this test stands, because it is what makes the work item's "or a new
    array row" true.

    NOT a measurement in the sense of the module header: nothing here takes a LENGTH from
    ``mcp.list_tools()``, only each row's own property, so the lane trap that header describes does
    not apply here. Measured 2026-09-16: no row belongs to a display tool, so the core-only lane
    reads the same rows; and a row whose tool the running lane does not carry is COLLECTED like a
    disagreement rather than thrown, so the day that measurement stops holding, the report names the
    row and the lane instead of a bare lookup error. ⚠️ Its reach is the array rows and stops there:
    an unrecognized spelling on any other field is compared to nothing.
    """
    advertised = await wire_tools_by_name()
    disagreements: list[str] = []
    for tool, field in ts._OUTPUT_ARRAY_ITEMS:
        try:
            annotation = _array_row_annotation(tool, field)
        except AssertionError as refusal:
            disagreements.append(f"{tool}.{field}: {refusal}")
            continue
        listed = advertised.get(tool)
        if listed is None:
            disagreements.append(
                f"{tool}.{field}: this lane carries no such tool, so the row cannot be held "
                "against the schema it advertises"
            )
            continue
        schema = ((listed.outputSchema or {}).get("properties") or {}).get(field)
        if not isinstance(schema, dict):
            disagreements.append(f"{tool}.{field}: the wire advertises no property for this row")
            continue
        if _row_admits_null(annotation) != ts._schema_permits_null(schema):
            reading = "nullable" if _row_admits_null(annotation) else "not nullable"
            disagreements.append(
                f"{tool}.{field}: the union reader reads {annotation!r} as {reading}, "
                f"the wire advertises {schema}"
            )
    assert not disagreements, (
        "the union reader and the advertised schema disagree about a declared array row, so the "
        "nullable-array count is reading a spelling rather than a type; teach the walk the "
        "spelling, do not edit the row:\n  " + "\n  ".join(disagreements)
    )


def test_every_return_path_claim_reads_the_table_of_its_own_tool() -> None:
    """A return-path claim must count the table of the tool its sentence is about.

    The tool is named twice per claim and was compared nowhere: once by the sentence, through the
    claim's scope or its pattern, and once by the conformance test handed to the measure. Measured
    on 2026-09-16 in a throwaway copy: giving the discover_pvs claim the find_channels table left
    the whole module green, because :func:`_return_path_rows` refuses a disagreement between the two
    tables, so the two are equal by construction and a swap can never surface as a number.

    The rule, one line per shape: a claim scoped to a conformance test or to the tool's own
    docstring reads that tool's table; a module-level claim names its tool in the pattern. A claim
    whose derivation reads BOTH tables is about the pair rather than about one tool, and is skipped
    here; ``_return_path_rows`` is the one such measure and it raises when they disagree.

    RED-PROOF: swap either conformance constant into the other claim and this reports the pair.
    """
    findings: list[str] = []
    for claim in _CLAIMS:
        derivation = _derivation_source(claim.measure)
        read = sorted(
            tool for tool, (name, _test) in _CONFORMANCE_BY_TOOL.items() if name in derivation
        )
        if len(read) != 1:
            continue
        tool = read[0]
        about = claim.scope if claim.scope and claim.scope != "<module>" else ""
        if about and about not in {tool, _CONFORMANCE_BY_TOOL[tool][1]}:
            findings.append(f"{claim.label}: scoped to {about!r} but reads the {tool} table")
        elif not about and tool not in claim.phrase.pattern:
            findings.append(
                f"{claim.label}: reads the {tool} table while neither its scope nor its pattern "
                "names that tool"
            )
    assert not findings, (
        "a return-path claim counts a table that belongs to another tool, and the two tables are "
        "pinned equal, so no number can show it:\n  " + "\n  ".join(findings)
    )
