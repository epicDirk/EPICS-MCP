"""Tool functions for discovering EPICS PVs by name or pattern."""

from typing import TypedDict

from epics_mcp.errors import EpicsConnectionError, PVNotFoundError, PVTimeoutError
from epics_mcp.provenance import Reach, reach_of
from epics_mcp.services.channelfinder_client import DEFAULT_MAX_RESULTS
from epics_mcp.services.checkers import query_channels
from epics_mcp.services.epics_client import pv_get

# Metacharacters that switch discover_pvs from a concrete-name connect to a ChannelFinder glob
# query. ChannelFinder's own glob understands ``*`` and ``?``; bracket classes are not part of its
# syntax and are passed through verbatim (matched literally by the server).
_WILDCARD_CHARS = "*?[]"

# The honest 'no wildcard enumeration without ChannelFinder' stub, returned unchanged when CF is
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
    "connectivity probe, status 'registered' means 'known to ChannelFinder'. Use get_pvs / "
    "get_pv_value for live values/connectivity. The glob is interpreted by the server and is "
    "ANCHORED and CASE-INSENSITIVE: wrap a substring in * to match inside a name."
)


# discover_pvs' tool result shape (S29, typed MCP outputSchema). Lives here, not at
# ``query_channels``: the wildcard branch only READS that shared payload and builds its OWN
# literal, so this type describes the discover shape, not the ChannelFinder one.
# (``query_channels`` has since been typed in its own right, ``ChannelQueryResult``, so the
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
#     carries no ``required``, so an absent key is invisible to a real client. For a
#     merely-sometimes-absent field the nullable type is therefore SCHEMA HONESTY, enforced by
#     the conformance test's Part B, not by the runtime.
#   * A field set EXPLICITLY to None IS emitted as null, and the wire path DOES validate: the
#     MCP SDK's low-level handler runs jsonschema against the advertised outputSchema, so a
#     non-nullable annotation there earns a real client an ``Output validation error``. No such
#     field exists here today; the rule is written down so the next field does not have to
#     rediscover it. (The in-process ``FastMCP.call_tool`` shortcut does NOT validate, which is
#     why this tool's Part A drives a real ``fastmcp.Client`` instead of that shortcut.)
#
# ``pvs`` stays ``list[dict[str, object]]`` rather than a nested TypedDict: the entries are
# heterogeneous in THREE shapes, {pv_name, status, value} on a concrete hit, {pv_name, status}
# on a miss, {pv_name, status, ioc_name, host_name} for a registry match, and ``value`` carries
# whatever PVA type the channel holds. Same convention as AlarmHistoryResult.events; no output
# shape in this server uses a nested TypedDict. class-syntax: all names are valid identifiers.
class DiscoverPvsResult(TypedDict, total=False):
    pattern: str
    pvs: list[dict[str, object]]
    total: int
    capped: bool | None
    source: str | None
    note: str | None
    reach: Reach


async def _discover_pvs(pattern: str, timeout: float | None = None) -> DiscoverPvsResult:
    """Discover PVs by name.

    - **Concrete PV names**: connect via p4p and return a live status
      (found/not_found/timeout/error) plus the value on a hit.
    - **Wildcard patterns** (``* ? [ ]``): delegate to ChannelFinder, the runtime PV registry,
      via :func:`~epics_mcp.services.checkers.query_channels`. p4p alone cannot enumerate PVs by
      pattern; the CF glob search already exists, so discover_pvs just routes wildcards to it.

    A wildcard hit reports ``status: "registered"``: CF membership, NOT a live connect (distinct
    from the concrete-name ``found``). Use ``get_pvs``/``get_pv_value`` for liveness. The glob is
    interpreted by the ChannelFinder SERVER and is ANCHORED + CASE-INSENSITIVE (measured live
    2026-07-15, see ``find_channels``): a bare substring matches nothing, wrap it in ``*``. With
    ChannelFinder unconfigured the wildcard branch keeps an honest 'requires ChannelFinder' stub
    rather than a bare empty result.
    """
    # GB-64: this tool is the ONE read whose plane depends on the argument, so it computes its
    # own reach per branch instead of carrying a static ``@with_reach`` at the tool boundary. A
    # fixed plane would be wrong half the time, and wrong in the direction that matters: a
    # wildcard answered by an unconfigured ChannelFinder would be labelled with the live-PV
    # reach, i.e. it would report a lane that answered nothing. This tool carries no
    # ``with_reach`` decorator at all, so nothing overwrites what it sets here.
    if any(c in pattern for c in _WILDCARD_CHARS):
        wildcard = await _discover_by_channelfinder(pattern, timeout)
        wildcard["reach"] = reach_of("channelfinder")
        return wildcard

    # Treat as concrete PV name: try to connect. Keep the client's classification distinct (S3-5):
    # a timeout (IOC down / network) or a connection error must NOT collapse to "not_found", which
    # would make a diagnosable disconnect indistinguishable from a genuinely non-existent PV.
    try:
        result = await pv_get(pattern, timeout)
        return {
            "pattern": pattern,
            "pvs": [{"pv_name": pattern, "status": "found", "value": result.get("value")}],
            "total": 1,
            "reach": reach_of("live-pv"),
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
        "reach": reach_of("live-pv"),
    }


