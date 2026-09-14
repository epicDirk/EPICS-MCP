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

⚠️ The map MUST be recorded with ``COVERAGE_CORE=ctrace``. Where coverage defaults to the
``sys.monitoring`` core, that core disables a location after its FIRST observation, so later
tests covering the same line leave no context row. ⚠️ This said "on Python 3.12+" until GB-34 and
the floor was wrong against the pinned coverage: ``coverage/env.py`` reads
``SYSMON_DEFAULT = CPYTHON and PYVERSION >= (3, 14)``, so 3.12 and 3.13 record a C-tracer map
either way. Set the variable regardless; the interpreter is a configuration choice that moves under
this recipe. Measured on this repository, both maps recorded
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
import functools
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
_SERVICES = _REPO / "src" / "epics_mcp" / "services"
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


# ⛔ THE THREE CACHES BELOW ARE KEYED ON A DIRECTORY, AND A BARE ``functools.cache`` IS THE
# FORBIDDEN SIMPLIFICATION. Three tests in tests/test_guard_audit_cli.py replace a module constant
# to audit a STAND-IN tree, by NAME rather than by line because a line moves and a name does not,
# and these three references went stale twice inside the push that wrote them:
# ``test_check_notices_a_services_side_that_shrank``,
# ``test_check_refuses_an_empty_services_side_rather_than_agreeing`` and
# ``test_sweep_refuses_an_empty_services_side_before_it_opens_the_map`` monkeypatch ``_SERVICES``.
# Since GQ-403 the tests in tests/test_guard_audit_population.py swap ``_TESTS`` the same way, and
# tests/test_guard_audit_sweep.py swaps ``_SERVICES``; each writes its stand-in tree completely
# before the first call reads it, because a tree changed after that call is a stale key.
# A zero-argument cache
# is filled by the first real call, so the stand-in is never read and the tool answers about the
# PREVIOUS tree while reporting on the new one.
#
# Measured rather than reasoned, because the obvious prediction is wrong. Warming a zero-argument
# cache on the real tree and then pointing ``_SERVICES`` at an empty directory does NOT go green: it
# still exits 2, and it prints "6 client module(s) under <the empty directory>, but not one guard
# site in them". Six modules in a directory that holds none. It refused for a reason that is not
# true, which is worse than refusing for the right one and better only by luck: whether the answer
# lands on 0 or on 2 depends on which of the three functions is cached and in what order a caller
# touches them, and no reading of this file makes that predictable. That is the argument for the
# key, not the exit code.
#
# ``cache_clear()`` is not the remedy either. There is no single place to put it (three separate
# tests swap the constant) and a forgotten call fails GREEN. Keying on the directory READ AT CALL
# TIME needs no discipline: the monkeypatch changes the key, so the stand-in tree is recomputed and
# the refusal still fires with its own true diagnosis. The public functions stay zero-argument, so
# no caller changes.
#
# Why at all: ``claiming_tests`` re-globs and re-``ast.parse``s the whole tests/ tree on every call,
# measured 1.79 s before GQ-403 and 2.56 s after it (2026-09-14, the reach walk added), and one run
# of tests/test_guard_audit_cli.py drives it more than twenty times
# (25 on 2026-08-20; the exact figure moves with every test added to that module, so re-derive it
# by wrapping ``_claiming_at`` with a counter rather than trusting this one).
# The cached helpers return IMMUTABLE containers and the wrappers rebuild a fresh mutable one, so a
# caller that mutates the result cannot poison the next call.


@functools.cache
def _client_modules_at(root: Path) -> tuple[Path, ...]:
    return tuple(sorted(root.glob("*_client.py")))


def client_modules() -> list[Path]:
    """The six client modules whose edges this audit covers, in a stable order."""
    return list(_client_modules_at(_SERVICES))


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


@functools.cache
def _targets_at(root: Path) -> tuple[Target, ...]:
    """``enumerate_targets`` for one services directory. Keyed, see the note above the caches."""
    targets: list[Target] = []
    for path in _client_modules_at(root):
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
    return tuple(sorted(targets, key=lambda t: (t.module, t.lineno, t.col)))


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
    return list(_targets_at(_SERVICES))


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
    """``(test file name, test id)``: the pair a claiming test is keyed by.

    A node id is ``<path>::[<class>::]<function>[<param case>]``, and every part but the parameter
    case matters. Comparing with ``endswith("::" + name)`` ignored the file and could never match a
    parametrised id; measured on a real ctrace map, that credited
    ``test_olog_write.py::test_unknown_level_refused`` (a candidate carrying payload vocabulary)
    with the guard-line execution of a SAME-NAMED test in another file, and so hid it from the
    sham-candidate list this audit exists to produce.

    The CLASS is kept for the same reason, one level down (GQ-403): ``test_olog_update.py`` carries
    ``test_refuses_unroundtrippable_attachments`` in two classes, one against the real client and
    one under a helper-installed double, and dropping the class merged them. The test id is
    therefore ``Class::function`` inside a class and ``function`` outside one, exactly as
    ``claiming_tests`` records it. The parameter case is cut at its FIRST bracket, because a bracket
    cannot occur in a class or function name while ``::`` can occur inside a case id.
    """
    path, _sep, rest = node_id.partition("::")
    return Path(path).name, rest.split("[", 1)[0]


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
# (``monkeypatch.setattr("...services.olog_client.OlogClient", _Fake)``) or the module object
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
# Split by what checking them COSTS. The figures in ``PINNED_AST`` follow from this repository's
# own AST and are therefore cheap enough for the ordinary test gate. Those in ``PINNED_COVERAGE``
# are decided by which tests EXECUTED a guard line, which needs a coverage map recorded with
# COVERAGE_CORE=ctrace, so minutes, not milliseconds. ``--check`` without a database verifies the
# cheap ones and NAMES the ones it could not reach; it never reports "OK" for a figure it could not
# reach.
#
# ⚠️ Two counts stood in this paragraph, "the first two" and "the cheap pair", and both were true
# when ``DOUBLES`` and ``EDGE_VOCABULARY`` were the whole AST half. QA-19 added the two
# services-side figures below and neither sentence moved with them. Measured 2026-08-29,
# ``sham --check`` prints "checked 4 pin(s)". The two dicts are NAMED here instead of counted, so
# the next addition moves nothing in this comment.
DOUBLES = "tests under which a client class double is installed (body, helper, fixture, autouse)"
EDGE_VOCABULARY = "of those, carrying payload vocabulary"
NOT_EXECUTING = "of those, never executing a client-edge line"
SHAM_CANDIDATES = "and claiming something about the ANSWERED payload"
# The SERVICES side of the audit, and the reason it is pinned at all (QA-19). Every figure above
# is read off the TEST tree, so ``sham --check`` without a database compared nothing that
# ``_SERVICES`` controls. An audit pointed at a services directory that had shrunk, or moved,
# therefore printed "all agree with the recorded audit" and exited 0 while auditing a fraction of
# the code, or none of it: the two figures it did compare cannot disagree with the services side.
# A population is a claim like any other, so it is recorded like one.
CLIENT_MODULES = "client modules whose edges this audit covers"
GUARD_TARGETS = "mutable guard sites the audit can address in them"

