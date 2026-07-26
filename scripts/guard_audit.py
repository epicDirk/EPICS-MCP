#!/usr/bin/env python3
"""S31 sham-guard audit: are the client-edge guards actually OBSERVED by any test?

The clients in ``services/*_client.py`` validate what a foreign service answered, so an
unreadable payload becomes a loud error instead of a fabricated empty result. A test that
replaces the client CLASS with a double (``monkeypatch.setattr(<mod>, "<X>Client", _Fake)``)
removes every one of those guards from the path while still looking like it protects them: a
SHAM GUARD. See CLAUDE.md, evidence discipline point 8.

Two directions, and they answer different questions:

* ``sham`` (direction B, the roadmap's actual assignment): which tests CLAIM a guard they never
  execute. Pure analysis over the coverage map; runs no test at all.
* ``sweep`` (direction A): which guards no test observes, by mutating each one and seeing
  whether anything goes red.

⚠️ The map MUST be recorded with ``COVERAGE_CORE=ctrace``. On Python 3.12+ coverage defaults to
the ``sys.monitoring`` core, which disables a location after its FIRST observation, so later
tests covering the same line leave no context row. Measured on this repository, both maps recorded
on the same 1472-test tree (2026-07-26): 72 tests touch a guard line under the default core versus
292 under ctrace, median 2 versus 13 per covered line, and the default map reports fewer covering
tests on 58 of the 61 covered guard lines. A sweep driven by the default map would run a quarter of
the relevant tests and report false survivors, so the audit would itself be the sham guard it exists
to find. Record it with::

    COVERAGE_CORE=ctrace COVERAGE_FILE=<scratch>/cov uv run pytest \\
        --cov=src --cov-branch --cov-context=test

Keep COVERAGE_FILE outside the repo so the checked-in ``.coverage`` is untouched.

Mutation safety, because a mutation writes into the source tree: bytes only (never text mode,
the tree has mixed LF/CRLF), sha256 checked BEFORE each mutation against the run's baseline (a
check afterwards is tautological: it compares bytes the sweep wrote to a snapshot the sweep
took), restore in ``finally`` plus an ``atexit`` hook, and every mutant compiled before it is
run so a bad splice aborts instead of counting as "every guard is observed".
"""

from __future__ import annotations

import argparse
import ast
import atexit
import hashlib
import os
import re
import sqlite3
import subprocess
import sys
import traceback
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_SERVICES = _REPO / "src" / "epics_pv_mcp" / "services"
_TESTS = _REPO / "tests"

# A red run only proves the guard is OBSERVED if the failure carries the guard's own diagnosis.
# Forcing a check to fire can equally crash the happy path (``.get()`` on a non-dict, ``int()`` on
# a string), which says nothing about whether anyone watches the guard.
_DECLARED_EXC = re.compile(r"\b\w*(?:ResponseError|ConnectionError|EpicsError|ToolError)\b")
_COLLATERAL_EXC = re.compile(r"\b(?:TypeError|KeyError|AttributeError|IndexError|ValueError)\b")


@dataclass(frozen=True)
class Target:
    """One mutable guard site: an ``isinstance`` call, or a whole composite condition."""

    module: str
    lineno: int
    col: int
    end_lineno: int
    end_col: int
    form: str  # RAISE-GUARD | NARROW-BRANCH | FILTER | TERNARY | OTHER | WHOLE-CONDITION
    composite: bool  # the call is one conjunct of a larger boolean condition

    @property
    def key(self) -> str:
        """A unique id. The END offset is part of it, NOT decoration: a composite condition and
        its first conjunct start at the same line and column, so a start-only key collides for
        every such pair (measured, 9 of them here) and silently merges two different mutants
        into one row."""
        return f"{self.module}:{self.lineno}:{self.col}-{self.end_lineno}:{self.end_col}"


def client_modules() -> list[Path]:
    """The six client modules whose edges this audit covers, in a stable order."""
    return sorted(_SERVICES.glob("*_client.py"))


def _form_of(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    """Classify a call by the construct that consumes it: a raise guard is not a coercion."""
    current = node
    while current in parents:
        parent = parents[current]
        if isinstance(parent, ast.If):
            body_is_raise = all(isinstance(stmt, ast.Raise) for stmt in parent.body)
            return "RAISE-GUARD" if body_is_raise else "NARROW-BRANCH"
        if isinstance(parent, ast.IfExp):
            return "TERNARY"
        if isinstance(parent, ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp):
            return "FILTER"
        current = parent
    return "OTHER"


def _walk_with_parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}


