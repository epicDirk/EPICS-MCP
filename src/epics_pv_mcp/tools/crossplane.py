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

from epics_pv_mcp.errors import EpicsError
from epics_pv_mcp.services.crossplane import crossplane_check, render_markdown
from epics_pv_mcp.services.e3_db import parse_st_cmd
from epics_pv_mcp.services.inventory_adapter import DEFAULT_PV_CONTEXT_CAP, analyze_display_pvs
from epics_pv_mcp.services.naming_client import NamingServiceClient


def _run_check(
    displays_dir: str,
    st_cmd_path: str,
    query_naming: bool,
    context_cap: int,
    windows_paths: bool,
) -> dict[str, object]:
    """Synchronous body of the cross-plane check (run off the event loop in a thread).

    Bundles the blocking work — the macro-aware PV-inventory over ``displays_dir`` (the project
    ROOT), the ``st.cmd`` read, and the optional Naming-Service GET — into one call so the async
    tool stays non-blocking.
    """
    join_pvs, context_capped, glob_capped_count = analyze_display_pvs(
        Path(displays_dir), context_cap=context_cap, windows_paths=windows_paths
    )
    st_info = parse_st_cmd(Path(st_cmd_path).read_text(encoding="utf-8"))
    naming = NamingServiceClient() if query_naming else None
    report = crossplane_check(
        join_pvs,
        st_info,
        naming=naming,
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
    context_cap: int = DEFAULT_PV_CONTEXT_CAP,
    windows_paths: bool = False,
) -> dict[str, object]:
    """Join macro-aware display PVs with an e3 IOC ``st.cmd`` (+ optional Naming). Read-only.

    *displays_dir* is the project/dataset ROOT (the inventory binds macros via the operator
    top-levels there — a narrow per-IOC subdirectory under-resolves). *context_cap* bounds the
    per-display reachability contexts (higher = more complete, slower; ~60 s for a large dataset
    like fbis at the default). *windows_paths* resolves embedded ``<file>`` refs case-insensitively
    for a Windows host; default Linux (the ESS-console truth, deterministic).

    Returns ``{"report": <CrossPlaneReport JSON>, "markdown": <rendered report>}``.
    Raises :class:`EpicsError` (``INVALID_INPUT``) when a path does not exist.
    """
    displays = Path(displays_dir)
    st_cmd = Path(st_cmd_path)
    if not displays.is_dir():
        raise EpicsError(
            f"displays_dir is not a directory: {displays_dir}",
            error_code="INVALID_INPUT",
        )
    if not st_cmd.is_file():
        raise EpicsError(
            f"st_cmd_path is not a file: {st_cmd_path}",
            error_code="INVALID_INPUT",
        )
    return await asyncio.to_thread(
        _run_check, displays_dir, st_cmd_path, query_naming, context_cap, windows_paths
    )
