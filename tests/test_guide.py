"""Tests for the ``epics-pv://guide`` operational cookbook resource.

Covers three things E1 promises: the guide loads as package data (works in an editable AND an
installed layout), it actually answers the four operational questions a live smoke had to
re-derive, and every committed knowledge file stays facility-agnostic (no site hostnames /
personal names) — the CI-effective check (a best-effort denylist), since the local commit guard's
site patterns are git-ignored and absent on a fresh CI checkout.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from epics_pv_mcp.resources import get_guide

_ROOT = Path(__file__).resolve().parent.parent  # …/EPICS-MCP-Server


def test_guide_loads_as_package_data() -> None:
    text = get_guide()
    assert isinstance(text, str)
    assert len(text) > 500, "guide is suspiciously short — package-data load may be broken"


def test_guide_answers_the_smoke_findings() -> None:
    """The guide must answer the four things a live smoke had to re-derive — including the
    ANTI-pattern token (``getMatchingPVs``), not only the right call, so the test can't pass on a
    guide that names the right API but forgets to warn against the wrong one."""
    text = get_guide()
    anchors = [
        "ChannelFinder",
        "Archiver",
        "Alarm",
        "Naming",
        "Olog",  # plane landscape
        "getAllPVs",
        "getPVsForThisAppliance",
        "getMatchingPVs",  # enumerate + the anti-pattern
        "PV_TIMEOUT",
        "withheld",
        ".RTYP",  # error signatures + field read
        "certifi",
        "EPICS_MCP_CA_BUNDLE",  # CA-combine recipe
        "cluster",
        ":17668",  # retrieval-cluster-aware
    ]
    missing = [a for a in anchors if a not in text]
    assert not missing, f"guide missing required anchors: {missing}"


# The facility-agnostic rule covers EVERY tracked text file shipping to the fork. This enumerates
# them via ``git ls-files`` (the true shipping set — hermetic, no git-ignored local files like the
# sandbox), excluding THIS file (it defines the patterns as literals) and LICENSE (whose copyright
# line legitimately carries the author's name). The local hostname commit-hook is git-ignored /
# CI-absent, so THIS test is the CI-effective check — a best-effort DENYLIST, not a proof.
# Transcribe operationally-learned facts into agnostic form by hand, never paste raw.
_SCAN_SUFFIXES = {
    ".py",
    ".md",
    ".json",
    ".cfg",
    ".ini",
    ".txt",
    ".yml",
    ".yaml",
    ".rst",
    ".example",
    ".toml",  # pyproject.toml ships to the fork and carries the author line — scan it too
}
_EXCLUDED_NAMES = {"LICENSE"}
# Site/host identifiers + a filesystem path carrying a username (BOTH Windows \Users\ and Unix
# /home/<user>/ or /Users/<user>/ — the combined-CA path the guide teaches is the top leak vector).
_SITE_RE = re.compile(
    r"esss?\.lu\.se|\.esss?\b|\.ess\.eu|\.tn\b|\blinac-\d+|\balarm-logger-\d+|\bidmz-|-gw-tn"
    r"|\\Users\\|/home/\w+/|/Users/\w+/",
    re.IGNORECASE,
)
# Personal names (case-insensitive): decisions must be attributed impersonally in committed docs.
_PERSON_RE = re.compile(r"\bDirk\b|\bNordt\b", re.IGNORECASE)


def _tracked_text_files() -> list[Path]:
    """Every TRACKED text file (``git ls-files``) that ships to the fork — the true repo-wide,
    hermetic surface. Excludes THIS file (pattern literals) and LICENSE (copyright attribution)."""
    try:
        listing = subprocess.run(
            ["git", "ls-files"],
            cwd=_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        ).stdout
    except (FileNotFoundError, subprocess.SubprocessError) as exc:
        pytest.skip(f"git unavailable — cannot enumerate tracked files: {exc}")
    this = Path(__file__).resolve()
    files: list[Path] = []
    for rel in listing.splitlines():
        path = _ROOT / rel
        if (
            path.is_file()
            and path.resolve() != this
            and path.name not in _EXCLUDED_NAMES
            and path.suffix.lower() in _SCAN_SUFFIXES
        ):
            files.append(path)
    return sorted(files)


def test_knowledge_files_are_facility_agnostic() -> None:
    files = _tracked_text_files()
    guide = _ROOT / "src" / "epics_pv_mcp" / "operator_guide.md"
    assert guide in files, "the guide must exist and be scanned"

    problems: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        for label, regex in (("site", _SITE_RE), ("person", _PERSON_RE)):
            match = regex.search(text)
            if match:
                problems.append(f"{path.relative_to(_ROOT)} [{label}: {match.group(0)!r}]")
    assert not problems, f"facility identifier(s) leaked into committed files: {problems}"


def test_no_epics_sandbox_fiction() -> None:
    """S2: the ``live`` opt-in was documented as a non-existent ``EPICS_SANDBOX`` env var
    (``skipif(EPICS_SANDBOX)`` / ``EPICS_SANDBOX=1``) — a claim about our own repo that probes
    nothing: ``EPICS_SANDBOX=1 uv run pytest -m live`` runs ZERO live tests and exits 0, because
    each live module actually self-skips on its own ``EPICS_MCP_*`` URL/fixture vars. Pin the
    fiction as removed: no tracked file may mention it. Reuses ``_tracked_text_files()``, which
    excludes THIS file (see the exclusion in that helper) so the needle never self-matches.
    """
    offenders = [
        str(path.relative_to(_ROOT))
        for path in _tracked_text_files()
        if "EPICS_SANDBOX" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, (
        "EPICS_SANDBOX is a non-existent gate (no Python reads it); the real live opt-in is the "
        f"per-plane EPICS_MCP_* URL/fixture vars. It must not appear in tracked files: {offenders}"
    )
