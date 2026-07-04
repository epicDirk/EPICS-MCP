"""MCP adapter for ChannelFinder channel lookup (read-only).

Thin wrapper: the config-gated, off-loop query lives in the services layer
(:func:`epics_pv_mcp.services.checkers.query_channels`) so :mod:`~epics_pv_mcp.services.diagnose`
and :mod:`~epics_pv_mcp.tools.find_device` reuse it without an upward ``services → tools`` import
(M9). Default-disabled behaviour (no ``EPICS_MCP_CHANNELFINDER_URL`` → no network call) is enforced
there.
"""

from __future__ import annotations

from epics_pv_mcp.services.channelfinder_client import DEFAULT_MAX_RESULTS
from epics_pv_mcp.services.checkers import query_channels


async def _find_channels(
    name_pattern: str,
    max_results: int = DEFAULT_MAX_RESULTS,
    timeout: float = 5.0,
) -> dict[str, object]:
    """Query ChannelFinder for channels whose name matches *name_pattern* (glob ``*``/``?``).

    Thin MCP adapter over :func:`epics_pv_mcp.services.checkers.query_channels`.
    """
    return await query_channels(name_pattern, max_results=max_results, timeout=timeout)
