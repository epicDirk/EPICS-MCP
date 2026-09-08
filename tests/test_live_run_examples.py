"""Every ``-m live`` in the tracked tree, this file excepted, is scoped or a known DESCRIPTION.

WHY THIS EXISTS, and the reason is a measurement rather than a worry
--------------------------------------------------------------------
``CONTRIBUTING.md`` states the rule: a live run always names the module it means, because
``[tool.pytest.ini_options]`` sets ``testpaths`` and declares no ``addopts``, so a run that names
no path selects EVERY live module, four of which write real entries into a logbook with no delete.

The rule carried an exemption clause for its own examples, worded "the examples in this file and
in ``pyproject.toml``". Measured on 2026-09-08 ([GQ-335]) that population was wrong by ten: nine
module docstrings and ``.github/PULL_REQUEST_TEMPLATE.md`` showed an unscoped run and sat outside
both named files, and TWO of those nine were the docstrings of modules that write into Olog
themselves, which is the exact place somebody reads while about to run that module.

The clause itself was young: ``git log -S`` dates it from 2026-08-27 (``ca64647``) to 2026-09-08
(``b049635``), twelve days. The habit it excused was not: the same command dates the oldest
unscoped instruction in this repository to 2026-06-28. A clause that names a population it does
not contain is not a smaller version of a guard, it is a green light nothing checks, and it took
twelve days to write one that already missed ten places.

So the prose keeps saying WHY and this module owns WHETHER.

WHAT IS PROMISED, in five parts
-------------------------------
* **Population from ``git ls-files``**, not a hand-picked list. The lesson belongs to
  ``tests/test_prose_counters.py``, whose own ``_WATCHED`` IS a hand list and whose docstring
  records what that cost: a file the guard does not name is invisible however well the pattern
  fits it. ``tests/test_doc_links.py`` and ``tests/test_config_extra_spelling.py`` already resolve
  their populations this way, including the idle-run anchor that keeps an empty scan from reading
  as a clean one.
* **The unit of judgement is one MENTION, and its command is the enclosing inline-code span.**
  Not the line: a line carrying a scoped command AND a bare one read as scoped as a whole, which
  errs toward GREEN, the one direction this module exists to close. A mention outside any code
  span is judged on its whole line, and that case is real rather than a fallback nobody reaches,
  ``tests/test_write_gate_live.py`` writes its command as an indented ``Run::`` block with no
  backticks. What is left of the hole: a mention outside any span, on a line that ALSO names an
  unrelated live module. Measured 2026-09-08, the tree carries no such line.
* **Scoped means the command names a live module that EXISTS**, or the placeholder the two files
  stating the rule are written with. A path pointing at nothing would satisfy the shape and help
  nobody.
* **Everything else must be an INVENTORIED description**, listed in :data:`_DESCRIPTIONS` with the
  reason it is one. Their VALUE is nobody's promise; their EXISTENCE is. A new unscoped
  instruction goes red, and so does an inventory entry whose text has vanished, because a frozen
  entry nobody can find is a guard measuring a file that no longer says what it says.
  ⚠ What the inventory canNOT hold is the JUDGEMENT that a mention describes rather than
  instructs. That is written by hand into the dict, and two entries are genuinely close to the
  line (``CONTRIBUTING.md`` naming the selection mechanism, and the marker's own help text).
* **A snippet is repository TEXT, so it can collide with another text guard.** Paid for on
  2026-09-08: the first version of this inventory copied a whole line out of
  ``tests/test_guide.py``, including the retired env-var name that
  ``test_no_epics_sandbox_fiction`` forbids everywhere except the one file owning that needle.
  It stayed invisible until the commit made THIS file tracked, because that guard resolves its
  population from ``git ls-files`` as well. Keep a snippet clear of tokens another guard forbids;
  a shorter one always exists.

OUT OF SCOPE, stated rather than implied: this module reads TEXT. It cannot know whether a command
anybody actually typed was scoped, and it does not look at a CI workflow that builds its arguments
dynamically. It excludes exactly one file, its own, because a module that must quote the forbidden
form cannot be its own subject; the exclusion is DATA (:data:`_EXCLUDED`) so that widening it is an
edit ``test_the_exclusion_stays_one_file_wide`` refuses.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import NamedTuple

_REPO = Path(__file__).resolve().parents[1]

#: The token every live-run instruction and every description of one contains.
_MARKER = "-m live"

#: A module path that scopes a run. The trailing ``_live.py`` is what makes it a live module rather
#: than any test file, and it is also why this module's own name cannot match itself.
_SCOPED = re.compile(r"tests/[A-Za-z0-9_<>]+_live\.py")

#: The placeholders the two files that STATE the rule are written with. They name no file and are
#: exempt from the existence check below; every other scoped path has to be a tracked module.
_PLACEHOLDERS = frozenset({"tests/<module>_live.py", "tests/<something>_live.py"})

#: An inline-code span, in both spellings this repository uses (markdown's single backtick and
#: reStructuredText's double one). The greedy opener plus the backreference keeps ``x`` from
#: swallowing a following `y`.
_CODE_SPAN = re.compile(r"(`+)(.+?)\1")

#: ``-k`` as a scoping device, refused by the rule since [GQ-335]. Measured 2026-09-08: ``-k``
#: filters on a name SUBSTRING, so ``-k alarm`` selected one module and none that writes while
#: ``-k olog`` selected four and three that write. Safety that depends on the value is not safety.
_KEYWORD_FLAG = re.compile(r"(?<!\S)-k(?=\s|=)")

#: This file.
_SELF = "tests/test_live_run_examples.py"

#: The population's only exclusion, as DATA rather than as a condition buried in a loop: widening
#: it is then an edit to this set, which ``test_the_exclusion_stays_one_file_wide`` refuses.
_EXCLUDED = frozenset({_SELF})

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


class Mention(NamedTuple):
    """One occurrence of the marker, with the command text it belongs to."""

    path: str
    line_number: int
    line: str
    command: str

    def located(self) -> str:
        return f"{self.path}:{self.line_number}: {self.line}"


def _tracked_paths() -> list[str]:
    """Every tracked path, exactly as git spells it."""
    listing = subprocess.run(
        ["git", "-C", str(_REPO), "ls-files"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [line.strip() for line in listing.splitlines() if line.strip()]


def _command_at(line: str, position: int) -> str:
    """The command text the mention at *position* belongs to.

    The enclosing inline-code span, or the whole line when the mention sits outside every span.
    See the second promise in the module docstring for why the line is the wrong unit.
    """
    for span in _CODE_SPAN.finditer(line):
        if span.start() <= position < span.end():
            return span.group(2)
    return line


def _mentions() -> list[Mention]:
    """Every occurrence of the marker in the tracked tree, one entry per occurrence.

    A path that is not decodable text is skipped rather than guessed at: the population comes from
    git, which tracks binaries too.
    """
    found: list[Mention] = []
    for path in _tracked_paths():
        if path in _EXCLUDED:
            continue
        try:
            text = (_REPO / path).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if _MARKER not in text:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            position = line.find(_MARKER)
            while position != -1:
                found.append(Mention(path, number, line.strip(), _command_at(line, position)))
                position = line.find(_MARKER, position + len(_MARKER))
    return found


def _is_scoped(mention: Mention) -> bool:
    """Whether this mention's own command names the live module it runs."""
    return bool(_SCOPED.search(mention.command))


