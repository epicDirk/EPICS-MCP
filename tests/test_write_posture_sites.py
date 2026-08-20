"""GP-4: one page carries the write posture, every other statement of it points there.

The posture ("read-only by default", the mutating tools gated off) was written out in eight
documents plus the server's own `instructions` string, and nothing held those statements
together. Eight independent copies of a safety promise are eight chances for one to age, and
`SECURITY.md` is precisely the one nobody re-measures. This guard does not forbid the repetition,
which is deliberate: `README.md` and `SECURITY.md` are the public face and a reader who finds only
a cross-reference there is worse off than before. It requires that every copy names where the full
version lives, and that a NEW copy cannot appear unnoticed.

⛔ THE WIRE SURFACE IS READ AS A BUILT STRING, NEVER AS FILE TEXT, and that distinction is the
whole correctness of this module. The first version of it searched `server.py` as text. `get_guide`
occurs SEVEN times in that file and only ONE of them is inside the `instructions` string, so
deleting the pointer sentence left six matches and the test green while claiming the opposite. The
posture half had the same hole one step further out: any comment mentioning the phrase would have
satisfied it. `build_instructions()` is callable, `tests/test_server.py` and
`tests/test_guide_tool.py` both call it, and there was never a reason not to.

TWO CARRIERS, because the repetition serves two different readers and folding them together would
misfile one of them:

* `docs/safety.md` carries the **promise** to an operator or an adopter: what the server does by
  default. Its own opening line already says so, and it is linked from nine of the tracked
  markdown files (counting rule: `git ls-files "*.md"` minus `CHANGELOG.md`, one count per FILE
  that mentions the name anywhere; counting occurrences instead gives 22, and counting real
  markdown links gives 10 lines in 5 files, so the rule is what makes the figure mean anything).
* `docs/write-gate-contract.md` carries requirement 1 of the **specification** a gate author must
  meet. Its own opening line says it is "consulted when a gate is built or reviewed, not a rule
  that applies to every session", so it is not a promise to a user and cannot stand in for one.

SCOPE, stated rather than implied, because a guard that reads wider than it says is worse than a
narrow one:

* The population is the tracked markdown (from `git ls-files`, never from disk: Windows folds
  case and a disk-based population is green here and red on the runner, the lesson
  `tests/test_doc_links.py` records) plus the built `instructions` string in BOTH capability
  branches, which is the only statement of the posture that travels over the wire to every client.
* `CHANGELOG.md` is out. That costs nothing and was measured rather than assumed: under the
  patterns below it states the posture zero times. A changelog records what a release said, so a
  guard over it would need historical exemptions, the construction `docs/known-limits.md` section
  1 already rejected in writing.
* `tests/` is out. A statement of the contract inside a test module is addressed to whoever edits
  that test, which `CLAUDE.md` files as a tier 1 fact belonging beside its assertion, not on a
  shared page. `tests/test_write_gate_contract.py` restates the six points for exactly that reason.
* The pointer is required in the claim's own `##` section, not merely somewhere in the file. A
  section is the unit a reader navigates; a link three sections away is not a pointer, it is a
  coincidence. It is also NOT required on the claim's own line, which would force a link into a
  table cell and teach nothing. ⚠️ A file with no `##` heading at all has one section, the whole
  file: true today only of `README.md`, whose posture line sits above its first heading.

WHAT THIS DOES NOT CATCH, so nobody reads it as more: a statement of the posture in wording that
matches neither pattern. That is the same limit every phrase-based guard has, and it is the
likeliest cause when a site "vanishes", which is why the failure message says so before it offers
to shrink the set. The site set being pinned covers the other direction: a reworded copy in a NEW
file still goes red on the set, even when the phrase test cannot see it.

Its neighbour in `tests/test_write_gate_contract.py`,
`test_the_numeric_gated_phrase_matches_the_measured_pv_gate`, sweeps the same tree for a different
question: it checks the NUMBER in a "triple-gated" claim against the measured gate size. Deleting
the promise from all eight documents leaves it green. This module is placed beside it, not inside
it, so neither question narrows the other.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from epics_mcp.server import build_instructions

_REPO = Path(__file__).resolve().parents[1]

#: The page that carries the operator-facing promise. If it moves, move this constant with it.
_PROMISE_CARRIER = "docs/safety.md"

#: The page that carries the gate specification whose point 1 is the same fact for an author.
_SPEC_CARRIER = "docs/write-gate-contract.md"

#: The label the wire surface carries in the site set. It is NOT a file to grep: see the docstring.
_WIRE_SURFACE = "src/epics_mcp/server.py (built instructions)"

#: The pointer a client can follow. Not a file link: the header reaches a client with no repository
#: around it, and it is capped at 2048 bytes with only a handful to spare in the branch that ships
#: (`_DISPLAY_TOOLS_AVAILABLE` decides which branch that is, and
#: `tests/test_server.py::test_build_instructions_under_2048_bytes` holds the cap on both). Bytes
#: spent on a link nobody can follow would come out of the posture sentence itself.
#:
#: ⚠ NO HEAD-ROOM FIGURE IS WRITTEN HERE, and the reason is that one WAS, and it rotted. Until
#: GQ-117 this line said "11 to spare (measured 2037 ... 1909)". Those three numbers were right
#: when written on 2026-08-15 (`ba4e682`) and wrong from the next day: `6aa5836` spent +4 bytes on
#: the header, updated the copy of the figure that lives beside the string in `server.py`, and
#: left this one behind. Re-measured 2026-08-20: 2041 / 1913, seven to spare. The convention this
#: now follows is the one `server.py` states beside the string itself: a head-room figure in prose
#: drifts with every edit and nothing compares it, so measure it when the answer matters, with
#: `len(build_instructions(True).encode())` against the cap.
_WIRE_POINTER = "get_guide"

_PROMISE = re.compile(r"read-only by default|gated off by default", re.IGNORECASE)
_SPEC = re.compile(r"env(?:ironment)? on/off gate, default off", re.IGNORECASE)

#: Deliberately NOT `default off` on its own. Measured: case insensitively that also matches
#: `docs/deployment.md` (the ChannelFinder privacy allowlist), two notes in
#: `services/diagnose.py` (the Naming plane) and one in `tests/test_write_gate_contract.py`
#: (the read throttle), none of which is about the write posture. A guard whose noise outnumbers
#: its findings gets suppressed, and then it guards nothing.

#: Files excluded from the population, each with its reason in the module docstring.
_EXCLUDED = frozenset({"CHANGELOG.md"})

#: The decided set of places that state the posture. A place appearing here is not a defect; the
#: point is that the set cannot GROW without somebody deciding. Pinned by file, never by line:
#: line numbers move with any edit above them, so a table keyed on them rots for a reason that is
#: not a change in the finding.
_DECIDED_PROMISE_SITES = frozenset(
    {
        "README.md",  # landing page and the PyPI long description
        "SECURITY.md",  # the page a facility reads before approving
        "docs/safety.md",  # the carrier itself
        "docs/tools.md",  # the tool table's gate column
        "docs/deployment.md",  # bringing the server up in a new facility
        "src/epics_mcp/operator_guide.md",  # ships in the package, served as the guide
        _WIRE_SURFACE,  # the instructions string, over the wire, in both branches
    }
)

_DECIDED_SPEC_SITES = frozenset(
    {
        "CLAUDE.md",  # the six points in one line each
        "docs/write-gate-contract.md",  # the carrier itself
    }
)

_A_NEW_SITE = (
    "A new place states the write posture. Two ways out, both legitimate: EITHER give it a "
    f"pointer to {_PROMISE_CARRIER} (or {_SPEC_CARRIER} for the specification wording) inside its "
    "own '##' section, and add its path to the decided set in this module with a one-line reason, "
    "OR remove the statement and let the carrier answer for it. What is not legitimate is one more "
    "copy nobody decided on, which is the failure this guard exists to make visible."
)

_A_SITE_VANISHED = (
    "⚠️ READ THE FILE BEFORE YOU SHRINK THE SET. The likeliest cause by far is a REWORDING: the "
    "page still promises the posture in words these patterns do not match, and removing its path "
    "here would leave a live safety promise permanently unguarded. Check the file first. If it "
    "really no longer states the posture, remove the path in the same commit so the removal is on "
    "record; if it states it in new words, widen the pattern in this module instead."
)

_A_POINTER_IS_MISSING = (
    "The place is a decided one, so nothing has to be added to the set. What is missing is the "
    f"pointer: put a link to {_PROMISE_CARRIER} inside the same '##' section as the claim, so a "
    "reader of THAT passage learns where the full version lives."
)


def _tracked_markdown() -> list[str]:
    """Tracked `.md` paths, exactly as git spells them. Never a disk walk (see the docstring)."""
    listing = subprocess.run(
        ["git", "ls-files", "*.md"],
        cwd=_REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    return [path for path in listing.stdout.split("\n") if path and path not in _EXCLUDED]


def _read(path: str) -> str:
    return (_REPO / path).read_text(encoding="utf-8")


def _wire_texts() -> dict[str, str]:
    """The header as it actually goes out, in both capability branches.

    Both, because a core-only install carries the same posture and the same need to state it, and
    because a promise that survives in one branch only is a promise half the deployments never see.
    """
    return {
        "display tools registered": build_instructions(True),
        "core only": build_instructions(False),
    }


def _markdown_sites(pattern: re.Pattern[str]) -> dict[str, list[int]]:
    """`{path: [line numbers]}` for every tracked markdown line matching *pattern*."""
    found: dict[str, list[int]] = {}
    for path in _tracked_markdown():
        hits = [
            number
            for number, line in enumerate(_read(path).split("\n"), start=1)
            if pattern.search(line)
        ]
        if hits:
            found[path] = hits
    return found


def _promise_sites() -> set[str]:
    """Every place that states the promise, the wire surface included as a built string."""
    sites = set(_markdown_sites(_PROMISE))
    if all(_PROMISE.search(text) for text in _wire_texts().values()):
        sites.add(_WIRE_SURFACE)
    return sites


def _section_of(text: str, lineno: int) -> str:
    """The '##' section containing *lineno*, as one string.

    A section rather than the whole file, because "the others point there" is a claim about what a
    reader of THAT passage meets. A link in an unrelated section is not a pointer. A file with no
    '##' heading has exactly one section, which is the file.
    """
    lines = text.split("\n")
    starts = [index for index, line in enumerate(lines) if line.startswith("## ")]
    index = lineno - 1
    begin = 0
    for start in starts:
        if start <= index:
            begin = start
        else:
            break
    end = next((start for start in starts if start > index), len(lines))
    return "\n".join(lines[begin:end])


def test_both_carriers_still_state_the_posture() -> None:
    """The anchor. Without it a broken pattern reads as "every site is clean"."""
    promise = _read(_PROMISE_CARRIER)
    assert _PROMISE.search(promise), (
        f"{_PROMISE_CARRIER} no longer states the posture in the guarded wording. "
        "It is the carrier: if the wording changed, move the pattern with it."
    )
    assert "the fullest statement" in promise, (
        f"{_PROMISE_CARRIER} no longer says that it IS the carrier. That sentence is what makes "
        "the pointers below mean anything."
    )
    assert _SPEC.search(_read(_SPEC_CARRIER)), (
        f"{_SPEC_CARRIER} no longer states requirement 1 in the guarded wording."
    )


def test_the_set_of_promise_sites_is_the_decided_one() -> None:
    """One more copy of a safety promise is a decision, not a side effect of an edit."""
    measured = _promise_sites()
    assert measured, "no site matched at all, the pattern or the population broke"
    unexpected = sorted(measured - _DECIDED_PROMISE_SITES)
    vanished = sorted(_DECIDED_PROMISE_SITES - measured)
    assert not unexpected, f"unexpected: {unexpected}. {_A_NEW_SITE}"
    assert not vanished, (
        f"the write posture is no longer detectable in {vanished}. {_A_SITE_VANISHED}"
    )


def test_the_set_of_specification_sites_is_the_decided_one() -> None:
    """Same question for the author-facing wording, kept apart so neither set masks the other."""
    measured = frozenset(_markdown_sites(_SPEC))
    assert measured, "no site matched at all, the pattern or the population broke"
    unexpected = sorted(measured - _DECIDED_SPEC_SITES)
    vanished = sorted(_DECIDED_SPEC_SITES - measured)
    assert not unexpected, f"unexpected: {unexpected}. {_A_NEW_SITE}"
    assert not vanished, f"requirement 1 is no longer detectable in {vanished}. {_A_SITE_VANISHED}"


def test_every_promise_site_points_at_the_carrier() -> None:
    """Every statement except the carrier's own names where the full version lives."""
    offenders: dict[str, list[int]] = {}
    for path, linenos in _markdown_sites(_PROMISE).items():
        if path == _PROMISE_CARRIER:
            continue  # the carrier answers for itself
        text = _read(path)
        missing = [lineno for lineno in linenos if "safety.md" not in _section_of(text, lineno)]
        if missing:
            offenders[path] = missing
    assert not offenders, (
        f"stated without a pointer to {_PROMISE_CARRIER} in its own section: {offenders}. "
        f"{_A_POINTER_IS_MISSING}"
    )


