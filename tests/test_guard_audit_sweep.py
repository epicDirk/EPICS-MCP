"""S39 / GQ-402: the sweep half of ``scripts/guard_audit.py`` is exercised, not just described.

The sweep is the half that WRITES INTO THE SOURCE TREE: it splices a polarity into each guard site,
runs the covering tests, and restores the file. Until this module, ``splice``, ``classify``,
``cmd_sweep`` with its ``restore_all`` closure, ``_run_selection`` and the real
``load_coverage_map`` ran in no test at all (``tests/test_guard_audit_cli.py`` replaces the map
reader with a lambda at every use). A tool whose mutation path is untested is a sham guard of the
kind it exists to find.

Everything here runs IN-PROCESS and OFFLINE, on a stand-in services tree under ``tmp_path``:

* the tree is a single ``demo_client.py`` carrying the three guard shapes the audit knows (a raise
  guard, a composite condition over two lines that yields a WHOLE-CONDITION target, and a
  comprehension filter), swapped in through ``guard_audit._SERVICES`` exactly as the three tests in
  the CLI module already do. The source sits in a BYTES constant on purpose: ``_claiming_at`` parses
  every test module, and a ``setattr(..., "...Client", ...)`` written out as code anywhere in a
  test's reach, its body, a helper it calls or a fixture, would move ``PINNED_AST[DOUBLES]``;
* the coverage map is a REAL sqlite file written by ``coverage.CoverageData`` itself, never a
  hand-built schema, so the reader is measured against what the pinned coverage version writes;
* ``_run_selection`` is replaced by a fake that never starts a process. It identifies WHICH mutant
  is on disk at call time by comparing the bytes it finds against every splice the sweep can
  produce, which is what turns "the sweep wrote a mutant before running the tests and restored it
  afterwards" from a docstring into an assertion;
* ``guard_audit.atexit`` is replaced by a recorder. Without that, every ``cmd_sweep`` call would
  register a real interpreter-exit hook over files in ``tmp_path``.

The two directory caches in ``guard_audit`` are keyed on the PATH, so each scenario gets its own
subdirectory and writes it completely before the first call reads it; nothing mutates a stand-in
tree after that except the sweep under test and the deliberate foreign writes below.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from coverage import CoverageData

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "scripts"))

import guard_audit  # noqa: E402 - needs sys.path above

# The stand-in client edge. Line numbers matter below and are derived, never typed: the targets
# come out of ``guard_audit.enumerate_targets()`` over this very source. The ``\xc3\xbc`` in the
# last line is a UTF-8 two-byte character placed BEFORE a target on the same line: an AST column
# offset counts bytes, so a splice that decoded to text and indexed characters would land one
# byte early there. Written as an escape so the module itself stays ASCII.
_DEMO_SOURCE = (
    b'"""A stand-in client edge: three guard shapes the audit knows."""\n'
    b"\n"
    b"\n"
    b"class DemoResponseError(Exception):\n"
    b'    """The declared refusal of this edge."""\n'
    b"\n"
    b"\n"
    b"def read_items(payload: object) -> list[str]:\n"
    b"    if not isinstance(payload, dict):\n"
    b'        raise DemoResponseError("payload is not a mapping")\n'
    b"    if (\n"
    b'        not isinstance(payload.get("items"), list)\n'
    b'        or not payload["items"]\n'
    b"    ):\n"
    b'        raise DemoResponseError("items are missing")\n'
    b'    return [item for item in payload["\xc3\xbc"] if isinstance(item, str)]\n'
)
_MODULE = "demo_client.py"
_COVERED_BY = "tests/test_demo.py::test_reads_a_mapping"
_ALSO_COVERED_BY = "tests/test_demo.py::test_parametrised[case-'|']"


def _services_tree(into: Path, modules: dict[str, bytes]) -> Path:
    """Write *modules* into a fresh stand-in services directory and return it."""
    into.mkdir()
    for name, source in modules.items():
        (into / name).write_bytes(source)
    return into


def _targets_of(services: Path) -> list[guard_audit.Target]:
    """The audit's own inventory over *services*, in the order the sweep walks it."""
    return list(guard_audit._targets_at(services))


