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
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import ENGINE_COUPLED_MODULES, pytest_report_header
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


def test_the_header_stays_silent_when_nothing_is_missing() -> None:
    """Counter-probe: a line printed unconditionally would be noise, not a signal.

    This checkout HAS the engine, so the ordinary run this test belongs to must not carry the gap
    line. Without this the assertion above would also pass on a header that always prints.
    """
    assert engine_available(), (
        "this counter-probe assumes an installed engine; on an engine-less checkout it cannot "
        "distinguish the two cases and would be vacuous"
    )
    assert pytest_report_header() is None
