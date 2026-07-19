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
import base64
import uuid
from collections.abc import Callable

from epics_pv_mcp.config import get_config
from epics_pv_mcp.errors import EpicsConnectionError, EpicsError, OlogWriteDeniedError
from epics_pv_mcp.olog_safety import get_olog_safety
from epics_pv_mcp.services._http import basic_auth_header, http_status
from epics_pv_mcp.services._time_window import TimeWindowFormatError
from epics_pv_mcp.services.alarm_client import DEFAULT_ALARM_CONFIG, AlarmClient
from epics_pv_mcp.services.alarm_exceptions import AlarmConnectionError, AlarmError
from epics_pv_mcp.services.archiver_client import ArchiverClient
from epics_pv_mcp.services.archiver_exceptions import ArchiverConnectionError, ArchiverError
from epics_pv_mcp.services.channelfinder_client import DEFAULT_MAX_RESULTS, ChannelFinderClient
from epics_pv_mcp.services.channelfinder_exceptions import (
    ChannelFinderConnectionError,
    ChannelFinderError,
)
from epics_pv_mcp.services.coverage import AlarmChecker, ArchivedChecker
from epics_pv_mcp.services.crossplane import CFRegistryCapped, ChannelFinderChecker
from epics_pv_mcp.services.naming_client import NamingServiceClient
from epics_pv_mcp.services.naming_exceptions import NamingServiceError
from epics_pv_mcp.services.olog_attachments import (
    plan_attachments,
    read_uploads,
    write_download,
)
from epics_pv_mcp.services.olog_client import DEFAULT_MAX_LOGS, OlogClient
from epics_pv_mcp.services.olog_exceptions import OlogConnectionError, OlogError, OlogResponseError


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
        except AlarmError as exc:
            raise RuntimeError(f"Alarm query failed: {exc}") from exc
        if configured is None:
            # Tree produced nothing → the cell is unknown, not `no` (withheld != no).
            raise RuntimeError(
                f"Alarm tree {self._config_name!r} returned no configuration at all — "
                "unknown or misspelled tree name, or an empty tree"
            )
        return configured


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
# The download/list posture message: raw attachment bytes (and filenames, for a list) are author
# free text and bypass the dict-based redaction, so they leave ONLY from a declared local sandbox
# with the explicit opt-in — the same whole-mode threshold as an entry read, plus a second flag.
_ATTACH_WITHHELD_NOTE = (
    "Attachment bytes are withheld. Raw bytes leave only from a declared local test sandbox "
    "(loopback EPICS_MCP_OLOG_URL + EPICS_MCP_OLOG_ASSUME_TEST_DATA) AND with "
    "EPICS_MCP_OLOG_ALLOW_ATTACHMENT_DOWNLOAD=true (bytes bypass the entry redaction, so they need "
    "their own opt-in; the by-id endpoint has no server-side per-log authorization). Run "
    "epics-doctor to see the effective posture."
)
_ATTACH_LIST_WITHHELD_NOTE = (
    "Attachment filenames are withheld (author free text). Only the count is shown unless the Olog "
    "is a declared local test sandbox (loopback + EPICS_MCP_OLOG_ASSUME_TEST_DATA)."
)
# A base64 download is returned IN the tool result (response tokens), so it is capped far below the
# path-based ceiling (olog_attach_max_bytes) — a large blob must go to a workspace file, not the
# model context. The effective base64 cap is min(this, olog_attach_max_bytes).
_BASE64_DOWNLOAD_MAX_BYTES = 5 * 1024 * 1024


def _default_olog_id_factory() -> str:
    """A fresh attachment UUID. Injected into :func:`query_olog_create` so the upload logic is
    deterministic in tests (the project's no-``uuid``-in-logic rule); production uses this
    default."""
    return str(uuid.uuid4())


# Withheld (configured=None), NOT a negative: the tree yielded no configuration at all, so
# "this PV is not configured" cannot be told apart from "that is not the tree name". The name is
# case-sensitive in the query even though it is lower-cased to pick the index — see
# AlarmClient.is_alarm_configured.
_ALARM_TREE_UNKNOWN_NOTE = (
    "Alarm config tree {config!r} returned no configuration at all — unknown or misspelled tree "
    "name (it is CASE-SENSITIVE here), or an empty tree. Answer withheld rather than reported as "
    "'not configured'."
)