def _by_form(targets: list[guard_audit.Target], form: str) -> guard_audit.Target:
    found = [target for target in targets if target.form == form]
    assert len(found) == 1, f"the stand-in must carry exactly one {form} target, has {found}"
    return found[0]


def _write_map(db: Path, arcs_by_context: dict[str, dict[str, set[tuple[int, int]]]]) -> Path:
    """A coverage database written by coverage itself, one ``add_arcs`` call per context.

    ``basename`` is mandatory: ``CoverageData()`` without it opens ``<cwd>/.coverage``, which in
    this repository is the working file of the ordinary ``--cov`` run.
    """
    data = CoverageData(basename=str(db))
    for context, arcs in arcs_by_context.items():
        data.set_context(context)
        data.add_arcs(arcs)
    data.write()
    return db


# --- splice ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"], ids=["lf", "crlf"])
def test_splice_replaces_exactly_the_target_span_and_keeps_every_other_byte(
    tmp_path: Path, newline: bytes
) -> None:
    """One target, one span, and every byte outside it is untouched, line endings included.

    The expectation is built INDEPENDENTLY of ``splice``: the target's own source segment is cut
    out of the original with ``bytes.replace`` on a span that occurs once. A test that merely
    compiled the result would pass an off-by-one (``if not iTrue:`` compiles) and a test that
    counted line endings would pass a splice that appended a stray ``\\n`` at the end of the file.
    """
    source = _DEMO_SOURCE.replace(b"\n", newline)
    services = _services_tree(tmp_path / "services", {_MODULE: source})
    targets = _targets_of(services)
    for form in ("RAISE-GUARD", "FILTER"):
        target = _by_form([t for t in targets if not t.composite], form)
        lines = source.split(newline)
        segment = lines[target.lineno - 1][target.col : target.end_col]
        assert segment.startswith(b"isinstance("), segment
        assert source.count(segment) == 1, "the span must be unique for the expectation to hold"
        expected = source.replace(segment, b"True", 1)

        spliced = guard_audit.splice(source, target, b"True")

        assert spliced == expected, f"{form}: the splice changed bytes outside the target span"
        compile(spliced, _MODULE, "exec")


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"], ids=["lf", "crlf"])
def test_splice_collapses_a_multi_line_whole_condition_onto_one_line(
    tmp_path: Path, newline: bytes
) -> None:
    """A WHOLE-CONDITION target spans two lines; the splice joins them and keeps the rest."""
    source = _DEMO_SOURCE.replace(b"\n", newline)
    services = _services_tree(tmp_path / "services", {_MODULE: source})
    whole = _by_form(_targets_of(services), "WHOLE-CONDITION")
    assert whole.end_lineno == whole.lineno + 1, "the stand-in condition must span two lines"
    lines = source.split(newline)
    head = lines[whole.lineno - 1][: whole.col]
    tail = lines[whole.end_lineno - 1][whole.end_col :]
    expected = newline.join(
        [*lines[: whole.lineno - 1], head + b"False" + tail, *lines[whole.end_lineno :]]
    )

    spliced = guard_audit.splice(source, whole, b"False")

    assert spliced == expected
    compile(spliced, _MODULE, "exec")
    assert spliced.count(newline) == source.count(newline) - 1, "exactly one line was joined"


def test_splice_addresses_the_target_by_byte_offset_not_by_character(tmp_path: Path) -> None:
    """The FILTER target sits behind a two-byte character on its own line.

    ``ast`` reports ``col_offset`` in UTF-8 bytes. A splice that decoded the line and indexed
    characters would start one position early and eat the ``i`` of ``isinstance``; the byte-exact
    expectation of the first test would already catch that, this one names the reason.
    """
    services = _services_tree(tmp_path / "services", {_MODULE: _DEMO_SOURCE})
    target = _by_form(_targets_of(services), "FILTER")
    line = _DEMO_SOURCE.split(b"\n")[target.lineno - 1]
    assert b"\xc3\xbc" in line[: target.col], "the two-byte character must precede the target"
    assert len(line[: target.col].decode()) < target.col, "byte and character offsets differ"

    spliced = guard_audit.splice(_DEMO_SOURCE, target, b"True")

    assert b"if True]" in spliced
    assert b"iTrue" not in spliced and b"isinstance(item, str)" not in spliced


