"""Tests for the ``epics-pv://guide`` operational cookbook resource.

Covers three things E1 promises: the guide loads as package data (works in an editable AND an
installed layout), it actually answers the four operational questions a live smoke had to
re-derive, and every committed knowledge file stays facility-agnostic (no site hostnames /
personal names / usernames / realistic device-PV names) — the CI-effective check, since the local
commit guard's site patterns are git-ignored and absent on a fresh CI checkout.

The facility-agnostic guard has two layers. A repo-wide DENYLIST (``_SITE_RE`` / ``_PERSON_RE``)
catches known site tokens, username paths and personal names across every tracked text file — a
best-effort list, not a proof. On top of it, a repo-can't-enumerate-them class (realistic, specific
EPICS device/PV names) is handled the other way round: an ALLOWLIST-shaped structural detector
(``_PV_RE``) runs over the human-maintained knowledge/prose surface only (``_tracked_doc_files``),
flagging anything ESS-PV-shaped that is not a declared synthetic placeholder. The synthetic PV
fixtures under ``tests/`` are intentional and stay covered by the denylist, not the structural
check.
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
    ".cff",  # CITATION.cff ships to the fork; in scope by TYPE, exempt by NAME below
}
# Attribution files: a personal name in them is the POINT, not a leak. Both are named explicitly
# rather than left out of _SCAN_SUFFIXES, so the exemption is a decision a reader can see and
# revisit. Anything else with these suffixes is still scanned.
_EXCLUDED_NAMES = {"LICENSE", "CITATION.cff"}
# Site/host identifiers + a filesystem path carrying a username (BOTH Windows \Users\ and Unix
# /home/<user>/ or /Users/<user>/ — the combined-CA path the guide teaches is the top leak vector).
# The username arms accept '.' and '-' in the name (jane.doe, first-last would otherwise slip \w),
# and any on-site *.lu.se subdomain is caught, not only the two-label bare form (a multi-label
# subdomain host slipped the old pattern). The \b keeps 'value.lu.set'/'menu.lu.select' clean.
_SITE_RE = re.compile(
    r"esss?\.lu\.se|\.esss?\b|\.ess\.eu|\.tn\b|\blinac-\d+|\balarm-logger-\d+|\bidmz-|-gw-tn"
    r"|\.lu\.se\b"
    r"|\\Users\\|/home/[\w.-]+/|/Users/[\w.-]+/",
    re.IGNORECASE,
)
# Personal names (case-insensitive): decisions must be attributed impersonally in committed docs.
_PERSON_RE = re.compile(r"\bDirk\b|\bNordt\b", re.IGNORECASE)

# Realistic, specific EPICS device/PV names are the leak vector the denylist above cannot
# enumerate: a facility has thousands, all structurally like the synthetic ones. So instead of
# listing real names, we flag anything ESS-PV-SHAPED and pass only DECLARED synthetic placeholders.
# The negative lookbehind anchors the match at a run boundary, so it never fires mid-token inside a
# sanctioned 'SIM:PS-01:Cur-RB' placeholder (it would otherwise re-anchor on 'PS-01'). Three
# structural gates keep it off non-PV text: (1) a digit must appear SOMEWHERE in the device path — a
# real ESS numeric index sits either in the Section-Subsection head ('PS-01:...') OR in a later
# device segment ('ISrc-CS:ISS-Magtr-01:...'); this alone excludes 'NTScalar:Value'. (2) The
# [A-Z]-led head never anchors on a digit-led ISO-8601 timestamp. (3) The head needs >=1 hyphenated
# segment (the ESS Sec-Sub## shape), which is what excludes bare ALLCAPS enums like 'MODE1:AUTO'.
_PV_RE = re.compile(
    r"(?<![A-Za-z0-9:./-])"  # anchor at a run boundary (not mid SIM:.. / path / date)
    r"(?=[A-Za-z0-9:-]*[0-9])"  # a digit appears anywhere in the device path (head OR a segment)
    r"[A-Z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)+"  # head: >=1 hyphenated segment (ESS Sec-Sub##)
    r":[A-Za-z][A-Za-z0-9-]*"  # device segment after ':'
    r"(?::[A-Za-z][A-Za-z0-9-]*)*"  # optional further ':'-segments (signal)
)
# A ca/pva scheme prefix (incl. the TLS forms pvas://, cas://) is stripped before scanning: the '/'
# in _PV_RE's lookbehind would otherwise stop it anchoring on a pasted 'pvas://<real-pv>' — the
# natural Phoebus/pvget copy form, and the repo steers operators toward TLS-secured planes.
_PV_SCHEME_RE = re.compile(r"\b(?:pvas?|cas?)://", re.IGNORECASE)
# Declared "this-is-invented" markers. The COMPOUND ones match as a head PREFIX (DEV-TEST01 splits
# to ['DEV','TEST01'], so a whole-segment test would miss them); the single-word ones match as a
# WHOLE '-'-segment only — a prefix test on 'SIM' would wrongly exempt a real head like 'SIMD-01'.
# Deliberately NO generic ESS abbreviations (VAC/PS/SYS collide with real system names); the
# lookbehind already lets the one documented doc placeholder (SIM:PS-01:Cur-RB) pass.
_SYNTHETIC_COMPOUND = ("DEV-TEST", "ZZZ-FAKE")
_SYNTHETIC_SEGMENTS = frozenset({"SIM", "EXAMPLE", "FAKE", "DEMO", "DUMMY", "MOCK"})


def _is_synthetic_pv(token: str) -> bool:
    """A flagged PV-shaped token is a declared synthetic placeholder when its head (the part before
    the first ':') is prefixed by a compound marker (DEV-TEST/ZZZ-FAKE) or carries a single-word
    marker as a whole '-'-segment."""
    head = token.split(":", 1)[0]
    if head.startswith(_SYNTHETIC_COMPOUND):
        return True
    return any(segment in _SYNTHETIC_SEGMENTS for segment in head.split("-"))


def _pv_leak_tokens(text: str) -> list[str]:
    """The device/PV names in ``text`` that are not a declared synthetic placeholder — the
    realistic, specific names that must not appear on the knowledge surface. A ca/pva scheme prefix
    is neutralised first so a pasted 'pvas://<real-pv>' still anchors. (ISO-8601 timestamps need no
    filter: _PV_RE's [A-Z]-led head never anchors on a digit-led token.)"""
    scanned = _PV_SCHEME_RE.sub("  ", text)
    return [
        token for match in _PV_RE.finditer(scanned) if not _is_synthetic_pv(token := match.group(0))
    ]


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
    except FileNotFoundError as exc:
        # ONLY a missing git binary may skip. A FAILED git call (CalledProcessError:
        # not-a-repo on an sdist, Windows "dubious ownership", …) must fail loudly —
        # skipping it silently switched OFF the repo-wide privacy/hermeticity scan (QA:
        # the skip guarded exactly the class of environment drift it should report).
        pytest.skip(f"git binary unavailable — cannot enumerate tracked files: {exc}")
    except subprocess.SubprocessError as exc:
        pytest.fail(
            f"git ls-files failed ({exc}) — the repo-wide privacy scan did NOT run; "
            "fix the git environment (ownership/repo state) instead of skipping the guard"
        )
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


# The operator guide lives under src/ but is a knowledge file (prose), so it is scanned by the PV
# detector even though the rest of src/ (code + docstring examples) is not.
_DOC_GUIDE_REL = "src/epics_pv_mcp/operator_guide.md"
_DOC_SUFFIXES = {".md", ".rst", ".example", ".txt"}


def _tracked_doc_files() -> list[Path]:
    """The human-maintained knowledge/prose surface — the guide plus every tracked doc/prose file
    outside ``tests/`` and ``src/``. This is where a real device/PV name would be pasted by hand;
    the ~200 synthetic PV fixtures under ``tests/`` and the sanctioned code examples under
    ``src/*.py`` are intentional and covered by the repo-wide denylist, not the structural PV check.
    Filters the repo-wide ``_tracked_text_files()`` by path — no second ``git ls-files`` call."""
    docs: list[Path] = []
    for path in _tracked_text_files():
        rel = path.relative_to(_ROOT).as_posix()
        if rel == _DOC_GUIDE_REL or (
            not rel.startswith(("tests/", "src/")) and path.suffix.lower() in _DOC_SUFFIXES
        ):
            docs.append(path)
    return docs


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

    # The structural PV detector runs only over the knowledge/prose surface (the hand-paste vector);
    # a realistic device/PV name there is a leak. The prose scope deliberately omits structured PV
    # carriers (.json/.yaml/.bob/.db) — those are covered, if at all, by the denylist, not this
    # check. Test fixtures carry synthetic PV-shaped names by design and stay on the denylist above.
    for path in _tracked_doc_files():
        rel = path.relative_to(_ROOT)
        problems.extend(
            f"{rel} [pv: {token!r}]" for token in _pv_leak_tokens(path.read_text(encoding="utf-8"))
        )

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


def test_pv_detector_flags_realistic_names_and_passes_synthetic() -> None:
    """The PV detector must flag realistic, specific ESS-shaped device names — the leak class the
    old ``_SITE_RE`` let through — while passing declared synthetic placeholders, ISO timestamps and
    non-PV constructs. The positive fixtures use INVENTED section heads (QAB / VLXQ / WXY — no real
    ESS segment), so this assertion never itself commits a real facility name, and this file is
    excluded from the scan (see ``_tracked_text_files``). Each positive is paired with its premise:
    the old denylist did NOT catch it — that is what makes this guard provably able to go red. Two
    index positions are covered so the detector is not narrowed to one: the digit in the head
    ('QAB-ZZ07:...') AND the digit in a later device segment with an alpha-only head
    ('QAB-ZZ:Diag-BPM-09:...') — the real ESS naming family the pre-first-colon head gate missed."""

    def flags(name: str) -> bool:  # exact production path (scheme-strip + ISO + synthetic filter)
        return bool(_pv_leak_tokens(name))

    for realistic in (
        "VLXQ-42:PWRC-UNIT-003:Curr-RB",
        "QAB-ZZ07:Ctrl-XVR-03",
        "WXY-QQ42:Diag-BPM-09",
        "QAB-ZZ:Diag-BPM-09:Val",  # alpha-only head, digit in the DEVICE segment (Sec-Sub:Dev-NN)
        "pva://VLXQ-42:Ctrl-XVR-03",  # scheme-prefixed paste form (Phoebus/pvget output)
        "pvas://QAB-ZZ07:Ctrl-XVR-03",  # TLS-secured PVAccess paste form
        "SIMD-07:Ctrl-XVR-03",  # head merely STARTS with 'SIM' — not a whole-segment marker
    ):
        assert flags(realistic), f"detector must flag realistic PV {realistic!r}"
        assert not _SITE_RE.search(realistic), (
            f"premise: denylist is orthogonal — misses {realistic!r}"
        )

    for benign in (
        "SIM:PS-01:Cur-RB",  # the one documented doc placeholder (CLAUDE.md, operator_guide.md)
        "DEV-TEST01:Ctrl-EVR-01:status",  # canonical sandbox PV — a compound synthetic marker
        "X-SIM-01:Ctrl-Cmd",  # whole-segment 'SIM' marker -> synthetic (vs SIMD-07 above)
        "2026-07-08T12:45:58",  # ISO-8601: _PV_RE's [A-Z]-led head never anchors on a digit
        "Ctrl-XVR:Value",  # hyphenated head, no digit index -> not PV-shaped (pins lookahead)
        "MODE1:AUTO",  # ALLCAPS enum, not a PV
        "NTScalar:Value",  # PVA type name
    ):
        assert not flags(benign), f"detector must pass benign token {benign!r}"


def test_site_re_catches_username_paths_and_subdomains() -> None:
    """The two denylist hardenings: a username filesystem path carrying '.'/'-' in the name, and any
    on-site multi-label subdomain host — both slipped the pre-hardening pattern (``/home/\\w+/`` and
    the two-label-only domain match). A precision control pins the word boundary. This file defines
    the guard patterns as literals and is itself excluded from the scan."""
    for leak in (
        "/home/jane.doe/.epics/ca.pem",
        "/Users/first-last/combined-ca.pem",
        "host-07.dept.lu.se",
        "x.lu.se/path",
    ):
        assert _SITE_RE.search(leak), f"hardened _SITE_RE must catch {leak!r}"
    for benign in ("value.lu.set", "menu.lu.select"):
        assert not _SITE_RE.search(benign), f"word boundary must keep {benign!r} clean"
