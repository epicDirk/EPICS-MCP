r"""Coverage audit: which delivered PV has no display / no archive / no alarm — and the reverse.

The second System-Owner thread (Wedge 3, part 2). A **cross-plane coverage matrix** joining the
Wedge-0 display-PV index (PV → [operator screens]) with the runtime planes opi-foundry can read:
the **ChannelFinder** registry (what the IOCs actually serve), the **Archiver Appliance**, and the
**Phoebus Alarm** config. It answers the owner's question: *which PV do my IOCs deliver (CF) that
nobody put on a screen / that isn't archived / that has no alarm — and which shown PV isn't even
registered?*

Pure + deterministic; every runtime signal is **injected** as a Protocol so the join is
offline-testable (mirrors :mod:`~.crossplane`). Stays **free of ``opi_navigation`` imports** — the
``PvIndexEntry`` → :class:`IndexRow` translation happens at the tool/CLI edge.

**The matrix (both universes, normalized to the bare record name):**
- Display set ``D`` = the operator-facing, resolved, real-protocol PVs from the Wedge-0 index.
- ChannelFinder set ``C`` = the channels registered under *scope* (the delivered PVs). CF is the
  **anchor**: if it cannot answer (disabled / capped / failed), no cf-relative verdict is possible
  and ``D`` is reported alone.
- ``cf_and_display = C ∩ D`` (healthy core) · ``cf_only = C \ D`` = **registered but on no screen =
  operator blind-spot** (the headline signal) · ``display_only = D \ C`` = shown but not registered.

**Per-PV verdict** over the audited universe ``A`` (``C | D`` when CF is live, else ``D``):
``{has_display, registered_cf, archived, alarmed} ∈ {yes, no, withheld}``. ``withheld`` is NEVER
``no`` — a plane that could not answer (disabled, capped, per-PV timeout, or an incomplete display
inventory) withholds rather than false-flag a gap. ``critical_uncovered`` = CF-registered (provably
delivered) AND ≥1 **proven** gap (``no``); a PV with a ``withheld`` gap is excluded (the gap is not
provable) and named once in a note.

**Honesty (lower bounds):** ``displays_incomplete`` (context-capped → a not-in-``D`` PV could sit on
a not-fully-expanded display → ``has_display=withheld``, never a false blind-spot) · ``cf_capped`` /
CF-disabled (no cf verdicts) · per-PV archiver/alarm query failure (that cell withheld) · the
display_only/D ratio caveat (an incomplete CF, not real defects).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal, NamedTuple, Protocol

from pydantic import BaseModel, ConfigDict

# Reuse the normalization helper + ratio threshold from the Wedge-1 cross-plane join (NOT
# duplicated): bare-record-name normalization (strip a trailing ``.FIELD``) and the
# ">= this fraction unregistered ⇒ likely-incomplete-CF" caveat constant. The CF Protocol +
# capped-query signal are reused too (a coverage CF checker IS a crossplane ChannelFinderChecker).
from epics_pv_mcp.services.crossplane import (
    _CF_RATIO_CAVEAT_THRESHOLD,
    CFRegistryCapped,
    ChannelFinderChecker,
    _record_name,
)

#: A coverage cell. ``withheld`` heisst NIE ``no`` — die Fläche konnte nicht antworten.
Coverage3 = Literal["yes", "no", "withheld"]


class IndexRow(NamedTuple):
    """One operator-facing, resolved, real-protocol PV from the Wedge-0 ``PV → [displays]`` index.

    The narrow seam the audit needs from the ``opi_navigation`` PV-inventory: the tool/CLI edge
    translates each :class:`opi_navigation.pv_analysis.models.PvIndexEntry` into one of these (the
    ``pv`` already normalized to its protocol-free channel name). Vorbild: ``crossplane.JoinPv``.
    """

    pv: str
    protocol: str
    displays: tuple[str, ...]
    roles: tuple[str, ...]


class ArchivedChecker(Protocol):
    """Minimal read-only Archiver contract (injected so the join is offline-testable).

    ``is_archived(pv)`` returns whether *pv* is being archived, OR raises ``RuntimeError`` on a
    query failure/timeout — never another type. That lets the core withhold the per-PV cell (never
    ``no``); the edge translates Archiver errors into ``RuntimeError`` before they reach here.
    """

    def is_archived(self, pv: str) -> bool: ...


class AlarmChecker(Protocol):
    """Minimal read-only Alarm-Logger contract (injected so the join is offline-testable).

    ``is_alarm_configured(pv)`` returns whether *pv* has an alarm configuration, OR raises
    ``RuntimeError`` on a query failure — never another type (the edge translates Alarm errors).
    """

    def is_alarm_configured(self, pv: str) -> bool: ...


class PvCoverageRow(BaseModel):
    """The coverage verdict for one PV of the audited universe."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pv: str
    has_display: Coverage3
    registered_cf: Coverage3
    archived: Coverage3
    alarmed: Coverage3
    displays: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()


