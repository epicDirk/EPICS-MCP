"""Tool function for the cross-plane PV provenance check (Display ↔ e3 IOC ↔ Naming).

Read-only join of three planes opi-foundry owns separately: the **macro-expanded, per-instance**
PVs a ``.bob`` project references (via the SHA-pinned ``opi_navigation`` Wedge-0 inventory), the
device prefix an e3 IOC ``st.cmd`` declares, and (optionally) the ESS Naming Service registration
status. Pure file I/O + one optional read-only HTTP ``GET``; no running IOC and no PV writes.
Mirrors the ``epics-crossplane`` CLI as an MCP tool so the join is reachable from an agent.

``displays_dir`` is the project/dataset ROOT: the inventory binds display macros via the operator
top-levels found there, so a too-narrow per-IOC subdirectory leaves PVs unresolved. Display PVs the
inventory cannot resolve to a concrete channel are bucketed as *indeterminate* (dynamic/unresolved)
and never judged "broken"; non-channel protocols (loc/sim/sys/other) are excluded from the join.
See :mod:`epics_pv_mcp.services.crossplane`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from epics_pv_mcp.config import get_config
from epics_pv_mcp.paths import resolve_user_path
from epics_pv_mcp.services.channelfinder_client import DEFAULT_MAX_RESULTS, ChannelFinderClient
from epics_pv_mcp.services.channelfinder_exceptions import ChannelFinderError
from epics_pv_mcp.services.crossplane import (
    CFRegistryCapped,
    ChannelFinderChecker,
    crossplane_check,
    render_markdown,
)
from epics_pv_mcp.services.e3_db import load_ioc_db, parse_st_cmd
from epics_pv_mcp.services.inventory_adapter import DEFAULT_PV_CONTEXT_CAP, analyze_display_pvs
from epics_pv_mcp.services.naming_client import NamingServiceClient


class _CFRegistryChecker:
    """Edge adapter: the channel names ChannelFinder registers under an IOC prefix.

    Implements the core's :class:`ChannelFinderChecker` Protocol. Translates ChannelFinder/network
    errors into ``RuntimeError`` (and a truncated result into ``CFRegistryCapped``) so the pure core
    can withhold cf_unregistered without importing the ChannelFinder client or catching broad
    exceptions. Counts **all** registered channels regardless of ``pvStatus`` — a momentarily
    Inactive-but-present channel (recsync ``cleanOnStart``/reannounce lag) must NOT be flagged
    unregistered, which would violate the "never false-flag" contract.
    """

    def __init__(self, url: str, auth: str | None, max_results: int = DEFAULT_MAX_RESULTS) -> None:
        self._url = url
        self._auth = auth
        self._max_results = max_results

    def registered_under(self, prefix: str) -> set[str]:
        try:
            client = ChannelFinderClient(self._url, auth_header=self._auth)
            channels = client.find_channels(f"{prefix}*", max_results=self._max_results)
            if len(channels) >= self._max_results:
                # Truncated registry — withhold rather than diff a partial set (would false-flag).
                # CFRegistryCapped is a RuntimeError, NOT a ChannelFinderError → not caught below.
                raise CFRegistryCapped(
                    f"ChannelFinder returned >= {self._max_results} channels for '{prefix}*'"
                )
            return {channel["name"] for channel in channels}
        except ChannelFinderError as exc:
            raise RuntimeError(f"ChannelFinder query failed: {exc}") from exc


def _build_cf_checker(query_channelfinder: bool) -> ChannelFinderChecker | None:
    """Build the ChannelFinder checker iff requested AND a URL is configured.

    Returns ``None`` when not requested, or requested but ``channelfinder_url`` is unset — in the
    latter case the caller passes ``cf_requested=True`` so the core emits an honest "skipped — URL
    unset" note (no silent no-op).
    """
    if not query_channelfinder:
        return None
    cfg = get_config()
    if not cfg.channelfinder_url:
        return None
    return _CFRegistryChecker(
        cfg.channelfinder_url,
        cfg.channelfinder_auth or None,
        max_results=cfg.channelfinder_max_results,
    )


def _run_check(
    displays_dir: str,
    st_cmd_path: str,
    query_naming: bool,
    context_cap: int,
    windows_paths: bool,
    module_db_root: str,
    query_channelfinder: bool,
) -> dict[str, object]:
    """Synchronous body of the cross-plane check (run off the event loop in a thread).

    Bundles the blocking work — the macro-aware PV-inventory over ``displays_dir`` (the project
    ROOT), the ``st.cmd`` read, the optional IOC ``.db`` load (when *module_db_root* is given), and
    the optional Naming-Service GET — into one call so the async tool stays non-blocking.
    """
    join_pvs, context_capped, glob_capped_count = analyze_display_pvs(
        Path(displays_dir), context_cap=context_cap, windows_paths=windows_paths
    )
    st_info = parse_st_cmd(Path(st_cmd_path).read_text(encoding="utf-8"))
    naming = NamingServiceClient() if query_naming else None
    # Opt-in IOC .db enumeration: only when a module/db root is given (offline default unchanged).
    # ``complete`` gates the broken verdict — a partial/templated set withholds it (no false alarm).
    ioc_db: tuple[set[str], set[str]] | None = None
    ioc_db_complete = False
    if module_db_root:
        db_result = load_ioc_db(st_info, Path(module_db_root))
        ioc_db = (set(db_result.resolved), set(db_result.unresolved))
        ioc_db_complete = db_result.complete
    channelfinder = _build_cf_checker(query_channelfinder)
    report = crossplane_check(
        join_pvs,
        st_info,
        naming=naming,
        ioc_db=ioc_db,
        ioc_db_complete=ioc_db_complete,
        channelfinder=channelfinder,
        cf_requested=query_channelfinder,
        context_capped=context_capped,
        glob_capped_count=glob_capped_count,
    )
    return {
        "report": report.model_dump(mode="json"),
        "markdown": render_markdown(report),
    }


async def _crossplane_check(
    displays_dir: str,
    st_cmd_path: str,
    query_naming: bool = False,
    query_channelfinder: bool = False,
    context_cap: int = DEFAULT_PV_CONTEXT_CAP,
    windows_paths: bool = False,
    module_db_root: str = "",
) -> dict[str, object]:
    """Join macro-aware display PVs with an e3 IOC ``st.cmd`` (+ optional .db/Naming/CF). Read-only.

    *displays_dir* is the project/dataset ROOT (the inventory binds macros via the operator
    top-levels there — a narrow per-IOC subdirectory under-resolves). *context_cap* bounds the
    per-display reachability contexts (higher = more complete, slower; ~60 s for a large dataset
    like fbis at the default). *windows_paths* resolves embedded ``<file>`` refs case-insensitively
    for a Windows host; default Linux (the ESS-console truth, deterministic). *module_db_root*
    (opt-in) is a local directory holding the IOC's e3 module ``.db`` files: when supplied, concrete
    linked PVs are checked against the loaded set and a ``broken`` verdict is emitted ONLY if that
    set is provably complete (else withheld). Empty (default) = no .db, no ``broken`` verdict.
    *query_channelfinder* (opt-in) checks each concrete linked PV against ChannelFinder and reports
    those not registered as ``cf_unregistered`` (needs ``EPICS_MCP_CHANNELFINDER_URL``; unset → an
    honest "skipped" note, no network call).

    Returns ``{"report": <CrossPlaneReport JSON>, "markdown": <rendered report>}``.
    Raises :class:`EpicsError` (``INVALID_INPUT``) when a path does not exist.
    """
    # Canonicalize + existence-check + opt-in allowed_roots boundary (G3) on every
    # user path before any filesystem walk. module_db_root is optional.
    resolve_user_path(displays_dir, kind="dir", label="displays_dir")
    resolve_user_path(st_cmd_path, kind="file", label="st_cmd_path")
    if module_db_root:
        resolve_user_path(module_db_root, kind="dir", label="module_db_root")
    return await asyncio.to_thread(
        _run_check,
        displays_dir,
        st_cmd_path,
        query_naming,
        context_cap,
        windows_paths,
        module_db_root,
        query_channelfinder,
    )