# --- load_coverage_map ----------------------------------------------------------------------------


def test_load_coverage_map_reads_arcs_per_test_context_from_a_real_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reader over a database coverage wrote: filter, phase, pipe, negative arcs, no context.

    Five properties, each with the row that would go wrong without it:

    * only the client modules ``client_modules()`` names are read: ``other.py`` carries arcs and
      must not appear;
    * the phase suffix (``|run``, ``|setup``, ``|teardown``) is removed and a pipe INSIDE the node
      id survives, so ``test_parametrised[case-'|']`` keeps its name (splitting on the first pipe
      truncated six real ids, see ``_node_id_of``);
    * entry and exit arcs carry NEGATIVE line numbers and are counted on their absolute line, so
      an arc ``(-1, 9)`` covers lines 1 and 9. That is the reader's own semantics and it is pinned
      as a decision, not as coverage's: coverage marks the entry or exit of a code object starting
      at line N with ``-N`` and does not count N as executed. For a guard line it makes no
      difference, because a code object can only start on a guard line where the line itself is
      reached (a lambda or a generator expression created on it); a guard line is never the first
      line of a function entered from elsewhere (QA of GQ-403);
    * the empty default context, which coverage records for code run outside any test, is dropped;
    * the map is keyed by module NAME, whatever directory the database recorded.
    """
    services = _services_tree(tmp_path / "services", {_MODULE: _DEMO_SOURCE})
    monkeypatch.setattr(guard_audit, "_SERVICES", services)
    module = str(services / _MODULE)
    other = str(services / "other.py")
    db = _write_map(
        tmp_path / "cov",
        {
            f"{_COVERED_BY}|run": {module: {(-1, 9), (9, 10)}, other: {(1, 2)}},
            f"{_ALSO_COVERED_BY}|teardown": {module: {(9, 11), (11, 12)}},
            "tests/test_demo.py::test_setup_phase|setup": {module: {(16, -8)}},
            "": {module: {(4, 5)}},
        },
    )

    covering = guard_audit.load_coverage_map(db)

    assert set(covering) == {
        (_MODULE, 1),
        (_MODULE, 9),
        (_MODULE, 10),
        (_MODULE, 11),
        (_MODULE, 12),
        (_MODULE, 16),
        (_MODULE, 8),
    }
    assert covering[(_MODULE, 9)] == {_COVERED_BY, _ALSO_COVERED_BY}
    assert covering[(_MODULE, 1)] == {_COVERED_BY}, "the entry arc counts on its absolute line"
    assert covering[(_MODULE, 8)] == {"tests/test_demo.py::test_setup_phase"}
    assert covering[(_MODULE, 12)] == {_ALSO_COVERED_BY}, "the embedded pipe must survive"
    assert not any(key[0] == "other.py" for key in covering), "not a client module"


def test_a_line_coverage_database_yields_the_empty_map_the_callers_refuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The trap the reader's docstring names, pinned: without ``--cov-branch`` the data sits in
    ``line_bits`` and the ``arc`` table is empty, so the map is silently EMPTY. Both commands refuse
    an empty map with exit 2 rather than reporting on it; the sweep is driven here because it is
    the one that would otherwise write mutants against a blind map."""
    services = _services_tree(tmp_path / "services", {_MODULE: _DEMO_SOURCE})
    monkeypatch.setattr(guard_audit, "_SERVICES", services)
    recorder = _AtexitRecorder()
    monkeypatch.setattr(guard_audit, "atexit", recorder)
    db = tmp_path / "lines-only"
    data = CoverageData(basename=str(db))
    data.set_context(f"{_COVERED_BY}|run")
    data.add_lines({str(services / _MODULE): {9, 10, 16}})
    data.write()

    assert guard_audit.load_coverage_map(db) == {}
    runner = _FakeRunner(services, _DEMO_SOURCE)
    monkeypatch.setattr(guard_audit, "_run_selection", runner)
    exit_code = guard_audit.main(["guard_audit.py", "sweep", "--coverage-db", str(db)])

    assert exit_code == 2
    assert runner.calls == [], "nothing may be run against a blind map"
    assert recorder.hooks == [], "the restore hook is registered only once a sweep really starts"
    assert (services / _MODULE).read_bytes() == _DEMO_SOURCE


