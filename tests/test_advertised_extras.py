"""QA-22: nothing may tell a reader to install an extra this package does not declare.

The `all` extra was removed in 0.4.0. It resolved to exactly ``epics-mcp[dev]``, so it promised
"everything this package can do" and delivered the developer toolchain, and a reader could
reasonably expect the display-aware tools in it. Removing a name is the easy half; the hard half is
that ``uv sync --extra all`` was written in eight places, and three of them ship INSIDE the wheel:
two are the remediation lines ``epics-crossplane`` and ``epics-coverage`` print when the display
engine is missing, so the server's own diagnostic would have recommended a command that no longer
resolves.

That is why this guard reads shipped ``.py`` as well as documentation. A documentation-only check
is the shape that would have passed the three sites that matter most, and the same class already
happened once: the ``[displays]`` extra became a dependency group on 2026-07-28 and survived the
change in several files, including the operator guide that ships in the wheel.

What it catches is an INSTALL INSTRUCTION naming an extra: ``--extra <name>`` and
``epics-mcp[<name>]``. Honest limit, stated because it is easy to over-read: prose that merely
CALLS something an extra ("the ``displays`` extra") is not an install instruction and is not caught
here. That is the stale-name class, and it is checked by hand rather than mechanised, because a
guard over English prose was measured and rejected for this repository (``docs/known-limits.md``).
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_PYPROJECT = _REPO / "pyproject.toml"

# uv's flag form, and the PEP 508 bracket form anchored on this distribution's own name so a
# markdown link or a list item can never look like one.
_UV_EXTRA_RE = re.compile(r"--extra[=\s]+([A-Za-z0-9._-]+)")
_BRACKET_RE = re.compile(r"epics[-_]mcp\[([A-Za-z0-9._,-]+)\]")

_SCANNED_SUFFIXES = {".md", ".py", ".yml", ".yaml", ".json", ".example", ".cff", ".txt"}

# Excluded by ROLE, not because they happen to be inconvenient.
_NOT_ADVERTISEMENTS = {
    # The file that DECLARES the extras. Scanning it for advertisements is a category error, and
    # its comments record why `all` was removed, which necessarily names it.
    "pyproject.toml",
    # Release history: it records extras that existed and were removed, in the past tense. Same
    # exclusion, and the same reason, as the console-command guard next door.
    "CHANGELOG.md",
    # This file states the forms it searches for.
    "tests/test_advertised_extras.py",
}


def _declared_extras() -> set[str]:
    """The extras a user can actually install, read from the packaging metadata source."""
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    declared = set(data.get("project", {}).get("optional-dependencies", {}))
    assert declared, (
        "no [project.optional-dependencies] found in pyproject.toml; with an empty declared set "
        "every advertisement below would be a finding, so this is a broken anchor rather than a "
        "result. If the last extra was deliberately removed, delete this guard with it"
    )
    return declared


def _advertisements() -> dict[str, list[tuple[int, str]]]:
    """``{path: [(line number, extra name)]}`` for every install instruction naming an extra."""
    listing = subprocess.run(
        ["git", "-C", str(_REPO), "ls-files"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert "pyproject.toml" in listing, (
        f"git ls-files returned a tree without pyproject.toml ({len(listing)} entries), the "
        "population anchor broke"
    )

    found: dict[str, list[tuple[int, str]]] = {}
    for name in listing:
        if name in _NOT_ADVERTISEMENTS or Path(name).suffix not in _SCANNED_SUFFIXES:
            continue
        path = _REPO / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for match in (*_UV_EXTRA_RE.finditer(line), *_BRACKET_RE.finditer(line)):
                for extra in match.group(1).split(","):
                    found.setdefault(name, []).append((lineno, extra.strip()))
    return found


def test_the_scan_actually_finds_the_install_instructions_this_repo_has() -> None:
    """The anchor, and it comes first.

    Every assertion below is a "nothing bad was found" shape, which an expression that matches
    nothing satisfies perfectly. This repository documents its dev setup in several places and the
    two workflows sync with an extra, so a scan that returns nothing has stopped working rather
    than found a clean tree.
    """
    found = _advertisements()
    total = sum(len(hits) for hits in found.values())

    assert total >= 5, (
        f"only {total} install instruction(s) naming an extra were found across {len(found)} "
        f"files: {found}. This repository has several; the scan is no longer matching"
    )
    assert any(name.startswith("src/") for name in found), (
        f"no shipped src/ file was matched: {sorted(found)}. The wheel carries the remediation "
        "lines that recommend an install command, and those are the sites a documentation-only "
        "guard would miss, so their absence means the population shrank"
    )
    assert any(name.endswith(".md") for name in found), (
        f"no documentation page was matched: {sorted(found)}"
    )


def test_nothing_advertises_an_extra_that_is_not_declared() -> None:
    """QA-22: an install command a reader can copy has to be one that resolves."""
    declared = _declared_extras()
    offenders = {
        name: [(lineno, extra) for lineno, extra in hits if extra not in declared]
        for name, hits in _advertisements().items()
    }
    offenders = {name: hits for name, hits in offenders.items() if hits}

    assert not offenders, (
        f"these files tell a reader to install an extra that pyproject.toml does not declare "
        f"(declared: {sorted(declared)}): {offenders}. Either declare it or correct the command"
    )
