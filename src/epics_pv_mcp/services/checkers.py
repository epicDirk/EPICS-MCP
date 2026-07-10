"""The services layer's single downward edge to the four REST planes (M8/M9/C2).

This module is the one place higher layers reach the ChannelFinder / Archiver / Alarm / Naming
REST clients. It carries two kinds of edge:

1. **Injected protocol checkers + factories** (M8) — the pure cross-plane cores
   (:mod:`~.crossplane`, :mod:`~.coverage`) define the checker *Protocols* and receive concrete
   checkers as parameters. Each adapter translates its REST client's errors into ``RuntimeError``
   (a truncated ChannelFinder registry into :class:`~.crossplane.CFRegistryCapped`) so the pure
   cores can withhold a verdict without importing a client or catching broad exceptions.

2. **Per-plane async query functions** (M9) — :func:`query_archived`, :func:`query_alarm_configured`
   and :func:`query_channels` hold the config-gate (``*_URL`` set?) + off-loop
   (:func:`asyncio.to_thread`) + error-translation logic that used to live in ``tools/*``. The MCP
   tool wrappers **and** :mod:`~.diagnose` now call the SAME service function, so ``diagnose`` no
   longer imports upward from the tool layer (the ``service → tool`` inversion is gone).

Both kinds used to sit in the *tool* layer and were imported cross-layer (CLI → tools, tool →
tool, service → tool). They live here now, in the services layer, with public names, so tools,
CLIs and services import downward from one expected place (and the 5th REST plane has an obvious
home for its adapter, factory and query).
"""

from __future__ import annotations

import asyncio

from epics_pv_mcp.config import get_config
from epics_pv_mcp.errors import EpicsConnectionError
from epics_pv_mcp.services.alarm_client import DEFAULT_ALARM_CONFIG, AlarmClient
from epics_pv_mcp.services.alarm_exceptions import AlarmError
from epics_pv_mcp.services.archiver_client import ArchiverClient
from epics_pv_mcp.services.archiver_exceptions import ArchiverError
from epics_pv_mcp.services.channelfinder_client import DEFAULT_MAX_RESULTS, ChannelFinderClient
from epics_pv_mcp.services.channelfinder_exceptions import ChannelFinderError
from epics_pv_mcp.services.coverage import AlarmChecker, ArchivedChecker
from epics_pv_mcp.services.crossplane import CFRegistryCapped, ChannelFinderChecker
from epics_pv_mcp.services.naming_client import NamingServiceClient
from epics_pv_mcp.services.naming_exceptions import NamingServiceError
from epics_pv_mcp.services.olog_client import DEFAULT_MAX_LOGS, OlogClient
from epics_pv_mcp.services.olog_exceptions import OlogError


class CFRegistryChecker:
    """Edge adapter: the channel names ChannelFinder registers under an IOC prefix.

    Implements the core's :class:`~.crossplane.ChannelFinderChecker` Protocol. Translates
    ChannelFinder/network errors into ``RuntimeError`` (and a truncated result into
    :class:`~.crossplane.CFRegistryCapped`) so the pure core can withhold cf_unregistered
    without importing the ChannelFinder client or catching broad exceptions. Counts **all**
    registered channels regardless of ``pvStatus`` — a momentarily Inactive-but-present channel
    (recsync ``cleanOnStart``/reannounce lag) must NOT be flagged unregistered, which would
    violate the "never false-flag" contract.
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


class ArchiverChecker:
    """Edge adapter: per-PV 'is this PV archived?' (Archiver MGMT getPVStatus). One reused client.

    Implements the core's :class:`~.coverage.ArchivedChecker` Protocol. Translates Archiver errors
    into ``RuntimeError`` so the pure core can withhold the per-PV cell (never ``no``) without
    importing the Archiver client or catching broad exceptions.
    """

    def __init__(self, url: str, auth: str | None, timeout: float = 5.0) -> None:
        self._client = ArchiverClient(url, timeout=timeout, auth_header=auth)

    def is_archived(self, pv: str) -> bool:
        try:
            archived, _status = self._client.is_archived(pv)
            return archived
        except ArchiverError as exc:
            raise RuntimeError(f"Archiver query failed: {exc}") from exc


class AlarmConfigChecker:
    """Edge adapter: per-PV 'does this PV have an alarm config?' (Alarm Logger config search).

    Implements the core's :class:`~.coverage.AlarmChecker` Protocol. Translates Alarm errors into
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


def build_cf_checker(query_channelfinder: bool) -> ChannelFinderChecker | None:
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
    return CFRegistryChecker(
        cfg.channelfinder_url,
        cfg.channelfinder_auth or None,
        max_results=cfg.channelfinder_max_results,
    )


