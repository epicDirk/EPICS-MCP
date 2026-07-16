"""EPICS PV MCP Server — main entry point."""

import logging
from typing import Annotated, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from epics_pv_mcp import __version__
from epics_pv_mcp.prompts import compare_machine_state as _compare_machine_state
from epics_pv_mcp.prompts import diagnose_pv as _diagnose_pv
from epics_pv_mcp.resources import get_epics_config, get_guide, get_health
from epics_pv_mcp.services.diagnose import (
    DEFAULT_CHECK_ALARM,
    DEFAULT_CHECK_ARCHIVER,
    DEFAULT_CHECK_CHANNELFINDER,
    DEFAULT_CHECK_NAMING,
)
from epics_pv_mcp.tool_errors import translate_epics_errors
from epics_pv_mcp.tools.alarm import _get_alarm_history, _is_alarm_configured
from epics_pv_mcp.tools.archiver import (
    _get_archive_info,
    _get_pv_history,
    _is_archived,
    _list_archived_pvs,
)
from epics_pv_mcp.tools.channelfinder import _find_channels
from epics_pv_mcp.tools.diagnose_connection import _diagnose_connection
from epics_pv_mcp.tools.discover import _discover_pvs
from epics_pv_mcp.tools.info import _get_pv_info
from epics_pv_mcp.tools.monitor import _monitor_pv
from epics_pv_mcp.tools.naming import _lookup_device_name
from epics_pv_mcp.tools.olog import (
    _create_log_entry,
    _get_log_entry,
    _list_logbooks,
    _list_tags,
    _reply_to_log,
    _search_logbook,
)
from epics_pv_mcp.tools.read import _get_pv_value, _get_pvs
from epics_pv_mcp.tools.write import _set_pv_value

logger = logging.getLogger(__name__)

# Keep in sync with the epics-pv posture in SKILL.md
mcp = FastMCP(
    "epics-pv-mcp",
    instructions=(
        "Read-only EPICS PV access by default: read live values and metadata, monitor, "
        "discover, validate the PVs of a .bob display, cross-plane provenance, device lookup "
        "(screens + live + source IOC), ChannelFinder lookups, Archiver history + archive "
        "configuration, Alarm "
        "configuration and history, ESS Naming-Service device-name lookup, and Phoebus Olog "
        "logbook search (author dropped; free text withheld unless the Olog is a DECLARED local "
        "test sandbox — loopback URL AND EPICS_MCP_OLOG_ASSUME_TEST_DATA — where entries come "
        "back whole; run epics-doctor to see which). "
        "It can also WRITE to the Olog logbook "
        "(create_log_entry / reply_to_log) behind an OWN gate (EPICS_MCP_ALLOW_OLOG_WRITE + a "
        "test-server URL boundary + a logbook allowlist + a rate limit; the author is the write "
        "service account, not spoofable) — ALLOW_PV_WRITE is a separate gate and stays off. The "
        "PV-mutating tool, set_pv_value, is gated OFF by "
        "default and additionally requires EPICS_MCP_ALLOW_PV_WRITE=true plus a regex allowlist, "
        "a rate limit and an audit log. The REST-backed tools (find_channels, is_archived, "
        "get_pv_history, get_archive_info, list_archived_pvs, is_alarm_configured, "
        "get_alarm_history, lookup_device_name, search_logbook, get_log_entry, list_logbooks, "
        "list_tags) stay disabled "
        "until their *_URL env vars are set; an empty URL means "
        "no client and no network call. Network reach is decided by the LAUNCHER, not by this "
        "server: it opens no non-local connection until the EPICS address-list environment "
        "(EPICS_PVA_ADDR_LIST / EPICS_CA_ADDR_LIST and the matching *_AUTO_ADDR_LIST) and the "
        "*_URL vars are pointed somewhere — which a deployment may well have done, so do NOT "
        "assume isolation; run epics-doctor to see what this instance actually reaches. The write "
        "gates hold regardless of reach. File/dir tool arguments are canonicalized "
        "and existence-checked; an opt-in EPICS_MCP_ALLOWED_ROOTS (os.pathsep-separated) confines "
        "them to those roots (empty by default = no boundary). See .env.example for the commented "
        "template. For the service landscape, operational recipes (archiver PV enumeration, "
        "retrieval-cluster-aware appliances, the combined CA-bundle recipe) and typical error "
        "signatures, read the epics-pv://guide resource."
    ),
)
# S1-2: FastMCP exposes no public API to set the server version, so we reach the low-level MCP
# server defensively — a FastMCP upgrade that renames/removes ``_mcp_server`` then degrades to
# "version unset" instead of crashing the whole server at import with an AttributeError.
_low_level_server = getattr(mcp, "_mcp_server", None)
if _low_level_server is not None:
    _low_level_server.version = __version__

