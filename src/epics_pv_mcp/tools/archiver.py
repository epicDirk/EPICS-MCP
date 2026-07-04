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
        samples, capped = client.get_pv_history(pv, start, end, max_points=max_points)
        return {
            "enabled": True,
            "pv": pv,
            "from": start,
            "to": end,
            "samples": [dict(sample) for sample in samples],
            "total": len(samples),
            "capped": capped,
        }

    try:
        return await asyncio.to_thread(_run)
    except ArchiverError as exc:
        raise EpicsConnectionError(f"Archiver: {exc}") from exc