PINNED_AST: dict[str, int] = {
    # 102 -> 286 with GQ-403: the population follows helpers, fixtures and autouse fixtures now, not
    # the test body alone (184 tests more: 151 in test_doctor.py through its autouse fixture, 29 in
    # test_olog_update.py through _install_fake, 4 in test_diagnose.py through
    # _patch_naming_client; none lost). Measured 2026-09-14 with sham --check at EPICS-MCP bde5ea7
    # plus the GQ-403 change. Re-recorded because the CRITERION widened, not because the code under
    # audit changed.
    DOUBLES: 286,
    # 21 -> 91 with GQ-403, the same widening: 70 of the 184 carry payload vocabulary.
    EDGE_VOCABULARY: 91,
    CLIENT_MODULES: 6,
    # 96 -> 98 with GQ-290: the bare-[] branch in ArchiverClient.get_pv_history is one new
    # isinstance call plus one new whole condition. Re-recorded because the SHAPE grew, not
    # because a verdict changed: the new site is executed in both polarities (branch coverage of
    # tests/test_archiver.py over archiver_client.py, 2026-09-06, both arcs taken, no missing
    # branch), so it adds nothing to the unobserved tables.
    GUARD_TARGETS: 98,
}
# 102 -> 261 and 21 -> 85 with GQ-403, from a COVERAGE_CORE=ctrace map recorded 2026-09-14 over the
# suite in CI's shape (the eight engine-coupled modules not collected; 257 test functions execute a
# guard line). The two figures no longer equal their AST twins: 25 claiming tests execute a guard
# line, all of them TestServiceUpdate tests under _install_fake in test_olog_update.py: the service
# path calls the module-level attachment_round_trip in olog_client.py, three of whose guard lines
# it executes, before it reaches the doubled class.
# Six of those 25 carry payload vocabulary, hence 91 - 6 = 85.
PINNED_COVERAGE: dict[str, int] = {NOT_EXECUTING: 261, SHAM_CANDIDATES: 85}
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
    # GQ-403, read 2026-09-14. How: each test read in full by one read-only reader and re-read by
    # an adversarial one set to refute the reading (Opus, twenty-four agents over twelve batches),
    # no reading refuted, three re-read by hand; the per-test readings are in the workspace evidence
    # folder analysis/gq402-gq403-guard-audit-2026-09-14/. None asserts what a doubled client edge
    # does with an answered payload.
    # These three run under ``_patch_naming_client``. Two claim the classification in diagnose
    # and checkers (unregistered, withheld on a transport failure) with a well-formed or a raising
    # double; in the third the double sits behind the url gate and is never constructed, which
    # is what that test asserts. The vocabulary is the word 'answer' in their docstrings, about
    # a query result or a mutant, not about a service response.
    "test_diagnose.py::test_shell_naming_enabled_splits_unregistered",
    "test_diagnose.py::test_shell_naming_gate_empty_url_withholds_no_client",
    "test_diagnose.py::test_shell_naming_unreachable_is_withheld_not_false_typo",
    # BG-DTHR, read 2026-08-19 and judged NOT a sham guard, recorded here because the instruction
    # this list carries is "added names are what has not been read". Its ``_OkClient`` double is
    # the legitimate use this module's own docstring names, keeping a service-layer test off the
    # network, and it stands in for the TRANSPORT probe only: the test then restores the REAL
    # ``_identify`` and the REAL ``rest_get_json`` over the module's autouse stubs, so the chain it
    # actually judges runs unmocked. It claims nothing about what ChannelFinder answered; its
    # assertions are about which report list a throttled plane lands in, the exit category, and the
    # rendered verdict.
    "test_doctor.py::test_a_throttled_run_is_never_reported_as_confirmed",
    "test_doctor.py::test_unverified_plane_does_not_fail_but_is_reported",
    # GQ-403, read 2026-09-14. How: each test read in full by one read-only reader and re-read by
    # an adversarial one set to refute the reading (Opus, twenty-four agents over twelve batches),
    # no reading refuted, three re-read by hand; the per-test readings are in the workspace evidence
    # folder analysis/gq402-gq403-guard-audit-2026-09-14/. None asserts what a doubled client edge
    # does with an answered payload.
    # The sixty below are in the population only through the module's autouse fixture, which
    # replaces doctor.OlogClient for every test. For fifty-six that double is not on the path the
    # test drives at all (failure classification, identity probes, live posture, the write-gate
    # block, CLI arguments); four drive a run_doctor render the double keeps off the network.
    # Their vocabulary ('refus', 'unreadable', 'answer') describes doctor-level logic.
    "test_doctor.py::test_a_credential_in_the_olog_url_never_reaches_the_report",
    "test_doctor.py::test_a_denied_sub_probe_is_never_reported_as_a_confirmed_run",
    "test_doctor.py::test_a_denied_target_does_not_read_as_the_string_the_gate_compared",
    "test_doctor.py::test_a_null_device_audit_sink_is_refused_rather_than_called_writable",
    "test_doctor.py::test_a_pattern_outside_the_declared_spellings_is_not_called_narrow",
    "test_doctor.py::test_a_pattern_that_does_not_compile_is_named_rather_than_shown_as_an_allowlist",
    "test_doctor.py::test_a_permitted_target_can_be_one_the_block_cannot_name",
    "test_doctor.py::test_a_refusal_that_belongs_to_no_plane_is_named_beside_one_that_does",
    "test_doctor.py::test_a_run_that_hard_failed_still_says_part_of_it_was_not_measured",
    "test_doctor.py::test_a_run_that_was_denied_nothing_keeps_its_clean_verdict",
    "test_doctor.py::test_a_set_search_port_changes_the_posture_line",
    "test_doctor.py::test_a_throttled_identity_beacon_is_throttled_not_a_failed_probe",
    "test_doctor.py::test_a_throttled_plane_alone_does_not_get_a_second_refusal_clause",
    "test_doctor.py::test_a_verdict_keeps_a_cause_that_carries_no_credential",
    "test_doctor.py::test_alarm_failed_probe_is_identity_probe_failed",
    "test_doctor.py::test_alarm_missing_elastic_status_falls_back_to_ok",
    "test_doctor.py::test_alarm_unreadable_2xx_body_stays_unverified",
    "test_doctor.py::test_an_entry_the_client_refuses_is_reported_as_dropped",
    "test_doctor.py::test_an_entry_without_a_host_is_named_and_not_resolved",
    "test_doctor.py::test_an_unreachable_verdict_renders_the_message_the_probe_handed_it",
    "test_doctor.py::test_an_unreadable_ca_bundle_is_a_ca_error_on_both_arrival_shapes",
    "test_doctor.py::test_archiver_asks_the_metrics_route_and_refuses_redirects",
    "test_doctor.py::test_archiver_identity_requires_the_identity_field",
    "test_doctor.py::test_archiver_unmeasurable_ingest_stays_ok_but_leaves_a_trace",
    "test_doctor.py::test_audit_sink_that_does_not_exist_yet_is_undecided",
    "test_doctor.py::test_audit_sink_that_is_a_directory_is_refused",
    "test_doctor.py::test_classify_a_throttled_probe_is_throttled_not_unreachable",
    "test_doctor.py::test_classify_retry_error_is_api_error",
    "test_doctor.py::test_cli_bad_arg_exits_two",
    "test_doctor.py::test_cli_nonpositive_timeout_is_usage_error",
    "test_doctor.py::test_cli_verdict_with_nothing_configured_claims_no_identity",
    "test_doctor.py::test_every_problem_status_names_a_remedy",
    "test_doctor.py::test_every_rest_plane_is_actually_identity_probed",
    "test_doctor.py::test_identity_failed_probe_is_identity_probe_failed",
    "test_doctor.py::test_identity_of_a_different_known_service_is_unverified_with_the_name",
    "test_doctor.py::test_identity_unreadable_2xx_body_stays_unverified",
    "test_doctor.py::test_identity_unreadable_2xx_raw_valueerror_stays_unverified",
    "test_doctor.py::test_live_posture_rejects_off_spellings_pvxs_does_not_parse",
    "test_doctor.py::test_naming_identifies_via_its_swagger_contract",
    "test_doctor.py::test_no_configured_value_can_forge_a_line_anywhere_in_the_report",
    "test_doctor.py::test_no_plane_verdict_echoes_a_credential_its_exception_still_carries",
    "test_doctor.py::test_reads_denied_counts_only_this_runs_own_refusals",
    "test_doctor.py::test_retrieval_url_without_archiver_url_is_a_config_error",
    "test_doctor.py::test_run_doctor_closes_verification_complete_on_a_denied_sub_probe",
    "test_doctor.py::test_the_api_error_remedy_does_not_send_the_retrieval_plane_to_mgmt",
    "test_doctor.py::test_the_audit_probe_raises_nothing_on_an_unusable_path",
    "test_doctor.py::test_the_block_is_built_without_constructing_either_gate",
    "test_doctor.py::test_the_fallback_finding_names_the_variable_that_would_help",
    "test_doctor.py::test_the_fallback_note_stays_out_where_it_would_mislead",
    "test_doctor.py::test_the_installation_block_renders_where_the_reader_sees_it",
    "test_doctor.py::test_the_olog_target_rebuild_keeps_the_address_and_drops_a_plain_credential",
    "test_doctor.py::test_the_port_variable_goes_through_the_clients_arithmetic_too",
    "test_doctor.py::test_the_probe_refuses_a_non_regular_file_without_opening_it",
    "test_doctor.py::test_the_probe_reports_an_unusable_path_from_the_first_guard",
    "test_doctor.py::test_the_render_says_an_empty_pattern_refuses_the_start",
    "test_doctor.py::test_the_render_shows_where_an_armed_gate_can_write",
    "test_doctor.py::test_the_reported_logbooks_are_the_set_the_real_olog_gate_enforces",
    "test_doctor.py::test_the_reported_reach_violations_decide_whether_the_real_gate_can_be_built",
    "test_doctor.py::test_the_throttle_arm_outranks_every_predicate_that_reads_a_cause",
    "test_doctor.py::test_throttled_is_inconclusive_by_decision_and_a_subset_of_it",
    "test_olog.py::test_empty_page_past_the_end_is_not_annotated",
    "test_olog.py::test_list_log_levels_splits_outage_from_bad_answer",
    "test_olog.py::test_note_on_a_mixed_or_list_does_not_generalise",
    "test_olog.py::test_search_bad_time_is_not_a_connection_error",
    "test_olog.py::test_unreadable_levels_lookup_says_so_and_keeps_the_result",
    # GQ-403, read 2026-09-14. How: each test read in full by one read-only reader and re-read by
    # an adversarial one set to refute the reading (Opus, twenty-four agents over twelve batches),
    # no reading refuted, three re-read by hand; the per-test readings are in the workspace evidence
    # folder analysis/gq402-gq403-guard-audit-2026-09-14/. None asserts what a doubled client edge
    # does with an answered payload.
    # Refused by the service's own input check before any client call; the test asserts that the
    # capture double recorded no call at all.
    "test_olog_update.py::TestServiceUpdate::test_empty_title_refused",
    # The same four tests as before GQ-403, now CLASS-qualified because the identity keeps the
    # class (see ``test_identity``); read before and not re-judged.
    "test_olog_write.py::TestCreateLevelVocabulary::test_bad_level_does_not_burn_a_rate_token",
    "test_olog_write.py::TestCreateLevelVocabulary::test_blank_level_refused",
    "test_olog_write.py::TestCreateLevelVocabulary::test_unknown_level_refused",
    "test_olog_write.py::TestToolOrchestration::test_reply_tool_threads_and_bad_id_is_400",
    "test_write_gate_contract.py::test_round_trip_write_is_gate_denied_before_any_read",
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
# which this file's own docstring invites, reddens EDGE_VOCABULARY but leaves the verdict, and the
# coverage-dependent ``PINNED_COVERAGE[SHAM_CANDIDATES]``, to be re-judged by a person. That figure
# was spelled as a VALUE here until [GQ-132], and the value said 20 while the pin above said 21:
# naming the key instead cannot go stale, because the key is what moves with it. ⚠️ The first
# version of that repair wrote 'the pin twenty lines up', a fresh hand-typed figure inside a
# sentence about hand-typed figures, and it was wrong by 38.
UNPINNED_VERDICT = "no sham guard found BY THIS FILTER: a judgement about a list, not a count"


