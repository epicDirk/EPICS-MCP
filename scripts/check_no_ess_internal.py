#!/usr/bin/env python3
"""Pre-commit guard: block secrets and site-defined sensitive patterns.

Defense-in-depth after the 2026-07 privacy remediation. This repository is a PUBLIC fork, so
secrets and any site-internal identifiers must never be committed. The guard scans the staged
files pre-commit passes to it and fails (non-zero exit) on the first match, printing ``file:line``
(never the matched text itself, so the guard output cannot leak content).

Two pattern sources:
  * Built-in, universal secret formats (below) - safe to be public; they disclose nothing.
  * OPTIONAL site-specific patterns loaded from ``scripts/.pii_patterns.local`` (git-ignored),
    so this public repo never has to spell out internal hostnames, IP ranges, or identifiers.
    One Python regex per line; blank lines and ``#`` comments are ignored.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Universal secret formats - generic, low false-positive, safe to publish.
_BUILTIN: list[tuple[str, str]] = [
    ("secret: GitLab PAT", r"glpat-[0-9A-Za-z_\-]{20,}"),
    ("secret: GitHub token", r"gh[posru]_[0-9A-Za-z]{36,}|github_pat_[0-9A-Za-z_]{20,}"),
    ("secret: AWS access key", r"AKIA[0-9A-Z]{16}"),
    ("secret: private key block", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("secret: Slack token", r"xox[baprs]-[0-9A-Za-z\-]{10,}"),
]

_LOCAL_FILE = Path(__file__).with_name(".pii_patterns.local")
_SELF = Path(__file__).name


def load_patterns() -> list[tuple[str, re.Pattern[str]]]:
    """Compile the built-in secret patterns plus any site regexes from the git-ignored local file."""
    patterns: list[tuple[str, re.Pattern[str]]] = [(label, re.compile(rx)) for label, rx in _BUILTIN]
    if _LOCAL_FILE.exists():
        for lineno, raw in enumerate(_LOCAL_FILE.read_text(encoding="utf-8").splitlines(), start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            try:
                patterns.append((f"site pattern ({_LOCAL_FILE.name}:{lineno})", re.compile(line)))
            except re.error:
                sys.stderr.write(f"warning: bad regex in {_LOCAL_FILE.name}:{lineno}: {line!r}\n")
    return patterns


def scan(path: str, patterns: list[tuple[str, re.Pattern[str]]]) -> list[str]:
    """Return ``file:line: label`` for every pattern hit in *path* (text files only, no excerpt)."""
    hits: list[str] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for lineno, line in enumerate(handle, start=1):
                for label, pattern in patterns:
                    if pattern.search(line):
                        hits.append(f"{path}:{lineno}: {label}")
    except (OSError, UnicodeError):
        pass  # unreadable/binary - nothing to scan
    return hits


def main(argv: list[str]) -> int:
    """Scan the staged files in *argv*; return 1 (block the commit) if any pattern matched."""
    patterns = load_patterns()
    all_hits: list[str] = []
    for path in argv[1:]:
        if Path(path).name in {_SELF, _LOCAL_FILE.name}:
            continue
        all_hits.extend(scan(path, patterns))
    if all_hits:
        sys.stderr.write(
            "Blocked: secret or site-internal content must not be committed to this public fork.\n"
            "Remove it (or move it to the private workspace):\n"
        )
        for hit in all_hits:
            sys.stderr.write(f"  {hit}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
