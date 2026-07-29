"""Every console command answers ``--version``, and they all name the same version (QA-46).

Before this, ``epics-mcp`` had the flag and the four diagnostic commands did not, so the one
question a bug report always asks ("which version are you on?") could only be answered by the
command least likely to be in the reader's hand.

Two properties are pinned here, and the second is the one worth having:

* each command prints ``<its own name> <version>`` and exits 0;
* the VERSION TOKEN is identical across the commands that answer, and equals
  ``epics_mcp.__version__``.

⚠️ Not "all five print the same string": ``%(prog)s`` resolves per parser, so the five lines
differ by their first token BY DESIGN (``epics-doctor 0.4.0`` is not ``epics-mcp 0.4.0``). That
first token is asserted too, against the name ``[project.scripts]`` declares, which extends the
prog pin of QA-41 from one command to the whole set.

⚠️ **Three of the five are behaviour-checked where the display engine is absent, not five.** The
engine is deliberately absent in CI (``uv sync --extra dev --frozen``, no ``--group displays``;
``pyproject.toml`` says why), and on ``epics-coverage`` / ``epics-crossplane`` the availability
check runs BEFORE the parser is built, so ``--version`` cannot be reached there without it. Rather
than skip those two, this module asserts the OTHER outcome for them (the engine refusal, exit 2),
so it is total in both environments and the limit stays visible instead of silently untested.
Lifting that limit is QA-42, which needs the help text of ``epics-coverage`` to stop depending on
the engine.
"""

from __future__ import annotations

import contextlib
import importlib
import importlib.util
import io
import tomllib
from pathlib import Path

import pytest

from epics_mcp import __version__
from epics_mcp.cli_common import _USAGE_ERROR

_ROOT = Path(__file__).resolve().parent.parent
_PYPROJECT = _ROOT / "pyproject.toml"

#: The commands whose parser sits behind the ``opi_navigation`` availability check. Pinned as a
#: relation against the declared set below rather than as a standalone list, so a command that
#: becomes display-aware (or stops being) cannot leave this file describing the old shape.
_DISPLAY_AWARE = frozenset({"epics-coverage", "epics-crossplane"})


def _declared_entry_points() -> dict[str, str]:
    """``{command: module}`` from ``[project.scripts]``, the packaging authority.

    The same source as ``tests/test_examples_match_entry_points.py``, and for the same reason: it is
    what an install actually creates, so a sixth command added there is covered by this file the
    day it appears rather than the day somebody remembers to extend a hand-written list. That file
    needs only the keys; here the value is split as well, because the module has to be imported.
    """
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    return {
        command: target.rsplit(":", 1)[0] for command, target in data["project"]["scripts"].items()
    }


def _engine_installed() -> bool:
    """Whether ``opi_navigation`` is importable, which decides what the two display CLIs answer."""
    return importlib.util.find_spec("opi_navigation") is not None


def _run_version(module_name: str) -> tuple[int | None, str]:
    """Call ``main(["--version"])`` and return ``(exit_code, stdout)``.

    ``action="version"`` raises ``SystemExit`` from inside ``parse_args``; the engine refusal
    RETURNS an exit code instead. Both are normal answers here, so both are captured rather than
    one being treated as the failure of the other.
    """
    module = importlib.import_module(module_name)
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            code: int | None = module.main(["--version"])
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else None
    return code, out.getvalue()


def test_the_declared_commands_are_the_population_this_file_checks() -> None:
    """Anchor guard: an empty or renamed ``[project.scripts]`` must not leave this file green.

    Without it, every parametrized test below would collect zero cases and the suite would report
    success for a package that declares no commands at all.
    """
    declared = _declared_entry_points()

    assert declared, "no [project.scripts] found, the test anchor broke"
    assert set(declared) > _DISPLAY_AWARE, (
        f"display-aware commands {sorted(_DISPLAY_AWARE)} not all declared: {sorted(declared)}"
    )
    assert set(declared) - _DISPLAY_AWARE, (
        "every command is display-aware, so nothing is core-checked"
    )


@pytest.mark.parametrize("command", sorted(_declared_entry_points()))
def test_every_command_answers_version_with_its_own_name(command: str) -> None:
    """``<command> <version>``, exit 0, for every command that can reach its parser.

    Red-proof: drop ``add_version_argument`` from one command and argparse reports an unrecognised
    option (exit 2) instead; change its ``prog`` and the first token stops matching.
    """
    module_name = _declared_entry_points()[command]
    code, out = _run_version(module_name)

    if command in _DISPLAY_AWARE and not _engine_installed():
        # QA-42: the engine check returns before the parser exists, so the flag is unreachable. That
        # is the measured behaviour of a core-only install, and asserting it keeps this case honest
        # instead of skipped.
        assert code == _USAGE_ERROR, (
            f"{command} without the engine should refuse with a usage error"
        )
        assert not out, f"{command} printed something on stdout while refusing: {out!r}"
        return

    assert code == 0, f"{command} --version exited {code}"
    tokens = out.split()
    assert len(tokens) == 2, f"{command} --version printed {out!r}, expected '<command> <version>'"
    assert tokens[0] == command, (
        f"{command} --version names itself {tokens[0]!r}: prog must be the declared command name, "
        "not an interpreter path (QA-41)"
    )
    assert tokens[1] == __version__


def test_the_commands_that_answer_all_name_the_same_version() -> None:
    """The property the per-command test cannot state: the versions AGREE.

    Each command could pass the test above while reading its version from somewhere else. This reads
    them all and compares, which is what makes ``add_version_argument`` a single home rather than
    five copies that happen to agree today. ``epics_mcp.__version__`` is itself pinned to
    ``pyproject [project].version`` by ``tests/test_packaging.py``, so agreement here is agreement
    with the packaging metadata.
    """
    engine = _engine_installed()
    versions = {}
    for command, module_name in sorted(_declared_entry_points().items()):
        if command in _DISPLAY_AWARE and not engine:
            continue
        code, out = _run_version(module_name)
        assert code == 0, f"{command} --version exited {code}"
        versions[command] = out.split()[1]

    assert len(versions) >= 3, f"fewer commands answered than the core install provides: {versions}"
    assert set(versions.values()) == {__version__}, (
        f"the commands disagree about the version: {versions}"
    )