def _alarm_error_code(exc: AlarmError) -> str:
    """A discrete error code for an Alarm-Logger failure whose SERVER answered.

    The served HTTP status when it is readable, else a generic token — the sibling of
    :func:`_archiver_error_code` (see there for why these stay per-plane instead of one
    generalized helper).
    """
    status = http_status(exc)
    return f"ALARM_HTTP_{status}" if status is not None else "ALARM_RESPONSE_ERROR"


def _channelfinder_error_code(exc: ChannelFinderError) -> str:
    """A discrete error code for a ChannelFinder failure whose SERVER answered (sibling of
    :func:`_archiver_error_code`)."""
    status = http_status(exc)
    return f"CHANNELFINDER_HTTP_{status}" if status is not None else "CHANNELFINDER_RESPONSE_ERROR"


def _archiver_error_code(exc: ArchiverError) -> str:
    """A discrete error code for an Archiver failure whose SERVER answered.

    The served HTTP status when it is readable, else a generic token. Lives in the services layer
    (like its sibling :func:`_olog_error_code`) so ``tools/archiver`` imports it DOWNWARD — the
    ``services → tools`` direction is what the M9 lift exists to prevent.

    Kept separate from ``_olog_error_code`` rather than generalized: that one also feeds the Olog
    write AUDIT (which must stay metadata-only, SEC-5) and carries an EpicsError branch for the
    write gate, so folding them together would put that audit in the blast radius of an unrelated
    fix — for six lines of status-to-token boilerplate.

    Note: a retry-exhausted 502/503/504 arrives as ``requests.RetryError``, which is NOT a
    ConnectionError — so it lands here as an ArchiverResponseError with no readable status and maps
    to ARCHIVER_RESPONSE_ERROR. That is correct: the host DID answer, repeatedly.
    """
    status = http_status(exc)
    return f"ARCHIVER_HTTP_{status}" if status is not None else "ARCHIVER_RESPONSE_ERROR"


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
    except ArchiverConnectionError as exc:
        raise EpicsConnectionError(f"Archiver: {exc}") from exc
    except ArchiverError as exc:
        # The server ANSWERED (a served 4xx/5xx) — that is not an outage. Safe for diagnose, which
        # catches Exception totally here and withholds either way (diagnose._gather_archiver).
        raise EpicsError(f"Archiver: {exc}", error_code=_archiver_error_code(exc)) from exc


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
        result: dict[str, object] = {
            "enabled": True,
            "pv": pv,
            "config": config_name,
            "configured": configured,
            "detail": detail,
        }
        if configured is None:
            result["note"] = _ALARM_TREE_UNKNOWN_NOTE.format(config=config_name)
        return result

    try:
        return await asyncio.to_thread(_run)
    except AlarmConnectionError as exc:
        raise EpicsConnectionError(f"Alarm Logger: {exc}") from exc
    except AlarmError as exc:
        # S11 §8: the server ANSWERED (a served 4xx/5xx or an unreadable 2xx) — relabelling that
        # "cannot reach the Alarm Logger" sends the caller after an outage that is not happening.
        raise EpicsError(f"Alarm Logger: {exc}", error_code=_alarm_error_code(exc)) from exc


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
    except TimeWindowFormatError as exc:
        # Nothing was sent: the window itself is unusable. Reporting it as "cannot reach the
        # Alarm Logger" would send the caller after an outage that is not happening.
        raise EpicsError(f"Alarm Logger: {exc}", error_code="INVALID_TIME_WINDOW") from exc
    except AlarmConnectionError as exc:
        raise EpicsConnectionError(f"Alarm Logger: {exc}") from exc
    except AlarmError as exc:
        # S11 §8: the server ANSWERED — an unreadable payload is not an outage.
        raise EpicsError(f"Alarm Logger: {exc}", error_code=_alarm_error_code(exc)) from exc


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
    except ChannelFinderConnectionError as exc:
        raise EpicsConnectionError(f"ChannelFinder: {exc}") from exc
    except ChannelFinderError as exc:
        # S11 §8: the server ANSWERED — an unreadable payload is not an outage.
        raise EpicsError(
            f"ChannelFinder: {exc}", error_code=_channelfinder_error_code(exc)
        ) from exc


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
    offset: int = 0,
    sort: str = "down",
    timeout: float = 5.0,
) -> dict[str, object]:
    """Search the Phoebus Olog logbook. Read-only, gated.

    Default-disabled: with ``EPICS_MCP_OLOG_URL`` unset, returns a structured ``enabled: false``
    result and makes NO network call (no ESS egress). Every returned entry passes the Olog client's
    output projection: redacted against a real server (author dropped, free text withheld), but
    WHOLE against a declared loopback test sandbox — do NOT assume this layer only ever sees
    redacted data (ESS-spec pending; see services.olog_client._project). *offset*
    (Olog wire ``from``) pages past the first *size* results; *sort* orders by create time (``down``
    newest-first default, ``up`` oldest-first). ``total`` is the number of entries returned;
    ``total_matches`` is the true total across all pages (Olog ``hitCount``, ``None`` if the Olog
    version omits it); ``capped`` is True when more than *size* matched on this page. Backs
    ``search_logbook``.
    """
    cfg = get_config()
    if not cfg.olog_url:
        return {"enabled": False, "entries": [], "total": 0, "note": _OLOG_DISABLED_NOTE}

    def _run() -> dict[str, object]:
        client = OlogClient(cfg.olog_url, timeout=timeout, auth_header=cfg.olog_auth or None)
        entries, capped, total_matches = client.search_logbook(
            text=text,
            logbooks=logbooks,
            tags=tags,
            start=start,
            end=end,
            size=size,
            offset=offset,
            sort=sort,
        )
        return {
            "enabled": True,
            "entries": entries,
            "total": len(entries),
            "total_matches": total_matches,
            "capped": capped,
        }

    # Three outcomes, three classes. Collapsing them all into EpicsConnectionError (as this did)
    # tells the caller the service is unreachable when in truth the ARGUMENT was bad or the server
    # ANSWERED and said no — three different next actions reported as one.
    try:
        return await asyncio.to_thread(_run)
    except TimeWindowFormatError as exc:
        # Nothing was sent: the time window itself is unusable.
        raise EpicsError(f"Olog: {exc}", error_code="INVALID_TIME_WINDOW") from exc
    except OlogConnectionError as exc:
        # Genuinely could not reach Olog.
        raise EpicsConnectionError(f"Olog: {exc}") from exc
    except OlogError as exc:
        # The server answered (a served 4xx/5xx, or a payload we could not read).
        raise EpicsError(f"Olog: {exc}", error_code=_olog_error_code(exc)) from exc


