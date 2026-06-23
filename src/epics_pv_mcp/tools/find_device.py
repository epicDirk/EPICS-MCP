"""Tool for the Wedge-2 device lookup: which screens show device X + is it live + which IOC.

The live-plane counterpart of the offline ``find_screen`` (phoebus-display MCP). It REUSES the
build-once reverse-lookup ``opi_navigation.pv_analysis.find_displays`` (never rebuilt) and enriches
each matched channel with a p4p live read and a ChannelFinder source-IOC join — keeping the surface
split: the offline ``find_screen`` stays EPICS-free, the live enrichment lives here. Mirrors the
``crossplane`` trio (pure :mod:`~.services.device_lookup` assembly next to this thin async wrapper).

The blocking offline part (macro-aware inventory + reverse-lookup) runs off the event loop in a
thread; the p4p batch read and the ChannelFinder GET are awaited in the wrapper. The live read is
capped to ``max_batch_size`` channels (a device prefix matches hundreds-to-thousands; one batch
over that cap raises ``BATCH_TOO_LARGE``) — the screen list stays complete, only the live is capped.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path

from opi_navigation.pv_analysis import (
    DEFAULT_PV_CONTEXT_CAP,
    analyze_pv_inventory,
    channel_name,
    find_displays,
)
from opi_navigation.pv_analysis.lookup import MatchMode, PvLookupResult

from epics_pv_mcp.config import get_config
from epics_pv_mcp.errors import EpicsError
from epics_pv_mcp.services.device_lookup import (
    build_device_report,
    collect_channels,
    render_markdown,
)
from epics_pv_mcp.services.epics_client import pv_get_batch
from epics_pv_mcp.tools.channelfinder import _find_channels


def _run_lookup(
    displays_dir: str,
    query: str,
    match: MatchMode,
    context_cap: int,
    windows_paths: bool,
) -> tuple[PvLookupResult, tuple[str, ...]]:
    """Blocking offline part (run in a thread): macro-aware inventory → reverse-lookup → channels.

    *displays_dir* must be the project/dataset ROOT (the inventory binds display macros via the
    operator top-levels there — a narrow per-IOC subdirectory under-resolves, like ``crossplane``).
    """
    inventory = analyze_pv_inventory(
        Path(displays_dir), context_cap=context_cap, windows_paths=windows_paths
    )
    lookup = find_displays(inventory, query, match=match)
    return lookup, collect_channels(lookup)


async def _find_device(
    query: str,
    displays_dir: str,
    match: MatchMode = "prefix",
    timeout: float = 5.0,
    context_cap: int = DEFAULT_PV_CONTEXT_CAP,
    windows_paths: bool = False,
) -> dict[str, object]:
    """Find the operator screens for *query*, read its channels live, and join the serving IOC.

    Read-only. *query* is a device / PV channel (protocol prefix optional); *match* is
    ``exact``/``prefix``/``substring`` (matched against the protocol-stripped channel).
    *displays_dir* is the project/dataset ROOT. Live values come from p4p, localhost-isolated by
    default (does NOT reach ESS production until the launcher widens the EPICS address list); the
    live read is capped to ``max_batch_size`` channels with an honest note (the screen list stays
    complete). Source IOC comes from ChannelFinder, disabled by default (empty
    ``EPICS_MCP_CHANNELFINDER_URL`` → no source IOC, honest note). ``ca``-only PVs are not read
    under the single ``pva`` provider. Returns ``{"report": <JSON>, "markdown": <rendered>}``.
    Raises :class:`EpicsError` (``INVALID_INPUT``) on an empty query or a missing displays dir.
    """
    cleaned = query.strip()
    if not cleaned:
        raise EpicsError("query must not be empty", error_code="INVALID_INPUT")
    if not Path(displays_dir).is_dir():
        raise EpicsError(
            f"displays_dir is not a directory: {displays_dir}", error_code="INVALID_INPUT"
        )

    lookup, channels = await asyncio.to_thread(
        _run_lookup, displays_dir, cleaned, match, context_cap, windows_paths
    )

    # Live read, capped to one batch (a device prefix matches hundreds-to-thousands of channels;
    # >max_batch_size raises BATCH_TOO_LARGE). Screens stay complete; only the live part samples.
    cfg = get_config()
    read = channels[: cfg.max_batch_size]
    live_capped = len(channels) > len(read)
    live: Mapping[str, object] = (
        await pv_get_batch(list(read), timeout) if read else {"results": [], "errors": []}
    )

    # ChannelFinder source-IOC join with a match-aware glob: a substring match need not start with
    # the query, so broaden to ``*stem*``; prefix/exact stay anchored at ``stem*``. The exact-name
    # join in build_device_report filters the (over-broad) fetch. Best-effort — a CF outage must not
    # sink the screens+live result (mirrors pv_get_batch, which degrades rather than raising).
    stem = channel_name(cleaned).rstrip(":")
    glob = f"*{stem}*" if match == "substring" else f"{stem}*"
    try:
        iocs: Mapping[str, object] = await _find_channels(glob)
    except EpicsError:
        iocs = {
            "enabled": True,
            "channels": [],
            "note": "ChannelFinder unreachable — source IOC not resolved.",
        }

    report = build_device_report(
        lookup,
        live,
        iocs,
        total_matched=len(channels),
        live_capped=live_capped,
        channelfinder_enabled=bool(iocs.get("enabled")),
    )
    return {"report": report.model_dump(mode="json"), "markdown": render_markdown(report)}