def build_naming_client(query_naming: bool, timeout: float = 5.0) -> NamingServiceClient | None:
    """Build the ESS Naming-Service client iff requested AND a URL is configured.

    Mirrors :func:`build_cf_checker` and the diagnose gate: requested but ``naming_url`` unset →
    ``None`` (no client, no egress). The client carries no hard-coded ESS prod default, so
    crossplane_check never reaches ESS production naming unless ``EPICS_MCP_NAMING_URL`` is set.

    *timeout* is forwarded to the client (DS-2): the ``lookup_device_name`` tool passes its
    per-call timeout here, so a slow-but-reachable service over the ESS VPN honours it instead of
    silently falling back to the 5 s default. The default keeps existing positional callers
    (``orchestration`` crossplane path) unchanged.
    """
    if not query_naming:
        return None
    cfg = get_config()
    if not cfg.naming_url:
        return None
    return NamingServiceClient(base_url=cfg.naming_url, timeout=timeout)


def build_archiver_checker(query_archiver: bool) -> ArchivedChecker | None:
    """Build the Archiver checker iff requested AND ``EPICS_MCP_ARCHIVER_URL`` set (else None)."""
    if not query_archiver:
        return None
    cfg = get_config()
    if not cfg.archiver_url:
        return None
    return ArchiverChecker(cfg.archiver_url, cfg.archiver_auth or None)


def build_alarm_checker(query_alarm: bool, alarm_config: str) -> AlarmChecker | None:
    """Build the Alarm checker iff requested AND ``EPICS_MCP_ALARM_URL`` is set (else None)."""
    if not query_alarm:
        return None
    cfg = get_config()
    if not cfg.alarm_url:
        return None
    return AlarmConfigChecker(cfg.alarm_url, cfg.alarm_auth or None, config_name=alarm_config)


# ---------------------------------------------------------------------------
# Per-plane async query functions (M9) — the config-gate + off-loop + error-translation logic
# lifted out of the tool layer. The MCP tool wrappers AND diagnose() call these SAME functions,
# so services/diagnose no longer imports upward from tools/*. Each returns the exact
# tool-facing dict shape it did before the lift (lossless — the tool wrappers are now thin).
# ---------------------------------------------------------------------------

#: Archiver disabled note (mirrored in tools/archiver._get_pv_history, which is tool-only).
_ARCHIVER_DISABLED_NOTE = (
    "Archiver Appliance is disabled. Set EPICS_MCP_ARCHIVER_URL to the appliance root "
    "(e.g. http://archiver:17665)."
)
_ALARM_DISABLED_NOTE = (
    "Phoebus Alarm Logger is disabled. Set EPICS_MCP_ALARM_URL to the logger REST root "
    "(e.g. http://localhost:8081)."
)
_CF_DISABLED_NOTE = (
    "ChannelFinder is disabled. Set EPICS_MCP_CHANNELFINDER_URL to the "
    "ChannelFinder service root (e.g. http://host:8080/ChannelFinder)."
)
_NAMING_DISABLED_NOTE = (
    "ESS Naming Service is disabled. Set EPICS_MCP_NAMING_URL to the naming service root "
    "(e.g. http://naming.example) — WITHOUT a trailing /rest — to enable device-name lookup."
)
_OLOG_DISABLED_NOTE = (
    "Phoebus Olog is disabled. Set EPICS_MCP_OLOG_URL to the Olog REST root "
    "(e.g. http://host:8080/Olog) to enable logbook search."
)


async def query_archived(pv: str, timeout: float = 5.0) -> dict[str, object]:
    """Report whether *pv* is being archived (Archiver MGMT getPVStatus). Read-only, config-gated.

    Default-disabled: with ``EPICS_MCP_ARCHIVER_URL`` unset, returns a structured ``enabled: false``
    result and makes NO network call (preserves localhost isolation). Shared by the ``is_archived``
    tool and the diagnose Archiver plane. DS-4A: surfaces the enriched MGMT fields
    (``connection_state``/``last_event``/``is_monitored``/…) alongside ``archived``/``status`` from
    the SAME single ``getPVStatus`` call (diagnose reads only ``archived``; extras are additive).
    """
    cfg = get_config()
    if not cfg.archiver_url:
        return {"enabled": False, "pv": pv, "archived": None, "note": _ARCHIVER_DISABLED_NOTE}

    def _run() -> dict[str, object]:
        client = ArchiverClient(
            cfg.archiver_url, timeout=timeout, auth_header=cfg.archiver_auth or None
        )
        return {"enabled": True, "pv": pv, **client.get_archive_status(pv)}

    try:
        return await asyncio.to_thread(_run)
    except ArchiverError as exc:
        raise EpicsConnectionError(f"Archiver: {exc}") from exc


