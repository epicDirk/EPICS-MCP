"""Every ``-m live`` in the tracked tree is either scoped to a module or a known DESCRIPTION.

WHY THIS EXISTS, and the reason is a measurement rather than a worry
--------------------------------------------------------------------
``CONTRIBUTING.md`` states the rule: a live run always names the module it means, because
``[tool.pytest.ini_options]`` sets ``testpaths`` and declares no ``addopts``, so a run that names
no path selects EVERY live module, four of which write real entries into a logbook with no delete.

The rule carried an exemption clause for its own examples, worded "the examples in this file and
in ``pyproject.toml``". Measured on 2026-09-08 ([GQ-335]) that population was wrong by ten: nine
module docstrings and ``.github/PULL_REQUEST_TEMPLATE.md`` showed an unscoped run and sat outside
both named files, and TWO of those nine were the docstrings of modules that write into Olog
themselves, which is the exact place somebody reads while about to run that module. A clause that
names a population it does not contain is not a smaller version of the guard, it is a green light
that nothing checks, and it stood for the better part of a year.

So the prose keeps saying WHY, and this module owns WHETHER. It is the third generation of the
same lesson in this workspace: a better sentence ages, mechanics do not.

WHAT IS PROMISED, in four parts
-------------------------------
* **Population from ``git ls-files``**, not a hand-picked list. The lesson belongs to
  ``tests/test_prose_counters.py``, whose own ``_WATCHED`` IS a hand list and whose docstring
  records what that cost: a file the guard does not name is invisible however well the pattern
  fits it. ``tests/test_doc_links.py`` and ``tests/test_config_extra_spelling.py`` already resolve
  their populations this way, including the idle-run anchor that keeps an empty scan from reading
  as a clean one.
* **Scoped means: the same LINE carries a ``tests/<something>_live.py`` path**, in either order.
  Both orders occur in the tree today (``tests/test_write_gate_live.py`` puts the path last), and
  the same-line rule is enough because every scoped command in this repository fits on one line.
  A command that wraps would read as unscoped, which errs toward red.
* **Everything else must be an INVENTORIED description**, listed in :data:`_DESCRIPTIONS` with the
  reason it is one. Their VALUE is nobody's promise; their EXISTENCE is. A new unscoped
  instruction goes red, and so does an inventory entry whose text has vanished, because a frozen
  entry nobody can find is a guard measuring a file that no longer says what it says.
* **A snippet is repository TEXT, so it can collide with another text guard.** Paid for on
  2026-09-08: the first version of this inventory copied a whole line out of
  ``tests/test_guide.py``, including the retired env-var name that
  ``test_no_epics_sandbox_fiction`` forbids everywhere except the one file owning that needle.
  It stayed invisible until the commit made THIS file tracked, because that guard resolves its
  population from ``git ls-files`` as well. Keep a snippet clear of tokens another guard forbids;
  a shorter one always exists.
* **Out of scope, stated rather than implied**: this module reads TEXT. It cannot know whether a
  command anybody actually typed was scoped, and it does not look at CI workflows for a run that
  builds its arguments dynamically. It also excludes exactly one file, its own, because a module
  that must quote the forbidden form cannot be its own subject;
  ``test_the_only_exclusion_is_this_file`` pins that the exclusion stays one file wide.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]

#: The token every live-run instruction and every description of one contains.
_MARKER = "-m live"

#: A module path that scopes a run. ``<module>`` is admitted because the rule is written with that
#: placeholder in the two files that state it; the trailing ``_live.py`` is what makes it a live
#: module rather than any test file, and it is why this module's own name does not match itself.
_SCOPED = re.compile(r"tests/[A-Za-z0-9_<>]+_live\.py")

#: ``-k`` as a scoping device, refused by the rule since [GQ-335]. Measured 2026-09-08: ``-k``
#: filters on a name SUBSTRING, so ``-k alarm`` selected one module and none that writes while
#: ``-k olog`` selected four and three that write. Safety that depends on the value is not safety.
_KEYWORD_FLAG = re.compile(r"(?<!\S)-k(?=\s|=)")

#: This file, the one exclusion. See the docstring, last bullet.
_SELF = "tests/test_live_run_examples.py"

#: Mentions of the bare form that DESCRIBE it instead of instructing anybody to run it, keyed by
#: ``(tracked path, a snippet of the line)`` and never by line number: line numbers move with any
#: edit above them, the lesson ``tests/test_client_edge_guards.py`` records for its own table.
#: Each snippet must match exactly one line, which ``test_every_description_matches_one_line``
#: checks, so a snippet cannot quietly start covering a second mention.
_DESCRIPTIONS: dict[tuple[str, str], str] = {
    (
        "CONTRIBUTING.md",
        "Selection is `-m live`, and the",
    ): "names the selection mechanism, not a command to run",
    (
        "CONTRIBUTING.md",
        "a bare `-m live` falls back to the whole directory",
    ): "the rule's own statement of what the forbidden form does",
    (
        "CONTRIBUTING.md",
        "inventories every `-m live` in the",
    ): "the sentence pointing at this module",
    (
        "pyproject.toml",
        "`pytest -m live` in an empty env exited 0",
    ): "history: what S30 measured before the setup-time gate existed",
    (
        "pyproject.toml",
        "opt-in via -m live + the plane's",
    ): "the marker's help text, printed by `pytest --markers`; it names the marker, not a run",
    (
        "tests/live_gate.py",
        "``uv run pytest -m live`` in an empty environment",
    ): "history: the S30 measurement, same one as in pyproject.toml",
    (
        "tests/live_gate.py",
        "(``-m live``), it does not demand.",
    ): "states that the marker SELECTS rather than demands",
    (
        "tests/test_epics_address_ports_pinned.py",
        "which only SELECTS under ``-m live``",
    ): "explains why two modules stay out of a default run",
    (
        "tests/test_guide.py",
        "uv run pytest -m live`` runs ZERO live tests",
    ): "a statement about a sandbox posture, whose point is that it runs nothing",
    (
        "tests/test_read_live.py",
        "so a bare ``pytest -m live`` falls back to the whole directory",
    ): "the module's own warning about the forbidden form",
}


def _tracked_paths() -> list[str]:
    """Every tracked path, exactly as git spells it."""
    listing = subprocess.run(
        ["git", "-C", str(_REPO), "ls-files"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [line.strip() for line in listing.splitlines() if line.strip()]


def _mentions() -> list[tuple[str, int, str]]:
    """``(path, line number, stripped line)`` for every line carrying the marker.

    A path that is not decodable text is skipped rather than guessed at: the population comes from
    git, which tracks binaries too.
    """
    found: list[tuple[str, int, str]] = []
    for path in _tracked_paths():
        if path == _SELF:
            continue
        try:
            text = (_REPO / path).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if _MARKER not in text:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if _MARKER in line:
                found.append((path, number, line.strip()))
    return found


def _is_scoped(line: str) -> bool:
    """Whether *line* names the live module it runs."""
    return bool(_SCOPED.search(line))


def _described_by(path: str, line: str) -> tuple[str, str] | None:
    """The inventory key covering this line, or ``None``."""
    for key in _DESCRIPTIONS:
        if key[0] == path and key[1] in line:
            return key
    return None


def test_population_is_not_empty() -> None:
    """The idle-run anchor: a scan that found nothing must not read as a clean tree."""
    tracked = _tracked_paths()
    assert "CONTRIBUTING.md" in tracked, (
        f"git ls-files returned a tree without CONTRIBUTING.md ({len(tracked)} entries), the "
        "population is not what this guard thinks it is and every assertion below is vacuous"
    )
    mentions = _mentions()
    assert len(mentions) > len(_DESCRIPTIONS), (
        f"only {len(mentions)} mention(s) of {_MARKER!r} found, fewer than the "
        f"{len(_DESCRIPTIONS)} inventoried ones alone; the scan is broken, not the tree"
    )


def test_no_unscoped_instruction_survives() -> None:
    """[GQ-335]: no text in this repository tells anybody to run live without naming the module."""
    offenders = [
        f"{path}:{number}: {line}"
        for path, number, line in _mentions()
        if not _is_scoped(line) and _described_by(path, line) is None
    ]
    assert not offenders, (
        "these lines mention a live run without naming its module, and none of them is an "
        "inventoried description. Either name the module (`pytest tests/<module>_live.py -m "
        "live`) or add the line to _DESCRIPTIONS with the reason it describes rather than "
        "instructs:\n  " + "\n  ".join(offenders)
    )


def test_no_scoping_by_keyword_expression() -> None:
    """``-k`` is not a scoping device here, and this pins the decision rather than the prose."""
    offenders = [
        f"{path}:{number}: {line}"
        for path, number, line in _mentions()
        if _KEYWORD_FLAG.search(line)
    ]
    assert not offenders, (
        "a live-run line uses `-k`. It filters on a name SUBSTRING, so what it keeps out depends "
        "on the value (measured 2026-09-08: `-k alarm` selected no writing module, `-k olog` "
        "selected three of the four). Name the module instead:\n  " + "\n  ".join(offenders)
    )


def test_every_description_is_still_there() -> None:
    """A frozen entry nobody can find is a guard watching a sentence that no longer exists."""
    mentions = _mentions()
    orphans = [
        f"{path}: {snippet!r} ({reason})"
        for (path, snippet), reason in _DESCRIPTIONS.items()
        if not any(p == path and snippet in line for p, _, line in mentions)
    ]
    assert not orphans, (
        "these inventory entries match no line any more. The text moved or changed; re-read it, "
        "then either update the snippet or drop the entry:\n  " + "\n  ".join(orphans)
    )


def test_every_description_matches_one_line() -> None:
    """A snippet that matches twice would silently start covering a mention nobody inventoried."""
    mentions = _mentions()
    ambiguous = [
        f"{path}: {snippet!r} matches {hits} lines"
        for (path, snippet) in _DESCRIPTIONS
        if (hits := sum(1 for p, _, line in mentions if p == path and snippet in line)) > 1
    ]
    assert not ambiguous, "make each snippet unique within its file:\n  " + "\n  ".join(ambiguous)


def test_the_only_exclusion_is_this_file() -> None:
    """The exclusion is one file wide, and widening it has to be a visible edit."""
    this_module = f"tests/{Path(__file__).name}"
    assert this_module == _SELF, (
        f"_SELF says {_SELF!r} but this module is {this_module!r}; the excluded path and the "
        "excluding module have drifted apart, which would blind the scan somewhere else"
    )
    assert (_REPO / _SELF).is_file()
