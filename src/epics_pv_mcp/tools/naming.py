"""MCP adapter for ESS Naming-Service device-name lookup (read-only).

Thin wrapper: the config-gated, off-loop query lives in the services layer
(:func:`epics_pv_mcp.services.checkers.query_naming_lookup`), the same place ``diagnose_connection``
and ``crossplane_check`` reach the Naming Service — so this tool adds a standalone surface without
duplicating the gate or a ``services -> tools`` upward import (M9). Default-disabled behaviour
(no ``EPICS_MCP_NAMING_URL`` -> no network call, no ESS egress) is enforced there.
"""

from __future__ import annotations

from epics_pv_mcp.services.checkers import NameLookupResult, query_naming_lookup


async def _lookup_device_name(name: str, timeout: float = 5.0) -> NameLookupResult:
    """Look up whether an ESS device name is registered + ACTIVE in the Naming Service.

    Thin MCP adapter over :func:`epics_pv_mcp.services.checkers.query_naming_lookup`.
    """
    return await query_naming_lookup(name, timeout=timeout)