def claiming_tests() -> dict[str, list[tuple[str, bool]]]:
    """``{test file: [(test id, edge-claim)]}`` for tests that run under a client CLASS double.

    The primary signal is mechanical and per TEST: a ``setattr`` that replaces a client class is
    installed for that test, so the real client edge is out of its path. What counts as installed
    follows what pytest and Python actually do (GQ-403), and the routes are:

    * the test's own body;
    * a module function the test, or anything it reaches, calls by bare name, and a method of the
      test's own class called through ``self.``, transitively;
    * a fixture the test or one of its fixtures requests by PARAMETER, resolved in the test's
      class, then its module, then ``conftest.py`` of the same directory;
    * every autouse fixture of ``conftest.py``, of the module, and of the test's own class.

    Until GQ-403 only the body counted, and this docstring named the blind spot: three helpers and
    one autouse fixture installed doubles nothing saw (``_install_fake`` in ``test_olog_update.py``,
    ``_patch_naming_client`` in ``test_diagnose.py``, ``_wire_starved_archiver`` and the autouse
    ``_identity_never_touches_the_network`` in ``test_doctor.py``). ``claiming_routes`` names the
    route per test, because for most of the doctor module the autouse fixture is the only one, and
    a reader of the candidate list cannot see that from a test's own text.

    A per-FILE signal was tried first and rejected, and following an autouse fixture does not bring
    it back. That signal marked every test in a file that doubled a client ANYWHERE, a heuristic
    that swept in schema and CLI tests which never ran under one; an autouse fixture installs its
    double for every test in its scope as a fact of the run. The price is stated rather than
    hidden: one autouse fixture puts a whole module into the population.

    The test id is ``Class::function`` for a test in a class and ``function`` otherwise, and the
    class is not decoration. ``test_olog_update.py`` defines
    ``test_refuses_unroundtrippable_attachments`` twice, once against the real client and once
    under ``_install_fake``. Keyed by function name alone the two are ONE identity, so whatever the
    coverage map records for either is credited to both, and a test that executes no guard line
    leaves the candidate list on its twin's execution. Measured on 2026-09-14 both twins happen to
    execute the same three guard lines, so no figure moved here; the stand-in test in
    tests/test_guard_audit_population.py pins the case where only one does. ``test_identity`` keeps
    the class for the same reason.

    The second element flags payload vocabulary (``unreadable``, ``malformed``, ``_raises`` ...),
    read from the TEST's own name and docstring, never from a helper or fixture it reaches. That
    distinguishes "claims something about what the service ANSWERED" (the sham-guard candidates)
    from a double used merely to keep a service-layer test off the network, which is a legitimate
    use of the same tool. It is a filter for reading order, not a verdict.

    ``_boom`` doubles are negative controls rather than sham guards (they assert that no client is
    ever built), so a test stays out only when EVERY double in its reach is one. A ``_boom`` in the
    body does not exclude a test that an autouse fixture puts under a real double.

    Known blind spots, measured on this tree on 2026-09-14 and stated rather than implied:
    ``unittest.mock.patch`` and ``patch.object`` (one client double in the tree, in
    ``test_checkers.py``, and it is a ``_Boom``); a helper imported from ANOTHER module
    (``tests/wire_tools.py`` installs none); the ``usefixtures`` marker (four modules, all
    requesting ``loopback_write_env``, which installs none); a ``setattr`` whose target is an
    f-string (three in ``test_doctor.py``, whose tests are counted anyway through that module's
    autouse fixture); a helper passed as a VALUE instead of being called
    (``pytest.param(_served_404, ...)``); and a ``_boom`` handed to a helper through a parameter,
    which ``_is_boom_double`` reads as the parameter's name, not as what the caller passed.

    The population is deliberately NOT written down as a figure. It is a count of the current tree
    that nothing re-runs, and the previous wording carried the very error this audit exists to
    find: it said 21, taken from a grep whose match set includes the ``def`` line.

    The docstring IS read, deliberately, but only for ``_EDGE_CLAIM``: a test whose prose says "the
    service answered and we could not read it" states the edge claim in words its name does not
    carry. What must never come from prose is whether a double was INSTALLED: see
    ``class_double_calls``.
    """
    return {
        file: [(test_id, edge) for test_id, edge, _routes in entries]
        for file, entries in _claiming_at(_TESTS)
    }