# --- classify -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exit_code", "output", "verdict"),
    [
        (2, "interrupted", "INVALID-SELECTION"),
        (3, "internal error", "INVALID-SELECTION"),
        (4, "usage error: file not found", "INVALID-SELECTION"),
        (5, "no tests ran", "INVALID-SELECTION"),
        (0, "3 passed in 0.04s", "SURVIVED"),
        (0, "...\n3 passed, 1 deselected in 0.04s", "SURVIVED"),
        (0, "2 passed, 1 deselected in 0.04s", "INVALID-SELECTION"),
        (0, "13 passed in 0.04s", "INVALID-SELECTION"),
        (0, " 3 passed in 0.04s", "INVALID-SELECTION"),
        (0, "", "INVALID-SELECTION"),
        (1, "E   DemoResponseError: payload is not a mapping", "KILLED-DECLARED"),
        (1, "E   requests.exceptions.ConnectionError: refused", "KILLED-DECLARED"),
        (1, "E   ToolError: bad", "KILLED-DECLARED"),
        (1, "E   TypeError: 'NoneType' object is not subscriptable", "KILLED-COLLATERAL"),
        (1, "E   ValueError: invalid literal", "KILLED-COLLATERAL"),
        (1, "E   TypeError: crashed\nE   EpicsError: the guard spoke", "KILLED-DECLARED"),
        (1, "E   assert 1 == 2", "KILLED-ASSERTION"),
        (1, "E   ConnectionRefusedError: not a declared kind", "KILLED-ASSERTION"),
        (1, "", "KILLED-ASSERTION"),
    ],
)
def test_classify_judges_by_failure_kind_not_by_exit_code(
    exit_code: int, output: str, verdict: str
) -> None:
    """The verdict table, one row per branch the docstring of ``classify`` describes.

    The ``13 passed`` and ``2 passed`` rows pin the ``\\n`` anchor: a run that collected the wrong
    selection must not read as SURVIVED because its count happens to END with the expected digit.
    The mixed row pins the precedence: a declared exception in the output is the guard's own
    diagnosis and wins over a collateral TypeError beside it.
    """
    assert guard_audit.classify(exit_code, output, 3) == verdict


# --- cmd_sweep, dry run ---------------------------------------------------------------------------


class _AtexitRecorder:
    """Stands in for the ``atexit`` module: records the hook instead of arming the interpreter."""

    def __init__(self) -> None:
        self.hooks: list[Callable[[], None]] = []

    def register(self, hook: Callable[[], None]) -> Callable[[], None]:
        self.hooks.append(hook)
        return hook


_Answer = Callable[[str, bytes, int], tuple[int, str]]


def _survives(_key: str, _polarity: bytes, expected_passed: int) -> tuple[int, str]:
    return 0, f"{expected_passed} passed in 0.01s"