# === Tools ===


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
@translate_epics_errors
async def get_pv_value(
    pv_name: Annotated[str, Field(description="EPICS PV name")],
    timeout: Annotated[
        float | None,
        Field(description="Timeout in seconds (default: EPICS_MCP_DEFAULT_TIMEOUT)"),
    ] = None,
) -> dict[str, object]:
    """Get the current value of an EPICS Process Variable.

    The result carries the same best-effort metadata as get_pv_info
    (alarm/timestamp/display/control/value_alarm/enum)."""
    return await _get_pv_value(pv_name, timeout)


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
@translate_epics_errors
async def get_pvs(
    names: Annotated[
        list[str],
        Field(
            description="List of PV names to read (capped at the server's max_batch_size, "
            "default 100 — EPICS_MCP_MAX_BATCH_SIZE)"
        ),
    ],
    timeout: Annotated[
        float | None,
        Field(description="Timeout in seconds per PV (default: EPICS_MCP_DEFAULT_TIMEOUT)"),
    ] = None,
) -> dict[str, object]:
    """Batch-read multiple EPICS PVs in a single call.

    Each result carries the same best-effort metadata as get_pv_info
    (alarm/timestamp/display/control/value_alarm/enum)."""
    return await _get_pvs(names, timeout)


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    )
)
@translate_epics_errors
async def set_pv_value(
    pv_name: Annotated[str, Field(description="EPICS PV name")],
    value: Annotated[str, Field(description="New value to set")],
    timeout: Annotated[
        float | None,
        Field(description="Timeout in seconds (default: EPICS_MCP_DEFAULT_TIMEOUT)"),
    ] = None,
) -> dict[str, object]:
    """Set a PV value. Requires EPICS_MCP_ALLOW_PV_WRITE=true.

    Protected by safety layer: environment gate, regex allowlist,
    rate-limit (10/min default), and audit logging.
    """
    return await _set_pv_value(pv_name, value, timeout)


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
@translate_epics_errors
async def get_pv_info(
    pv_name: Annotated[str, Field(description="EPICS PV name")],
    timeout: Annotated[
        float | None,
        Field(description="Timeout in seconds (default: EPICS_MCP_DEFAULT_TIMEOUT)"),
    ] = None,
) -> dict[str, object]:
    """Get detailed PV metadata: value, alarm (severity/status incl. text + message),
    timestamp, display (units/limits/precision OR format/description), control (drive
    limits), value_alarm (active flag + the configured HIHI/HIGH/LOW/LOLO limits; NaN/unset
    limits and the per-PVA-unmapped per-level severities are omitted), and enum index/label/
    choices for enum PVs. Unset (zero-width) display/control limit pairs are omitted; DBR_CHAR
    waveforms come back as int lists.

    Record fields read directly: pass a channel with a field suffix (e.g. get_pv_info("PV.RTYP"),
    "PV.SCAN", "PV.HIHI") to read individual record metadata / alarm thresholds — useful when a PVA
    gateway serves the NT value_alarm limits as NaN. A cold (first) connection can need a longer
    timeout: default_timeout stays 5 s but pass timeout>=8 for the first read of an idle PV."""
    return await _get_pv_info(pv_name, timeout)


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    )
)
@translate_epics_errors
async def monitor_pv(
    name: Annotated[str, Field(description="EPICS PV name to monitor")],
    duration: Annotated[
        float,
        Field(
            description="Duration in seconds to monitor (clamped to the server's "
            "max_monitor_duration, default 60 — EPICS_MCP_MAX_MONITOR_DURATION)"
        ),
    ] = 10.0,
    max_events: Annotated[
        int,
        Field(
            description="Maximum events to collect (clamped to the server's max_monitor_events, "
            "default 1000 — EPICS_MCP_MAX_MONITOR_EVENTS)"
        ),
    ] = 100,
) -> dict[str, object]:
    """Subscribe to PV changes for a given duration and return collected events.

    Each event carries the same best-effort metadata as get_pv_info
    (alarm/timestamp/display/control/value_alarm/enum)."""
    return await _monitor_pv(name, duration, max_events)


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
@translate_epics_errors
async def discover_pvs(
    pattern: Annotated[
        str,
        Field(description="PV name or pattern to search for"),
    ],
    timeout: Annotated[
        float | None,
        Field(description="Timeout in seconds (default: EPICS_MCP_DEFAULT_TIMEOUT)"),
    ] = None,
) -> dict[str, object]:
    """Discover PVs by name. Wildcard patterns require ChannelFinder infrastructure."""
    return await _discover_pvs(pattern, timeout)


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
@translate_epics_errors
async def find_channels(
    name_pattern: Annotated[
        str,
        Field(
            description=(
                "Channel/PV name glob (ChannelFinder syntax: * and ?). ANCHORED and "
                "CASE-INSENSITIVE: a bare substring matches nothing — wrap it in * to search "
                "inside a name."
            )
        ),
    ],
    max_results: Annotated[
        int,
        Field(description="Cap on returned channels (a broad glob can match a whole site)"),
    ] = 500,
    timeout: Annotated[float, Field(description="Timeout in seconds")] = 5.0,
) -> dict[str, object]:
    """Query ChannelFinder: which IOC/host serves a PV, plus its tags/properties.

    Read-only. Disabled by default (set EPICS_MCP_CHANNELFINDER_URL to enable).

    The glob is matched by the SERVER, and both of its properties bite silently (measured live
    2026-07-15). It is ANCHORED: 'Ctrl-EVR-01' matches 0 channels while '*Ctrl-EVR-01*' matches
    them all, so a bare substring reads as 'no such channel' rather than as a syntax mistake.
    And it is CASE-INSENSITIVE: '*temp*', '*Temp*' and '*TEMP*' return the identical set, so a
    hit may differ in case from what was asked (e.g. '*Temp*' matching '...MorTemPrd').

    A malformed registry record (a non-dict element, or one without a usable name) raises a loud
    error — records are never silently dropped into a smaller, fabricated answer.
    """
    return await _find_channels(name_pattern, max_results, timeout)


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
@translate_epics_errors
async def lookup_device_name(
    name: Annotated[
        str,
        Field(
            description="ESS device name (the device part of a PV, without the trailing property — "
            "e.g. DEV-TEST01:Ctrl-EVR-01)"
        ),
    ],
    timeout: Annotated[float, Field(description="Timeout in seconds", gt=0)] = 5.0,
) -> dict[str, object]:
    """Look up an ESS device name in the Naming Service: is it registered and ACTIVE?

    Read-only. Disabled by default — returns enabled=false unless EPICS_MCP_NAMING_URL is set (no
    ESS egress otherwise). A reachable service answering "not registered" — HTTP 204, the signal
    the real service actually sends (measured 2026-07-16), or HTTP 404 — yields registered=false,
    a DEFINITIVE answer; OBSOLETE/DELETED also yield registered=false with the status preserved.
    A service/URL failure (unreachable, 5xx, bad JSON, timeout) and a 2xx record without a
    readable status are WITHHELD (registered=null + withheld=true), never collapsed into a false
    "not registered". Surfaces only registered/status/message. Unlike diagnose_connection this
    needs no live PV probe — it answers the pure registry question directly.
    """
    return await _lookup_device_name(name, timeout)


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
@translate_epics_errors
async def is_archived(
    pv: Annotated[str, Field(description="EPICS PV name")],
    timeout: Annotated[float, Field(description="Timeout in seconds")] = 5.0,
) -> dict[str, object]:
    """Report whether a PV is being archived (EPICS Archiver Appliance MGMT getPVStatus).

    Read-only. Disabled by default — returns enabled=false unless EPICS_MCP_ARCHIVER_URL is set.
    Beyond archived/status the result surfaces the MGMT record's connection_state (source IOC
    connected now?), last_event (time of the last archived sample), is_monitored, sampling_period
    and appliance when present — same single getPVStatus call, no extra cost. An unreadable
    getPVStatus payload raises a loud error — never a fabricated archived=false: the appliance
    answers even an UNKNOWN pv with a real record (status "Not being archived", measured), so
    that record is the only definitive negative.

    is_archived answers only for a NAMED PV. To ENUMERATE the archived PVs use list_archived_pvs
    (getAllPVs / getPVsForThisAppliance, NOT getMatchingPVs — it 404s on split/proxied deployments).
    See the epics-pv://guide resource.
    """
    return await _is_archived(pv, timeout)


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
@translate_epics_errors
async def get_pv_history(
    pv: Annotated[str, Field(description="EPICS PV name")],
    start: Annotated[
        str, Field(description="Window start, ISO-8601 (e.g. 2026-06-01T00:00:00.000Z)")
    ],
    end: Annotated[str, Field(description="Window end, ISO-8601")],
    max_points: Annotated[
        int,
        # ge=1: a non-positive cap is meaningless — it would empty a valid response and the client
        # would then mislabel it "withheld". le caps an absurd inline pull (page by window instead).
        Field(
            description="Cap on returned samples (a wide window on a fast PV is unbounded)",
            ge=1,
            le=100000,
        ),
    ] = 5000,
    timeout: Annotated[float, Field(description="Timeout in seconds", gt=0)] = 5.0,
) -> dict[str, object]:
    """Fetch archived samples for a PV over an ISO-8601 window (Archiver retrieval getData.json).

    Read-only. Disabled by default — returns enabled=false unless EPICS_MCP_ARCHIVER_URL is set. The
    result includes the getData.json meta block (PV metadata such as EGU units and PREC precision)
    alongside the samples. capped is true when the window held more than max_points samples. status
    disambiguates an empty result: "ok" (samples returned), "empty" (a valid but sample-less window)
    or "withheld" (an unreadable response, with a withheld_reason) — so an empty samples list is
    never mistaken for "no data" when the truth is "could not read". A single unreadable sample
    in the data array withholds the WHOLE result (it is never silently skipped or zero-filled
    into a plausible sample).
    """
    return await _get_pv_history(pv, start, end, max_points, timeout)


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
@translate_epics_errors
async def get_archive_info(
    pv: Annotated[str, Field(description="EPICS PV name")],
    timeout: Annotated[float, Field(description="Timeout in seconds", gt=0)] = 5.0,
) -> dict[str, object]:
    """Report HOW a PV is archived — its archive configuration (Archiver MGMT getPVTypeInfo).

    Read-only. Disabled by default — returns enabled=false unless EPICS_MCP_ARCHIVER_URL is set.
    Complements is_archived (live connection state) and get_pv_history (the samples): surfaces the
    archive CONFIGURATION — sampling method/period, retention (the STS/MTS/LTS data_stores),
    computed event/storage rates, dbr_type, archived fields, source host_name and creation_time.
    found is false when the appliance has no type-info record for the PV (unknown / never
    archived) — it signals that with HTTP 404 and ONLY that; an unreadable 2xx raises a loud
    error instead of a false found (neither a fabricated "not archived" nor, for an unrelated
    body, a fabricated found=true).

    get_archive_info answers only for a NAMED PV; list_archived_pvs enumerates them
    (getAllPVs / getPVsForThisAppliance, NOT getMatchingPVs — 404s on split/proxied). See
    epics-pv://guide.
    """
    return await _get_archive_info(pv, timeout)


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
@translate_epics_errors
async def list_archived_pvs(
    pattern: Annotated[
        str | None,
        Field(
            description="Optional PV-name glob (e.g. 'DEV-TEST01:*'); omit to list all. "
            "Cannot be combined with this_appliance=true — that endpoint has no name filter"
        ),
    ] = None,
    this_appliance: Annotated[
        bool,
        Field(
            description="List only THIS cluster member (getPVsForThisAppliance) instead of all. "
            "This endpoint cannot filter by name — leave pattern unset"
        ),
    ] = False,
    limit: Annotated[
        int,
        # ge=1: a non-positive cap is meaningless — a negative limit would make the client's
        # names[:limit] slice silently DROP names and falsely report capped. le caps an absurd pull.
        Field(
            description="Cap on returned PV names (a whole appliance can hold tens of thousands)",
            ge=1,
            le=100000,
        ),
    ] = 5000,
    timeout: Annotated[float, Field(description="Timeout in seconds", gt=0)] = 5.0,
) -> dict[str, object]:
    """List the PV names the Archiver Appliance archives (Archiver MGMT getAllPVs).

    Read-only. Disabled by default — returns enabled=false unless EPICS_MCP_ARCHIVER_URL is set.
    Uses getAllPVs (whole appliance) or, with this_appliance=true, getPVsForThisAppliance (this
    cluster member) — NOT getMatchingPVs, which 404s on split/proxied deployments.

    pattern is an optional name glob and works ONLY with this_appliance=false (it maps to getAllPVs'
    pv param). getPVsForThisAppliance has NO name filter at all, so pattern together with
    this_appliance=true is REFUSED (INVALID_ARGUMENT) rather than ignored — the endpoint would
    otherwise return a full, plausible list of the WRONG PVs. To filter by name, drop
    this_appliance.

    capped is true when the appliance held more than limit names (honest over-fetch). PV names carry
    no person data — no redaction needed.
    """
    return await _list_archived_pvs(pattern, this_appliance, limit, timeout)


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
@translate_epics_errors
async def is_alarm_configured(
    pv: Annotated[str, Field(description="EPICS PV name")],
    config_name: Annotated[
        str,
        Field(
            description=(
                "Alarm config-tree name (top-level topic, e.g. Accelerator). CASE-SENSITIVE: a "
                "wrong or mis-cased name yields configured=null (withheld), never false."
            )
        ),
    ] = "Accelerator",
    timeout: Annotated[float, Field(description="Timeout in seconds", gt=0)] = 5.0,
) -> dict[str, object]:
    """Report whether a PV has an alarm configuration (Phoebus Alarm Logger /search/alarm/config).

    Read-only. Disabled by default — returns enabled=false unless EPICS_MCP_ALARM_URL is set.
    A hit proves the PV is configured in the alarm tree; a miss is a real negative only when the
    Alarm Logger was running at config-import time (else the config change never reached its index).

    configured is true / false / null, and null means WITHHELD — the tree itself returned nothing,
    so 'this PV is not configured' cannot be told apart from 'that is not the tree name'; a note
    then says so. An unreadable payload or record raises a loud error instead of falling through
    to the tree probe as a false negative. config_name is CASE-SENSITIVE even though the server
    lower-cases it to pick the index (measured live 2026-07-15: 'accelerator' selects the right
    index and matches nothing, reporting exactly like a genuinely unconfigured PV). The returned
    config field echoes your input — it is NOT the server confirming the tree exists.
    """
    return await _is_alarm_configured(pv, config_name, timeout)


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
@translate_epics_errors
async def get_alarm_history(
    pv: Annotated[
        str,
        Field(
            description="EPICS PV / device name (matched as a wildcard substring on the alarm "
            "config path; each event carries its own pv/config so over-matches stay visible)"
        ),
    ],
    start: Annotated[
        str,
        Field(
            description="Window start (REQUIRED) — absolute (ISO-8601, e.g. 2026-06-01T00:00:00Z) "
            "or a single relative amount (e.g. '8 hours', '2 days'). No months/years — use days "
            "or weeks."
        ),
    ],
    end: Annotated[
        str,
        Field(
            description="Window end (REQUIRED) — absolute (ISO-8601) or a single relative amount "
            "(e.g. 'now')"
        ),
    ],
    max_events: Annotated[
        int,
        # le=999, not 1000: the client requests size=max_events+1 so `capped` is an honest
        # fetched>max_events. The Alarm Logger's default es_max_size is 1000 and it clamps
        # size=min(es_max_size, requested); capping max_events at 999 keeps size<=1000 so the +1
        # probe still fits under the DEFAULT ceiling. (A backend configured with a lower es_max_size
        # can still under-report capped — documented on AlarmClient.get_alarm_history.)
        Field(description="Cap on returned events, newest first", ge=1, le=999),
    ] = 100,
    timeout: Annotated[float, Field(description="Timeout in seconds", gt=0)] = 5.0,
) -> dict[str, object]:
    """Fetch the alarm state history of a PV over a window (Phoebus Alarm Logger /search/alarm).

    Read-only. Disabled by default — returns enabled=false unless EPICS_MCP_ALARM_URL is set. start
    and end are required (a defaultless query must not pull the whole history). The stream carries
    alarm STATE changes and also alarm-CONFIG-change messages (the config field prefix
    state:/config: distinguishes them). Events are newest first and carry only technical fields
    (severity/message/value/time/current_severity/current_message/enabled/mode/pv/config); the raw
    doc's user/host (who acknowledged/enabled/disabled) and command are stripped (privacy). capped
    is true when more than max_events matched. An unreadable payload or record raises a loud
    error — never an empty result that reads as 'nothing alarmed'.

    Time window: an absolute value is normalized to zone-explicit UTC before sending (a naive one
    is read as UTC); a single relative amount ('8 hours', 'now') passes through. A value the server
    would misread is rejected before any request rather than sent: the Alarm Logger does not reject
    an unreadable time, it silently takes it as 'now' and answers 200 with an empty list that is
    indistinguishable from 'nothing alarmed'.
    """
    return await _get_alarm_history(pv, start, end, max_events, timeout)


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
@translate_epics_errors
async def search_logbook(
    text: Annotated[
        str | None, Field(description="Free-text to search in the log description")
    ] = None,
    logbooks: Annotated[
        str | None, Field(description="Comma-separated logbook names to filter by")
    ] = None,
    tags: Annotated[str | None, Field(description="Comma-separated tag names to filter by")] = None,
    start: Annotated[
        str | None,
        Field(
            description="Window start — an absolute time (ISO-8601, e.g. '2026-07-15T10:00:00Z') "
            "or a single amount ('7 days', '90 min'). No months/years — use days or weeks."
        ),
    ] = None,
    end: Annotated[
        str | None,
        Field(
            description="Window end — an absolute time (ISO-8601) or a single amount. "
            "Omit to search up to now."
        ),
    ] = None,
    size: Annotated[int, Field(description="Cap on returned entries", ge=1, le=200)] = 50,
    offset: Annotated[
        int, Field(description="0-based pagination offset — read past the first page", ge=0)
    ] = 0,
    sort: Annotated[
        Literal["down", "up"],
        Field(
            description=(
                "Create-time order: 'down' newest-first (default) or 'up' oldest-first. "
                "Rejected here if it is neither: Olog reads any unrecognized value as 'up', "
                "silently returning the REVERSE of the documented default."
            )
        ),
    ] = "down",
    timeout: Annotated[float, Field(description="Timeout in seconds", gt=0)] = 5.0,
) -> dict[str, object]:
    """Search the Phoebus Olog electronic logbook (Olog REST /logs/search).

    Read-only. Disabled by default — returns enabled=false unless EPICS_MCP_OLOG_URL is set.
    DS-PRIVACY: entries are redacted — technical fields (id, dates, level, state) and logbook/tag
    NAMES are kept, author/owner is dropped, the title/description free text is WITHHELD (a person
    can be named inside it), attachments are a count only. They come back WHOLE only for a DECLARED
    local sandbox (a loopback URL AND EPICS_MCP_OLOG_ASSUME_TEST_DATA), so results are judgeable;
    the shape is the same either way (the full mode only adds fields). ESS-spec pending — run
    epics-doctor for the effective posture.

    Time window: start/end take an absolute time (ISO-8601 — normalized to UTC before sending;
    a naive value is read as UTC) or a single relative amount ('7 days', '90 min', 'now'). Months
    and years are NOT supported by Olog — use days or weeks. A value Olog could not read is
    rejected before any request rather than sent: Olog does not reject an unreadable time, it
    silently reads it as 'now' and answers 200 with an empty result that is indistinguishable
    from 'nothing matched'.

    Page the history with offset (0-based; Olog wire 'from') and order with sort ('down'=newest
    first, the default; 'up'=oldest first). sort only accepts those two values and is rejected
    otherwise, because Olog does not reject an unrecognized order: it silently applies 'up' —
    the REVERSE of the documented default — and answers 200 with a well-formed page (measured
    live 2026-07-15: 'newest' and 'garbage' both returned oldest-first). total is the number of
    entries returned; total_matches is the true total across all pages (Olog hitCount); capped is
    true when more than size matched on this page. An unreadable payload or entry raises a loud
    error — never an empty result that reads as 'nothing matched'.
    """
    return await _search_logbook(
        text=text,
        logbooks=logbooks,
        tags=tags,
        start=start,
        end=end,
        size=size,
        offset=offset,
        sort=sort,
        timeout=timeout,
    )


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
@translate_epics_errors
async def get_log_entry(
    log_id: Annotated[str, Field(description="Olog log entry id")],
    timeout: Annotated[float, Field(description="Timeout in seconds", gt=0)] = 5.0,
) -> dict[str, object]:
    """Fetch one Phoebus Olog entry by id (Olog REST /logs/{id}).

    Read-only. Disabled by default — returns enabled=false with found=null (the plane was NOT
    checked) unless EPICS_MCP_OLOG_URL is set. Same DS-PRIVACY posture as search_logbook
    (redacted: author dropped, title/description withheld, attachments as a count; whole only for
    a DECLARED local sandbox).

    found is false ONLY on the service's definitive HTTP 404; an unreadable 2xx raises a loud
    error (it is neither a "not found" nor projected as a fabricated entry). NOTE: a real Olog
    answers 401 for an unknown id on this anonymous read path (measured 2026-07-16 — its error
    dispatch requires auth), which surfaces as an error, not as found=false.
    """
    return await _get_log_entry(log_id, timeout)


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
@translate_epics_errors
async def list_logbooks(
    timeout: Annotated[float, Field(description="Timeout in seconds", gt=0)] = 5.0,
) -> dict[str, object]:
    """List the valid Phoebus Olog logbook names (Olog REST /logbooks).

    Read-only. Disabled by default — returns enabled=false unless EPICS_MCP_OLOG_URL is set. Returns
    the logbook NAMES only (owners dropped) — the valid values for search_logbook(logbooks=…).
    An unreadable listing raises a loud error — never an empty 'there are none'.
    """
    return await _list_logbooks(timeout)


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
@translate_epics_errors
async def list_tags(
    timeout: Annotated[float, Field(description="Timeout in seconds", gt=0)] = 5.0,
) -> dict[str, object]:
    """List the valid Phoebus Olog tag names (Olog REST /tags).

    Read-only. Disabled by default — returns enabled=false unless EPICS_MCP_OLOG_URL is set. Returns
    the tag NAMES only — the valid values for search_logbook(tags=…). Tags carry no owner.
    An unreadable listing raises a loud error — never an empty 'there are none'.
    """
    return await _list_tags(timeout)


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    )
)
@translate_epics_errors
async def create_log_entry(
    title: Annotated[str, Field(description="Log entry title (required, non-empty)")],
    logbooks: Annotated[
        str, Field(description="Comma-separated target logbook name(s) — must already exist")
    ],
    description: Annotated[str | None, Field(description="Log body / description text")] = None,
    level: Annotated[
        str | None, Field(description="Entry level (e.g. 'Info'; server default when omitted)")
    ] = None,
    tags: Annotated[
        str | None, Field(description="Comma-separated tag name(s) — must already exist")
    ] = None,
    timeout: Annotated[float, Field(description="Timeout in seconds", gt=0)] = 5.0,
) -> dict[str, object]:
    """Post a new entry to the Phoebus Olog electronic logbook (Olog REST PUT /logs).

    MUTATING. Disabled by default and behind its OWN gate (separate from set_pv_value): it needs
    EPICS_MCP_ALLOW_OLOG_WRITE=true AND a test-server URL boundary (only a loopback Olog, or an
    allowlisted URL with EPICS_MCP_OLOG_WRITE_ALLOW_REMOTE=true) AND a logbook allowlist
    (EPICS_MCP_OLOG_WRITE_LOGBOOKS) AND a rate limit — ALLOW_PV_WRITE is untouched. The author
    (owner) is the configured write service account, set server-side; a caller cannot spoof it. The
    returned entry follows the same posture as a read (redacted; whole only for a DECLARED local
    sandbox — where a write can therefore verify what it just wrote). A write response that is not
    the created entry raises a loud error — it is never projected as a fabricated confirmation.
    With EPICS_MCP_OLOG_URL unset the tool returns enabled=false and makes no network call.
    """
    return await _create_log_entry(
        title=title,
        logbooks=logbooks,
        description=description,
        level=level,
        tags=tags,
        timeout=timeout,
    )


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    )
)
@translate_epics_errors
async def reply_to_log(
    log_id: Annotated[str, Field(description="Id of the existing Olog entry to reply to")],
    title: Annotated[str, Field(description="Reply title (required, non-empty)")],
    logbooks: Annotated[
        str, Field(description="Comma-separated target logbook name(s) — must already exist")
    ],
    description: Annotated[str | None, Field(description="Reply body / description text")] = None,
    level: Annotated[
        str | None, Field(description="Entry level (e.g. 'Info'; server default when omitted)")
    ] = None,
    tags: Annotated[
        str | None, Field(description="Comma-separated tag name(s) — must already exist")
    ] = None,
    timeout: Annotated[float, Field(description="Timeout in seconds", gt=0)] = 5.0,
) -> dict[str, object]:
    """Reply to an existing Phoebus Olog entry (Olog REST PUT /logs?inReplyTo=log_id).

    MUTATING. Same gate, service account, and DS-PRIVACY redaction as create_log_entry — it threads
    the new entry to log_id via the Olog Log Entry Group. A log_id that identifies no existing entry
    returns a clear HTTP 400 error. Disabled by default (needs EPICS_MCP_OLOG_URL +
    EPICS_MCP_ALLOW_OLOG_WRITE).
    """
    return await _reply_to_log(
        log_id=log_id,
        title=title,
        logbooks=logbooks,
        description=description,
        level=level,
        tags=tags,
        timeout=timeout,
    )


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
@translate_epics_errors
async def diagnose_connection(
    pv_name: Annotated[str, Field(description="The PV to diagnose")],
    timeout: Annotated[
        float | None,
        Field(description="Live-probe timeout in seconds (default: config diagnose_timeout, 5.0)"),
    ] = None,
    check_channelfinder: Annotated[
        bool,
        Field(
            description="Consult ChannelFinder: is the PV registered, its last-known pvStatus, and "
            "which IOC/host serves it. Withheld when EPICS_MCP_CHANNELFINDER_URL is unset."
        ),
    ] = DEFAULT_CHECK_CHANNELFINDER,
    check_naming: Annotated[
        bool,
        Field(
            description="Consult the ESS Naming Service to tell a typo apart from an unregistered "
            "device. Default False + gated on EPICS_MCP_NAMING_URL — no ESS egress unless enabled."
        ),
    ] = DEFAULT_CHECK_NAMING,
    check_archiver: Annotated[
        bool,
        Field(description="Corroborate with the Archiver (recent samples ⇒ recently connected)."),
    ] = DEFAULT_CHECK_ARCHIVER,
    check_alarm: Annotated[
        bool,
        Field(description="Corroborate with the Alarm tree (known ⇒ a real, monitored PV)."),
    ] = DEFAULT_CHECK_ALARM,
) -> dict[str, object]:
    """Diagnose WHY a PV is (dis)connected: state + likely cause + per-plane evidence + next steps.

    Read-only. The live p4p connect is the ONLY truth for connected/disconnected — a disconnected
    PV is a NORMAL input (this does NOT raise). ChannelFinder/Naming/Archiver/Alarm are explanatory
    only: they give a likely_cause + evidence, never flip the verdict, and a disabled/errored plane
    is 'withheld' (never a false negative). likely_cause is one of healthy, ioc_down, name_typo,
    unregistered, indeterminate; 'indeterminate' is first-class and honest. On a PVA name-server a
    typo and a dead IOC both time out (PV_NOT_FOUND only under UDP broadcast), so cause is keyed on
    ChannelFinder/Naming, never the transport error code. No collision/uniqueness claim is made
    (multi-responder detection is out of scope). Naming is off by default (no ESS egress).
    """
    return await _diagnose_connection(
        pv_name,
        timeout=timeout,
        check_channelfinder=check_channelfinder,
        check_naming=check_naming,
        check_archiver=check_archiver,
        check_alarm=check_alarm,
    )


