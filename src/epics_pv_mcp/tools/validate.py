"""Tool functions for validating EPICS PV connectivity."""

from epics_pv_mcp.errors import (
    EpicsConnectionError,
    EpicsError,
    PVNotFoundError,
    PVTimeoutError,
)
from epics_pv_mcp.services.epics_client import pv_get


async def _validate_pvs(
    pvs: list[str] | None = None,
    file_path: str | None = None,
    timeout: float = 5.0,
) -> dict:
    """Check PV connectivity. Accepts PV list or .bob file path.

    file_path mode requires phoebus_mcp_core.bob_parser (optional dependency).
    Without it, only pvs list mode is available.
    """
    if file_path and not pvs:
        try:
            from phoebus_mcp_core.bob_parser import extract_pvs
        except ImportError as exc:
            raise EpicsError(
                "file_path mode requires phoebus-mcp-core. Install it or provide pvs list instead.",
                error_code="MISSING_DEPENDENCY",
            ) from exc
        # extract_pvs liefert direkt die PV-Namensliste (das frühere parse_bob
        # existierte nicht — die korrekte Funktion ist extract_pvs).
        pvs = extract_pvs(file_path)
        if not pvs:
            return {
                "file_path": file_path,
                "total": 0,
                "connected": 0,
                "disconnected": 0,
                "pvs": [],
            }

    if not pvs:
        raise EpicsError(
            "Provide either pvs list or file_path",
            error_code="INVALID_INPUT",
        )

    # Try to get each PV, classify as connected or disconnected
    results = []
    connected = 0
    disconnected = 0
    for pv_name in pvs:
        try:
            result = await pv_get(pv_name, timeout)
            results.append(
                {
                    "pv_name": pv_name,
                    "status": "connected",
                    "value": result.get("value"),
                }
            )
            connected += 1
        except (PVTimeoutError, PVNotFoundError, EpicsConnectionError):
            results.append({"pv_name": pv_name, "status": "disconnected"})
            disconnected += 1

    return {
        "total": len(pvs),
        "connected": connected,
        "disconnected": disconnected,
        "pvs": results,
    }