def _isinstance_calls(tree: ast.AST) -> Iterator[ast.Call]:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "isinstance":
            yield node


def enumerate_targets() -> list[Target]:
    """Every mutable guard site across the client modules, derived from the AST, never from grep.

    Offsets, not line numbers: 11 lines carry two calls each and one ``if`` spans seven lines with
    three, so a line-wise replacement cannot address a single call and would break the ``or``
    scaffolding into a SyntaxError.

    Composite conditions get an extra WHOLE-CONDITION target. Splicing one conjunct of
    ``not isinstance(x, dict) or "secs" not in x`` leaves the rest of the condition standing, so the
    guard does not disappear and a green result would mean "this conjunct is unobserved", not
    "this guard is unguarded".
    """
    targets: list[Target] = []
    for path in client_modules():
        tree = ast.parse(path.read_bytes())
        parents = _walk_with_parents(tree)
        whole: set[tuple[int, int]] = set()
        for call in _isinstance_calls(tree):
            composite = isinstance(parents.get(call), ast.BoolOp) or isinstance(
                parents.get(parents.get(call, call), call), ast.BoolOp
            )
            targets.append(
                Target(
                    module=path.name,
                    lineno=call.lineno,
                    col=call.col_offset,
                    end_lineno=call.end_lineno or call.lineno,
                    end_col=call.end_col_offset or call.col_offset,
                    form=_form_of(call, parents),
                    composite=composite,
                )
            )
            if composite:
                node: ast.AST = call
                while node in parents and not isinstance(parents[node], ast.If | ast.IfExp):
                    node = parents[node]
                owner = parents.get(node)
                if isinstance(owner, ast.If | ast.IfExp):
                    test = owner.test
                    span = (test.lineno, test.col_offset)
                    if span not in whole:
                        whole.add(span)
                        targets.append(
                            Target(
                                module=path.name,
                                lineno=test.lineno,
                                col=test.col_offset,
                                end_lineno=test.end_lineno or test.lineno,
                                end_col=test.end_col_offset or test.col_offset,
                                form="WHOLE-CONDITION",
                                composite=True,
                            )
                        )
    return sorted(targets, key=lambda t: (t.module, t.lineno, t.col))


def splice(data: bytes, target: Target, replacement: bytes) -> bytes:
    """Replace *target*'s byte span with *replacement*, preserving every line ending verbatim."""
    lines = data.splitlines(keepends=True)
    head = lines[target.lineno - 1][: target.col]
    tail = lines[target.end_lineno - 1][target.end_col :]
    merged = head + replacement + tail
    return b"".join([*lines[: target.lineno - 1], merged, *lines[target.end_lineno :]])


# coverage stores a context as ``<node id>|<phase>``. The phase is the LAST segment, and a node id
# may itself contain a pipe, and six do here, e.g. a parametrised case whose id ends in
# ``[level-'|']``. Splitting on the FIRST pipe truncated those to a prefix that matches no test.
_COVERAGE_PHASES = frozenset({"setup", "run", "teardown"})


def _node_id_of(context: str) -> str:
    """The test node id inside a coverage context, with the trailing phase removed."""
    head, _sep, tail = context.rpartition("|")
    return head if tail in _COVERAGE_PHASES and head else context


def test_identity(node_id: str) -> tuple[str, str]:
    """``(test file name, function name)``: the pair a claiming test is keyed by.

    A node id is ``<path>::[<class>::]<function>[<param case>]``, and BOTH tails matter. Comparing
    with ``endswith("::" + name)`` ignored the file and could never match a parametrised id;
    measured on a real ctrace map, that credited ``test_olog_write.py::test_unknown_level_refused``
    (a candidate carrying payload vocabulary) with the guard-line execution of a SAME-NAMED test
    in another file, and so hid it from the sham-candidate list this audit exists to produce.
    """
    path, _sep, rest = node_id.partition("::")
    function = rest.rpartition("::")[2] if "::" in rest else rest
    return Path(path).name, function.split("[")[0]


