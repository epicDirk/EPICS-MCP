"""The services layer's single downward edge to the four REST planes (M8/M9/C2).

This module is the one place higher layers reach the ChannelFinder / Archiver / Alarm / Naming
REST clients. It carries two kinds of edge:

1. **Injected protocol checkers + factories** (M8), the pure cross-plane cores
   (:mod:`~.crossplane`, :mod:`~.coverage`) define the checker *Protocols* and receive concrete
   checkers as parameters. Each adapter translates its REST client's errors into ``RuntimeError``
   (a truncated ChannelFinder registry into :class:`~.crossplane.CFRegistryCapped`) so the pure
   cores can withhold a verdict without importing a client or catching broad exceptions.

2. **Per-plane async query functions** (M9), :func:`query_archived`, :func:`query_alarm_configured`
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
from typing import Any, TypedDict

from epics_mcp.config import get_config
from epics_mcp.errors import EpicsConnectionError, EpicsError
from epics_mcp.provenance import Reach
from epics_mcp.services._http import http_status, shown_cause
from epics_mcp.services._time_window import TimeWindowFormatError
from epics_mcp.services.alarm_client import AlarmClient
from epics_mcp.services.alarm_exceptions import AlarmConnectionError, AlarmError
from epics_mcp.services.archiver_client import ArchiverClient
from epics_mcp.services.archiver_exceptions import ArchiverConnectionError, ArchiverError
from epics_mcp.services.channelfinder_client import DEFAULT_MAX_RESULTS, ChannelFinderClient
from epics_mcp.services.channelfinder_exceptions import (
    ChannelFinderConnectionError,
    ChannelFinderError,
)

# Backward-compat re-export: tools/olog.py and the olog tests import these Olog query functions
# (and the private _olog_error_code) from here; they now live in checkers_olog (MA-1 split). The
# ``name as name`` form makes the re-export explicit for mypy --strict (no_implicit_reexport).
from epics_mcp.services.checkers_olog import (
    _olog_error_code as _olog_error_code,
)
from epics_mcp.services.checkers_olog import (
    query_olog_add_attachment as query_olog_add_attachment,
)
from epics_mcp.services.checkers_olog import (
    query_olog_create as query_olog_create,
)
from epics_mcp.services.checkers_olog import (
    query_olog_download as query_olog_download,
)
from epics_mcp.services.checkers_olog import (
    query_olog_entry as query_olog_entry,
)
from epics_mcp.services.checkers_olog import (
    query_olog_levels as query_olog_levels,
)
from epics_mcp.services.checkers_olog import (
    query_olog_list_attachments as query_olog_list_attachments,
)
from epics_mcp.services.checkers_olog import (
    query_olog_logbooks as query_olog_logbooks,
)
from epics_mcp.services.checkers_olog import (
    query_olog_search as query_olog_search,
)
from epics_mcp.services.checkers_olog import (
    query_olog_tags as query_olog_tags,
)
from epics_mcp.services.checkers_olog import (
    query_olog_update as query_olog_update,
)
from epics_mcp.services.coverage import AlarmChecker, ArchivedChecker
from epics_mcp.services.crossplane import CFRegistryCapped, ChannelFinderChecker
from epics_mcp.services.naming_client import NamingServiceClient
from epics_mcp.services.naming_exceptions import NamingServiceError


class CFRegistryChecker:
    """Edge adapter: the channel names ChannelFinder registers under an IOC prefix.

    Implements the core's :class:`~.crossplane.ChannelFinderChecker` Protocol. Translates
    ChannelFinder/network errors into ``RuntimeError`` (and a truncated result into
    :class:`~.crossplane.CFRegistryCapped`) so the pure core can withhold cf_unregistered
    without importing the ChannelFinder client or catching broad exceptions. Counts **all**
    registered channels regardless of ``pvStatus``, a momentarily Inactive-but-present channel
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
                # Truncated registry: withhold rather than diff a partial set (would false-flag).
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
        config_name: str,
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
                f"Alarm tree {self._config_name!r} returned no configuration at all, "
                "unknown or misspelled tree name, or an empty tree"
            )
        return configured