class _FakeRunner:
    """Stands in for ``_run_selection``: never starts a process, identifies the mutant on disk.

    Before the sweep runs, every splice it can produce over the stand-in tree is computed once;
    at each call the bytes found on disk are looked up in that table, so the record says which
    target and which polarity the sweep had written at the moment it asked for the tests. A call
    that finds NO mutant on disk, or one that is not in the table, fails the test right there:
    that is the assertion "the mutant is written before the run".

    ``foreign_write`` optionally stamps foreign bytes into a named module during call number
    ``foreign_on_call`` (1-based), which is how the restore refusal paths are driven.
    """

    def __init__(
        self,
        services: Path,
        source: bytes,
        answer: _Answer = _survives,
        *,
        foreign_on_call: int | None = None,
        foreign_module: str = _MODULE,
    ) -> None:
        self.services = services
        self.originals = {path: path.read_bytes() for path in sorted(services.glob("*_client.py"))}
        self.mutants: dict[bytes, tuple[Path, str, bytes]] = {}
        for target in guard_audit._targets_at(services):
            path = services / target.module
            for polarity in (b"True", b"False"):
                mutant = guard_audit.splice(self.originals[path], target, polarity)
                self.mutants[mutant] = (path, target.key, polarity)
        self.answer = answer
        self.foreign_on_call = foreign_on_call
        self.foreign_module = foreign_module
        self.calls: list[tuple[str, bytes, tuple[str, ...], int]] = []
        self.foreign_bytes = source + b"# a parallel window wrote this line\n"

    def __call__(self, node_ids: list[str], timeout: int) -> tuple[int, str]:
        found = [
            (path, *self.mutants[data])
            for path, data in ((path, path.read_bytes()) for path in self.originals)
            if data in self.mutants
        ]
        assert len(found) == 1, f"exactly one mutant must be on disk at run time, found {found}"
        path, mutant_of, key, polarity = found[0]
        assert path == mutant_of, f"the mutant of {mutant_of.name} was written into {path.name}"
        self.calls.append((key, polarity, tuple(node_ids), timeout))
        if self.foreign_on_call == len(self.calls):
            (self.services / self.foreign_module).write_bytes(self.foreign_bytes)
        return self.answer(key, polarity, len(node_ids))


def _sweep_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, modules: dict[str, bytes]
) -> tuple[Path, Path, _AtexitRecorder]:
    """A stand-in tree, a map that covers every target but the FILTER, and the atexit recorder.

    The FILTER guard is left without a covering test on purpose, so the run carries one
    NEVER-EXECUTED row; the raise guard and both composite targets share their lines with a
    covering test each.
    """
    services = _services_tree(tmp_path / "services", modules)
    monkeypatch.setattr(guard_audit, "_SERVICES", services)
    recorder = _AtexitRecorder()
    monkeypatch.setattr(guard_audit, "atexit", recorder)
    arcs: dict[str, dict[str, set[tuple[int, int]]]] = {f"{_COVERED_BY}|run": {}}
    arcs[f"{_ALSO_COVERED_BY}|run"] = {}
    for target in guard_audit._targets_at(services):
        if target.form == "FILTER":
            continue
        module = str(services / target.module)
        arcs[f"{_COVERED_BY}|run"].setdefault(module, set()).add((target.lineno, target.lineno + 1))
        if target.form == "RAISE-GUARD" and not target.composite:
            arcs[f"{_ALSO_COVERED_BY}|run"].setdefault(module, set()).add((-1, target.lineno))
    db = _write_map(tmp_path / "cov", {ctx: a for ctx, a in arcs.items() if a})
    return services, db, recorder


def _sweep(db: Path, timeout: int = 7) -> int:
    return guard_audit.main(
        ["guard_audit.py", "sweep", "--coverage-db", str(db), "--timeout", str(timeout)]
    )


def _expected_sequence(services: Path) -> list[tuple[str, bytes]]:
    """The (key, polarity) order the sweep walks: inventory order, True before False."""
    return [
        (target.key, polarity)
        for target in guard_audit._targets_at(services)
        if target.form != "FILTER"
        for polarity in (b"True", b"False")
    ]


