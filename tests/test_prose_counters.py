"""S32: the numbers this repository's prose NAMES are compared to the sets they describe.

Comments and docstrings here state sizes: "all 22 schemas", "7 (resp. 9) rows", "TEN of the
twelve", and until now nothing checked a single one of them. The cost was measured, not feared:
the S29 11th typing target pulled 18 counters over by hand, two adversarial QA rounds found 8 of
them still wrong, and one had already been wrong before that build began. The reading half of this
guard is ``tests/prose_numbers.py``; this module is the comparing half.

WHAT IS PROMISED, in three parts, because a vaguer promise would be the same defect one layer up:

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
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from tests import prose_numbers as pn
from tests import test_server as ts
from tests.prose_numbers import ProseBlock, ProseSite, iter_blocks, iter_sites, parse_count

_TESTS = Path(__file__).resolve().parent
_SRC = _TESTS.parent / "src" / "epics_mcp"

# The files whose prose is watched. `checkers_olog.py` is named by the roadmap entry; the next
# three carry sentences that DUPLICATE a number watched in test_server.py, and a duplicate nobody
# compares is how the canonical side drifts while the test side is dutifully corrected.
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
    """

    label: str
    phrase: re.Pattern[str]
    measure: Callable[[], int]
    reads: tuple[str, ...]
    scope: str = ""

    def applies_to(self, block: ProseBlock) -> bool:
        return not self.scope or block.qualname == self.scope


def _claim(
    label: str,
    pattern: str,
    measure: Callable[[], int],
    *,
    reads: tuple[str, ...],
    scope: str = "",
) -> _Claim:
    """*reads* is keyword-only and REQUIRED, so a new claim cannot be added without declaring it."""
    return _Claim(label, re.compile(pattern, re.IGNORECASE), measure, reads, scope)


def _derivation_source(measure: Callable[[], int]) -> str:
    """A claim's derivation as source text, so a typed-in answer THERE is visible too.

    Parsed, never split on text. A NAMED function yields its body WITHOUT the docstring: those
    docstrings explain which set they count, and accusing them would punish the explanation the
    rest of this module asks for. A LAMBDA yields its body, its enclosing statement carries the
    claim's own pattern, which legitimately contains digits. Only executable code is searched.

    THE ORDER OF THOSE TWO IS LOAD-BEARING and it used to be the other way round. The lambda walk
    ran first over the WHOLE parsed statement, so a named measure containing a lambda anywhere:
    a ``key=lambda ...`` in a sort, say, had its entire body discarded and only that lambda's body
    inspected. Latent on the delivered table (measured: 0 of 79 named measures contain one) and a
    trap the moment one does, because the discarded body is exactly where a typed-in answer would
    sit. A named function is now matched before anything is walked.

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
    * FILES are checked in one direction only. Two measures reach their file through
      ``_typed_dict_fields``, which parses the whole package, so "this file was parsed" cannot be
      told apart from "some file in ``src`` was parsed". Tightening that needs a third recorder over
      the index's keys; until then it is stated here rather than implied away.

    FIRST it checks that the recorder did not change the measurement, and that check is here
    because it was needed: the first recorder written for this used ``__getattr__``, ``vars(ts)``
    never reached it, and the twelve ``*_ALWAYS_PRESENT`` constants traced as 0. A trace of a
    different computation than the guard checks is not evidence about the guard, and it fails
    towards accusing innocent claims, which is the worse direction.
    """
    untraced = claim.measure()
    traced, paths, attributes = _trace_measure(claim.measure)
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


def _none_valued(mapping: Mapping[str, str | None]) -> int:
    """Fields the map declares with NO advertised base type.

    ``None`` means specifically ``object | None``. An ``X | None`` with a concrete ``X`` still
    carries its non-null type (``archived: bool | None`` maps to ``"boolean"``), so the estate's
    "enrichment fields" are not simply its nullable fields. Every claim below that counts an
    enrichment, topology or type-info subset rests on that distinction.
    """
    return sum(1 for value in mapping.values() if value is None)


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


def _registered_tools(path: Path) -> int:
    """Tools a module REGISTERS, in either house form.

    Counting ``async def`` instead would miss the difference that matters: dropping one
    ``mcp.tool(...)(fn)`` line takes a tool off the wire while leaving the coroutine defined, so
    every count stays green and a tool disappears silently. Both forms are counted.
    """
    found = 0
    for node in ast.walk(_parsed(path)):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            found += sum(1 for decorator in node.decorator_list if _is_mcp_tool(decorator))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Call):
            # ``mcp.tool(...)(fn)``: the call-position form. Requiring the OUTER call's func to
            # be a call is what keeps a decorator, which ``ast.walk`` also visits on its own, from
            # being counted a second time.
            found += _is_mcp_tool(node.func)
    return found


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


