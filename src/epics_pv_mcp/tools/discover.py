"""Tool functions for discovering EPICS PVs by name or pattern."""

from typing import TypedDict

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


# discover_pvs' tool result shape (S29 -- typed MCP outputSchema). Lives here, not at
# ``query_channels``: the wildcard branch only READS that shared payload and builds its OWN
# literal, so this type describes the discover shape, not the ChannelFinder one.
# (``query_channels`` has since been typed in its own right -- ``ChannelQueryResult`` -- so the
# point is no longer that the shared signature stays untouched; it is that the two shapes are
# genuinely different literals.)
#
# ``total=False`` over the union of FOUR return literals: concrete-hit, concrete-miss, wildcard
# with ChannelFinder disabled, wildcard enabled. ``pattern``/``pvs``/``total`` are on all four ->
# non-nullable. ``capped``/``source`` are wildcard-enabled-only; ``note`` is on BOTH wildcard
# paths (the 'requires ChannelFinder' stub AND the registry-provenance note) and absent only on
# the two concrete-name paths -> all three are ``X | None``.
#
# Why the nullability, measured under standalone fastmcp (the two halves differ, and the
# difference is the point):
#   * An ABSENT total=False key is DROPPED from the wire, not emitted as null, and the schema
#     carries no ``required`` -- so an absent key is invisible to a real client. For a
#     merely-sometimes-absent field the nullable type is therefore SCHEMA HONESTY, enforced by
#     the conformance test's Part B, not by the runtime.
#   * A field set EXPLICITLY to None IS emitted as null, and the wire path DOES validate: the
#     MCP SDK's low-level handler runs jsonschema against the advertised outputSchema, so a
#     non-nullable annotation there earns a real client an ``Output validation error``. No such
#     field exists here today; the rule is written down so the next field does not have to
#     rediscover it. (The in-process ``FastMCP.call_tool`` shortcut does NOT validate -- which is
#     why this tool's Part A drives a real ``fastmcp.Client`` instead of that shortcut.)
#
# ``pvs`` stays ``list[dict[str, object]]`` rather than a nested TypedDict: the entries are
# heterogeneous in THREE shapes -- {pv_name, status, value} on a concrete hit, {pv_name, status}
# on a miss, {pv_name, status, ioc_name, host_name} for a registry match -- and ``value`` carries
# whatever PVA type the channel holds. Same convention as AlarmHistoryResult.events; no output
# shape in this server uses a nested TypedDict. class-syntax: all names are valid identifiers.
class DiscoverPvsResult(TypedDict, total=False):
    pattern: str
    pvs: list[dict[str, object]]
    total: int
    capped: bool | None
    source: str | None
    note: str | None


async def _discover_pvs(pattern: str, timeout: float | None = None) -> DiscoverPvsResult:
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


async def _discover_by_channelfinder(pattern: str, timeout: float | None) -> DiscoverPvsResult:
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

    # Both shape checks are KEPT although ``query_channels`` is now typed: the seam is replaced
    # wholesale by test doubles (see the ``capped`` note below), so its wire type is not a runtime
    # guarantee. The element check is a comprehension FILTER rather than an ``if not …: continue``
    # because the latter's body is provably dead under the typed seam and mypy's warn_unreachable
    # rejects it -- the filter form says the same thing and is what diagnose.py:386-387 and
    # checkers_olog.py:751-753 already use on this kind of payload. Honest scope: neither check is
    # OBSERVED -- no double injects a non-list or a non-dict element today, and the pair is mutually
    # masking (tests/test_client_edge_guards.py records that class). They stay because a typing
    # change must not alter runtime behaviour, not because they are guarded.
    raw_channels = result.get("channels")
    pvs: list[dict[str, object]] = [
        {
            "pv_name": channel.get("name"),
            "status": "registered",
            "ioc_name": channel.get("ioc_name"),
            "host_name": channel.get("host_name"),
        }
        for channel in (raw_channels if isinstance(raw_channels, list) else [])
        if isinstance(channel, dict)
    ]
    return {
        "pattern": pattern,
        "pvs": pvs,
        "total": len(pvs),
        # bool()-coerce, and NOT because the seam is untyped -- it is typed now
        # (``ChannelQueryResult.capped`` is ``bool | None``). The coercion stays for two reasons the
        # annotation cannot cover: the seam is REPLACED by a test double that deliberately injects a
        # truthy STRING here (pinned by the conformance test's fake), and ``None`` from a
        # count_only-shaped payload must land as ``False``, not as a null in a non-nullable field.
        "capped": bool(result.get("capped")),
        "source": "channelfinder",
        "note": _WILDCARD_REGISTRY_NOTE,
    }