async def query_olog_entry(log_id: str, timeout: float = 5.0) -> dict[str, object]:
    """Return one Olog entry by id (URL+declaration-bound posture). Read-only, config-gated.

    Default-disabled: with ``EPICS_MCP_OLOG_URL`` unset, returns ``enabled: false`` with
    ``found: None`` — the plane was NOT checked, mirroring the ``archived: None`` /
    ``configured: None`` / ``registered: None`` / get_archive_info ``found: None`` siblings
    (S11 Zusatzfläche 3: this was the lone ``found: False`` among them — a definitive "does not
    exist" from a plane that was never asked). ``found`` is False ONLY for the definitive 404.
    Backs ``get_log_entry``.
    """
    cfg = get_config()
    if not cfg.olog_url:
        return {"enabled": False, "id": log_id, "found": None, "note": _OLOG_DISABLED_NOTE}

    def _run() -> dict[str, object]:
        client = OlogClient(cfg.olog_url, timeout=timeout, auth_header=cfg.olog_auth or None)
        entry = client.get_log_entry(log_id)
        if entry is None:
            return {"enabled": True, "id": log_id, "found": False}
        return {"enabled": True, "id": log_id, "found": True, "entry": entry}

    try:
        return await asyncio.to_thread(_run)
    except OlogConnectionError as exc:
        raise EpicsConnectionError(f"Olog: {exc}") from exc
    except OlogError as exc:
        # S11 §8: the server ANSWERED — an unreadable payload is not an outage (search already
        # lives this three-way split; see query_olog_search).
        raise EpicsError(f"Olog: {exc}", error_code=_olog_error_code(exc)) from exc