def claiming_routes() -> dict[tuple[str, str], tuple[str, ...]]:
    """``{(test file, test id): routes}``: how each claiming test came to run under its double.

    A route is ``body``, ``helper <name>``, ``fixture <name>`` or ``autouse <name>``, named by the
    FIRST hop from the test: a helper that calls another helper is reported under the first one.
    """
    return {
        (file, test_id): routes
        for file, entries in _claiming_at(_TESTS)
        for test_id, _edge, routes in entries
    }


_AnyFunction = ast.FunctionDef | ast.AsyncFunctionDef


@dataclass(frozen=True)
class _Scope:
    """One namespace a test can reach into: ``conftest.py``, its module, or its class.

    ``functions`` holds what a CALL at that level resolves to (module functions for a module and
    for ``conftest.py``, methods for a class), ``fixtures`` what a parameter resolves to, by the
    name pytest registers. ``parent`` is the next level out, ``None`` above ``conftest.py``.
    """

    kind: str  # conftest | module | class
    functions: dict[str, _AnyFunction]
    fixtures: dict[str, _AnyFunction]
    autouse: tuple[str, ...]
    parent: _Scope | None


def _fixture_registration(function: _AnyFunction) -> tuple[str, bool] | None:
    """``(name pytest registers, autouse)`` when *function* is a fixture, else ``None``."""
    for decorator in function.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        named_fixture = (isinstance(target, ast.Attribute) and target.attr == "fixture") or (
            isinstance(target, ast.Name) and target.id == "fixture"
        )
        if not named_fixture:
            continue
        name, autouse = function.name, False
        if isinstance(decorator, ast.Call):
            for keyword in decorator.keywords:
                if keyword.arg == "autouse" and isinstance(keyword.value, ast.Constant):
                    autouse = keyword.value.value is True
                elif keyword.arg == "name":
                    name = _string_constant(keyword.value) or name
        return name, autouse
    return None


