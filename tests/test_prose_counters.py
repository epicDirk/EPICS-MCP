"""S32: the numbers this repository's prose NAMES are compared to the sets they describe.

Comments and docstrings here state sizes — "all 22 schemas", "7 (resp. 9) rows", "TEN of the
twelve" — and until now nothing checked a single one of them. The cost was measured, not feared:
the S29 11th typing target pulled 18 counters over by hand, two adversarial QA rounds found 8 of
them still wrong, and one had already been wrong before that build began. The reading half of this
guard is ``tests/prose_numbers.py``; this module is the comparing half.

WHAT IS PROMISED, in three parts, because a vaguer promise would be the same defect one layer up:

* **Derived** — every claim in ``_CLAIMS`` is compared to a set that is computed here and now. A
  wrong number goes red with its file, line and enclosing scope. Nothing in ``_CLAIMS`` may carry a
  typed-in expectation; ``test_no_claim_hard_codes_its_expectation`` enforces that.
* **Inventoried** — every OTHER phrase the detector finds is listed in ``_FROZEN`` with a reason.
  Those are not verified: a new one, or a vanished one, goes red (``test_inventory_is_partitioned``
  and ``test_inventory_size_is_pinned``), but their VALUE is nobody's promise. Most are historical
  anchors, runtime measurements, or claims about test bodies, which no constant can settle.
* **Out of scope** — the detector pairs a number with one of a closed list of collection nouns, or
  reads the ``N of the M`` shape. It does not see every statement about a size, and it does not see
  numbers inside f-string assertion messages at all. The closed list lives in
  ``prose_numbers.COLLECTION_NOUNS`` and is the whole of the coverage claim.

Which claims are DERIVED and which are merely inventoried was decided by measurement, not taste: a
``git`` pickaxe over the real S29 commits shows the drift lives in five families — the typed-tool
cardinality, the untyped remainder, the explicit rows of ``_ALWAYS_PRESENT_BY_TOOL``, the element
schema split, and the declared arrays — plus the per-tool "EXACTLY the N mapped fields" line every
typing step adds a new one of. Lane counts and the "four paths" family have not moved once in the
recorded history of the file.

Every measurement here reads a module constant or AST-scans a source file. None of them takes a
length from ``mcp.list_tools()``: that answers with FEWER tools in the core-only lane than in the
full one, so a count taken there would pass locally and break the core-only CI — the trap
``tests/test_server.py`` warns about at its own ``_TYPED_OUTPUT_TOOLS``. (This paragraph
deliberately names no figure. A module that derives the lane counts for five other files must not
hand-type them in its own docstring, where nothing would ever check them.)
"""

from __future__ import annotations

import ast
import inspect
import re
import textwrap
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from tests import prose_numbers as pn
from tests import test_server as ts
from tests.prose_numbers import ProseBlock, ProseSite, iter_blocks, iter_sites, parse_count

_TESTS = Path(__file__).resolve().parent
_SRC = _TESTS.parent / "src" / "epics_pv_mcp"

# The files whose prose is watched. `checkers_olog.py` is named by the roadmap entry; the next
# three carry sentences that DUPLICATE a number watched in test_server.py, and a duplicate nobody
# compares is how the canonical side drifts while the test side is dutifully corrected.
#
# This guard's own two files are NOT watched, and the reason is measured rather than convenient.
# Watching them surfaces 41 phrases, of which roughly four fifths are QUOTATIONS of the estate's
# prose used to explain the design ("all 22 schemas", "TEN of the twelve"). Inventorying those
# would add dozens of rows saying "this is an example, not a claim" — a blanket exemption wearing
# a table's clothes. The genuine risk in them was a handful of sentences that hand-typed a number
# this module itself derives; those sentences were rewritten to stop naming the number at all, so
# there is nothing left to rot. Watching them properly is a separate, named piece of work.
_WATCHED: tuple[tuple[str, Path], ...] = (
    ("tests/test_server.py", _TESTS / "test_server.py"),
    ("services/checkers_olog.py", _SRC / "services" / "checkers_olog.py"),
    ("services/checkers.py", _SRC / "services" / "checkers.py"),
    ("tools/archiver.py", _SRC / "tools" / "archiver.py"),
    ("server.py", _SRC / "server.py"),
)