async def query_olog_logbooks(timeout: float = 5.0) -> dict[str, object]:
    """List the valid Olog logbook names. Read-only, config-gated, name-only (owners dropped).

    Default-disabled: with ``EPICS_MCP_OLOG_URL`` unset, returns ``enabled: false`` and makes no
    network call. When enabled, returns the logbook NAMES only — each Olog ``owner`` (PII) is
    dropped in the client. These names are the valid filter values for
    ``search_logbook(logbooks=…)``. Backs ``list_logbooks``.
    """
    cfg = get_config()
    if not cfg.olog_url:
        return {"enabled": False, "logbooks": [], "note": _OLOG_DISABLED_NOTE}

    def _run() -> dict[str, object]:
        client = OlogClient(cfg.olog_url, timeout=timeout, auth_header=cfg.olog_auth or None)
        return {"enabled": True, "logbooks": client.list_logbooks()}

    try:
        return await asyncio.to_thread(_run)
    except OlogConnectionError as exc:
        raise EpicsConnectionError(f"Olog: {exc}") from exc
    except OlogError as exc:
        # S11 §8: the server ANSWERED — an unreadable payload is not an outage (search already
        # lives this three-way split; see query_olog_search).
        raise EpicsError(f"Olog: {exc}", error_code=_olog_error_code(exc)) from exc


async def query_olog_tags(timeout: float = 5.0) -> dict[str, object]:
    """List the valid Olog tag names. Read-only, config-gated (a ``Tag`` has no owner — PII-free).

    Default-disabled: with ``EPICS_MCP_OLOG_URL`` unset, returns ``enabled: false`` and makes no
    network call. These names are the valid filter values for ``search_logbook(tags=…)``. Backs
    ``list_tags``.
    """
    cfg = get_config()
    if not cfg.olog_url:
        return {"enabled": False, "tags": [], "note": _OLOG_DISABLED_NOTE}

    def _run() -> dict[str, object]:
        client = OlogClient(cfg.olog_url, timeout=timeout, auth_header=cfg.olog_auth or None)
        return {"enabled": True, "tags": client.list_tags()}

    try:
        return await asyncio.to_thread(_run)
    except OlogConnectionError as exc:
        raise EpicsConnectionError(f"Olog: {exc}") from exc
    except OlogError as exc:
        # S11 §8: the server ANSWERED — an unreadable payload is not an outage (search already
        # lives this three-way split; see query_olog_search).
        raise EpicsError(f"Olog: {exc}", error_code=_olog_error_code(exc)) from exc


def _olog_error_code(exc: BaseException) -> str:
    """A discrete, freetext-free error code for an Olog failure (never a message string).

    An :class:`EpicsError` carries its own code; an Olog connection/response error maps to a
    discrete token (the served HTTP status for a response error, when known); anything else is
    INTERNAL. Never the exception message — a write FAILED audit must stay metadata-only (SEC-5),
    which is also why this stays freetext-free now that the read path classifies errors with it."""
    if isinstance(exc, EpicsError):
        return exc.error_code
    if isinstance(exc, OlogConnectionError):
        return "OLOG_CONNECTION_ERROR"
    if isinstance(exc, OlogResponseError):
        status = http_status(exc)
        return f"OLOG_HTTP_{status}" if status is not None else "OLOG_RESPONSE_ERROR"
    return "INTERNAL"


