"""Live connection diagnosis for a single PV — "why is this PV disconnected?" (read-only).

This is Wedge 4: it automates the manual ``diagnose_pv`` prompt into one deterministic verdict.
It answers *why* a PV hangs, not just *whether* it is connected.

Two architectural invariants make this module different from every other tool here:

1. **The live p4p connect is the ONLY truth for connected/disconnected.** ChannelFinder, Naming,
   Archiver and Alarm are *explanatory only*: they produce a ``likely_cause`` and evidence, but they
   NEVER flip the connection verdict and are NEVER the sole basis for a confident negative. A plane
   that is disabled or errors is ``withheld`` — never a positive cause (``withheld != no``). Order:
   **connect first, then explain.**

2. **Exception-catching is INVERTED here.** Everywhere else a failed ``pv_get`` propagates as an
   ``EpicsError`` that the tool wrapper turns into a ``ToolError``. Here a disconnected PV is the
   NORMAL input, so :func:`_probe_live` CATCHES the p4p exceptions and maps them to
   :class:`LiveEvidence`. Only a genuine *internal* error (a non-``EpicsError`` bug) yields
   ``state="unknown"``. The thin tool wrapper keeps the standard ``EpicsError -> ToolError`` shell
   for those rare internal errors only.

**Name-server timeout collapse** (why cause is never read off the error code): on a PVA name-server
(TCP, as in the local sandbox) a typo PV and a dead IOC BOTH surface as ``PV_TIMEOUT`` —
``PV_NOT_FOUND`` only exists under UDP broadcast search. So the decision tree keys cause on
ChannelFinder/Naming membership, never on the transport error code.

**``network_unreachable`` is deliberately absent** from :data:`LikelyCause`: distinguishing "the
network can't reach the IOC" from "the IOC is down" needs a transport probe (a TCP connect / ping)
that the MVP does not have — every plane here is query-based. Emitting it would be a dead branch, so
that case resolves to ``indeterminate`` with a config-oriented note. Reserved for a future phase.
"""

from __future__ import annotations

import asyncio
from typing import Literal

from pydantic import BaseModel, ConfigDict

from epics_pv_mcp.config import get_config
from epics_pv_mcp.errors import EpicsError
from epics_pv_mcp.services.epics_client import pv_get
from epics_pv_mcp.services.naming_client import NamingServiceClient
from epics_pv_mcp.tools.alarm import _is_alarm_configured
from epics_pv_mcp.tools.archiver import _is_archived
from epics_pv_mcp.tools.channelfinder import _find_channels

# --- Enums (Literal so mypy checks exhaustiveness in the ``match`` below) ---
State = Literal["connected", "disconnected", "unknown"]
LikelyCause = Literal["healthy", "ioc_down", "name_typo", "unregistered", "indeterminate"]
Confidence = Literal["confirmed", "likely", "indeterminate"]

#: pvStatus values (case-insensitive) that mean the record's IOC is up per ChannelFinder/RecSync.
_HEALTHY_PV_STATUS = frozenset({"online", "active"})