def _scope_of(body: list[ast.stmt], kind: str, parent: _Scope | None) -> _Scope:
    """The functions and fixtures defined directly in *body*, never in a nested block."""
    functions: dict[str, _AnyFunction] = {}
    fixtures: dict[str, _AnyFunction] = {}
    autouse: list[str] = []
    for statement in body:
        if not isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        registration = _fixture_registration(statement)
        if registration is None:
            functions[statement.name] = statement
            continue
        name, is_autouse = registration
        fixtures[name] = statement
        if is_autouse:
            autouse.append(name)
    return _Scope(kind, functions, fixtures, tuple(autouse), parent)


def _locally_bound(function: _AnyFunction) -> frozenset[str]:
    """The names *function* binds itself, each of which shadows a module function of that name.

    Measured case: a test in ``test_doctor.py`` defines its own ``_served_404`` beside the
    module-level helper of the same name, and a call inside it means the local one. The bodies of
    nested functions, lambdas and classes are not entered; only their names bind here.
    """
    arguments = function.args
    bound = {arg.arg for arg in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)}
    bound.update(arg.arg for arg in (arguments.vararg, arguments.kwarg) if arg is not None)
    pending: list[ast.AST] = list(function.body)
    while pending:
        node = pending.pop()
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            bound.add(node.name)
            continue
        if isinstance(node, ast.Lambda):
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        elif isinstance(node, ast.alias):
            bound.add((node.asname or node.name).split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        pending.extend(ast.iter_child_nodes(node))
    return frozenset(bound)


def _callees(function: _AnyFunction, home: _Scope) -> Iterator[tuple[_AnyFunction, _Scope]]:
    """The functions *function* calls that this scan resolves, each with the scope it lives in.

    A bare name resolves to a function of the enclosing MODULE, because Python looks a name up in
    the globals and never in the class from inside a method, unless *function* binds that name
    itself. ``self.<name>`` resolves to a method of the enclosing class.
    """
    module = home.parent if home.kind == "class" else home
    if module is None:
        return
    local = _locally_bound(function)
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        if isinstance(callee, ast.Name) and callee.id not in local:
            target = module.functions.get(callee.id)
            if target is not None:
                yield target, module
        elif (
            home.kind == "class"
            and isinstance(callee, ast.Attribute)
            and isinstance(callee.value, ast.Name)
            and callee.value.id == "self"
        ):
            method = home.functions.get(callee.attr)
            if method is not None:
                yield method, home


def _requested(function: _AnyFunction) -> list[str]:
    """The fixture names a test or a fixture asks for: its parameters, minus ``self``.

    Only ever applied to tests and fixtures. A plain helper's parameters are values its caller
    passes, and measured on this tree eleven live helpers take a parameter named like a fixture.
    """
    arguments = function.args
    names = [arg.arg for arg in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)]
    return [name for name in names if name not in ("self", "cls")]


