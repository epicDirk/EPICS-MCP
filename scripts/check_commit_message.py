#!/usr/bin/env python3
"""commit-msg guard: the commit MESSAGE is a leak surface no file guard can see.

WHY THIS EXISTS, MEASURED (GB-33). ``test_guide.py`` scans the files ``git ls-files`` reports, and
those are clean. Commit metadata is outside that population by construction, and nothing had ever
pointed at it. Both halves, each with the command that produces it, on 2026-08-15:

  * signature: ``git log --format='%ae' | sort | uniq -c`` counts 614 commits carrying the
    facility address, out of the 670 that predate the identity switch (``git rev-list --count
    <first noreply commit>^``). That figure can no longer grow; every commit since the switch
    signs with the noreply address.
  * body: running the shared detector over ``git log --format=%B`` flags 2 of 722 bodies for a
    real device name.

Configuring the clone fixed the signature half. This closes the message half, and it stops the
growth rather than healing the past, because the history cannot be rewritten (tags and published
releases point at the commit ids).

⚠️ The figure above replaced "507 of 563", which was carried over from the work item and had been
superseded by a fresh measurement a week earlier. A number without its measuring rule cannot be
rechecked and therefore drifts in exactly that way, which is why the commands are written here
rather than the results alone.

IT INVENTS NO DETECTOR. Both needles already exist and are imported, never copied:

  * ``check_no_ess_internal.load_patterns()`` - secret formats plus the site patterns,
  * ``pv_leak_scan.pv_leak_tokens()`` - the structural device/PV-name detector, shared with
    ``tests/test_guide.py``, which is what keeps the two surfaces on one rule.

⚠️ ONE OF THOSE TWO IS WEAKER HERE THAN IT IS ON THE FILE SURFACE. The site patterns come from
``scripts/.pii_patterns.local``, which is git-ignored, so a clone that does not carry it runs this
guard with the built-in secret formats and the structural detector alone, silently. On the file
surface that gap has a fallback, the committed repo-wide test that CI runs; here there is none,
because CI never reaches the commit-msg stage at all. The structural detector is the half that
does not depend on a local file, and it is the half the measured leak needed.

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
#: patterns' username/drive arms.
#:
#: ⚠️ THE ANCHOR IS COLUMN ZERO, AND THAT IS GIT'S RULE RATHER THAN A SIMPLIFICATION. An earlier
#: shape allowed leading whitespace (``^\s*#``) and was therefore BLINDER than git, not stricter:
#: measured with ``git stripspace --strip-comments`` on git 2.53, an indented ``   # note`` is
#: KEPT and reaches the commit, while that pattern skipped it. A device name written after an
#: indented hash would have been published unreported. Reading the default comment character only;
#: a clone that reconfigured ``core.commentChar`` gets a stricter scan, never a blinder one.
_COMMENT_LINE = re.compile(r"^#")

#: Everything below the scissors line is git's own diff/status dump, likewise removed before the
#: commit is written; ``--verbose`` puts a full diff there, which would otherwise be scanned as if
#: the author had typed it.
#:
#: ⚠️ THE LITERAL IS EXACT, FOR THE SAME REASON. A loose ``#\s*-+\s*>8\s*-+`` matched a hand-typed
#: ``#--->8---`` too, so one such line anywhere in a body silently switched the scan off for
#: everything below it, with no trace for the author. Measured rather than read off the source: a
#: ``git commit -v`` whose editor dumps ``.git/COMMIT_EDITMSG`` writes this line, byte for byte,
#: with and without ``--cleanup=scissors``.
_SCISSORS_LINE = "# ------------------------ >8 ------------------------"


def message_lines(text: str) -> list[tuple[int, str]]:
    """The lines of *text* that will really become the commit message, with their line numbers.

    Numbers are those of the file the author edited, not of the filtered result, so a reported
    position can be found in the editor.
    """
    kept: list[tuple[int, str]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if line.rstrip() == _SCISSORS_LINE:
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
    """Scan each message file named in *argv*; return 1 (block the commit) on any finding.

    HANDED NO FILE, THIS REFUSES rather than returning success. It looks like an over-reaction and
    is the opposite: pre-commit passes the message file only while the hook keeps
    ``pass_filenames: true``, so a silent 0 here would let the hook go permanently inert through a
    one-word configuration change while every test stayed green. That is the wiring-present,
    mechanism-absent shape this repository keeps finding, and a guard that cannot tell "nothing to
    scan" from "nothing found" has already lost the argument.
    """
    if len(argv) < 2:
        sys.stderr.write(
            "Blocked: no commit message file was passed, so nothing was scanned. A commit-msg "
            "hook is handed the message path by git; if this ran from pre-commit, check that the "
            "hook still carries `pass_filenames: true`.\n"
        )
        return 1
    reported: list[str] = []
    for path in argv[1:]:
        try:
            # Strict, deliberately. `errors="replace"` turned an undecodable message into a CLEAN
            # one: measured on a UTF-16 encoded file, a device name in the body came back
            # unreported, because the substituted characters broke the token apart. Git stores the
            # editor's bytes verbatim (`i18n.commitEncoding` exists for exactly that), so a message
            # this cannot decode is the case to refuse, not the case to pass.
            text = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            # A message file that cannot be read is a finding, not a pass: staying silent here
            # would turn every environment problem into an unguarded commit.
            sys.stderr.write(f"cannot read commit message file {path}: {exc}\n")
            return 1
        except UnicodeDecodeError as exc:
            sys.stderr.write(
                f"Blocked: the commit message file {path} is not valid UTF-8 ({exc.reason} at "
                f"byte {exc.start}), so it cannot be scanned. Re-encode the message as UTF-8, or "
                "set i18n.commitEncoding deliberately; an unreadable message is not a clean one.\n"
            )
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
