"""Tests for the ``epics://guide`` operational cookbook resource.

Covers three things E1 promises: the guide loads as package data (works in an editable AND an
installed layout), it actually answers the four operational questions a live smoke had to
re-derive, and every committed knowledge file stays facility-agnostic (no site hostnames /
personal names / usernames / realistic device-PV names), the CI-effective check, since the local
commit guard's site patterns are git-ignored and absent on a fresh CI checkout.

The facility-agnostic guard has two layers, and both now read the SAME population: every tracked
text file. A repo-wide DENYLIST (``_SITE_RE`` / ``_PERSON_RE``) catches known site tokens, username
paths and personal names, a best-effort list, not a proof. On top of it, a
repo-can't-enumerate-them class (realistic, specific EPICS device/PV names) is handled the other
way round: an ALLOWLIST-shaped structural detector (``pv_leak_scan.PV_RE``) flags anything
ESS-PV-shaped that is not a declared synthetic placeholder.

The structural detector used to read a doc-suffix subset outside ``tests/`` and ``src/``, which was
20 of 180 files, and that is QA-28. Measured, the PATH half of that filter excluded nothing: the
only doc-suffixed file under either directory is the guide, which the filter admitted by name
anyway. The SUFFIX half did all the narrowing, and what it narrowed away was every ``.py`` file,
which is where QA-27 (a real device name, in THIS module) had been sitting unseen. Widening cost
zero hits on the 160 files it newly reads: the PV fixtures under ``tests/`` pass because they carry
the declared synthetic markers, not because anything exempts them.

The one file that needs an exemption is this one, and only for one layer. It defines the denylist
patterns as literals, so the denylist still skips it. The structural detector reads it, and
subtracts :data:`_PV_SHAPES_DECLARED_HERE`, the PV shapes this module states on purpose; what holds
that list honest, and what provably cannot, is written at the declaration.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from epics_mcp.resources import get_guide

_ROOT = Path(__file__).resolve().parent.parent  # .../EPICS-MCP-Server

# The detector is shared with the commit-message guard, so it lives in scripts/ and is imported
# rather than duplicated. The sys.path splice is this repository's established way of reaching a
# scripts/ module from a test (six other modules do the same); scripts/ is not a package, and
# making it one would change what the packaging guard reads.
sys.path.insert(0, str(_ROOT / "scripts"))

from pv_leak_scan import pv_leak_tokens  # noqa: E402 (needs the sys.path splice above)


def test_guide_loads_as_package_data() -> None:
    text = get_guide()
    assert isinstance(text, str)
    assert len(text) > 500, "guide is suspiciously short, package-data load may be broken"


def test_guide_answers_the_smoke_findings() -> None:
    """The guide must answer the four things a live smoke had to re-derive, including the
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
# them via ``git ls-files`` (the true shipping set, hermetic, no git-ignored local files like the
# sandbox), excluding THIS file (it defines the patterns as literals) and LICENSE (whose copyright
# line legitimately carries the author's name). The local hostname commit-hook is git-ignored /
# CI-absent, so THIS test is the CI-effective check, a best-effort DENYLIST, not a proof.
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
    ".toml",  # pyproject.toml ships to the fork and carries the author line, scan it too
    ".cff",  # CITATION.cff ships to the fork; in scope by TYPE, exempt by NAME below
}
# Attribution files: a personal name in them is the POINT, not a leak. Both are named explicitly
# rather than left out of _SCAN_SUFFIXES, so the exemption is a decision a reader can see and
# revisit. Anything else with these suffixes is still scanned.
_EXCLUDED_NAMES = {"LICENSE", "CITATION.cff"}
# Site/host identifiers + a filesystem path carrying a username (BOTH Windows \Users\ and Unix
# /home/<user>/ or /Users/<user>/, the combined-CA path the guide teaches is the top leak vector).
# The username arms accept '.' and '-' in the name (jane.doe, first-last would otherwise slip \w),
# and any on-site *.lu.se subdomain is caught, not only the two-label bare form (a multi-label
# subdomain host slipped the old pattern). The \b keeps 'value.lu.set'/'menu.lu.select' clean.
#
# The last arm is a Windows DRIVE path: it leaks the developer's machine layout even when no user
# name appears in it, and the \Users\ arm above does not see it (QA-29, where a shipped module
# docstring carried one for two months). A word character must FOLLOW the separator, which keeps
# an EPICS DB fixture value like record(bo, "c:\\") clean, and the lookbehind keeps a PV paste
# form like pva://SEC-SUB:Dev-01 clean (the scheme's 'a:' is preceded by a word character).
# Deliberately ONE segment, not two: requiring a trailing separator would let 'D:/toolrepo' and
# 'D:\toolrepo' through, and those leak just as much. Use a POSIX-style placeholder in docs
# instead of a real Windows example path.
_SITE_RE = re.compile(
    r"esss?\.lu\.se|\.esss?\b|\.ess\.eu|\.tn\b|\blinac-\d+|\balarm-logger-\d+|\bidmz-|-gw-tn"
    r"|\.lu\.se\b"
    r"|\\Users\\|/home/[\w.-]+/|/Users/[\w.-]+/"
    r"|(?<!\w)[A-Za-z]:[\\/][\w.-]+",
    re.IGNORECASE,
)
# Personal names (case-insensitive): decisions must be attributed impersonally in committed docs.
_PERSON_RE = re.compile(r"\bDirk\b|\bNordt\b", re.IGNORECASE)