class CoverageReport(BaseModel):
    """Deterministic cross-plane coverage report (JSON via ``model_dump_json``). Tuples sorted."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: str
    #: Which planes actually answered (always ``display``; ``channelfinder``/``archiver``/``alarm``
    #: only when wired). A plane absent here contributes only ``withheld`` cells.
    planes_live: tuple[str, ...] = ()
    rows: tuple[PvCoverageRow, ...] = ()
    #: C ∩ D — registered AND on a screen (the healthy core).
    cf_and_display: tuple[str, ...] = ()
    #: C \ D — registered but on NO screen (operator blind-spot). Lower bound when displays capped.
    cf_only: tuple[str, ...] = ()
    #: D \ C — shown but NOT registered in ChannelFinder.
    display_only: tuple[str, ...] = ()
    #: Headline: CF-registered AND ≥1 PROVEN gap (``no``; withheld gaps excluded).
    critical_uncovered: tuple[str, ...] = ()
    #: Triage splits — CF-registered PVs with a proven gap on each plane.
    blind_spots: tuple[str, ...] = ()
    unarchived: tuple[str, ...] = ()
    unalarmed: tuple[str, ...] = ()
    #: Count of channels ChannelFinder registered under *scope* (0 also when CF was withheld).
    cf_registered: int = 0
    #: True when the ChannelFinder query hit the result cap → all cf verdicts withheld.
    cf_capped: bool = False
    #: Operator displays whose per-instance PVs are incomplete (inventory context cap) — a
    #: not-in-D PV may sit on a capped one → ``has_display`` withheld, never a false ``no``.
    displays_incomplete: tuple[str, ...] = ()
    #: Honest caveats (lower bounds, withholds, skipped planes).
    notes: tuple[str, ...] = ()


def _plane_verdict(checker_call: object, pv: str) -> Coverage3:
    """Per-PV runtime verdict with H2 withheld-on-failure (never ``no`` when the query failed).

    *checker_call* is the bound method (``is_archived`` / ``is_alarm_configured``) or ``None`` when
    the plane is disabled → ``withheld`` for every PV. A ``RuntimeError`` (the edge's translation of
    a query failure/timeout) withholds THIS cell only — the rest of the plane keeps answering.
    """
    if checker_call is None:
        return "withheld"
    try:
        return "yes" if checker_call(pv) else "no"  # type: ignore[operator]
    except RuntimeError:
        return "withheld"


def audit_coverage(
    index_rows: Iterable[IndexRow],
    *,
    scope: str = "",
    channelfinder: ChannelFinderChecker | None = None,
    cf_requested: bool = False,
    archived: ArchivedChecker | None = None,
    archive_requested: bool = False,
    alarmed: AlarmChecker | None = None,
    alarm_requested: bool = False,
    context_capped: tuple[str, ...] = (),
    glob_capped_count: int = 0,
) -> CoverageReport:
    """Join the Wedge-0 display-PV index with the CF/Archiver/Alarm planes into a coverage matrix.

    *index_rows* are the operator-facing, resolved, real-protocol PVs (translated from the
    ``opi_navigation`` index at the edge). *scope* is a record-name prefix that narrows BOTH the CF
    query (``registered_under(scope)``) and the display set ``D`` (post-filter); ``""`` audits the
    whole site (the CF query then hits ``*`` and almost certainly the cap — sandbox/small-scope use
    only). *channelfinder*/*archived*/*alarmed* are the injected runtime checkers (``None`` = that
    plane disabled → withheld); *_requested* drive the honest "skipped — URL unset" notes. CF is the
    anchor: when disabled/capped/failed, no cf-relative cell or set-diff is computable and only
    ``D`` (with its display verdicts) is reported. *context_capped*/*glob_capped_count* carry the
    inventory's lower-bound signals.
    """
    # --- Display set D (normalized to bare record name on the D side; M8/L5: normalize C too). ---
    display_rows: dict[str, IndexRow] = {}
    for row in index_rows:
        rec = _record_name(row.pv)
        if scope and not rec.startswith(scope):
            continue  # scope post-filter on D
        if rec in display_rows:
            # Field-suffix normalization can collapse record + record.EGU into one record: merge.
            prev = display_rows[rec]
            display_rows[rec] = IndexRow(
                pv=rec,
                protocol=prev.protocol,
                displays=tuple(sorted(set(prev.displays) | set(row.displays))),
                roles=tuple(sorted(set(prev.roles) | set(row.roles))),
            )
        else:
            display_rows[rec] = IndexRow(
                pv=rec, protocol=row.protocol, displays=row.displays, roles=row.roles
            )
    display_set = set(display_rows)

    # --- ChannelFinder set C (the delivered-PV anchor). Withhold on cap/failure/disabled. ---
    cf_set: set[str] = set()
    cf_registered = 0
    cf_capped = False
    cf_withheld = False
    if channelfinder is not None:
        try:
            registered = channelfinder.registered_under(scope)
            cf_set = {_record_name(name) for name in registered}  # normalize C too (M8/L5)
            cf_registered = len(registered)
        except CFRegistryCapped:
            cf_withheld = cf_capped = True
        except RuntimeError:
            cf_withheld = True
    else:
        cf_withheld = True
    cf_live = channelfinder is not None and not cf_withheld

    # --- Audited universe A and the cf set-diffs (only when CF is live). ---
    universe = (cf_set | display_set) if cf_live else set(display_set)
    cf_and_display = sorted(cf_set & display_set) if cf_live else []
    cf_only = sorted(cf_set - display_set) if cf_live else []
    display_only = sorted(display_set - cf_set) if cf_live else []

    incomplete = bool(context_capped)

    rows: list[PvCoverageRow] = []
    blind_spots: list[str] = []
    unarchived: list[str] = []
    unalarmed: list[str] = []
    critical: list[str] = []
    archive_withheld: list[str] = []
    alarm_withheld: list[str] = []
    withheld_gap_excluded: list[str] = []

    for pv in sorted(universe):
        in_display = pv in display_set
        # Display plane is offline-complete UP TO the context cap: a not-in-D PV is a proven absence
        # only when no display was capped; otherwise it could sit on a capped display → withheld.
        has_display: Coverage3 = "yes" if in_display else ("withheld" if incomplete else "no")
        registered_cf: Coverage3 = "withheld" if cf_withheld else ("yes" if pv in cf_set else "no")
        archived_v = _plane_verdict(archived.is_archived if archived is not None else None, pv)
        alarmed_v = _plane_verdict(alarmed.is_alarm_configured if alarmed is not None else None, pv)
        if archived is not None and archived_v == "withheld":
            archive_withheld.append(pv)
        if alarmed is not None and alarmed_v == "withheld":
            alarm_withheld.append(pv)

        src = display_rows.get(pv)
        rows.append(
            PvCoverageRow(
                pv=pv,
                has_display=has_display,
                registered_cf=registered_cf,
                archived=archived_v,
                alarmed=alarmed_v,
                displays=src.displays if src else (),
                roles=src.roles if src else (),
            )
        )

        # Triage over the DELIVERED PVs only (registered_cf == yes). A withheld gap is NOT a gap.
        if registered_cf == "yes":
            gaps = [
                plane
                for plane, verdict in (
                    ("display", has_display),
                    ("archive", archived_v),
                    ("alarm", alarmed_v),
                )
                if verdict == "no"
            ]
            if has_display == "no":
                blind_spots.append(pv)
            if archived_v == "no":
                unarchived.append(pv)
            if alarmed_v == "no":
                unalarmed.append(pv)
            if gaps:
                critical.append(pv)
            elif "withheld" in (has_display, archived_v, alarmed_v):
                # Delivered PV with a withheld (unprovable) gap and no proven gap — excluded from
                # critical_uncovered (the gap cannot be proven), surfaced once in a note.
                withheld_gap_excluded.append(pv)

    planes_live = ["display"]
    if cf_live:
        planes_live.append("channelfinder")
    if archived is not None:
        planes_live.append("archiver")
    if alarmed is not None:
        planes_live.append("alarm")

    notes = _coverage_notes(
        scope=scope,
        cf_withheld=cf_withheld,
        cf_capped=cf_capped,
        channelfinder_present=channelfinder is not None,
        cf_requested=cf_requested,
        archived_present=archived is not None,
        archive_requested=archive_requested,
        alarmed_present=alarmed is not None,
        alarm_requested=alarm_requested,
        context_capped=context_capped,
        glob_capped_count=glob_capped_count,
        display_only=display_only,
        display_total=len(display_set),
        archive_withheld=archive_withheld,
        alarm_withheld=alarm_withheld,
        withheld_gap_excluded=withheld_gap_excluded,
    )

    return CoverageReport(
        scope=scope,
        planes_live=tuple(planes_live),
        rows=tuple(rows),
        cf_and_display=tuple(cf_and_display),
        cf_only=tuple(cf_only),
        display_only=tuple(display_only),
        critical_uncovered=tuple(sorted(critical)),
        blind_spots=tuple(sorted(blind_spots)),
        unarchived=tuple(sorted(unarchived)),
        unalarmed=tuple(sorted(unalarmed)),
        cf_registered=cf_registered,
        cf_capped=cf_capped,
        displays_incomplete=tuple(sorted(context_capped)),
        notes=tuple(notes),
    )


def _coverage_notes(
    *,
    scope: str,
    cf_withheld: bool,
    cf_capped: bool,
    channelfinder_present: bool,
    cf_requested: bool,
    archived_present: bool,
    archive_requested: bool,
    alarmed_present: bool,
    alarm_requested: bool,
    context_capped: tuple[str, ...],
    glob_capped_count: int,
    display_only: list[str],
    display_total: int,
    archive_withheld: list[str],
    alarm_withheld: list[str],
    withheld_gap_excluded: list[str],
) -> list[str]:
    """Build the honest caveat notes (no silent ``no``, every lower bound named)."""
    notes: list[str] = []
    if not scope:
        notes.append(
            "Unscoped audit (scope='') — the ChannelFinder query hits '*' and almost certainly the "
            "result cap on a real site, withholding the whole matrix. Pass a device/prefix scope "
            "for a usable site result (the default is for the sandbox / small scopes only)."
        )
    # ChannelFinder is the anchor — without it no cf verdict or set-diff is computable.
    if not channelfinder_present:
        if cf_requested:
            notes.append(
                "ChannelFinder check requested but EPICS_MCP_CHANNELFINDER_URL is unset — no "
                "delivered-PV anchor; cf_only/display_only/cf_and_display/critical_uncovered are "
                "not computable (only the raw display set D is reported)."
            )
        else:
            notes.append(
                "ChannelFinder disabled — no delivered-PV anchor; only the raw display set D is "
                "reported (no coverage verdict). Enable a ChannelFinder checker for the matrix."
            )
    elif cf_capped:
        notes.append(
            "ChannelFinder returned a capped (truncated) result — every cf verdict is withheld "
            "(diffing against a partial registry would false-flag). Narrow the scope or raise "
            "EPICS_MCP_CHANNELFINDER_MAX_RESULTS."
        )
    elif cf_withheld:
        notes.append(
            "ChannelFinder query failed — every cf verdict is withheld (a delivered PV cannot be "
            "established against an unavailable registry)."
        )
    if archive_requested and not archived_present:
        notes.append(
            "Archiver check requested but EPICS_MCP_ARCHIVER_URL is unset — 'archived' is withheld "
            "for every PV (no network call)."
        )
    if alarm_requested and not alarmed_present:
        notes.append(
            "Alarm check requested but EPICS_MCP_ALARM_URL is unset — 'alarmed' is withheld for "
            "every PV (no network call)."
        )
    if archive_withheld:
        notes.append(
            f"'archived' withheld for {len(archive_withheld)} PV(s) — the per-PV Archiver query "
            "failed/timed out for them (a partial-plane lower bound; never counted as a gap)."
        )
    if alarm_withheld:
        notes.append(
            f"'alarmed' withheld for {len(alarm_withheld)} PV(s) — the per-PV Alarm query "
            "failed/timed out for them (a partial-plane lower bound; never counted as a gap). "
            "NOTE: a clean miss on /search/alarm/config is a real negative only if the Logger "
            "was running at config-import time (the config index is a change-log)."
        )
    if context_capped:
        notes.append(
            f"{len(context_capped)} display(s) hit the context cap — a not-shown PV could "
            "sit on a not-fully-expanded display, so 'has_display=no'/blind_spots are WITHHELD for "
            "those (a lower bound; re-run with a higher context cap)."
        )
    if glob_capped_count:
        notes.append(
            f"{glob_capped_count} template <file> reference(s) hit the glob cap — some embedded "
            "screens were dropped; the display set D is a lower bound."
        )
    if withheld_gap_excluded:
        notes.append(
            f"{len(withheld_gap_excluded)} delivered PV(s) have a withheld gap and no proven gap "
            "— excluded from critical_uncovered (a withheld cell is never a gap)."
        )
    if (
        display_only
        and display_total
        and len(display_only) / display_total >= _CF_RATIO_CAVEAT_THRESHOLD
    ):
        notes.append(
            f"{len(display_only)}/{display_total} shown PV(s) are NOT registered in ChannelFinder "
            "(>= 50%) — this almost certainly means an INCOMPLETE ChannelFinder (e.g. a partial "
            "test registry or too-narrow scope), not real defects; treat display_only as a "
            "coverage signal, not a defect list."
        )
    return notes


def render_markdown(report: CoverageReport) -> str:
    """Render a :class:`CoverageReport` as deterministic Markdown."""
    lines = ["# Cross-Plane Coverage Audit", ""]
    lines.append(f"- **Scope:** `{report.scope or '* (whole site)'}`")
    lines.append(f"- **Planes live:** {', '.join(report.planes_live)}")
    lines.append(f"- **ChannelFinder registered (under scope):** {report.cf_registered}")
    lines.append("")
    lines.append(f"- **Registered AND on a screen (cf_and_display):** {len(report.cf_and_display)}")
    lines.append(f"- **Registered but on NO screen (cf_only / blind-spot):** {len(report.cf_only)}")
    lines.extend(f"  - {pv}" for pv in report.cf_only)
    lines.append(f"- **Shown but NOT registered (display_only):** {len(report.display_only)}")
    lines.extend(f"  - {pv}" for pv in report.display_only)
    lines.append("")
    lines.append(
        f"- **🔴 critical_uncovered (delivered + proven gap):** {len(report.critical_uncovered)}"
    )
    lines.extend(f"  - {pv}" for pv in report.critical_uncovered)
    lines.append(
        f"  - blind_spots: {len(report.blind_spots)}, unarchived: {len(report.unarchived)}, "
        f"unalarmed: {len(report.unalarmed)}"
    )
    if report.displays_incomplete:
        lines.append(
            f"- **Displays with incomplete inventory (lower bound):** "
            f"{len(report.displays_incomplete)}"
        )
    if report.notes:
        lines.append("")
        lines.append("## Notes")
        lines.extend(f"- {note}" for note in report.notes)
    return "\n".join(lines)
