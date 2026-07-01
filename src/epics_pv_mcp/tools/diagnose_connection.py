"""Thin wrapper for the ``diagnose_connection`` tool (read-only).

All logic lives in :mod:`epics_pv_mcp.services.diagnose`; this only runs the diagnosis and returns
the frozen report as a plain JSON-able dict (the MCP surface). A disconnected PV is a NORMAL result
here — the service catches the p4p exceptions internally, so this wrapper does not translate a
disconnect into an error (only genuine internal errors reach the ``EpicsError -> ToolError`` shell).
"""

from __future__ import annotations

from epics_pv_mcp.services.diagnose import diagnose


async def _diagnose_connection(
    pv_name: str,
    *,
    timeout: float | None = None,
    check_channelfinder: bool = True,
    check_naming: bool = False,
    check_archiver: bool = False,
    check_alarm: bool = False,
) -> dict[str, object]:
    """Run the live-authoritative diagnosis and return the report as a dict."""
    report = await diagnose(
        pv_name,
        timeout=timeout,
        check_channelfinder=check_channelfinder,
        check_naming=check_naming,
        check_archiver=check_archiver,
        check_alarm=check_alarm,
    )
    return report.model_dump(mode="json")