# The structural device/PV-name detector MOVED to scripts/pv_leak_scan.py, and the move is the
# point rather than tidying: a second surface needs the same needle. The commit MESSAGE is a leak
# vector no file guard can reach (GB-33 measured real device names in commit bodies), so
# scripts/check_commit_message.py scans it with exactly this detector rather than a second copy
# that would drift. The pattern, its three structural gates and the synthetic markers are
# documented at the definition; what stays here is the POPULATION this repository scans with it.


def _tracked_text_files(*, include_this_file: bool = False) -> list[Path]:
    """Every TRACKED text file (``git ls-files``) that ships to the fork, the true repo-wide,
    hermetic surface. Excludes LICENSE (copyright attribution) and, by default, THIS file.

    THIS file is excluded because it defines the DENYLIST patterns as literals: ``_SITE_RE`` and
    ``_PERSON_RE`` would match their own source, so a repo-wide denylist sweep that read this file
    would report eleven site tokens that are the guard's own needle. Measured: eleven distinct,
    and zero in every other tracked ``.py``, so the exemption exists for one file and one layer.

    ``include_this_file`` narrows that exemption to the layer that needs it. The STRUCTURAL PV
    detector is a different needle and does not match the denylist patterns, so it can and must
    read this file: QA-27 was a real device name pasted into this very module, and a scan that
    skips it cannot see the class it exists for. What that scan owes in return is a declared list
    of the PV shapes stated here on purpose, which is :data:`_PV_SHAPES_DECLARED_HERE`.
    """
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
        # not-a-repo on an sdist, Windows "dubious ownership", ...) must fail loudly:
        # skipping it silently switched OFF the repo-wide privacy/hermeticity scan (QA:
        # the skip guarded exactly the class of environment drift it should report).
        pytest.skip(f"git binary unavailable, cannot enumerate tracked files: {exc}")
    except subprocess.SubprocessError as exc:
        pytest.fail(
            f"git ls-files failed ({exc}), the repo-wide privacy scan did NOT run; "
            "fix the git environment (ownership/repo state) instead of skipping the guard"
        )
    this = Path(__file__).resolve()
    files: list[Path] = []
    for rel in listing.splitlines():
        path = _ROOT / rel
        if (
            path.is_file()
            and (include_this_file or path.resolve() != this)
            and path.name not in _EXCLUDED_NAMES
            and path.suffix.lower() in _SCAN_SUFFIXES
        ):
            files.append(path)
    return sorted(files)