def test_a_clean_sweep_writes_each_mutant_runs_its_tests_restores_and_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit 0, one row per target and polarity, and the file is the original again.

    The fake runner is the witness: it records the mutant it FOUND ON DISK at each call, so the
    sequence below proves the sweep wrote every polarity of every covered target in inventory
    order and asked for exactly the covering tests, sorted, with the timeout from the command
    line. The verdicts per row come from the canned answers, one per verdict kind, so the report
    is checked for the classification it printed, not only for its shape.
    """
    services, db, recorder = _sweep_fixture(tmp_path, monkeypatch, {_MODULE: _DEMO_SOURCE})
    targets = guard_audit._targets_at(services)
    raise_guard = _by_form([t for t in targets if not t.composite], "RAISE-GUARD")
    whole = _by_form(list(targets), "WHOLE-CONDITION")

    def answer(key: str, polarity: bytes, expected_passed: int) -> tuple[int, str]:
        if key == raise_guard.key and polarity == b"True":
            return 1, "E   DemoResponseError: payload is not a mapping"
        if key == whole.key and polarity == b"True":
            return 1, "E   TypeError: 'NoneType' object is not subscriptable"
        if key == whole.key:
            return 1, "E   assert [] == ['a']"
        return 0, f"{expected_passed} passed in 0.01s"

    runner = _FakeRunner(services, _DEMO_SOURCE, answer)
    monkeypatch.setattr(guard_audit, "_run_selection", runner)

    assert _sweep(db, timeout=7) == 0
    reported = capsys.readouterr().err

    assert [(key, polarity) for key, polarity, _ids, _t in runner.calls] == _expected_sequence(
        services
    )
    assert all(timeout == 7 for _k, _p, _ids, timeout in runner.calls), "the CLI timeout"
    raise_calls = [ids for key, _p, ids, _t in runner.calls if key == raise_guard.key]
    assert raise_calls == [(_ALSO_COVERED_BY, _COVERED_BY)] * 2, "sorted covering node ids"
    assert (services / _MODULE).read_bytes() == _DEMO_SOURCE, "restored after the last mutant"
    assert "restore command if this run is killed" in reported
    assert "status --porcelain" in reported
    rows = {
        (raise_guard.key, "RAISE-GUARD", "True", "KILLED-DECLARED"),
        (raise_guard.key, "RAISE-GUARD", "False", "SURVIVED"),
        (whole.key, "WHOLE-CONDITION", "True", "KILLED-COLLATERAL"),
        (whole.key, "WHOLE-CONDITION", "False", "KILLED-ASSERTION"),
        (_by_form(list(targets), "FILTER").key, "FILTER", "n/a", "NEVER-EXECUTED"),
    }
    for key, form, applied, verdict in rows:
        pattern = rf"^{re.escape(key)}\s+{form}\s+{re.escape(applied)}\s+{verdict}$"
        assert re.search(pattern, reported, re.MULTILINE), f"missing row: {pattern!r}\n{reported}"
    assert len(recorder.hooks) == 1, "one restore hook per sweep"
    recorder.hooks[0]()
    assert (services / _MODULE).read_bytes() == _DEMO_SOURCE, "the hook is a no-op afterwards"
    assert "REFUSING" not in reported


def test_a_foreign_write_during_an_early_run_is_refused_left_alone_and_exits_three(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The restore refusal, then the drift check of the next polarity: exit 3, nothing clobbered.

    Order of events, which is what the assertions read back: mutant written, fake runs and stamps
    foreign bytes into the same module, the inner ``finally`` calls ``restore_all`` which finds
    bytes that are neither the baseline nor what the sweep wrote and REFUSES, the next polarity's
    sha check sees the drift and aborts with 3. The foreign bytes must still be on disk at the end:
    stamping the original over a parallel window's edit is the one thing the sweep must never do.
    """
    services, db, recorder = _sweep_fixture(tmp_path, monkeypatch, {_MODULE: _DEMO_SOURCE})
    runner = _FakeRunner(services, _DEMO_SOURCE, foreign_on_call=1)
    monkeypatch.setattr(guard_audit, "_run_selection", runner)

    assert _sweep(db) == 3
    reported = capsys.readouterr().err

    assert len(runner.calls) == 1, "the sweep must stop at the drift, not run the next polarity"
    assert f"REFUSING to restore {_MODULE}" in reported
    assert f"DRIFT in {_MODULE}: foreign write. Aborting." in reported
    assert (services / _MODULE).read_bytes() == runner.foreign_bytes, "left untouched ON PURPOSE"
    recorder.hooks[0]()
    assert (services / _MODULE).read_bytes() == runner.foreign_bytes, "and the exit hook agrees"


