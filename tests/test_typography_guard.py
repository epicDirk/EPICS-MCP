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
    _DOUBLED_HYPHEN,
    _FORBIDDEN,
    is_excepted,
    main,
    scan,
)

EM_DASH = chr(0x2014)
ELLIPSIS = chr(0x2026)
GUARD_SOURCE = _REPO / "scripts" / "check_typography.py"

# The one real exception, assembled rather than written out: spelled literally, this test file
# would trip the guard on its own commit. Which it did, on the first attempt, and that is the
# cheapest end-to-end proof the hook is wired up at all.
PATHSPEC_LINE = f"git status --porcelain{_DOUBLED_HYPHEN}src/"

# Deliberately an INDEPENDENT list, not derived from the guard. Parametrising over ``_FORBIDDEN``
# itself was the first version and it is a sham guard: deleting a rule deletes its probe, so the
# suite gets SMALLER and stays green, which is the failure mode CLAUDE.md point 8 is about. Held
# against the guard's table by :func:`test_the_rule_set_is_exactly_this`, so the two can only
# diverge loudly.
EXPECTED: tuple[str, ...] = (
    chr(0x2014),  # em dash
    chr(0x2013),  # en dash
    chr(0x2026),  # ellipsis
    chr(0x201E),  # low double quote
    chr(0x201C),  # left double quote
    chr(0x201D),  # right double quote
    chr(0x201A),  # low single quote
    chr(0x2018),  # left single quote
    _DOUBLED_HYPHEN,
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