#: Every PV-shaped string THIS module states on purpose: the positive fixtures of the detector
#: test below, plus the two shapes the pattern comments above use to explain what anchors where.
#: The structural detector scans this file, so these are precisely what it must not report, and
#: anything else PV-shaped in here is a real leak.
#:
#: One list rather than two, because two would drift apart, and every entry is ASSERTED rather
#: than merely listed: ``test_pv_detector_flags_realistic_names_and_passes_synthetic`` iterates it
#: and requires each entry to be something the detector actually flags. An entry that stops being
#: PV-shaped therefore reddens instead of quietly widening the scan, which is the "does the
#: exception still bite" property, measured rather than assumed.
#:
#: ⚠️ What holds this list, stated exactly, because the tempting extra guard does NOT work. An
#: obvious addition is "the declared set equals the PV-shaped strings this file carries", asserted
#: as a set in both directions. It was written, measured, and removed: only one of its two
#: directions can ever go red. An undeclared token appearing here reddens (and the scan below
#: reddens on it too, so that half is duplicated), while a declaration with no other use CANNOT
#: redden, because the declaration is itself an appearance in the scanned file. A guard whose name
#: promises two directions and delivers one is the shape this repository keeps finding, so it is
#: absent rather than present and misleading.
#:
#: ⚠️ The residual hole, named rather than papered over: this is a SELF-GRANTED allowance. Pasting
#: a real device name INTO this list would pass everything. Nothing mechanical can close that from
#: inside the file it exempts. What holds it is the rule below and the review of any diff touching
#: these lines.
#:
#: Every head is INVENTED (QAB / VLXQ / WXY / SEC-SUB, no real facility segment), so stating them
#: here cannot itself commit a facility name. That rule is the whole reason the class can be
#: declared at all.
_PV_SHAPES_DECLARED_HERE = (
    "VLXQ-42:PWRC-UNIT-003:Curr-RB",
    "QAB-ZZ07:Ctrl-XVR-03",
    "WXY-QQ42:Diag-BPM-09",
    "QAB-ZZ:Diag-BPM-09:Val",  # alpha-only head, digit in the DEVICE segment (Sec-Sub:Dev-NN)
    "pva://VLXQ-42:Ctrl-XVR-03",  # scheme-prefixed paste form (Phoebus/pvget output)
    "pvas://QAB-ZZ07:Ctrl-XVR-03",  # TLS-secured PVAccess paste form
    "SIMD-07:Ctrl-XVR-03",  # head merely STARTS with 'SIM', not a whole-segment marker
    "pva://SEC-SUB:Dev-01",  # the _SITE_RE lookbehind example, stated in the comment above
    "QAB-ZZ:Diag-BPM-09",  # the alpha-only-head example in the detector test's docstring below
)


def _declared_pv_tokens() -> set[str]:
    """What the detector EXTRACTS from the declared shapes, which is what the scan compares.

    Derived by running the production extractor over each declared shape rather than by listing
    tokens by hand, because the two differ: a scheme-prefixed shape is stored with its ``pva://``
    and read without it, so a hand-written token list would have drifted from the first entry.
    """
    return {token for shape in _PV_SHAPES_DECLARED_HERE for token in pv_leak_tokens(shape)}


def test_knowledge_files_are_facility_agnostic() -> None:
    files = _tracked_text_files()
    guide = _ROOT / "src" / "epics_mcp" / "operator_guide.md"
    assert guide in files, "the guide must exist and be scanned"

    problems: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        for label, regex in (("site", _SITE_RE), ("person", _PERSON_RE)):
            match = regex.search(text)
            if match:
                problems.append(f"{path.relative_to(_ROOT)} [{label}: {match.group(0)!r}]")

    # The structural PV detector runs over the SAME population as the denylist above, plus this
    # file (QA-28). It used to run over a doc-suffix subset outside tests/ and src/, which reached
    # 20 of the 180 tracked text files and, measured, was narrowed by the SUFFIX alone: the only
    # doc-suffixed file under either directory is the guide, which that filter admitted by name
    # anyway, so the path half excluded nothing at all. What it did exclude was every .py, and
    # QA-27 was a real device name in one of them.
    #
    # Widening costs nothing today, measured over the 160 files it newly reads: ZERO hits. The
    # ~200 PV-shaped fixtures under tests/ are not passed by an exemption but by the declared
    # synthetic markers (SIM / DEV-TEST / ZZZ-FAKE / EXAMPLE / FAKE / DEMO / DUMMY / MOCK) that
    # is_synthetic_pv already applies. The previous rationale here named an exemption that did
    # not exist. What the widening buys is the future: a real device name pasted into any tracked
    # text file is now reported, which is the class the denylist above cannot enumerate.
    #
    # This file is scanned with its declared shapes subtracted; see _PV_SHAPES_DECLARED_HERE for
    # why that list is asserted in both directions rather than trusted.
    declared = _declared_pv_tokens()
    this = Path(__file__).resolve()
    for path in _tracked_text_files(include_this_file=True):
        rel = path.relative_to(_ROOT)
        allowed = declared if path.resolve() == this else frozenset()
        problems.extend(
            f"{rel} [pv: {token!r}]"
            for token in pv_leak_tokens(path.read_text(encoding="utf-8"))
            if token not in allowed
        )

    assert not problems, f"facility identifier(s) leaked into committed files: {problems}"