def load_coverage_map(db_path: Path) -> dict[tuple[str, int], set[str]]:
    """``{(module, lineno): {test node id}}`` from a ``--cov-context=test`` database.

    Reads the ``arc`` table: with branch coverage on, that is where the data lives: ``line_bits``
    stays empty, and querying it instead yields a silent, uniformly empty map.
    """
    names = {path.name for path in client_modules()}
    connection = sqlite3.connect(db_path)
    try:
        file_rows = connection.execute("select id, path from file")
        files = {int(fid): str(path) for fid, path in file_rows}
        context_rows = connection.execute("select id, context from context")
        contexts = {int(cid): str(ctx) for cid, ctx in context_rows}
        covering: dict[tuple[str, int], set[str]] = {}
        rows = connection.execute("select file_id, context_id, fromno, tono from arc")
        for file_id, context_id, fromno, tono in rows:
            module = Path(str(files[int(file_id)])).name
            if module not in names:
                continue
            context = _node_id_of(contexts[int(context_id)])
            if not context:
                continue
            for lineno in (abs(int(fromno)), abs(int(tono))):
                covering.setdefault((module, lineno), set()).add(context)
        return covering
    finally:
        connection.close()


# The name a client class carries, in either house form: the dotted string path
# (``monkeypatch.setattr("…services.olog_client.OlogClient", _Fake)``) or the module object
# (``monkeypatch.setattr(checkers, "OlogClient", _Fake)``).
_CLIENT_CLASS = re.compile(r"\w*Client")


def _is_setattr(call: ast.Call) -> bool:
    """``setattr(...)`` or ``<anything>.setattr(...)``: the fixture may be renamed."""
    func = call.func
    return (isinstance(func, ast.Name) and func.id == "setattr") or (
        isinstance(func, ast.Attribute) and func.attr == "setattr"
    )