def _fixture_in_reach(name: str, test_home: _Scope) -> tuple[_AnyFunction, _Scope] | None:
    """Resolve a requested fixture the way pytest does for a test: class, module, conftest."""
    scope: _Scope | None = test_home
    while scope is not None:
        fixture = scope.fixtures.get(name)
        if fixture is not None:
            return fixture, scope
        scope = scope.parent
    return None


def _installs_a_real_double(function: _AnyFunction) -> bool:
    """*function*'s own text installs at least one client class double that is not a ``_boom``."""
    return any(not _is_boom_double(call) for call in class_double_calls(function))


class _ReachWalker:
    """Follows the reach of the tests of ONE module, remembering what it has already walked.

    The memory is not an optimisation for its own sake. The tests of a module call the same
    helpers and share the same autouse fixtures, so walking each reach afresh re-walked those
    bodies once per test: measured on this tree on 2026-09-14, one scan took 6.3 s, where the
    body-only scan before GQ-403 had been measured at 1.79 s.

    Everything is keyed on ``id()`` of AST nodes, which is sound only while their trees are alive.
    That is why there is one walker per module, created and dropped inside the scan, and never a
    module-level cache: a collected tree frees its ids for the next one.
    """

    def __init__(self) -> None:
        self._callees: dict[int, tuple[tuple[_AnyFunction, _Scope], ...]] = {}
        self._real: dict[int, bool] = {}
        self._reaches: dict[tuple[int, int], bool] = {}

    def callees(
        self, function: _AnyFunction, home: _Scope
    ) -> tuple[tuple[_AnyFunction, _Scope], ...]:
        key = id(function)
        if key not in self._callees:
            self._callees[key] = tuple(_callees(function, home))
        return self._callees[key]

    def installs_a_real_double(self, function: _AnyFunction) -> bool:
        key = id(function)
        if key not in self._real:
            self._real[key] = _installs_a_real_double(function)
        return self._real[key]

    def reaches_a_real_double(
        self, start: _AnyFunction, start_home: _Scope, start_is_fixture: bool, test_home: _Scope
    ) -> bool:
        """Follow calls, and a fixture's own requests, from *start* until a real double turns up.

        Visited functions are remembered within one walk, because recursion is ordinary here:
        measured on this tree, ten test helpers call themselves. A walk from a plain helper never
        consults the test's scopes, so its answer is shared by every test; a walk from a fixture
        resolves further fixtures from the TEST's scopes and is remembered per test scope.
        """
        key = (id(start), id(test_home) if start_is_fixture else 0)
        if key in self._reaches:
            return self._reaches[key]
        pending: list[tuple[_AnyFunction, _Scope, bool]] = [(start, start_home, start_is_fixture)]
        seen: set[int] = set()
        found = False
        while pending and not found:
            function, home, is_fixture = pending.pop()
            if id(function) in seen:
                continue
            seen.add(id(function))
            if self.installs_a_real_double(function):
                found = True
                break
            pending.extend((callee, where, False) for callee, where in self.callees(function, home))
            if is_fixture:
                for name in _requested(function):
                    resolved = _fixture_in_reach(name, test_home)
                    if resolved is not None:
                        pending.append((*resolved, True))
        self._reaches[key] = found
        return found

    def routes(self, test: _AnyFunction, home: _Scope) -> tuple[str, ...]:
        """The routes by which *test* runs under a real client class double, sorted, or empty."""
        starts: list[tuple[str, _AnyFunction, _Scope, bool]] = []
        for callee, callee_home in self.callees(test, home):
            prefix = "self." if callee_home is home and home.kind == "class" else ""
            starts.append((f"helper {prefix}{callee.name}", callee, callee_home, False))
        for name in _requested(test):
            resolved = _fixture_in_reach(name, home)
            if resolved is not None:
                starts.append((f"fixture {name}", *resolved, True))
        scope: _Scope | None = home
        while scope is not None:
            starts.extend(
                (f"autouse {name}", scope.fixtures[name], scope, True) for name in scope.autouse
            )
            scope = scope.parent
        routes = {"body"} if self.installs_a_real_double(test) else set()
        for label, function, function_home, is_fixture in starts:
            if label not in routes and self.reaches_a_real_double(
                function, function_home, is_fixture, home
            ):
                routes.add(label)
        return tuple(sorted(routes))