def test_every_specification_site_points_at_its_carrier() -> None:
    """The author-facing half, pointing at the specification rather than at the promise."""
    offenders: dict[str, list[int]] = {}
    for path, linenos in _markdown_sites(_SPEC).items():
        if path == _SPEC_CARRIER:
            continue
        text = _read(path)
        missing = [
            lineno
            for lineno in linenos
            if "write-gate-contract.md" not in _section_of(text, lineno)
        ]
        if missing:
            offenders[path] = missing
    assert not offenders, (
        f"requirement 1 is restated without a pointer to {_SPEC_CARRIER} in its own section: "
        f"{offenders}. Put a link to it inside the same '##' section as the restatement."
    )


def test_the_wire_surface_states_the_posture_and_carries_its_own_pointer() -> None:
    """The header as a BUILT string, in both branches, because that is what reaches a client.

    ⛔ Not `server.py` as file text. That was the first version and it was a sham: `get_guide`
    appears seven times in the file and once in the header, so the pointer could be deleted with
    the test still green.
    """
    for branch, header in _wire_texts().items():
        assert _PROMISE.search(header), (
            f"the server instructions ({branch}) no longer state the write posture. This is the "
            "ONLY statement of it that reaches a client, and no documentation page substitutes "
            "for it. Edit build_instructions in src/epics_mcp/server.py."
        )
        assert _WIRE_POINTER in header, (
            f"the server instructions ({branch}) no longer name {_WIRE_POINTER}, which is the "
            "pointer a client can actually follow to the full posture. Edit build_instructions in "
            "src/epics_mcp/server.py."
        )