def test_no_epics_sandbox_fiction() -> None:
    """S2: the ``live`` opt-in was documented as a non-existent ``EPICS_SANDBOX`` env var
    (``skipif(EPICS_SANDBOX)`` / ``EPICS_SANDBOX=1``), a claim about our own repo that probes
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


def test_denylist_flags_a_windows_drive_path_and_passes_look_alikes() -> None:
    """QA-29: a local drive path leaks the developer's machine layout, and the ``\\Users\\`` arm
    never saw one that carries no user name. A shipped module docstring named its source as such a
    path and survived two months of review plus a full QA pass.

    Every fixture here is INVENTED (see the sibling PV test for the same rule): the offending
    string was a real local path, so pinning it verbatim would commit the leak into the guard
    meant to remove it.

    The benign side pins why the arm is written the way it is: a word character must follow the
    separator, and the lookbehind must reject a URL scheme."""
    for leak in (
        "D:/toolrepo/pkg/mod.py",
        "C:\\dev\\repo\\x.py",
        "E:/toolrepo",  # a single segment leaks too, so no trailing separator is required
    ):
        assert _SITE_RE.search(leak), f"denylist must flag drive path {leak!r}"

    for benign in (
        'record(bo, "c:\\\\")',  # EPICS DB fixture value: separator, then no word character
        "pva://SEC-SUB:Dev-01",  # PV paste form: the scheme's 'a:' is preceded by a word character
        "pvas://SEC-SUB:Dev-01",
        "http://naming:8080/rest",
        "https://example.invalid/x",
    ):
        assert not _SITE_RE.search(benign), f"denylist must pass {benign!r}"


def test_pv_detector_flags_realistic_names_and_passes_synthetic() -> None:
    """The PV detector must flag realistic, specific ESS-shaped device names, the leak class the
    old ``_SITE_RE`` let through, while passing declared synthetic placeholders, ISO timestamps and
    non-PV constructs. The positive fixtures use INVENTED section heads (QAB / VLXQ / WXY / SEC-SUB,
    no real ESS segment), so this assertion never itself commits a real facility name. Each positive
    is paired with its premise: the old denylist did NOT catch it, that is what makes this guard
    provably able to go red. Two index positions are covered so the detector is not narrowed to
    one: the digit in the head ('QAB-ZZ07:...') AND the digit in a later device segment with an
    alpha-only head ('QAB-ZZ:Diag-BPM-09:...'), the real ESS naming family that the
    pre-first-colon head gate missed.

    The fixtures come from :data:`_PV_SHAPES_DECLARED_HERE` rather than from a tuple written out
    here, and that is the seam QA-28 needed: this file is now INSIDE the structural scan, which
    subtracts exactly that list. Sharing it means the scan cannot be widened by adding a shape that
    nothing asserts, and an asserted shape cannot fall out of the scan's allowance.
    """

    def flags(name: str) -> bool:  # exact production path (scheme-strip + ISO + synthetic filter)
        return bool(pv_leak_tokens(name))

    for realistic in _PV_SHAPES_DECLARED_HERE:
        assert flags(realistic), f"detector must flag realistic PV {realistic!r}"
        assert not _SITE_RE.search(realistic), (
            f"premise: denylist is orthogonal, misses {realistic!r}"
        )

    for benign in (
        "SIM:PS-01:Cur-RB",  # the one documented doc placeholder (CLAUDE.md, operator_guide.md)
        "DEV-TEST01:Ctrl-EVR-01:status",  # canonical sandbox PV, a compound synthetic marker
        "X-SIM-01:Ctrl-Cmd",  # whole-segment 'SIM' marker -> synthetic (vs SIMD-07 above)
        "2026-07-08T12:45:58",  # ISO-8601: PV_RE's [A-Z]-led head never anchors on a digit
        "Ctrl-XVR:Value",  # hyphenated head, no digit index -> not PV-shaped (pins lookahead)
        "MODE1:AUTO",  # ALLCAPS enum, not a PV
        "NTScalar:Value",  # PVA type name
    ):
        assert not flags(benign), f"detector must pass benign token {benign!r}"


def test_site_re_catches_username_paths_and_subdomains() -> None:
    """The two denylist hardenings: a username filesystem path carrying '.'/'-' in the name, and any
    on-site multi-label subdomain host, both slipped the pre-hardening pattern (``/home/\\w+/`` and
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