async def query_olog_create(
    title: str,
    logbooks: list[str],
    description: str | None = None,
    level: str | None = None,
    tags: list[str] | None = None,
    in_reply_to: str | None = None,
    attachments: list[str] | None = None,
    embed_image_base64: str | None = None,
    timeout: float = 5.0,
    *,
    id_factory: Callable[[], str] = _default_olog_id_factory,
) -> dict[str, object]:
    """Create (or, with *in_reply_to*, reply to) an Olog log entry. MUTATING, gated, redacted.

    Default-disabled: with ``EPICS_MCP_OLOG_URL`` unset, returns ``enabled: false`` and makes NO
    network call. When enabled, the :class:`~epics_pv_mcp.olog_safety.OlogWriteGate` (env gate +
    test-server URL boundary + logbook allowlist + attachment size cap + rate limit) runs BEFORE any
    I/O — a denial raises (audited DENY) before a client is even constructed. The full server
    response
    is run through the read redaction before return (owner dropped, title/description withheld). A
    completed write is audited ALLOW; a write that passes the gate but fails at the HTTP layer is
    audited FAILED (no entry id/owner) and re-raised. Backs ``create_log_entry`` / ``reply_to_log``.

    *attachments* (OA1) are workspace file paths uploaded with the entry (create-with-attachments,
    ``PUT /logs/multipart``); *embed_image_base64* is a small inline image whose
    ``![](attachment/<uuid>)`` markup is appended to the description. Attachment SIZES are summed
    (``stat``, not read) and fed to the gate's size cap BEFORE any bytes are read; the UUIDs are
    minted by *id_factory* (injected for deterministic tests). ``attachments_uploaded`` echoes the
    ``{id[, filename]}`` of each upload (filename only in whole-mode — it is author free text).
    """
    cfg = get_config()
    if not cfg.olog_url:
        return {"enabled": False, "created": False, "note": _OLOG_DISABLED_NOTE}

    caller = "reply_to_log" if in_reply_to is not None else "create_log_entry"

    def _run() -> dict[str, object]:
        gate = get_olog_safety()
        # Cheap gate checks FIRST — env / URL boundary / logbook allowlist — BEFORE any filesystem
        # I/O, so a denied write never even stats the attachment paths (no file-existence oracle for
        # a caller the gate rejects). A denial is audited DENY and propagates unchanged.
        gate.check_write_preconditions(logbooks, caller=caller)
        # Now resolve + SIZE attachments (stat, NO read yet) so the size cap can refuse an
        # over-limit
        # upload before any bytes are materialised (anti-DoS). A bad path / bad base64 raises
        # EpicsError(INVALID_INPUT) here — before the full gate, and still before any file READ.
        plan = plan_attachments(attachments, embed_image_base64, id_factory)
        # Full gate: re-runs the (idempotent) cheap checks, then the attachment size cap and the
        # rate
        # token — the token is taken only here, on the success path, once. File READ + network
        # follow.
        gate.check_write_allowed(logbooks, caller=caller, attachment_bytes=plan.total_bytes)
        uploads = read_uploads(plan.specs) if plan.specs else None
        effective_description = (
            (description or "") + plan.inline_markup if plan.inline_markup else description
        )
        auth = basic_auth_header(cfg.olog_write_user, cfg.olog_write_password)
        client = OlogClient(cfg.olog_url, timeout=timeout, auth_header=auth)
        try:
            entry = client.create_log_entry(
                title=title,
                logbooks=logbooks,
                description=effective_description,
                level=level,
                tags=tags,
                in_reply_to=in_reply_to,
                attachments=uploads,
            )
        except Exception as exc:  # broad on purpose: audit ANY failed write attempt, then re-raise
            gate.audit_write_failed(
                logbooks=logbooks,
                level=level,
                title_len=len(title),
                error_code=_olog_error_code(exc),
                in_reply_to=in_reply_to,
                caller=caller,
            )
            raise
        gate.audit_write(
            entry_id=str(entry.get("id", "")),
            logbooks=logbooks,
            level=level,
            title_len=len(title),
            owner=cfg.olog_write_user,
            in_reply_to=in_reply_to,
            caller=caller,
            attachment_count=len(plan.specs),
            attachment_bytes=plan.total_bytes,
        )
        result: dict[str, object] = {"enabled": True, "created": True, "entry": entry}
        if uploads is not None:
            # Filenames are author free text → whole-mode only; ids are UUIDs (safe) → always.
            result["attachments_uploaded"] = [
                (
                    {"id": u["id"], "filename": u["filename"]}
                    if client.whole_mode
                    else {"id": u["id"]}
                )
                for u in uploads
            ]
        return result

    try:
        return await asyncio.to_thread(_run)
    except OlogConnectionError as exc:
        raise EpicsConnectionError(f"Olog: {exc}") from exc
    except OlogError as exc:
        # S11 §8: the server ANSWERED — an unreadable payload is not an outage (search already
        # lives this three-way split; see query_olog_search).
        raise EpicsError(f"Olog: {exc}", error_code=_olog_error_code(exc)) from exc


