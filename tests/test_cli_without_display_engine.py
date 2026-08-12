"""The two display-aware CLIs must refuse politely where the engine is absent, not traceback.

Found by installing the built wheel into a clean environment and running every console script the
package declared AT THE TIME, which was five: three worked, and ``epics-crossplane`` and
``epics-coverage`` died on their module-level import chain with a bare ``ModuleNotFoundError``.
That is the first thing an outside user sees after ``pip install``, and it reads as a broken
package rather than as a missing optional capability. The counts in this file are dated wherever
they describe that measurement, because an undated one is how the refusal itself came to name
three of five for two releases; the guards below derive theirs from ``[project.scripts]``.

``server.py`` already had the right posture for the MCP surface: register the display tools only
when the engine imports, keep the core server up. These tests pin the same decision for the CLI
surface, which had never been given it.

The engine is normally PRESENT in this checkout, so most tests here fake its absence rather than
relying on the environment. That is the only way they can run in CI, where the engine is absent,
AND locally, where it is not.

QA-42 MOVED THE SEAM, and this module is where that is pinned. The check used to run BEFORE the
parser was built, which made ``--help`` and ``--version`` unreachable on exactly the install that
needs them most. It now runs AFTER ``parse_args``, and a usage error still reaches it through
``cli_common.DisplayEngineAwareParser``. So there are two refusal paths, not one, and they are
guarded separately: past the parser (``main`` RETURNS the code) and inside ``error`` (``main``
RAISES ``SystemExit``). Raise-versus-return is what tells the new behaviour from the old one; the
exit code does not, because both answer 2 where the engine is absent.

⚠️ WHAT THE ``engine_absent`` FIXTURE CANNOT SHOW, and it is the headline property of QA-42: it
fakes ``find_spec`` only. The real import machinery is untouched and the engine IS installed here,
so a re-introduced engine import inside the parser builds fine under the fixture and reddens only
in CI. Every claim of the form "this works with no engine at all" is therefore proven in a
SUBPROCESS under ``tests.engine_gate.BLOCKER``, which is total in both environments.
"""

from __future__ import annotations

import ast
import importlib
import os
import re
import subprocess
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path

import pytest

from epics_mcp import __version__
from epics_mcp.cli_common import (
    _ENGINE_ENTRY_POINT,
    _report_engine_absent,
    _report_engine_broken,
    require_display_engine,
)
from tests.engine_gate import BLOCKER, engine_available

_REPO = Path(__file__).resolve().parents[1]
_COMMANDS = (
    ("epics_mcp.cli_coverage", "epics-coverage"),
    ("epics_mcp.cli_crossplane", "epics-crossplane"),
)

#: Arguments each command accepts as COMPLETE, so ``parse_args`` succeeds and the refusal that
#: follows is the post-parse one. The paths need not exist: they are validated inside the
#: orchestrator, which the engine check returns before.
_VALID_ARGV = {
    "epics-coverage": ["--displays", "x"],
    "epics-crossplane": ["--displays", "x", "--st-cmd", "y"],
}

#: The failure ``engine_present_but_broken`` injects, as a constant so a guard can derive the
#: expected refusal from the SAME exception the fixture raises instead of restating its message.
#: The everyday cause of that state is a missing transitive dependency, hence this one.
_BROKEN_ENGINE_CAUSE = ImportError("No module named 'numpy'")


def _normalized(text: str) -> str:
    """Collapse whitespace: the refusals are hand-wrapped for a terminal, so a phrase that happens
    to straddle a line break is not a different phrase."""
    return " ".join(text.split())


def _derived_refusal(
    reporter: Callable[[str], int], command: str, capsys: pytest.CaptureFixture[str]
) -> str:
    """The exact text *reporter* writes for *command*, read back and whitespace-normalized.

    DERIVED rather than written out as a literal, and that is decision OX applied here. A fixed
    needle measures the SPELLING of ``cli_common``'s refusal: reword one line there and every
    assertion below goes red for a cosmetic reason, whose natural repair is to loosen back to
    ``command in err``. But ``command`` alone is satisfied by argparse's OWN message, which prints
    ``prog`` twice, so that loosening would silently acquit the very mutation these guards exist to
    catch. Comparing against what the reporter actually writes is immune to both.
    """
    reporter(command)
    return _normalized(capsys.readouterr().err)


