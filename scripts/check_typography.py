#!/usr/bin/env python3
"""Pre-commit guard: keep typographic punctuation out of this repository.

Why a guard at all: the public fork was written over months with an em dash as the default
connector, 3026 of them at the peak, plus ellipses, en dashes, curly quotes and a doubled hyphen
standing in for a dash where the character itself had already been avoided. Removing them was a
multi-day sweep. Nothing stopped them coming back, and a sweep nobody guards is a sweep somebody
repeats.

Why not ruff: RUF001-003 ("ambiguous unicode") sound like they cover this and do not. Probed
character by character through ``ruff --isolated --select RUF001,RUF002,RUF003``: of the nine
entries this repository listed in ``allowed-confusables``, ruff recognises exactly THREE, the en
dash, the typographic apostrophe and the multiplication sign. The em dash, the ellipsis and all
four curly quotes produce no finding at all, whether in a string, a docstring or a comment; the
list was carrying four entries that never did anything. The doubled hyphen is plain ASCII and out
of reach for a confusables check by construction. So ruff guards the en dash inside ``.py`` (a
second, independent pair of eyes, kept deliberately) and this guard covers every character in
every text file.

What is deliberately NOT forbidden, because it earns its place: U+2019, the typographic
apostrophe, is legitimate inside English prose; and the arrow, the almost-equal sign and the
multiplication sign carry meaning no ASCII substitute holds. The arrow in particular is the agreed
column separator in two endpoint tables, where a wider substitute would destroy the alignment it
exists to hold.

Scope: every file pre-commit hands over that decodes as UTF-8. A binary is skipped by the decode
itself, not by an extension list that would need maintaining. Output is ``file:line: label``, the
label naming the character; the line's text is never echoed, mirroring check_no_ess_internal.py.

Exceptions live in ``scripts/.typography-exceptions`` next to this file, keyed on a LITERAL
FRAGMENT of the offending line rather than a line number, because a line number moves with any
edit above it and would rot for a reason that is not a change in the finding. An exception frees
the whole LINE, not one character on it: the file that needs one is rare enough that a narrower
key would be more machinery than the problem deserves.
"""

from __future__ import annotations

import sys
from pathlib import Path

# The characters are spelled as CODE POINTS on purpose: this guard is subject to its own rule, and
# a literal em dash here would make it block itself the moment pre-commit hands it its own source.
# The doubled hyphen is ASCII, where that trick is unavailable, so it is assembled from parts.
_DOUBLED_HYPHEN = " " + "-" * 2 + " "

_FORBIDDEN: list[tuple[str, str]] = [
    (chr(0x2014), "em dash: use a comma, a colon or a semicolon"),
    (chr(0x2013), "en dash: use a hyphen"),
    (chr(0x2026), "ellipsis: use three full stops"),
    (chr(0x201E), 'low double quote: use "'),
    (chr(0x201C), 'left double quote: use "'),
    (chr(0x201D), 'right double quote: use "'),
    (chr(0x201A), "low single quote: use an ASCII apostrophe"),
    (chr(0x2018), "left single quote: use an ASCII apostrophe"),
    (_DOUBLED_HYPHEN, "doubled hyphen for a dash: use a comma, a colon or a semicolon"),
]

_EXCEPTIONS_FILE = Path(__file__).with_name(".typography-exceptions")


def load_exceptions() -> list[tuple[str, str]]:
    """Return ``(path_suffix, line_fragment)`` pairs; a line matching both is not reported."""
    if not _EXCEPTIONS_FILE.exists():
        return []
    pairs: list[tuple[str, str]] = []
    for raw in _EXCEPTIONS_FILE.read_text(encoding="utf-8").splitlines():
        entry = raw.strip()
        if not entry or entry.startswith("#"):
            continue
        path_part, separator, fragment = entry.partition("::")
        if not separator or not fragment:
            sys.stderr.write(f"warning: bad exception line in {_EXCEPTIONS_FILE.name}: {entry!r}\n")
            continue
        pairs.append((path_part.strip(), fragment))
    return pairs


def is_excepted(path: str, line: str, exceptions: list[tuple[str, str]]) -> bool:
    """True when *line* in *path* is covered by an entry: BOTH path suffix and fragment must hit."""
    posix = Path(path).as_posix()
    return any(posix.endswith(suffix) and fragment in line for suffix, fragment in exceptions)


def scan(path: str, exceptions: list[tuple[str, str]]) -> list[str]:
    """Return ``file:line: label`` for every forbidden character in *path*, text files only."""
    try:
        content = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return []  # binary or unreadable, nothing a typographic rule applies to
    hits: list[str] = []
    for lineno, line in enumerate(content.splitlines(), start=1):
        found = [label for character, label in _FORBIDDEN if character in line]
        if found and not is_excepted(path, line, exceptions):
            hits.extend(f"{path}:{lineno}: {label}" for label in found)
    return hits


def main(argv: list[str]) -> int:
    """Scan the staged files in *argv*; return 1 (block the commit) on any forbidden character."""
    exceptions = load_exceptions()
    all_hits: list[str] = []
    for path in argv[1:]:
        if Path(path).name == _EXCEPTIONS_FILE.name:
            continue  # an exception has to quote the character it exempts
        all_hits.extend(scan(path, exceptions))
    if all_hits:
        sys.stderr.write(
            "Blocked: typographic punctuation must not enter this repository.\n"
            "Replace the character (see the label), or add a justified exception to "
            f"{_EXCEPTIONS_FILE.name}:\n"
        )
        for hit in all_hits:
            sys.stderr.write(f"  {hit}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
