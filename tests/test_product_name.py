"""QA-24: the product is called "EPICS MCP", and the old name cannot come back.

The name "EPICS PV MCP Server" was still standing at ``README.md:1`` after decision NH had renamed
the visible identities, which is to say it was still the title of the package index project page.
Measured at the time: 21 places, 7 of them inside the published wheel. The reason it is wrong is
stated by the rename itself, in the changelog for 0.3.0: the distribution became ``epics-mcp``
because "the PV plane is one of six", and a title saying PV contradicts that in its own words.

Nothing mechanical saw it, and that is the interesting part. A stale NAME is invisible to every
guard in this suite: the resource guard reads URIs, the entry-point guard reads backticked
``epics-*`` commands, ``test_prose_counters`` reads Python, and the language and typography hooks
read form rather than truth. So the guard is this file, and it works in two directions, because
either one alone lets a rename land halfway:

* the dead string appears in no tracked file except the two that keep it ON PURPOSE, and
* the three load-bearing titles carry the canonical name EXACTLY, not as a substring.

Exactness matters in the second direction. Under a containment check, "# EPICS MCP Server" passes
while still carrying the shape that was removed, which CLAUDE.md's evidence discipline point 4 names
as a defect rather than a style choice.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]

PRODUCT = "EPICS MCP"
_DEAD = "EPICS PV MCP"

# This file states the dead name in order to search for it, so it exempts itself. It gets NO anchor
# assertion below: an anchor on a self-exempt file asserts the guard's own source against itself and
# can never go red, which is the sham guard this repository audits for.
_SELF = "tests/test_product_name.py"

# Kept on purpose, each with the reason, because an exception without one becomes a blanket
# permission the moment nobody remembers what it was for.
_KEEPS_THE_DEAD_NAME = {
    "CHANGELOG.md": (
        "a released entry naming the baseline this project grew out of. History records what was "
        "true then; editing it to match a later decision would make the changelog a summary of the "
        "present rather than a record"
    ),
    "tests/test_release_ready.py": (
        "the synthetic long description of a METADATA fixture, standing in for any project page. "
        "The release gate only checks that the body is non-empty and typed, never what it says"
    ),
}


def _tracked_text() -> dict[str, str]:
    """Every tracked file, as text, with the ones that are not UTF-8 dropped.

    Its own population rather than the one ``tests/test_guide.py`` builds, deliberately: that
    helper's suffix list carries neither ``.db`` nor ``.bob``, and its exclusions drop
    ``CITATION.cff`` outright. Inheriting it would have left four of the nineteen renamed sites
    unwatched, one of them a title this file pins below.
    """
    listing = subprocess.run(
        ["git", "-C", str(_REPO), "ls-files"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    # A successful git call can still return an empty listing, and an empty population makes every
    # assertion below pass while checking nothing. Anchored on a file that must exist.
    assert "README.md" in listing, (
        f"git ls-files returned a tree without README.md ({len(listing)} entries), the population "
        "anchor broke; the assertions in this module would pass vacuously"
    )

    files: dict[str, str] = {}
    for name in listing:
        path = _REPO / name
        if not path.is_file():
            continue
        try:
            files[name] = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
    return files


def test_the_dead_product_name_appears_nowhere_it_was_not_declared() -> None:
    """The rename cannot land halfway, and it cannot creep back later."""
    files = _tracked_text()
    offenders = {
        name: [i for i, line in enumerate(text.splitlines(), 1) if _DEAD in line]
        for name, text in files.items()
        if _DEAD in text and name not in _KEEPS_THE_DEAD_NAME and name != _SELF
    }

    assert not offenders, (
        f"{_DEAD!r} is back in {sorted(offenders)}, at these lines: {offenders}. The product is "
        f"called {PRODUCT!r}; use that, or add the file to _KEEPS_THE_DEAD_NAME with the reason"
    )


def test_each_declared_exception_still_carries_the_dead_name() -> None:
    """An exception list rots into a blanket permission the moment its entries stop applying.

    Both entries below are exempted because they hold the old name for a stated reason. If one of
    them stops holding it, the entry no longer describes anything and simply widens the guard, so
    the entry itself is checked rather than trusted. Same shape as
    ``tests/test_language_guard.py::test_the_committed_exceptions_all_still_bite``.
    """
    files = _tracked_text()
    for name, why in _KEEPS_THE_DEAD_NAME.items():
        assert name in files, f"{name} is gone; drop its exception rather than leaving it standing"
        assert _DEAD in files[name], (
            f"{name} no longer contains {_DEAD!r}, so its exception ({why}) protects nothing and "
            "now only widens the guard. Remove the entry"
        )


def _first_line(text: str) -> str:
    return text.splitlines()[0]


def _the_title_key(text: str) -> str:
    keys = [line for line in text.splitlines() if line.startswith("title:")]
    assert len(keys) == 1, f"expected exactly one title: key, found {keys}"
    return keys[0]


# path, why this line is load-bearing, how to find it, what it must say EXACTLY.
_TITLES: tuple[tuple[str, str, Callable[[str], str], str], ...] = (
    (
        "README.md",
        "the H1, and README is the long_description, so this is the index project page title",
        _first_line,
        f"# {PRODUCT}",
    ),
    (
        "CITATION.cff",
        "the title strangers cite the work under, which cannot be corrected in their bibliography",
        _the_title_key,
        f'title: "{PRODUCT}"',
    ),
    (
        "src/epics_mcp/operator_guide.md",
        "the first line of the guide that ships in the wheel and is served to every session",
        _first_line,
        f"# {PRODUCT}: Operator Guide (`epics-pv://guide`)",
    ),
)


def test_the_load_bearing_titles_carry_the_product_name_exactly() -> None:
    """The other direction: the dead name being absent does not mean the right one is present.

    Compared as whole lines against one constant. A containment check would accept
    "# EPICS MCP Server", which is the shape the rename removed, and would therefore report success
    for a half-finished rename.
    """
    files = _tracked_text()
    for name, why, locate, expected in _TITLES:
        assert name in files, f"{name} is missing, and it carries {why}"
        actual = locate(files[name])
        assert actual == expected, (
            f"{name} carries {why}, so its title is pinned. Expected {expected!r}, found "
            f"{actual!r}. If the product is being renamed again, change PRODUCT here and every "
            "line this module pins, in one commit"
        )