@cache
def _typed_dict_fields() -> dict[str, dict[str, str]]:
    """Every ``TypedDict`` in the package by name, with its fields' normalized annotations.

    BOTH spellings, and the functional one is not hypothetical: ``ArchiverHistoryResult`` in
    ``tools/archiver.py`` HAS to use it, because ``from`` is a Python keyword and cannot be a
    class-syntax field name. A class-syntax-only index did not see that tool's result shape at
    all, latent while none of its fields is nullable, and silent the moment one becomes so.

    Only class-body annotations count, never any annotated assignment: a function LOCAL would
    otherwise stand in for a field (``warnings: list[str] = []`` already exists inside a function
    body in this package).

    Base resolution is deliberately ABSENT rather than carried untested, measured, no TypedDict
    in this package inherits from another one, so the code that walked bases would never run.
    Inherited fields therefore stay outside every count that reads this index; that is a scope
    statement, not an oversight, and it goes red the day it matters only if someone adds the walk
    together with a test for it.
    """
    index: dict[str, dict[str, str]] = {}
    for path in sorted(_SRC.rglob("*.py")):
        for node in ast.walk(_parsed(path)):
            if isinstance(node, ast.ClassDef) and any(
                (isinstance(base, ast.Name) and base.id == "TypedDict")
                or (isinstance(base, ast.Attribute) and base.attr == "TypedDict")
                for base in node.bases
            ):
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
                        f"{path.name}: {target.id} uses the functional TypedDict form without a "
                        "literal field mapping, so its fields cannot be read from the syntax tree"
                    )
                index[target.id] = {
                    key.value: ast.unparse(annotation).replace(" ", "")
                    for key, annotation in zip(fields.keys, fields.values, strict=True)
                    if isinstance(key, ast.Constant) and isinstance(key.value, str)
                }
    return index