class _Model(BaseModel):
    """Frozen, closed value object (deterministic; unknown fields rejected)."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class LiveEvidence(_Model):
    """The authoritative plane: the result of the single live p4p read. Always consulted."""

    consulted: bool = True
    #: The ONLY connected/disconnected truth. False on any disconnect
    #: (timeout / not-found / connection error).
    connected: bool
    value: object | None = None
    #: Alarm severity text (e.g. ``NO_ALARM``/``MINOR``/``MAJOR``) when the live read carried it.
    severity: str | None = None
    #: Machine error code on a disconnect (``PV_TIMEOUT`` / ``PV_NOT_FOUND`` /
    #: ``EPICS_CONNECTION_FAILED``); ``None`` when connected or on an internal failure.
    error_code: str | None = None
    error: str | None = None


class ChannelFinderEvidence(_Model):
    """Explanatory: is the PV in the runtime PV directory, and its last-known status/provenance."""

    #: True iff CF was enabled AND the query returned (not disabled, not errored).
    consulted: bool
    #: ``None`` when withheld; else True iff an exact-name channel exists.
    registered: bool | None = None
    #: Last-known ``pvStatus`` from RecSync (race-prone cadence → informs cause, never sets state).
    pv_status: str | None = None
    ioc_name: str | None = None
    host_name: str | None = None
    capped: bool = False
    withheld: bool = False
    note: str | None = None


class NamingEvidence(_Model):
    """Explanatory: is the device name registered ACTIVE in the ESS Naming Service."""

    consulted: bool
    registered: bool | None = None
    status: str | None = None
    withheld: bool = False
    note: str | None = None


class ArchiverEvidence(_Model):
    """Corroboration only: recently archived ⇒ recently connected (confidence, not cause)."""

    consulted: bool
    archived: bool | None = None
    withheld: bool = False
    note: str | None = None


class AlarmEvidence(_Model):
    """Corroboration only: known to the alarm tree ⇒ a real, monitored PV (not a typo)."""

    consulted: bool
    configured: bool | None = None
    withheld: bool = False
    note: str | None = None


class DiagnoseEvidence(_Model):
    """Per-plane RAW evidence. ``live`` is always present; the rest may be withheld."""

    live: LiveEvidence
    channelfinder: ChannelFinderEvidence
    naming: NamingEvidence
    archiver: ArchiverEvidence
    alarm: AlarmEvidence


class CauseResult(_Model):
    """The PURE verdict from :func:`derive_cause`: cause + confidence + operator guidance."""

    likely_cause: LikelyCause
    confidence: Confidence
    next_steps: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


class DiagnoseReport(_Model):
    """Full diagnosis: state, likely cause, confidence, per-plane evidence, next steps."""

    pv_name: str
    state: State
    likely_cause: LikelyCause
    confidence: Confidence
    evidence: DiagnoseEvidence
    next_steps: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    #: Planes that were REQUESTED but could not contribute (disabled URL / query error) — never a
    #: false negative. Distinct from a plane that was simply not requested.
    withheld: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# PURE decision tree (the whole Unit-test target — no I/O, deterministic)
# ---------------------------------------------------------------------------


def derive_cause(state: State, ev: DiagnoseEvidence) -> CauseResult:
    """Map (live state + explanatory evidence) to a likely cause. PURE — same input, same output.

    Never reads the transport error code to decide typo vs. ioc-down (name-server timeout collapse);
    keys cause on ChannelFinder/Naming membership only. Corroboration (Archiver recent sample, Alarm
    config hit) lifts *confidence* but never creates a cause. ``network_unreachable`` is not emitted
    (no transport probe in the MVP) — that case is ``indeterminate`` with a config note.
    """
    match state:
        case "connected":
            notes: list[str] = []
            if ev.live.severity and ev.live.severity.upper() != "NO_ALARM":
                notes.append(
                    f"PV is connected but in alarm ({ev.live.severity}) — that is a data/alarm "
                    "issue, not a connection problem."
                )
            cf = ev.channelfinder
            if cf.consulted and cf.pv_status and cf.pv_status.lower() not in _HEALTHY_PV_STATUS:
                notes.append(
                    f"ChannelFinder last-known pvStatus={cf.pv_status!r} is stale — the PV is live."
                )
            return CauseResult(
                likely_cause="healthy",
                confidence="confirmed",
                next_steps=(
                    "PV answers on PVA — the connection is healthy. (This confirms one responder, "
                    "not uniqueness: multi-responder/collision detection is out of scope.)",
                ),
                notes=tuple(notes),
            )
        case "unknown":
            return CauseResult(
                likely_cause="indeterminate",
                confidence="indeterminate",
                next_steps=(
                    "Retry — the live probe failed for an internal reason (see notes), not a "
                    "diagnosable disconnect.",
                ),
            )
        case "disconnected":
            return _derive_disconnected(ev)


def _derive_disconnected(ev: DiagnoseEvidence) -> CauseResult:
    """Cause for a disconnected PV — CF membership first, then Naming, else indeterminate."""
    cf = ev.channelfinder
    source = _source_suffix(cf)

    if cf.consulted and cf.registered:
        pvst = (cf.pv_status or "").strip()
        if pvst and pvst.lower() not in _HEALTHY_PV_STATUS:
            # CF knows the PV and its last-known status is not up → the IOC looks down.
            confirmed = ev.archiver.consulted and ev.archiver.archived is True
            notes: tuple[str, ...] = (
                f"ChannelFinder last-known pvStatus={cf.pv_status!r} (may be stale).",
            )
            if confirmed:
                notes += ("Archiver has recent samples — corroborates a formerly-live PV.",)
            return CauseResult(
                likely_cause="ioc_down",
                confidence="confirmed" if confirmed else "likely",
                next_steps=(
                    f"Check whether the IOC{source} is running and reachable.",
                    "Restart/inspect the IOC, then re-read the PV.",
                ),
                notes=notes,
            )
        if ev.live.error_code == "PV_NOT_FOUND":
            # Only reachable under UDP broadcast search (never on a name-server) — CF knows it, the
            # network could not find it ⇒ IOC down.
            return CauseResult(
                likely_cause="ioc_down",
                confidence="likely",
                next_steps=(f"Check whether the IOC{source} is running.",),
                notes=("ChannelFinder registers the PV but the network search found no server.",),
            )
        # CF-hit + (pvStatus up or absent) + timeout/connection error → not decidable without a
        # transport probe. Under localhost isolation this most often means the client addr-list was
        # not widened, so keep it honest as a config note rather than claim network_unreachable.
        return CauseResult(
            likely_cause="indeterminate",
            confidence="indeterminate",
            next_steps=(
                "Confirm the IOC is up AND that this client's EPICS address list can reach it "
                "(EPICS_PVA_ADDR_LIST / name-server). Timeout alone cannot separate the two.",
            ),
            notes=("ChannelFinder knows the PV but the live probe timed out.",),
        )

    if cf.consulted and cf.registered is False and not cf.capped:
        # CF-miss (and the query was NOT truncated) — lean on Naming to split typo
        # vs. unregistered.
        nm = ev.naming
        if nm.consulted and nm.registered:
            notes = ("Naming Service: the device name is registered ACTIVE, but no PV is served.",)
            if ev.alarm.consulted and ev.alarm.configured:
                notes += ("Alarm tree knows the PV — it is a real, expected channel.",)
            return CauseResult(
                likely_cause="unregistered",
                confidence="likely",
                next_steps=(
                    "The device exists but the PV is not served — check the IOC/db that should "
                    "provide it (record name, IOC deployment).",
                ),
                notes=notes,
            )
        if nm.consulted and nm.registered is False:
            return CauseResult(
                likely_cause="name_typo",
                confidence="likely",
                next_steps=(
                    "Candidate typo: the device name is not registered in the Naming Service — "
                    "double-check the PV spelling / device name.",
                ),
                notes=(
                    "name_typo is a CANDIDATE: on a PVA name-server 'not found' collapses to a "
                    "timeout, so a typo cannot be confirmed by transport alone.",
                ),
            )
        # Naming withheld / not requested → the disambiguator is missing; do NOT guess.
        return CauseResult(
            likely_cause="indeterminate",
            confidence="indeterminate",
            next_steps=(
                "Not in ChannelFinder. Enable Naming (check_naming=true + EPICS_MCP_NAMING_URL) to "
                "tell a typo apart from an unregistered device.",
            ),
            notes=("PV is not in ChannelFinder and the Naming Service was not consulted.",),
        )

    # CF withheld / capped / not consulted → no reliable directory signal.
    return CauseResult(
        likely_cause="indeterminate",
        confidence="indeterminate",
        next_steps=(
            "Enable ChannelFinder (check_channelfinder=true + EPICS_MCP_CHANNELFINDER_URL) to "
            "explain the disconnect (registered? which IOC?).",
        ),
        notes=("ChannelFinder was withheld/capped — cannot classify the disconnect.",),
    )


def _source_suffix(cf: ChannelFinderEvidence) -> str:
    """`` serving it (<ioc> on <host>)`` — None-safe; empty string when CF has no provenance."""
    if cf.ioc_name and cf.host_name:
        return f" serving it ({cf.ioc_name} on {cf.host_name})"
    if cf.ioc_name:
        return f" serving it ({cf.ioc_name})"
    return ""


# ---------------------------------------------------------------------------
# Async I/O shell — each plane-gatherer is TOTAL (catches its own errors → evidence, never raises)
# ---------------------------------------------------------------------------


async def _probe_live(pv_name: str, timeout: float) -> LiveEvidence:
    """The authoritative probe. A disconnect is normal input → caught and mapped, never raised.

    An ``EpicsError`` (timeout / not-found / connection) means *disconnected* with a code. Any other
    exception is a genuine internal bug → ``error_code=None`` so the shell reports
    ``state="unknown"``.
    """
    try:
        result = await pv_get(pv_name, timeout=timeout)
    except EpicsError as exc:
        return LiveEvidence(connected=False, error_code=exc.error_code, error=str(exc))
    except Exception as exc:  # noqa: BLE001 — internal probe failure surfaces as state="unknown"
        return LiveEvidence(connected=False, error_code=None, error=f"{type(exc).__name__}: {exc}")
    alarm = result.get("alarm")
    severity = None
    if isinstance(alarm, dict):
        sev = alarm.get("severity_text")
        severity = str(sev) if sev is not None else None
    return LiveEvidence(connected=True, value=result.get("value"), severity=severity)


def _state_from_live(live: LiveEvidence) -> State:
    """connected iff read returned; disconnected on a coded error; else unknown (internal bug)."""
    if live.connected:
        return "connected"
    return "disconnected" if live.error_code is not None else "unknown"


async def _gather_channelfinder(
    pv_name: str, requested: bool, timeout: float
) -> ChannelFinderEvidence:
    """Exact-name ChannelFinder lookup. Disabled/errored → withheld (never a false negative)."""
    if not requested:
        return ChannelFinderEvidence(consulted=False, note="ChannelFinder not requested.")
    try:
        result = await _find_channels(pv_name, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 — TOTAL: any failure withholds, never crashes diagnose()
        return ChannelFinderEvidence(
            consulted=False, withheld=True, note=f"ChannelFinder error: {exc}"
        )
    if not result.get("enabled"):
        note = result.get("note")
        return ChannelFinderEvidence(
            consulted=False, withheld=True, note=str(note) if note else "ChannelFinder disabled."
        )
    channels = result.get("channels")
    rows = channels if isinstance(channels, list) else []
    exact = next((c for c in rows if isinstance(c, dict) and c.get("name") == pv_name), None)
    props = exact.get("properties") if isinstance(exact, dict) else None
    pv_status = props.get("pvStatus") if isinstance(props, dict) else None
    return ChannelFinderEvidence(
        consulted=True,
        registered=exact is not None,
        pv_status=str(pv_status) if pv_status is not None else None,
        ioc_name=_opt_str(exact, "ioc_name"),
        host_name=_opt_str(exact, "host_name"),
        capped=bool(result.get("capped")),
    )


async def _gather_naming(pv_name: str, requested: bool, timeout: float) -> NamingEvidence:
    """ESS Naming lookup — GATED HERE: only when requested AND ``naming_url`` is configured.

    The shared :class:`NamingServiceClient` and its two other callers (crossplane tool + CLI) are
    left UNTOUCHED; this is the empty-URL-disables-client discipline used for CF/Archiver/Alarm. A
    FRESH instance per call (the client caches per-instance) wrapped in a thread (sync requests).
    """
    if not requested:
        return NamingEvidence(
            consulted=False, note="Naming not requested (default off — no ESS egress)."
        )
    cfg = get_config()
    if not cfg.naming_url:
        return NamingEvidence(
            consulted=False,
            withheld=True,
            note="Naming withheld: set EPICS_MCP_NAMING_URL to enable (default off = no egress).",
        )
    device_name = _device_name(pv_name)

    def _run() -> NamingEvidence:
        client = NamingServiceClient(base_url=cfg.naming_url, timeout=timeout)
        status = client.validate_name(device_name)
        return NamingEvidence(
            consulted=True, registered=status["registered"], status=status["status"] or None
        )

    try:
        return await asyncio.to_thread(_run)
    except Exception as exc:  # noqa: BLE001 — any Naming failure withholds, never a false verdict
        return NamingEvidence(consulted=False, withheld=True, note=f"Naming error: {exc}")


async def _gather_archiver(pv_name: str, requested: bool, timeout: float) -> ArchiverEvidence:
    """Corroboration: is the PV archived? Disabled/errored → withheld."""
    if not requested:
        return ArchiverEvidence(consulted=False, note="Archiver not requested.")
    try:
        result = await _is_archived(pv_name, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 — TOTAL: any failure withholds, never crashes diagnose()
        return ArchiverEvidence(consulted=False, withheld=True, note=f"Archiver error: {exc}")
    if not result.get("enabled"):
        return ArchiverEvidence(consulted=False, withheld=True, note="Archiver disabled.")
    archived = result.get("archived")
    return ArchiverEvidence(
        consulted=True, archived=bool(archived) if archived is not None else None
    )


async def _gather_alarm(pv_name: str, requested: bool, timeout: float) -> AlarmEvidence:
    """Corroboration: is the PV in the alarm tree? Disabled/errored → withheld."""
    if not requested:
        return AlarmEvidence(consulted=False, note="Alarm not requested.")
    try:
        result = await _is_alarm_configured(pv_name, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 — TOTAL: any failure withholds, never crashes diagnose()
        return AlarmEvidence(consulted=False, withheld=True, note=f"Alarm error: {exc}")
    if not result.get("enabled"):
        return AlarmEvidence(consulted=False, withheld=True, note="Alarm logger disabled.")
    configured = result.get("configured")
    return AlarmEvidence(
        consulted=True, configured=bool(configured) if configured is not None else None
    )


def _collect_withheld(ev: DiagnoseEvidence) -> tuple[str, ...]:
    """Planes REQUESTED but unable to answer (disabled URL / error). Never a false negative."""
    withheld: list[str] = []
    for name, plane in (
        ("channelfinder", ev.channelfinder),
        ("naming", ev.naming),
        ("archiver", ev.archiver),
        ("alarm", ev.alarm),
    ):
        if plane.withheld:
            withheld.append(name)
    return tuple(withheld)


async def diagnose(
    pv_name: str,
    *,
    timeout: float | None = None,
    check_channelfinder: bool = True,
    check_naming: bool = False,
    check_archiver: bool = False,
    check_alarm: bool = False,
) -> DiagnoseReport:
    """Diagnose why *pv_name* is (dis)connected — read-only, a disconnect is normal input.

    The live p4p probe decides ``state``; the explanatory planes run CONCURRENTLY (each total) and
    only inform ``likely_cause``/``confidence``. Naming is gated here (off by default + naming_url).
    """
    cfg = get_config()
    probe_timeout = timeout if timeout is not None else cfg.diagnose_timeout

    live = await _probe_live(pv_name, probe_timeout)
    state = _state_from_live(live)

    channelfinder, naming, archiver, alarm = await asyncio.gather(
        _gather_channelfinder(pv_name, check_channelfinder, probe_timeout),
        _gather_naming(pv_name, check_naming, probe_timeout),
        _gather_archiver(pv_name, check_archiver, probe_timeout),
        _gather_alarm(pv_name, check_alarm, probe_timeout),
    )
    evidence = DiagnoseEvidence(
        live=live, channelfinder=channelfinder, naming=naming, archiver=archiver, alarm=alarm
    )
    cause = derive_cause(state, evidence)

    notes = cause.notes
    if state == "unknown" and live.error:
        notes = (*notes, f"Internal probe error: {live.error}")

    return DiagnoseReport(
        pv_name=pv_name,
        state=state,
        likely_cause=cause.likely_cause,
        confidence=cause.confidence,
        evidence=evidence,
        next_steps=cause.next_steps,
        notes=notes,
        withheld=_collect_withheld(evidence),
    )


def _device_name(pv_name: str) -> str:
    """Strip the trailing PV property to get the device name (…:EVR-01:12VValue → …:EVR-01)."""
    return pv_name.rsplit(":", 1)[0] if ":" in pv_name else pv_name


def _opt_str(row: object, key: str) -> str | None:
    """Read an optional string field from a raw channel dict (None-safe)."""
    if isinstance(row, dict):
        val = row.get(key)
        if val is not None:
            return str(val)
    return None
