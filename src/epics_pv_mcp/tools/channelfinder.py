"""Tool function for ChannelFinder channel lookup (read-only).

Default-disabled: when ``EPICS_MCP_CHANNELFINDER_URL`` is unset, returns a structured
``enabled: false`` result and makes **no** network call (preserves localhost isolation).
"""

from __future__ import annotations

import asyncio

from epics_pv_mcp.config import get_config
from epics_pv_mcp.errors import EpicsConnectionError
from epics_pv_mcp.services.channelfinder_client import DEFAULT_MAX_RESULTS, ChannelFinderClient
from epics_pv_mcp.services.channelfinder_exceptions import ChannelFinderError


async def _find_channels(
    name_pattern: str,
    max_results: int = DEFAULT_MAX_RESULTS,
    timeout: float = 5.0,
) -> dict[str, object]:
    """Query ChannelFinder for channels whose name matches *name_pattern* (glob ``*``/``?``)."""
    cfg = get_config()
    if not cfg.channelfinder_url:
        return {
            "enabled": False,
            "channels": [],
            "total": 0,
            "note": (
                "ChannelFinder is disabled. Set EPICS_MCP_CHANNELFINDER_URL to the "
                "ChannelFinder service root (e.g. http://host:8080/ChannelFinder)."
            ),
        }

    def _run() -> dict[str, object]:
        client = ChannelFinderClient(
            cfg.channelfinder_url,
            timeout=timeout,
            auth_header=cfg.channelfinder_auth or None,
        )
        channels = client.find_channels(name_pattern, max_results=max_results)
        return {
            "enabled": True,
            "channels": [dict(channel) for channel in channels],
            "total": len(channels),
            "capped": len(channels) >= max_results,
        }

    try:
        return await asyncio.to_thread(_run)
    except ChannelFinderError as exc:
        raise EpicsConnectionError(f"ChannelFinder: {exc}") from exc
