"""GP-4: one page carries the write posture, every other statement of it points there.

The posture ("read-only by default", the mutating tools gated off) was written out in eight
documents plus the server's own `instructions` string, and nothing held those statements
together. Eight independent copies of a safety promise are eight chances for one to age, and
`SECURITY.md` is precisely the one nobody re-measures. This guard does not forbid the repetition,
which is deliberate: `README.md` and `SECURITY.md` are the public face and a reader who finds only
a cross-reference there is worse off than before. It requires that every copy names where the full
version lives, and that a NEW copy cannot appear unnoticed.

TWO CARRIERS, because the repetition serves two different readers and folding them together would
misfile one of them:

* `docs/safety.md` carries the **promise** to an operator or an adopter: what the server does by
  default. Its own opening line already says so, and nine places in the tree already link it.
* `docs/write-gate-contract.md` carries requirement 1 of the **specification** a gate author must
  meet. Its own opening line says it is "consulted when a gate is built or reviewed, not a rule
  that applies to every session", so it is not a promise to a user and cannot stand in for one.

SCOPE, stated rather than implied, because a guard that reads wider than it says is worse than a
narrow one:

* The population is the tracked markdown (from `git ls-files`, never from disk: Windows folds
  case and a disk-based population is green here and red on the runner, the lesson
  `tests/test_doc_links.py` records) plus `src/epics_mcp/server.py`, whose `instructions` string is
  the only statement of the posture that travels over the wire to every client.
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
  table cell and teach nothing.

WHAT THIS DOES NOT CATCH, so nobody reads it as more: a statement of the posture in wording that
matches neither pattern. That is the same limit every phrase-based guard has, and the reason the
site set is pinned as well: a reworded copy in a NEW file still goes red on the set, even when the
phrase test cannot see it.

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

_REPO = Path(__file__).resolve().parents[1]

#: The page that carries the operator-facing promise. If it moves, move this constant with it.
_PROMISE_CARRIER = "docs/safety.md"

#: The page that carries the gate specification whose point 1 is the same fact for an author.
_SPEC_CARRIER = "docs/write-gate-contract.md"

#: The `instructions` string is the only posture statement that reaches a client with no
#: repository around it, so its pointer cannot be a file link. It names the tool instead.
_WIRE_SURFACE = "src/epics_mcp/server.py"
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

#: The decided set of files that state the posture. A file appearing here is not a defect; the
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
        _WIRE_SURFACE,  # the instructions string, over the wire
    }
)

_DECIDED_SPEC_SITES = frozenset(
    {
        "CLAUDE.md",  # the six points in one line each
        "docs/write-gate-contract.md",  # the carrier itself
    }
)

_HOW_TO_RESOLVE = (
    "Two ways out, and both are legitimate: EITHER give the new statement a pointer to "
    f"{_PROMISE_CARRIER} (or {_SPEC_CARRIER} for the specification wording) inside its own "
    "'##' section, and add its path to the set in this module with a one-line reason, OR remove "
    "the statement and let the carrier answer for it. What is not legitimate is a ninth copy "
    "nobody decided on, which is the failure this guard exists to make visible."
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


def _population() -> list[str]:
    """Every surface a reader or a client can meet the posture on."""
    return [*_tracked_markdown(), _WIRE_SURFACE]


def _read(path: str) -> str:
    return (_REPO / path).read_text(encoding="utf-8")


def _sites(pattern: re.Pattern[str]) -> dict[str, list[int]]:
    """`{path: [line numbers]}` for every line of the population matching *pattern*."""
    found: dict[str, list[int]] = {}
    for path in _population():
        hits = [
            number
            for number, line in enumerate(_read(path).split("\n"), start=1)
            if pattern.search(line)
        ]
        if hits:
            found[path] = hits
    return found


def _section_of(text: str, lineno: int) -> str:
    """The '##' section containing *lineno*, as one string.

    A section rather than the whole file, because "the others point there" is a claim about what a
    reader of THAT passage meets. A link in an unrelated section is not a pointer.
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
    """A ninth copy of a safety promise is a decision, not a side effect of an edit."""
    measured = frozenset(_sites(_PROMISE))
    assert measured, "no site matched at all, the pattern or the population broke"
    unexpected = sorted(measured - _DECIDED_PROMISE_SITES)
    vanished = sorted(_DECIDED_PROMISE_SITES - measured)
    assert not unexpected, f"new statement of the write posture in {unexpected}. {_HOW_TO_RESOLVE}"
    assert not vanished, (
        f"the write posture disappeared from {vanished}. If that was intended, remove the path "
        "from the set in this module in the same commit, so the removal is a decision on record."
    )


def test_the_set_of_specification_sites_is_the_decided_one() -> None:
    """Same question for the author-facing wording, kept apart so neither set masks the other."""
    measured = frozenset(_sites(_SPEC))
    assert measured, "no site matched at all, the pattern or the population broke"
    assert measured == _DECIDED_SPEC_SITES, (
        f"specification wording moved: measured {sorted(measured)}, "
        f"decided {sorted(_DECIDED_SPEC_SITES)}. {_HOW_TO_RESOLVE}"
    )


def test_every_promise_site_points_at_the_carrier() -> None:
    """Every statement except the carrier's own names where the full version lives."""
    offenders: dict[str, list[int]] = {}
    for path, linenos in _sites(_PROMISE).items():
        if path in (_PROMISE_CARRIER, _WIRE_SURFACE):
            continue  # the carrier answers for itself; the wire surface has its own test
        text = _read(path)
        missing = [lineno for lineno in linenos if "safety.md" not in _section_of(text, lineno)]
        if missing:
            offenders[path] = missing
    assert not offenders, (
        f"the write posture is stated without a pointer to {_PROMISE_CARRIER} in its own section: "
        f"{offenders}. {_HOW_TO_RESOLVE}"
    )


def test_every_specification_site_points_at_its_carrier() -> None:
    """The author-facing half, pointing at the specification rather than at the promise."""
    offenders: dict[str, list[int]] = {}
    for path, linenos in _sites(_SPEC).items():
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
        f"{offenders}. {_HOW_TO_RESOLVE}"
    )


def test_the_wire_surface_states_the_posture_and_carries_its_own_pointer() -> None:
    """The `instructions` string reaches a client that has no repository, so its pointer is a TOOL.

    It gets no file link on purpose, and not only because a link would be useless there: the
    header is capped at 2048 bytes (`test_build_instructions_under_2048_bytes`) and measured 1909
    at the time of writing, so bytes spent on a link a client cannot follow are bytes taken from
    the posture itself.
    """
    text = _read(_WIRE_SURFACE)
    assert _PROMISE.search(text), (
        "the server instructions no longer state the write posture. This is the ONLY statement of "
        "it that reaches a client, and no documentation page substitutes for it."
    )
    assert _WIRE_POINTER in text, (
        f"the server instructions no longer name {_WIRE_POINTER}, which is the pointer a client "
        "can actually follow to the full posture."
    )