# Display-aware tools (validate_pvs / crossplane_check / coverage_audit / find_device) need the
# opi_navigation PV-inventory — the optional `[displays]` extra. Register them only when it is
# installed, so the core PV server installs and starts standalone without it.
try:
    from epics_pv_mcp.display_tools import register_display_tools

    register_display_tools(mcp)
except ImportError:
    logger.info(
        "Display tools disabled: opi_navigation not installed (pip install epics-pv-mcp[displays])"
    )


# === Resources ===


@mcp.resource("epics-pv://health")
def health() -> dict[str, object]:
    """Server status, p4p version, write configuration."""
    return get_health()


@mcp.resource("epics-pv://config")
def epics_config() -> dict[str, object]:
    """Non-secret configuration values."""
    return get_epics_config()


@mcp.resource("epics-pv://guide")
def guide() -> str:
    """Agent-readable operational cookbook: service planes, recipes, error signatures."""
    return get_guide()


# === Prompts ===


@mcp.prompt()
def diagnose_pv(pv_name: str) -> str:
    """Step-by-step PV diagnosis workflow."""
    return _diagnose_pv(pv_name)


@mcp.prompt()
def compare_machine_state(pv_prefix: str, reference_file: str = "") -> str:
    """Compare current machine state to expected values."""
    return _compare_machine_state(pv_prefix, reference_file)


def main() -> None:
    """Entry point for the MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