@dataclass(frozen=True)
class _Claim:
    """A phrase that names a size, and the set that decides what the size is."""

    label: str
    phrase: re.Pattern[str]
    measure: Callable[[], int]
    scope: str = ""

    def applies_to(self, block: ProseBlock) -> bool:
        return not self.scope or block.qualname == self.scope


def _claim(label: str, pattern: str, measure: Callable[[], int], scope: str = "") -> _Claim:
    return _Claim(label, re.compile(pattern, re.IGNORECASE), measure, scope)


def _derivation_source(measure: Callable[[], int]) -> str:
    """A claim's derivation as source text, so a typed-in answer THERE is visible too.

    Two things are deliberately NOT inspected, because both are legitimate places for a number to
    appear as prose rather than as an answer. A lambda's source is its whole enclosing statement,
    so everything before the ``lambda`` keyword — the claim's own pattern — is dropped. And a named
    function's DOCSTRING is dropped: these docstrings explain which set they count ("the twelve
    constants", "the TWO real-client tests"), and accusing them would punish the explanation the
    rest of this module asks for. Only executable code is searched.

    Unreadable source yields the empty string — no accusation, which is the honest answer when
    nothing was inspected.
    """
    try:
        source = textwrap.dedent(inspect.getsource(measure))
    except (OSError, TypeError):  # pragma: no cover - no measure here is unreadable
        return ""
    _, separator, tail = source.partition("lambda")
    if separator:
        return tail
    node = ast.parse(source).body[0]
    if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):  # pragma: no cover
        return source
    body = node.body[1:] if ast.get_docstring(node) else node.body
    return "\n".join(ast.unparse(statement) for statement in body)


# --- the measurements -------------------------------------------------------------------------


def _none_valued(mapping: Mapping[str, str | None]) -> int:
    """Fields the map declares with NO advertised base type.

    ``None`` means specifically ``object | None``. An ``X | None`` with a concrete ``X`` still
    carries its non-null type (``archived: bool | None`` maps to ``"boolean"``), so "the 8
    enrichment fields" is not "the nullable fields". Every 8 and 26 below rests on that.
    """
    return sum(1 for value in mapping.values() if value is None)


@cache
def _parsed(path: Path) -> ast.Module:
    """Parse once per process. Nothing here mutates a source file, so caching is safe — and the
    uncached version re-read and re-parsed the 177 KB test module up to 44 times per run."""
    return ast.parse(path.read_text(encoding="utf-8-sig"))


def _module_ast(module: Any) -> ast.Module:
    return _parsed(Path(module.__file__))


@cache
def _always_present_constants() -> int:
    """The ``*_ALWAYS_PRESENT`` constants — the "twelve" the prose counts."""
    return sum(1 for name in vars(ts) if name.endswith("_ALWAYS_PRESENT"))


def _named_tests(suffix: str) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        node
        for node in _module_ast(ts).body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.endswith(suffix)
    ]


@cache
def _conformance_tests() -> int:
    """The per-tool ``*_conforms_to_its_schema`` tests — the other "twelve"."""
    return len(_named_tests("_conforms_to_its_schema"))


@cache
def _real_client_conformance_tests() -> int:
    """Conformance tests that drive a REAL client over a ``paths`` table — the "TWO".

    BOTH halves are checked. Testing only for a local ``paths`` table makes the measure a proxy
    for the claim rather than the claim: converting such a test to the in-process ``call_tool``
    while keeping its table would falsify three separate sentences and redden nothing.
    """
    found = 0
    for node in _named_tests("_conforms_to_its_schema"):
        has_table = any(
            isinstance(inner, ast.AnnAssign)
            and isinstance(inner.target, ast.Name)
            and inner.target.id == "paths"
            for inner in ast.walk(node)
        )
        drives_client = any(
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Name)
            and inner.func.id == "Client"
            for inner in ast.walk(node)
        )
        found += has_table and drives_client
    return found


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
            # ``mcp.tool(...)(fn)`` — the call-position form. Requiring the OUTER call's func to
            # be a call is what keeps a decorator, which ``ast.walk`` also visits on its own, from
            # being counted a second time.
            found += _is_mcp_tool(node.func)
    return found