async def query_olog_add_attachment(
    log_id: str,
    attachments: list[str] | None = None,
    embed_image_base64: str | None = None,
    timeout: float = 5.0,
    *,
    id_factory: Callable[[], str] = _default_olog_id_factory,
) -> dict[str, object]:
    """Attach files to an EXISTING Olog entry (OA1b). MUTATING, gated, WHOLE-MODE ONLY.

    Default-disabled (no ``EPICS_MCP_OLOG_URL`` → ``enabled: false``, no network). WHOLE-MODE ONLY:
    the server's ``POST /logs/multipart`` runs a DESTRUCTIVE ``updateLog`` (it prunes any attachment
    not resubmitted and overwrites title/body/logbooks/tags/level/properties), so a safe attach must
    round-trip the target entry's FULL content — readable only whole (loopback +
    ``EPICS_MCP_OLOG_ASSUME_TEST_DATA``). Against a redacted server it is refused. The write goes
    through the SAME :class:`~epics_pv_mcp.olog_safety.OlogWriteGate` as create, with the allowlist
    keyed on the TARGET entry's own logbooks (read first). Backs ``add_log_attachment``.
    """
    cfg = get_config()
    if not cfg.olog_url:
        return {"enabled": False, "added": False, "note": _OLOG_DISABLED_NOTE}
    if not attachments and not embed_image_base64:
        raise EpicsError(
            "add_log_attachment needs at least one attachment (attachments or embed_image_base64)",
            error_code="INVALID_INPUT",
        )
    if not log_id.strip().isdigit():
        raise EpicsError(
            "add_log_attachment needs a numeric log_id (the id of the target entry)",
            error_code="INVALID_INPUT",
        )
    caller = "add_log_attachment"

    def _run() -> dict[str, object]:
        auth = basic_auth_header(cfg.olog_write_user, cfg.olog_write_password)
        client = OlogClient(cfg.olog_url, timeout=timeout, auth_header=auth)
        # Whole-mode FIRST — before any read or write. It is required for the round-trip source AND
        # implies the loopback write-URL boundary; a redacted/remote server is refused structurally.
        if not client.whole_mode:
            raise OlogWriteDeniedError(
                "Olog write refused: add_log_attachment needs a declared local test sandbox "
                "(loopback EPICS_MCP_OLOG_URL + EPICS_MCP_OLOG_ASSUME_TEST_DATA). Attaching to an "
                "existing entry must round-trip its full content — the server's update prunes any "
                "attachment or field not resubmitted — which is withheld against a redacted server."
            )
        raw = client.get_raw_entry(log_id)
        if raw is None:
            raise EpicsError(
                f"add_log_attachment: no Olog entry with id {log_id}", error_code="OLOG_HTTP_404"
            )
        raw_logbooks = raw.get("logbooks")
        logbook_items = raw_logbooks if isinstance(raw_logbooks, list) else []
        target_logbooks = [
            str(item["name"]) for item in logbook_items if isinstance(item, dict) and "name" in item
        ]
        raw_level = raw.get("level")
        level = raw_level if isinstance(raw_level, str) else None
        title_len = len(str(raw.get("title") or ""))
        gate = get_olog_safety()
        # Gate ordering mirrors create: cheap checks (env / URL / allowlist on the TARGET logbooks)
        # with NO filesystem first, then stat-size the attachments, then the size cap + rate token.
        gate.check_write_preconditions(target_logbooks, caller=caller)
        plan = plan_attachments(attachments, embed_image_base64, id_factory)
        gate.check_write_allowed(target_logbooks, caller=caller, attachment_bytes=plan.total_bytes)
        uploads = read_uploads(plan.specs)
        try:
            entry = client.add_attachment(log_id, raw, uploads, inline_markup=plan.inline_markup)
        except Exception as exc:  # broad on purpose: audit ANY failed attach, then re-raise
            gate.audit_write_failed(
                logbooks=target_logbooks,
                level=level,
                title_len=title_len,
                error_code=_olog_error_code(exc),
                caller=caller,
            )
            raise
        gate.audit_write(
            entry_id=str(entry.get("id", log_id)),
            logbooks=target_logbooks,
            level=level,
            title_len=title_len,
            owner=cfg.olog_write_user,
            caller=caller,
            attachment_count=len(plan.specs),
            attachment_bytes=plan.total_bytes,
        )
        # Whole-mode is guaranteed here (refused otherwise), so filenames may be echoed.
        return {
            "enabled": True,
            "added": True,
            "entry": entry,
            "attachments_uploaded": [{"id": u["id"], "filename": u["filename"]} for u in uploads],
        }

    try:
        return await asyncio.to_thread(_run)
    except OlogConnectionError as exc:
        raise EpicsConnectionError(f"Olog: {exc}") from exc
    except OlogError as exc:
        raise EpicsError(f"Olog: {exc}", error_code=_olog_error_code(exc)) from exc


