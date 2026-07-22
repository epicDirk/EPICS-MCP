"""Tool functions for discovering EPICS PVs by name or pattern."""

from epics_pv_mcp.errors import EpicsConnectionError, PVNotFoundError, PVTimeoutError
from epics_pv_mcp.services.channelfinder_client import DEFAULT_MAX_RESULTS
from epics_pv_mcp.services.checkers import query_channels
from epics_pv_mcp.services.epics_client import pv_get

# Metacharacters that switch discover_pvs from a concrete-name connect to a ChannelFinder glob
# query. ChannelFinder's own glob understands ``*`` and ``?``; bracket classes are not part of its
# syntax and are passed through verbatim (matched literally by the server).
_WILDCARD_CHARS = "*?[]"

# The honest 'no wildcard enumeration without ChannelFinder' stub — returned unchanged when CF is
# not configured, so an empty answer never reads as "no such PV" (kept byte-identical to the
# pre-MA-2 stub so its meaning is stable).
_WILDCARD_STUB_NOTE = (
    "Wildcard PV discovery requires ChannelFinder or similar "
    "infrastructure. p4p alone cannot enumerate PVs by pattern. "
    "Try a concrete PV name instead."
)

# Provenance note on a wildcard result: ChannelFinder REGISTRY membership is not a liveness probe.
_WILDCARD_REGISTRY_NOTE = (
    "Wildcard matches are ChannelFinder REGISTRY entries (registered channels), NOT a live "
    "connectivity probe — status 'registered' means 'known to ChannelFinder'. Use get_pvs / "
    "get_pv_value for live values/connectivity. The glob is interpreted by the server and is "
    "ANCHORED and CASE-INSENSITIVE: wrap a substring in * to match inside a name."
)


async def _discover_pvs(pattern: str, timeout: float | None = None) -> dict[str, object]:
    """Discover PVs by name.

    - **Concrete PV names**: connect via p4p and return a live status
      (found/not_found/timeout/error) plus the value on a hit.
    - **Wildcard patterns** (``* ? [ ]``): delegate to ChannelFinder — the runtime PV registry —
      via :func:`~epics_pv_mcp.services.checkers.query_channels`. p4p alone cannot enumerate PVs by
      pattern; the CF glob search already exists, so discover_pvs just routes wildcards to it.

    A wildcard hit reports ``status: "registered"`` — CF membership, NOT a live connect (distinct
    from the concrete-name ``found``). Use ``get_pvs``/``get_pv_value`` for liveness. The glob is
    interpreted by the ChannelFinder SERVER and is ANCHORED + CASE-INSENSITIVE (measured live
    2026-07-15, see ``find_channels``): a bare substring matches nothing — wrap it in ``*``. With
    ChannelFinder unconfigured the wildcard branch keeps an honest 'requires ChannelFinder' stub
    rather than a bare empty result.
    """
    if any(c in pattern for c in _WILDCARD_CHARS):
        return await _discover_by_channelfinder(pattern, timeout)

    # Treat as concrete PV name — try to connect. Keep the client's classification distinct (S3-5):
    # a timeout (IOC down / network) or a connection error must NOT collapse to "not_found", which
    # would make a diagnosable disconnect indistinguishable from a genuinely non-existent PV.
    try:
        result = await pv_get(pattern, timeout)
        return {
            "pattern": pattern,
            "pvs": [{"pv_name": pattern, "status": "found", "value": result.get("value")}],
            "total": 1,
        }
    except PVNotFoundError:
        status = "not_found"
    except PVTimeoutError:
        status = "timeout"
    except EpicsConnectionError:
        status = "error"
    return {
        "pattern": pattern,
        "pvs": [{"pv_name": pattern, "status": status}],
        "total": 0,
    }


async def _discover_by_channelfinder(pattern: str, timeout: float | None) -> dict[str, object]:
    """Route a wildcard *pattern* to the ChannelFinder glob search, mapped into the discover shape.

    Returns the honest 'requires ChannelFinder' stub when CF is not configured (``enabled: False``)
    so an empty answer never reads as 'no such PV'. Otherwise each registered channel becomes a
    ``{pv_name, status: "registered", ioc_name, host_name}`` entry — the uniform discover shape,
    with the CF ``capped`` flag carried through (a broad glob can truncate the registry).
    """
    result = await query_channels(
        pattern,
        max_results=DEFAULT_MAX_RESULTS,
        # query_channels takes a concrete float; None means "use the CF client default" (5.0).
        timeout=timeout if timeout is not None else 5.0,
    )
    if not result.get("enabled"):
        return {"pattern": pattern, "pvs": [], "total": 0, "note": _WILDCARD_STUB_NOTE}

    pvs: list[dict[str, object]] = []
    raw_channels = result.get("channels")
    if isinstance(raw_channels, list):
        for channel in raw_channels:
            if not isinstance(channel, dict):
                continue
            pvs.append(
                {
                    "pv_name": channel.get("name"),
                    "status": "registered",
                    "ioc_name": channel.get("ioc_name"),
                    "host_name": channel.get("host_name"),
                }
            )
    return {
        "pattern": pattern,
        "pvs": pvs,
        "total": len(pvs),
        "capped": bool(result.get("capped")),
        "source": "channelfinder",
        "note": _WILDCARD_REGISTRY_NOTE,
    }
