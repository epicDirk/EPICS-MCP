"""The typographic-character guard, proven able to go red.

A guard that cannot fail is the defect it was meant to remove (CLAUDE.md, evidence discipline
point 5), so every character the guard claims to catch is driven through it here, and the clean
case is driven too: a rule that blocked everything would pass a red-only test.

Two properties beyond "it finds the character" are load-bearing and each has its own test:

* The guard must not block ITSELF. Its forbidden characters are spelled as ``chr(0x2014)`` rather
  than written out, which is easy to undo by hand while everything still looks right; the guard's
  own source is therefore run through the guard.
* The exception must be keyed on line CONTENT, not a line number. Both halves are tested to bite,
  and, more importantly, an exception written for one file must NOT free the same text elsewhere.

The forbidden characters appear in this module only as ``chr(...)``, for the same reason they do
in the guard: written out, this test file would block its own commit.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "scripts"))

from check_typography import (  # noqa: E402 (needs the sys.path splice above)
    _FORBIDDEN,
    is_excepted,
    main,
    scan,
)

EM_DASH = chr(0x2014)
ELLIPSIS = chr(0x2026)
GUARD_SOURCE = _REPO / "scripts" / "check_typography.py"

# The doubled hyphen, spelled LOCALLY and not imported from the guard (QA-3): the first version
# imported ``_DOUBLED_HYPHEN``, which put the guard's own constant on both sides of every
# comparison, so corrupting it in the guard left this whole module green while the guard silently
# stopped catching the sequence. Assembled from parts because written out, this line would trip
# the guard on its own commit.
DOUBLED_HYPHEN = " " + "--" + " "

# The bare pair, for the line-edge table (QA-13). There the surrounding spaces are the CONTEXT
# under test, so they must not be baked into the sample the way they are above. Spelling it out is
# safe: quoted, it has a quote on either side rather than a space or a line edge, so the guard does
# not find it here. Every row of that table is assembled from THIS, never written out, because a
# row spelled literally would be a finding in this file, and nothing guards that: the self-block
# test scans the GUARD's source, not the test's.
HYPHENS = "--"

# The one real exception, assembled rather than written out: spelled literally, this test file
# would trip the guard on its own commit. Which it did, on the first attempt, and that is the
# cheapest end-to-end proof the hook is wired up at all.
PATHSPEC_LINE = f"git status --porcelain{DOUBLED_HYPHEN}src/"

# Deliberately an INDEPENDENT list, not derived from the guard. Parametrising over ``_FORBIDDEN``
# itself was the first version and it is a sham guard: deleting a rule deletes its probe, so the
# suite gets SMALLER and stays green, which is the failure mode CLAUDE.md point 8 is about. Held
# against the guard's table by :func:`test_the_rule_set_is_exactly_this`, so the two can only
# diverge loudly. Every entry is spelled here, none imported: importing one entry from the guard
# (the doubled hyphen, until QA-3) made that one entry immune to exactly the divergence this list
# exists to catch, measured by mutating the guard's constant with the suite staying green.
EXPECTED: tuple[str, ...] = (
    chr(0x2014),  # em dash
    chr(0x2013),  # en dash
    chr(0x2026),  # ellipsis
    chr(0x201E),  # low double quote
    chr(0x201C),  # left double quote
    chr(0x201D),  # right double quote
    chr(0x201A),  # low single quote
    chr(0x2018),  # left single quote
    DOUBLED_HYPHEN,
)


def test_the_rule_set_is_exactly_this() -> None:
    """RED-PROOF for the probes below: without this, removing a rule would remove its probe too.

    A deleted rule then reads as a smaller, still-green suite rather than a failure, which is
    exactly the shape of a guard that cannot go red.
    """
    assert tuple(character for character, _label in _FORBIDDEN) == EXPECTED


@pytest.mark.parametrize("character", EXPECTED)
def test_every_forbidden_character_is_found(character: str, tmp_path: Path) -> None:
    """One row per rule: stop detecting any of these characters and its row fails."""
    target = tmp_path / "sample.md"
    target.write_text(f"a line with a{character}character in it\n", encoding="utf-8")

    hits = scan(str(target), exceptions=[])

    assert len(hits) == 1, hits
    assert hits[0].startswith(f"{target}:1: ")


def test_clean_text_is_not_reported(tmp_path: Path) -> None:
    """The counter-direction: without it, a guard that flagged every line would pass the row above.

    The sample deliberately carries what the guard must TOLERATE: the typographic apostrophe, the
    arrow that holds the endpoint tables' alignment, and a single hyphen.
    """
    target = tmp_path / "clean.md"
    tolerated = f"{chr(0x2019)} {chr(0x2192)} {chr(0x00D7)}"
    target.write_text(f"it{chr(0x2019)}s read-only: endpoint {tolerated} 2\n", encoding="utf-8")

    assert scan(str(target), exceptions=[]) == []


def test_the_doubled_hyphen_is_caught_at_a_line_edge(tmp_path: Path) -> None:
    """QA-13: the needle carries spaces, but a line EDGE is the same context as a space.

    Before this, the rule could only see the form between two spaces, so it was blind exactly
    where a dash lands when a sentence wraps. The one real instance in this repository sat at a
    line end and went unreported for months.

    Both directions in one table, because a rule that fired on every line would pass a hit-only
    test. The silent rows are the forms this widening could plausibly have swallowed: a long
    hyphen run (the repository writes its section banners that way), a command-line flag, and the
    pair inside a word. Red-provable against the pre-QA-13 guard, where the first two rows fail.
    """
    cases: list[tuple[str, bool]] = [
        (f"{HYPHENS} the form leads the line", True),
        (f"the line trails off into {HYPHENS}", True),
        (f"between two spaces {HYPHENS} as before", True),
        (HYPHENS, True),
        (f"a run of three {HYPHENS}- stays silent", False),
        (f"a banner {HYPHENS}---- stays silent", False),
        (f"{HYPHENS}check is a flag, not a dash", False),
        (f"    {HYPHENS}check indented is one too", False),
        (f"glued in a{HYPHENS}word", False),
    ]
    target = tmp_path / "cases.md"
    target.write_text("\n".join(text for text, _expected in cases) + "\n", encoding="utf-8")

    hits = scan(str(target), exceptions=[])

    caught = {
        lineno
        for lineno, _case in enumerate(cases, start=1)
        if any(hit.startswith(f"{target}:{lineno}: ") for hit in hits)
    }
    expected = {lineno for lineno, (_text, is_hit) in enumerate(cases, start=1) if is_hit}
    assert caught == expected, hits
    # No row may report twice: one line, one finding, or the table above proves less than it says.
    assert len(hits) == len(expected), hits


def test_the_guard_does_not_block_itself() -> None:
    """The guard's own source must survive its own rule, which is why it spells characters as code
    points. Writing one of them out is the easy regression, and it looks perfectly fine on screen.
    """
    assert scan(str(GUARD_SOURCE), exceptions=[]) == []


def test_the_exception_needs_both_halves() -> None:
    """A path suffix alone or a fragment alone must not free a line: that is what makes the
    exception file a list of decided cases instead of a blanket."""
    exceptions = [("scripts/guard_audit.py", "status --porcelain")]

    assert is_excepted("scripts/guard_audit.py", PATHSPEC_LINE, exceptions)
    # right file, wrong line
    assert not is_excepted("scripts/guard_audit.py", f"prose{EM_DASH}with a dash", exceptions)
    # right line, wrong file: an exception must not travel to another module
    assert not is_excepted("src/other.py", PATHSPEC_LINE, exceptions)


def test_the_exception_does_not_leak_into_a_neighbouring_directory() -> None:
    """The key names a PATH, so it must not match a different path that merely ends the same way
    (QA-11).

    ``endswith`` compares characters, not path components, so the committed key
    ``scripts/guard_audit.py`` also freed ``myscripts/guard_audit.py``: an exception decided for
    one file, silently covering another in a directory nobody had looked at. Red on the pre-fix
    code, where this asserted a match.

    The counter-direction is asserted beside it, because a predicate that matched nothing would
    also pass the row above: the file the key was written for still has to be freed, and so does
    the equivalent absolute path a manual run hands over.
    """
    exceptions = [("scripts/guard_audit.py", "status --porcelain")]

    assert not is_excepted("myscripts/guard_audit.py", PATHSPEC_LINE, exceptions)
    assert not is_excepted("src/scripts_guard_audit.py", PATHSPEC_LINE, exceptions)
    assert is_excepted("scripts/guard_audit.py", PATHSPEC_LINE, exceptions)
    assert is_excepted("/repo/scripts/guard_audit.py", PATHSPEC_LINE, exceptions)


def test_both_guards_anchor_paths_the_same_way() -> None:
    """The two guards carry this predicate twice, so the pair is pinned rather than trusted.

    They are standalone pre-commit scripts with no shared module between them, and a fix applied to
    one of two identical copies is the classic half-repair: the tree would look mended while the
    other guard kept leaking exceptions. Every row below is asserted against BOTH implementations,
    so a divergence goes red here instead of being discovered by the next audit.
    """
    from check_language import path_matches as language_path_matches
    from check_typography import path_matches as typography_path_matches

    cases = [
        ("scripts/guard_audit.py", "scripts/guard_audit.py", True),
        ("/repo/scripts/guard_audit.py", "scripts/guard_audit.py", True),
        ("myscripts/guard_audit.py", "scripts/guard_audit.py", False),
        ("scripts/guard_audit.py", "guard_audit.py", True),
        ("scripts/my_guard_audit.py", "guard_audit.py", False),
        ("scripts/guard_audit.py", "", False),
    ]

    measured = [
        (path, suffix, language_path_matches(path, suffix), typography_path_matches(path, suffix))
        for path, suffix, _ in cases
    ]

    assert [(p, s, e, e) for p, s, e in cases] == measured


def test_the_exception_frees_the_line_it_names(tmp_path: Path) -> None:
    """End to end through :func:`scan`: the pathspec line survives, a real dash beside it does
    not."""
    target = tmp_path / "guard_audit.py"
    target.write_text(
        f"    print('{PATHSPEC_LINE}')\n    # a comment{EM_DASH}with a real dash\n",
        encoding="utf-8",
    )
    exceptions = [("guard_audit.py", "status --porcelain")]

    hits = scan(str(target), exceptions)

    assert len(hits) == 1, hits
    assert hits[0].startswith(f"{target}:2: ")


def test_a_binary_file_is_skipped(tmp_path: Path) -> None:
    """Scope claim, made executable: the file set is decided by the UTF-8 decode, not by an
    extension list that would need maintaining and would silently miss a new text format."""
    target = tmp_path / "payload.bin"
    target.write_bytes(b"\xff\xfe\x00binary\x00")

    assert scan(str(target), exceptions=[]) == []


def test_main_blocks_and_reports_the_location(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The exit code is what pre-commit acts on, and the message must say WHERE, not just that."""
    dirty = tmp_path / "dirty.md"
    dirty.write_text(f"first line\nsecond{ELLIPSIS}line\n", encoding="utf-8")
    clean = tmp_path / "clean.md"
    clean.write_text("nothing to see\n", encoding="utf-8")

    assert main(["check_typography.py", str(clean), str(dirty)]) == 1
    assert main(["check_typography.py", str(clean)]) == 0

    reported = capsys.readouterr().err
    assert f"{dirty}:2:" in reported
    assert str(clean) not in reported
