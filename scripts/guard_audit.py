#!/usr/bin/env python3
"""S31 sham-guard audit: are the client-edge guards actually OBSERVED by any test?

The clients in ``services/*_client.py`` validate what a foreign service answered, so an
unreadable payload becomes a loud error instead of a fabricated empty result. A test that
replaces the client CLASS with a double (``monkeypatch.setattr(<mod>, "<X>Client", _Fake)``)
removes every one of those guards from the path while still looking like it protects them — a
SHAM GUARD. See CLAUDE.md, evidence discipline point 8.

Two directions, and they answer different questions:

* ``sham`` (direction B, the roadmap's actual assignment) — which tests CLAIM a guard they never
  execute. Pure analysis over the coverage map; runs no test at all.
* ``sweep`` (direction A) — which guards no test observes, by mutating each one and seeing
  whether anything goes red.

⚠️ The map MUST be recorded with ``COVERAGE_CORE=ctrace``. On Python 3.12+ coverage defaults to
the ``sys.monitoring`` core, which disables a location after its FIRST observation, so later
tests covering the same line leave no context row. Measured on this repository: 72 covering tests
under the default core versus 291 under ctrace, median 2 versus 12. A sweep driven by the default
map would run a quarter of the relevant tests and report false survivors — the audit would itself
be the sham guard it exists to find. Record it with::

    COVERAGE_CORE=ctrace COVERAGE_FILE=<scratch>/cov uv run pytest \\
        --cov=src --cov-branch --cov-context=test

Keep COVERAGE_FILE outside the repo so the checked-in ``.coverage`` is untouched.

Mutation safety, because a mutation writes into the source tree: bytes only (never text mode —
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
        every such pair — measured, 9 of them here — and silently merges two different mutants
        into one row."""
        return f"{self.module}:{self.lineno}:{self.col}-{self.end_lineno}:{self.end_col}"


def client_modules() -> list[Path]:
    """The six client modules whose edges this audit covers, in a stable order."""
    return sorted(_SERVICES.glob("*_client.py"))


