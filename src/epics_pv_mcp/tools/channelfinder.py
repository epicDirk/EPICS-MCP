"""MCP adapter for ChannelFinder channel lookup (read-only).

Thin wrapper: the config-gated, off-loop query lives in the services layer
(:func:`epics_pv_mcp.services.checkers.query_channels`) so :mod:`~epics_pv_mcp.services.diagnose`
and :mod:`~epics_pv_mcp.tools.find_device` reuse it without an upward ``services → tools`` import
(M9). Default-disabled behaviour (no ``EPICS_MCP_CHANNELFINDER_URL`` → no network call) is enforced
there.
"""

from __future__ import annotations

from epics_pv_mcp.services.channelfinder_client import DEFAULT_MAX_RESULTS
from epics_pv_mcp.services.checkers import (
    ChannelVocabularyResult,
    query_channel_vocabulary,
    query_channels,
)


async def _find_channels(
    name_pattern: str,
    max_results: int = DEFAULT_MAX_RESULTS,
    timeout: float = 5.0,
    *,
    has_properties: dict[str, str] | None = None,
    lacks_properties: list[str] | None = None,
    not_property_values: dict[str, str] | None = None,
    has_tags: list[str] | None = None,
    lacks_tags: list[str] | None = None,
    count_only: bool = False,
) -> dict[str, object]:
    """Query ChannelFinder for channels whose name matches *name_pattern* (glob ``*``/``?``).

    Thin MCP adapter over :func:`epics_pv_mcp.services.checkers.query_channels`; forwards the MA-2
    property/tag filters and ``count_only``.
    """
    return await query_channels(
        name_pattern,
        max_results=max_results,
        timeout=timeout,
        has_properties=has_properties,
        lacks_properties=lacks_properties,
        not_property_values=not_property_values,
        has_tags=has_tags,
        lacks_tags=lacks_tags,
        count_only=count_only,
    )


async def _list_channel_vocabulary(timeout: float = 5.0) -> ChannelVocabularyResult:
    """List the ChannelFinder property + tag NAMES ``find_channels`` can be filtered on.

    Thin MCP adapter over :func:`epics_pv_mcp.services.checkers.query_channel_vocabulary`;
    default-disabled behaviour (no ``EPICS_MCP_CHANNELFINDER_URL`` → no network call) lives there.
    """
    return await query_channel_vocabulary(timeout=timeout)