@cache
def _core_lane_tools() -> int:
    """Tools ``server.py`` registers — the core lane, AST-scanned, never taken from the wire."""
    return _registered_tools(_SRC / "server.py")


@cache
def _display_tools() -> int:
    """The display-extra tools. AST-scanned, never imported: importing ``display_tools.py`` pulls
    ``opi_navigation``, which the core-only lane does not have."""
    return _registered_tools(_SRC / "display_tools.py")


@cache
def _full_lane_tools() -> int:
    return _core_lane_tools() + _display_tools()


def _items_valued(element_schema: dict[str, object]) -> int:
    """Rows of ``_OUTPUT_ARRAY_ITEMS`` that declare *element_schema* — by identity, as intended."""
    return sum(1 for value in ts._OUTPUT_ARRAY_ITEMS.values() if value is element_schema)


@cache
def _distinct_element_schemas() -> int:
    """How many element schemas the array rows share between them."""
    return len({id(value) for value in ts._OUTPUT_ARRAY_ITEMS.values()})


def _is_typed_dict(node: ast.ClassDef) -> bool:
    return any(
        (isinstance(base, ast.Name) and base.id == "TypedDict")
        or (isinstance(base, ast.Attribute) and base.attr == "TypedDict")
        for base in node.bases
    )


@cache
def _nullable_array_fields() -> frozenset[str]:
    """``TypedDict`` FIELDS annotated ``list[...] | None``, across the whole package.

    Read from the ANNOTATIONS rather than from a live schema on purpose: ``mcp.list_tools()``
    answers differently in the two lanes, and a count taken there would pass locally and break
    core-only CI. Matched by field name, which is what ``_OUTPUT_ARRAY_ITEMS`` is keyed on.

    Two scoping mistakes were possible and both are closed. Reading only the two ``checkers``
    modules missed three of the array rows — they are declared in ``tools/discover.py``,
    ``tools/archiver.py`` and ``services/archiver_client.py`` — so the count was right only by the
    accident of those three being non-nullable. And accepting any annotated assignment let a
    function LOCAL stand in for a field (``warnings: list[str] = []`` already exists inside a
    function body), so only class-body annotations of a ``TypedDict`` subclass count.
    """
    names: set[str] = set()
    for path in sorted(_SRC.rglob("*.py")):
        for node in ast.walk(_parsed(path)):
            if not isinstance(node, ast.ClassDef) or not _is_typed_dict(node):
                continue
            for item in node.body:
                if not isinstance(item, ast.AnnAssign) or not isinstance(item.target, ast.Name):
                    continue
                annotation = ast.unparse(item.annotation).replace(" ", "")
                if annotation.startswith("list[") and annotation.endswith("|None"):
                    names.add(item.target.id)
    return frozenset(names)


@cache
def _nullable_array_rows() -> int:
    """Array rows whose field is an ``X | None`` — the ``anyOf[array, null]`` subset."""
    nullable = _nullable_array_fields()
    return sum(1 for _tool, field in ts._OUTPUT_ARRAY_ITEMS if field in nullable)


