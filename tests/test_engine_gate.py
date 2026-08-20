"""QA-48: the test suite's own question about the display engine has to ANSWER, never raise.

``tests/engine_gate.py`` exists because four places asked it with a bare
``importlib.util.find_spec("opi_navigation")``, and that call propagates whatever a meta-path
finder raises. Two of the four ran at COLLECTION time, so under a restricted import system the
whole run died instead of skipping the engine-coupled modules.

Three guards, because none of them catches the others' case:

* the helper answers for every finder failure (in-process, cheap, pins the contract),
* collection actually survives a raising finder (subprocess, pins the OUTCOME the contract is
  for, and this is the red-proof the ticket demands made durable),
* no tracked test reintroduces a bare call (structural, pins the BREADTH: a fifth site added
  next month is caught without anyone re-running the subprocess argument).
"""

from __future__ import annotations

import ast
import functools
import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

from tests.conftest import ENGINE_COUPLED_MODULES, pytest_configure, pytest_report_header
from tests.engine_gate import BLOCKER as _BLOCKER
from tests.engine_gate import (
    REQUIRE_DISPLAYS_ENV,
    displays_demanded,
    engine_available,
    engine_collection_decision,
)

_REPO = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "raised",
    [ModuleNotFoundError("blocked"), ImportError("finder is confused"), RuntimeError("no idea")],
    ids=["module-not-found", "import-error", "anything-else"],
)
def test_the_gate_answers_for_every_finder_failure(
    raised: BaseException, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whatever a finder throws, the answer is False and nothing propagates.

    ``ModuleNotFoundError`` is the absent case; the other two stand for a finder that could not
    answer at all. ``cli_common.require_display_engine`` keeps those apart because it has two exit
    codes to report with; a collection hook has one decision, so they collapse here. What does NOT
    collapse is a BROKEN engine (find_spec answers, the import then fails): that still reads as
    present, so its modules are collected and fail loudly, which the sibling module pins.
    """

    def _raise(name: str, package: str | None = None) -> object:
        raise raised

    monkeypatch.setattr(importlib.util, "find_spec", _raise)

    assert engine_available() is False


def _collect_without_the_engine(
    *, demanded: bool, quiet: bool = True
) -> subprocess.CompletedProcess[str]:
    """Collect the suite in a subprocess where ``opi_navigation`` is genuinely unreachable.

    The environment is passed EXPLICITLY, and that is load-bearing rather than tidy (GB-27): a
    child inherits the parent's environment, so once ``EPICS_MCP_REQUIRE_DISPLAYS`` exists, a
    developer with it set in their shell would silently flip this probe into the opposite case and
    redden a guard that has nothing to do with their change. Measured before the switch to an
    explicit env: the child saw the variable. So the demanded case SETS it and the default case
    SCRUBS it, and neither depends on who is running.

    ``quiet=False`` drops ``-q``, because ``-q`` suppresses the report HEADER, which is where the
    gap line lives. Measured: with ``-q`` the header assertion failed against a collection listing
    that was otherwise perfectly correct.
    """
    env = dict(os.environ)
    if demanded:
        env[REQUIRE_DISPLAYS_ENV] = "1"
    else:
        env.pop(REQUIRE_DISPLAYS_ENV, None)

    flags = "'--collect-only', '-p', 'no:cacheprovider', 'tests/'"
    if quiet:
        flags = "'--collect-only', '-q', '-p', 'no:cacheprovider', 'tests/'"
    script = _BLOCKER + f"import pytest\nraise SystemExit(pytest.main([{flags}]))\n"
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=_REPO,
        env=env,
        timeout=300,
        check=False,
    )


def test_collection_survives_a_raising_finder() -> None:
    """The outcome the helper exists for, proven the only way it can be: in a subprocess.

    Faking the finder inside this process cannot show it, because ``tests/conftest.py`` was
    imported long ago. Measured before the repair, with this exact blocker::

        ImportError while loading conftest '.../tests/conftest.py'
        tests/conftest.py:22: in <module>
            if importlib.util.find_spec("opi_navigation") is None:
        E   ModuleNotFoundError: No module named 'opi_navigation'
        (exit 4, nothing collected)

    Collection rather than a full run, because both collapsing sites are collection-time (the
    ``conftest`` line and ``test_server``'s ``skipif`` decorator) and a full run under the blocker
    would cost the suite a second time for no extra claim.
    """
    result = _collect_without_the_engine(demanded=False)

    assert "ImportError while loading conftest" not in (result.stdout + result.stderr), (
        "a raising finder still takes the collector down instead of skipping the engine-coupled "
        f"modules:\n{(result.stdout + result.stderr)[-1200:]}"
    )
    assert result.returncode == 0, (result.stdout + result.stderr)[-1200:]
    assert "tests collected" in result.stdout, result.stdout[-800:]


def _bare_engine_probes(source: str) -> list[int]:
    """Line numbers of ``find_spec("opi_navigation")`` calls, read from the SYNTAX TREE.

    From the tree, never from the text, and that is measured rather than stylistic: the first
    version of this guard matched a regex over the source and its single hit was the DOCSTRING of
    ``engine_absent``, the fixture in ``tests/test_cli_without_display_engine.py`` that fakes the
    engine away. Prose describing the call is not the call. ``scripts/guard_audit.py`` records the
    same lesson about the same class of guard, in the same words, one directory over.

    ⚠️ That citation used to carry a LINE number, and QA-42 moved the fixture past it: the line it
    named is now an unrelated assignment. Nothing mechanises a citation in prose, so the number is
    gone rather than re-measured. The fixture is named instead, and a name survives an edit.

    Reading the tree also settles the two exemptions a text scan would have needed, by making them
    unnecessary: ``tests/engine_gate.py`` reaches the name through a constant rather than a
    literal, and this module never calls ``find_spec`` at all, so neither is a hit and neither
    needs an entry that would have to be maintained.
    """
    lines: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name != "find_spec" or not node.args:
            continue
        first = node.args[0]
        if (
            isinstance(first, ast.Constant)
            and isinstance(first.value, str)
            and first.value.split(".")[0] == "opi_navigation"
        ):
            lines.append(node.lineno)
    return sorted(lines)


def test_no_tracked_test_calls_find_spec_on_the_engine_directly() -> None:
    """The breadth: one helper, and it stays the only door.

    A fifth bare call added later would reintroduce exactly the collection abort the sibling guard
    above measured, in a file nobody thinks to re-probe. Population from ``git ls-files`` so a new
    test file is covered the day it is tracked, with an anchor on a file that must be there, since
    an empty listing would let this pass while checking nothing.

    Scoped to ``tests/``, deliberately: the production path was made total first (QA-14,
    ``cli_common.require_display_engine``), and ``server.py`` asks the same question inside its own
    handling. Widening this to ``src/`` would flag that handled call as if it were a defect.
    """
    listing = subprocess.run(
        ["git", "-C", str(_REPO), "ls-files", "tests/*.py"],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    ).stdout.splitlines()
    assert "tests/conftest.py" in listing, (
        f"git ls-files returned a tests/ tree without conftest.py ({len(listing)} entries), the "
        "population anchor broke and this assertion would pass vacuously"
    )

    offenders = {
        rel: found
        for rel in listing
        if (found := _bare_engine_probes((_REPO / rel).read_text(encoding="utf-8")))
    }

    assert not offenders, (
        "a test asks for the display engine with a bare find_spec, which propagates whatever a "
        f"meta-path finder raises: {offenders}. Use tests.engine_gate.engine_available() instead."
    )


def test_the_breadth_guard_reads_calls_and_not_prose() -> None:
    """The property that repair was FOR, pinned on constructed input rather than on the tree.

    On today's tree both spellings agree (there is no bare call left), so no tree-driven assertion
    can hold this apart. The docstring line below is the exact shape that made the text version
    report a false positive.
    """
    a_call = 'import importlib.util\nx = importlib.util.find_spec("opi_navigation")\n'
    prose = 'def f():\n    """Make find_spec(\'opi_navigation\') answer None."""\n'
    through_a_name = (
        'from importlib.util import find_spec\nENGINE = "opi_navigation"\nfind_spec(ENGINE)\n'
    )

    assert _bare_engine_probes(a_call) == [2], "the guard must see a real call"
    assert _bare_engine_probes(prose) == [], "prose describing the call is not the call"
    assert _bare_engine_probes(through_a_name) == [], (
        "a call that reaches the name through a constant is what engine_gate itself does, and is "
        "not the defect this guard is for"
    )


# --- GB-27: a DEMANDED display run must not be reported green ---------------------------------


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", "  1  "])
def test_displays_demanded_accepts_truthy_spellings(value: str) -> None:
    assert displays_demanded({REQUIRE_DISPLAYS_ENV: value}) is True


@pytest.mark.parametrize("value", ["", "   ", "0", "false", "no", "off", "maybe"])
def test_displays_demanded_rejects_everything_else(value: str) -> None:
    assert displays_demanded({REQUIRE_DISPLAYS_ENV: value}) is False


def test_displays_demanded_is_false_when_the_variable_is_absent() -> None:
    """The default run and CI do not set it, and that MUST stay the silent path."""
    assert displays_demanded({}) is False


@pytest.mark.parametrize(
    ("available", "demanded", "expected"),
    [
        (True, False, "collect"),
        (True, True, "collect"),
        (False, False, "ignore"),
        (False, True, "fail"),
    ],
)
def test_the_collection_decision_covers_all_four_combinations(
    available: bool, demanded: bool, expected: str
) -> None:
    """The three outcomes, and the one asymmetry that carries the ticket.

    Demanding changes nothing while the engine is there (a demand is not a different suite), and
    everything while it is not. Without the fourth row this would also pass if "fail" were
    unreachable.
    """
    assert engine_collection_decision(available=available, demanded=demanded) == expected


def test_the_demanded_run_refuses_instead_of_collecting_a_partial_suite() -> None:
    """The outcome the switch exists for, proven the only way it can be: in a subprocess.

    Faking the finder inside this process cannot show it, because ``tests/conftest.py`` was
    imported long ago and its decision is already made.

    ⚠️ The assertions read STDERR and do not pin exit 1. ``pytest.UsageError`` from
    ``pytest_configure`` is reported as a usage error: measured, that is exit 4 and a single line
    on stderr, with stdout EMPTY. A copy of the sibling guard above (returncode == 0, text in
    stdout) would have looked at the wrong stream and the wrong code.
    """
    result = _collect_without_the_engine(demanded=True)
    combined = result.stdout + result.stderr

    assert result.returncode != 0, f"a demanded run over a missing engine stayed green:\n{combined}"
    assert REQUIRE_DISPLAYS_ENV in result.stderr, (
        "the refusal must name the variable that caused it, otherwise the reader hunts the cause "
        f"in the test tree instead of in their environment:\n{combined[-1200:]}"
    )
    assert "test_validate.py" in result.stderr, (
        f"the refusal must name what would have been skipped:\n{combined[-1200:]}"
    )
    assert "tests collected" not in result.stdout, (
        f"the run must refuse BEFORE collecting a partial suite:\n{result.stdout[-800:]}"
    )


def test_an_undemanded_run_carries_its_own_gap_in_the_report_header() -> None:
    """The half that needs no switch and no credential, and the one that fixes the damage.

    CI syncs without ``--group displays`` deliberately, so it tests exactly the standalone core a
    public user gets. What was wrong is that its green report said nothing about the modules it
    did not run, so a reader could not tell a full run from a partial one.
    """
    result = _collect_without_the_engine(demanded=False, quiet=False)

    assert "opi_navigation engine absent" in result.stdout, (
        "a run that drops the display-coupled modules must say so in its header:\n"
        f"{result.stdout[:1500]}"
    )
    for module in ENGINE_COUPLED_MODULES:
        assert module in result.stdout, f"{module} missing from the header gap line"
    assert REQUIRE_DISPLAYS_ENV in result.stdout, (
        "the header must name the way to turn the silent skip into a refusal"
    )


def test_the_header_speaks_only_for_the_ignore_decision() -> None:
    """Counter-probe: a line printed unconditionally would be noise, not a signal.

    ⚠️ This test used to open with ``assert engine_available()``, and that is the defect it now
    guards against. On an engine-less checkout, which is EXACTLY the CI lane this repository runs
    on purpose, that line is ``assert False``: the GB-27 commit turned CI red and it stayed red for
    six runs, because the author measured locally, where the engine IS installed. A ``skipif``
    would have hidden it rather than fixed it, and it would have switched the counter-probe off
    precisely where it is needed; the ``skipif`` decorators were deliberately abolished here
    anyway (``tests/live_gate.py``).

    The repair is to stop asking the ENVIRONMENT for a decision the hook can be HANDED. The header
    reads a frozen module global, so the branch a run happens to be in used to decide which
    branches were testable at all. With the decision injected, all three are, everywhere: in CI,
    on a developer machine, and under the blocker.

    What this does NOT claim is that pytest calls the hook or prints its answer. That half needs a
    real process and is pinned by the two subprocess guards above. Same two-layer split the file
    already uses for ``engine_collection_decision``.
    """
    assert pytest_report_header(decision="collect") is None, (
        "an installed engine leaves no gap, so the header must stay silent"
    )
    assert pytest_report_header(decision="fail") is None, (
        "the demanded-but-impossible run refuses in pytest_configure; a header would be dead prose"
    )

    lines = pytest_report_header(decision="ignore")
    assert lines and "opi_navigation engine absent" in lines[0], (
        f"the ignore decision is the one that MUST speak, got: {lines!r}"
    )


def test_the_refusal_speaks_only_for_the_fail_decision() -> None:
    """The other hook, same seam: injected rather than inherited from this run's environment.

    ``pytest_configure`` is the loud half of GB-27, and until now nothing tested its two SILENT
    branches at all. They matter: a refusal on ``collect`` would break every ordinary run, and one
    on ``ignore`` would break the standalone-core case this repository ships for.
    """
    for decision in ("collect", "ignore"):
        pytest_configure(_UNUSED_CONFIG, decision=decision)  # must not raise

    with pytest.raises(pytest.UsageError, match=REQUIRE_DISPLAYS_ENV):
        pytest_configure(_UNUSED_CONFIG, decision="fail")


#: ``pytest_configure`` deletes its ``config`` argument unread, so the counter-probe above has
#: nothing to build. Named rather than passed as a bare ``None`` so the next reader sees that the
#: hook genuinely does not touch it, instead of wondering what a None config would do.
_UNUSED_CONFIG = cast(pytest.Config, None)


# --- The engine-coupled module list is data, and data drifts ----------------------------------


def _module_level_imports(tree: ast.Module) -> set[str]:
    """Dotted names imported at MODULE level, the only ones that can break COLLECTION.

    Module level is the whole question here: ``server.py`` reaches the engine too, but inside
    ``_load_display_registrar``, so importing it costs nothing and it is correctly absent from
    ``ENGINE_COUPLED_MODULES``. A guard that counted any import would flag it and would have to
    grow an exemption for the one file that does this RIGHT.

    ``if``/``try`` bodies are walked because a guarded import still executes at module level; a
    ``def``/``class`` body is not, for the reason above.
    """
    found: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import | ast.ImportFrom):
            found |= _names_of(node)
        elif isinstance(node, ast.If | ast.Try):
            for inner in ast.walk(node):
                if isinstance(inner, ast.Import | ast.ImportFrom):
                    found |= _names_of(inner)
    return found


def _names_of(node: ast.Import | ast.ImportFrom) -> set[str]:
    """The dotted names one import statement binds, in every spelling.

    One home, because two walkers ask: this file's module-level guard and the below-module-level
    one further down. They disagreed once when the logic sat inline in both, which is the failure
    a shared helper removes rather than documents.

    ``from . import x`` yields nothing: a relative import stays inside its own package and can
    never name the engine.
    """
    if isinstance(node, ast.Import):
        return {alias.name for alias in node.names}
    if node.level or not node.module:
        return set()
    return {node.module} | {f"{node.module}.{alias.name}" for alias in node.names}


def _reaches(name: str, tainted: frozenset[str] | set[str]) -> bool:
    """Is *name*, or any package prefix of it, in *tainted*?

    ``from epics_mcp.tools import validate`` yields both ``epics_mcp.tools`` and
    ``epics_mcp.tools.validate``, and ``import a.b.c`` binds ``a``; walking the prefixes covers
    every spelling with one rule instead of a case analysis per import form.
    """
    while name:
        if name in tainted:
            return True
        name = name.rpartition(".")[0]
    return False


def _engine_tainted_modules() -> set[str]:
    """Every ``epics_mcp`` module that reaches ``opi_navigation`` at module level, transitively."""
    return set(_engine_tainted_frozen())


@functools.cache
def _engine_tainted_frozen() -> frozenset[str]:
    """The cached half. Zero arguments is SAFE here, and that is a measured claim rather than a
    convention: the only ``monkeypatch.setattr`` in this module is the one on
    ``importlib.util.find_spec`` inside ``_collect_without_the_engine``, nothing replaces ``_REPO``,
    and the answer is a pure function of the tracked source tree, which no test here rewrites.
    Named rather than given a line number: the first version of this sentence said "line 61" and
    was off by one before it was committed, because the same diff added an import above it.

    Why: the walk re-``ast.parse``s every file under ``src/`` and is called 15 times per run, 13 of
    them from the parametrised walker at the bottom of this file. What that costs is derived from
    the module's own before and after rather than from a stopwatch on the function: 42.27 s -> 26.83
    s with nothing else changed, so 14 eliminated walks are worth 15.44 s, about 1.1 s each and
    roughly 16.5 s for all fifteen. Re-derive it the same way, by reverting this decorator and
    running the module alone.

    A ``frozenset`` rather than a ``set``, and the wrapper above rebuilds a mutable copy: one caller
    hands the result to ``_engine_imports_below_module_level``, and a cached mutable would let a
    future callee poison every later call.
    """
    src = _REPO / "src"
    graph: dict[str, set[str]] = {}
    for path in sorted(src.rglob("*.py")):
        parts = list(path.relative_to(src).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        graph[".".join(parts)] = _module_level_imports(ast.parse(path.read_text(encoding="utf-8")))

    tainted = {
        module
        for module, imports in graph.items()
        if any(name.split(".")[0] == _ENGINE_PKG for name in imports)
    }
    changed = True
    while changed:  # transitive closure; the graph is ~60 modules, a fixpoint loop is plenty
        changed = False
        for module, imports in graph.items():
            if module in tainted:
                continue
            if any(_reaches(name, tainted) for name in imports):
                tainted.add(module)
                changed = True
    return frozenset(tainted)


#: The optional display engine, by package name. One spelling, because three functions ask.
_ENGINE_PKG = "opi_navigation"


def test_the_engine_coupled_module_list_is_complete_and_real() -> None:
    """``ENGINE_COUPLED_MODULES`` is hand-written data, and nothing checked it (QA, 2026-08-03).

    It feeds ``collect_ignore``, the header line and the refusal, so a wrong entry makes all three
    wrong at once, and in the direction that hurts: a SEVENTH engine-coupled module would not be
    ignored, would fail at import, and would redden the engine-less lane only. That is the exact
    damage class this file already carries once, where a counter-probe asserted an installed engine
    and CI went red for six runs while every local run stayed green.

    Both directions, because they fail differently. A missing entry is the silent-in-CI defect
    above; a stale entry (a renamed or moved file) keeps being counted by ``len()``, so the header
    and the refusal name a module that no longer exists and overstate the gap.

    Module level only, and that is what makes the guard precise rather than noisy: see
    :func:`_module_level_imports`.
    """
    tainted = _engine_tainted_modules()
    assert tainted, "the taint walk found nothing at all, so this assertion would be vacuous"

    coupled = {
        path.name
        for path in sorted((_REPO / "tests").glob("test_*.py"))
        if any(
            _reaches(name, tainted) or name.split(".")[0] == _ENGINE_PKG
            for name in _module_level_imports(ast.parse(path.read_text(encoding="utf-8")))
        )
    }

    assert coupled == set(ENGINE_COUPLED_MODULES), (
        "ENGINE_COUPLED_MODULES no longer matches the tree. Missing from the list (these would "
        f"break the engine-less lane): {sorted(coupled - set(ENGINE_COUPLED_MODULES))}. Listed but "
        f"not coupled (these inflate the header and the refusal): "
        f"{sorted(set(ENGINE_COUPLED_MODULES) - coupled)}"
    )

    missing_files = [
        name for name in ENGINE_COUPLED_MODULES if not (_REPO / "tests" / name).is_file()
    ]
    assert not missing_files, (
        f"ENGINE_COUPLED_MODULES names files that do not exist: {missing_files}. collect_ignore "
        "resolves names against the conftest directory, so such an entry silently matches nothing."
    )


def test_the_coupling_guard_reads_module_level_and_not_function_level() -> None:
    """The property the guard is FOR, pinned on constructed input rather than on the tree.

    On today's tree every coupled module imports at module level, so no tree-driven assertion can
    hold this apart. ``epics_mcp/server.py`` is the real case for the second spelling: it reaches
    the engine inside ``_load_display_registrar``, which is why it is correctly NOT in the list.
    """
    at_module_level = "from epics_mcp.tools.validate import x\n"
    inside_a_function = "def f():\n    from epics_mcp.tools.validate import x\n"
    guarded_at_module_level = "try:\n    import opi_navigation\nexcept ImportError:\n    pass\n"

    assert "epics_mcp.tools.validate" in _module_level_imports(ast.parse(at_module_level))
    assert _module_level_imports(ast.parse(inside_a_function)) == set(), (
        "an import inside a function cannot break collection and must not be flagged"
    )
    assert "opi_navigation" in _module_level_imports(ast.parse(guarded_at_module_level)), (
        "a guarded import still executes at module level"
    )


# --- The other half: an engine import BELOW module level ---------------------------------------

#: The helper that answers "is the engine there", by name.
#:
#: ⛔ MATCHED STRUCTURALLY, NEVER AS A SUBSTRING, and that is a correction paid for by a post-build
#: review of this very guard. The first version asked ``"engine_available" in ast.unparse(...)``,
#: which cannot tell a guard from its own inversion. Measured on that version, ALL of these passed:
#: ``if not engine_available():`` around the import (it then runs exactly when the engine is
#: ABSENT), ``if engine_available:`` with the call forgotten (always truthy),
#: ``@pytest.mark.skipif(engine_available(), ...)`` (inverted), and a ``@pytest.mark.skip`` that
#: merely mentioned the name in its reason text. A one-word mutation switched the guard off in
#: silence, which is the defect class this file exists to remove.
_ENGINE_GUARD = "engine_available"


def _is_engine_call(node: ast.expr) -> bool:
    """``engine_available()`` as a CALL, however it is qualified."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    return name == _ENGINE_GUARD


def _is_absent_engine_test(node: ast.expr) -> bool:
    """``not engine_available()``, the "the engine is missing" spelling."""
    return (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.Not)
        and _is_engine_call(node.operand)
    )


def _skips_a_test_without_the_engine(decorator: ast.expr) -> bool:
    """``@pytest.mark.skipif(not engine_available(), ...)`` and nothing that merely mentions it."""
    if not isinstance(decorator, ast.Call):
        return False
    func = decorator.func
    if not (isinstance(func, ast.Attribute) and func.attr == "skipif"):
        return False
    return bool(decorator.args) and _is_absent_engine_test(decorator.args[0])


def _leaves_the_function(body: list[ast.stmt]) -> bool:
    """Does this suite abandon the test, by return, raise, or a pytest skip?"""
    for stmt in body:
        if isinstance(stmt, ast.Return | ast.Raise):
            return True
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            func = stmt.value.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name in {"skip", "xfail", "exit"}:
                return True
    return False


def _engine_guarded_lines(func: ast.FunctionDef | ast.AsyncFunctionDef) -> set[int]:
    """Lines of *func* that cannot execute without the engine, by either accepted spelling.

    TWO spellings, because this repository documents the second one as the preferred style
    (``tests/live_gate.py``: the marker decorators were deliberately replaced by a call at the top
    of the test). A guard that recognised only the ``if`` would redden the direction the tree is
    moving in, measured on a constructed case before this was written.

    * ``if engine_available():`` guards its BODY.
    * ``if not engine_available(): pytest.skip(...)`` as a DIRECT statement of the function guards
      everything AFTER it. Direct only, deliberately: a skip buried in a loop or a branch may never
      be reached, and treating the rest of the function as covered would be the over-approximation
      that turns a guard into a rubber stamp.
    """
    guarded: set[int] = set()
    for node in ast.walk(func):
        if isinstance(node, ast.If) and _is_engine_call(node.test):
            for stmt in node.body:
                guarded.update(range(stmt.lineno, (stmt.end_lineno or stmt.lineno) + 1))
    for index, stmt in enumerate(func.body):
        if not (isinstance(stmt, ast.If) and _is_absent_engine_test(stmt.test)):
            continue
        if not _leaves_the_function(stmt.body):
            continue
        rest = func.body[index + 1 :]
        if rest:
            guarded.update(range(rest[0].lineno, (func.end_lineno or rest[0].lineno) + 1))
    return guarded


def _engine_imports_below_module_level(
    tree: ast.Module, tainted: set[str]
) -> tuple[list[str], list[str]]:
    """``(all, unguarded)`` engine-reaching imports inside a function, as ``<func>:<line>``.

    Resolved through the ANCESTOR CHAIN of each import rather than per function, which is the
    second correction from the same review. ``ast.walk`` yields a nested function as its own root,
    so the first version re-asked the question there with the enclosing guard out of view: a helper
    or a deferred ``def`` inside a properly skipped test was reported unguarded, and counted twice.

    A decorator only counts on a ``test_`` function, because pytest honours a mark on a collected
    ITEM and on nothing else. A ``skipif`` on a helper or a fixture reads like a guard and does
    nothing at run time, and putting one there is the obvious way to silence a false positive.
    """
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    every: list[str] = []
    unguarded: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Import | ast.ImportFrom):
            continue
        names = _names_of(node)
        if not any(_reaches(n, tainted) or n.split(".")[0] == _ENGINE_PKG for n in names):
            continue
        enclosing: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
        parent = parents.get(node)
        while parent is not None:
            if isinstance(parent, ast.FunctionDef | ast.AsyncFunctionDef):
                enclosing.append(parent)
            parent = parents.get(parent)
        if not enclosing:
            continue  # module level, class body included: the completeness guard above owns those
        where = f"{enclosing[0].name}:{node.lineno}"
        every.append(where)
        # ANY enclosing function may carry the guard: the innermost one, or the test that owns it.
        if not any(
            (
                func.name.startswith("test_")
                and any(_skips_a_test_without_the_engine(dec) for dec in func.decorator_list)
            )
            or node.lineno in _engine_guarded_lines(func)
            for func in enclosing
        ):
            unguarded.append(where)
    return every, unguarded


def test_an_engine_import_below_module_level_is_guarded() -> None:
    """The other half of the coupling question, and the half nothing was watching.

    ``ENGINE_COUPLED_MODULES`` and the guard above answer "can this module be IMPORTED without the
    engine". They are complete for that question and deliberately blind to an import inside a
    ``def`` (see :func:`_module_level_imports`), which is right: such an import costs nothing at
    COLLECTION. It costs everything at RUN time, and nothing was asking. Measured: on 2026-08-16
    two test functions grew an engine-reaching import in their body, collection stayed clean, both
    modules were correctly absent from the list, and the engine-less CI lane was red for 8 runs
    while every local run stayed green and every window reporting it was right.

    ⚠️ That count was written as 21 first, taken from a handover instead of measured, in the very
    repair whose subject is a number nobody checked. 21 was that handover's figure for the whole
    red period, itself wrong (16), and neither belongs here: this defect entered at 15:08 UTC and
    the lane went green at 23:45, which is 8 runs
    (``gh run list --workflow=ci.yml --json conclusion,createdAt``, filtered to that window).

    The remedy is NOT the list. Adding such a module would redden the guard above ("listed but not
    coupled"), measured: neither module imports the engine at module level. The remedy is a guard
    on the import itself, either a ``skipif`` on the function or an ``if engine_available():``
    around it, and this holds that.

    ⚠️ HONEST REACH, because a guard that oversells itself is worse than none. It reads literal
    ``import`` statements. It does NOT see:

    * ``importlib.import_module`` or ``__import__`` with a COMPUTED name, and this tree has one:
      ``tests/test_provenance.py`` builds a module name out of a dict, so extending that dict with
      a display-gated tool would pass here and fail in CI;
    * one test module importing another, which this tree also has
      (``tests/test_commit_message_guard.py``), and which the guard above shares, since its taint
      walk covers ``src/`` only.

    Provably red: put the deleted ``from epics_mcp.display_tools import register_display_tools``
    back into ``tests/test_prompts.py``, or drop the ``skipif`` from the display-tool test in
    ``tests/test_server.py``.
    """
    tainted = _engine_tainted_modules()
    every: list[str] = []
    unguarded: dict[str, list[str]] = {}
    # EVERY python file under tests/, not just test_*.py at the top: conftest.py owns autouse
    # fixtures that run for every test in the suite, and an engine import in one of those is the
    # worst version of exactly this defect. The first version of this population read
    # glob("test_*.py") and could not have seen it.
    for path in sorted((_REPO / "tests").rglob("*.py")):
        if path.name in ENGINE_COUPLED_MODULES:
            continue  # the whole module is dropped at collection, so its bodies never run
        found, bad = _engine_imports_below_module_level(
            ast.parse(path.read_text(encoding="utf-8")), tainted
        )
        every.extend(f"{path.name}::{where}" for where in found)
        if bad:
            unguarded[path.name] = bad

    assert every, (
        "no test function imports the engine below module level at all, so this TREE-DRIVEN half "
        "is vacuous. That is allowed to happen and is not a reason to delete the guard: the "
        "constructed pin below holds the walker's behaviour on its own. Check that the population "
        "really emptied rather than that the walk broke, then relax this line."
    )
    assert not unguarded, (
        f"engine-reaching import(s) inside a test function with no engine guard: {unguarded}. "
        "These are collected and RUN on an engine-less install and fail there with "
        "ModuleNotFoundError. Guard the import with a skipif on the function or an "
        "'if engine_available():', or drop the import if nothing uses it. Do NOT add the module "
        "to ENGINE_COUPLED_MODULES: that list is about module-level imports and the guard for it "
        "would go red."
    )


#: What the walker must and must not flag, as SOURCE rather than as tree. Every sibling walker in
#: this file has such a pin; the one that shipped without it is the one whose first version
#: accepted its own inversion, so this table is the actual repair and the structural matching above
#: is only how it is met. Left column: does an unguarded engine import get reported.
_WALKER_CASES = (
    (True, "unguarded", "def test_x():\n    from epics_mcp.display_tools import y\n"),
    (
        False,
        "skipif on the test",
        "@pytest.mark.skipif(not engine_available(), reason='x')\n"
        "def test_x():\n    from epics_mcp.display_tools import y\n",
    ),
    (
        False,
        "if engine_available(): around it",
        "def test_x():\n    if engine_available():\n"
        "        from epics_mcp.display_tools import y\n",
    ),
    (
        False,
        "the early-skip idiom this repository prefers",
        "def test_x():\n    if not engine_available():\n        pytest.skip('no engine')\n"
        "    from epics_mcp.display_tools import y\n",
    ),
    (
        True,
        "INVERTED if: the import runs exactly when the engine is absent",
        "def test_x():\n    if not engine_available():\n"
        "        from epics_mcp.display_tools import y\n",
    ),
    (
        True,
        "the call forgotten, so the test is always truthy",
        "def test_x():\n    if engine_available:\n        from epics_mcp.display_tools import y\n",
    ),
    (
        True,
        "INVERTED skipif",
        "@pytest.mark.skipif(engine_available(), reason='x')\n"
        "def test_x():\n    from epics_mcp.display_tools import y\n",
    ),
    (
        True,
        "the name only mentioned in a reason text",
        "@pytest.mark.skip(reason='engine_available() says no')\n"
        "def test_x():\n    from epics_mcp.display_tools import y\n",
    ),
    (
        True,
        "a fixture, where a decorator cannot help",
        "@pytest.fixture\ndef reg():\n    from epics_mcp.display_tools import y\n    return y\n",
    ),
    (
        True,
        "skipif on a HELPER, which pytest does not honour",
        "@pytest.mark.skipif(not engine_available(), reason='x')\n"
        "def _load():\n    from epics_mcp.display_tools import y\n",
    ),
    (
        False,
        "a nested def inside a properly skipped test",
        "@pytest.mark.skipif(not engine_available(), reason='x')\n"
        "def test_x():\n    def helper():\n        from epics_mcp.display_tools import y\n"
        "    return helper()\n",
    ),
    (
        True,
        "the ELSE branch of if engine_available()",
        "def test_x():\n    if engine_available():\n        pass\n    else:\n"
        "        from epics_mcp.display_tools import y\n",
    ),
    (
        False,
        "module level, which the completeness guard above owns",
        "from epics_mcp.display_tools import y\n",
    ),
)


@pytest.mark.parametrize(("flagged", "label", "source"), _WALKER_CASES)
def test_the_below_module_level_walker_on_constructed_input(
    flagged: bool, label: str, source: str
) -> None:
    """The walker's behaviour pinned on source it cannot have been tuned to, both directions.

    Eight of these thirteen were RED against the first version of the walker, measured, and four of
    those eight were false NEGATIVES, i.e. it waved through the exact spellings a tired author
    writes by accident. A tree-driven assertion could not have caught any of them: today's tree
    contains none of these shapes, which is the whole reason a guard needs constructed input as
    well as a population.
    """
    _, unguarded = _engine_imports_below_module_level(ast.parse(source), _engine_tainted_modules())
    assert bool(unguarded) is flagged, f"{label}: expected flagged={flagged}, got {unguarded!r}"
