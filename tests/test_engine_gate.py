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
import subprocess
import sys
from pathlib import Path

import pytest

from tests.engine_gate import engine_available

_REPO = Path(__file__).resolve().parents[1]

# The blocker both subprocess guards install: find_spec RAISES rather than answering None, which
# is the restricted-import-system case cli_common.require_display_engine calls "the measured one".
_BLOCKER = (
    "import sys\n"
    "class _Blocker:\n"
    "    def find_spec(self, name, path=None, target=None):\n"
    "        if name.split('.')[0] == 'opi_navigation':\n"
    '            raise ModuleNotFoundError(f"No module named {name!r}")\n'
    "        return None\n"
    "sys.meta_path.insert(0, _Blocker())\n"
)


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
    script = (
        _BLOCKER + "import pytest\n"
        "raise SystemExit(pytest.main("
        "['--collect-only', '-q', '-p', 'no:cacheprovider', 'tests/']))\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=_REPO,
        timeout=300,
        check=False,
    )

    assert "ImportError while loading conftest" not in (result.stdout + result.stderr), (
        "a raising finder still takes the collector down instead of skipping the engine-coupled "
        f"modules:\n{(result.stdout + result.stderr)[-1200:]}"
    )
    assert result.returncode == 0, (result.stdout + result.stderr)[-1200:]
    assert "tests collected" in result.stdout, result.stdout[-800:]


def _bare_engine_probes(source: str) -> list[int]:
    """Line numbers of ``find_spec("opi_navigation")`` calls, read from the SYNTAX TREE.

    From the tree, never from the text, and that is measured rather than stylistic: the first
    version of this guard matched a regex over the source and its single hit was
    ``test_cli_without_display_engine.py:51``, the DOCSTRING of the fixture that fakes the engine
    away. Prose describing the call is not the call. ``scripts/guard_audit.py`` records the same
    lesson about the same class of guard, in the same words, one directory over.

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