async def query_alarm_configured(
    pv: str,
    config_name: str = DEFAULT_ALARM_CONFIG,
    timeout: float = 5.0,
) -> dict[str, object]:
    """Report whether *pv* has an alarm configuration (Alarm Logger /search/alarm/config).

    Default-disabled: with ``EPICS_MCP_ALARM_URL`` unset, returns ``enabled: false`` and makes no
    network call. Shared by the ``is_alarm_configured`` tool and the diagnose Alarm plane.
    """
    cfg = get_config()
    if not cfg.alarm_url:
        return {"enabled": False, "pv": pv, "configured": None, "note": _ALARM_DISABLED_NOTE}

    def _run() -> dict[str, object]:
        client = AlarmClient(cfg.alarm_url, timeout=timeout, auth_header=cfg.alarm_auth or None)
        configured, detail = client.is_alarm_configured(pv, config_name=config_name)
        return {
            "enabled": True,
            "pv": pv,
            "config": config_name,
            "configured": configured,
            "detail": detail,
        }

    try:
        return await asyncio.to_thread(_run)
    except AlarmError as exc:
        raise EpicsConnectionError(f"Alarm Logger: {exc}") from exc


async def query_alarm_history(
    pv: str,
    start: str,
    end: str,
    max_events: int = 100,
    timeout: float = 5.0,
) -> dict[str, object]:
    """Report the alarm history of *pv* over ``[start, end]`` (Alarm Logger ``/search/alarm``).

    Default-disabled: with ``EPICS_MCP_ALARM_URL`` unset, returns ``enabled: false`` and makes no
    network call. *start*/*end* are required (a defaultless query must not pull the whole history).
    Events are newest-first and projected onto the technical allowlist — the raw doc's
    ``user``/``host``/``command`` are stripped (DS-PRIVACY). ``capped`` is True when more than
    *max_events* matched (the newest are kept). Shared by the ``get_alarm_history`` tool.
    """
    cfg = get_config()
    if not cfg.alarm_url:
        return {"enabled": False, "pv": pv, "events": [], "note": _ALARM_DISABLED_NOTE}

    def _run() -> dict[str, object]:
        client = AlarmClient(cfg.alarm_url, timeout=timeout, auth_header=cfg.alarm_auth or None)
        events, capped = client.get_alarm_history(pv, start, end, max_events)
        return {
            "enabled": True,
            "pv": pv,
            "start": start,
            "end": end,
            "events": events,
            "total": len(events),
            "capped": capped,
        }

    try:
        return await asyncio.to_thread(_run)
    except AlarmError as exc:
        raise EpicsConnectionError(f"Alarm Logger: {exc}") from exc


async def query_channels(
    name_pattern: str,
    max_results: int = DEFAULT_MAX_RESULTS,
    timeout: float = 5.0,
) -> dict[str, object]:
    """Query ChannelFinder for channels whose name matches *name_pattern* (glob ``*``/``?``).

    Default-disabled: with ``EPICS_MCP_CHANNELFINDER_URL`` unset, returns ``enabled: false`` and
    makes no network call. Shared by the ``find_channels`` tool, ``find_device`` and the diagnose
    ChannelFinder plane.
    """
    cfg = get_config()
    if not cfg.channelfinder_url:
        return {"enabled": False, "channels": [], "total": 0, "note": _CF_DISABLED_NOTE}

    def _run() -> dict[str, object]:
        client = ChannelFinderClient(
            cfg.channelfinder_url,
            timeout=timeout,
            auth_header=cfg.channelfinder_auth or None,
        )
        # S8-6: fetch one MORE than the cap and truncate, so ``capped`` is an honest
        # ``fetched > max_results`` (the Archiver pattern) rather than ``>= max_results``, which
        # false-flags exactly ``max_results`` real channels as truncated (an off-by-one).
        fetched = client.find_channels(name_pattern, max_results=max_results + 1)
        capped = len(fetched) > max_results
        channels = fetched[:max_results]
        return {
            "enabled": True,
            "channels": [dict(channel) for channel in channels],
            "total": len(channels),
            "capped": capped,
        }

    try:
        return await asyncio.to_thread(_run)
    except ChannelFinderError as exc:
        raise EpicsConnectionError(f"ChannelFinder: {exc}") from exc


