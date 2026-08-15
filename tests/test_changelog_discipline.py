"""GP-7: a length rule for NEW changelog entries, and it can only ever look forward.

`CLAUDE.md` already says what belongs in `CHANGELOG.md` and what does not ("Not audit runs, test
methodology, red-proof notes ..."), and that rule was written after the file had drifted into a
763-line work journal. It states a KIND, never a size, so nothing stops an entry that is nominally
user-facing and is in fact a measurement report. This module gives that existing rule a number.

FORWARD-ONLY IS THE BUILD, NOT A PROMISE. The guard reads the `## [Unreleased]` section and
nothing else. Everything already cut under a version heading is invisible to it, permanently and
without a single exemption row. That matters: `docs/known-limits.md` section 1 rejected a guard
over markdown figures precisely because it would have needed "roughly twenty historical, not
derivable rows ... a blanket exemption wearing a table's clothes". This shape needs none, because
released history is out of scope by construction rather than by a list. `[0.6.0]` is on PyPI; a
rule that reached it would be asking for a rewrite of something published.

THE CAP IS MEASURED, NOT CHOSEN. Bytes per top-level entry, produced by `entries_of` below rather
than by a separate script. That matters more than it looks: a first pass measured the same
sections with an ad-hoc counter whose treatment of the trailing blank line differed, and every
figure came out 20 to 30 B high. A byte figure is only a figure together with the code that
produced it, so these come from the function this module actually uses. **Mean and median are
ROUNDED to the nearest integer; p90 is the NEAREST RANK,** `sorted[ceil(0.9 * n) - 1]`, stated
because six textbook definitions give six answers and one of them differs by 269 B here.

    section        entries   mean   median    p90    max   above 1400
    [Unreleased]        10    828      872   1093   1233            0
    [0.6.0]             45   1015      839   1778   2021           13
    [0.5.0]             21    791      655   1342   1618            2
    [0.4.0]             28    500      534    818    958            0
    [0.3.0]             38    411      380    674   1086            0
    [0.2.0]              3    275      308    372    372            0

1400 clears every entry standing in `[Unreleased]` today (167 B of headroom above the largest),
sits above the p90 of five of the six sections, and would have stopped 13 of the 45 entries in
`[0.6.0]`, the section whose mean of 1015 against `[0.3.0]`'s 411 is what the ticket calls a
factor of 2.4.

⚠️ **THOSE 13 ARE NOT ALL "ANALYSIS", AND SAYING SO WOULD BE THE COMFORTABLE READING.** Measured,
they include one BREAKING change and four credential-disclosure fixes, which is exactly the
category `CLAUDE.md` requires a changelog to carry. The cap therefore does NOT say "this did not
belong here"; it says "this belonged here SHORTER". The honest claim is about where the detail
goes, not about whether the entry exists.

⚠️ **And where it goes is not free either.** `pyproject.toml` ships `src`, `docs` and the top-level
documents in the sdist; `tests/` and `scripts/` are excluded. Of the three alternative homes the
convention names, a package consumer reaches the operator guide and `docs/`, never a commit body
and never a guard docstring. For a fix a USER has to act on, the operator guide is the alternative
that actually arrives.

⚠️ **The rule is per ENTRY, so splitting one entry into three bullets passes it.** Measured, and it
is not worth closing: a cap on the section would punish a busy release, and a cap on the whole
file is what `docs/known-limits.md` section 1 rejected. This one makes the length of a single
claim visible; it cannot make someone write a short one.

⚠️ The number is a threshold, not a measurement of anything, so it does NOT belong to the class of
figures `tests/test_prose_counters.py` re-derives. If a later section makes it wrong, change it
here deliberately and say why in the commit; do not raise it to make one entry fit.

COUNTING RULE, spelled out because a byte figure without one is not a figure: an entry starts at a
line beginning `- ` or `* ` in column zero and runs to the next such line, the next `###`
subheading, or the end of the section. Its size is those lines joined with newlines, trailing
whitespace stripped, encoded UTF-8. Line endings therefore never count: the working copy here is
CRLF and the blob is LF, a difference of one byte per line, which on a 9-entry section is 100 B of
pure measurement artefact.

WHAT THIS DOES NOT CHECK: the KIND of an entry, which stays prose in `CLAUDE.md`, and the shape of
the section. The sibling Java repository does hold the shape (no release section carrying the same
category twice) and this section currently carries `### Changed` twice; that is reported rather
than fixed here, because it is a different question with a different owner.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_CHANGELOG = _REPO / "CHANGELOG.md"

# The house way of reaching scripts/ (tests/test_release_ready.py and
# tests/test_changelog_section.py do the same): scripts/ is not a package, and importing it as
# `scripts.x` would make mypy see the file under two module names.
sys.path.insert(0, str(_REPO / "scripts"))

from changelog_section import extract  # noqa: E402 (needs the sys.path splice above)

#: The maximum size of ONE entry under `[Unreleased]`. Derived in the module docstring.
MAX_ENTRY_BYTES = 1400

#: The heading the rule hangs off, matched as a whole line so a rename cannot slip past.
UNRELEASED_HEADING = "## [Unreleased]"

_TOP_LEVEL_ENTRY = re.compile(r"^[-*] ")
_SUBHEADING = re.compile(r"^#{3,} ")

#: ⛔ The message a RELEASE produces, and the reason it is written out rather than left to a bare
#: assert. `CONTRIBUTING.md` step 2 says "Close the [Unreleased] section under the new version",
#: which read literally means renaming the heading, and that reddens this module. The right move
#: is what both shipped releases actually did, measured at the tags: leave an EMPTY
#: `## [Unreleased]` heading standing above the new version. Without that sentence here the
#: cheapest way out of a red release is deleting the guard, which is the outcome this whole module
#: exists to prevent.
_NO_HEADING = (
    f"CHANGELOG.md has no {UNRELEASED_HEADING!r} line, so this guard reads nothing and would "
    "otherwise pass in silence. If you are cutting a RELEASE: keep an empty "
    f"{UNRELEASED_HEADING!r} heading above the new version section, which is what v0.5.0 and "
    "v0.6.0 both did, and this goes green again. If the heading was renamed for another reason, "
    "rename it here in the same commit. Removing this guard is the wrong repair in both cases."
)


def _unreleased_body(text: str) -> str:
    """The body under `[Unreleased]`, or the empty string when the section is present and empty.

    ⛔ THE POSITIVE ASSERTION COMES FIRST, AND IT IS THE POINT OF THIS FUNCTION. An empty
    `[Unreleased]` is the normal state between releases, and `extract` signals it by raising
    `SystemExit`. Catching that bare would also swallow every OTHER reason it exits: a renamed
    heading, broken markdown, a moved anchor. The guard would then be green while reading nothing
    at all, which is the failure mode a guard is least likely to be caught in. So the heading is
    proven to exist first, and only the exit that names an EMPTY section is accepted; anything else
    is re-raised as a failure that says what happened.
    """
    assert UNRELEASED_HEADING in text.split("\n"), _NO_HEADING
    try:
        return extract(text, "Unreleased")
    except SystemExit as exit_signal:
        message = str(exit_signal)
        if "empty" in message:
            return ""  # the normal state between releases
        raise AssertionError(
            f"the [Unreleased] section could not be read, and NOT because it is empty: {message}"
        ) from exit_signal


def entries_of(body: str) -> list[tuple[str, int]]:
    """`(first line, size in bytes)` for every top-level entry in *body*, in file order."""
    found: list[tuple[str, int]] = []
    current: list[str] = []

    def flush() -> None:
        if current:
            joined = "\n".join(current).rstrip()
            found.append((current[0], len(joined.encode("utf-8"))))

    for line in body.split("\n"):
        if _TOP_LEVEL_ENTRY.match(line):
            flush()
            current = [line]
        elif _SUBHEADING.match(line):
            flush()
            current = []
        elif current:
            current.append(line)
    flush()
    return found


def oversized(body: str, cap: int = MAX_ENTRY_BYTES) -> list[tuple[str, int]]:
    """Every entry of *body* above *cap*, as `(first line, size)`."""
    return [(first, size) for first, size in entries_of(body) if size > cap]


def test_the_unreleased_heading_exists() -> None:
    """The assertion that keeps every test below from passing on an unread file.

    It carries the same message as the reader, deliberately: this is the test a RELEASE trips
    over, and a bare assert there prints a diff of the changelog and no instruction at all.
    """
    assert UNRELEASED_HEADING in _CHANGELOG.read_text(encoding="utf-8").split("\n"), _NO_HEADING


def test_no_unreleased_entry_exceeds_the_cap() -> None:
    """The rule itself, on the file that will actually be released."""
    body = _unreleased_body(_CHANGELOG.read_text(encoding="utf-8"))
    too_long = oversized(body)
    assert not too_long, (
        "an entry under [Unreleased] is longer than "
        f"{MAX_ENTRY_BYTES} B: "
        + "; ".join(f"{size} B at {first[:70]!r}" for first, size in too_long)
        + ". A changelog entry says what changed for a USER, and the cap is about the LENGTH of "
        "that sentence, never about whether the entry belongs (a BREAKING change and a credential "
        "fix are among the entries it would have stopped in 0.6.0). Move the measurement that "
        "justified the change: for something a user has to act on, the operator guide is the only "
        "alternative home that ships with the package; a commit body and a guard docstring do not."
    )


def test_released_sections_are_out_of_scope() -> None:
    """The forward-only half, asserted rather than assumed.

    `[0.6.0]` is published on PyPI and carries entries above the cap. Proving that the guard does
    not see them is what makes "the rule works forward" a fact instead of an intention: if a later
    refactor pointed the check at the whole file, this test is the one that would go red.
    """
    whole = _CHANGELOG.read_text(encoding="utf-8")
    released = extract(whole, "0.6.0")
    assert oversized(released), (
        "the published 0.6.0 section no longer has an entry above the cap, so this test can no "
        "longer prove that released history is out of scope. Pick another published section that "
        "does, or retire this test with a reason."
    )
    # The counts, not the bodies: an over-cap entry in [Unreleased] reddens the cap test above,
    # and this one used to print several KB of changelog beside it, which reads as "the guard is
    # broken" rather than as "your entry is long".
    assert len(oversized(_unreleased_body(whole))) == 0, (
        "this test cannot say anything about scope while [Unreleased] itself is over the cap. "
        "Fix the entry that test_no_unreleased_entry_exceeds_the_cap names; this one is a "
        "consequence, not a second finding."
    )


def test_a_renamed_heading_is_red_rather_than_silently_green() -> None:
    """RED-PROOF for the positive assertion, and the reason it exists.

    A typo in the heading makes `extract` exit exactly as an empty section does. Without the
    heading check the guard would return "" here, find zero entries, and pass. The failure it
    would hide is total: no entry checked at all, forever, with a green gate.
    """
    broken = "# Changelog\n\n## [Unrelased]\n\n- " + "x" * (MAX_ENTRY_BYTES + 1) + "\n"
    with pytest.raises(AssertionError, match="no '## \\[Unreleased\\]' line"):
        _unreleased_body(broken)


def test_an_empty_unreleased_section_passes() -> None:
    """The one exit that IS accepted, pinned so a later tightening cannot break the normal state.

    Between a release and the next change this section is empty for days at a time.
    """
    empty = "# Changelog\n\n## [Unreleased]\n\n## [0.6.0] - 2026-08-14\n\n- something released\n"
    assert _unreleased_body(empty) == ""
    assert oversized(_unreleased_body(empty)) == []


def test_an_oversized_entry_under_unreleased_is_caught() -> None:
    """RED-PROOF for the cap: the same entry is a violation above the line and invisible below it.

    Both halves in one test on purpose. The first alone would also pass if the guard read the
    whole file; the second is what proves it does not.
    """
    entry = "- **A change.** " + "x" * MAX_ENTRY_BYTES + "\n"
    under_unreleased = f"# Changelog\n\n## [Unreleased]\n\n{entry}\n## [0.6.0]\n\n- short\n"
    under_released = f"# Changelog\n\n## [Unreleased]\n\n- short\n\n## [0.6.0]\n\n{entry}"

    assert len(oversized(_unreleased_body(under_unreleased))) == 1
    assert oversized(_unreleased_body(under_released)) == []