def _string_constant(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def class_double_calls(node: ast.AST) -> list[ast.Call]:
    """Every call inside *node* that replaces a client CLASS, read from the syntax tree.

    Read from the TREE, never from the source text, and the reason is measured rather than
    stylistic: a regex over the function's source segment also searches its DOCSTRING, and this
    estate's own exemplar of the CORRECT seam quotes the wrong idiom there to explain what it is
    avoiding. It was counted as a class double for eleven months of nothing noticing, which put
    both AST pins one too high while ``sham --check`` reported "all agree": the sham this file
    exists to find, inside the file.

    ⚠️ The obvious repair is the wrong one, and it was measured before it was rejected: blanking
    string literals before matching collapses the population from 107 to FOUR, because the client
    class NAME IS a string literal in both house idioms. "Do not match inside strings" is exactly
    backwards here; what has to change is reading text at all.

    ``monkeypatch.setattr`` has two signatures and they disagree about what the second slot means:
    ``(target, "Name", value)`` and ``("dotted.path.Name", value)``. Requiring three arguments for
    the first is not decoration: in the two-argument form the second slot is the REPLACEMENT, so
    ``setattr("mod.SOME_SETTING", "OlogClient")`` would otherwise be read as installing a double.
    """
    found: list[ast.Call] = []
    for inner in ast.walk(node):
        if not isinstance(inner, ast.Call) or not _is_setattr(inner):
            continue
        if len(inner.args) >= 3:
            attribute = _string_constant(inner.args[1])
            if attribute and _CLIENT_CLASS.fullmatch(attribute):
                found.append(inner)
                continue
        dotted = _string_constant(inner.args[0]) if inner.args else None
        if dotted and "." in dotted and _CLIENT_CLASS.fullmatch(dotted.rsplit(".", 1)[1]):
            found.append(inner)
    return found


def _is_boom_double(call: ast.Call) -> bool:
    """The replacement handed to this call is a ``_boom`` double, a negative control.

    Judged per CALL rather than over the whole function text: the rule is about what this double
    IS, and a test that installs a real double AND mentions a ``_boom`` one elsewhere is not a
    negative control. Measured: on the delivered tree both readings exclude the same 19 tests.
    """
    if len(call.args) >= 3:
        replacement: ast.expr | None = call.args[2]
    elif len(call.args) == 2:
        replacement = call.args[1]
    else:
        replacement = None
    return replacement is not None and "_boom" in ast.dump(replacement)


# Vocabulary that marks a test as being ABOUT a payload the client edge inspects, as opposed to
# a double used merely to keep a service-layer test off the network.
_EDGE_CLAIM = re.compile(
    r"unreadable|payload|malformed|non_dict|without_name|non_string|_raises"
    # Widened after a review found the first list too narrow to carry the "none found" claim:
    # a test whose docstring says "the service ANSWERED and we could not read it" states the
    # edge claim exactly, and none of the words above appear in it.
    r"|answer|respons|reject|refus|invalid|bad_|strict|corrupt|garbage|junk|schema"
)


# --- the recorded findings, as data ------------------------------------------------------------
#
# S33. The 2026-07-25 audit's figures used to live only in a docstring, where nothing compared them
# to the code they describe. They are pinned here, in the tool that produces them, so the tool can
# check itself; ``tests/test_client_edge_guards.py`` imports these rather than restating them.
#
# Split by what checking them COSTS. The first two follow from this repository's own AST and are
# therefore cheap enough for the ordinary test gate. The other two are decided by which tests
# EXECUTED a guard line, which needs a coverage map recorded with COVERAGE_CORE=ctrace, so minutes,
# not milliseconds. ``--check`` without a database verifies the cheap pair and says so; it never
# reports "OK" for a figure it could not reach.
DOUBLES = "tests installing a client class double in their own body"
EDGE_VOCABULARY = "of those, carrying payload vocabulary"
NOT_EXECUTING = "of those, never executing a client-edge line"
SHAM_CANDIDATES = "and claiming something about the ANSWERED payload"

PINNED_AST: dict[str, int] = {DOUBLES: 103, EDGE_VOCABULARY: 21}
PINNED_COVERAGE: dict[str, int] = {NOT_EXECUTING: 103, SHAM_CANDIDATES: 21}
PINNED: dict[str, int] = {**PINNED_AST, **PINNED_COVERAGE}

# The candidate list, by NAME. A count is not a finding: the verdict "no sham guard found" was
# reached by a person reading THESE tests, and a list of the same length with different members
# satisfies every numeric pin. Recording the members turns "re-judge the verdict" from an
# instruction nobody can check into a diff: added names are what has not been read.
PINNED_CANDIDATES: tuple[str, ...] = (
    "test_archiver.py::test_get_pv_history_bad_time_is_not_a_connection_error",
    "test_archiver.py::test_list_archived_pvs_empty_pattern_with_this_appliance_is_fine",
    "test_archiver.py::test_list_archived_pvs_refuses_pattern_with_this_appliance",
    "test_checkers.py::test_query_alarm_configured_response_error_is_not_a_connection_error",
    "test_checkers.py::test_query_alarm_history_response_error_is_not_a_connection_error",
    "test_checkers.py::test_query_channels_response_error_is_not_a_connection_error",
    "test_checkers.py::test_query_naming_lookup_404_is_definitive_not_registered",
    "test_checkers.py::test_query_naming_lookup_obsolete_preserves_status",
    "test_checkers.py::test_query_olog_response_error_is_not_a_connection_error",
    "test_doctor.py::test_unverified_plane_does_not_fail_but_is_reported",
    "test_olog.py::test_empty_page_past_the_end_is_not_annotated",
    "test_olog.py::test_list_log_levels_splits_outage_from_bad_answer",
    "test_olog.py::test_note_on_a_mixed_or_list_does_not_generalise",
    "test_olog.py::test_search_bad_time_is_not_a_connection_error",
    "test_olog.py::test_unreadable_levels_lookup_says_so_and_keeps_the_result",
    "test_olog_attachments.py::test_refuses_when_not_whole_mode",
    "test_olog_write.py::test_bad_level_does_not_burn_a_rate_token",
    "test_olog_write.py::test_blank_level_refused",
    "test_olog_write.py::test_reply_tool_threads_and_bad_id_is_400",
    "test_olog_write.py::test_unknown_level_refused",
    "test_write_gate_contract.py::test_pre_gate_refusal_is_coded_apart_and_writes_no_audit_line",
)

# Two recipes, because the whole point of the split above is that they cost different amounts. A
# deviation in the AST pair is re-measured in under a second; telling its author to run a full
# ctrace suite would invert the very distinction this file is organised around.
RERUN_AST = (
    "re-read the candidate list (sham --list-candidates prints it, no database needed), re-judge "
    "the verdict, then re-record PINNED_AST here. Re-measure with: uv run python "
    "scripts/guard_audit.py sham --check"
)
RERUN_COVERAGE = (
    "re-record with: COVERAGE_CORE=ctrace COVERAGE_FILE=<scratch>/cov uv run pytest --cov=src "
    "--cov-branch --cov-context=test, then uv run python scripts/guard_audit.py sham --check "
    "--coverage-db <scratch>/cov"
)

# What the audit CANNOT pin, said here rather than left to be noticed: the verdict "no sham guard
# found" was reached by a human READING the candidate list, and only its SIZE is recorded. A list
# of the same length with different members satisfies every pin below. Widening _EDGE_CLAIM,
# which this file's own docstring invites, reddens EDGE_VOCABULARY but leaves the verdict, and
# the coverage-dependent 20, to be re-judged by a person.
UNPINNED_VERDICT = "no sham guard found BY THIS FILTER: a judgement about a list, not a count"


def claiming_tests() -> dict[str, list[tuple[str, bool]]]:
    """``{test file: [(test name, edge-claim)]}`` for tests that install a client CLASS double.

    The primary signal is mechanical and per-FUNCTION: the test's own body replaces a client
    class, so the real client edge is out of the path for that test. A per-file signal was tried
    first and is useless: it marks every test in a file that doubles a client anywhere, which
    swept in schema and CLI tests that never touch a client.

    The second element flags payload vocabulary (``unreadable``, ``malformed``, ``_raises`` …).
    That distinguishes "claims something about what the service ANSWERED" (the sham-guard
    candidates) from a double used merely to keep a service-layer test off the network, which is
    a legitimate use of the same tool. It is a filter for reading order, not a verdict.

    ``_boom`` doubles are excluded by construction: their point is that nothing runs (they assert
    no client is ever built), so they are negative controls rather than sham guards.

    Known blind spot, stated rather than implied: a double installed OUTSIDE the test body is not
    seen here. That covers a pytest fixture and, measured on this tree, a plain module-level helper
    and ``tests/test_olog_update.py`` installs one in ``_install_fake`` and 21 tests call it. Until
    the criterion was read from the syntax tree, three of those 21 were counted, by accident: the
    regex matched their METHOD patch onto the double. Consistently blind beats arbitrarily
    half-sighted; widening the signal to helper-installed doubles is its own piece of work.

    The docstring IS read, deliberately, but only for ``_EDGE_CLAIM``: a test whose prose says "the
    service answered and we could not read it" states the edge claim in words its name does not
    carry. What must never come from prose is whether a double was INSTALLED: see
    ``class_double_calls``.
    """
    found: dict[str, list[tuple[str, bool]]] = {}
    # pytest's own default is ``test_*.py *_test.py``, and pyproject sets no ``python_files``. A
    # narrower glob here would let a contributor add doubles under a name pytest RUNS and the
    # audited population does not see. Latent today: no such file exists, so this closes a hole
    # rather than fixing a wrong figure.
    for path in sorted(set(_TESTS.glob("test_*.py")) | set(_TESTS.glob("*_test.py"))):
        for node in ast.walk(ast.parse(path.read_bytes())):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if not node.name.startswith("test_"):
                continue
            if not [call for call in class_double_calls(node) if not _is_boom_double(call)]:
                continue
            claim = bool(_EDGE_CLAIM.search(node.name + (ast.get_docstring(node) or "")))
            found.setdefault(path.name, []).append((node.name, claim))
    return found


def cmd_targets(_args: argparse.Namespace) -> int:
    """Print the target inventory grouped by form: the population, before any mutation."""
    targets = enumerate_targets()
    by_form: dict[str, int] = {}
    for target in targets:
        by_form[target.form] = by_form.get(target.form, 0) + 1
    sys.stderr.write(f"targets: {len(targets)} across {len(client_modules())} client modules\n")
    for form in sorted(by_form):
        sys.stderr.write(f"  {form:16s} {by_form[form]}\n")
    for target in targets:
        sys.stderr.write(f"  {target.key:44s} {target.form}\n")
    return 0


def population() -> dict[str, int]:
    """The two figures direction B can state WITHOUT a coverage map, from this file's AST alone."""
    claiming = claiming_tests()
    return {
        DOUBLES: sum(len(entries) for entries in claiming.values()),
        EDGE_VOCABULARY: sum(1 for entries in claiming.values() for _n, edge in entries if edge),
    }


def _compare(measured: dict[str, int]) -> int:
    """Report ``--check``: 1 for a deviation, 0 for agreement, and never silence about the rest.

    What was NOT compared is DERIVED from the pins minus the measurements, never hand-passed. The
    first version took the skipped list as an argument, so a pin belonging to neither list simply
    vanished while the verdict line still read "all agree with the recorded audit": the sham this
    function exists to make impossible, inside the function itself.
    """
    unknown = sorted(measured.keys() - PINNED.keys())
    if unknown:
        sys.stderr.write(f"measured but not pinned, so nothing to compare against: {unknown}\n")
    deviating = [
        (name, f"  {name}: pinned {PINNED[name]}, measured {value}")
        for name, value in sorted(measured.items())
        if name in PINNED and PINNED[name] != value
    ]
    skipped = sorted(PINNED.keys() - measured.keys())
    if skipped:
        sys.stderr.write("NOT checked here:\n")
        for name in skipped:
            reason = "needs --coverage-db" if name in PINNED_COVERAGE else "nothing measured it"
            sys.stderr.write(f"  {name} (pinned {PINNED[name]}): {reason}\n")
    sys.stderr.write(f"NOT pinnable at all: {UNPINNED_VERDICT}\n")
    if deviating:
        cheap = all(name in PINNED_AST for name, _line in deviating)
        # A DISJUNCTION when a coverage figure moved, not a diagnosis. Those figures depend on the
        # map as much as on the code, and the loudest hazard this file knows about (a map recorded
        # without COVERAGE_CORE=ctrace) produces exactly this deviation on a byte-identical tree.
        # The tool cannot tell the two apart: measured, the sqlite ``tracer`` table is empty even
        # for a map that WAS recorded with ctrace, so there is no marker to read. Naming only the
        # code would send a reader to re-record pins from a map that ran a quarter of the tests.
        sys.stderr.write(
            "PIN DEVIATION: the recorded audit describes different code now:\n"
            if cheap
            else "PIN DEVIATION: either the code changed, or this map is BLIND. A map recorded "
            "without COVERAGE_CORE=ctrace sees a fraction of the covering tests and deviates the "
            "same way. Check the core BEFORE re-recording anything:\n"
        )
        sys.stderr.write("\n".join(line for _name, line in deviating) + "\n")
        sys.stderr.write(f"{RERUN_AST if cheap else RERUN_COVERAGE}\n")
        return 1
    compared = len(measured.keys() & PINNED.keys())
    sys.stderr.write(f"checked {compared} pin(s): all agree with the recorded audit\n")
    return 0


def cmd_sham(args: argparse.Namespace) -> int:
    """Direction B: tests that claim a client-edge guard but never execute one."""
    # The AST half runs FIRST and unconditionally. It needs no coverage map, and putting it after
    # the map would make ``--check`` without a database dereference a missing path and die with a
    # TypeError, i.e. exit 1, indistinguishable from "a pin deviates", which is the one thing exit 1
    # is now contracted to mean.
    measured = population()
    if getattr(args, "list_candidates", False):
        # The list RERUN_AST sends a reader to, at AST cost. It used to be printed only on the
        # --coverage-db path, so the CHEAP recipe's first instruction demanded the expensive
        # artifact: the inversion the whole cost split exists to prevent.
        claiming = claiming_tests()
        sys.stderr.write(
            f"{measured[DOUBLES]} tests install a client class double in their own body; "
            f"{measured[EDGE_VOCABULARY]} of them carry payload vocabulary and are the list the "
            "verdict was reached by READING:\n"
        )
        for filename, entries in sorted(claiming.items()):
            for name, is_edge_claim in entries:
                if is_edge_claim:
                    sys.stderr.write(f"  {filename}::{name}\n")
        return 0
    if args.coverage_db is None:
        return _compare(measured)

    covering = load_coverage_map(Path(args.coverage_db))
    if not covering:
        sys.stderr.write(
            "empty coverage map: was it recorded with --cov-branch --cov-context=test?\n"
        )
        # The cheap pins were computed BEFORE the map was opened, so refusing to compare them here
        # would throw away work already in hand and leave the caller unable to tell "your map is
        # unusable" from "your map is unusable AND the AST pins moved". They are compared, the
        # coverage pair is named as unreached, and only then does the map problem decide the code.
        if args.check:
            _compare(measured)
        return 2
    # "executes a client edge" means a GUARD line, not merely any line of a client module: a test
    # can drive the client all day without ever reaching the check it claims to protect.
    guard_lines = {(target.module, target.lineno) for target in enumerate_targets()}
    executing: set[tuple[str, str]] = {
        test_identity(test)
        for key, tests in covering.items()
        if key in guard_lines
        for test in tests
    }
    # Test FUNCTIONS, not node ids: parametrised cases collapse onto their function, which is the
    # granularity a claiming test is recorded at. The figure is therefore lower than the number of
    # ids in the map and must not be compared with one.
    sys.stderr.write(
        f"map: {len(executing)} test functions execute at least one client-edge GUARD line\n"
    )
    sham_edge: list[str] = []
    sham_other = 0
    for filename, entries in sorted(claiming_tests().items()):
        for name, is_edge_claim in entries:
            if (filename, name) in executing:
                continue
            if is_edge_claim:
                sham_edge.append(f"{filename}::{name}")
            else:
                sham_other += 1
    measured[NOT_EXECUTING] = len(sham_edge) + sham_other
    measured[SHAM_CANDIDATES] = len(sham_edge)
    if args.check:
        # MEMBERS as well as counts, and BOTH are reported before anything returns. The verdict is
        # a judgement about these tests, so a list of the same length with different members must
        # not read as agreement: this file's own UNPINNED_VERDICT has said so since S33 and
        # nothing acted on it. Stopping at the first of the two findings would make a run's silence
        # about the other unreadable, which is the shape of defect this whole file is against.
        appeared = sorted(set(sham_edge) - set(PINNED_CANDIDATES))
        vanished = sorted(set(PINNED_CANDIDATES) - set(sham_edge))
        if appeared or vanished:
            sys.stderr.write(
                "CANDIDATE LIST CHANGED: the verdict was reached by reading the old one:\n"
            )
            for entry in appeared:
                sys.stderr.write(f"  + {entry}  (never read; read it before re-recording)\n")
            for entry in vanished:
                sys.stderr.write(f"  - {entry}\n")
            sys.stderr.write(f"{RERUN_AST}\n")
        return _compare(measured) or (1 if appeared or vanished else 0)
    sys.stderr.write(
        f"tests installing a client CLASS double in their own body: {measured[DOUBLES]}\n"
        f"  of those, never executing a client-edge line: {measured[NOT_EXECUTING]}\n"
        f"  and claiming something about the ANSWERED payload: {measured[SHAM_CANDIDATES]}\n\n"
    )
    for entry in sham_edge:
        sys.stderr.write(f"  {entry}\n")
    return 0


def _run_selection(node_ids: list[str], timeout: int) -> tuple[int, str]:
    """Run exactly *node_ids* and return ``(exit code, combined output)``."""
    env = dict(os.environ)
    env.update(
        PYTHONDONTWRITEBYTECODE="1",
        OPENBLAS_NUM_THREADS="1",
        OMP_NUM_THREADS="1",
    )
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--tb=line", *node_ids],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=_REPO,
        env=env,
    )
    return completed.returncode, completed.stdout + completed.stderr