def _tests_in(
    module_body: list[ast.stmt], module: _Scope
) -> Iterator[tuple[str, _AnyFunction, _Scope]]:
    """``(test id, test, home scope)`` for what pytest collects: ``test_*`` functions at module
    level and ``test_*`` methods of ``Test*`` classes (pyproject sets neither ``python_functions``
    nor ``python_classes``, so pytest's defaults are the rule)."""
    for statement in module_body:
        if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef):
            if statement.name.startswith("test_") and _fixture_registration(statement) is None:
                yield statement.name, statement, module
        elif isinstance(statement, ast.ClassDef) and statement.name.startswith("Test"):
            owner = _scope_of(statement.body, "class", module)
            for method in owner.functions.values():
                if method.name.startswith("test_"):
                    yield f"{statement.name}::{method.name}", method, owner


@functools.cache
def _claiming_at(
    root: Path,
) -> tuple[tuple[str, tuple[tuple[str, bool, tuple[str, ...]], ...]], ...]:
    """``claiming_tests`` plus routes, for one test directory. Keyed, see the note above the caches.

    This is the expensive one: it re-``ast.parse``s every test module and ``conftest.py`` and
    follows every test's reach, and the CLI test modules drive it many times per run. How long one
    call takes moves with the tree, so the point is the ORDER, not a number.
    """
    conftest_path = root / "conftest.py"
    conftest = (
        _scope_of(ast.parse(conftest_path.read_bytes()).body, "conftest", None)
        if conftest_path.is_file()
        else None
    )
    found: dict[str, list[tuple[str, bool, tuple[str, ...]]]] = {}
    # pytest's own default is ``test_*.py *_test.py``, and pyproject sets no ``python_files``. A
    # narrower glob here would let a contributor add doubles under a name pytest RUNS and the
    # audited population does not see. Latent today: no such file exists, so this closes a hole
    # rather than fixing a wrong figure.
    for path in sorted(set(root.glob("test_*.py")) | set(root.glob("*_test.py"))):
        body = ast.parse(path.read_bytes()).body
        module = _scope_of(body, "module", conftest)
        walker = _ReachWalker()
        for test_id, test, home in _tests_in(body, module):
            routes = walker.routes(test, home)
            if not routes:
                continue
            claim = bool(_EDGE_CLAIM.search(test.name + (ast.get_docstring(test) or "")))
            found.setdefault(path.name, []).append((test_id, claim, routes))
    return tuple((file, tuple(entries)) for file, entries in found.items())


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
    """The four figures direction B can state WITHOUT a coverage map, from the ASTs alone.

    Two describe the TEST side (which tests install a client double) and two the SERVICES side
    (what there is to audit at all). Both halves are needed, and only the first half existed:
    with the test-side pair alone, ``--check`` compared nothing that ``_SERVICES`` controls and so
    could not contradict an audit pointed at a services directory that had shrunk or moved.
    """
    claiming = claiming_tests()
    return {
        DOUBLES: sum(len(entries) for entries in claiming.values()),
        EDGE_VOCABULARY: sum(1 for entries in claiming.values() for _n, edge in entries if edge),
        CLIENT_MODULES: len(client_modules()),
        GUARD_TARGETS: len(enumerate_targets()),
    }


