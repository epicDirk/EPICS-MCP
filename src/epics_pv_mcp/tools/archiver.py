"""Tool functions for the EPICS Archiver Appliance (read-only).

``_is_archived`` is a thin MCP adapter over the services-layer query
(:func:`epics_pv_mcp.services.checkers.query_archived`) so :mod:`~epics_pv_mcp.services.diagnose`
reuses it without an upward ``services → tools`` import (M9). ``_get_pv_history`` stays here
(tool-only, not shared with diagnose). Both are default-disabled: with ``EPICS_MCP_ARCHIVER_URL``
unset they return a structured ``enabled: false`` result and make **no** network call (preserves
localhost isolation).
"""

from __future__ import annotations

import asyncio
from typing import Literal, TypedDict

from epics_pv_mcp.config import get_config
from epics_pv_mcp.errors import EpicsConnectionError, EpicsError
from epics_pv_mcp.services._time_window import TimeWindowFormatError
from epics_pv_mcp.services.archiver_client import (
    DEFAULT_MAX_POINTS,
    DEFAULT_MAX_PV_NAMES,
    ArchiverClient,
)
from epics_pv_mcp.services.archiver_exceptions import ArchiverConnectionError, ArchiverError
from epics_pv_mcp.services.checkers import (
    ArchiveStatusResult,
    _archiver_error_code,
    query_archived,
)

#: Used by _get_pv_history below; the sibling _is_archived note lives with query_archived in
#: services/checkers.py (same string, self-contained per layer — no services ↔ tools coupling).
_DISABLED_NOTE = (
    "Archiver Appliance is disabled. Set EPICS_MCP_ARCHIVER_URL to the appliance root "
    "(e.g. http://archiver:17665)."
)


# get_pv_history's tool result shape (S29 — typed MCP outputSchema; the pattern the Olog cluster set
# in services/checkers_olog.py). ``total=False`` because the disabled path (below) carries only a
# subset. Every field ABSENT on some return path is typed ``X | None``; the measured rationale lives
# in ``services/checkers_olog.py``'s "Tool result shapes" header (short version: an absent key is
# DROPPED from the wire, so nullability there is schema honesty enforced by the conformance test's
# static half; an EXPLICIT None is emitted and the wire path validates it for real).
# Only ``enabled``/``pv``/``samples``/``total`` are on
# EVERY path (disabled + enabled) and stay non-nullable; the disabled path omits
# ``from``/``to``/``capped``/``meta``/``status``/``withheld_reason``, and a success path with no
# note omits ``note`` — so all seven are nullable. ``samples`` and ``meta`` keep opaque ``dict``
# shapes (the sample projection / getData.json meta block). Functional syntax is REQUIRED: ``from``
# is a Python keyword and cannot be a class-syntax field name. mypy --strict checks every
# ``return {...}`` literal below against this shape.
ArchiverHistoryResult = TypedDict(
    "ArchiverHistoryResult",
    {
        "enabled": bool,
        "pv": str,
        "samples": list[dict[str, object]],
        "total": int,
        "from": str | None,
        "to": str | None,
        "capped": bool | None,
        "meta": dict[str, object] | None,
        "status": Literal["ok", "empty", "withheld"] | None,
        "withheld_reason": str | None,
        "note": str | None,
    },
    total=False,
)


async def _is_archived(pv: str, timeout: float = 5.0) -> ArchiveStatusResult:
    """Report whether *pv* is being archived (Archiver MGMT getPVStatus).

    Thin MCP adapter over :func:`epics_pv_mcp.services.checkers.query_archived`.
    """
    return await query_archived(pv, timeout=timeout)