@cache
def _channel_info_fields() -> int:
    """Fields of ``ChannelInfo`` — a NAMESAKE of the find_channels key count, not the same set."""
    for node in ast.walk(_parsed(_SRC / "services" / "channelfinder_client.py")):
        if isinstance(node, ast.ClassDef) and node.name == "ChannelInfo":
            return sum(1 for item in node.body if isinstance(item, ast.AnnAssign))
    raise AssertionError("ChannelInfo is gone from channelfinder_client.py")


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
    """Rows of the ``paths`` table inside *test_name* — the return paths it drives."""
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
    """``query_olog_*`` coroutines in checkers_olog.py — the "ten" its own header claims."""
    return sum(
        1
        for node in _parsed(_SRC / "services" / "checkers_olog.py").body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name.startswith("query_olog_")
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
    """A closure per row — a loop variable captured directly would measure the LAST constant."""

    def measure() -> int:
        return len(getattr(ts, constant))

    return measure


def _mapped_field_claims() -> tuple[_Claim, ...]:
    return tuple(
        _claim(f"{constant} size", _MAPPED, _size_of(constant), scope=test)
        for test, constant in _MAPPED_FIELD_CLAIMS
    )


_CLAIMS: tuple[_Claim, ...] = (
    # --- family 1: the typed-tool cardinality, re-stated on every typing step -------------------
    _claim(
        "typed tools (recursive traversal)",
        r"traversal of all (\w+) schemas",
        lambda: len(ts._TYPED_OUTPUT_TOOLS),
    ),
    _claim(
        "typed tools (recursive walk)",
        r"walk of all (\w+) typed schemas",
        lambda: len(ts._TYPED_OUTPUT_TOOLS),
    ),
    _claim(
        "typed tools (scope note)",
        rf"properties of the (\w+) tools in {_TYPED}",
        lambda: len(ts._TYPED_OUTPUT_TOOLS),
    ),
    _claim(
        "typed tools (scan note)",
        r"a scan of all (\w+) schemas",
        lambda: len(ts._TYPED_OUTPUT_TOOLS),
    ),
    # --- family 2: the untyped remainder --------------------------------------------------------
    _claim(
        "untyped remainder",
        r"list fields of the (\w+) tools that are not typed yet",
        lambda: _full_lane_tools() - len(ts._TYPED_OUTPUT_TOOLS),
    ),
    # --- family 3: the rows of _ALWAYS_PRESENT_BY_TOOL and the twelve constants ------------------
    _claim(
        "explicit rows",
        r"the (\w+) explicit rows point straight at",
        lambda: len(ts._ALWAYS_PRESENT_BY_TOOL) - len(ts._OLOG_ALWAYS_PRESENT),
    ),
    _claim(
        "static-check constants",
        r"(\w+) of the \w+ are consulted only to",
        lambda: _always_present_constants() - _real_client_conformance_tests(),
    ),
    _claim(
        "always-present constants",
        r"\w+ of the (\w+) are consulted only to",
        _always_present_constants,
    ),
    _claim(
        "runtime-bound constants",
        r"The remaining (\w+), _DISCOVER_PVS_ALWAYS_PRESENT",
        _real_client_conformance_tests,
    ),
    _claim(
        "in-process conformance tests",
        r"(\w+) of the \w+ drive ``FastMCP.call_tool``",
        lambda: _conformance_tests() - _real_client_conformance_tests(),
    ),
    _claim(
        "conformance tests", r"\w+ of the (\w+) drive ``FastMCP.call_tool``", _conformance_tests
    ),
    _claim(
        "real-client conformance tests",
        r"The other (\w+), test_discover_pvs",
        _real_client_conformance_tests,
    ),
    _claim(
        "tools those ten cover",
        r"of the (\w+) tools THOSE TEN cover",
        lambda: len(ts._TYPED_OUTPUT_TOOLS) - _real_client_conformance_tests(),
    ),
    # --- family 4: the element-schema split ------------------------------------------------------
    _claim(
        "string-item rows (shared-dict note)",
        r"silently move (\w+) \(resp",
        lambda: _items_valued(ts._STRING_ITEMS),
    ),
    _claim(
        "opaque-item rows (shared-dict note)",
        r"silently move \w+ \(resp\. (\w+)\) rows",
        lambda: _items_valued(ts._OPAQUE_OBJECT_ITEMS),
    ),
    _claim(
        "string-item rows",
        r"most for the (\w+) rows carrying",
        lambda: _items_valued(ts._STRING_ITEMS),
    ),
    _claim(
        "opaque-item rows",
        r"the other (\w+) rows are",
        lambda: _items_valued(ts._OPAQUE_OBJECT_ITEMS),
    ),
    # --- family 5: the declared arrays -----------------------------------------------------------
    _claim("declared arrays", r"of the (\w+) declared arrays", lambda: len(ts._OUTPUT_ARRAY_ITEMS)),
    _claim("nullable arrays", r"(\w+) of the \w+ declared arrays", _nullable_array_rows),
    _claim(
        "nullable arrays (schema-test note)",
        r"the (\w+) rows happen to match",
        _nullable_array_rows,
    ),
    _claim(
        "shared element schemas", r"The (\w+) element schemas the estate", _distinct_element_schemas
    ),
    # --- the return-path tables the real-client conformance tests drive ---------------------------
    _claim(
        "discover_pvs return paths",
        r"discover_pvs emits on EVERY one of its (\w+) return paths",
        lambda: _paths_rows(_DISCOVER_CONFORMANCE),
        scope="<module>",
    ),
    # Anchored on the TOOL NAME. One pattern matched BOTH module-level sentences, so the
    # find_channels claim was verified against discover_pvs' table. Equal today — which means the
    # day either tool gains a path, the guard would demand the CORRECT sentence be changed back.
    _claim(
        "find_channels return paths",
        r"find_channels emits on EVERY one of its (\w+) return paths",
        lambda: _paths_rows(_FIND_CHANNELS_CONFORMANCE),
        scope="<module>",
    ),
    _claim(
        "discover_pvs paths (part A)",
        r"drives ALL (\w+) return paths",
        lambda: _paths_rows(_DISCOVER_CONFORMANCE),
        scope=_DISCOVER_CONFORMANCE,
    ),
    _claim(
        "discover_pvs paths (matter)",
        r"ALL (\w+) paths matter",
        lambda: _paths_rows(_DISCOVER_CONFORMANCE),
        scope=_DISCOVER_CONFORMANCE,
    ),
    _claim(
        "discover_pvs paths (union)",
        r"The (\w+) paths together",
        lambda: _paths_rows(_DISCOVER_CONFORMANCE),
        scope=_DISCOVER_CONFORMANCE,
    ),
    _claim(
        "find_channels paths (modes)",
        r"(\w+) paths . and here the \w+ are two MODES",
        lambda: _paths_rows(_FIND_CHANNELS_CONFORMANCE),
        scope=_FIND_CHANNELS_CONFORMANCE,
    ),
    _claim(
        "find_channels paths (rows)",
        r"makes all (\w+) rows load-bearing",
        lambda: _paths_rows(_FIND_CHANNELS_CONFORMANCE),
        scope=_FIND_CHANNELS_CONFORMANCE,
    ),
    _claim(
        "find_channels paths (load-bearing)",
        r"the reason all (\w+) paths are load-bearing",
        lambda: _paths_rows(_FIND_CHANNELS_CONFORMANCE),
        scope=_FIND_CHANNELS_CONFORMANCE,
    ),
    _claim(
        "find_channels paths (coverage)",
        r"satisfiable by two of the (\w+)",
        lambda: _paths_rows(_FIND_CHANNELS_CONFORMANCE),
        scope=_FIND_CHANNELS_CONFORMANCE,
    ),
    _claim(
        "real-client paths (wire note)", r"drive a real client over (\w+) paths", _return_path_rows
    ),
    _claim(
        "find_channels return paths (tool description)",
        r"the (\w+) paths differ further",
        _return_path_rows,
    ),
    _claim(
        "find_channels keys", r"permits all (\w+) keys", lambda: len(ts._FIND_CHANNELS_BASE_TYPE)
    ),
    # --- the per-tool mapped-field lines ---------------------------------------------------------
    *_mapped_field_claims(),
    # --- the ``X | None`` subsets of the per-tool maps --------------------------------------------
    _claim(
        "archive-status enrichment fields",
        r"The (\w+) DS-4A/AR-D enrichment fields",
        lambda: _none_valued(ts._ARCHIVE_STATUS_BASE_TYPE),
    ),
    _claim(
        "archive-status enrichment fields (runtime note)",
        r"status \+ the (\w+) enrichment fields",
        lambda: _none_valued(ts._ARCHIVE_STATUS_BASE_TYPE),
    ),
    _claim(
        "appliance topology fields",
        r"the (\w+) object\|None topology fields",
        lambda: _none_valued(ts._GET_APPLIANCE_INFO_BASE_TYPE),
    ),
    _claim(
        "archive-info projection fields",
        r"The (\w+) getPVTypeInfo projection fields",
        lambda: _none_valued(ts._GET_ARCHIVE_INFO_BASE_TYPE),
    ),
    _claim(
        "archive-info type-info fields",
        r"the (\w+) object\|None type-info fields",
        lambda: _none_valued(ts._GET_ARCHIVE_INFO_BASE_TYPE),
    ),
    # --- the same subsets, re-stated in src/ where the values are produced -----------------------
    _claim(
        "enrichment fields (validator note)",
        r"bite on these (\w+) fields",
        lambda: _none_valued(ts._ARCHIVE_STATUS_BASE_TYPE),
    ),
    _claim(
        "type-info fields (builder note)",
        r"the (\w+) type-info fields appear only",
        lambda: _none_valued(ts._GET_ARCHIVE_INFO_BASE_TYPE),
    ),
    _claim(
        "topology fields (builder note)",
        r"the (\w+) projected topology fields",
        lambda: _none_valued(ts._GET_APPLIANCE_INFO_BASE_TYPE),
    ),
    _claim(
        "topology fields (copy note)",
        r"The (\w+) fields are COPIED UNCONVERTED",
        lambda: _none_valued(ts._GET_APPLIANCE_INFO_BASE_TYPE),
    ),
    _claim(
        "ChannelInfo fields",
        r"``ChannelInfo`` \((\w+) fields, all required\)",
        _channel_info_fields,
    ),
    # --- the per-tool tables the Olog and archiver-history claims point at ------------------------
    _claim(
        "olog always-present tools",
        r"uniform over all (\w+) tools",
        lambda: len(ts._OLOG_ALWAYS_PRESENT),
    ),
    # "Pre-fix this tripped on 8/11 tools": the numerator is a frozen historical measurement, but
    # the DENOMINATOR is the live Olog tool count and drifts with every Olog tool added.
    _claim(
        "olog tools (historical ratio)",
        r"tripped on \d+/(\w+) tools",
        lambda: len(ts._OLOG_ALWAYS_PRESENT),
    ),
    _claim(
        "archiver-history fields",
        r"ArchiverHistoryResult's (\w+) fields",
        lambda: len(ts._ARCHIVER_HISTORY_BASE_TYPE),
    ),
    # --- the annotation tables -------------------------------------------------------------------
    _claim(
        "title-parameter tools",
        r"The (\w+) tools that carry a parameter",
        lambda: len(ts._TITLE_PARAMETER_TOOLS),
    ),
    _claim(
        "title-parameter tools (trap note)",
        r"the (\w+) tools with a ``title`` PARAMETER",
        lambda: len(ts._TITLE_PARAMETER_TOOLS),
    ),
    _claim(
        "annotation hint fields",
        r"The (\w+) boolean hint fields",
        lambda: len(ts._ANNOTATION_HINTS),
    ),
    _claim(
        "annotation hint fields (law)",
        r"with all (\w+) hint fields explicitly set",
        lambda: len(ts._ANNOTATION_HINTS),
    ),
    _claim(
        "annotation hint fields (client note)",
        r"The (\w+) hints drive real client behaviour",
        lambda: len(ts._ANNOTATION_HINTS),
    ),
    _claim(
        "annotation hint fields (default note)",
        r"leaves all (\w+) fields None",
        lambda: len(ts._ANNOTATION_HINTS),
    ),
    _claim(
        "destructive golden rows",
        r"antecedent holds for exactly (\w+) tools today",
        _destructive_golden_rows,
    ),
    # --- the two lanes ---------------------------------------------------------------------------
    _claim("core lane (displays absent)", r"\[displays\] absent, (\w+) tools", _core_lane_tools),
    _claim("full lane (install note)", r"a full \((\w+)\) install", _full_lane_tools),
    _claim("core lane (budget)", r"core-only lane \((\w+) tools\)", _core_lane_tools),
    _claim("full lane (budget)", r"the full lane \((\w+)\) pass", _full_lane_tools),
    _claim("full lane (golden map)", r"Covers all (\w+) full-lane tools", _full_lane_tools),
    _claim(
        "display tools (golden map)", r"the (\w+) display-extra tools are absent in", _display_tools
    ),
    _claim("core lane (golden map)", r"core-only CI \((\w+) tools\)", _core_lane_tools),
    _claim(
        "full lane (golden map presence)", r"PRESENT in the full lane \((\w+)\)", _full_lane_tools
    ),
    _claim("core lane (drift logic)", r"Core-only lane \((\w+)\)", _core_lane_tools),
    _claim("core lane (annotations)", r"core-only CI = (\w+) tools", _core_lane_tools),
    _claim("full lane (annotations)", r"core-only CI = \w+ tools, full = (\w+)", _full_lane_tools),
    _claim(
        "display tools (registrar)", r"registers exactly the (\w+) display tools", _display_tools
    ),
    # --- the canonical Olog block ----------------------------------------------------------------
    _claim("olog query functions", r"The (\w+) ``query_olog_\*`` functions", _olog_query_functions),
    _claim(
        "olog query functions (re-export)", r"re-exports the (\w+) functions", _olog_query_functions
    ),
)


# --- the inventory ------------------------------------------------------------------------------

# Phrases the detector finds and NOBODY verifies, each with the reason it cannot be derived. Keyed
# by (file, enclosing scope, phrase, value) — never by line number, which moves with any edit above
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
    ): "how many tools emit no null on the driven path — observed at runtime, not declared",
    (
        "tests/test_server.py",
        "test_search_logbook_payload_path_is_guarded_below_the_client",
        "1``: 19 other tests",
        19,
    ): "how many tests fall under a mutated env var — a suite measurement, and the sentence itself "
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
    ): ("test functions that react to one mutation — no table declares them"),
    ("tests/test_server.py", "test_output_schema_typed_only_for_typed_tools", "three tests", 3): (
        "test functions a red proof turns red — measured by running it, not declared"
    ),
    # --- structural claims about code that no constant enumerates -------------------------------
    ("server.py", "main", "three olog write paths", 3): (
        "Olog write paths through the server; no table lists them"
    ),
    ("services/checkers.py", "<module>", "three generic schema guards", 3): (
        "guards in the JSON-Schema walker; no table lists them"
    ),
    ("services/checkers_olog.py", "<module>", "four rest-plane checkers", 4): (
        "REST planes, not a Python collection — there is no plane enum to count"
    ),
    ("services/checkers_olog.py", "<module>", "two validators", 2): (
        "a fact about the fastmcp SDK, not about this repository"
    ),
    ("services/checkers_olog.py", "query_olog_add_attachment._run", "two gate modules", 2): (
        "gate modules named in prose; the write-gate contract test discovers them by scanning"
    ),
    ("tools/archiver.py", "<module>", "two non-nullable fields", 2): (
        "fields present on every path of one schema — a shape claim, not a table size"
    ),
    # --- a DIFFERENT set that happens to have the same size --------------------------------------
    ("tests/test_server.py", _FIND_CHANNELS_CONFORMANCE, "two enabled paths", 2): (
        "the enabled half of the return paths, not the return-path table itself"
    ),
    ("tests/test_server.py", _FIND_CHANNELS_CONFORMANCE, "four client-edge error paths", 4): (
        "error paths inside the ChannelFinder client — a different four from the return paths"
    ),
    ("tests/test_server.py", _FIND_CHANNELS_CONFORMANCE, "two of the four", 2): (
        "how few paths satisfy the union — a coverage property, measured, not declared"
    ),
}