#: A console command as this repository writes one in prose: in backticks, prefixed ``epics-``.
#: Same shape and same reason as ``tests/test_examples_match_entry_points.py``.
_COMMAND_IN_PROSE = re.compile(r"`(epics-[a-z][a-z0-9-]*)`")

#: The claim the refusal makes about what still works, as one match: the spelled-out number and
#: the bracketed list. Read from the WHITESPACE-NORMALIZED message, since it is hand-wrapped.
_STILL_WORKS_CLAIM = re.compile(r"The other (\w+) commands \(([^)]*)\)")

#: Number words spelled out in this repository's prose, so a claim of "five" can be read back.
_SPELLED = {"three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9}

#: What ``cli_common`` offers a command that CANNOT work without the display engine. CALLING
#: either is what makes a command display-aware, which is why the derivation below reads calls
#: rather than a list somebody has to remember to extend.
_ENGINE_GATE_NAMES = frozenset({"require_display_engine", "DisplayEngineAwareParser"})

#: The phrase docs/tools.md uses to claim the core-install set. An anchor, not a line number,
#: because the sentence is hand-wrapped and a line number is the weaker anchor.
_CORE_INSTALL_ANCHOR = "are part of the core install"

#: Markdown separates paragraphs on a blank line, whatever the file's line endings are.
_PARAGRAPH_BREAK = re.compile(r"(?:\r?\n){2,}")


def _declared_commands() -> dict[str, str]:
    """``{command: module}`` from ``[project.scripts]``, the packaging authority.

    Same source as ``tests/test_cli_version.py`` and ``test_examples_match_entry_points.py``, and
    for the same reason: it is what an install actually creates, so an eighth command is covered
    here the day it appears rather than the day somebody remembers this file.
    """
    data = tomllib.loads((_REPO / "pyproject.toml").read_text(encoding="utf-8"))
    return {
        command: target.rsplit(":", 1)[0] for command, target in data["project"]["scripts"].items()
    }


def _called_name(func: ast.expr) -> str | None:
    """The bare name a call targets: ``f()``, ``mod.f()`` and ``pkg.mod.f()`` all answer ``f``."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _display_aware_commands() -> set[str]:
    """The declared commands whose entry-point module CALLS the engine gate.

    DERIVED, not listed. A list is what produced the defect these guards exist for: the refusal
    named three of five working commands for two releases because both halves were hand-maintained
    prose.

    ⚠️ It reads CALLS, and the first version of this read IMPORTS, which was too narrow in both
    directions. ``from epics_mcp.cli_common import require_display_engine`` is one of at least six
    ways to reach that function: a relative import, ``from epics_mcp import cli_common`` with a
    dotted call, a plain ``import epics_mcp.cli_common``, a star import and a re-export through a
    third module all reach it while matching no such ImportFrom, so a core command could take the
    gate with every guard in this file staying green. In the other direction a dead import, left
    behind by a refactor, classified a working command as display-aware and the guards would then
    have demanded its removal from the refusal. A call is the fact that decides the behaviour, and
    it has one shape in the tree however the name arrived. It also ignores prose: ``server.py``
    names ``cli_common.require_display_engine`` in a docstring and is correctly not counted.

    Honest limit: a command that reaches the engine WITHOUT this gate, by importing
    ``opi_navigation`` itself, is invisible here. That is a different defect (the traceback QA-14
    removed) and the guards in this module pin it separately.
    """
    aware = set()
    for command, module_name in _declared_commands().items():
        source = (_REPO / "src").joinpath(*module_name.split(".")).with_suffix(".py")
        assert source.is_file(), f"{command} points at {module_name}, whose source is missing"
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and _called_name(node.func) in _ENGINE_GATE_NAMES:
                aware.add(command)
    return aware


def _core_commands() -> set[str]:
    """The commands that work on an install with no display engine at all."""
    return set(_declared_commands()) - _display_aware_commands()


#: Every console entry point as ``(module, command)``, for properties every CLI must hold
#: regardless of the engine. DERIVED, because the hand-written version of this named four of
#: seven: it was written when there were four and never grew with ``epics-init``, ``epics-testpv``
#: or the server, so three commands sat outside a guard whose whole point is that it holds for all
#: of them. The command travels with the module because the guard asserts the usage line.
_ALL_ENTRY_POINTS = tuple(
    sorted((module, command) for command, module in _declared_commands().items())
)


@pytest.fixture
def engine_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``importlib.util.find_spec('opi_navigation')`` answer None, as a clean install does."""
    real_find_spec = importlib.util.find_spec

    def _find_spec(name: str, package: str | None = None) -> object | None:
        if name == "opi_navigation" or name.startswith("opi_navigation."):
            return None
        return real_find_spec(name, package)

    monkeypatch.setattr(importlib.util, "find_spec", _find_spec)


def test_the_engine_is_reported_present_in_this_checkout() -> None:
    """A control for the fixture below: without it, a broken fake would look like a passing test.

    Skips rather than fails where the engine really is absent (CI), because that is a legitimate
    environment, not a defect.

    The question goes through ``tests.engine_gate`` (QA-48), which reaches ``find_spec`` as an
    attribute of ``importlib.util`` for the reason that matters here: the ``engine_absent`` fixture
    below fakes an engine-less environment by monkeypatching exactly that attribute, and this
    control has to see the same world the fixture builds.
    """
    if not engine_available():
        pytest.skip("opi_navigation absent in this environment, nothing to control against")

    assert require_display_engine("epics-coverage") is None


def test_a_missing_engine_is_a_usage_error_not_a_crash(
    engine_absent: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit code 2, the usage-error code: nothing went wrong, the command cannot apply here."""
    code = require_display_engine("epics-coverage")

    assert code == 2
    message = capsys.readouterr().err
    assert message.startswith("epics-coverage: needs the opi_navigation display engine")


def test_the_engine_gate_partitions_the_declared_commands() -> None:
    """Anchor for the two guards below: an empty or broken derivation must not leave them green.

    Both directions have to be non-empty. An empty ``display_aware`` would mean the import scan
    stopped seeing the gate, and then "the commands that work without the engine" would silently
    become "every command", which every message trivially satisfies.

    It also holds the hand-written ``_COMMANDS`` pair in this file against the same derivation, so
    a third display-aware command cannot leave the older parametrized guards checking two of three.
    """
    declared = set(_declared_commands())
    display_aware = _display_aware_commands()

    assert declared, "no [project.scripts] found, the test anchor broke"
    assert display_aware, "no command imports the engine gate, the derivation broke"
    assert display_aware < declared
    assert display_aware == {command for _, command in _COMMANDS}, (
        "the hand-written _COMMANDS pair no longer matches the modules that import the engine gate"
    )


def test_the_message_says_what_still_works_and_how_to_get_the_rest(
    engine_absent: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """A refusal that only says "no" sends the reader to the issue tracker. This one has to name
    every command that DOES work and the one command that installs the engine, because the
    reader's next question is always one of those two.

    ⚠️ THE SET, NOT A HANDFUL OF NAMES, and that half was learned the expensive way. This guard
    used to assert that three names appear somewhere in the text. It stayed green while the message
    named three of the five working commands: ``epics-init`` dropped out of the sentence when 0.5.0
    added it, ``epics-testpv`` when it was added after that, and a reader on a core-only install
    was told that two commands they can actually run do not work. Measured from a real wheel
    installed into a clean environment, which is also how the original defect in this module was
    found. ``test_examples_match_entry_points.py`` had already learned this for the README's
    command list; the same disease sat one file away, in prose nothing read.

    The expectation is DERIVED (``[project.scripts]`` minus the display-aware commands), so a new
    command reddens this the day it is declared. The number is asserted from the same set as the
    names, so the two cannot drift apart from each other either.
    """
    require_display_engine("epics-crossplane")
    message = _normalized(capsys.readouterr().err)

    claim = _STILL_WORKS_CLAIM.search(message)
    assert claim, f"the refusal no longer states which commands still work: {message!r}"
    listed = set(re.findall(r"epics-[a-z][a-z0-9-]*", claim.group(2)))
    core = _core_commands()

    assert listed == core, (
        "the refusal's command list disagrees with [project.scripts]: "
        f"only in the message {sorted(listed - core)}, only declared {sorted(core - listed)}"
    )
    assert _SPELLED.get(claim.group(1)) == len(core), (
        f"the refusal says 'the other {claim.group(1)} commands' for {len(core)} of them"
    )
    assert "--group displays" in message, "the refusal must still say how to get the engine"


def test_the_tools_page_names_the_same_core_commands_as_the_refusal() -> None:
    """The second surface stating this fact, and it disagreed with the first one DIFFERENTLY.

    ``docs/tools.md`` listed four commands as the core install and left out ``epics-mcp``, while
    the refusal listed three and left out two others: two pages, two different incomplete sets,
    neither read by anything. The doc-sync clause in this repository's ``CLAUDE.md`` says to
    enumerate every surface that describes a behaviour rather than to recall them, and this is that
    enumeration turned into a guard for the one fact that had two homes.

    Anchored on the phrase rather than on a line number, because the sentence is hand-wrapped and
    a line number is the weaker anchor. Bounded by its PARAGRAPH: bounding the claim by the
    preceding full stop instead reaches back over anything that carries none, which in Markdown is
    most headings, bullets and table cells, so a command named in a heading above would be read as
    part of the claim and the page could name four while this guard read five.
    """
    page = (_REPO / "docs" / "tools.md").read_text(encoding="utf-8")
    claiming = [
        " ".join(block.split())
        for block in _PARAGRAPH_BREAK.split(page)
        if _CORE_INSTALL_ANCHOR in " ".join(block.split())
    ]

    assert len(claiming) == 1, (
        f"expected exactly one paragraph claiming {_CORE_INSTALL_ANCHOR!r}, found {len(claiming)}"
    )
    listed = set(_COMMAND_IN_PROSE.findall(claiming[0].split(_CORE_INSTALL_ANCHOR)[0]))
    core = _core_commands()

    assert listed == core, (
        "docs/tools.md disagrees with [project.scripts] about the core install: "
        f"only on the page {sorted(listed - core)}, only declared {sorted(core - listed)}"
    )


@pytest.mark.parametrize(("module_name", "command"), _COMMANDS)
def test_complete_arguments_still_meet_the_refusal_after_parsing(
    module_name: str, command: str, engine_absent: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """The POST-PARSE refusal path: arguments argparse is happy with, engine still missing.

    Deliberately called with a COMPLETE argument list, which is the half that only this path can
    reach. Its sibling below covers the other half, where argparse rejects the arguments first.

    Red-proof is RAISE versus RETURN, not the exit code and not the message. Before QA-42 the check
    ran ahead of the parser, so ``main([])`` returned 2 and the two paths were indistinguishable;
    delete the post-parse check today and this call runs on into ``resolve_user_path``, which raises
    ``EpicsError`` on the nonexistent ``x`` and also exits 2. Only the refusal TEXT tells those
    apart, which is why it is asserted rather than the code alone.
    """
    module = importlib.import_module(module_name)

    code = module.main(_VALID_ARGV[command])
    refusal = _normalized(capsys.readouterr().err)

    assert code == 2, f"{command} should refuse with a usage error, not run"
    assert refusal == _derived_refusal(_report_engine_absent, command, capsys)


@pytest.mark.parametrize(("module_name", "command"), _COMMANDS)
def test_a_usage_error_is_answered_with_the_engine_not_with_argparse(
    module_name: str, command: str, engine_absent: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """The ERROR-PATH refusal: a reader who mistypes on a core-only install gets usable advice.

    This is QA-42's first pre-decision made durable (project decision, 2026-07-31: the engine
    message stays). Moving the check behind the parser would otherwise
    have answered ``epics-coverage --nope`` with "the following arguments are required: --displays",
    advice nobody can act on, because supplying it would not make the command run either.

    The argv carries the REQUIRED options plus the unknown one, deliberately: argparse checks
    required arguments inside ``parse_known_args`` and unrecognised extras afterwards, in a
    different call site, so a bare ``["--nope"]`` would measure the missing-argument path while
    claiming the unknown-argument one (measured: it reports ``--displays``, never ``--nope``).

    Red-proof: drop the ``DisplayEngineAwareParser`` override and argparse answers instead. It also
    exits 2, and its own message contains the command name TWICE, so an ``assert command in err``
    would acquit that mutation; equality against the derived refusal, plus the negative half below,
    is what reddens.
    """
    module = importlib.import_module(module_name)

    with pytest.raises(SystemExit) as exc:
        module.main([*_VALID_ARGV[command], "--nope"])
    refusal = _normalized(capsys.readouterr().err)

    assert exc.value.code == 2
    assert refusal == _derived_refusal(_report_engine_absent, command, capsys)
    assert "unrecognized arguments" not in refusal, "argparse answered instead of the engine"


@pytest.mark.parametrize(("module_name", "command"), _COMMANDS)
def test_a_usage_error_on_a_BROKEN_engine_exits_one_not_two(
    module_name: str,
    command: str,
    engine_present_but_broken: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The engine decides the exit code, so one cause keeps one code (project decision, 2026-07-31).

    A usage error would be argparse's 2. Here the engine is INSTALLED and will not load, which
    ``cli_common`` reports as 1, and that is the code the same command returns on the same install
    with correct arguments. Reporting 2 would give the same broken engine two different codes
    depending on whether the reader also mistyped something.

    ``docs/tools.md`` states this to users, and a documented promise with no guard is the class this
    repository refuses. Red-proof: return ``_USAGE_ERROR`` unconditionally from the override.
    """
    module = importlib.import_module(module_name)

    with pytest.raises(SystemExit) as exc:
        module.main([*_VALID_ARGV[command], "--nope"])
    refusal = _normalized(capsys.readouterr().err)

    assert exc.value.code == 1, "an installed engine that will not load is a failure, not usage"
    assert refusal == _derived_refusal(
        lambda name: _report_engine_broken(name, _BROKEN_ENGINE_CAUSE), command, capsys
    )


@pytest.mark.parametrize(("module_name", "command"), _COMMANDS)
def test_help_answers_without_the_engine(
    module_name: str, command: str, engine_absent: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """The whole point of QA-42: ``--help`` explains the command on an install that cannot run it.

    Before this, both commands answered ``--help`` with the engine refusal and exit 2, so the first
    thing a reader met after ``pip install`` was an instruction to install something they do not
    need in order to READ. Three of the five commands there were could explain themselves; now every
    declared command can.

    Red-proof: put ``require_display_engine`` back ahead of the parser and ``main`` RETURNS 2 with
    an empty stdout, so ``pytest.raises`` fails with DID NOT RAISE. ⚠️ This one is in-process and
    therefore blind to a re-introduced engine IMPORT (see the module docstring); its subprocess
    sibling below is the total version.
    """
    module = importlib.import_module(module_name)

    with pytest.raises(SystemExit) as exc:
        module.main(["--help"])

    assert exc.value.code == 0, f"{command} --help must answer, not refuse"
    assert f"usage: {command}" in capsys.readouterr().out


@pytest.mark.parametrize(("module_name", "command"), _COMMANDS)
def test_help_answers_with_the_engine_genuinely_unreachable(module_name: str, command: str) -> None:
    """The same claim as above, in the only environment that can actually prove it.

    The ``engine_absent`` fixture fakes ``find_spec``; it does not touch the import system, and
    ``opi_navigation`` is really installed in this checkout. So a ``DEFAULT_PV_CONTEXT_CAP`` import
    put back into the parser would build fine under the fixture and redden only in CI, which is the
    environment dependence this module's header calls a defect. Here the engine cannot be imported
    at all, so the parser has to stand on its own.

    Red-proof, measured both ways: restore ``default=DEFAULT_PV_CONTEXT_CAP`` (or its f-string in
    the help text) and the subprocess dies with ``ModuleNotFoundError`` before argparse prints
    anything.
    """
    script = BLOCKER + f"import {module_name} as m\nm.main(['--help'])\n"

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=_REPO,
        timeout=120,
        check=False,
    )

    assert "ModuleNotFoundError" not in result.stderr, (
        f"{command} needs the engine to build its parser:\n{result.stderr[-800:]}"
    )
    assert result.returncode == 0, result.stderr[-800:]
    assert f"usage: {command}" in result.stdout


@pytest.mark.parametrize(("module_name", "command"), _COMMANDS)
def test_version_answers_without_the_engine(
    module_name: str, command: str, engine_absent: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """QA-46 met QA-42 here, and the seam between them has MOVED.

    ``--version`` was added to every console command (five at the time), but on these two the
    availability check used to run before the parser existed, so the flag was unreachable on a
    core-only install. That
    limit is what QA-42 lifted, and this test is the inverted form of the one that pinned it: the
    old version asserted exit 2 and the refusal, and its own docstring predicted that a QA-42 that
    moved argument handling ahead of the check would turn it red on purpose.

    ``action="version"`` prints through ``parser.exit``, never through ``parser.error``, so
    ``DisplayEngineAwareParser`` does not intercept it. That is the mechanism, and it is why the
    flag answers while a usage error still does not.
    """
    module = importlib.import_module(module_name)

    with pytest.raises(SystemExit) as exc:
        module.main(["--version"])

    assert exc.value.code == 0, f"{command} --version must answer without the engine"
    assert capsys.readouterr().out.split() == [command, __version__]


@pytest.mark.parametrize(("module_name", "command"), _COMMANDS)
@pytest.mark.parametrize("argv", ["[]", "_VALID"], ids=["no-arguments", "valid-arguments"])
def test_the_module_imports_at_all_without_the_engine(
    module_name: str, command: str, argv: str
) -> None:
    """The actual regression, and the one a fake cannot show: the module-level import chain.

    Faking ``find_spec`` inside this process proves nothing about it, because the module is already
    imported. So this runs a SUBPROCESS whose import system cannot produce ``opi_navigation`` at
    all, which is what a package-index install looks like. Red before the fix: ModuleNotFoundError
    on import, long before any of this module's other tests get a say.

    STRENGTHENED at QA-42, not repaired: the no-argument case still runs (it now travels the
    ``error`` override, since ``--displays`` is required), and a valid-argument case joins it so the
    post-parse path is exercised in a real process too. The reason it needed strengthening is that
    its three assertions had become satisfiable without the refusal: argparse exits 2 and prints
    ``prog`` in its own message, so exit code plus command name could not tell the engine's answer
    from argparse's. The refusal text is now asserted, and it is derived rather than spelled out.
    """
    arguments = "[]" if argv == "[]" else repr(_VALID_ARGV[command])
    script = BLOCKER + f"import {module_name} as m\nsys.exit(m.main({arguments}))\n"

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=_REPO,
        timeout=120,
        check=False,
    )

    assert "ModuleNotFoundError" not in result.stderr, (
        f"{command} still dies on its import chain without the engine:\n{result.stderr[-800:]}"
    )
    assert result.returncode == 2, result.stderr[-800:]
    assert _normalized(result.stderr).startswith(
        f"{command}: needs the opi_navigation display engine"
    ), f"the refusal did not come from the engine:\n{result.stderr[-800:]}"


@pytest.mark.skipif(
    not engine_available(),
    reason="the resolved context cap comes from the engine, so this cannot run on a core-only "
    "install; CI reports it as a SKIP rather than not collecting it at all",
)
@pytest.mark.parametrize(("module_name", "command"), _COMMANDS)
@pytest.mark.parametrize(
    ("flag", "expected"), [([], None), (["--context-cap", "7"], 7), (["--context-cap", "0"], 0)]
)
def test_the_context_cap_reaches_the_request(
    module_name: str,
    command: str,
    flag: list[str],
    expected: int | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--context-cap`` is passed through, and an omitted flag resolves to the ENGINE's default.

    Nothing checked this before QA-42, and the change is exactly what made it worth checking: the
    argparse default moved from the engine constant to ``None``, so a forgotten resolution line
    would put ``None`` into a frozen dataclass, which does not validate, on a field typed ``int``.
    ``mypy --strict`` cannot see it either, because ``argparse.Namespace`` attribute access is
    ``Any``. The failure would surface deep inside the inventory, far from its cause.

    ⚠️ The ZERO case is the one that earns its keep. ``cap = args.context_cap or DEFAULT`` reads
    perfectly naturally and silently turns a deliberate ``--context-cap 0`` into the engine default;
    the two positive cases are green on that mutant and only this one reddens.

    ⚠️ THIS TEST CANNOT RUN IN CI, and that is a property of the import graph, not a choice: the
    resolved value comes from ``services/inventory_adapter``, the sole importer of the engine, and
    CI installs the core only. It lives here rather than in ``test_coverage_tool.py`` so the gap is
    a visible SKIP instead of a module CI never collects (project decision, 2026-07-31).
    """
    from epics_mcp.services import orchestration
    from epics_mcp.services.inventory_adapter import DEFAULT_PV_CONTEXT_CAP

    module = importlib.import_module(module_name)
    entry_point = "build_coverage_report" if command == "epics-coverage" else "run_crossplane"
    captured: list[object] = []

    def _capture(request: object) -> object:
        captured.append(request)
        raise SystemExit(0)  # stop before the real join; the request is all this test wants

    monkeypatch.setattr(orchestration, entry_point, _capture)

    with pytest.raises(SystemExit):
        module.main([*_VALID_ARGV[command], *flag])

    assert captured, f"{command} never reached the orchestrator"
    assert captured[0].context_cap == (  # type: ignore[attr-defined]
        DEFAULT_PV_CONTEXT_CAP if expected is None else expected
    )


@pytest.fixture
def engine_present_but_broken(monkeypatch: pytest.MonkeyPatch) -> None:
    """A half-installed engine: ``find_spec`` finds it, the import then fails.

    BOTH halves are faked, deliberately. Faking only the import made these tests pass here (engine
    present) and fail in CI (engine absent, so the absent branch answered first and the import
    probe was never reached), which is the environment dependence this module's header warns about,
    reintroduced. The everyday cause of this state is a missing transitive dependency.

    The import half is keyed on ``_ENGINE_ENTRY_POINT`` rather than on a literal module name, so
    moving the probe deeper into the engine cannot silently stop this fixture from biting.
    """
    real_find_spec = importlib.util.find_spec
    real_import_module = importlib.import_module

    def _find_spec(name: str, package: str | None = None) -> object | None:
        if name == "opi_navigation":
            return object()  # only tested for "is not None"
        return real_find_spec(name, package)

    def _import_module(name: str, package: str | None = None) -> object:
        if name == _ENGINE_ENTRY_POINT:
            raise _BROKEN_ENGINE_CAUSE
        return real_import_module(name, package)

    monkeypatch.setattr(importlib.util, "find_spec", _find_spec)
    monkeypatch.setattr(importlib, "import_module", _import_module)


@pytest.fixture
def engine_top_package_fine_entry_point_broken(monkeypatch: pytest.MonkeyPatch) -> None:
    """The state a top-package probe cannot see: the package imports, its entry point does not.

    This is not a hypothetical. Measured on the real engine: ``opi_navigation/__init__.py`` is a
    docstring with no imports in it, so ``import opi_navigation`` executes nothing that a missing
    transitive dependency could break. An engine in exactly this state answered "usable" to the
    old probe, and the caller then met the bare ModuleNotFoundError.

    BOTH halves faked, for the reason the fixture above states: the engine is present in this
    checkout and absent in CI, and a test that asks the environment passes in one and fails in the
    other. Here the top-package import is faked to SUCCEED, which is the whole point.
    """
    real_find_spec = importlib.util.find_spec
    real_import_module = importlib.import_module

    def _find_spec(name: str, package: str | None = None) -> object | None:
        if name == "opi_navigation":
            return object()  # only tested for "is not None"
        return real_find_spec(name, package)

    def _import_module(name: str, package: str | None = None) -> object:
        if name == "opi_navigation":
            return object()  # the docstring-only top package: nothing to fail
        if name == _ENGINE_ENTRY_POINT:
            raise ModuleNotFoundError("No module named 'pydantic'")
        return real_import_module(name, package)

    monkeypatch.setattr(importlib.util, "find_spec", _find_spec)
    monkeypatch.setattr(importlib, "import_module", _import_module)


def test_a_broken_entry_point_behind_a_healthy_top_package_is_reported(
    engine_top_package_fine_entry_point_broken: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """Probing the TOP package proves a directory exists, not that the engine works (QA-14b).

    The top package of this engine is a docstring, so it cannot fail, so a probe that stopped there
    could only ever answer "usable". Falsified by shadowing the installed engine with a
    docstring-only package whose submodule imported a missing module: the helper returned None and
    the caller raised ModuleNotFoundError, which is the outcome the helper exists to prevent.

    Red on the pre-fix probe, which imported ``opi_navigation`` alone: it returned None and wrote
    nothing, so both assertions below fail.
    """
    code = require_display_engine("epics-coverage")
    message = capsys.readouterr().err

    assert code == 1, "an engine whose entry point will not import is broken, not usable"
    assert "No module named 'pydantic'" in message  # the real cause, named


def test_the_probe_targets_the_module_the_adapter_actually_imports() -> None:
    """The entry point is only meaningful while it is the module the engine is really used through.

    ``services/inventory_adapter.py`` states that it is the sole importer of the engine, and the
    probe's affordability argument rests on that being true: one name, not a list of every
    submodule some CLI happens to need. If the adapter moves to a different module, this goes red
    instead of the probe quietly guarding the wrong door.
    """
    adapter = (_REPO / "src" / "epics_mcp" / "services" / "inventory_adapter.py").read_text(
        encoding="utf-8"
    )

    assert f"from {_ENGINE_ENTRY_POINT} import" in adapter, (
        f"the adapter no longer imports {_ENGINE_ENTRY_POINT}, so the availability probe is "
        "guarding a module the engine is not reached through any more"
    )


def test_an_installed_but_broken_engine_is_not_reported_as_missing(
    engine_present_but_broken: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """An engine that is PRESENT and does not import is a failure, not a missing capability (QA-14).

    The probe used to be ``find_spec`` alone. A half-installed engine passed that check, and the
    command then died in its own import line with a bare traceback: exactly the outcome this helper
    exists to prevent, one step further along. ``server.py`` had the right posture for the MCP
    surface already, logging the failure loud and keeping the core up; the CLI surface had never
    been given it.

    Red on the pre-fix code twice over: it returned ``None`` (the caller proceeded straight into the
    traceback), so both the exit code and the message assertion fail.
    """
    code = require_display_engine("epics-coverage")
    message = capsys.readouterr().err

    assert code == 1, "an installed engine that will not load is a command failure, not usage"
    assert "installed but failed to load" in message
    assert "No module named 'numpy'" in message  # the actual cause, not a guess about it
    assert "which is not installed" not in message  # NOT the absent message


def test_the_absent_and_broken_messages_do_not_share_their_advice(
    engine_present_but_broken: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """The two refusals must give DIFFERENT advice, which is the whole point of telling them apart.

    Without this, the fix could satisfy the test above by printing a second message that still ends
    in "install the engine", advice that cannot help a reader whose engine is already installed.
    The absent case keeps the install instruction and the list of commands that still work; the
    broken case must say that installing will not help.

    The absent message is taken from ``_report_engine_absent`` directly rather than by faking the
    environment a second way inside one test: both reporters are pure writers to stderr, so calling
    one is the same evidence with none of the fixture juggling.
    """
    require_display_engine("epics-coverage")
    # Whitespace-normalized: these messages are hand-wrapped for a terminal, so a phrase that
    # happens to straddle a line break is not a missing phrase.
    broken = " ".join(capsys.readouterr().err.split())

    _report_engine_absent("epics-coverage")
    absent = " ".join(capsys.readouterr().err.split())

    assert "--group displays" in absent
    assert "epics-doctor" in absent  # what still works
    assert "installing the engine will not help" in broken
    assert "which is not installed" not in broken


@pytest.mark.parametrize(("module_name", "command"), _ALL_ENTRY_POINTS)
def test_help_survives_a_legacy_encoded_console(module_name: str, command: str) -> None:
    """``--help`` must never die with a bare traceback on a cp1252 console (QA-8).

    Measured on Windows before the fix: ``epics-coverage --help`` and ``epics-crossplane --help``
    raised ``UnicodeEncodeError`` on the U+2194 arrow in their parser description whenever stdout
    was not UTF-8 (any redirected or legacy-encoded console), because ``configure_stdout()`` ran
    AFTER ``parse_args`` and argparse prints the help inside it. Most of the other help texts are
    ASCII today, so they pass either way; every declared entry point is parametrized in anyway so
    a future non-ASCII character in any of them cannot re-open the hole.

    ``PYTHONIOENCODING=cp1252:strict`` forces the legacy stream on every platform, so this runs
    red-provably on Linux CI too, not only on a Windows console.

    ⚠️ The usage line is asserted, not just the exit code, because the three negatives below are
    all satisfied by a command that prints NOTHING: a module that loses its ``__main__`` block
    exits 0 with empty output and would pass a guard about the help text without ever printing
    one. Its sibling above already asserts the same line.

    ⚠️ The exit code is pinned to 0 for every one of them. It used to accept ``(0, 2)``, because
    2 was the engine refusal that legitimately preceded parsing on a core-only install. QA-42
    abolished
    that outcome, and leaving the tolerance would have kept a standing permission for the very
    regression the ticket removed, in the ONE environment that reproduces a published install: this
    module is not in ``conftest``'s ``collect_ignore``, so CI runs it with the engine genuinely
    absent. Tightening it costs one character and makes it the cheapest permanent guard for the
    whole change. A traceback was never fine and still is not.
    """
    env = {**os.environ, "PYTHONIOENCODING": "cp1252:strict"}

    result = subprocess.run(
        [sys.executable, "-m", module_name, "--help"],
        capture_output=True,
        text=True,
        cwd=_REPO,
        timeout=120,
        env=env,
        check=False,
    )

    assert "UnicodeEncodeError" not in result.stderr, result.stderr[-800:]
    assert "Traceback" not in result.stderr, result.stderr[-800:]
    assert result.returncode == 0, result.stderr[-800:]
    assert f"usage: {command}" in result.stdout, (
        f"{command} --help exited 0 without printing a usage line: {result.stdout[:200]!r}"
    )