def _form_of(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    """Classify a call by the construct that consumes it — a raise guard is not a coercion."""
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
    ``not isinstance(x, dict) or "secs" not in x`` leaves the rest of the condition standing — the
    guard does not disappear, so a green result would mean "this conjunct is unobserved", not
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


def load_coverage_map(db_path: Path) -> dict[tuple[str, int], set[str]]:
    """``{(module, lineno): {test node id}}`` from a ``--cov-context=test`` database.

    Reads the ``arc`` table: with branch coverage on, that is where the data lives — ``line_bits``
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
            context = contexts[int(context_id)].split("|")[0]
            if not context:
                continue
            for lineno in (abs(int(fromno)), abs(int(tono))):
                covering.setdefault((module, lineno), set()).add(context)
        return covering
    finally:
        connection.close()


# A class-level double, in either house form: the dotted string path
# (``monkeypatch.setattr("…services.olog_client.OlogClient", _Fake)``) or the module object
# (``monkeypatch.setattr(checkers, "OlogClient", _Fake)``).
_CLASS_DOUBLE = re.compile(r"setattr\(\s*[^)]*?[\"']?\w*Client[\"']?\s*,")

# Vocabulary that marks a test as being ABOUT a payload the client edge inspects, as opposed to
# a double used merely to keep a service-layer test off the network.
_EDGE_CLAIM = re.compile(
    r"unreadable|payload|malformed|non_dict|without_name|non_string|_raises"
    # Widened after a review found the first list too narrow to carry the "none found" claim:
    # a test whose docstring says "the service ANSWERED and we could not read it" states the
    # edge claim exactly, and none of the words above appear in it.
    r"|answer|respons|reject|refus|invalid|bad_|strict|corrupt|garbage|junk|schema"
)


def claiming_tests() -> dict[str, list[tuple[str, bool]]]:
    """``{test file: [(test name, edge-claim)]}`` for tests that install a client CLASS double.

    The primary signal is mechanical and per-FUNCTION: the test's own body replaces a client
    class, so the real client edge is out of the path for that test. A per-file signal was tried
    first and is useless — it marks every test in a file that doubles a client anywhere, which
    swept in schema and CLI tests that never touch a client.

    The second element flags payload vocabulary (``unreadable``, ``malformed``, ``_raises`` …).
    That distinguishes "claims something about what the service ANSWERED" — the sham-guard
    candidates — from a double used merely to keep a service-layer test off the network, which is
    a legitimate use of the same tool. It is a filter for reading order, not a verdict.

    ``_boom`` doubles are excluded by construction: their point is that nothing runs (they assert
    no client is ever built), so they are negative controls rather than sham guards.

    Known blind spot, stated rather than implied: a double installed by a FIXTURE rather than in
    the test body is not seen here.
    """
    found: dict[str, list[tuple[str, bool]]] = {}
    for path in sorted(_TESTS.glob("test_*.py")):
        text = path.read_text(encoding="utf-8")
        entries: list[tuple[str, bool]] = []
        for node in ast.walk(ast.parse(text)):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if not node.name.startswith("test_"):
                continue
            body = ast.get_source_segment(text, node) or ""
            if not _CLASS_DOUBLE.search(body) or "_boom" in body:
                continue
            claim = bool(_EDGE_CLAIM.search(node.name + (ast.get_docstring(node) or "")))
            entries.append((node.name, claim))
        if entries:
            found[path.name] = entries
    return found


def cmd_targets(_args: argparse.Namespace) -> int:
    """Print the target inventory grouped by form — the population, before any mutation."""
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


def cmd_sham(args: argparse.Namespace) -> int:
    """Direction B: tests that claim a client-edge guard but never execute one."""
    covering = load_coverage_map(Path(args.coverage_db))
    if not covering:
        sys.stderr.write(
            "empty coverage map — was it recorded with --cov-branch --cov-context=test?\n"
        )
        return 2
    # "executes a client edge" means a GUARD line, not merely any line of a client module: a test
    # can drive the client all day without ever reaching the check it claims to protect.
    guard_lines = {(target.module, target.lineno) for target in enumerate_targets()}
    executing: set[str] = {
        test for key, tests in covering.items() if key in guard_lines for test in tests
    }
    sys.stderr.write(f"map: {len(executing)} tests execute at least one client-edge GUARD line\n")
    doubles = 0
    sham_edge: list[str] = []
    sham_other = 0
    for filename, entries in sorted(claiming_tests().items()):
        doubles += len(entries)
        for name, is_edge_claim in entries:
            if any(test.endswith(f"::{name}") for test in executing):
                continue
            if is_edge_claim:
                sham_edge.append(f"{filename}::{name}")
            else:
                sham_other += 1
    sys.stderr.write(
        f"tests installing a client CLASS double in their own body: {doubles}\n"
        f"  of those, never executing a client-edge line: {len(sham_edge) + sham_other}\n"
        f"  and claiming something about the ANSWERED payload: {len(sham_edge)}\n\n"
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
    failure carries the guard's own diagnosis — a TypeError from forcing a check on means the
    happy path crashed, not that anyone observed the refusal.

    ⚠️ Honest limit, measured: for a RAISE-GUARD the enabling polarity makes the condition
    constantly true, so the guard fires on every input and every covering test dies with the
    module's own exception. ``KILLED-DECLARED`` is therefore TAUTOLOGICAL there — it says "a test
    executes this line", not "someone observes the refusal". All 21 raise guards in this estate
    have such a polarity. Only the DISABLING polarity carries information for them.
    """
    # pytest exit codes: 0 pass · 1 tests failed · 2 interrupted · 3 internal error · 4 usage
    # error · 5 no tests collected. Only 1 is evidence about the guard. ⚠️ Everything else used to
    # fall through to the KILLED- buckets, i.e. straight into "the guard is observed" — a renamed
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
        sys.stderr.write("empty coverage map — refusing to sweep against a blind map\n")
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
                    f"what this sweep wrote — a foreign write. Left untouched ON PURPOSE; the "
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
    for name, func in (("sham", cmd_sham), ("sweep", cmd_sweep)):
        child = sub.add_parser(name, help=func.__doc__ or "")
        child.add_argument("--coverage-db", required=True, help="a ctrace --cov-context=test db")
        child.add_argument("--timeout", type=int, default=120, help="seconds per mutant run")
        child.set_defaults(func=func)
    args = parser.parse_args(argv[1:])
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