async def _get_pv_history(
    pv: str,
    start: str,
    end: str,
    max_points: int = DEFAULT_MAX_POINTS,
    timeout: float = 5.0,
) -> ArchiverHistoryResult:
    """Fetch archived samples for *pv* between *start* and *end* (ISO-8601), capped."""
    cfg = get_config()
    if not cfg.archiver_url:
        return {"enabled": False, "pv": pv, "samples": [], "total": 0, "note": _DISABLED_NOTE}

    def _run() -> ArchiverHistoryResult:
        # get_pv_history hits /retrieval/data — in a split deployment that lives on a separate
        # Tomcat (:17668) from mgmt (:17665). Pass the retrieval URL; it falls back to
        # archiver_url inside ArchiverClient when the retrieval URL env is unset (single-JVM).
        client = ArchiverClient(
            cfg.archiver_url,
            timeout=timeout,
            auth_header=cfg.archiver_auth or None,
            retrieval_url=cfg.archiver_retrieval_url or None,
        )
        history = client.get_pv_history(pv, start, end, max_points=max_points)
        result: ArchiverHistoryResult = {
            "enabled": True,
            "pv": pv,
            "from": start,
            "to": end,
            "samples": [dict(sample) for sample in history["samples"]],
            "total": len(history["samples"]),
            "capped": history["capped"],
            "meta": history["meta"],  # DS-4A: getData.json meta block (EGU units, PREC precision)
            # DS-4B: status disambiguates an empty result — "empty" (real empty window) vs.
            # "withheld" (unreadable response), so a bare [] is never mistaken for "no data".
            "status": history["status"],
        }
        if history["note"]:
            result["note"] = history["note"]
        if history["withheld_reason"] is not None:
            result["withheld_reason"] = history["withheld_reason"]
        return result

    # Three outcomes, three classes — see _archiver_error_code. Collapsing them into
    # EpicsConnectionError sent readers after a network problem that was not happening: the
    # Archiver 500s on a time it cannot read, and answering IS not being unreachable.
    try:
        return await asyncio.to_thread(_run)
    except TimeWindowFormatError as exc:
        raise EpicsError(f"Archiver: {exc}", error_code="INVALID_TIME_WINDOW") from exc
    except ArchiverConnectionError as exc:
        raise EpicsConnectionError(f"Archiver: {exc}") from exc
    except ArchiverError as exc:
        raise EpicsError(f"Archiver: {exc}", error_code=_archiver_error_code(exc)) from exc


# list_archived_pvs' tool result shape (S29 — typed MCP outputSchema). ``total=False``: the disabled
# path omits ``capped`` and the enabled path omits ``note``. ``enabled``/``pvs``/``total`` are on
# EVERY path → non-nullable; the sometimes-absent ``capped``/``note`` are ``X | None`` (the S29
# convention that a sometimes-absent field permits null; its conformance test enforces it).
# Measured under standalone FastMCP: an ABSENT total=False key is DROPPED from the wire, not emitted
# as null, so nullability here is schema-honesty — the runtime null trap only bites a field set
# EXPLICITLY to None (none here; cf. get_archive_info.found). ``pvs`` is a list of PV NAMES, never
# free values. class-syntax: all field names are valid identifiers.
class ArchivedPvsResult(TypedDict, total=False):
    enabled: bool
    pvs: list[str]
    total: int
    capped: bool | None
    note: str | None


