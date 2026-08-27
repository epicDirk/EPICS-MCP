#!/usr/bin/env python3
"""The structural device/PV-name detector, with ONE home and two readers.

Realistic, specific EPICS device/PV names are the leak vector a denylist cannot enumerate: a
facility has thousands, all structurally like the synthetic ones. So instead of listing real names
this flags anything ESS-PV-SHAPED and passes only DECLARED synthetic placeholders.

WHY IT LIVES HERE RATHER THAN IN THE TEST THAT USED TO OWN IT. Two surfaces need the same needle
and neither may carry a copy of it (the repository rule is "never copy it, link it"):

  * ``tests/test_guide.py`` scans every TRACKED text file, which is the CI-effective check,
  * ``scripts/check_commit_message.py`` scans the COMMIT MESSAGE, a surface no file guard can
    reach. That gap is measured rather than imagined, and the measuring rule belongs with the
    figure: running this detector over ``git log --format=%B`` flags 2 of 722 commit bodies
    (2026-08-15). The signature half of the same surface is larger and is stated where it is
    acted on, in ``check_commit_message.py``.

⚠️ THIS MODULE IS NOT SHIPPED. ``scripts/`` is absent from ``[tool.hatch.build.targets.sdist]
only-include`` in pyproject.toml, deliberately and with its reason stated there, so this detector
reaches no sdist and no wheel. It is development surface for this repository's own guards. Anyone
looking for it from outside a checkout will not find it, and that is the intended scope rather
than an oversight.

EVERY PV SHAPE NAMED IN THIS FILE IS A DECLARED SYNTHETIC PLACEHOLDER, and that is load-bearing
rather than tidy: this file is a tracked ``.py``, so ``test_guide.py`` reads it with the very
detector defined below. A realistic example in a comment here would be a leak inside the guard
against leaks. Verified by running the scan over this file.
"""

from __future__ import annotations

import re

# The negative lookbehind anchors the match at a run boundary, so it never fires mid-token inside a
# sanctioned 'SIM:PS-01:Cur-RB' placeholder (it would otherwise re-anchor on 'PS-01'). Three
# structural gates keep it off non-PV text: (1) a digit must appear SOMEWHERE in the device path, a
# real ESS numeric index sits either in the Section-Subsection head ('SIM:PS-01:...') OR in a later
# device segment ('SIM-ZZ:Diag-BPM-09:...'); this alone excludes 'NTScalar:Value'. (2) The
# [A-Z]-led head never anchors on a digit-led ISO-8601 timestamp. (3) The head needs >=1 hyphenated
# segment (the ESS Sec-Sub## shape), which is what excludes bare ALLCAPS enums like 'MODE1:AUTO'.
PV_RE = re.compile(
    r"(?<![A-Za-z0-9:./-])"  # anchor at a run boundary (not mid SIM:.. / path / date)
    r"(?=[A-Za-z0-9:-]*[0-9])"  # a digit appears anywhere in the device path (head OR a segment)
    r"[A-Z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)+"  # head: >=1 hyphenated segment (ESS Sec-Sub##)
    r":[A-Za-z][A-Za-z0-9-]*"  # device segment after ':'
    r"(?::[A-Za-z][A-Za-z0-9-]*)*"  # optional further ':'-segments (signal)
)
# A ca/pva scheme prefix (incl. the TLS forms pvas://, cas://) is stripped before scanning: the '/'
# in PV_RE's lookbehind would otherwise stop it anchoring on a pasted 'pvas://<real-pv>', the
# natural Phoebus/pvget copy form, and the repo steers operators toward TLS-secured planes.
PV_SCHEME_RE = re.compile(r"\b(?:pvas?|cas?)://", re.IGNORECASE)
# Declared "this-is-invented" markers. The COMPOUND ones match as a head PREFIX (DEV-TEST01 splits
# to ['DEV','TEST01'], so a whole-segment test would miss them); the single-word ones match as a
# WHOLE '-'-segment only, a prefix test on 'SIM' would wrongly exempt a real head like 'SIMD-01'.
# Deliberately NO generic ESS abbreviations (VAC/PS/SYS collide with real system names); the
# lookbehind already lets the one documented doc placeholder (SIM:PS-01:Cur-RB) pass.
SYNTHETIC_COMPOUND = ("DEV-TEST", "ZZZ-FAKE")
SYNTHETIC_SEGMENTS = frozenset({"SIM", "EXAMPLE", "FAKE", "DEMO", "DUMMY", "MOCK"})


