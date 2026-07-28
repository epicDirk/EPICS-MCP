"""The German-language guard, proven able to go red, in all three of its signals.

A guard that cannot fail is the defect it was meant to remove (CLAUDE.md, evidence discipline
point 5). Here that has a sharper edge than usual, because the guard REPLACES a search that
failed silently: the first sweep used an umlaut grep and declared the repository English. Run
over the pre-translation tree (db5a83c), this guard reports 156 German lines, and **87 of them
carry no umlaut at all**: the grep was blind to more than half its subject and said nothing about
it. So the tests below do not just show the guard finding German; they show each signal finding
German the OTHER signals miss, which is the property the umlaut grep lacked and nobody noticed.

The counter-direction gets equal weight. English prose that merely looks German-ish must pass:
the MIT licence line (``mit``), a site acronym (``BIS``), and ordinary English words that end in
letters the morphology signal watches. Without those, a guard that flagged everything would sail
through the red-proof rows.

This module is skipped by the guard itself (see ``_SELF_EXEMPT``): its probes are German
sentences, and a fragment-keyed exception would have to list every one of them.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "scripts"))

from check_language import (  # noqa: E402 (needs the sys.path splice above)
    _ENGLISH_SUFFIX_LOOKALIKES,
    _SELF_EXEMPT,
    _SUFFIX_RE,
    german_signals,
    is_excepted,
    main,
    scan,
)

WORD = "German function word"
UMLAUT = "umlaut or eszett"
MORPHOLOGY = "German word formation"


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        # Signal 1 alone: no umlaut, no German suffix. This is the class the umlaut grep missed,
        # and it is 87 of the 156 German lines in the pre-translation tree, measured.
        ("# mcp bleibt explizit im Vertrag, wir nutzen das Modul direkt", [WORD]),
        # Signal 2 alone: a German word with no function word and no watched suffix.
        ("# Größe: 12 Zeichen, Rest abgeschnitten", [UMLAUT]),
        # Signal 2 via the ESZETT alone (QA-4): no umlaut, no function word, no watched suffix.
        # Without this row, dropping 0xDF from the umlaut class left the whole module green,
        # measured: the only eszett lines in the tree sit behind the casefold exception.
        ("# Straßenname: XYZ", [UMLAUT]),
        # Signal 3 alone: neither a function word nor an umlaut in sight. This signal found a real
        # line the other two walked past ("ehrlichen"), which is why it is not decoration.
        ("# Die ehrlichen Notes: glob-cap (Adapter)", [MORPHOLOGY]),
        # All three at once, the ordinary case.
        ("# Die Prüfung der Konfiguration ist fehlerhaft", [WORD, UMLAUT, MORPHOLOGY]),
    ],
)
def test_each_signal_catches_what_the_others_miss(line: str, expected: list[str]) -> None:
    """RED-PROOF per signal: disable any one of the three and its row fails on its own.

    The rows are chosen so exactly the listed signals fire, which is what makes them a proof
    about the SIGNAL rather than about the guard as a whole.
    """
    assert german_signals(line) == expected


@pytest.mark.parametrize(
    "line",
    [
        "MIT License",  # the licence, not the German preposition
        'license = { text = "MIT" }',
        "# the tree names are site-specific, DTL/BIS/FBIS, and a match needs the real one",
        "# a read-only client that config parsing hands the resolved URL",
        "# nothing here is German, and this line has to stay quiet",
        "such that the young config is rich in which nothing is flagged",
        # The QA-5 dictionary audit: English words the suffix rule matched before the lookalike
        # discard, and a URL whose host label is the German preposition. Each row blocked a
        # perfectly ordinary English commit until the fix.
        "# the CLI was hamstrung by its module-level import chain",
        "# the trap had sprung on unsung code, strung together overnight",
        "# temperatures arrive in Fahrenheit here, never in Celsius",
        "# see https://web.mit.edu/paper and www.mit.edu for the source",
    ],
)
def test_english_stays_quiet(line: str) -> None:
    """The counter-direction, without which every row above would pass on a guard that says yes
    to everything. The ALL-CAPS rows are not hypothetical: before that rule the guard reported the
    MIT licence six times and the site acronym once, measured over the whole tree.
    """
    assert german_signals(line) == []


@pytest.mark.parametrize("word", sorted(_ENGLISH_SUFFIX_LOOKALIKES))
def test_every_suffix_lookalike_stays_quiet(word: str) -> None:
    """One row per stoplist entry, so emptying or breaking the discard reddens every word it
    protected rather than whichever example happens to sit in a sample sentence."""
    assert german_signals(f"# the {word} case stays English") == []


def test_every_suffix_lookalike_is_actually_a_lookalike() -> None:
    """The stoplist's population anchor (MU): an entry that no longer matches ``_SUFFIX_RE`` is a
    stale exception that silently widens the discard, so it goes red here instead. This also
    keeps the list honest against a future suffix change: tightening the regex must shrink the
    list in the same commit."""
    stale = sorted(word for word in _ENGLISH_SUFFIX_LOOKALIKES if not _SUFFIX_RE.fullmatch(word))

    assert stale == [], f"stoplist entries the suffix rule would never flag anyway: {stale}"


def test_the_whole_english_tree_is_quiet() -> None:
    """The strongest counter-direction available: the repository as it stands must be silent.

    A false-positive rate above zero on tens of thousands of lines of real prose would make the
    hook unusable, and no hand-written sample can show that. The three self-exempt files are
    excluded here exactly as ``main`` excludes them, so this measures the guard as it runs.
    """
    sources = [
        path
        for directory in ("src", "tests", "scripts")
        for path in (_REPO / directory).rglob("*.py")
        if path.resolve() not in _SELF_EXEMPT
    ]
    assert len(sources) > 100, f"only {len(sources)} files scanned, the walk found too little"

    assert [hit for path in sources for hit in scan(str(path), _live_exceptions())] == []


def _live_exceptions() -> list[tuple[str, str]]:
    """The committed exception list, so this test measures the guard as it actually runs."""
    from check_language import load_exceptions

    return load_exceptions()


def test_the_exception_needs_both_halves() -> None:
    """A path alone or a fragment alone must not free a line, so the file stays a list of decided
    cases rather than a blanket."""
    exceptions = [("tests/test_safety.py", 'probe = "SIM:PS-01')]
    german = "# ein deutscher Kommentar"

    assert is_excepted("tests/test_safety.py", 'probe = "SIM:PS-01 blah"', exceptions)
    assert not is_excepted("tests/test_safety.py", german, exceptions)  # right file, wrong line
    assert not is_excepted("src/other.py", 'probe = "SIM:PS-01 blah"', exceptions)  # wrong file


def test_the_committed_exceptions_all_still_bite() -> None:
    """Every exception must still match something, or it is a stale entry that silently widens.

    Checked against the real files, so a translated or deleted line turns its exception into a
    failure here instead of leaving a dead rule behind that nobody notices. A vanished FILE is a
    stale entry too, which is why the missing case is reported rather than skipped.
    """
    stale: list[str] = []
    for suffix, fragment in _live_exceptions():
        target = _REPO / suffix
        if not target.exists():
            stale.append(f"{suffix}::{fragment} (file is gone)")
        elif not any(fragment in line for line in target.read_text(encoding="utf-8").splitlines()):
            stale.append(f"{suffix}::{fragment} (no line matches)")

    assert stale == [], f"exception entries that no longer bite: {stale}"


def test_the_guard_skips_the_three_files_that_spell_german_on_purpose() -> None:
    """Named rather than implied: this test, the guard and the exception list are all German by
    construction, and each would otherwise block its own commit.

    The three are named as PATHS and each is asserted to exist: a set of names that no longer point
    at anything is an exemption for nothing, and it would read exactly like a working one.
    """
    expected = {
        _REPO / "scripts" / "check_language.py",
        _REPO / "scripts" / ".language-exceptions",
        _REPO / "tests" / "test_language_guard.py",
    }

    assert {path.resolve() for path in expected} == _SELF_EXEMPT
    assert [path for path in expected if not path.exists()] == []


def test_a_namesake_elsewhere_is_not_exempt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The exemption is keyed on the file, not on its name (QA-10).

    Keyed on the bare name, ANY file called ``test_language_guard.py`` was exempt wherever it sat,
    so a second one, in another package or a vendored tree, would have been silently free of the
    guard. This is the counter-probe for that: a namesake outside the exempted path is scanned like
    any other file. Red on the pre-fix code, where ``main`` compared ``Path(path).name``.
    """
    namesake = tmp_path / "test_language_guard.py"
    namesake.write_text("# und noch eine deutsche Zeile\n", encoding="utf-8")

    assert main(["check_language.py", str(namesake)]) == 1
    assert f"{namesake}:1:" in capsys.readouterr().err


