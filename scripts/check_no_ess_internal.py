#!/usr/bin/env python3
"""Pre-commit guard: block ESS-internal identifiers, PII process-markers, and secrets.

Defense-in-depth after the 2026-07 privacy remediation. This repository is a PUBLIC fork, so
ESS-internal network/infrastructure details, personal work-process annotations, and any real
secret must never be committed. The guard scans the staged files pre-commit passes to it and
fails (non-zero exit) on the first match, printing ``file:line`` so the author can remove the
content before the commit lands.

Patterns are kept deliberately NARROW to avoid false positives on the legitimate public account
name ``epicDirk`` and on generic open-source version strings. It is not a substitute for review;
it is a backstop that catches the specific classes that leaked before.
"""

from __future__ import annotations

import re
import sys

# (label, compiled pattern) — each pattern targets a class that must not reach this public repo.
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("secret: GitLab PAT", re.compile(r"glpat-[0-9A-Za-z_\-]{20,}")),
    ("secret: GitHub token", re.compile(r"gh[posru]_[0-9A-Za-z]{36,}|github_pat_[0-9A-Za-z_]{20,}")),
    ("secret: AWS access key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("secret: private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("secret: Slack token", re.compile(r"xox[baprs]-[0-9A-Za-z\-]{10,}")),
    ("ESS network: internal host IP", re.compile(r"REDACTED-NET\.\d{1,3}")),
    ("ESS host: naming/domain", re.compile(r"[A-Za-z0-9._\-]*REDACTED-DOMAIN")),
    ("ESS infra: ansible role", re.compile(r"REDACTED-ROLE-[a-z]")),
    ("ESS infra: internal GitLab", re.compile(r"REDACTED-GITLAB")),
    ("process marker: approval log", re.compile(r"Dirks?\s+OK\b")),
    ("network-posture leak", re.compile(r"REDACTED", re.IGNORECASE)),
]

# The guard itself defines the patterns as string literals; never flag it.
_SELF = "check_no_ess_internal.py"


def scan(path: str) -> list[str]:
    """Return ``file:line: label -> excerpt`` for every pattern hit in *path* (text files only)."""
    hits: list[str] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for lineno, line in enumerate(handle, start=1):
                for label, pattern in PATTERNS:
                    if pattern.search(line):
                        hits.append(f"{path}:{lineno}: {label} -> {line.strip()[:100]}")
    except (OSError, UnicodeError):
        pass  # unreadable/binary — nothing to scan
    return hits


def main(argv: list[str]) -> int:
    """Scan the staged files in *argv*; return 1 (block the commit) if any pattern matched."""
    all_hits: list[str] = []
    for path in argv[1:]:
        if path.endswith(_SELF):
            continue
        all_hits.extend(scan(path))
    if all_hits:
        sys.stderr.write(
            "Blocked: ESS-internal / PII / secret content must not be committed to this public "
            "fork.\nRemove the following (or move it to the private workspace):\n"
        )
        for hit in all_hits:
            sys.stderr.write(f"  {hit}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
