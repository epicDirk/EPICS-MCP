"""CLI for the read-only config self-check (``epics-doctor``).

Probes every CONFIGURED plane (a transport probe, refined on success by an identity probe, up to
two requests for a healthy plane, THREE on the archiver, whose identified appliance is also asked
whether it is ingesting; retries on a 5xx add more) and prints whether it is reachable,
whether the CA bundle works, whether the service **identifies itself as the one that URL is
supposed to point at**, what the ChannelFinder privacy redaction is set to, and what the two write
gates would allow and where, the ``flutter doctor`` of this server. Read-only, it probes, never
writes, and it touches exactly the planes that are CONFIGURED (a disabled plane makes no network
call). ⚠️ The write-gate block is INFORMATIVE: it never moves the verdict or the exit code, and it
describes THIS process's environment rather than a running server's.

Exit code (a DELIBERATE convention, unlike the other CLIs where a finding is exit 0, doctor is
a scriptable pass/fail):

* ``0``: nothing failed and no identity probe failed (healthy, honestly disabled/info-only,
  reachable with its identity ``unverified``, a 2xx that just could not be named, or ``no_ingest``,
  identity proven but the service is not doing its job, which is a finding and not a failure);
* ``1``: a configured plane HARD-failed (unreachable / ca_error / api_error / config_error /
  backend_down / probe-disconnect), **or an internal EpicsError**. Those two deliberately share a
  code (QA-15, argued at the ``except`` in :func:`main`) and are told apart on stderr, which only
  the internal one writes;
* ``2``: a usage error, bad arguments only (argparse's own convention);
* ``3``: INCONCLUSIVE: a configured plane is reachable but its identity probe FAILED (a served
  non-2xx like a 401/404, a transport error, or a refused redirect on the identity endpoint). Not a
  hard failure (the plane's TOOL endpoints may work), but not a silent all-clear either.

The exit code relates to ``--json`` as: ``0`` = ``ok`` ∧ no ``inconclusive_identity_planes``; ``3``
= ``ok`` ∧ some ``inconclusive_identity_planes``; ``1`` = not ``ok``, or an internal error and no
report at all; ``2`` = usage. So ``ok`` alone is True for BOTH exit 0 and exit 3, do NOT derive the
exit code from ``ok`` alone.

⚠️ Exit ``0`` means "nothing failed", NOT "everything was confirmed": a plane can be reachable with
its identity unverified and still exit 0 (that is honest, not healthy, see ``doctor.py``). A
machine reader must therefore look at ``verification_complete`` / ``unverified_planes`` /
``inconclusive_identity_planes`` / ``degraded_planes`` in ``--json``, not only at the exit code,
and for POSITIVE confirmation assert ``identified_planes`` is non-empty (``verification_complete``
is vacuously true on an empty config). ``degraded_planes`` is the one none of the others covers: a
plane there proved its identity and is measurably not doing its job, so it appears IN
``identified_planes``, leaves ``ok`` and ``verification_complete`` True, and exits 0.

Usage::

    epics-doctor [--probe-pv DEV-TEST01:Ctrl-EVR-01:12VValue] [--timeout 5] [--json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Literal

from epics_mcp.cli_common import add_version_argument, configure_stdout, positive_timeout
from epics_mcp.errors import EpicsError
from epics_mcp.services.doctor import _FAILING_STATUSES, DoctorReport, run_doctor
from epics_mcp.write_posture import OlogWriteGateReport, PvWriteGateReport

#: One glyph per status for the human-readable render (deterministic). A status gets a mark of
#: its own rather than borrowing ✓ or ✗ when it is neither "confirmed" nor "broken". Every one of
#: them is glossed here, because a mark nobody explains is a character the reader cannot act on:
#: ``·`` = the plane is OFF, its URL is unset, so no client was built and nothing was probed
#: (exit 0), ``i`` = the live plane has no URL to probe and no ``--probe-pv`` was given, so the
#: line states the posture it would use rather than a verdict (exit 0), ``?`` = answered 2xx but
#: not nameable (exit 0), ``!`` = the identity probe failed (exit 3), ``~`` = identity IS proven
#: and the service is reachable, but it is not doing its job (exit 0, the archiver that archives
#: nothing). ``~`` is deliberately not ``?``: that one means "we could not tell what this is",
#: and here we can. ``·`` and ``i`` are likewise not ``✓``: nothing was verified in either case.
#:
#: What each mark REPLACED, stated per mark because the earlier wording put them in one bucket
#: and that bucket was measured wrong: ``?`` and ``~`` used to be reported as ``ok`` (``✓``),
#: while ``!`` stood as ``unverified`` (``?``) until the identity probe learned to tell a served
#: non-2xx from an unreadable body. ``·`` and ``i`` were never conflated with anything: they
#: have carried their own marks since the first version of this CLI. No count is given here on
#: purpose; the mapping below is the enumeration, and a figure beside it can only drift.
_STATUS_MARK = {
    "ok": "✓",
    "disabled": "·",
    "info": "i",
    "unverified": "?",
    "no_ingest": "~",
    "identity_probe_failed": "!",
    "config_error": "✗",
    "ca_error": "✗",
    "api_error": "✗",
    "unreachable": "✗",
    "disconnected": "✗",
    "backend_down": "✗",
}


def _exit_category(report: DoctorReport) -> Literal["failed", "inconclusive", "clean"]:
    """The ONE verdict precedence, consumed by both :func:`main` (→ exit code) and :func:`_render`
    (→ verdict line) so the two cannot drift. A hard failure dominates an inconclusive identity
    probe, which dominates a clean run: ``failed`` → exit 1, ``inconclusive`` → exit 3, ``clean`` →
    exit 0. ``report.ok`` is False iff a plane HARD-failed; ``inconclusive_identity_planes`` is
    non-empty iff a probe failed (with ``ok`` still True, an inconclusive probe is not a hard
    failure). A ``clean`` run may still be ``unverified`` (answered 2xx, unnameable), that is exit
    0, honest-not-confirmed."""
    if not report.ok:
        return "failed"
    if report.inconclusive_identity_planes:
        return "inconclusive"
    return "clean"


#: Category → process exit code. The single mapping :func:`main` uses.
_EXIT_CODE: dict[str, int] = {"failed": 1, "inconclusive": 3, "clean": 0}


#: Longest configured value this block prints verbatim. Past it the value is cut and the cut is
#: named, because a single env var can be arbitrarily long (measured: a 70 000-character address
#: list was echoed in full into one line) and a report nobody can read is a report nobody checks.
_VALUE_LIMIT = 200


def _one_line(value: str, limit: int = _VALUE_LIMIT) -> str:
    """*value* made safe to interpolate into a fixed-layout line, and capped.

    Every string this block prints comes from the environment, and the block exists so that a
    reviewer can hold a configuration file somebody ELSE wrote up against what it would do. That
    makes the values hostile input, and it was exploitable rather than theoretical. Measured: with
    ``EPICS_MCP_PV_WRITE_PATTERN`` set to ``.*|`` followed by a carriage return and a forged
    ``PV names allowed: ^MPS:ONLY:.*$``, the pattern is a valid regex, ``SafetyLayer`` accepts any
    PV under it, and a terminal shows the forged narrow allowlist because the CR returns the cursor
    to column 0. A newline instead forges whole extra lines, including a second ``Overall:`` and a
    second ``PV write: OFF``, above the real ones.

    So control characters, which no legitimate value in this configuration carries, are shown as
    their escapes instead of acted on. ``--json`` is deliberately NOT put through this: a JSON
    encoder escapes them safely on its own, and a machine reader should see the value as configured.
    """
    escaped = _escaped(value)
    return escaped if len(escaped) <= limit else f"{escaped[:limit]}... (cut after {limit} chars)"


def _escaped(value: str) -> str:
    """*value* with every non-printable shown as its escape. The cap is :func:`_one_line`'s half.

    Split out because the rest of the report needs the ESCAPING and must not have the CAP. The
    forging hole is not a property of the write block, it is a property of any line built from a
    string this process did not author, and the report has two more such sources, both measured:
    the privacy allowlists come from ``EPICS_MCP_CHANNELFINDER_SAFE_*`` verbatim, and a plane's
    ``detail`` embeds the raw EPICS search-path values (``_live_search_posture`` interpolates
    ``os.environ`` after a ``strip()``, which trims the ENDS and leaves an interior newline alone).
    A remedy line can legitimately run past 200 characters, though, and cutting one would trade a
    forged line for a truncated instruction.
    """
    return "".join(
        char if char.isprintable() or char == " " else repr(char)[1:-1] for char in value
    )


def _pv_write_lines(pv: PvWriteGateReport) -> list[str]:
    """The PV write gate's lines: whether it is armed, WHAT it allows, and WHERE a write can go.

    The "where" half is the one an approver actually asks for, and it is why the reach appears
    here rather than being left to the live plane's own posture line. Those two answer different
    questions and can disagree: the live line judges the ACTIVE provider's auto-address switch
    only, while the write gate demands both providers, so a configuration can read
    ``localhost-isolated`` up there and still be refused a write-enabled start. Measured, not
    feared. This line is computed with the gate's own function, so it is the gate's answer rather
    than a second opinion about it.

    ⚠️ It predicts ONE of the PV gate's FOUR start conditions. The pattern lines above it cover two
    (a pattern must be set, and it must compile) and the audit line below covers the fourth, and
    none of the four is evaluated together, which is why nothing here says the server would start.
    The figure said three until an independent round measured the fourth: a pattern that does not
    compile makes ``SafetyLayer`` raise, and the block used to print it exactly like a working one.
    """
    if not pv.armed:
        return ["  PV write:   OFF (no PV write can leave this server)"]
    lines = ["  PV write:   ARMED"]
    if not pv.name_pattern:
        lines.append(
            "              PV names allowed: (none set, so a write-enabled server refuses to start)"
        )
    elif not pv.pattern_is_valid_regex:
        # Before the branch below, deliberately: a broken pattern is not a narrow one, and the
        # reassurance under the else-branch ("the WHOLE name has to match") would be describing an
        # allowlist that never comes into existence.
        lines.append(f"              PV names allowed: {_one_line(pv.name_pattern)}")
        lines.append(
            "              (NOT a valid regular expression, so a write-enabled server refuses "
            "to start)"
        )
    elif pv.pattern_allows_every_name:
        lines.append(
            f"              PV names allowed: {_one_line(pv.name_pattern)} "
            "(this spelling allows EVERY PV)"
        )
    else:
        lines.append(f"              PV names allowed: {_one_line(pv.name_pattern)}")
        # The second line says WHAT was checked rather than calling the pattern narrow. The wide
        # check is a comparison against a fixed list of spellings, so a pattern that admits every
        # name but is written outside that list (an alternation with an empty branch, an inline
        # flag) lands here, and a reader who took silence for narrowness would be wrong.
        lines.append(
            "              (a regular expression; the WHOLE name has to match. Whether it is "
            "wide was"
        )
        lines.append(
            "              checked against a fixed list of spellings, not by reading the "
            "expression)"
        )
    lines.append(f"              at most {pv.rate_limit_per_minute} writes per minute")
    if pv.search_reach_violations:
        lines.append(
            "              search reach: NOT loopback-only, so a write-enabled server refuses "
            "to start:"
        )
        # ⚠️ The ONE escaping call in this file that cannot be proven able to go red, and it is
        # labelled rather than removed: ``write_reach_violations`` builds these strings with
        # ``!r``, so their control characters are already escaped when they arrive. This is a
        # second layer that only starts earning its place if that function ever stops using
        # ``!r``, and a reader who finds it uncovered by the forgery test should know why.
        lines += [f"                {_one_line(v)}" for v in pv.search_reach_violations]
    else:
        lines.append("              search reach: loopback-only")
    return lines


#: Printed under the two Olog target verdicts that can rest on the allowlist (BG-DCMP). The address
#: above them is REBUILT by ``url_without_credentials``, which lower-cases the host and drops a
#: query and a fragment, while the gate reads the RAW ``olog_url``; measured, a target configured
#: in mixed case prints exactly the string already in the allowlist and is refused anyway, so a
#: reader repairing the allowlist from that line compares two values that read identically.
#:
#: ⚠️ It deliberately claims NOTHING about a comparison having taken place, and the first draft did.
#: The refusal is reached by six states in which no allowlist is ever read: five SEC-2 vetoes, plus
#: an exactly-allowlisted target with ``EPICS_MCP_OLOG_WRITE_ALLOW_REMOTE`` unset, where the ``and``
#: chain short-circuits first. In a seventh, a plain-http allowlisted target, the comparison ran and
#: SUCCEEDED and the https rule is what denies. A sentence naming the comparison sent the reader of
#: all seven hunting for a character difference that is not the cause, which is the class of defect
#: this whole line of tickets exists to remove. It also no longer calls the value an ADDRESS: on an
#: unparseable URL the slot holds the literal ``(unparseable)``, and nothing would reach it.
_TARGET_IS_NOT_THE_CONFIGURED_STRING = (
    "              (shown for reading. The string the gate works from is EPICS_MCP_OLOG_URL",
    "              exactly as configured, and this line need not match it character for character)",
)


def _olog_write_lines(olog: OlogWriteGateReport) -> list[str]:
    """The Olog write gate's lines. Two of its states are read backwards without a word of prose.

    An EMPTY logbook allowlist on an armed gate is deny-all, not unrestricted, and a target that
    the test-server boundary refuses makes an ARMED gate write nothing at all. Both are stated in
    words rather than left to the reader, because the naive reading of each is its opposite. And
    an allowlisted REMOTE target is called what it is: those writes reach a real logbook, which is
    a different approval from a loopback sandbox even though the gate permits both.

    ⚠️ The printed address is the NORMALISING redaction, which is the right one here because the
    question this block asks is which address a write would reach. The two branches whose verdict
    can rest on the ALLOWLIST, refused and allowlisted-remote, say so out loud; the loopback branch
    does not, because being loopback is a property of the address itself and no configured string
    is being matched. That the allowed branch needs it too was a QA finding, not the first draft:
    the word "allowlisted" IS the result of a comparison, and measured, a target configured as
    ``https://Olog.Example.org/Olog`` and allowlisted under that same spelling prints
    ``https://olog.example.org/Olog``, so an operator tidying the allowlist from that line turns a
    working gate into a deny-all.
    """
    if not olog.armed:
        return ["  Olog write: OFF (no logbook entry can leave this server)"]
    lines = ["  Olog write: ARMED"]
    if olog.logbooks:
        lines.append(f"              logbooks: {_one_line(', '.join(olog.logbooks))}")
    else:
        lines.append("              logbooks: (none set, so every Olog write is denied)")
    lines.append(f"              at most {olog.rate_limit_per_minute} writes per minute")
    target = _one_line(olog.target_url)
    if not olog.target_url:
        lines.append("              target: (no Olog URL set, so there is nothing to write to)")
    elif not olog.target_allowed:
        lines.append(
            f"              target: {target} is NOT a permitted write target, "
            "so every write is denied"
        )
        lines += _TARGET_IS_NOT_THE_CONFIGURED_STRING
    elif olog.target_is_loopback:
        lines.append(f"              target: {target} (loopback, a local test server)")
    else:
        lines.append(
            f"              target: {target} (REMOTE and allowlisted: these writes "
            "reach a real logbook)"
        )
        lines += _TARGET_IS_NOT_THE_CONFIGURED_STRING
    return lines


def _write_safety_lines(report: DoctorReport) -> list[str]:
    """The write-gate block: what may be written, and where. Informative, and glyph-free.

    Glyph-free on purpose. The marks are the per-plane vocabulary, explained by a legend that ships
    with the server; borrowing one here would either invent a pairing that legend has to carry or
    reuse a mark for a different question. This block states its verdicts in words instead.

    The heading names WHOSE environment was read, which is the block's sharpest limit rather than a
    caveat: this command runs in its own process, while a running server was started by an MCP
    client from a different env block. Without that sentence, "PV write: OFF" reads as a statement
    about the server that is answering the client, and it is not one.
    """
    write = report.write_safety
    lines = [
        "Write gates (as THIS command's environment has them; a running server may have been",
        "started with a different one):",
    ]
    lines += _pv_write_lines(write.pv)
    lines += _olog_write_lines(write.olog)
    audit = write.audit
    if not audit.path:
        lines.append(
            "  audit log:  not set, so the trail goes to stderr and is lost on restart "
            "(a write-enabled"
        )
        lines.append("              server refuses to start without a durable path)")
        return lines
    path = _one_line(audit.path)
    if audit.writable is True:
        lines.append(f"  audit log:  {path}, writable")
    elif audit.writable is False:
        lines.append(f"  audit log:  {path}, NOT usable: {_one_line(audit.note or '')}")
    else:
        lines.append(f"  audit log:  {path}, not decidable here: {_one_line(audit.note or '')}")
    # Which of the two sentences below applies is decided by ``os.path.isabs``, NOT by whether the
    # resolved string differs. That difference was the old test and it is a DIFFERENT question:
    # ``os.path.abspath`` also NORMALISES, so an absolute path can come back respelled while still
    # naming the same file for both processes. Measured on Windows: an absolute path written with
    # FORWARD slashes comes back with backslashes, and the line used to call it "relative". That
    # spelling is the one an MCP client's JSON block invites, because a backslash has to be escaped
    # there, so it was not an exotic case. Same on POSIX for a ``/./`` segment or a doubled
    # separator.
    if not os.path.isabs(audit.path):
        # A RELATIVE path is resolved against the current directory, and the two processes have
        # different ones: this command runs in the operator's shell, the server is started by an
        # MCP client somewhere else. Printing only the configured string would say "writable"
        # about a file the server will never touch, without saying which file was checked. The
        # heading disclaims the environment; the working directory is not part of it.
        lines.append(
            "              (relative, so the server resolves it against ITS working directory; "
            f"checked here as {_one_line(audit.resolved_path)})"
        )
    elif audit.resolved_path != audit.path:
        # Absolute, merely respelled. Still printed, because the note above quotes the resolved
        # form and an unexplained second spelling reads like a second file. No warning attached:
        # there is no working directory in this case for the two processes to disagree about.
        lines.append(
            f"              (the same file, normalised to {_one_line(audit.resolved_path)})"
        )
    return lines


def _armed_gate_names(report: DoctorReport) -> list[str]:
    """The write gates that are ARMED, for the verdict tail. Empty is the normal case."""
    write = report.write_safety
    return [name for name, armed in (("PV", write.pv.armed), ("Olog", write.olog.armed)) if armed]


#: The report's honest-but-not-healthy lists, in verdict-precedence order, each with the word the
#: tail clause uses for it. The word is the one a ``--json`` reader meets in the field name, so the
#: human sentence and the machine key stay one vocabulary; it is NOT mechanically the field name
#: minus a suffix, and the entry that shows why is ``inconclusive_identity_planes``, whose
#: field-minus-suffix would be ``inconclusive_identity`` while both the guide and the verdict branch
#: above call that state "inconclusive".
#:
#: Read as FIELDS of the report rather than re-derived from ``report.planes``: ``run_doctor`` has
#: already classified every plane into exactly these lists, and a second classification here would
#: be free to drift from the one ``--json`` publishes.
_OTHER_STATES: tuple[tuple[str, str], ...] = (
    ("inconclusive_identity_planes", "inconclusive"),
    ("degraded_planes", "degraded"),
    ("unverified_planes", "unverified"),
)

#: The fields a branch may declare as already named. Pinned so a typo in a ``named=`` argument is
#: loud: without it the branch keeps its own category in the tail as well, and the verdict names the
#: same planes twice while every test stays green (measured).
_NAMEABLE_STATES: frozenset[str] = frozenset(field for field, _ in _OTHER_STATES)


def _other_states_clause(report: DoctorReport, *, named: frozenset[str]) -> str:
    """The honest-but-not-healthy states this verdict has NOT already named, as one tail clause.

    ONE seam for the three branches that can hide something, so a category can be added to the
    report without being carried into each of them by hand: a branch declares what IT names and
    gets the rest. ⚠️ It is three of the five verdict branches, NOT all five: the
    ``verification_complete`` and bare-``unverified`` branches are reachable only when the other
    lists are empty, so they have nothing to add and do not call this.

    Measured on the code this replaces, over every state ``run_doctor`` can build: eleven of
    sixteen printed a verdict that hid the NAME of a plane in one of these three categories, and
    the operator who fixed what the sentence named then found the next thing. (Twelve hid a plane
    name of any kind; the twelfth is the failed branch naming no failure at all, which is its own
    change.)

    Planes are NAMED, never counted alone. The clause this generalises counted them ("N other
    plane(s) also unverified") while the two branches that disclosed at all printed names, so the
    weaker standard was the one being copied. A count says something is wrong; the reader still has
    to find WHERE, and the report is the only thing that knows.

    No positional wording ("above", "below"), for the reason recorded at ``_REMEDY`` in
    ``services/doctor.py``: this clause is appended to sentences of very different lengths, so a
    direction is a promise about layout that the layout does not keep.
    """
    unknown = named - _NAMEABLE_STATES
    if unknown:  # pragma: no cover - a caller typo, loud rather than a silently doubled clause
        raise ValueError(f"not honest-but-not-healthy report fields: {sorted(unknown)}")
    parts: list[str] = []
    for field, word in _OTHER_STATES:
        if field in named:
            continue
        planes: list[str] = getattr(report, field)
        if planes:
            parts.append(f"{len(planes)} {word} ({', '.join(planes)})")
    return f" Also NOT healthy: {'; '.join(parts)}." if parts else ""


def _render(report: DoctorReport) -> str:
    """Render a human-readable per-plane report (deterministic).

    EVERY string this builds from a value the process did not author goes through :func:`_escaped`
    or :func:`_one_line` first, and that is the whole report rather than the write block alone: a
    forged line is worth the same wherever it is injected, and the block was only the first place
    the question got asked. ``test_no_configured_value_can_forge_a_line_anywhere_in_the_report``
    checks the finished report instead of enumerating these call sites, so a line added later is
    covered without anyone remembering to add it to a list.
    """
    lines = ["EPICS-MCP doctor, read-only per-plane config check", ""]
    for plane in report.planes:
        mark = _STATUS_MARK.get(plane.status, "?")
        lines.append(f"  {mark} {plane.plane:<14} {plane.status}")
        if plane.detail:
            lines.append(f"      {_escaped(plane.detail)}")
    lines.append("")
    lines.append("Privacy (ChannelFinder redaction):")
    owners = _escaped(", ".join(report.privacy.cf_safe_owner_accounts)) or (
        "(empty, all owners redacted)"
    )
    props = _escaped(", ".join(report.privacy.cf_safe_property_names)) or (
        "(empty, all properties redacted)"
    )
    lines.append(f"  owner allowlist:    {owners}")
    lines.append(f"  property allowlist: {props}")
    lines.append("")
    lines += _write_safety_lines(report)
    lines.append("")
    # One precedence, shared with main() via _exit_category, so the verdict word and the exit code
    # can never drift: failed → PROBLEM (exit 1), inconclusive → INCONCLUSIVE (exit 3), clean → OK.
    category = _exit_category(report)
    if category == "failed":
        # The failures are NAMED here, and they lead. This branch outranks every other state, so it
        # is the one that hides the most, and the tail below would otherwise make the planes nobody
        # has to fix today the only ones the last line names. There is no failed_planes field to
        # read: the report carries a list per honest-but-not-healthy category and none for the
        # failures, so they are derived from the plane statuses. That is the SAME derivation
        # run_doctor uses for `ok` (services/doctor.py), so the headline and the exit code cannot
        # disagree, and nothing on the wire changes.
        failed = [plane.plane for plane in report.planes if plane.status in _FAILING_STATUSES]
        # The empty case is not a state run_doctor can build (it sets `ok` False only BECAUSE one of
        # these statuses is present) but a hand-built report can, and a "0 plane(s) failed" headline
        # under a PROBLEM verdict would be worse than the sentence this one replaces.
        what = (
            f"{len(failed)} configured plane(s) FAILED: {', '.join(failed)}"
            if failed
            else "a configured plane failed"
        )
        # The full stop is the tail's, not the headline's: without it this branch, and only this
        # branch, ran two sentences together, since the other two end their own text in one.
        verdict = f"PROBLEM, {what} (see above)." + _other_states_clause(report, named=frozenset())
    elif category == "inconclusive":
        # The S4 lie in its exact form: "all configured planes healthy" was printed for a
        # ChannelFinder URL at a week-dead container because a neighbour answered 401. That probe
        # FAILED: it is not a silent OK. Not a hard PROBLEM either (the plane's tool endpoints may
        # work), so it earns its own INCONCLUSIVE verdict and exit 3, never "OK".
        planes = ", ".join(report.inconclusive_identity_planes)
        n = len(report.inconclusive_identity_planes)
        # This branch used to carry its own tail, "(N other plane(s) also unverified)", which named
        # ONE of the two categories it outranks and named it as a COUNT. _other_states_clause covers
        # both and names the planes; the count-only form is gone rather than kept beside it, because
        # one sentence disclosing the same kind of fact two ways is how a reader learns to skip it.
        verdict = (
            f"INCONCLUSIVE, {n} identity probe(s) FAILED (reachable, but the identity endpoint "
            f"did not return a usable response): {planes}. Not a confirmed failure, but not "
            "confirmed healthy, see the '!' lines above."
            + _other_states_clause(report, named=frozenset({"inconclusive_identity_planes"}))
        )
    elif report.degraded_planes:
        # A degraded plane is exit 0 and leaves verification_complete True, so without this branch
        # the run would print the tool's STRONGEST confirmation ("answered AS ITSELF") directly
        # under a "~" line saying the appliance archives nothing. That is the S4 lie in a new
        # costume: the sentence would be literally true (it did answer as itself) and would still
        # tell the operator the opposite of what the report above says. The verdict word changes;
        # the exit code does not (_exit_category is untouched, this stays "clean" -> 0).
        planes = ", ".join(report.degraded_planes)
        n = len(report.degraded_planes)
        verdict = (
            f"OK, nothing failed, but {n} plane(s) proved their identity and are NOT doing their "
            f"job: {planes}. Reachable and confirmed is not working; see the '~' lines above."
            + _other_states_clause(report, named=frozenset({"degraded_planes"}))
        )
    elif report.verification_complete:
        # verification_complete is "no plane unverified or inconclusive", vacuously true when
        # nothing was probed at all. The strong sentence is earned by actual identifications: with
        # zero, "answered AS ITSELF" would read as confirmation of probes that never ran (measured:
        # it printed for a completely empty config). Same source as the JSON's identified_planes, so
        # the human verdict and the machine signal cannot drift.
        verified = len(report.identified_planes)
        if verified:
            verdict = f"OK, every identity-probed plane answered AS ITSELF ({verified} verified)"
        else:
            verdict = (
                "OK, nothing failed, but nothing was identity-verified either "
                "(no REST plane is configured)"
            )
    else:
        # Clean (exit 0) but some plane answered 2xx without a nameable identity, honest, not
        # confirmed. Saying "healthy" would claim a confirmation we do not have.
        unverified = ", ".join(report.unverified_planes)
        verdict = (
            f"OK, no plane failed, but {len(report.unverified_planes)} could not prove its "
            f"identity: {unverified}. Reachable ≠ confirmed; see the '?' lines above."
        )
    # An ARMED write gate earns a sentence ON the verdict, not only in the block above it. The
    # verdict is the LAST line, it is labelled "Overall", and by layout convention everything above
    # it is in its scope, so "Overall: OK" under a line reading "PV write: ARMED" reads as an
    # all-clear on the write posture. It is not one: nothing in this report checks a write gate,
    # it only reports what one is set to. Same reason the degraded and inconclusive branches above
    # exist. UNLIKE them it appends a second line instead of choosing a different verdict word: the
    # verdict is about the planes and stays true as it is, what was missing is a fact beside it.
    # The exit code is untouched either way.
    lines.append(f"Overall: {verdict}")
    armed = _armed_gate_names(report)
    if armed:
        subject = f"the {' and '.join(armed)} write gate" + ("s are" if len(armed) > 1 else " is")
        lines.append(
            f"         Nothing here CHECKED a write gate, and {subject} ARMED "
            "(see the block above)."
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Run the self-check, print the report. Returns 0 (clean) / 1 (a plane hard-failed, OR an
    internal error, see the ``except`` below) / 2 (a usage error) / 3 (reachable, but an identity
    probe failed, inconclusive)."""
    # Before the parser (QA-8): argparse prints ``--help`` inside ``parse_args``, so a non-ASCII
    # character in any help text would die on a cp1252 console if the reconfigure came later.
    configure_stdout()
    parser = argparse.ArgumentParser(
        # prog pinned: argparse's default is interpreter dependent and prints an absolute console
        # script path on 3.14 (QA-41). Same reason at every entry point of this package.
        prog="epics-doctor",
        description="Read-only config self-check: is every configured EPICS plane reachable?",
    )
    add_version_argument(parser)
    parser.add_argument(
        "--probe-pv",
        default=None,
        help="probe a live PV to pass/fail the live plane (default: info only, no live read)",
    )
    parser.add_argument(
        "--timeout",
        type=positive_timeout,
        default=None,
        help="per-plane timeout in seconds, > 0 (default: config, 5.0 s)",
    )
    parser.add_argument("--json", action="store_true", help="emit the raw report as JSON")
    args = parser.parse_args(argv)

    try:
        report = asyncio.run(run_doctor(probe_pv=args.probe_pv, timeout=args.timeout))
    except EpicsError as exc:  # gatherers are total, so this is only a genuine internal error
        # Exit 1, the command-failed code (QA-15). This used to return 2, which
        # ``cli_common._USAGE_ERROR`` and argparse both define as a USAGE error: the caller was
        # told it had passed something wrong when the command itself had broken.
        #
        # Named trade-off, taken deliberately: 1 is also the code for "a configured plane hard
        # failed", so a script can no longer tell an internal error from a genuine negative
        # finding by exit code alone. That is the lesser evil. The two are distinguishable on
        # stderr (this branch is the only one that writes a ``doctor:`` line, and it is the only
        # path that produces NO report on stdout), whereas the old value collided with a
        # convention the other three CLIs share and argparse enforces from outside this file.
        sys.stderr.write(f"doctor: {exc}\n")
        return 1

    if args.json:
        sys.stdout.write(json.dumps(report.model_dump(mode="json"), indent=2) + "\n")
    else:
        sys.stdout.write(_render(report) + "\n")
    # 0 clean / 3 inconclusive / 1 hard-failed, via the same _exit_category _render used, so the
    # printed verdict and the exit code are guaranteed consistent.
    return _EXIT_CODE[_exit_category(report)]


if __name__ == "__main__":
    raise SystemExit(main())