async def _discover_by_channelfinder(pattern: str, timeout: float | None) -> DiscoverPvsResult:
    """Route a wildcard *pattern* to the ChannelFinder glob search, mapped into the discover shape.

    Returns the honest 'requires ChannelFinder' stub when CF is not configured (``enabled: False``)
    so an empty answer never reads as 'no such PV'. Otherwise each registered channel becomes a
    ``{pv_name, status: "registered", ioc_name, host_name}`` entry, the uniform discover shape,
    with the CF ``capped`` flag carried through (a broad glob can truncate the registry).

    On an off-contract payload this DEGRADES QUIETLY, which the stub sentence above does not cover
    and which is the asymmetry worth knowing before editing here: a non-list ``channels`` is
    discarded whole and a non-dict entry is dropped, in both cases producing an empty answer that
    still carries ``source`` and the registry note, so it reads exactly like an empty registry. No
    real call path produces such a payload (the client raises first), and the two checks that do
    this are pinned test by test in the S35 block of ``tests/test_discover.py``.
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
    # guarantee. The element check is a comprehension FILTER rather than an ``if not ...: continue``
    # because the latter's body is provably dead under the typed seam and mypy's warn_unreachable
    # rejects it (probed with the loop form spliced in: mypy answers "Statement is unreachable"),
    # the filter form says the same thing and is what ``diagnose._gather_channelfinder`` and
    # ``checkers_olog.query_olog_add_attachment`` / ``query_olog_update`` already use on this kind
    # of payload. Named by FUNCTION, and that is the repair rather than the style: this sentence
    # carried ``diagnose.py:386-387`` and ``checkers_olog.py:751-753``, which pointed at the right
    # blocks when written (429f2ce, 2026-07-25; the second window stopped one line short of the
    # filter itself) and have since drifted by +3 and by -36 lines onto a
    # ``ChannelFinderEvidence`` return and a dict literal. A reader checking them today finds
    # unrelated code and concludes the comment is confused. Same rot that
    # ``tests/test_client_edge_guards.py`` records for its own table.
    #
    # Honest scope (S35). Both checks are OBSERVED now by the S35 block in
    # ``tests/test_discover.py``, as a SECTION rather than test by test: traced, two of its five
    # never reach the element-check line at all, because their payload is rejected whole by the
    # list check first. The gap was also narrower than this comment used to
    # say. It said "neither check is OBSERVED", which is true only of the ENABLING polarity:
    # forcing either check to ``False`` empties the answer and reddens
    # ``test_discover_wildcard_delegates_to_channelfinder``, which has covered that half all along.
    # Forcing either to ``True`` left the FULL suite green, 2023 passed under both mutants,
    # measured on this tree before the S35 tests existed. That is the mutual masking, and it is
    # mechanical: with a dict or a str payload the mutant iterates keys or characters and the
    # OTHER check drops every one, so guarded and mutant answer identically.
    # Each check is therefore pinned by TWO instruments, because one instrument is a point and
    # never a class. Measured, and the first draft of this comment had it backwards: a tuple
    # catches ``(list, tuple)``/``Sequence`` and MISSES ``MutableSequence``, while a ``deque``
    # catches ``MutableSequence`` and misses ``(list, tuple)``; a ``MappingProxyType`` catches
    # ``Mapping``/``hasattr(.., "get")`` and, being immutable, MISSES ``MutableMapping``, which a
    # ``UserDict`` catches. Disjoint sets on both sides. A subclass case guards the other
    # direction, ``type(x) is list`` / ``type(x) is dict``.
    # Coverage, stated as the sweep that produced it rather than as an adjective: 19 weakenings of
    # the two checks were spliced in and run against the file. ONE survives,
    # ``isinstance(raw_channels, (list, dict))``, and it is residue rather than an equivalent
    # mutant: two payloads do separate it (a dict keyed by a ``__hash__``-defining dict subclass,
    # and a dict subclass with its own ``__iter__``), neither reachable from a JSON body.
    # ``scripts/guard_audit.py`` does not sweep these two lines and cannot as it stands: it globs
    # ``services/*_client.py``, so this module is outside its population by construction, and these
    # tests are the whole of the watch here.
    # They stay because a typing change must not alter runtime behaviour, and now also because they
    # are pinned. What they deliberately do NOT do is match the client underneath, which REFUSES
    # the same shapes loudly: ``ChannelFinderClient.find_channels`` raises on a non-list body and
    # on a non-dict record. Here the same shapes answer empty. That divergence is named in the
    # tests, and it is not a wire-contract question, since no real call path reaches these lines.
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
        # bool()-coerce, and NOT because the seam is untyped, it is typed now
        # (``ChannelQueryResult.capped`` is ``bool | None``). The coercion stays for the one reason
        # the annotation cannot cover: the seam is REPLACED wholesale by a test double that
        # deliberately injects a truthy STRING here (pinned by the conformance test's fake), and a
        # raw copy would then emit that string into this ``boolean | null`` field.
        "capped": bool(result.get("capped")),
        "source": "channelfinder",
        "note": _WILDCARD_REGISTRY_NOTE,
    }