async def _list_archived_pvs(
    pattern: str | None = None,
    this_appliance: bool = False,
    limit: int = DEFAULT_MAX_PV_NAMES,
    timeout: float = 5.0,
) -> ArchivedPvsResult:
    """List the PV names the Archiver Appliance archives (MGMT getAllPVs / getPVsForThisAppliance).

    Default-disabled — with ``EPICS_MCP_ARCHIVER_URL`` unset returns ``enabled: false`` + an empty
    list and makes NO network call. Uses getAllPVs (whole appliance) or getPVsForThisAppliance (this
    member, with ``this_appliance=True``) — NOT getMatchingPVs (404 on split/proxied).

    ``pattern`` (an optional name glob, e.g. ``DEV-TEST01:*``) maps to getAllPVs' ``pv`` param and
    works ONLY there. getPVsForThisAppliance has NO name filter at all, so ``pattern`` with
    ``this_appliance=True`` is REFUSED rather than ignored — see the guard below. ``capped`` is
    honest. PV names carry no person data, so no redaction is needed.
    """
    # Refuse BEFORE the config gate: a bad argument is bad regardless of deployment, so a caller
    # testing against no archiver still learns the call is wrong (and it makes no network call
    # either way). Mirrors query_olog_create's "gate FIRST, before any client construction or I/O".
    if pattern and this_appliance:
        # Measured against a live appliance: getPVsForThisAppliance ignores pv, regex, pattern AND
        # name — every reply is byte-identical to the unfiltered one. Forwarding the glob would look
        # like filtering and silently return a full, plausible list of the WRONG PVs (with
        # capped=true, which reads as a legitimate truncation). A loud refusal beats that.
        # `pattern and` (truthy, not `is not None`) mirrors the client's own `if pattern:` so an
        # empty string stays "no pattern" on both paths.
        raise EpicsError(
            "list_archived_pvs: pattern is not supported together with this_appliance=True. The "
            "MGMT getPVsForThisAppliance endpoint has no name filter — it IGNORES a pv param "
            "(measured: the reply is byte-identical with and without it), so it cannot answer a "
            "name-filtered question for one cluster member. Drop this_appliance to filter by name "
            "across the appliance (getAllPVs does honour pv), or drop pattern to enumerate this "
            "member. Client-side filtering is deliberately not offered: limit is applied "
            "server-side, so filtering a capped page would return an arbitrary subset behind a "
            "meaningless capped flag.",
            error_code="INVALID_ARGUMENT",
        )

    cfg = get_config()
    if not cfg.archiver_url:
        return {"enabled": False, "pvs": [], "total": 0, "note": _DISABLED_NOTE}

    def _run() -> ArchivedPvsResult:
        client = ArchiverClient(
            cfg.archiver_url, timeout=timeout, auth_header=cfg.archiver_auth or None
        )
        if this_appliance:
            names, capped = client.get_pvs_for_this_appliance(limit=limit)
        else:
            names, capped = client.get_all_pvs(pattern=pattern, limit=limit)
        return {"enabled": True, "pvs": names, "total": len(names), "capped": capped}

    try:
        return await asyncio.to_thread(_run)
    except ArchiverConnectionError as exc:
        raise EpicsConnectionError(f"Archiver: {exc}") from exc
    except ArchiverError as exc:
        # The server ANSWERED (a served 4xx/5xx) — that is not an outage.
        raise EpicsError(f"Archiver: {exc}", error_code=_archiver_error_code(exc)) from exc


# get_archive_info's tool result shape (S29 — typed MCP outputSchema). ``total=False``: only
# ``enabled``/``pv`` are on EVERY path (disabled, enabled-found, enabled-not-found) → the only two
# non-nullable fields. ``found`` is on every path but a TRI-STATE: True (record), False (HTTP 404,
# definitively not archived), None (disabled, plane NOT checked) → ``bool | None``. The disabled
# path sets it EXPLICITLY to None (emitted as null), so a non-nullable ``found: bool`` would make
# the advertised schema misrepresent the emitted content; the conformance test is the guard.
# ``note`` is disabled-only. The 26 getPVTypeInfo projection fields are enabled+found-only and
# COPIED UNCONVERTED (client dict[str, object]; wire types are mixed — numeric limits as strings,
# ``paused`` a bool, ``archive_fields``/``data_stores`` lists) → each ``object | None`` is the
# faithful type. All names are valid identifiers → class-syntax.
class ArchiveInfoResult(TypedDict, total=False):
    enabled: bool
    pv: str
    found: bool | None
    note: str | None
    dbr_type: object | None
    sampling_method: object | None
    sampling_period: object | None
    event_rate: object | None
    storage_rate: object | None
    bytes_per_event: object | None
    element_count: object | None
    archive_fields: object | None
    data_stores: object | None
    host_name: object | None
    creation_time: object | None
    appliance: object | None
    paused: object | None
    upper_alarm_limit: object | None
    upper_warning_limit: object | None
    lower_warning_limit: object | None
    lower_alarm_limit: object | None
    upper_display_limit: object | None
    lower_display_limit: object | None
    upper_ctrl_limit: object | None
    lower_ctrl_limit: object | None
    precision: object | None
    units: object | None
    controlling_pv: object | None
    policy_name: object | None
    modification_time: object | None


