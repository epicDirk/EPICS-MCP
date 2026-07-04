"""Edge adapters + factories that build the injected protocol checkers (M8/C2).

The pure cross-plane cores (:mod:`~.crossplane`, :mod:`~.coverage`) define the checker
*Protocols* and receive concrete checkers as parameters. Those concrete adapters and
their config-gated factories used to live in the *tool* layer and were imported cross-layer
by the CLIs and by one tool from another — a layering inversion. They live here now, in the
services layer, with public names, so tools **and** CLIs import downward from one expected
place (and the 5th REST plane's adapter has an obvious home).

Each adapter translates its REST client's errors into ``RuntimeError`` (a truncated
ChannelFinder registry into :class:`~.crossplane.CFRegistryCapped`) so the pure cores can
withhold a verdict without importing a client or catching broad exceptions.
"""

from __future__ import annotations

from epics_pv_mcp.config import get_config
from epics_pv_mcp.services.alarm_client import DEFAULT_ALARM_CONFIG, AlarmClient
from epics_pv_mcp.services.alarm_exceptions import AlarmError
from epics_pv_mcp.services.archiver_client import ArchiverClient
from epics_pv_mcp.services.archiver_exceptions import ArchiverError
from epics_pv_mcp.services.channelfinder_client import DEFAULT_MAX_RESULTS, ChannelFinderClient
from epics_pv_mcp.services.channelfinder_exceptions import ChannelFinderError
from epics_pv_mcp.services.coverage import AlarmChecker, ArchivedChecker
from epics_pv_mcp.services.crossplane import CFRegistryCapped, ChannelFinderChecker
from epics_pv_mcp.services.naming_client import NamingServiceClient


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


def build_naming_client(query_naming: bool) -> NamingServiceClient | None:
    """Build the ESS Naming-Service client iff requested AND a URL is configured.

    Mirrors :func:`build_cf_checker` and the diagnose gate: requested but ``naming_url`` unset →
    ``None`` (no client, no egress). The client carries no hard-coded ESS prod default, so
    crossplane_check never reaches ESS production naming unless ``EPICS_MCP_NAMING_URL`` is set.
    """
    if not query_naming:
        return None
    cfg = get_config()
    if not cfg.naming_url:
        return None
    return NamingServiceClient(base_url=cfg.naming_url)


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