def test_a_foreign_write_during_the_last_run_still_exits_three(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No next polarity is left to notice the drift, so the refusal itself has to decide the exit.

    Measured before the fix: the sweep refused the restore on stderr, printed the FULL result
    table and returned 0, with the foreign bytes on disk. A caller reading the exit code would
    have taken a clean bill of health for a tree it had to inspect by hand.
    """
    services, db, _recorder = _sweep_fixture(tmp_path, monkeypatch, {_MODULE: _DEMO_SOURCE})
    last = len(_expected_sequence(services))
    runner = _FakeRunner(services, _DEMO_SOURCE, foreign_on_call=last)
    monkeypatch.setattr(guard_audit, "_run_selection", runner)

    assert _sweep(db) == 3
    reported = capsys.readouterr().err

    assert len(runner.calls) == last
    assert f"REFUSING to restore {_MODULE}" in reported
    assert f"FOREIGN WRITE: restore refused for {_MODULE}" in reported
    assert (services / _MODULE).read_bytes() == runner.foreign_bytes


def test_a_foreign_write_into_a_module_already_swept_still_exits_three(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two modules: the write lands in the first while the second is being mutated.

    The per-polarity drift check looks only at the module of the CURRENT target, so the first
    module's foreign bytes are seen by ``restore_all`` alone. With six client modules in the real
    tree this is the likelier shape of a collision, and before the fix it also exited 0.
    """
    # The second module differs by a trailing comment, so no mutant of one is byte-equal to a
    # mutant of the other and the fake can tell them apart.
    modules = {"a_client.py": _DEMO_SOURCE, "b_client.py": _DEMO_SOURCE + b"# second module\n"}
    services, db, _recorder = _sweep_fixture(tmp_path, monkeypatch, modules)
    sequence = _expected_sequence(services)
    first_of_b = next(i for i, (key, _p) in enumerate(sequence, start=1) if key.startswith("b_"))
    runner = _FakeRunner(
        services, _DEMO_SOURCE, foreign_on_call=first_of_b, foreign_module="a_client.py"
    )
    monkeypatch.setattr(guard_audit, "_run_selection", runner)

    assert _sweep(db) == 3
    reported = capsys.readouterr().err

    assert "REFUSING to restore a_client.py" in reported
    assert "FOREIGN WRITE: restore refused for a_client.py;" in reported
    assert (services / "a_client.py").read_bytes() == runner.foreign_bytes
    assert (services / "b_client.py").read_bytes() == modules["b_client.py"], "restored"


def test_a_splice_that_does_not_compile_aborts_with_four_before_anything_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    services, db, _recorder = _sweep_fixture(tmp_path, monkeypatch, {_MODULE: _DEMO_SOURCE})
    runner = _FakeRunner(services, _DEMO_SOURCE)
    monkeypatch.setattr(guard_audit, "_run_selection", runner)
    monkeypatch.setattr(guard_audit, "splice", lambda _data, _target, _polarity: b"def (\n")

    assert _sweep(db) == 4
    reported = capsys.readouterr().err

    first = guard_audit._targets_at(services)[0]
    assert f"bad splice at {first.key}" in reported
    assert runner.calls == [], "no mutant was run"
    assert (services / _MODULE).read_bytes() == _DEMO_SOURCE, "no mutant was written"


def test_an_invalid_selection_aborts_with_five_and_restores_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    services, db, _recorder = _sweep_fixture(tmp_path, monkeypatch, {_MODULE: _DEMO_SOURCE})
    runner = _FakeRunner(services, _DEMO_SOURCE, lambda _k, _p, _n: (4, "usage error"))
    monkeypatch.setattr(guard_audit, "_run_selection", runner)

    assert _sweep(db) == 5
    reported = capsys.readouterr().err

    first = guard_audit._targets_at(services)[0]
    assert f"invalid selection for {first.key}. Aborting." in reported
    assert len(runner.calls) == 1
    assert (services / _MODULE).read_bytes() == _DEMO_SOURCE


def test_a_timeout_in_the_mutant_run_exits_nine_and_still_restores_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The exception branch of the inner ``finally``: the run raises, the file comes back."""
    services, db, _recorder = _sweep_fixture(tmp_path, monkeypatch, {_MODULE: _DEMO_SOURCE})

    def times_out(_key: str, _polarity: bytes, _n: int) -> tuple[int, str]:
        raise subprocess.TimeoutExpired(cmd="pytest", timeout=7)

    runner = _FakeRunner(services, _DEMO_SOURCE, times_out)
    monkeypatch.setattr(guard_audit, "_run_selection", runner)

    assert _sweep(db) == 9
    reported = capsys.readouterr().err

    assert "TimeoutExpired" in reported
    assert (services / _MODULE).read_bytes() == _DEMO_SOURCE


def _invalid_selection(_key: str, _polarity: bytes, _n: int) -> tuple[int, str]:
    return 4, "usage error"


def _times_out(_key: str, _polarity: bytes, _n: int) -> tuple[int, str]:
    raise subprocess.TimeoutExpired(cmd="pytest", timeout=7)


@pytest.mark.parametrize(
    ("answer", "other_outcome"),
    [(_invalid_selection, 5), (_times_out, 9)],
    ids=["invalid-selection", "timeout"],
)
def test_a_foreign_write_outranks_the_abort_or_the_crash_it_coincides_with(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    answer: _Answer,
    other_outcome: int,
) -> None:
    """A refused restore decides the exit whatever else ended the run.

    Found by the QA of GQ-402 and measured on stand-in trees before the ordering existed: the
    refusal fed ``refused`` only for a loop that ran to its end, so a foreign write during a run
    that also returned an invalid selection exited 5, and one during a run that also timed out
    exited 9, the code ``main`` reserves for a crash. Both now exit 3, name the other outcome, and
    leave the foreign bytes where they are.
    """
    services, db, _recorder = _sweep_fixture(tmp_path, monkeypatch, {_MODULE: _DEMO_SOURCE})
    runner = _FakeRunner(services, _DEMO_SOURCE, answer, foreign_on_call=1)
    monkeypatch.setattr(guard_audit, "_run_selection", runner)

    assert _sweep(db) == 3
    reported = capsys.readouterr().err

    assert f"REFUSING to restore {_MODULE}" in reported
    assert f"(the run also ended with {other_outcome})" in reported
    assert (services / _MODULE).read_bytes() == runner.foreign_bytes


# --- _run_selection -------------------------------------------------------------------------------


class _FakeSubprocess:
    """Stands in for the ``subprocess`` module inside ``guard_audit``: records, never spawns."""

    def __init__(self) -> None:
        self.seen: dict[str, Any] = {}

    def run(self, cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.seen = {"cmd": cmd, **kwargs}
        return subprocess.CompletedProcess(cmd, 1, stdout="1 passed", stderr="warn\n")


def test_run_selection_pins_the_child_command_and_overrides_the_thread_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What the child gets, measured against a parent that carries OTHER values.

    The three variables are set to foreign values first. Without that step the red proof is
    false-green twice over: ``tests/conftest.py`` defaults ``OPENBLAS_NUM_THREADS`` to ``1`` for
    every run, and the shell this suite runs in exports ``OMP_NUM_THREADS=1``, so a mutant that
    dropped either line from ``env.update`` would still show ``1`` in the child.
    """
    for name, foreign in (
        ("OPENBLAS_NUM_THREADS", "48"),
        ("OMP_NUM_THREADS", "0"),
        ("PYTHONDONTWRITEBYTECODE", ""),
    ):
        monkeypatch.setenv(name, foreign)
    fake = _FakeSubprocess()
    monkeypatch.setattr(guard_audit, "subprocess", fake)
    node_ids = ["tests/test_demo.py::test_b", "tests/test_demo.py::test_a"]

    result = guard_audit._run_selection(node_ids, 7)

    assert result == (1, "1 passedwarn\n"), "stdout and stderr, concatenated without a separator"
    assert fake.seen["cmd"] == [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "--tb=line",
        *node_ids,
    ]
    assert fake.seen["cwd"] == guard_audit._REPO
    assert fake.seen["timeout"] == 7
    assert fake.seen["capture_output"] is True and fake.seen["text"] is True
    child = fake.seen["env"]
    for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "PYTHONDONTWRITEBYTECODE"):
        assert child[name] == "1", f"{name} must reach the child as 1 whatever the parent holds"