def _described_by(mention: Mention) -> tuple[str, str] | None:
    """The inventory key covering this mention, or ``None``."""
    for key in _DESCRIPTIONS:
        if key[0] == mention.path and key[1] in mention.line:
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
    offenders = [m.located() for m in _mentions() if not _is_scoped(m) and _described_by(m) is None]
    assert not offenders, (
        "these lines mention a live run without naming its module, and none of them is an "
        "inventoried description. Either name the module (`pytest tests/<module>_live.py -m "
        "live`) or add the line to _DESCRIPTIONS with the reason it describes rather than "
        "instructs:\n  " + "\n  ".join(offenders)
    )


def test_no_scoping_by_keyword_expression() -> None:
    """``-k`` is not a scoping device here, and this pins the decision rather than the prose."""
    offenders = [m.located() for m in _mentions() if _KEYWORD_FLAG.search(m.command)]
    assert not offenders, (
        "a live-run command uses `-k`. It filters on a name SUBSTRING, so what it keeps out "
        "depends on the value (measured 2026-09-08: `-k alarm` selected no writing module, "
        "`-k olog` selected three of the four). Name the module instead:\n  "
        + "\n  ".join(offenders)
    )


def test_every_scoped_command_names_a_module_that_exists() -> None:
    """A path that points at nothing has the right shape and helps nobody."""
    tracked = set(_tracked_paths())
    unknown = [
        f"{m.path}:{m.line_number}: {named}"
        for m in _mentions()
        for named in _SCOPED.findall(m.command)
        if named not in _PLACEHOLDERS and named not in tracked
    ]
    assert not unknown, (
        "these commands scope to a live module that is not a tracked file (a rename, a typo, or a "
        "placeholder this guard does not know):\n  " + "\n  ".join(unknown)
    )


def test_every_description_is_still_there() -> None:
    """A frozen entry nobody can find is a guard watching a sentence that no longer exists."""
    mentions = _mentions()
    orphans = [
        f"{path}: {snippet!r} ({reason})"
        for (path, snippet), reason in _DESCRIPTIONS.items()
        if not any(m.path == path and snippet in m.line for m in mentions)
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
        if (hits := len({m.line_number for m in mentions if m.path == path and snippet in m.line}))
        > 1
    ]
    assert not ambiguous, "make each snippet unique within its file:\n  " + "\n  ".join(ambiguous)


def test_the_exclusion_stays_one_file_wide() -> None:
    """Pins the WIDTH, not just the name: an added path here is what this refuses."""
    this_module = f"tests/{Path(__file__).name}"
    assert frozenset({this_module}) == _EXCLUDED, (
        f"_EXCLUDED is {sorted(_EXCLUDED)} but the only file this guard may skip is "
        f"{this_module!r}. Every other path it skips is a place it cannot see."
    )
    assert (_REPO / this_module).is_file()