def services_side_anchor() -> str | None:
    """Name the way the SERVICES side can be broken rather than merely different, else ``None``.

    The pins above already make a SHRUNKEN population deviate, which is what QA-19 asked for. An
    EMPTY one deviates as well, but a deviation says "the recorded audit describes different code
    now" and sends the reader to re-judge a verdict and re-record figures. For a path that resolves
    to nothing that is exactly the wrong instruction: no code changed, the tool lost its subject.
    So emptiness is refused in its own words BEFORE any comparison, the same shape the caller of
    ``load_coverage_map`` already uses for a blind map.
    """
    modules = client_modules()
    if not modules:
        return f"no *_client.py under {_SERVICES}"
    if not enumerate_targets():
        return f"{len(modules)} client module(s) under {_SERVICES}, but not one guard site in them"
    return None


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
    broken = services_side_anchor()
    if broken is not None:
        sys.stderr.write(f"REFUSING TO AUDIT: {broken}. The audit anchor broke.\n")
        return 2
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
        # The ROUTE is printed beside each name because the test's own text often does not show
        # the double at all: for most of test_doctor.py an autouse fixture is the only route, and a
        # reader judging a candidate needs to know what took the client off its path (GQ-403).
        routes = claiming_routes()
        sys.stderr.write(
            f"{measured[DOUBLES]} tests run under a client class double (in their body, or through "
            "a helper, a fixture or an autouse fixture); "
            f"{measured[EDGE_VOCABULARY]} of them carry payload vocabulary and are the list the "
            "verdict was reached by READING, each with its route:\n"
        )
        for filename, entries in sorted(claiming.items()):
            for name, is_edge_claim in entries:
                if is_edge_claim:
                    route = ", ".join(routes[(filename, name)])
                    sys.stderr.write(f"  {filename}::{name}  [{route}]\n")
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
    # ⛔ THE SIZE OF THE MAP IS A FLOOR, NOT A REPORT LINE, AND THE PINS CANNOT SUBSTITUTE FOR IT.
    # Found by an adversarial pass after the CI job was already green, and it is the exact trap this
    # audit exists to name, one level up.
    #
    # Until GQ-403 ``PINNED_COVERAGE[NOT_EXECUTING]`` equalled ``PINNED_AST[DOUBLES]`` and
    # ``PINNED_COVERAGE[SHAM_CANDIDATES]`` equalled ``PINNED_AST[EDGE_VOCABULARY]``: no claiming
    # test executed a guard line, so both coverage figures sat at their arithmetic MAXIMUM.
    # ``measured[NOT_EXECUTING]`` is ``|claiming \\ executing|``, bounded above by ``|claiming|``,
    # so a map seeing FEWER executions could not push it any higher and the comparison stayed green.
    # Since GQ-403 the population follows helpers and fixtures, and some of its tests DO execute a
    # guard line (the figures and their source are at ``PINNED_COVERAGE``), so a map that loses
    # exactly those executions now deviates. That is detection by accident, not a floor: by the same
    # arithmetic, not measured, a map recorded from the one module that carries them keeps them all
    # and agrees again. What the pins can be trusted with is unchanged: a claiming test that STARTS
    # executing a guard line, which is the sham signal and is worth having.
    #
    # Measured 2026-08-20, all three in the CI shape: a full ctrace run covers 246 test functions; a
    # map recorded from ONE module (tests/test_olog.py) covers 30 and still printed
    # "checked 6 pin(s): all agree with the recorded audit", exit 0; a map recorded without
    # COVERAGE_CORE=ctrace on Python 3.14 covers 75. The middle case is the one that matters: a
    # non-empty but nearly worthless map is invisible to the empty-map refusal above AND to the
    # pins, so the job would report success having verified nothing.
    #
    # The floor is opt-in rather than a constant here, because the right value depends on the
    # SELECTION the caller ran, and a caller who deliberately audits a subset is not making a
    # mistake. CI passes it; a developer probing one module does not.
    if args.min_covering_tests is not None and len(executing) < args.min_covering_tests:
        sys.stderr.write(
            f"MAP TOO SMALL: {len(executing)} covering test functions, floor is "
            f"{args.min_covering_tests}. The pins cannot be trusted with this: a map that "
            "measured almost nothing can still agree with them. Re-record over the WHOLE "
            f"suite with per-test contexts.\n{RERUN_COVERAGE}\n"
        )
        return 2
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
        f"tests under a client CLASS double (body, helper, fixture, autouse): {measured[DOUBLES]}\n"
        f"  of those, never executing a client-edge line: {measured[NOT_EXECUTING]}\n"
        f"  and claiming something about the ANSWERED payload: {measured[SHAM_CANDIDATES]}\n\n"
    )
    for entry in sham_edge:
        sys.stderr.write(f"  {entry}\n")
    return 0


def _run_selection(node_ids: list[str], timeout: int) -> tuple[int, str]:
    """Run exactly *node_ids* and return ``(exit code, combined output)``."""
    env = dict(os.environ)
    # OPENBLAS_NUM_THREADS pins the BLAS thread arena: on a many-core machine it can abort
    # the child before pytest prints anything (see CONTRIBUTING.md, the dev-setup section).
    # OMP_NUM_THREADS is not a second spelling of that one. Measured 2026-09-04 (GQ-296): this
    # OpenBLAS is a pthreads build, without USE_OPENMP and with no OpenMP runtime linked, so it
    # reads OMP_NUM_THREADS only as a last fallback and the variable above already decides its
    # arena. It is kept because GQ-296 decided it, and the honest scope of that decision is
    # narrow: what is measured is that the value reaches the child and is obeyed by anything
    # reading it, NOT that it caps a consumer in this child, which nobody has measured. The rule
    # for the measured half needs GNU coreutils, so Git Bash on Windows: `nproc` answers the
    # EXPORTED value and `env -u OMP_NUM_THREADS nproc` the real core count.
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
    # Before the map, because it is the cheaper and the more fundamental brokenness: a sweep with
    # nothing to mutate finds nothing unobserved and reads as a clean bill of health.
    broken = services_side_anchor()
    if broken is not None:
        sys.stderr.write(f"REFUSING TO SWEEP: {broken}. The audit anchor broke.\n")
        return 2
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
    # Every file a restore was REFUSED for, and it decides the exit code on its own. The drift
    # check below looks only at the module of the CURRENT target and only before its next
    # polarity, so a foreign write during the LAST mutant run, or into a module the sweep had
    # already finished, was seen by this closure alone. Measured before this set existed
    # (GQ-402): both cases printed the refusal, then the full result table, and exited 0 with the
    # foreign bytes on disk, a clean bill of health for a tree that needed inspecting by hand.
    refused: set[Path] = set()

    def restore_all() -> None:
        for path, data in originals.items():
            current = path.read_bytes()
            if hashlib.sha256(current).hexdigest() == baseline[path]:
                continue
            if current != written.get(path):
                refused.add(path)
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
        f"  git -C {_REPO} status --porcelain -- src/epics_mcp/services/\n"
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
    if refused:
        # 3, the code the drift abort already uses: whoever reads it must look at the working
        # tree now. The table above still stands, because its verdicts were reached on the sweep's
        # own mutants; what it cannot vouch for is the state it left behind.
        names = ", ".join(sorted(path.name for path in refused))
        sys.stderr.write(f"FOREIGN WRITE: restore refused for {names}; inspect them. Exit 3.\n")
        return 3
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
    sham_parser.add_argument(
        "--min-covering-tests",
        type=int,
        default=None,
        help="refuse (exit 2) a --coverage-db whose map covers fewer than N test functions. The "
        "pins cannot detect a small map, see the note at the check itself; an unattended caller "
        "should pass this, a developer auditing one module should not",
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