def build_cf_checker(query_channelfinder: bool) -> ChannelFinderChecker | None:
    """Build the ChannelFinder checker iff requested AND a URL is configured.

    Returns ``None`` when not requested, or requested but ``channelfinder_url`` is unset, in the
    latter case the caller passes ``cf_requested=True`` so the core emits an honest "skipped, URL
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

    Mirrors :func:`build_cf_checker`: requested but ``naming_url`` unset → ``None``
    (no client, no egress). The client carries no hard-coded ESS prod default, so
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


def build_alarm_checker(query_alarm: bool, alarm_config: str | None) -> AlarmChecker | None:
    """Build the Alarm checker iff requested AND ``EPICS_MCP_ALARM_URL`` is set (else None).

    MA-2b(d): when the plane is actually active (requested AND URL set) the caller MUST name the
    alarm tree, a missing ``alarm_config`` is a LOUD ``INVALID_INPUT`` rather than a silent scan of
    a guessed default tree that matches nothing. When the plane is inactive (not requested / URL
    unset) a missing tree is moot, so it returns ``None`` without complaint.
    """
    if not query_alarm:
        return None
    cfg = get_config()
    if not cfg.alarm_url:
        return None
    if alarm_config is None:
        raise EpicsError(
            "The alarm plane was requested but no alarm tree (alarm_config) was named; there is no "
            "correct default tree (they are site-specific). Name the tree to query.",
            error_code="INVALID_INPUT",
        )
    return AlarmConfigChecker(cfg.alarm_url, cfg.alarm_auth or None, config_name=alarm_config)


# ---------------------------------------------------------------------------
# Per-plane async query functions (M9), the config-gate + off-loop + error-translation logic
# lifted out of the tool layer. The MCP tool wrappers AND diagnose() call these SAME functions,
# so services/diagnose no longer imports upward from tools/*. Each returns the exact
# tool-facing dict shape it did before the lift (lossless, the tool wrappers are now thin).
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
    "(e.g. http://naming.example), WITHOUT a trailing /rest, to enable device-name lookup."
)


# Withheld (configured=None), NOT a negative: the tree yielded no configuration at all, so
# "this PV is not configured" cannot be told apart from "that is not the tree name". The name is
# case-sensitive in the query even though it is lower-cased to pick the index, see
# AlarmClient.is_alarm_configured.
_ALARM_TREE_UNKNOWN_NOTE = (
    "Alarm config tree {config!r} returned no configuration at all, unknown or misspelled tree "
    "name (it is CASE-SENSITIVE here), or an empty tree. Answer withheld rather than reported as "
    "'not configured'."
)

# MA-2b(d): no alarm tree named. There is no correct default (site-specific trees), so we withhold
# rather than probe a guessed tree that would match nothing and read as "not configured".
_ALARM_NO_TREE_NOTE = (
    "No alarm config tree was named, so alarm config cannot be checked, there is no correct "
    "default tree (they are site-specific). Answer withheld; name the tree to query."
)


def _alarm_error_code(exc: AlarmError) -> str:
    """A discrete error code for an Alarm-Logger failure whose SERVER answered.

    The served HTTP status when it is readable, else a generic token, the sibling of
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
    (like its sibling :func:`_olog_error_code`) so ``tools/archiver`` imports it DOWNWARD, the
    ``services → tools`` direction is what the M9 lift exists to prevent.

    Kept separate from ``_olog_error_code`` rather than generalized: that one also feeds the Olog
    write AUDIT (which must stay metadata-only, SEC-5) and carries an EpicsError branch for the
    write gate, so folding them together would put that audit in the blast radius of an unrelated
    fix, for six lines of status-to-token boilerplate.

    Note: a retry-exhausted 502/503/504 arrives as ``requests.RetryError``, which is NOT a
    ConnectionError, so it lands here as an ArchiverResponseError with no readable status and maps
    to ARCHIVER_RESPONSE_ERROR. That is correct: the host DID answer, repeatedly.
    """
    status = http_status(exc)
    return f"ARCHIVER_HTTP_{status}" if status is not None else "ARCHIVER_RESPONSE_ERROR"


# --- Tool result shape (S29): is_archived's archive-status tri-state -----------------------------
# One total=False TypedDict over every key across query_archived's return paths (disabled /
# enabled). Same MA-1 nullability rule as AlarmConfiguredResult above: a field ABSENT on some
# return path, or present-but-sometimes-None (``archived`` is None on the disabled path), is
# typed ``X | None``; the measured rationale lives in ``services/checkers_olog.py``'s "Tool result
# shapes" header. ``archived`` is the load-bearing case: an EXPLICIT None on the disabled path, and
# the WIRE path validates the emitted null against the advertised schema, so a non-nullable
# annotation would fail a real client's call. Only ``enabled`` + ``pv`` are present on EVERY path
# and never null.
#
# The 8 DS-4A/AR-D enrichment fields are copied UNCOERCED from the getPVStatus record
# (get_archive_status: ``result[out_key] = record[source_key]``), so their wire value is whatever
# the appliance sent, measured both bool and str across the fixtures, hence ``object | None``,
# NOT ``str | None``: a stricter scalar would make a bool value fail its own outputSchema at call
# time. That consequence is REAL but the mechanism is not fastmcp's: standalone fastmcp builds no
# output model and does not validate the return. The check that bites lives one layer out, in the
# MCP SDK's low-level call_tool handler, which runs jsonschema over the structuredContent against
# the advertised schema, so the failure surfaces only on the WIRE, never on the in-process
# ``FastMCP.call_tool`` this repo's conformance tests MOSTLY drive (the discover_pvs and
# find_channels ones are the exceptions, both drive a real client, and there is now a generic
# wire test over every typed tool; the full statement,
# including the second validator and what neither of them sees, is in checkers_olog.py's header).
# Which makes the loose type the safer one here: a mis-typed raw-copy would otherwise pass every
# per-tool test and break a real client. The flip side is that a loose type advertises almost
# nothing, ``object | None`` renders as an unconstrained schema, so no validator can ever bite
# on these 8 fields. That is the accepted trade, not an oversight: it is why the wire tests
# deliberately do NOT chase payload coverage here.
# ``archived``/``status`` ARE computed (bool / non-empty str), so they stay precisely typed.
class ArchiveStatusResult(TypedDict, total=False):
    enabled: bool
    pv: str
    archived: bool | None
    status: str | None
    connection_state: object | None
    last_event: object | None
    is_monitored: object | None
    sampling_period: object | None
    appliance: object | None
    connection_loss_regain_count: object | None
    connection_first_established: object | None
    connection_last_restablished: object | None  # upstream typo, verbatim (getPVStatus wire key)
    note: str | None
    reach: Reach


async def query_archived(pv: str, timeout: float = 5.0) -> ArchiveStatusResult:
    """Report whether *pv* is being archived (Archiver MGMT getPVStatus). Read-only, config-gated.

    Default-disabled: with ``EPICS_MCP_ARCHIVER_URL`` unset, returns a structured ``enabled: false``
    result and makes NO network call (preserves localhost isolation). Shared by the ``is_archived``
    tool and the diagnose Archiver plane. DS-4A: surfaces the enriched MGMT fields
    (``connection_state``/``last_event``/``is_monitored``/...) alongside ``archived``/``status``
    from the SAME single ``getPVStatus`` call (diagnose reads only ``archived``; extras are
    additive).
    """
    cfg = get_config()
    if not cfg.archiver_url:
        return {"enabled": False, "pv": pv, "archived": None, "note": _ARCHIVER_DISABLED_NOTE}

    def _run() -> ArchiveStatusResult:
        client = ArchiverClient(
            cfg.archiver_url, timeout=timeout, auth_header=cfg.archiver_auth or None
        )
        status = client.get_archive_status(pv)
        # archived (bool) + status (non-empty str) are always present and computed; bool()/str() are
        # identities that satisfy the TypedDict from get_archive_status' dict[str, object].
        result: ArchiveStatusResult = {
            "enabled": True,
            "pv": pv,
            "archived": bool(status["archived"]),
            "status": str(status["status"]),
        }
        # DS-4A/AR-D enrichment: present only when getPVStatus carried the field. A TypedDict needs
        # LITERAL keys, so each is set explicitly under a presence guard (no loop over a key list);
        # the value is copied uncoerced (object | None), matching the wire (was a ** splat before).
        if "connection_state" in status:
            result["connection_state"] = status["connection_state"]
        if "last_event" in status:
            result["last_event"] = status["last_event"]
        if "is_monitored" in status:
            result["is_monitored"] = status["is_monitored"]
        if "sampling_period" in status:
            result["sampling_period"] = status["sampling_period"]
        if "appliance" in status:
            result["appliance"] = status["appliance"]
        if "connection_loss_regain_count" in status:
            result["connection_loss_regain_count"] = status["connection_loss_regain_count"]
        if "connection_first_established" in status:
            result["connection_first_established"] = status["connection_first_established"]
        if "connection_last_restablished" in status:
            result["connection_last_restablished"] = status["connection_last_restablished"]
        return result

    try:
        return await asyncio.to_thread(_run)
    except ArchiverConnectionError as exc:
        raise EpicsConnectionError(f"Archiver: {exc}") from exc
    except ArchiverError as exc:
        # The server ANSWERED (a served 4xx/5xx): that is not an outage. Safe for diagnose, which
        # catches Exception totally here and withholds either way (diagnose._gather_archiver).
        raise EpicsError(f"Archiver: {exc}", error_code=_archiver_error_code(exc)) from exc


# --- Tool result shape (S29): is_alarm_configured's tri-state ------------------------------------
# One total=False TypedDict over every key across query_alarm_configured's return paths (disabled /
# no-tree-withheld / configured). Same MA-1 nullability rule as the Olog shapes, the measured
# rationale (absent key is DROPPED, not null; an explicit None IS emitted and the WIRE path
# validates it) lives once in ``services/checkers_olog.py``'s "Tool result shapes" header; do not
# restate it here. A field ABSENT on some return path, or present-but-sometimes-None
# (``configured`` is None on the disabled/withheld/tree-unknown paths), is typed ``X | None``.
# ``configured`` is the load-bearing case: it is an EXPLICIT None, so a non-nullable annotation
# would make a real client's call fail validation, not merely mis-advertise the shape.
# Only ``enabled`` + ``pv`` are present on EVERY path and never null. fastmcp yields a typed
# outputSchema (``properties``;
# ``anyOf[T, null]`` for the nullable fields) instead of the bare ``{additionalProperties: true}`` a
# plain dict yields; mypy --strict checks every ``return {...}`` literal below against this shape.
class AlarmConfiguredResult(TypedDict, total=False):
    enabled: bool
    pv: str
    configured: bool | None
    config: str | None
    withheld: bool | None
    detail: dict[str, object] | None
    note: str | None
    reach: Reach


async def query_alarm_configured(
    pv: str,
    config_name: str | None = None,
    timeout: float = 5.0,
) -> AlarmConfiguredResult:
    """Report whether *pv* has an alarm configuration (Alarm Logger /search/alarm/config).

    Default-disabled: with ``EPICS_MCP_ALARM_URL`` unset, returns ``enabled: false`` and makes no
    network call. Shared by the ``is_alarm_configured`` tool and the diagnose Alarm plane.

    MA-2b(d): *config_name* (the alarm tree) has NO default, there is no correct one (the trees are
    site-specific and a committed default cannot name one). With it ``None`` the answer is withheld
    honestly (``configured: None`` + a note) instead of probing a guessed tree that matches nothing;
    NO network call is made for the guess. The ``is_alarm_configured`` tool makes the tree required,
    so a user never reaches this branch, ``diagnose`` (which has no tree to offer) degrades to an
    honest withheld here instead of a silently-wrong answer.
    """
    cfg = get_config()
    if not cfg.alarm_url:
        return {"enabled": False, "pv": pv, "configured": None, "note": _ALARM_DISABLED_NOTE}
    if config_name is None:
        return {
            "enabled": True,
            "pv": pv,
            "config": None,
            "configured": None,
            "withheld": True,
            "note": _ALARM_NO_TREE_NOTE,
        }

    def _run() -> AlarmConfiguredResult:
        client = AlarmClient(cfg.alarm_url, timeout=timeout, auth_header=cfg.alarm_auth or None)
        configured, detail = client.is_alarm_configured(pv, config_name=config_name)
        result: AlarmConfiguredResult = {
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
        # S11 §8: the server ANSWERED (a served 4xx/5xx or an unreadable 2xx), relabelling that
        # "cannot reach the Alarm Logger" sends the caller after an outage that is not happening.
        raise EpicsError(f"Alarm Logger: {exc}", error_code=_alarm_error_code(exc)) from exc


# get_alarm_history's tool result shape (S29, typed MCP outputSchema). ``total=False``: the
# disabled path carries {enabled, pv, events, note}; the enabled path adds start/end/total/capped.
# ``enabled``/``pv``/``events`` are on EVERY path (events is [] when disabled, never null) →
# non-nullable; ``start``/``end``/``total``/``capped`` (enabled-only) and ``note`` (disabled-only)
# are ``X | None``. ``events`` is a list of projected alarm docs (opaque dict items). class-syntax.
class AlarmHistoryResult(TypedDict, total=False):
    enabled: bool
    pv: str
    events: list[dict[str, object]]
    start: str | None
    end: str | None
    total: int | None
    capped: bool | None
    note: str | None
    reach: Reach


async def query_alarm_history(
    pv: str,
    start: str,
    end: str,
    max_events: int = 100,
    timeout: float = 5.0,
    *,
    root: str | None = None,
    command: str | None = None,
    severity: str | None = None,
    current_severity: str | None = None,
) -> AlarmHistoryResult:
    """Report the alarm history of *pv* over ``[start, end]`` (Alarm Logger ``/search/alarm``).

    Default-disabled: with ``EPICS_MCP_ALARM_URL`` unset, returns ``enabled: false`` and makes no
    network call. *start*/*end* are required (a defaultless query must not pull the whole history).
    Events are newest-first and projected onto the technical allowlist, the raw doc's
    ``user``/``host``/``command`` are stripped (DS-PRIVACY). ``capped`` is True when more than
    *max_events* matched (the newest are kept). Shared by the ``get_alarm_history`` tool.
    """
    cfg = get_config()
    if not cfg.alarm_url:
        return {"enabled": False, "pv": pv, "events": [], "note": _ALARM_DISABLED_NOTE}

    def _run() -> AlarmHistoryResult:
        client = AlarmClient(cfg.alarm_url, timeout=timeout, auth_header=cfg.alarm_auth or None)
        events, capped = client.get_alarm_history(
            pv,
            start,
            end,
            max_events,
            root=root,
            command=command,
            severity=severity,
            current_severity=current_severity,
        )
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
        # S11 §8: the server ANSWERED, an unreadable payload is not an outage.
        raise EpicsError(f"Alarm Logger: {exc}", error_code=_alarm_error_code(exc)) from exc


# find_channels' tool result shape (S29, typed MCP outputSchema). It lives HERE, at the shared
# service, and not at the tool adapter the way ``DiscoverPvsResult`` does: this literal IS the
# tool's result (``tools/channelfinder.py`` only forwards it), so the type belongs where the dict
# is built, and the other consumers of the shared payload inherit it.
#
# ``total=False`` over the union of FOUR return literals, and the two MODES are DISJOINT: a
# ``count_only`` path carries ``match_count`` and never ``channels``/``total``/``capped``; a list
# path carries ``channels``/``total`` and never ``match_count``. ``capped`` is narrower still:
# it exists ONLY on the configured list path, not on the unconfigured one, which adds ``note``
# instead. Only ``enabled`` is on all four -> non-nullable;
# every other field is ``X | None``. The nullability rationale is the canonical one in
# ``services/checkers_olog.py``'s "Tool result shapes" header; the closest precedent for a
# PARAMETER deciding which keys exist at all is ``OlogDownloadResult`` (as_base64 vs output_path).
#
# ``channels`` stays ``list[dict[str, object]]`` instead of a nested TypedDict, the same
# convention as ``DiscoverPvsResult.pvs`` and ``ArchiverHistoryResult.samples``; no output shape in
# this server nests one. Here that is a CHOICE with a price, named rather than hidden: a channel
# element really is always a ``ChannelInfo`` (6 fields, all required), so the opaque
# ``{type: object}`` item schema advertises less than is known. Tightening it would put the first
# ``$defs`` on the wire and touches three generic schema guards, so it is its own job, not a side
# effect of this one.
class ChannelQueryResult(TypedDict, total=False):
    enabled: bool
    channels: list[dict[str, object]] | None
    total: int | None
    capped: bool | None
    match_count: int | None
    note: str | None
    reach: Reach


async def query_channels(
    name_pattern: str,
    max_results: int = DEFAULT_MAX_RESULTS,
    timeout: float = 5.0,
    *,
    has_properties: dict[str, str] | None = None,
    lacks_properties: list[str] | None = None,
    not_property_values: dict[str, str] | None = None,
    has_tags: list[str] | None = None,
    lacks_tags: list[str] | None = None,
    count_only: bool = False,
) -> ChannelQueryResult:
    """Query ChannelFinder for channels whose name matches *name_pattern* (glob ``*``/``?``).

    Default-disabled: with ``EPICS_MCP_CHANNELFINDER_URL`` unset, returns ``enabled: false`` and
    makes no network call. Shared by the ``find_channels`` tool, ``find_device`` and the diagnose
    ChannelFinder plane.

    MA-2 optional filters (property/tag, negation) narrow the search server-side; ``count_only``
    returns an exact ``match_count`` from the ``/count`` endpoint instead of the channel list. A
    rejected/redacted filter surfaces as an ``INVALID_INPUT`` :class:`EpicsError`.
    """
    cfg = get_config()
    if not cfg.channelfinder_url:
        if count_only:
            return {"enabled": False, "match_count": 0, "note": _CF_DISABLED_NOTE}
        return {"enabled": False, "channels": [], "total": 0, "note": _CF_DISABLED_NOTE}

    # Conditional forwarding: pass ONLY the filters that are actually set, so the no-filter path is
    # a byte-identical call to the pre-MA-2 code, that is what keeps ``find_device``/``diagnose``
    # and the fixed-signature CF test doubles (which take no filter kwargs) working untouched.
    filters: dict[str, Any] = {}
    if has_properties:
        filters["has_properties"] = has_properties
    if lacks_properties:
        filters["lacks_properties"] = lacks_properties
    if not_property_values:
        filters["not_property_values"] = not_property_values
    if has_tags:
        filters["has_tags"] = has_tags
    if lacks_tags:
        filters["lacks_tags"] = lacks_tags

    def _run() -> ChannelQueryResult:
        client = ChannelFinderClient(
            cfg.channelfinder_url,
            timeout=timeout,
            auth_header=cfg.channelfinder_auth or None,
        )
        if count_only:
            return {"enabled": True, "match_count": client.count_channels(name_pattern, **filters)}
        # S8-6: fetch one MORE than the cap and truncate, so ``capped`` is an honest
        # ``fetched > max_results`` (the Archiver pattern) rather than ``>= max_results``, which
        # false-flags exactly ``max_results`` real channels as truncated (an off-by-one).
        fetched = client.find_channels(name_pattern, max_results=max_results + 1, **filters)
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
    except ValueError as exc:
        # MA-2: a rejected/redacted filter (the _build_query_params guards) is BAD INPUT, not an
        # outage, surface it as INVALID_INPUT, never as an unreachable/response error.
        raise EpicsError(f"ChannelFinder: {exc}", error_code="INVALID_INPUT") from exc
    except ChannelFinderConnectionError as exc:
        raise EpicsConnectionError(f"ChannelFinder: {exc}") from exc
    except ChannelFinderError as exc:
        # S11 §8: the server ANSWERED, an unreadable payload is not an outage.
        raise EpicsError(
            f"ChannelFinder: {exc}", error_code=_channelfinder_error_code(exc)
        ) from exc


# list_channel_vocabulary's tool result shape (S29, typed MCP outputSchema). ``total=False``: the
# disabled path adds ``note`` and the enabled path omits it. ``enabled``/``properties``/``tags`` are
# on EVERY path (both list the same keys; disabled returns empty lists, never null) → non-nullable;
# only ``note`` (disabled-only) is ``str | None``. ``properties``/``tags`` are lists of NAMES.
class ChannelVocabularyResult(TypedDict, total=False):
    enabled: bool
    properties: list[str]
    tags: list[str]
    note: str | None
    reach: Reach


async def query_channel_vocabulary(timeout: float = 5.0) -> ChannelVocabularyResult:
    """List the ChannelFinder property + tag NAMES a caller can filter ``find_channels`` on.

    Default-disabled: with ``EPICS_MCP_CHANNELFINDER_URL`` unset, returns ``enabled: false`` and
    makes no network call. ``properties`` is the DS-privacy allowlisted subset that exists in this
    instance (so it never advertises a key ``find_channels`` would refuse); ``tags`` is the full
    server tag set (tags are ungated). Both fetched in one worker: if either listing is
    unreadable/unreachable the call fails loudly (an unreadable listing must never read as an empty
    vocabulary). No filter input → no ``ValueError`` gate arm (unlike :func:`query_channels`).
    """
    cfg = get_config()
    if not cfg.channelfinder_url:
        return {"enabled": False, "properties": [], "tags": [], "note": _CF_DISABLED_NOTE}

    def _run() -> ChannelVocabularyResult:
        client = ChannelFinderClient(
            cfg.channelfinder_url,
            timeout=timeout,
            auth_header=cfg.channelfinder_auth or None,
        )
        return {
            "enabled": True,
            "properties": client.list_properties(),
            "tags": client.list_tags(),
        }

    try:
        return await asyncio.to_thread(_run)
    except ChannelFinderConnectionError as exc:
        raise EpicsConnectionError(f"ChannelFinder: {exc}") from exc
    except ChannelFinderError as exc:
        # S11 §8: the server ANSWERED, an unreadable payload is not an outage.
        raise EpicsError(
            f"ChannelFinder: {exc}", error_code=_channelfinder_error_code(exc)
        ) from exc


# Nullability mirrors AlarmConfiguredResult (see its comment above; the measured rationale lives
# in ``services/checkers_olog.py``'s "Tool result shapes" header). A field ABSENT on some return
# path, status/message (success-only), withheld (withheld-only), note (disabled/withheld), or
# None on some path, ``registered``, the tri-state, is typed ``X | None``. ``registered`` is the
# load-bearing case: an EXPLICIT None, which the wire path validates. Only ``enabled`` + ``name``
# are present on EVERY path and never null. fastmcp then yields a typed outputSchema
# (``properties``;
# ``anyOf[T, null]`` for the nullable fields) instead of the bare ``{additionalProperties: true}``
# a plain dict yields; mypy --strict checks every ``return {...}`` literal below against this shape.
class NameLookupResult(TypedDict, total=False):
    enabled: bool
    name: str
    registered: bool | None
    status: str | None
    message: str | None
    withheld: bool | None
    note: str | None
    reach: Reach


async def query_naming_lookup(name: str, timeout: float = 5.0) -> NameLookupResult:
    """Look up an ESS device name in the Naming Service: is it registered + ACTIVE?

    Read-only, config-gated. Default-disabled: with ``EPICS_MCP_NAMING_URL`` unset, returns a
    structured ``enabled: false`` result and makes NO network call (no ESS egress). Backs the
    standalone ``lookup_device_name`` tool, the one naming plane that had no ``query_*`` sibling
    (it was only reachable indirectly via ``diagnose_connection``/``crossplane_check``). Since
    GB-98 ``diagnose._gather_naming`` calls THIS function too, so the naming probe has one
    implementation rather than a pair that agreed by inspection.

    Answer semantics (DS-2, a service error must NEVER masquerade as "not registered"):

    * **Reachable + registered** → ``registered: true`` with ``status`` (ACTIVE) and ``message``.
    * **Reachable + not registered** (a genuine 404 on ``deviceNames``) → ``registered: false``, a
      DEFINITIVE answer.
    * **Reachable + OBSOLETE/DELETED/unknown** → ``registered: false`` with the status preserved.
    * **Unreachable / 5xx / bad JSON / timeout** → ``registered: null`` + ``withheld: true`` with a
      note; the reachability is probed FIRST (``check_connectivity``) so a
      down/slow service is withheld rather than read as a definitive answer.

    Surfaces only the privacy-safe :class:`NameStatus` projection (``registered/status/message``);
    the richer raw ``DeviceNameElement`` (deviceType/discipline/system/subsystem) is deliberately
    NOT exposed (MVP scope). Shared by the ``lookup_device_name`` tool.
    """
    client = build_naming_client(True, timeout=timeout)
    if client is None:
        return {"enabled": False, "name": name, "registered": None, "note": _NAMING_DISABLED_NOTE}

    def _run() -> NameLookupResult:
        # Probe reachability FIRST so an unreachable/timing-out service is WITHHELD by the
        # ``except`` below, not read as a definitive answer. ⚠ This used to say it MIRRORED
        # diagnose._gather_naming, and that was the problem rather than the reassurance it read as:
        # two copies of one probe. The gatherer calls this function now, so this ordering is the
        # only copy and both callers inherit it. A reachable
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
        # WITHHOLD on any service error: an unexpected bug (non-NamingServiceError) still escapes
        # to the tool shell's translate_epics_errors as an [INTERNAL] ToolError.
        return {
            "enabled": True,
            "name": name,
            "registered": None,
            "withheld": True,
            # ``shown_cause`` rather than a raw ``{exc}``, and this is the LAST-RESORT net
            # rather than the primary one: the client edge already redacts every address it
            # names. What it does not cover is text the exception carries for other reasons,
            # and the output-side rule needs no knowledge of the secret (it withholds any
            # message carrying an ``@``). GB-98 measured why it belongs HERE: diagnose used
            # to apply it on its own side, and routing the gatherer through this function
            # would have dropped it for that caller while ``lookup_device_name``, which
            # never had it, kept going without. One net, both callers.
            "note": f"Naming lookup withheld: {shown_cause(exc)}",
        }
