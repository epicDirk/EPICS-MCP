"""Tool function for the cross-plane coverage audit (Display ↔ ChannelFinder ↔ Archiver ↔ Alarm).

Read-only join of the Wedge-0 display-PV index (``PV → [screens]``, via the SHA-pinned
``opi_navigation`` inventory) with the runtime planes: ChannelFinder (delivered PVs), the Archiver
Appliance, and the Phoebus Alarm config. Pure file I/O + optional read-only HTTP GETs; no running
IOC and no PV writes. Mirrors the ``epics-coverage`` CLI as an MCP tool.

``displays_dir`` is the project/dataset ROOT (the inventory binds display macros via the operator
top-levels there). *scope* narrows both the ChannelFinder query and the display set; the runtime
checkers (CF/Archiver/Alarm) are built ONLY when their plane is requested AND its ``*_URL`` is set —
otherwise that plane is withheld with an honest note. See :mod:`epics_pv_mcp.services.coverage`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from epics_pv_mcp.config import get_config
from epics_pv_mcp.paths import resolve_user_path
from epics_pv_mcp.services.alarm_client import DEFAULT_ALARM_CONFIG, AlarmClient
from epics_pv_mcp.services.alarm_exceptions import AlarmError
from epics_pv_mcp.services.archiver_client import ArchiverClient
from epics_pv_mcp.services.archiver_exceptions import ArchiverError
from epics_pv_mcp.services.coverage import (
    AlarmChecker,
    ArchivedChecker,
    audit_coverage,
    render_markdown,
)
from epics_pv_mcp.services.inventory_adapter import DEFAULT_PV_CONTEXT_CAP, analyze_display_index
from epics_pv_mcp.tools.crossplane import _build_cf_checker


class _ArchiverChecker:
    """Edge adapter: per-PV 'is this PV archived?' (Archiver MGMT getPVStatus). One reused client.

    Implements the core's :class:`ArchivedChecker` Protocol. Translates Archiver errors into
    ``RuntimeError`` so the pure core can withhold the per-PV cell (never ``no``) without importing
    the Archiver client or catching broad exceptions.
    """

    def __init__(self, url: str, auth: str | None, timeout: float = 5.0) -> None:
        self._client = ArchiverClient(url, timeout=timeout, auth_header=auth)

    def is_archived(self, pv: str) -> bool:
        try:
            archived, _status = self._client.is_archived(pv)
            return archived
        except ArchiverError as exc:
            raise RuntimeError(f"Archiver query failed: {exc}") from exc


class _AlarmChecker:
    """Edge adapter: per-PV 'does this PV have an alarm config?' (Alarm Logger config search).

    Implements the core's :class:`AlarmChecker` Protocol. Translates Alarm errors into
    ``RuntimeError`` so the pure core withholds the per-PV cell (never ``no``) on a query failure.
    """

    def __init__(
        self,
        url: str,
        auth: str | None,
        config_name: str = DEFAULT_ALARM_CONFIG,
        timeout: float = 5.0,
    ) -> None:
        self._client = AlarmClient(url, timeout=timeout, auth_header=auth)
        self._config_name = config_name

    def is_alarm_configured(self, pv: str) -> bool:
        try:
            configured, _detail = self._client.is_alarm_configured(
                pv, config_name=self._config_name
            )
            return configured
        except AlarmError as exc:
            raise RuntimeError(f"Alarm query failed: {exc}") from exc


def _build_archiver_checker(query_archiver: bool) -> ArchivedChecker | None:
    """Build the Archiver checker iff requested AND ``EPICS_MCP_ARCHIVER_URL`` set (else None)."""
    if not query_archiver:
        return None
    cfg = get_config()
    if not cfg.archiver_url:
        return None
    return _ArchiverChecker(cfg.archiver_url, cfg.archiver_auth or None)


def _build_alarm_checker(query_alarm: bool, alarm_config: str) -> AlarmChecker | None:
    """Build the Alarm checker iff requested AND ``EPICS_MCP_ALARM_URL`` is set (else None)."""
    if not query_alarm:
        return None
    cfg = get_config()
    if not cfg.alarm_url:
        return None
    return _AlarmChecker(cfg.alarm_url, cfg.alarm_auth or None, config_name=alarm_config)


def _run_audit(
    displays_dir: str,
    scope: str,
    query_channelfinder: bool,
    query_archiver: bool,
    query_alarm: bool,
    alarm_config: str,
    context_cap: int,
    windows_paths: bool,
) -> dict[str, object]:
    """Synchronous body of the coverage audit (run off the event loop in a thread)."""
    index_rows, context_capped, glob_capped_count = analyze_display_index(
        Path(displays_dir), context_cap=context_cap, windows_paths=windows_paths
    )
    channelfinder = _build_cf_checker(query_channelfinder)
    archived = _build_archiver_checker(query_archiver)
    alarmed = _build_alarm_checker(query_alarm, alarm_config)
    report = audit_coverage(
        index_rows,
        scope=scope,
        channelfinder=channelfinder,
        cf_requested=query_channelfinder,
        archived=archived,
        archive_requested=query_archiver,
        alarmed=alarmed,
        alarm_requested=query_alarm,
        context_capped=context_capped,
        glob_capped_count=glob_capped_count,
    )
    return {
        "report": report.model_dump(mode="json"),
        "markdown": render_markdown(report),
    }


async def _coverage_audit(
    displays_dir: str,
    scope: str = "",
    query_channelfinder: bool = False,
    query_archiver: bool = False,
    query_alarm: bool = False,
    alarm_config: str = DEFAULT_ALARM_CONFIG,
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
    *alarm_config* is the alarm tree name (default ``Accelerator``). *context_cap*/*windows_paths*
    tune the PV-inventory (higher cap = more complete, slower; default Linux path resolution).

    Returns ``{"report": <CoverageReport JSON>, "markdown": <rendered report>}``.
    Raises :class:`EpicsError` (``INVALID_INPUT``) when *displays_dir* does not exist.
    """
    resolve_user_path(displays_dir, kind="dir", label="displays_dir")
    return await asyncio.to_thread(
        _run_audit,
        displays_dir,
        scope,
        query_channelfinder,
        query_archiver,
        query_alarm,
        alarm_config,
        context_cap,
        windows_paths,
    )