async def query_olog_download(
    log_id: str | None = None,
    filename: str | None = None,
    attachment_id: str | None = None,
    output_path: str | None = None,
    as_base64: bool = False,
    timeout: float = 5.0,
) -> dict[str, object]:
    """Download one Olog attachment's raw bytes. Read-only, config-gated, and POSTURE-gated (OA1).

    Default-disabled: with ``EPICS_MCP_OLOG_URL`` unset, returns ``enabled: false`` +
    ``downloaded: false`` and makes NO network call. Bytes leave ONLY from a declared local sandbox
    (whole-mode) AND with the explicit ``EPICS_MCP_OLOG_ALLOW_ATTACHMENT_DOWNLOAD`` opt-in —
    otherwise
    the result is ``withheld: true`` and NO byte fetch happens (the posture is checked BEFORE the
    request; the client's :meth:`~.OlogClient._require_attachment_bytes_allowed` is a further
    backstop). Identify the attachment by ``(log_id + filename)`` — the primary route — or by
    ``attachment_id`` (the by-id route an inline image uses). Bytes cross the MCP boundary either
    written to ``output_path`` (a NEW workspace file, ``EPICS_MCP_ALLOWED_ROOTS``-checked) or, with
    ``as_base64=true``, base64-encoded in the result (small files only — the payload is response
    tokens). Backs ``download_log_attachment``.
    """
    if not filename and not attachment_id:
        raise EpicsError(
            "download needs either filename (with log_id) or attachment_id",
            error_code="INVALID_INPUT",
        )
    if filename and not log_id:
        raise EpicsError("download by filename needs log_id", error_code="INVALID_INPUT")
    if not as_base64 and not output_path:
        raise EpicsError(
            "download needs output_path (a workspace file to write) or as_base64=true",
            error_code="INVALID_INPUT",
        )
    if as_base64 and output_path:
        # Contradictory sinks: base64 would silently win (the elif below never runs) and the
        # smaller base64 cap would apply — an output_path handover for a >5 MiB file would then
        # fail spuriously. Refuse up front rather than pick one silently.
        raise EpicsError(
            "download takes either output_path or as_base64, not both",
            error_code="INVALID_INPUT",
        )
    cfg = get_config()
    if not cfg.olog_url:
        return {"enabled": False, "downloaded": False, "note": _OLOG_DISABLED_NOTE}

    def _run() -> dict[str, object]:
        client = OlogClient(cfg.olog_url, timeout=timeout, auth_header=cfg.olog_auth or None)
        # Posture FIRST: if raw bytes may not leave, withhold structurally — no byte fetch at all.
        if not client.attachment_bytes_allowed:
            return {
                "enabled": True,
                "downloaded": False,
                "withheld": True,
                "note": _ATTACH_WITHHELD_NOTE,
            }
        # A base64 result rides back in the tool output (response tokens), so it is capped far below
        # the path-based ceiling; a path download uses the client's default (olog_attach_max_bytes).
        max_bytes = (
            min(cfg.olog_attach_max_bytes, _BASE64_DOWNLOAD_MAX_BYTES) if as_base64 else None
        )
        if attachment_id is not None:
            content, server_filename, content_type = client.get_attachment_by_id(
                attachment_id, max_bytes=max_bytes
            )
        elif log_id is not None and filename is not None:
            content, server_filename, content_type = client.get_attachment(
                log_id, filename, max_bytes=max_bytes
            )
        else:  # unreachable — the outer guards ensure one identity is fully specified
            raise EpicsError("download identity is incomplete", error_code="INVALID_INPUT")
        result: dict[str, object] = {
            "enabled": True,
            "downloaded": True,
            "size_bytes": len(content),
            "content_type": content_type,
            "filename": server_filename,
        }
        if as_base64:
            result["content_base64"] = base64.b64encode(content).decode("ascii")
        elif output_path is not None:
            result["output_path"] = write_download(output_path, content)
        else:  # unreachable — guarded above
            raise EpicsError("download needs output_path or as_base64", error_code="INVALID_INPUT")
        return result

    try:
        return await asyncio.to_thread(_run)
    except OlogConnectionError as exc:
        raise EpicsConnectionError(f"Olog: {exc}") from exc
    except OlogError as exc:
        raise EpicsError(f"Olog: {exc}", error_code=_olog_error_code(exc)) from exc


