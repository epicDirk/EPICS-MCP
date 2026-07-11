"""Tests for the ``epics-pv://guide`` operational cookbook resource.

Covers three things E1 promises: the guide loads as package data (works in an editable AND an
installed layout), it actually answers the four operational questions a live smoke had to
re-derive, and every committed knowledge file stays facility-agnostic (no site hostnames /
personal names) — the CI-effective guarantee, since the local commit guard's site patterns are
git-ignored and absent on a fresh CI checkout.
"""

from __future__ import annotations

import re
from pathlib import Path

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


# Every knowledge file E1 commits ships to the public fork; the local hostname guard is
# git-ignored/CI-absent, so THIS test is the enforced facility-agnostic guarantee. Files that do
# not exist yet (added in a later commit of the same series) are skipped, but the guide is required.
_KNOWLEDGE_FILES = (
    "src/epics_pv_mcp/operator_guide.md",
    "OPERATING.md",
    "CLAUDE.md",
)
_SITE_RE = re.compile(
    r"esss?\.lu\.se|\.ess\.eu|\.tn\b|\blinac-\d+|\balarm-logger-\d+|\bidmz-|-gw-tn|\\Users\\",
    re.IGNORECASE,
)
# The proper name (case-sensitive): decisions must be attributed impersonally in committed docs.
_PERSON_RE = re.compile(r"\bDirk\b")


def test_knowledge_files_are_facility_agnostic() -> None:
    scanned: list[str] = []
    for rel in _KNOWLEDGE_FILES:
        path = _ROOT / rel
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        site = {m.group(0) for m in _SITE_RE.finditer(text)}
        person = set(_PERSON_RE.findall(text))
        assert not site, f"{rel}: site identifier(s) leaked into a committed knowledge file: {site}"
        assert not person, f"{rel}: personal name leaked into a committed knowledge file"
        scanned.append(rel)
    assert "src/epics_pv_mcp/operator_guide.md" in scanned, (
        "the guide file must exist and be scanned"
    )
