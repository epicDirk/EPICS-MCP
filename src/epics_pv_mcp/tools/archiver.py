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

from epics_pv_mcp.config import get_config
from epics_pv_mcp.errors import EpicsConnectionError
from epics_pv_mcp.services.archiver_client import DEFAULT_MAX_POINTS, ArchiverClient
from epics_pv_mcp.services.archiver_exceptions import ArchiverError
from epics_pv_mcp.services.checkers import query_archived

#: Used by _get_pv_history below; the sibling _is_archived note lives with query_archived in
#: services/checkers.py (same string, self-contained per layer — no services ↔ tools coupling).
_DISABLED_NOTE = (
    "Archiver Appliance is disabled. Set EPICS_MCP_ARCHIVER_URL to the appliance root "
    "(e.g. http://archiver:17665)."
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
) -> dict[str, object]:
    """Fetch archived samples for *pv* between *start* and *end* (ISO-8601), capped."""
    cfg = get_config()
    if not cfg.archiver_url:
        return {"enabled": False, "pv": pv, "samples": [], "total": 0, "note": _DISABLED_NOTE}

    def _run() -> dict[str, object]:
        # get_pv_history hits /retrieval/data — in the ESS 4-instance topology that lives on a
        # separate Tomcat (:17668) from mgmt (:17665). Pass the retrieval URL; it falls back to
        # archiver_url inside ArchiverClient when the retrieval URL env is unset (single-JVM).
        client = ArchiverClient(
            cfg.archiver_url,
            timeout=timeout,
            auth_header=cfg.archiver_auth or None,
            retrieval_url=cfg.archiver_retrieval_url or None,
        )
        history = client.get_pv_history(pv, start, end, max_points=max_points)
        result: dict[str, object] = {
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

    try:
        return await asyncio.to_thread(_run)
    except ArchiverError as exc:
        raise EpicsConnectionError(f"Archiver: {exc}") from exc


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
    except ArchiverError as exc:
        raise EpicsConnectionError(f"Archiver: {exc}") from exc