async def _get_archive_info(pv: str, timeout: float = 5.0) -> ArchiveInfoResult:
    """Report the archive configuration of *pv* (Archiver MGMT ``getPVTypeInfo``).

    Tool-only (not shared with diagnose), like :func:`_get_pv_history`: builds the client
    directly against the MGMT root. Default-disabled — with ``EPICS_MCP_ARCHIVER_URL`` unset it
    returns ``enabled: false`` + ``found: None`` and makes NO network call. ``found`` is ``None``
    when the plane was NOT checked (disabled), ``False`` when a reachable appliance has no
    type-info record for *pv* (unknown / never-archived) and ``True`` otherwise — a disabled plane
    must never masquerade as a definitive "not archived", mirroring the sibling ``archived: None``
    / ``configured: None`` / ``registered: None`` gates. The MGMT record carries no person data
    (the client projects it onto its ``_TYPE_INFO_FIELDS`` allowlist).
    """
    cfg = get_config()
    if not cfg.archiver_url:
        return {"enabled": False, "pv": pv, "found": None, "note": _DISABLED_NOTE}

    def _run() -> ArchiveInfoResult:
        client = ArchiverClient(
            cfg.archiver_url, timeout=timeout, auth_header=cfg.archiver_auth or None
        )
        # Explicit literal-key + presence-guard build: mypy --strict rejects ``**dict[str, object]``
        # into a TypedDict. ``found`` is always present (True, or False on a 404); the 26 type-info
        # fields appear only on the found path (getPVTypeInfo omits nulls) — each guarded. The
        # ``bool()`` on ``found`` is an identity (client returns a real bool) that narrows object.
        info = client.get_pv_type_info(pv)
        result: ArchiveInfoResult = {"enabled": True, "pv": pv}
        if "found" in info:
            result["found"] = bool(info["found"])
        if "dbr_type" in info:
            result["dbr_type"] = info["dbr_type"]
        if "sampling_method" in info:
            result["sampling_method"] = info["sampling_method"]
        if "sampling_period" in info:
            result["sampling_period"] = info["sampling_period"]
        if "event_rate" in info:
            result["event_rate"] = info["event_rate"]
        if "storage_rate" in info:
            result["storage_rate"] = info["storage_rate"]
        if "bytes_per_event" in info:
            result["bytes_per_event"] = info["bytes_per_event"]
        if "element_count" in info:
            result["element_count"] = info["element_count"]
        if "archive_fields" in info:
            result["archive_fields"] = info["archive_fields"]
        if "data_stores" in info:
            result["data_stores"] = info["data_stores"]
        if "host_name" in info:
            result["host_name"] = info["host_name"]
        if "creation_time" in info:
            result["creation_time"] = info["creation_time"]
        if "appliance" in info:
            result["appliance"] = info["appliance"]
        if "paused" in info:
            result["paused"] = info["paused"]
        if "upper_alarm_limit" in info:
            result["upper_alarm_limit"] = info["upper_alarm_limit"]
        if "upper_warning_limit" in info:
            result["upper_warning_limit"] = info["upper_warning_limit"]
        if "lower_warning_limit" in info:
            result["lower_warning_limit"] = info["lower_warning_limit"]
        if "lower_alarm_limit" in info:
            result["lower_alarm_limit"] = info["lower_alarm_limit"]
        if "upper_display_limit" in info:
            result["upper_display_limit"] = info["upper_display_limit"]
        if "lower_display_limit" in info:
            result["lower_display_limit"] = info["lower_display_limit"]
        if "upper_ctrl_limit" in info:
            result["upper_ctrl_limit"] = info["upper_ctrl_limit"]
        if "lower_ctrl_limit" in info:
            result["lower_ctrl_limit"] = info["lower_ctrl_limit"]
        if "precision" in info:
            result["precision"] = info["precision"]
        if "units" in info:
            result["units"] = info["units"]
        if "controlling_pv" in info:
            result["controlling_pv"] = info["controlling_pv"]
        if "policy_name" in info:
            result["policy_name"] = info["policy_name"]
        if "modification_time" in info:
            result["modification_time"] = info["modification_time"]
        return result

    try:
        return await asyncio.to_thread(_run)
    except ArchiverConnectionError as exc:
        raise EpicsConnectionError(f"Archiver: {exc}") from exc
    except ArchiverError as exc:
        # The server ANSWERED (a served 4xx/5xx) — that is not an outage.
        raise EpicsError(f"Archiver: {exc}", error_code=_archiver_error_code(exc)) from exc