# Per FILE, not one total. A single number lets an addition cancel a deletion out: measured, a real
# size-naming sentence could be removed and every test stayed green because an unrelated one
# appeared elsewhere. Per file narrows that to "added and removed in the SAME file in the same
# commit". These are the only hand-kept numbers outside ``_FROZEN``'s keys.
_INVENTORY_SIZES: dict[str, int] = {
    "tests/test_server.py": 79,
    "services/checkers_olog.py": 5,
    "services/checkers.py": 5,
    "tools/archiver.py": 5,
    "server.py": 2,
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
    """Covered means the number sits in a claim's CAPTURE GROUP — not merely in its match.

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
    """Every claim must still find its phrase — a claim that matches nothing verifies nothing.

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
        "these prose numbers are neither derived nor inventoried — add a claim in _CLAIMS if the "
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
    claim or frozen row still matches somewhere else — and several claims do match twice."""
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
        + ". A phrase was added or removed — update the pin together with _CLAIMS/_FROZEN."
    )


def test_no_claim_hard_codes_its_expectation() -> None:
    """No claim may spell out the answer it is supposed to derive — in EITHER half.

    Checking only the pattern is not enough, and that was settled by execution rather than by
    argument: replacing one claim's derivation with a typed-in answer left every test in this
    module green. A typed-in integer in the MEASURE is the same defect as one in the PATTERN, so
    the derivation's source is inspected too, in digits AND in words — this prose spells its
    numbers as words far more often than as digits, so a digits-only check guards the rarer half.

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