def classify(exit_code: int, output: str, expected_passed: int) -> str:
    """Turn one mutant run into a verdict, by FAILURE KIND rather than by exit code.

    ``exit code 0`` alone is not "survived": an invalid node id exits 4/5 with nothing run, which
    would read as a dead guard. And a red run is only evidence that the guard is watched when the
    failure carries the guard's own diagnosis: a TypeError from forcing a check on means the
    happy path crashed, not that anyone observed the refusal.

    ⚠️ Honest limit, measured: for a RAISE-GUARD the enabling polarity makes the condition
    constantly true, so the guard fires on every input and every covering test dies with the
    module's own exception. ``KILLED-DECLARED`` is therefore TAUTOLOGICAL there: it says "a test
    executes this line", not "someone observes the refusal". All 21 raise guards in this estate
    have such a polarity. Only the DISABLING polarity carries information for them.
    """
    # pytest exit codes: 0 pass · 1 tests failed · 2 interrupted · 3 internal error · 4 usage
    # error · 5 no tests collected. Only 1 is evidence about the guard. ⚠️ Everything else used to
    # fall through to the KILLED- buckets, i.e. straight into "the guard is observed": a renamed
    # or moved test would have counted AS evidence FOR every guard it used to cover.
    if exit_code not in (0, 1):
        return "INVALID-SELECTION"
    if exit_code == 0:
        if f"\n{expected_passed} passed" not in f"\n{output}":
            return "INVALID-SELECTION"
        return "SURVIVED"
    if _DECLARED_EXC.search(output):
        return "KILLED-DECLARED"
    if _COLLATERAL_EXC.search(output):
        return "KILLED-COLLATERAL"
    return "KILLED-ASSERTION"