def test_the_real_exempt_file_stays_exempt_whatever_the_path_form() -> None:
    """The other half of QA-10: the three files themselves must stay silent however the path is
    spelled. pre-commit hands over a repo-relative path, a manual run an absolute one, and a
    ``rglob`` walk a third form again. Resolving both sides buys that indifference; comparing
    strings against one canonical spelling would not, and the failure would look like the guard
    working.
    """
    canonical = _REPO / "tests" / "test_language_guard.py"
    detoured = _REPO / "tests" / ".." / "tests" / "test_language_guard.py"

    assert main(["check_language.py", str(canonical)]) == 0
    assert main(["check_language.py", str(detoured)]) == 0


def test_main_blocks_and_reports_the_location(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The exit code is what pre-commit acts on, and the message must say WHERE."""
    dirty = tmp_path / "dirty.py"
    dirty.write_text("# fine\n# und noch eine deutsche Zeile\n", encoding="utf-8")
    clean = tmp_path / "clean.py"
    clean.write_text("# nothing to see\n", encoding="utf-8")

    assert main(["check_language.py", str(clean), str(dirty)]) == 1
    assert main(["check_language.py", str(clean)]) == 0

    reported = capsys.readouterr().err
    assert f"{dirty}:2:" in reported
    assert str(clean) not in reported