async def query_naming_lookup(name: str, timeout: float = 5.0) -> dict[str, object]:
    """Look up an ESS device name in the Naming Service: is it registered + ACTIVE?

    Read-only, config-gated. Default-disabled: with ``EPICS_MCP_NAMING_URL`` unset, returns a
    structured ``enabled: false`` result and makes NO network call (no ESS egress). Backs the
    standalone ``lookup_device_name`` tool — the one naming plane that had no ``query_*`` sibling
    (it was only reachable indirectly via ``diagnose_connection``/``crossplane_check``).

    Answer semantics (DS-2 — a service error must NEVER masquerade as "not registered"):

    * **Reachable + registered** → ``registered: true`` with ``status`` (ACTIVE) and ``message``.
    * **Reachable + not registered** (a genuine 404 on ``deviceNames``) → ``registered: false`` — a
      DEFINITIVE answer.
    * **Reachable + OBSOLETE/DELETED/unknown** → ``registered: false`` with the status preserved.
    * **Unreachable / 5xx / bad JSON / timeout** → ``registered: null`` + ``withheld: true`` with a
      note; the reachability is probed FIRST (``check_connectivity``, like ``diagnose``) so a
      down/slow service is withheld rather than read as a definitive answer.

    Surfaces only the privacy-safe :class:`NameStatus` projection (``registered/status/message``);
    the richer raw ``DeviceNameElement`` (deviceType/discipline/system/subsystem) is deliberately
    NOT exposed (MVP scope). Shared by the ``lookup_device_name`` tool.
    """
    client = build_naming_client(True, timeout=timeout)
    if client is None:
        return {"enabled": False, "name": name, "registered": None, "note": _NAMING_DISABLED_NOTE}

    def _run() -> dict[str, object]:
        # Probe reachability FIRST (mirrors diagnose._gather_naming) so an unreachable/timing-out
        # service is WITHHELD by the ``except`` below, not read as a definitive answer. A reachable
        # HEAD plus a 404 on deviceNames = the real "not registered" (validate_name returns
        # registered=False); a NON-404 deviceNames failure PROPAGATES out of validate_name (DS-2)
        # and is caught below -> withheld, never a false registered=False.
        client.check_connectivity()
        status = client.validate_name(name)
        return {
            "enabled": True,
            "name": name,
            "registered": status["registered"],
            "status": status["status"],
            "message": status["message"],
        }

    try:
        return await asyncio.to_thread(_run)
    except NamingServiceError as exc:
        # Both check_connectivity (NamingServiceConnectionError) and validate_name
        # (NamingServiceResponseError on a NON-404 failure) raise NamingServiceError subclasses; a
        # genuine 404 is handled INSIDE validate_name (returns registered=False, does not raise).
        # WITHHOLD on any service error — an unexpected bug (non-NamingServiceError) still escapes
        # to the tool shell's translate_epics_errors as an [INTERNAL] ToolError.
        return {
            "enabled": True,
            "name": name,
            "registered": None,
            "withheld": True,
            "note": f"Naming lookup withheld: {exc}",
        }


async def query_olog_search(
    text: str | None = None,
    logbooks: str | None = None,
    tags: str | None = None,
    start: str | None = None,
    end: str | None = None,
    size: int = DEFAULT_MAX_LOGS,
    timeout: float = 5.0,
) -> dict[str, object]:
    """Search the Phoebus Olog logbook, returning DS-PRIVACY-redacted entries. Read-only, gated.

    Default-disabled: with ``EPICS_MCP_OLOG_URL`` unset, returns a structured ``enabled: false``
    result and makes NO network call (no ESS egress). Every returned entry passes the Olog client's
    output allowlist (author/free-text withheld) — raw entries never reach this layer. ``capped`` is
    True when more than *size* matched (the first *size* are kept). Backs ``search_logbook``.
    """
    cfg = get_config()
    if not cfg.olog_url:
        return {"enabled": False, "entries": [], "total": 0, "note": _OLOG_DISABLED_NOTE}

    def _run() -> dict[str, object]:
        client = OlogClient(cfg.olog_url, timeout=timeout, auth_header=cfg.olog_auth or None)
        entries, capped = client.search_logbook(
            text=text, logbooks=logbooks, tags=tags, start=start, end=end, size=size
        )
        return {"enabled": True, "entries": entries, "total": len(entries), "capped": capped}

    try:
        return await asyncio.to_thread(_run)
    except OlogError as exc:
        raise EpicsConnectionError(f"Olog: {exc}") from exc


async def query_olog_entry(log_id: str, timeout: float = 5.0) -> dict[str, object]:
    """Return one Olog entry by id (DS-PRIVACY-redacted). Read-only, config-gated.

    Default-disabled: with ``EPICS_MCP_OLOG_URL`` unset, returns ``enabled: false`` and makes no
    network call. ``found`` is False when the entry does not exist. Backs ``get_log_entry``.
    """
    cfg = get_config()
    if not cfg.olog_url:
        return {"enabled": False, "id": log_id, "found": False, "note": _OLOG_DISABLED_NOTE}

    def _run() -> dict[str, object]:
        client = OlogClient(cfg.olog_url, timeout=timeout, auth_header=cfg.olog_auth or None)
        entry = client.get_log_entry(log_id)
        if entry is None:
            return {"enabled": True, "id": log_id, "found": False}
        return {"enabled": True, "id": log_id, "found": True, "entry": entry}

    try:
        return await asyncio.to_thread(_run)
    except OlogError as exc:
        raise EpicsConnectionError(f"Olog: {exc}") from exc