def cmd_sweep(args: argparse.Namespace) -> int:
    """Direction A: mutate each guard in both polarities and record what, if anything, notices."""
    covering = load_coverage_map(Path(args.coverage_db))
    if not covering:
        sys.stderr.write("empty coverage map: refusing to sweep against a blind map\n")
        return 2

    originals = {path: path.read_bytes() for path in client_modules()}
    baseline = {path: hashlib.sha256(data).hexdigest() for path, data in originals.items()}

    # What THIS sweep last wrote into each file. Restoring is only safe while the bytes on disk
    # are still ours: anything else is a foreign write, and stamping the original over it destroys
    # a parallel window's work. Measured before this existed: a foreign write during the mutant
    # run was overwritten silently and the sweep still reported success.
    written: dict[Path, bytes] = {}

    def restore_all() -> None:
        for path, data in originals.items():
            current = path.read_bytes()
            if hashlib.sha256(current).hexdigest() == baseline[path]:
                continue
            if current != written.get(path):
                sys.stderr.write(
                    f"REFUSING to restore {path.name}: on-disk bytes are neither the baseline nor "
                    f"what this sweep wrote, a foreign write. Left untouched ON PURPOSE; the "
                    f"pristine copy is in this process's memory only, so save the file elsewhere "
                    f"before re-running.\n"
                )
                continue
            path.write_bytes(data)
            written.pop(path, None)

    atexit.register(restore_all)
    sys.stderr.write(
        "restore command if this run is killed:\n"
        f"  git -C {_REPO} status --porcelain -- src/epics_pv_mcp/services/\n"
    )

    results: list[tuple[Target, str, str]] = []
    try:
        for target in enumerate_targets():
            tests = sorted(covering.get((target.module, target.lineno), set()))
            if not tests:
                results.append((target, "n/a", "NEVER-EXECUTED"))
                continue
            for polarity in (b"True", b"False"):
                path = _SERVICES / target.module
                current = path.read_bytes()
                if hashlib.sha256(current).hexdigest() != baseline[path]:
                    sys.stderr.write(f"DRIFT in {target.module}: foreign write. Aborting.\n")
                    return 3
                mutated = splice(current, target, polarity)
                try:
                    compile(mutated, str(path), "exec")
                except SyntaxError as exc:
                    sys.stderr.write(f"bad splice at {target.key}: {exc}. Aborting.\n")
                    return 4
                try:
                    path.write_bytes(mutated)
                    written[path] = mutated
                    code, output = _run_selection(tests, args.timeout)
                finally:
                    # NOT an unconditional write-back: the mutant run is where nearly all of the
                    # sweep's wall time sits, so it is also where a foreign write is most likely
                    # to land. restore_all() checks whose bytes are on disk first.
                    restore_all()
                verdict = classify(code, output, len(tests))
                results.append((target, polarity.decode(), verdict))
                if verdict == "INVALID-SELECTION":
                    sys.stderr.write(f"invalid selection for {target.key}. Aborting.\n")
                    return 5
    finally:
        restore_all()

    for target, applied, verdict in results:
        sys.stderr.write(f"{target.key:44s} {target.form:16s} {applied:5s} {verdict}\n")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("targets", help="print the guard inventory").set_defaults(func=cmd_targets)
    # ``sweep`` keeps the shared requirement; ``sham`` gets its own parser because ``--check``
    # gives it a mode that needs no database. Relaxing the requirement in the shared loop would
    # have relaxed it for ``sweep`` too, which dereferences the path unconditionally.
    sweep_parser = sub.add_parser("sweep", help=cmd_sweep.__doc__ or "")
    sweep_parser.add_argument("--coverage-db", required=True, help="a ctrace --cov-context=test db")
    sweep_parser.add_argument("--timeout", type=int, default=120, help="seconds per mutant run")
    sweep_parser.set_defaults(func=cmd_sweep)

    sham_parser = sub.add_parser("sham", help=cmd_sham.__doc__ or "")
    sham_parser.add_argument("--coverage-db", help="a ctrace --cov-context=test db")
    sham_parser.add_argument(
        "--check",
        action="store_true",
        help="compare against the recorded findings; exit 1 on a deviation. Without "
        "--coverage-db only the AST-derivable pins are checked, and the rest is named as unchecked",
    )
    sham_parser.add_argument(
        "--list-candidates",
        action="store_true",
        help="print the payload-vocabulary candidate list from the AST alone, no database needed: "
        "the list RERUN_AST asks a developer to re-read",
    )
    sham_parser.set_defaults(func=cmd_sham)

    args = parser.parse_args(argv[1:])
    if (
        args.command == "sham"
        and args.coverage_db is None
        and not args.check
        and not args.list_candidates
    ):
        # Reporting mode still needs a map. Spelled out rather than left to ``required=True`` so
        # that the message a caller has always seen does not change under them.
        sham_parser.error("the following arguments are required: --coverage-db")
    try:
        result: int = args.func(args)
    except Exception:  # noqa: BLE001 - the point is that a crash must not look like a verdict
        # Exit 1 is contracted to mean "a pin deviates", so a crash needs its own code. NOT 3:
        # cmd_sweep already returns 3 for a detected foreign write, which is this tool's most
        # safety-critical finding: colliding with it would let a wrapper read "check your
        # working tree now" as "the tool crashed, retry".
        traceback.print_exc()
        return 9
    return result


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
