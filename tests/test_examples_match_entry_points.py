"""Drift guard: every console command the project advertises must be one it installs (QA-18b).

Measured drift, which is why this exists. ``examples/mcp.json`` launched the server as
``epics-pv-mcp``. That command was the pre-0.3.0 name, kept as an alias in 0.3.0 and REMOVED in
0.4.0, so the example told a reader to run something no installation provides. It survived two
renames untouched for one reason: nothing in the repository references the file, so no reader and
no test ever looked at it. The same stale name sat in the bug-report template's environment
question, where every reporter reads it.

The authority is ``[project.scripts]`` in ``pyproject.toml``, because that is what actually gets
installed. Two directions, the same shape as ``test_readme_resources.py``:

1. Every ``command`` in an example client config is a declared console script. This is the
   direction that was found broken.
2. No documentation page advertises an ``epics-*`` command that is not declared. Wider than the
   examples, because the template proved the drift is not confined to them.

``CHANGELOG.md`` is excluded from direction 2 by design, not by convenience: a changelog RECORDS
removed commands, so naming ``epics-pv-mcp`` there is correct and must stay possible. Every other
page describes the software as it is now.

Honest limit: this matches commands written in backticks, which is how this repository writes them
throughout (measured: the sweep that motivated this guard found every real occurrence that way). A
command written as bare prose would not be seen. Widening the pattern to bare words would match
ordinary English after ``epics-``, which is a worse trade than the gap it closes.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_PYPROJECT = _ROOT / "pyproject.toml"
_EXAMPLES = _ROOT / "examples"

#: A console command as this repository writes one: in backticks, prefixed ``epics-``.
_COMMAND_IN_PROSE = re.compile(r"`(epics-[a-z][a-z0-9-]*)`")


def _declared_scripts() -> set[str]:
    """The console commands an install actually creates, read from the packaging authority."""
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    return set(data["project"]["scripts"])


def _documentation_pages() -> list[Path]:
    """Every user-facing page EXCEPT the changelog, which names removed commands on purpose."""
    top_level = ("README.md", "OPERATING.md", "SECURITY.md", "ARCHITECTURE.md", "CONTRIBUTING.md")
    pages = [_ROOT / name for name in top_level]
    pages.extend(sorted((_ROOT / "docs").glob("*.md")))
    pages.extend(sorted((_ROOT / ".github").rglob("*.md")))
    pages.append(_ROOT / "src" / "epics_mcp" / "operator_guide.md")
    return [page for page in pages if page.is_file()]


def _example_configs() -> list[Path]:
    return sorted(_EXAMPLES.glob("*.json"))


def test_every_example_launches_a_command_that_is_installed() -> None:
    """An example client config must not name a console command no install provides.

    Red on the pre-fix tree, where ``examples/mcp.json`` said ``epics-pv-mcp``, a command
    ``[project.scripts]`` stopped declaring in 0.4.0.
    """
    declared = _declared_scripts()
    assert declared, "no [project.scripts] found, the test anchor broke"

    configs = _example_configs()
    assert configs, f"no example configs found under {_EXAMPLES}, the test anchor broke"

    wrong: dict[str, list[str]] = {}
    for config in configs:
        servers = json.loads(config.read_text(encoding="utf-8")).get("mcpServers", {})
        named = [
            entry["command"]
            for entry in servers.values()
            if isinstance(entry, dict) and "command" in entry
        ]
        assert named, f"{config.name} declares no command, the test anchor broke"
        undeclared = sorted(set(named) - declared)
        if undeclared:
            wrong[config.name] = undeclared

    assert not wrong, (
        f"example configs launch commands that no install provides: {wrong}; "
        f"declared={sorted(declared)}"
    )


def test_no_documentation_advertises_an_undeclared_command() -> None:
    """The direction the bug-report template proved is real: stale commands outside the examples.

    Red on the pre-fix tree, where ``.github/ISSUE_TEMPLATE/bug_report.md`` asked reporters for
    their ``epics-pv-mcp`` version.
    """
    declared = _declared_scripts()
    assert declared, "no [project.scripts] found, the test anchor broke"

    pages = _documentation_pages()
    assert pages, "no documentation pages found, the test anchor broke"

    invented: dict[str, list[str]] = {}
    for page in pages:
        text = page.read_text(encoding="utf-8")
        extra = sorted(set(_COMMAND_IN_PROSE.findall(text)) - declared)
        if extra:
            invented[str(page.relative_to(_ROOT)).replace("\\", "/")] = extra

    assert not invented, (
        f"documentation advertises commands the package does not install: {invented}; "
        f"declared={sorted(declared)}"
    )


def test_the_readme_names_every_installed_command_and_counts_them_right() -> None:
    """The other direction, which this file was missing: documentation -> code was checked, code ->
    documentation was not.

    ``README.md`` promises a number of commands and then lists them in brackets. Both halves are
    hand-maintained prose, and nothing read either: adding an entry point without touching the
    README left it quietly claiming one command too few, which is what happened when
    ``epics-testpv`` was added. Asserting the SET rather than the count catches a name that drifts
    as well as a number that does, and the count comes from the same set so the two cannot disagree.
    """
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    declared = _declared_scripts()
    spelled = {
        "six": 6,
        "seven": 7,
        "eight": 8,
        "nine": 9,
    }

    sentence = next(
        line for line in readme.splitlines() if "commands (`epics-" in line.replace("\n", "")
    )
    # The claim spans two source lines: the number ends the first, the list opens the second.
    index = readme.splitlines().index(sentence)
    claim = " ".join(readme.splitlines()[index - 1 : index + 3])
    listed = set(_COMMAND_IN_PROSE.findall(claim))
    claimed_number = next(
        (value for word, value in spelled.items() if f"plus {word}" in claim), None
    )

    assert listed == declared, (
        "the README's bracketed command list disagrees with [project.scripts]: "
        f"only in the README {sorted(listed - declared)}, only declared {sorted(declared - listed)}"
    )
    assert claimed_number == len(declared), (
        f"the README says 'plus <number> commands' for {claimed_number} but "
        f"{len(declared)} are declared"
    )
