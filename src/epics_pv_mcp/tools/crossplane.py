"""Tool function for the cross-plane PV provenance check (Display ↔ e3 IOC ↔ Naming).

Read-only join of three planes opi-foundry owns separately: the **macro-expanded, per-instance**
PVs a ``.bob`` project references (via the SHA-pinned ``opi_navigation`` Wedge-0 inventory), the
device prefix an e3 IOC ``st.cmd`` declares, and (optionally) the ESS Naming Service registration
status. Pure file I/O + one optional read-only HTTP ``GET``; no running IOC and no PV writes.

Thin MCP adapter: the join orchestration lives in
:func:`epics_pv_mcp.services.orchestration.run_crossplane` (shared verbatim with the
``epics-crossplane`` CLI, so the two can no longer drift). This wrapper only builds the request,
offloads the blocking work to a thread so the async tool stays non-blocking, and serializes the
report + its Markdown rendering.

``displays_dir`` is the project/dataset ROOT: the inventory binds display macros via the operator
top-levels found there, so a too-narrow per-IOC subdirectory leaves PVs unresolved. Display PVs the
inventory cannot resolve to a concrete channel are bucketed as *indeterminate* (dynamic/unresolved)
and never judged "broken"; non-channel protocols (loc/sim/sys/other) are excluded from the join.
See :mod:`epics_pv_mcp.services.crossplane`.
"""

from __future__ import annotations

import asyncio

from epics_pv_mcp.services.crossplane import render_markdown
from epics_pv_mcp.services.inventory_adapter import DEFAULT_PV_CONTEXT_CAP
from epics_pv_mcp.services.orchestration import CrossPlaneRequest, run_crossplane


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
    request = CrossPlaneRequest(
        displays_dir=displays_dir,
        st_cmd_path=st_cmd_path,
        query_naming=query_naming,
        query_channelfinder=query_channelfinder,
        context_cap=context_cap,
        windows_paths=windows_paths,
        module_db_root=module_db_root,
    )
    report = await asyncio.to_thread(run_crossplane, request)
    return {"report": report.model_dump(mode="json"), "markdown": render_markdown(report)}