# get_appliance_info's tool result shape (S29 — typed MCP outputSchema). ``total=False``: the
# disabled path carries only {enabled, note}. ``enabled`` is on every path → non-nullable; ``note``
# (disabled-only) and the 8 projected topology fields (enabled-only; ``version`` is also omit-when-
# null at the client) are ``X | None``. The 8 fields are COPIED UNCONVERTED from getApplianceInfo
# (client dict[str, object]; the wire types are the vendor bean's — a str URL, an int
# clusterInetPort) → each ``object | None`` is the faithful type (a concrete str | None would
# misrepresent a non-str value). No pv/found key (appliance-scoped). class-syntax: valid names.
class ApplianceInfoResult(TypedDict, total=False):
    enabled: bool
    note: str | None
    identity: object | None
    mgmt_url: object | None
    engine_url: object | None
    retrieval_url: object | None
    etl_url: object | None
    data_retrieval_url: object | None
    cluster_inet_port: object | None
    version: object | None


async def _get_appliance_info(timeout: float = 5.0) -> ApplianceInfoResult:
    """Report the appliance's own topology (Archiver MGMT ``getApplianceInfo``) — Fundort 3.

    Tool-only (not shared with diagnose), like :func:`_get_archive_info`: builds the client against
    the MGMT root. Default-disabled — with ``EPICS_MCP_ARCHIVER_URL`` unset it returns
    ``{enabled: false, note}`` and makes NO network call. The result shape has no ``pv``/``found``
    key: getApplianceInfo is appliance-scoped, with no PV present/absent duality — the appliance
    answers (``enabled: true`` + the projected body) or the call errors. A served non-2xx
    (including a 404 = wrong endpoint) propagates as an ArchiverError, never a swallowed answer.
    """
    cfg = get_config()
    if not cfg.archiver_url:
        return {"enabled": False, "note": _DISABLED_NOTE}

    def _run() -> ApplianceInfoResult:
        client = ArchiverClient(
            cfg.archiver_url, timeout=timeout, auth_header=cfg.archiver_auth or None
        )
        # Explicit literal-key + presence-guard build: mypy --strict rejects ``**dict[str, object]``
        # into a TypedDict, and a guarded copy preserves the client's omit-when-null (e.g. version).
        info = client.get_appliance_info()
        result: ApplianceInfoResult = {"enabled": True}
        if "identity" in info:
            result["identity"] = info["identity"]
        if "mgmt_url" in info:
            result["mgmt_url"] = info["mgmt_url"]
        if "engine_url" in info:
            result["engine_url"] = info["engine_url"]
        if "retrieval_url" in info:
            result["retrieval_url"] = info["retrieval_url"]
        if "etl_url" in info:
            result["etl_url"] = info["etl_url"]
        if "data_retrieval_url" in info:
            result["data_retrieval_url"] = info["data_retrieval_url"]
        if "cluster_inet_port" in info:
            result["cluster_inet_port"] = info["cluster_inet_port"]
        if "version" in info:
            result["version"] = info["version"]
        return result

    try:
        return await asyncio.to_thread(_run)
    except ArchiverConnectionError as exc:
        raise EpicsConnectionError(f"Archiver: {exc}") from exc
    except ArchiverError as exc:
        # The server ANSWERED (a served 4xx/5xx) — that is not an outage.
        raise EpicsError(f"Archiver: {exc}", error_code=_archiver_error_code(exc)) from exc
