"""Tool for the Wedge-2 device lookup: where device X is shown + is it live + which IOC.

The live-plane counterpart of the offline ``find_screen`` (phoebus-display MCP). It REUSES the
build-once reverse-lookup ``opi_navigation.pv_analysis.find_displays`` (never rebuilt) and enriches
each matched channel with a p4p live read and a ChannelFinder source-IOC join, keeping the surface
split: the offline ``find_screen`` stays EPICS-free, the live enrichment lives here. Mirrors the
``crossplane`` trio (pure :mod:`~.services.device_lookup` assembly next to this thin async wrapper).

The blocking offline part (macro-aware inventory + reverse-lookup) runs off the event loop in a
thread; the p4p batch read and the ChannelFinder GET are awaited in the wrapper. The live read is
capped to ``max_batch_size`` channels (a device prefix matches hundreds-to-thousands; one batch
over that cap raises ``BATCH_TOO_LARGE``), and that cap does not shorten the match list. The
match list has its own two limits, the inventory walk's context and glob caps, and both are
reported as ``notes`` since GB-65.

⚠ A MATCH IS NOT NECESSARILY A SCREEN, and this module said it was until GQ-21. The lookup cuts on
``operator_facing`` and never on the kind of file, so a ``.plt`` Data Browser trend opened by an
``open_file`` button is a top level of its own and comes back among the matches. That is a correct
answer and is deliberately not filtered; ``services.device_lookup`` carries the kind out on
``ScreenMatch.node_kind`` and splits the count into ``display_count``/``trend_count``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from pathlib import Path

from opi_navigation.pv_analysis import (
    DEFAULT_PV_CONTEXT_CAP,
    channel_name,
    find_displays,
)
from opi_navigation.pv_analysis.lookup import MatchMode, PvLookupResult
from opi_navigation.pv_analysis.models import PvInventory

from epics_mcp.config import get_config
from epics_mcp.errors import EpicsError
from epics_mcp.paths import resolve_user_path
from epics_mcp.services.checkers import ChannelQueryResult, query_channels
from epics_mcp.services.device_lookup import (
    build_device_report,
    collect_channels,
    render_markdown,
)
from epics_mcp.services.epics_client import pv_get_batch
from epics_mcp.services.inventory_adapter import analyze_inventory

logger = logging.getLogger(__name__)


def _run_lookup(
    displays_dir: str,
    query: str,
    match: MatchMode,
    context_cap: int,
    windows_paths: bool,
) -> tuple[PvLookupResult, tuple[str, ...], tuple[str, ...], int]:
    """Blocking offline part (run in a thread): macro-aware inventory → reverse-lookup → channels.

    *displays_dir* must be the project/dataset ROOT (the inventory binds display macros via the
    operator top-levels there, a narrow per-IOC subdirectory under-resolves, like ``crossplane``).

    Returns ``(lookup, channels, context_capped, glob_capped_count)``. The last two are the walk's
    own incompleteness signals, and they have to be returned HERE because ``find_displays`` projects
    the inventory down to the matched displays, so a caller downstream can no longer ask.

    They are not read here, though. :func:`~epics_mcp.services.inventory_adapter.analyze_inventory`
    runs the walk and hands the tail back beside whatever the projection returned, so this tool
    counts the same thing as the other three by construction rather than by agreement (GB-71). It
    used to call the engine itself and read both fields off the inventory, which is precisely how
    it managed to forget them altogether until GB-65.
    """

    def project(inventory: PvInventory) -> tuple[PvLookupResult, tuple[str, ...]]:
        """The reverse-lookup plus its channel list, both out of the SAME walk."""
        lookup = find_displays(inventory, query, match=match)
        return lookup, collect_channels(lookup)

    (lookup, channels), context_capped, glob_capped_count, _files_walked = analyze_inventory(
        Path(displays_dir), project, context_cap=context_cap, windows_paths=windows_paths
    )
    return lookup, channels, context_capped, glob_capped_count


async def _find_device(
    query: str,
    displays_dir: str,
    match: MatchMode = "prefix",
    timeout: float = 5.0,
    context_cap: int = DEFAULT_PV_CONTEXT_CAP,
    windows_paths: bool = False,
) -> dict[str, object]:
    """Find where *query* is shown, read its channels live, and join the serving IOC.

    Read-only. *query* is a device / PV channel (protocol prefix optional); *match* is
    ``exact``/``prefix``/``substring`` (matched against the protocol-stripped channel).
    *displays_dir* is the project/dataset ROOT. A match is an operator screen or a Data Browser
    trend opened by a button, said on ``screens[].node_kind``. Live values come from p4p; reach
    follows the
    launcher's EPICS search env (address lists / name servers / auto-addr search, run
    ``epics-doctor`` for the effective posture); the live read is capped to ``max_batch_size``
    channels with an honest note, and that cap does not shorten the screen list; the walk's own
    context and glob caps do, and each carries its own note. Source IOC comes from
    ChannelFinder, disabled by default (empty ``EPICS_MCP_CHANNELFINDER_URL`` → no source IOC,
    honest note). ``ca``-only PVs are not read
    under the single ``pva`` provider. Returns ``{"report": <JSON>, "markdown": <rendered>}``.
    Raises :class:`EpicsError` (``INVALID_INPUT``) on an empty query or a missing displays dir.
    """
    cleaned = query.strip()
    if not cleaned:
        raise EpicsError("query must not be empty", error_code="INVALID_INPUT")
    # Canonicalize + existence-check + opt-in allowed_roots boundary (G3).
    resolve_user_path(displays_dir, kind="dir", label="displays_dir")

    lookup, channels, context_capped, glob_capped_count = await asyncio.to_thread(
        _run_lookup, displays_dir, cleaned, match, context_cap, windows_paths
    )

    # Live read, capped to one batch (a device prefix matches hundreds-to-thousands of channels;
    # >max_batch_size raises BATCH_TOO_LARGE). THIS cap only samples the live part; the screen list
    # is untouched by it (its own limits are the walk caps above, each with its own note).
    cfg = get_config()
    read = channels[: cfg.max_batch_size]
    live_capped = len(channels) > len(read)
    live: Mapping[str, object]
    try:
        live = await pv_get_batch(list(read), timeout) if read else {"results": [], "errors": []}
    except EpicsError as exc:
        # S27: the provider broke its batch length contract (UPSTREAM_CONTRACT_ERROR). The offline
        # screens are already computed and must survive, degrade the LIVE part rather than sink the
        # whole tool (mirrors the ChannelFinder best-effort handling below; both carry a report
        # note). Not silent: logged AND surfaced as a report note (build_device_report reads live's
        # "note"). BATCH_TOO_LARGE cannot fire here (read is capped above), catches the breach.
        logger.warning("Live read degraded for query %r (provider contract): %s", cleaned, exc)
        live = {
            "results": [],
            "errors": [],
            "note": "Live values unavailable, the EPICS provider returned a malformed batch. "
            "Only the live half failed; the screen list is unaffected by it.",
        }

    # ChannelFinder source-IOC join with a match-aware glob: a substring match need not start with
    # the query, so broaden to ``*stem*``; prefix/exact stay anchored at ``stem*``. The exact-name
    # join in build_device_report filters the (over-broad) fetch. Best-effort, a CF outage must not
    # sink the screens+live result (mirrors the live-read handling above: a provider-contract breach
    # degrades the live part and leaves the screen list untouched; "untouched by THIS failure" is
    # the claim, not "complete", and the walk caps are a separate matter that IS reported, see
    # DeviceLookupReport).
    stem = channel_name(cleaned).rstrip(":")
    glob = f"*{stem}*" if match == "substring" else f"{stem}*"
    try:
        # Typed as the shared service's own result (S29), not as a bare Mapping: that makes the
        # DEGRADATION literal below type-checked, where a misspelled key used to be invisible.
        iocs: ChannelQueryResult = await query_channels(glob)
    except Exception:  # noqa: BLE001 (CF is best-effort: ANY failure degrades, never sinks the tool)
        # S7-6: broaden beyond EpicsError. query_channels maps ChannelFinder errors to
        # EpicsConnectionError, but an unexpected client/projection bug (a non-EpicsError) must also
        # degrade to "unreachable" rather than propagate and sink the screens+live result.
        iocs = {
            "enabled": True,
            "channels": [],
            "note": "ChannelFinder unreachable, source IOC not resolved.",
        }

    report = build_device_report(
        lookup,
        live,
        iocs,
        total_matched=len(channels),
        live_read=len(read),
        live_capped=live_capped,
        channelfinder_enabled=bool(iocs.get("enabled")),
        context_capped=context_capped,
        glob_capped_count=glob_capped_count,
    )
    return {"report": report.model_dump(mode="json"), "markdown": render_markdown(report)}
