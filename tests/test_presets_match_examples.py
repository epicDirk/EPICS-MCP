"""Drift guard: the ``sandbox`` preset and ``examples/mcp.json`` describe the same setup.

The two exist for different readers. ``examples/mcp.json`` is what someone finds by browsing the
repository; ``epics-init --preset sandbox`` is what they get by running the command. Both claim to
be "the localhost-only configuration", so if they ever disagreed, one of them would be teaching a
newcomer something the other contradicts, and nothing would say which.

This is the shape ``test_examples_match_entry_points.py`` already uses for the same class of
problem: two places state one fact, so a test states that they agree.

WHAT IS ASSERTED, precisely, because the loose version of this promise is unbuildable. Not that the
two are byte-identical: a preset is a mapping and the file is serialised JSON, so bytes depend on
indentation and the trailing newline, and pinning those would fail on a reformat that changed
nothing. What is pinned is the PARSED document, which is the level at which the two actually make
the same claim (the same lesson as decision NS: an acceptance criterion has to say which part of
the output it guarantees).

Neither side is the authority. Both are declarations of the same intent, so this test says they
agree rather than deriving one from the other; whoever changes one changes both. The one place
there IS an authority is the console command, which ``[project.scripts]`` decides, and that is
asserted separately below.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from epics_mcp.presets import PRESETS, SERVER_COMMAND, SERVER_KEY, render_client_config

_ROOT = Path(__file__).resolve().parent.parent
_EXAMPLE = _ROOT / "examples" / "mcp.json"
_PYPROJECT = _ROOT / "pyproject.toml"


def _example_document() -> dict[str, object]:
    """The checked-in example client config, parsed."""
    document = json.loads(_EXAMPLE.read_text(encoding="utf-8"))
    assert isinstance(document, dict), "the example is not a JSON object, the test anchor broke"
    return document


def test_the_sandbox_preset_renders_the_checked_in_example() -> None:
    """The whole document, so a divergence anywhere in it is caught: the server key, the command,
    and every variable with its value."""
    rendered = json.loads(render_client_config(PRESETS["sandbox"].env))

    assert rendered == _example_document()


def test_the_example_is_keyed_the_way_the_presets_are() -> None:
    """Pinned on its own so a mismatch reports WHICH half moved. Without it, renaming the server
    key would fail the test above with a whole-document diff and leave the reader to find the one
    changed line."""
    servers = _example_document()["mcpServers"]

    assert isinstance(servers, dict)
    assert list(servers) == [SERVER_KEY]


def test_the_emitted_command_is_one_the_project_installs() -> None:
    """Here there IS an authority: ``[project.scripts]`` is what actually gets installed, so a
    preset naming anything else emits a configuration that cannot start.

    This is the failure ``test_examples_match_entry_points.py`` was written for, arriving through a
    second door: that guard reads the example FILE, and it would not see a preset that renders a
    stale command into a file the user writes themselves.
    """
    declared = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))["project"]["scripts"]

    assert SERVER_COMMAND in declared