async def query_olog_list_attachments(log_id: str, timeout: float = 5.0) -> dict[str, object]:
    """List one Olog entry's attachments (id + fileMetadataDescription; filename whole-mode only).

    Read-only, config-gated. Default-disabled: with ``EPICS_MCP_OLOG_URL`` unset returns
    ``enabled: false`` + ``found: None``. ``found: false`` for a definitive 404. Reuses
    :meth:`~.OlogClient.get_log_entry`'s projection: in whole-mode the raw ``attachments`` array
    (id / filename / fileMetadataDescription) is surfaced; in redacted mode only the count is known
    and filenames are withheld (author free text). Backs ``list_log_attachments``.
    """
    cfg = get_config()
    if not cfg.olog_url:
        return {
            "enabled": False,
            "id": log_id,
            "found": None,
            "attachments": [],
            "note": _OLOG_DISABLED_NOTE,
        }

    def _run() -> dict[str, object]:
        client = OlogClient(cfg.olog_url, timeout=timeout, auth_header=cfg.olog_auth or None)
        entry = client.get_log_entry(log_id)
        if entry is None:
            return {"enabled": True, "id": log_id, "found": False, "attachments": []}
        raw = entry.get("attachments")
        if isinstance(raw, list):
            # whole-mode: the raw list is present (id/filename/fileMetadataDescription/checksum).
            attachments: list[dict[str, object]] = [
                {
                    "id": item.get("id"),
                    "filename": item.get("filename"),
                    "fileMetadataDescription": item.get("fileMetadataDescription"),
                }
                for item in raw
                if isinstance(item, dict)
            ]
            return {
                "enabled": True,
                "id": log_id,
                "found": True,
                "attachments": attachments,
                "attachment_count": len(attachments),
            }
        # Redacted mode: no raw list — only the synthesised count; filenames withheld.
        return {
            "enabled": True,
            "id": log_id,
            "found": True,
            "attachments": [],
            "withheld": True,
            "attachment_count": entry.get("attachment_count"),
            "note": _ATTACH_LIST_WITHHELD_NOTE,
        }

    try:
        return await asyncio.to_thread(_run)
    except OlogConnectionError as exc:
        raise EpicsConnectionError(f"Olog: {exc}") from exc
    except OlogError as exc:
        raise EpicsError(f"Olog: {exc}", error_code=_olog_error_code(exc)) from exc
