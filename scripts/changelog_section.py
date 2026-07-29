"""Print one version's section of CHANGELOG.md, for the GitHub release body.

Why this exists: a tag published the package but left no GitHub release behind, so the repository's
Releases page kept showing 0.2.0 as "Latest" while 0.3.0 was on the index. Creating the release
from the workflow fixes that, and a release needs a body. The alternative, `gh release
--generate-notes`, lists commit subjects, which would ignore the changelog this project actually
maintains and describe a release by its plumbing instead of by what changed for a user.

Deterministic by construction: it reads a file and slices it. No clock, no network, no git.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

#: A Keep a Changelog version heading: ``## [0.4.0] - 2026-07-29`` (the date is optional, and
#: ``## [Unreleased]`` matches too, which is what lets a caller ask for it by name).
_HEADING = re.compile(r"^## \[(?P<version>[^\]]+)\]")


def extract(text: str, version: str) -> str:
    """Return the body of *version*'s section, without its own heading. Raises if absent.

    Absent is an ERROR, never an empty string: this feeds a release body, and a silent empty result
    would publish a release that says nothing about a version somebody is about to install. The
    version is matched with or without a leading ``v`` so a caller can pass a tag name straight
    through.
    """
    wanted = version.removeprefix("v")
    lines = text.splitlines()
    start: int | None = None
    for index, line in enumerate(lines):
        match = _HEADING.match(line)
        if match is None:
            continue
        if start is not None:  # the next version heading ends the section
            return "\n".join(lines[start:index]).strip()
        if match.group("version").removeprefix("v") == wanted:
            start = index + 1
    if start is None:
        available = [m.group("version") for m in map(_HEADING.match, lines) if m]
        raise SystemExit(
            f"changelog_section: no section for {version!r}. Present: {', '.join(available)}"
        )
    return "\n".join(lines[start:]).strip()  # the last section runs to the end of the file


def main(argv: list[str] | None = None) -> int:
    # prog pinned: argparse's default is interpreter dependent and prints an absolute console
    # script path on 3.14 (QA-41). Same reason at every entry point of this package.
    parser = argparse.ArgumentParser(
        prog="changelog_section",
        description="Print one version's CHANGELOG section, for a release body.",
    )
    parser.add_argument("version", help="version or tag name, e.g. 0.4.0 or v0.4.0")
    parser.add_argument(
        "--changelog",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "CHANGELOG.md",
        help="path to the changelog (default: the one next to this repository's pyproject.toml)",
    )
    args = parser.parse_args(argv)
    sys.stdout.write(extract(args.changelog.read_text(encoding="utf-8"), args.version) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
