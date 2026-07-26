"""Thin wrapper for the ``diagnose_connection`` tool (read-only).

All logic lives in :mod:`epics_pv_mcp.services.diagnose`; this only runs the diagnosis and returns
the frozen report as a plain JSON-able dict (the MCP surface). A disconnected PV is a NORMAL result
here, the service catches the p4p exceptions internally, so this wrapper does not translate a
disconnect into an error (only genuine internal errors reach the ``EpicsError -> ToolError`` shell).
"""

from __future__ import annotations

from epics_pv_mcp.services.diagnose import (
    DEFAULT_CHECK_ALARM,
    DEFAULT_CHECK_ARCHIVER,
    DEFAULT_CHECK_CHANNELFINDER,
    DEFAULT_CHECK_NAMING,
    diagnose,
)


async def _diagnose_connection(
    pv_name: str,
    *,
    timeout: float | None = None,
    check_channelfinder: bool = DEFAULT_CHECK_CHANNELFINDER,
    check_naming: bool = DEFAULT_CHECK_NAMING,
    check_archiver: bool = DEFAULT_CHECK_ARCHIVER,
    check_alarm: bool = DEFAULT_CHECK_ALARM,
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
