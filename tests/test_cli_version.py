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

⚠️ **All five are checked in every environment, and that is new (QA-42).** Until then, the two
display-aware commands ran their engine check BEFORE the parser was built, so ``--version`` could
not be reached on a core-only install; this module carried a branch asserting the OTHER outcome for
them (the engine refusal, exit 2) and a matching skip in the agreement test below. The engine is
deliberately absent in CI (``uv sync --extra dev --frozen``, no ``--group displays``;
``pyproject.toml`` says why), which is precisely the environment that branch described.

QA-42 moved the check behind ``parse_args``, and ``action="version"`` prints through
``parser.exit``, never through ``parser.error``, so the flag now answers everywhere. The branch and
the skip are therefore GONE rather than inverted: an environment-conditional assertion here would
have kept the two commands out of the agreement property in the one environment it matters in,
while the count floor stayed satisfied by the other three. What holds the engine-absent behaviour
of these two is ``tests/test_cli_without_display_engine.py``, which FAKES the absence and so runs
in both environments.
"""

from __future__ import annotations

import contextlib
import importlib
import io
import tomllib
from pathlib import Path

import pytest

from epics_mcp import __version__

_ROOT = Path(__file__).resolve().parent.parent
_PYPROJECT = _ROOT / "pyproject.toml"


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


def _run_version(module_name: str) -> tuple[int | None, str]:
    """Call ``main(["--version"])`` and return ``(exit_code, stdout)``.

    ``action="version"`` prints inside ``parse_args`` and raises ``SystemExit``, on all five
    commands and in every environment. The ``SystemExit`` is caught here rather than left to
    ``pytest.raises`` so the exit code and the printed line can be asserted together.
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

    It used to carry two further clauses, partitioning the declared set into display-aware and core
    commands. They went with the partition itself (QA-42): that distinction no longer changes what
    any command answers to ``--version``, and a relation kept alive past the behaviour it described
    is a claim nobody re-measures.
    """
    declared = _declared_entry_points()

    assert declared, "no [project.scripts] found, the test anchor broke"


@pytest.mark.parametrize("command", sorted(_declared_entry_points()))
def test_every_command_answers_version_with_its_own_name(command: str) -> None:
    """``<command> <version>``, exit 0, for every declared command, in every environment.

    Unconditional since QA-42. The engine-absent branch that used to sit here asserted the opposite
    outcome for the two display-aware commands, and it was DEAD CODE in any checkout that has the
    engine installed, which is every developer machine: only CI ever executed it. A branch the local
    gate cannot reach is a branch a local gate cannot prove, and that is how a red main gets pushed.

    Red-proof: drop ``add_version_argument`` from one command and argparse reports an unrecognised
    option (exit 2) instead; change its ``prog`` and the first token stops matching.
    """
    module_name = _declared_entry_points()[command]
    code, out = _run_version(module_name)

    assert code == 0, f"{command} --version exited {code}"
    tokens = out.split()
    assert len(tokens) == 2, f"{command} --version printed {out!r}, expected '<command> <version>'"
    assert tokens[0] == command, (
        f"{command} --version names itself {tokens[0]!r}: prog must be the declared command name, "
        "not an interpreter path (QA-41)"
    )
    assert tokens[1] == __version__


def test_all_declared_commands_name_the_same_version() -> None:
    """The property the per-command test cannot state: the versions AGREE.

    Each command could pass the test above while reading its version from somewhere else. This reads
    them all and compares, which is what makes ``add_version_argument`` a single home rather than
    five copies that happen to agree today. ``epics_mcp.__version__`` is itself pinned to
    ``pyproject [project].version`` by ``tests/test_packaging.py``, so agreement here is agreement
    with the packaging metadata.

    ⚠️ The floor is the DECLARED count, not a number. It used to be ``>= 3`` beside a skip for the
    two display-aware commands, and that pair was the more dangerous half of the old shape: after
    QA-42 those two answer on a core-only install too, but the skip would have gone on excluding
    them there while three core commands kept the floor satisfied. The guard would then have
    checked three of the five that answer, in the one environment reproducing a published install,
    and looked complete. Deriving the floor makes a silently shrinking population impossible.
    """
    declared = _declared_entry_points()
    versions = {}
    for command, module_name in sorted(declared.items()):
        code, out = _run_version(module_name)
        assert code == 0, f"{command} --version exited {code}"
        versions[command] = out.split()[1]

    assert len(versions) == len(declared), f"a declared command did not answer: {versions}"
    assert set(versions.values()) == {__version__}, (
        f"the commands disagree about the version: {versions}"
    )