def is_synthetic_pv(token: str) -> bool:
    """A flagged PV-shaped token is a declared synthetic placeholder when its head (the part before
    the first ':') is prefixed by a compound marker (DEV-TEST/ZZZ-FAKE) or carries a single-word
    marker as a whole '-'-segment."""
    head = token.split(":", 1)[0]
    if head.startswith(SYNTHETIC_COMPOUND):
        return True
    return any(segment in SYNTHETIC_SEGMENTS for segment in head.split("-"))


def pv_leak_tokens(text: str) -> list[str]:
    """The device/PV names in ``text`` that are not a declared synthetic placeholder, the
    realistic, specific names that must not appear on the knowledge surface. A ca/pva scheme prefix
    is neutralised first so a pasted 'pvas://<real-pv>' still anchors. (ISO-8601 timestamps need no
    filter: PV_RE's [A-Z]-led head never anchors on a digit-led token.)

    TWO PASSES, AND THE SECOND IS THE FIX FOR A MEASURED BLIND SPOT (GQ-208). A '#' where the
    first segment starts hid the whole name: the 'DEV-TEST01:#Ctrl-EVR-01' shape was reported as
    nothing at all, not merely reported short. TWO gates cause that, not one, so widening either
    alone leaves the other blind: the segment class ':[A-Za-z]' wants a letter immediately after
    the colon, AND the digit lookahead's character class stops at the '#', so a digit sitting
    behind the '#' cannot be reached at all. Both halves were measured before and after.

    WHY A SECOND PASS RATHER THAN A WIDER PATTERN, and this is the load-bearing part. Letting the
    pattern run THROUGH a '#' lets one match swallow the next, and is_synthetic_pv judges a whole
    token by the head before its first colon, so a synthetic head would then vouch for a real
    device name sitting behind the '#'. Measured: six shapes that are reported today go silent
    that way. A union of two passes cannot do that, by construction rather than by measurement:
    the first pass is the old behaviour byte for byte, so nothing reported today can stop being
    reported, and a union only ever adds. PV_RE is therefore untouched, and so is the count of
    structural gates documented at its definition. That count is deliberately NOT repeated here:
    a number restated in a second place is a number that can rot in one of them, and this
    docstring has no guard holding it against the pattern it would be describing.

    "ONLY EVER ADDS" IS MEANT LITERALLY, DOWN TO DUPLICATES AND ORDER, and the code below is
    written that way rather than merely aiming at it. The first pass is returned as the old
    comprehension built it, repeated tokens and text order included, and the second pass APPENDS
    only names the first did not produce. The old result is therefore a prefix of the new one, for
    any input. A tidier "collect both passes into one deduplicated list" fails that: it silently
    collapses a name that legitimately occurs twice, which changes a shared function's contract
    for every caller in order to make one guard read better.

    A SECOND-PASS NAME IS REPORTED WITHOUT ITS '#', so it can be a string that does not appear
    verbatim in the scanned text. That is the point rather than a defect: what leaks is the NAME,
    not the byte run that carried it. It is stated here because ``test_guide.py`` prints the token
    in its failure message, so a reader who greps for it may find nothing and needs to know why.

    THE SAME GLUING CAN DESTROY A SYNTHETIC MARKER, which is the one way this guard can newly cry
    wolf. Removing the '#' joins whatever alphanumeric run precedes it INTO the head, and the head
    is what is_synthetic_pv reads, so a marker that was whole becomes a prefix of something else:
    a 'Ref' written flush against a sanctioned DEV-TEST head yields a flagged 'RefDEV-TEST...'.
    Only a run flush against the '#' does it; a space or a bracket in between keeps the head
    intact. Measured over the tracked tree and the whole history at the time of writing: the
    triggering context appears in 9 of 221 files and 3 of 869 commit bodies, and none of them
    produces a token, so the cost today is zero and the mechanism is named rather than hidden.

    The shape named above is SYNTHETIC for the reason the module header gives: this file is read
    by the detector it defines, and without the declared-shapes allowance that only
    tests/test_guide.py receives. An invented head in this docstring would redden
    test_knowledge_files_are_facility_agnostic.
    """
    scanned = PV_SCHEME_RE.sub("  ", text)
    found = [
        token for match in PV_RE.finditer(scanned) if not is_synthetic_pv(token := match.group(0))
    ]
    if "#" not in scanned:
        # Not a shortcut but an identity: with no '#' to remove, the second pass would read the
        # same string and can only re-find what is already in `found`. What it saves is measured
        # rather than assumed, and it is lopsided: it fires for 29 of the 221 tracked text files
        # (a '#' is a comment and a heading character, so most files carry one) but for 812 of
        # 869 commit bodies. The surface where a block is most disruptive is the one it helps.
        return found
    seen = set(found)
    for match in PV_RE.finditer(scanned.replace("#", "")):
        token = match.group(0)
        if not is_synthetic_pv(token) and token not in seen:
            seen.add(token)
            found.append(token)
    return found
