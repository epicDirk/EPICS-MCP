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
from epics_pv_mcp.services.checkers import _archiver_error_code, query_archived

#: Used by _get_pv_history below; the sibling _is_archived note lives with query_archived in
#: services/checkers.py (same string, self-contained per layer — no services ↔ tools coupling).
_DISABLED_NOTE = (
    "Archiver Appliance is disabled. Set EPICS_MCP_ARCHIVER_URL to the appliance root "
    "(e.g. http://archiver:17665)."
)


# get_pv_history's tool result shape (S29 — typed MCP outputSchema; the pattern the Olog cluster set
# in services/checkers_olog.py). ``total=False`` because the disabled path (below) carries only a
# subset. Every field ABSENT on some return path is typed ``X | None``, because FastMCP serializes
# an omitted total=False key as JSON ``null`` (its ``convert_result`` dumps the model WITHOUT
# ``exclude_none``): a non-nullable type would make the emitted structuredContent violate the tool's
# own advertised outputSchema (the MA-1 trap). Only ``enabled``/``pv``/``samples``/``total`` are on
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


async def _is_archived(pv: str, timeout: float = 5.0) -> dict[str, object]:
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


async def _list_archived_pvs(
    pattern: str | None = None,
    this_appliance: bool = False,
    limit: int = DEFAULT_MAX_PV_NAMES,
    timeout: float = 5.0,
) -> dict[str, object]:
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

    def _run() -> dict[str, object]:
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


async def _get_archive_info(pv: str, timeout: float = 5.0) -> dict[str, object]:
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

    def _run() -> dict[str, object]:
        client = ArchiverClient(
            cfg.archiver_url, timeout=timeout, auth_header=cfg.archiver_auth or None
        )
        return {"enabled": True, "pv": pv, **client.get_pv_type_info(pv)}

    try:
        return await asyncio.to_thread(_run)
    except ArchiverConnectionError as exc:
        raise EpicsConnectionError(f"Archiver: {exc}") from exc
    except ArchiverError as exc:
        # The server ANSWERED (a served 4xx/5xx) — that is not an outage.
        raise EpicsError(f"Archiver: {exc}", error_code=_archiver_error_code(exc)) from exc


async def _get_appliance_info(timeout: float = 5.0) -> dict[str, object]:
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

    def _run() -> dict[str, object]:
        client = ArchiverClient(
            cfg.archiver_url, timeout=timeout, auth_header=cfg.archiver_auth or None
        )
        return {"enabled": True, **client.get_appliance_info()}

    try:
        return await asyncio.to_thread(_run)
    except ArchiverConnectionError as exc:
        raise EpicsConnectionError(f"Archiver: {exc}") from exc
    except ArchiverError as exc:
        # The server ANSWERED (a served 4xx/5xx) — that is not an outage.
        raise EpicsError(f"Archiver: {exc}", error_code=_archiver_error_code(exc)) from exc
