#!/usr/bin/env python3
"""commit-msg guard: the commit MESSAGE is a leak surface no file guard can see.

WHY THIS EXISTS, MEASURED (GB-33). ``test_guide.py`` scans the files ``git ls-files`` reports, and
those are clean. Commit metadata is outside that population by construction, and nothing had ever
pointed at it: 507 of 563 commits carried a facility identity in the signature, and some bodies
carried real device names, on a PUBLIC repository. The signature half is fixed by configuring the
clone; this closes the message half, and it stops the growth rather than healing the past, because
the history cannot be rewritten (tags and published releases point at the commit ids).

IT INVENTS NO DETECTOR. Both needles already exist and are imported, never copied:

  * ``check_no_ess_internal.load_patterns()`` - secret formats plus the git-ignored site patterns,
  * ``pv_leak_scan.pv_leak_tokens()`` - the structural device/PV-name detector, shared with
    ``tests/test_guide.py``, which is what keeps the two surfaces on one rule.

⚠️ THE HOOK IS NOT INSTALLED BY CLONING, AND NO TEST CAN PROVE THAT IT IS. Wiring it lives in
``.pre-commit-config.yaml`` (``default_install_hook_types``), and a test can hold that wiring; what
a test cannot hold is a file in ``.git/hooks``, which is per-clone state outside the tree. A FRESH
CLONE HAS THIS GUARD ONLY AFTER::

    uv run pre-commit install --hook-type commit-msg

Until somebody types it, this script is inert on that machine and the commit-message surface is
unguarded there. It is stated here, in CONTRIBUTING.md and in the Definition of Done rather than
assumed, because a guard believed to be running is worse than one known to be absent.

WHAT IS DELIBERATELY NOT CHECKED: language and typography. ``check_language.py`` and
``check_typography.py`` police the tracked TREE; a German word or an em dash in a commit subject is
a style question, not a leak, and widening this hook to them would make it noisy on the surface
where a block is most disruptive.

Output discipline follows the sibling guard: a hit is reported as ``line N: label``, never with the
matched text, so the guard's own output cannot leak what it found.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from check_no_ess_internal import load_patterns
from pv_leak_scan import pv_leak_tokens

#: Git strips comment lines before it stores the message, so scanning them would judge text that
#: never becomes part of any commit. That matters here rather than being a nicety: the template git
#: writes lists the staged paths, and an absolute path in that list legitimately trips the site
#: patterns' username/drive arms. Reading the default comment character only; a clone that has
#: reconfigured ``core.commentChar`` gets a stricter scan, never a blinder one.
_COMMENT_LINE = re.compile(r"^\s*#")

#: Everything below a scissors line is git's own diff/status dump, likewise removed before the
#: commit is written. `--verbose` puts a full diff there, which would otherwise be scanned as if
#: the author had typed it.
_SCISSORS = re.compile(r"^\s*#\s*-+\s*>8\s*-+")


def message_lines(text: str) -> list[tuple[int, str]]:
    """The lines of *text* that will really become the commit message, with their line numbers.

    Numbers are those of the file the author edited, not of the filtered result, so a reported
    position can be found in the editor.
    """
    kept: list[tuple[int, str]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if _SCISSORS.match(line):
            break
        if not _COMMENT_LINE.match(line):
            kept.append((number, line))
    return kept


def findings(text: str) -> list[str]:
    """Every reason *text* must not become a commit message, as ``line N: label``.

    Pure, so both directions are provable from literals: the red cases and the counter-direction
    are asserted in ``tests/test_commit_message_guard.py`` without a git repository, which is what
    lets CI check a hook CI never runs (``pre-commit run --all-files`` drives the pre-commit stage
    only).
    """
    patterns = load_patterns()
    reported: list[str] = []
    for number, line in message_lines(text):
        for label, pattern in patterns:
            if pattern.search(line):
                reported.append(f"line {number}: {label}")
        if pv_leak_tokens(line):
            reported.append(f"line {number}: device/PV name (not a declared synthetic placeholder)")
    return reported


def main(argv: list[str]) -> int:
    """Scan each message file named in *argv*; return 1 (block the commit) on any finding."""
    reported: list[str] = []
    for path in argv[1:]:
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            # A message file that cannot be read is a finding, not a pass: staying silent here
            # would turn every environment problem into an unguarded commit.
            sys.stderr.write(f"cannot read commit message file {path}: {exc}\n")
            return 1
        reported.extend(findings(text))
    if reported:
        sys.stderr.write(
            "Blocked: the commit MESSAGE carries a secret, a site-internal identifier or a real "
            "device/PV name. This repository is public, and a message cannot be corrected after "
            "the fact the way a file can, because tags and published releases pin the commit ids.\n"
            "Rewrite the line (use a synthetic placeholder such as SIM:PS-01:Cur-RB):\n"
        )
        for finding in reported:
            sys.stderr.write(f"  {finding}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
