"""Tool functions for the EPICS Archiver Appliance (read-only).

Default-disabled: when ``EPICS_MCP_ARCHIVER_URL`` is unset, returns a structured
``enabled: false`` result and makes **no** network call (preserves localhost isolation).
"""

from __future__ import annotations

import asyncio

from epics_pv_mcp.config import get_config
from epics_pv_mcp.errors import EpicsConnectionError
from epics_pv_mcp.services.archiver_client import DEFAULT_MAX_POINTS, ArchiverClient
from epics_pv_mcp.services.archiver_exceptions import ArchiverError

_DISABLED_NOTE = (
    "Archiver Appliance is disabled. Set EPICS_MCP_ARCHIVER_URL to the appliance root "
    "(e.g. http://archiver:17665)."
)


async def _is_archived(pv: str, timeout: float = 5.0) -> dict[str, object]:
    """Report whether *pv* is being archived (Archiver MGMT getPVStatus)."""
    cfg = get_config()
    if not cfg.archiver_url:
        return {"enabled": False, "pv": pv, "archived": None, "note": _DISABLED_NOTE}

    def _run() -> dict[str, object]:
        client = ArchiverClient(
            cfg.archiver_url, timeout=timeout, auth_header=cfg.archiver_auth or None
        )
        archived, status = client.is_archived(pv)
        return {"enabled": True, "pv": pv, "archived": archived, "status": status}

    try:
        return await asyncio.to_thread(_run)
    except ArchiverError as exc:
        raise EpicsConnectionError(f"Archiver: {exc}") from exc


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
        client = ArchiverClient(
            cfg.archiver_url, timeout=timeout, auth_header=cfg.archiver_auth or None
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
