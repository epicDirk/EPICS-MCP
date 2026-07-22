"""Tool function for the cross-plane coverage audit (Display ↔ ChannelFinder ↔ Archiver ↔ Alarm).

Read-only join of the Wedge-0 display-PV index (``PV → [screens]``, via the SHA-pinned
``opi_navigation`` inventory) with the runtime planes: ChannelFinder (delivered PVs), the Archiver
Appliance, and the Phoebus Alarm config. Pure file I/O + optional read-only HTTP GETs; no running
IOC and no PV writes.

Thin MCP adapter: the audit orchestration lives in
:func:`epics_pv_mcp.services.orchestration.build_coverage_report` (shared verbatim with the
``epics-coverage`` CLI). This wrapper only builds the request, offloads the blocking work to a
thread, and serializes the report + its Markdown rendering.

``displays_dir`` is the project/dataset ROOT (the inventory binds display macros via the operator
top-levels there). *scope* narrows both the ChannelFinder query and the display set; the runtime
checkers (CF/Archiver/Alarm) are built ONLY when their plane is requested AND its ``*_URL`` is set —
otherwise that plane is withheld with an honest note. See :mod:`epics_pv_mcp.services.coverage`.
"""

from __future__ import annotations

import asyncio

from epics_pv_mcp.services.coverage import render_markdown
from epics_pv_mcp.services.inventory_adapter import DEFAULT_PV_CONTEXT_CAP
from epics_pv_mcp.services.orchestration import CoverageRequest, build_coverage_report


async def _coverage_audit(
    displays_dir: str,
    scope: str = "",
    query_channelfinder: bool = False,
    query_archiver: bool = False,
    query_alarm: bool = False,
    alarm_config: str | None = None,
    context_cap: int = DEFAULT_PV_CONTEXT_CAP,
    windows_paths: bool = False,
) -> dict[str, object]:
    """Cross-plane coverage audit: which delivered PV has no display/archive/alarm — and back.

    Read-only. *displays_dir* is the project/dataset ROOT (the inventory binds macros via the
    operator top-levels there). *scope* is a record-name prefix narrowing both the CF query and the
    display set; ``""`` audits the whole site (the CF query then hits the cap — sandbox/small-scope
    only). *query_channelfinder* is the anchor (needs its URL); without it no
    coverage verdict is possible, only the raw display set. *query_archiver*/*query_alarm* add the
    archive/alarm planes (need their ``*_URL``); each missing URL withholds that plane with a note.
    *alarm_config* is the alarm tree name — REQUIRED when the alarm plane is active (requested AND
    its URL set); there is no default (the trees are site-specific), so opting into the alarm plane
    without naming a tree is a loud INVALID_INPUT rather than a silent scan of a guessed tree.
    *context_cap*/*windows_paths* tune the PV-inventory (higher cap = more complete, slower).

    Returns ``{"report": <CoverageReport JSON>, "markdown": <rendered report>}``.
    Raises :class:`EpicsError` (``INVALID_INPUT``) when *displays_dir* does not exist.
    """
    request = CoverageRequest(
        displays_dir=displays_dir,
        scope=scope,
        query_channelfinder=query_channelfinder,
        query_archiver=query_archiver,
        query_alarm=query_alarm,
        alarm_config=alarm_config,
        context_cap=context_cap,
        windows_paths=windows_paths,
    )
    report = await asyncio.to_thread(build_coverage_report, request)
    return {"report": report.model_dump(mode="json"), "markdown": render_markdown(report)}