@cache
def _registered_tool_functions() -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """Registered tool name to the function that implements it, from the two registrar modules.

    Both registration forms, for the same reason :func:`_registered_tools` counts both: server.py
    decorates, display_tools.py calls ``mcp.tool(...)(fn)`` after the fact, and a decorator-only
    reader would know nothing about the display tools at all.

    One discovery, several readers, the return annotation and the parameter names are two
    questions about the same set, and asking each of them its own way is how the two answers start
    disagreeing.
    """
    found: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for path in (_SRC / "server.py", _SRC / "display_tools.py"):
        tree = _parsed(path)
        defined = {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                if any(_is_mcp_tool(decorator) for decorator in node.decorator_list):
                    found[node.name] = node
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Call)
                and _is_mcp_tool(node.func)
            ):
                found.update(
                    {
                        argument.id: defined[argument.id]
                        for argument in node.args
                        if isinstance(argument, ast.Name) and argument.id in defined
                    }
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
    annotation answers as a one-element set.
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


@cache
def _nullable_array_rows() -> int:
    """Array rows whose field is an ``X | None``, the ``anyOf[array, null]`` subset.

    Bound to the (tool, field) PAIR the row is keyed on, resolved through the TOOL'S OWN result
    type. Matching by field NAME across the package read a set the sentence does not name, and the
    collisions are real rather than imagined: ``pvs``, ``tags``, ``properties`` and ``samples``
    each carry two different annotations here, so one internal TypedDict declaring any of them
    nullable would have moved this count without a single array row changing.

    Loud rather than lenient when a row stops resolving: a silent skip would let the count fall
    while the prose stayed green, which is the whole failure mode this module exists for.
    """
    index = _typed_dict_fields()
    results = _tool_result_type()
    found = 0
    for tool, field in ts._OUTPUT_ARRAY_ITEMS:
        fields = index.get(results.get(tool, ""))
        if fields is None or field not in fields:
            raise AssertionError(
                f"the array row ({tool!r}, {field!r}) no longer resolves to a TypedDict field "
                f"(the tool's return annotation reads {results.get(tool)!r}), the row, the "
                "return annotation or the result shape moved"
            )
        found += _row_admits_null(fields[field])
    return found


@cache
def _channel_info_fields() -> int:
    """Fields of ``ChannelInfo``, a NAMESAKE of the find_channels key count, not the same set."""
    fields = _typed_dict_fields().get("ChannelInfo")
    if fields is None:
        raise AssertionError("ChannelInfo is gone from the package's TypedDicts")
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


# --- the claims -------------------------------------------------------------------------------

_TYPED = "_TYPED_OUTPUT_TOOLS"
_MAPPED = r"EXACTLY the (\w+) mapped fields"

# The per-tool "EXACTLY the N mapped fields" line: one row per typed tool, bound by the test it
# lives in. Every typing step adds one, which is why the family is derived rather than inventoried.
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
    _claim(
        "explicit rows",
        r"the (\w+) explicit rows point straight at",
        lambda: len(ts._ALWAYS_PRESENT_BY_TOOL) - len(ts._OLOG_ALWAYS_PRESENT),
        reads=("_ALWAYS_PRESENT_BY_TOOL", "_OLOG_ALWAYS_PRESENT"),
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
    _claim(
        "string-item rows",
        r"most for the (\w+) rows carrying",
        lambda: _rows_advertising(ts._STRING_ITEMS),
        reads=("_OUTPUT_ARRAY_ITEMS", "_STRING_ITEMS"),
    ),
    _claim(
        "opaque-item rows",
        r"the other (\w+) rows are",
        lambda: _rows_advertising(ts._OPAQUE_OBJECT_ITEMS),
        reads=("_OUTPUT_ARRAY_ITEMS", "_OPAQUE_OBJECT_ITEMS"),
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
    _claim(
        "discover_pvs return paths",
        r"discover_pvs emits on EVERY one of its (\w+) return paths",
        lambda: _paths_rows(_DISCOVER_CONFORMANCE),
        scope="<module>",
        reads=("tests/test_server.py",),
    ),
    # Anchored on the TOOL NAME. One pattern matched BOTH module-level sentences, so the
    # find_channels claim was verified against discover_pvs' table. Equal today, which means the
    # day either tool gains a path, the guard would demand the CORRECT sentence be changed back.
    _claim(
        "find_channels return paths",
        r"find_channels emits on EVERY one of its (\w+) return paths",
        lambda: _paths_rows(_FIND_CHANNELS_CONFORMANCE),
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
        lambda: _paths_rows(_FIND_CHANNELS_CONFORMANCE),
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
    _claim(
        "archive-status enrichment fields",
        r"The (\w+) DS-4A/AR-D enrichment fields",
        lambda: _none_valued(ts._ARCHIVE_STATUS_BASE_TYPE),
        reads=("_ARCHIVE_STATUS_BASE_TYPE",),
    ),
    # The detector cannot see this one: four words sit between the number and its noun and the gap
    # allows two. A claim is matched against the block text directly, so it guards the sentence
    # anyway, and it must, because its twin thirty lines away IS derived. Which of two sentences
    # about the same set is watched may not depend on how the author spelled the type.
    _claim(
        "archive-status enrichment fields (exposes note)",
        r"The (\w+) ``object \| None`` enrichment fields",
        lambda: _none_valued(ts._ARCHIVE_STATUS_BASE_TYPE),
        scope="test_is_archived_exposes_typed_output_schema",
        reads=("_ARCHIVE_STATUS_BASE_TYPE",),
    ),
    _claim(
        "archive-status enrichment fields (runtime note)",
        r"status \+ the (\w+) enrichment fields",
        lambda: _none_valued(ts._ARCHIVE_STATUS_BASE_TYPE),
        reads=("_ARCHIVE_STATUS_BASE_TYPE",),
    ),
    _claim(
        "appliance topology fields",
        r"the (\w+) object\|None topology fields",
        lambda: _none_valued(ts._GET_APPLIANCE_INFO_BASE_TYPE),
        reads=("_GET_APPLIANCE_INFO_BASE_TYPE",),
    ),
    _claim(
        "archive-info projection fields",
        r"The (\w+) getPVTypeInfo projection fields",
        lambda: _none_valued(ts._GET_ARCHIVE_INFO_BASE_TYPE),
        reads=("_GET_ARCHIVE_INFO_BASE_TYPE",),
    ),
    _claim(
        "archive-info type-info fields",
        r"the (\w+) object\|None type-info fields",
        lambda: _none_valued(ts._GET_ARCHIVE_INFO_BASE_TYPE),
        reads=("_GET_ARCHIVE_INFO_BASE_TYPE",),
    ),
    # --- the same subsets, re-stated in src/ where the values are produced -----------------------
    _claim(
        "enrichment fields (validator note)",
        r"bite on these (\w+) fields",
        lambda: _none_valued(ts._ARCHIVE_STATUS_BASE_TYPE),
        reads=("_ARCHIVE_STATUS_BASE_TYPE",),
    ),
    _claim(
        "type-info fields (builder note)",
        r"the (\w+) type-info fields appear only",
        lambda: _none_valued(ts._GET_ARCHIVE_INFO_BASE_TYPE),
        reads=("_GET_ARCHIVE_INFO_BASE_TYPE",),
    ),
    _claim(
        "topology fields (builder note)",
        r"the (\w+) projected topology fields",
        lambda: _none_valued(ts._GET_APPLIANCE_INFO_BASE_TYPE),
        reads=("_GET_APPLIANCE_INFO_BASE_TYPE",),
    ),
    _claim(
        "topology fields (copy note)",
        r"The (\w+) fields are COPIED UNCONVERTED",
        lambda: _none_valued(ts._GET_APPLIANCE_INFO_BASE_TYPE),
        reads=("_GET_APPLIANCE_INFO_BASE_TYPE",),
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
}

# Per FILE, not one total. A single number lets an addition cancel a deletion out: measured, a real
# size-naming sentence could be removed and every test stayed green because an unrelated one
# appeared elsewhere. Per file narrows that to "added and removed in the SAME file in the same
# commit". These are the only hand-kept numbers outside ``_FROZEN``'s keys.
_INVENTORY_SIZES: dict[str, int] = {
    "tests/test_server.py": 79,
    "services/checkers_olog.py": 4,
    "services/checkers.py": 5,
    "tools/archiver.py": 5,
    # 2 -> 3 as the repair of the same defect the "display-gated tools" claim above records: the
    # third phrase has been in build_instructions' docstring since c5d4ad7 with no pin to match it.
    "server.py": 3,
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
    wrong in one build. Going red here means the prose is stale, not that the guard is."""
    wrong: list[str] = []
    for block in _watched_blocks():
        for match, claim in _claim_matches(block):
            named = parse_count(match.group(1))
            expected = claim.measure()
            if named != expected:
                wrong.append(
                    f"{block.where()} {claim.label}: prose says {named}, the set has {expected}"
                )
    assert not wrong, "prose counters no longer match their sets:\n  " + "\n  ".join(wrong)


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


def test_inventory_size_is_pinned() -> None:
    """The one hand-kept number in this module, and it is deliberate.

    Narrow on purpose, because the honest residual is narrow: a deleted DERIVED phrase is normally
    caught by ``test_every_claim_still_matches_prose`` and a deleted INVENTORIED one by
    ``test_every_frozen_entry_still_exists``. What escapes both is ONE occurrence of a phrase whose
    claim or frozen row still matches somewhere else, and several claims do match twice."""
    actual = Counter(site.block.path for site in iter_sites(_watched_blocks()))
    drift = {
        path: (actual.get(path, 0), _INVENTORY_SIZES.get(path))
        for path in {*_INVENTORY_SIZES, *actual}
        if actual.get(path, 0) != _INVENTORY_SIZES.get(path)
    }
    assert not drift, (
        "the number of size-naming phrases changed (file: found vs pinned): "
        + ", ".join(
            f"{path} {found} vs {pinned}" for path, (found, pinned) in sorted(drift.items())
        )
        + ". A phrase was added or removed, update the pin together with _CLAIMS/_FROZEN."
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
    * The FILE half is one-directional and, for the two measures that reach their file through
      ``_typed_dict_fields``, nearly free: that index parses the whole package, so "this file was
      parsed" does not distinguish it from any other file under ``src``.

    All three are stated here rather than left to be discovered.

    Cost, measured rather than estimated: about six seconds for the whole table. That is two cold
    measurement passes per claim, one traced, one not, because the recorder is compared against the
    untraced answer, with every memoized helper cleared on both sides of the traced one. The
    clearing is what the seconds buy: against a warm cache a measure touches nothing at all and the
    test would pass while proving nothing. Halving it by leaving the caches warm afterwards is
    possible and deliberately not done, it would let a value computed under the recorder outlive
    the trace, and a cheap test that can poison the rest of the suite is the wrong trade.
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


def test_the_tracer_can_see_every_source() -> None:
    """The tracer's PRECONDITION, asserted instead of assumed: ``_parsed`` is the only file reader.

    The tracer watches two seams. If a measure ever reads a file some other way, that read becomes
    invisible and every claim resting on it silently stops being traced, a guard that cannot go red
    is the defect it was built to remove. So the property the tracer depends on is a test, and it
    names no count: the reader has to sit inside ``_parsed``, however many there are."""
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
        expected = claim.measure()
        forms = {str(expected)} | {
            word for word, value in pn._WORD_VALUES.items() if value == expected
        }
        skeleton = re.sub(r"\\[dwsSWbAZ]|\{\d+(?:,\d+)?\}", "", claim.phrase.pattern)
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
