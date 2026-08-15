"""EPICS MCP server, main entry point."""

import argparse
import importlib.util
import logging
from collections.abc import Callable, Sequence
from typing import Annotated, Literal

from fastmcp import FastMCP
from fastmcp.tools import ToolResult
from mcp.types import TextContent, ToolAnnotations
from pydantic import Field

from epics_mcp import __version__
from epics_mcp.cli_common import add_version_argument, configure_stdout
from epics_mcp.config import get_config
from epics_mcp.errors import SafetyConfigError
from epics_mcp.presets import PRESETS
from epics_mcp.prompts import compare_machine_state as _compare_machine_state
from epics_mcp.prompts import diagnose_pv as _diagnose_pv
from epics_mcp.prompts import setup_epics_mcp as _setup_epics_mcp

# ``get_guide`` under an alias: the module namespace now also carries a TOOL of that name (one
# name across all three CS-Studio surfaces), and the resource handler below has to keep reaching
# the packaged TEXT rather than calling the tool.
from epics_mcp.resources import get_epics_config, get_health
from epics_mcp.resources import get_guide as _guide_text
from epics_mcp.safety import get_safety
from epics_mcp.services.checkers import (
    AlarmConfiguredResult,
    AlarmHistoryResult,
    ArchiveStatusResult,
    ChannelQueryResult,
    ChannelVocabularyResult,
    NameLookupResult,
)
from epics_mcp.services.checkers_olog import (
    OlogAddAttachmentResult,
    OlogCreateResult,
    OlogDownloadResult,
    OlogEntryResult,
    OlogLevelsResult,
    OlogListAttachmentsResult,
    OlogLogbooksResult,
    OlogSearchResult,
    OlogTagsResult,
    OlogUpdateResult,
)
from epics_mcp.services.diagnose import (
    DEFAULT_CHECK_ALARM,
    DEFAULT_CHECK_ARCHIVER,
    DEFAULT_CHECK_CHANNELFINDER,
    DEFAULT_CHECK_NAMING,
)
from epics_mcp.tool_errors import translate_epics_errors
from epics_mcp.tools.alarm import _get_alarm_history, _is_alarm_configured
from epics_mcp.tools.archiver import (
    ApplianceInfoResult,
    ArchivedPvsResult,
    ArchiveInfoResult,
    ArchiverHistoryResult,
    _get_appliance_info,
    _get_archive_info,
    _get_pv_history,
    _is_archived,
    _list_archived_pvs,
)
from epics_mcp.tools.channelfinder import _find_channels, _list_channel_vocabulary
from epics_mcp.tools.diagnose_connection import _diagnose_connection
from epics_mcp.tools.discover import DiscoverPvsResult, _discover_pvs
from epics_mcp.tools.guide import TOPICS as _GUIDE_TOPICS
from epics_mcp.tools.guide import serve_guide as _serve_guide
from epics_mcp.tools.info import _get_pv_info
from epics_mcp.tools.monitor import _monitor_pv
from epics_mcp.tools.naming import _lookup_device_name
from epics_mcp.tools.olog import (
    _add_log_attachment,
    _create_log_entry,
    _download_log_attachment,
    _get_log_entry,
    _list_log_attachments,
    _list_log_levels,
    _list_logbooks,
    _list_tags,
    _reply_to_log,
    _search_logbook,
    _update_log_entry,
)
from epics_mcp.tools.read import _get_pv_value, _get_pvs
from epics_mcp.tools.write import _set_pv_value

logger = logging.getLogger(__name__)


def _display_tools_available() -> bool:
    """True iff the optional ``displays`` group is installed (its sole package is opi_navigation).

    ``find_spec`` has no import side effects, but it is NOT total: it propagates whatever a
    meta-path finder raises, and this call runs at MODULE level through _load_display_registrar,
    so an unhandled raise here took the whole core server down with a traceback. Measured before
    this guard existed: under a finder that raises for opi_navigation, ``import epics_mcp.server``
    died, while the display-aware CLIs degraded cleanly, because cli_common.require_display_engine
    had already been given this treatment (QA-14). The same question was being answered two ways.

    So the answer is total now, with the mapping that function measured: a ModuleNotFoundError from
    a finder still means the module is not there, which is the supported core-only state and stays
    silent; anything else is a finder that could not answer, which is not the same claim and is
    logged loud rather than reported as "not installed". Either way an optional group must never
    crash the core.

    ``tests/conftest.py`` gates its display-coupled modules on the same expression, spelled out
    there rather than imported from here, so this function is not on that path.
    """
    try:
        return importlib.util.find_spec("opi_navigation") is not None
    except ModuleNotFoundError:
        # A finder saying "no such module" IS the absent answer, not a failure to answer.
        return False
    except Exception:  # an optional group must never crash core, logged loud just below
        logger.error(
            "the opi_navigation availability probe failed (a meta-path finder could not answer); "
            "treating the display group as unavailable, core PV tools remain available.",
            exc_info=True,
        )
        return False


def build_instructions(display_tools_available: bool) -> str:
    """Render the server ``instructions`` from the actual capability set (S26/N06).

    The display-gated capabilities are advertised only when the ``displays`` group is installed,
    so a core-only install does not over-claim them. A pure function of the flag → both branches
    are directly testable without a reimport.

    ⚠️ Advertised is a SMALLER set than gated, and the difference is not an oversight to repair
    here. ``display_tools.register`` gates FOUR tools; the clause below names three of them
    (validate_pvs, crossplane_check, find_device) and ``coverage_audit`` is in neither branch of
    this header. There is no room for it: the budget below is measured at the guard and what is
    left of it is a handful of bytes, so a fourth clause would truncate the whole header rather
    than add a line. The tool is registered, described and guided like the others; it is only this
    one pre-choice channel that cannot afford to mention it. Read the enumeration as "what fits",
    never as "what the group installs".
    """
    display_clause = (
        # "file or display view" earns its bytes: without it this clause reads as a promise that
        # the tool reports what a display SHOWS, which is not the view it takes by default. The
        # byte budget below leaves no room to spell that out; the tool description does.
        #
        # GB-79 named the second file kind here for ZERO net bytes, and the wording was measured
        # rather than chosen: ".bob display" -> ".bob or .plt" is the only candidate that spends
        # none of the head-room the budget has left. Spelling it out as ".bob display or .plt
        # trend" is 14 characters longer, and measured against the cap today that does not fit at
        # all, which is what the guard below is for: it keeps room for new tool lines. Which kind
        # is which, and that a trend answers the two views by different routes, is the tool
        # description's job under the division of labour the paragraph above states.
        "validate the PVs of a .bob or .plt (file or display view), cross-plane provenance, "
        "device lookup (screens + live + source IOC), "
        if display_tools_available
        else ""
    )
    return (
        # The guide pointer leads so the header never truncates its own escape hatch; the detail
        # this used to inline (per-tool write clause, full network-reach prose) lives in the guide.
        #
        # It names the get_guide TOOL, not the epics-pv://guide resource it used to name, and that
        # is the whole point of this sentence rather than a rewording: a resource is
        # application-controlled and a model does not pull from one, so the old pointer sent the
        # reader to a channel they could not use. The resource still exists for a human or an
        # application. ⚠️ The sentence is also the ONLY thing that makes this surface's guide
        # discoverable before a tool is chosen, so it is pinned by name in
        # test_guide_tool.py: dropping it while trimming this header would leave the whole guide
        # unreachable with every other guard still green. Measured: the swap FREED 29 bytes (187
        # to 158), which is what made it fit at all, the header having had 2 bytes left.
        "Call get_guide FIRST unless you already know the tool you need: the service landscape, "
        "operational recipes and error signatures, one named section at a time. "
        "Read-only EPICS PV access by default: read live values and metadata, monitor, "
        "discover, " + display_clause + "ChannelFinder lookups, Archiver history + archive "
        "configuration, Alarm configuration and history, ESS Naming-Service device-name lookup, "
        "and Phoebus Olog logbook search (whole entries: title, text, author, attachments). "
        "It can also WRITE to the Olog logbook (create/reply/update entries, each carrying "
        "attachments) behind its OWN gate (EPICS_MCP_ALLOW_OLOG_WRITE, a named target logbook, a "
        "test-server URL boundary, a logbook allowlist, an upload-size cap, a rate limit; the "
        "author is the write service account, not spoofable). "
        "The PV-mutating tool set_pv_value is gated OFF by default and additionally requires "
        "EPICS_MCP_ALLOW_PV_WRITE=true plus a regex allowlist, a rate limit and an audit log, a "
        "separate gate from the Olog one, and it stays off. "
        "Either gate needs a durable EPICS_MCP_AUDIT_LOG_FILE to start; the loopback-only reach is "
        "a PV-gate condition only. A sanctioned PV write refuses an out-of-range value before the "
        "put. "
        "After a sanctioned write it reads the value back and returns a structured result "
        "(verified/readback/tolerance) plus a READBACK audit event, so a wrong or not-landed value "
        "is surfaced, not silently accepted. "
        "REST-backed tools stay disabled until their *_URL env vars are set. "
        # READ, not "network": the PV-WRITE reach is not the launcher's, it is forced loopback-only
        # at start (safety.py:77-89, stated above). Says PV because the OLOG write reach IS the
        # launcher's, bounded by an env allowlist rather than by an assert. This header is capped at
        # 2048 bytes, and what is left of that budget is measured by the guard named
        # test_build_instructions_under_2048_bytes rather than named here, because a head-room
        # figure written into prose drifts with every edit to this string and nothing compares it.
        # So the qualifier is one word and the detail lives in epics-pv://guide.
        "READ reach is decided by the LAUNCHER, not this server: a deployment may well point "
        "the EPICS env at a real facility, so do NOT assume isolation, run epics-doctor to see "
        "what this instance actually reaches. The write gates hold regardless of reach. "
        "File/dir tool arguments are canonicalized and existence-checked; an opt-in "
        "EPICS_MCP_ALLOWED_ROOTS confines them (empty by default = no boundary). See .env.example "
        "for the commented configuration template."
    )


def _load_display_registrar() -> Callable[[FastMCP], None] | None:
    """Load the display-tool registrar iff the optional ``displays`` group is installed AND
    imports cleanly. Returns the registrar (run once the server is built) or ``None``, the ONE
    capability truth every surface derives from (tool registration, the ``instructions`` string,
    and the ``compare_machine_state`` prompt), so they can never diverge (S26/N06).

    Degrade-loud posture:
    - A MISSING group (``find_spec`` None) is the supported core-only state, return None silently
      so the core PV server installs and starts standalone.
    - An INSTALLED group that fails to import (broken transitive dep, corrupt module, ...) is a
      BROKEN deployment, not a missing one: log ERROR with the correct attribution and return None,
      so the core PV server stays up AND no surface over-claims display tools that did not register.
      The catch is broad on purpose, an OPTIONAL group must never crash the core server, while the
      ERROR + exc_info keep the failure loud (the former broad ``except ImportError`` logged INFO
      "not installed", mis-attributing an internal import failure as a missing package).
    """
    if not _display_tools_available():
        return None
    try:
        from epics_mcp.display_tools import register_display_tools
    except Exception:  # an optional group must never crash core, logged loud just below
        logger.error(
            "opi_navigation is installed but the display tools failed to load "
            "(broken displays dependency group); core PV tools remain available.",
            exc_info=True,
        )
        return None
    return register_display_tools


# One capability truth: the registrar exists only if the extra is installed AND imports cleanly.
_display_registrar = _load_display_registrar()
_DISPLAY_TOOLS_AVAILABLE = _display_registrar is not None

# Keep in sync with the epics-pv posture in SKILL.md
mcp = FastMCP(
    "epics-mcp",
    version=__version__,
    instructions=build_instructions(_DISPLAY_TOOLS_AVAILABLE),
    # mask_error_details controls ONLY whether the detail text of a NON-ToolError exception (a
    # genuine internal bug) reaches the client: True masks it to "Error calling tool '<name>'",
    # False and None pass it through. The curated ``[<code>] message`` / ``[INTERNAL] ...``
    # ToolError text reaches the client under EVERY setting anyway (ToolError is the deliberate
    # client boundary, measured for None, False and True). An explicit False is the unmasked
    # SDK 1.0 behaviour, kept identical here to the standalone default None. A hardening option
    # and a deliberate operator choice, not ours to make: True would mask internal exception
    # strings, more consistent with the redaction posture but leaving no detail on a bug.
    mask_error_details=False,
)


# === Tools ===


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        # Closed world: this tool reads its own packaged document and nothing else. It is the one
        # tool here that contacts no service at all, which is what makes it safe as the very first
        # call of a session, before epics-doctor has said what this instance even reaches.
        openWorldHint=False,
    ),
)
@translate_epics_errors
async def get_guide(
    topic: Annotated[
        str | None,
        Field(
            description=(
                "One part of the guide instead of the whole document. The keys, in document "
                f"order: {', '.join(_GUIDE_TOPICS)}. They PARTITION the guide, so a section key "
                "(posture, planes, tools, recipes, errors) serves that section's own text and NOT "
                "the subsections under it, which are keys of their own: 'tools' is the tool "
                "inventory, the Olog and write-posture detail below it is 'olog-output', "
                "'olog-filters', 'pv-write', 'audit', 'olog-write', 'olog-attachments'. Omit the "
                "argument, or pass an empty string, for everything; surrounding whitespace is "
                "trimmed. Any OTHER unknown topic is a hard error naming the ones that exist, "
                "never a nearest guess and never a silent fall back to the whole guide."
            )
        ),
    ] = None,
) -> ToolResult:
    """The operational cookbook of this surface: service planes, recipes, error signatures.

    Call it before deciding how to attack an operational question. The individual tool
    descriptions tell you how to CALL one, which is a different question; this says which chain
    answers which question, what an error signature means, and what the planes can and cannot
    prove. It reads nothing but its own packaged document: no PV, no REST plane, no file of
    yours, so it can neither time out nor depend on how this instance is configured.

    The whole document is around 85 KB, which is why 'topic' exists and why omitting it should be
    the exception. The keys partition the document: each returns exactly its own part, VERBATIM,
    so an excerpt is a real excerpt and never a rendering, and the largest single part is under a
    third of the whole. An unknown topic is refused with [UNKNOWN_TOPIC] naming every valid key.

    The same text is also served as the resource epics-pv://guide, for a human or an application.
    An application has to ask for it, though, which is why this tool exists beside it.
    """
    # ⛔ ToolResult with ONE text block and no structuredContent, deliberately. The guide is a
    # document to READ, not a measurement to index: beside the text there is no field a caller
    # could want. And the choice is measured rather than tasteful. A ``-> str`` return makes
    # FastMCP 3.4.4 wrap the value in ``{"result": ...}`` and send it BOTH ways, so the document
    # crosses the wire twice, the second copy JSON-escaped and therefore longer, so one call costs
    # a little over TWICE the document versus once for the shape below. Deliberately a ratio and
    # not a byte count: the exact figure stood here and in two other files, was wrong within a day
    # of being written, and nothing re-ran it. The measured version lives in
    # tests/test_guide_tool.py::test_the_rounded_size_claims_still_hold.
    return ToolResult(content=[TextContent(type="text", text=_serve_guide(topic))])


@mcp.tool(
    output_schema=None,
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@translate_epics_errors
async def get_pv_value(
    pv_name: Annotated[str, Field(description="EPICS PV name")],
    # gt=0 on every timeout in this file (QA-71). A zero timeout was assumed to be the honest
    # counterpart to QA-65's caps, raising rather than fabricating a success. Measured over every
    # tool that lacked the bound, that was true for only part of them: the rest answered
    # plausibly instead (find_device reported "no screen references this device", validate_pvs
    # reported the PV as disconnected). Refusing at the boundary is the reading that is right
    # either way. The counts stay in the CHANGELOG and in the guard's docstring, where a stale
    # one is visible; a statistic does not belong at a call site.
    timeout: Annotated[
        float | None,
        Field(description="Timeout in seconds (default: EPICS_MCP_DEFAULT_TIMEOUT)", gt=0),
    ] = None,
) -> dict[str, object]:
    """Get the current value of an EPICS Process Variable.

    The result carries the same best-effort metadata as get_pv_info
    (alarm/timestamp/display/control/value_alarm/enum), including which alarm level says what."""
    return await _get_pv_value(pv_name, timeout)


@mcp.tool(
    output_schema=None,
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@translate_epics_errors
async def get_pvs(
    pv_names: Annotated[
        list[str],
        Field(
            description="List of PV names to read (capped at the server's max_batch_size, "
            "default 100, EPICS_MCP_MAX_BATCH_SIZE)"
        ),
    ],
    timeout: Annotated[
        float | None,
        Field(description="Timeout in seconds per PV (default: EPICS_MCP_DEFAULT_TIMEOUT)", gt=0),
    ] = None,
) -> dict[str, object]:
    """Batch-read multiple EPICS PVs in a single call.

    Each result carries the same best-effort metadata as get_pv_info
    (alarm/timestamp/display/control/value_alarm/enum), including which alarm level says what.
    A per-PV read failure lands in the errors
    list; a structural provider fault, the native batch returning a different number of values than
    requested, surfaces loudly as [UPSTREAM_CONTRACT_ERROR] rather than silently dropping PVs."""
    return await _get_pvs(pv_names, timeout)


@mcp.tool(
    output_schema=None,
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
    # Advisory, CLIENT-side consent hint (NOT the load-bearing guard). A client that honours it
    # obtains a human approval before each write; an older / other MCP client ignores it silently.
    # The load-bearing, client-INDEPENDENT guard stays server-side: three gate checks (env gate,
    # regex allowlist, rate-limit) plus the mandatory audit, which records a verdict rather than
    # reaching one, and the post-admission drive-limit refusal, which is not a gate check either
    # because it has already spent a rate token (see safety.py). Kept red-provable by the
    # consent-invariant test.
    meta={"anthropic/requiresUserInteraction": True},
)
@translate_epics_errors
async def set_pv_value(
    pv_name: Annotated[str, Field(description="EPICS PV name")],
    value: Annotated[str, Field(description="New value to set")],
    timeout: Annotated[
        float | None,
        Field(description="Timeout in seconds (default: EPICS_MCP_DEFAULT_TIMEOUT)", gt=0),
    ] = None,
) -> dict[str, object]:
    """Set a PV value. Requires EPICS_MCP_ALLOW_PV_WRITE=true.

    Protected by safety layer: environment gate, regex allowlist, rate-limit (10/min default),
    audit logging to a durable path (EPICS_MCP_AUDIT_LOG_FILE) and a loopback-only EPICS client
    search reach; a write-enabled server refuses to start without either of the last two. The
    load-bearing, client-independent guard on whether this SERVER attempts the write. Whether it
    LANDS is the IOC's own access security, which this server does not model: an allowlisted name
    is our policy, not a promise about the record. See the epics-pv://guide resource.

    Value bounds (always-on, pre-put): the written value is checked against the record's OWN drive
    limits (control DRVL/DRVH, read on the pre-read). An out-of-range value is denied with
    error_code PV_WRITE_OUT_OF_BOUNDS BEFORE the put, it never reaches the IOC, and emits a
    BOUNDS_DENY audit event. A record that declares no drive limits (or a
    non-numeric value) is not bounds-checkable; the write proceeds and ``bounds_note`` says so.

    Readback verification (always-on): after the write the value is read back and compared against
    what was written. The result carries ``verified`` (true = within tolerance / false = mismatch /
    null = not verifiable, e.g. a readback timeout), plus ``readback``, ``tolerance`` and ``note``,
    and a ``READBACK_OK``/``READBACK_MISMATCH``/``READBACK_UNVERIFIED`` audit line follows the
    ``ALLOW``. A mismatch does NOT raise, the put happened (``status`` stays ``"success"``); the
    loud signal is ``verified=false`` plus the audit event, so a wrong or not-landed value is
    surfaced rather than accepted silently. Tolerance is the record's ``control.min_step`` when it
    has one (> 0), else ``EPICS_MCP_READBACK_TOLERANCE``.

    On an enum PV a written LABEL is compared by index, exact and without a tolerance: it is
    resolved against the record's own choices the way the write path resolves it (case-sensitive,
    first match wins), and ``readback`` stays the index, as get_pv_value reports it. Writing the
    INDEX instead stays on the numeric comparison above, tolerance included. One consequence to
    expect on a command record that clears itself after the pulse: if it has already cleared when
    the readback arrives, it reads back its idle state and the verdict is a mismatch, which is what
    the readback saw rather than a statement that the command failed.

    Client-side consent hint (advisory, NOT a gate): the tools/list entry carries
    _meta["anthropic/requiresUserInteraction"]=true. A client that honours it prompts a human
    before every write - even under bypassPermissions - and, on a recognising client, fails closed:
    a non-interactive run denies the call rather than writing silently, so a headless write needs a
    reachable human (or the client's programmatic approval callback). An older or non-recognising
    client ignores the hint; the server-side safety layer above is what actually gates the CALL,
    with the IOC deciding the write itself as described there.
    """
    return await _set_pv_value(pv_name, value, timeout)


@mcp.tool(
    output_schema=None,
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@translate_epics_errors
async def get_pv_info(
    pv_name: Annotated[str, Field(description="EPICS PV name")],
    timeout: Annotated[
        float | None,
        Field(description="Timeout in seconds (default: EPICS_MCP_DEFAULT_TIMEOUT)", gt=0),
    ] = None,
) -> dict[str, object]:
    """Get detailed PV metadata: value, alarm (severity/status incl. text + message),
    timestamp, display (units/limits/precision OR format/description), control (drive
    limits), value_alarm (active flag + the configured HIHI/HIGH/LOW/LOLO limits; NaN/unset
    limits and the per-PVA-unmapped per-level severities are omitted), and enum index/label/
    choices for enum PVs. Unset (zero-width) display/control limit pairs are omitted; DBR_CHAR
    waveforms come back as int lists.

    The two alarm levels do not mix, and reading the wrong one gives the wrong cause.
    alarm.status_text is the coarse pvData NT category of the alarm SOURCE, one of eight values
    (NONE, DEVICE, DRIVER, RECORD, DB, CONF, UNDEFINED, CLIENT), and it is NEVER the fine CA STAT
    condition. A threshold or state name (HIHI, LOLO, UDF, SIMM and their siblings) reaches you as
    plain text in alarm.message instead: the status says WHERE the alarm came from, the message says
    WHY. So do not expect a threshold name in status_text, and do not map one into it.
    ⚠ alarm.message is whatever the server sent and is often empty even on a real alarm. When it is,
    the WHY is not in this reply at all: severity_text still says how bad it is, and the thresholds
    themselves can be read as record fields (get_pv_info("PV.HIHI") and its siblings). Say the cause
    is unreported rather than reaching for status_text, which answers a different question.

    Record fields read directly: pass a channel with a field suffix (e.g. get_pv_info("PV.RTYP"),
    "PV.SCAN", "PV.HIHI") to read individual record metadata / alarm thresholds, useful when a PVA
    gateway serves the NT value_alarm limits as NaN. A cold (first) connection can need a longer
    timeout: default_timeout stays 5 s but pass timeout>=8 for the first read of an idle PV."""
    return await _get_pv_info(pv_name, timeout)


@mcp.tool(
    output_schema=None,
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
@translate_epics_errors
async def monitor_pv(
    pv_name: Annotated[str, Field(description="EPICS PV name to monitor")],
    duration: Annotated[
        float,
        # gt=0: this is a collection WINDOW, not a timeout. Zero would end the subscription
        # before any update could arrive and answer with a successful empty result, which is
        # the very ambiguity the connection field below exists to remove.
        Field(
            description="Duration in seconds to monitor (clamped to the server's "
            "max_monitor_duration, default 60, EPICS_MCP_MAX_MONITOR_DURATION)",
            gt=0,
        ),
    ] = 10.0,
    max_events: Annotated[
        int,
        # ge=1: a non-positive cap is meaningless, it would empty a valid response and the client
        # would then mislabel it. The same reason its four capped siblings carry, and here it is
        # literal: min(0, max_monitor_events) is 0, so the stream is discarded and the answer
        # reads exactly like a PV that had nothing to say.
        Field(
            description="Maximum events to collect (clamped to the server's max_monitor_events, "
            "default 1000, EPICS_MCP_MAX_MONITOR_EVENTS)",
            ge=1,
            le=100000,
        ),
    ] = 100,
) -> dict[str, object]:
    """Subscribe to PV changes for a given duration and return collected events.

    Each event carries the same best-effort metadata as get_pv_info
    (alarm/timestamp/display/control/value_alarm/enum), including which alarm level says what.

    Also returns ``connection`` (connected/disconnected/unknown), so an empty ``events`` list
    says which it is: a quiet PV, or one that was never reachable. When there is something to
    explain, ``connection_detail`` carries one sentence saying what."""
    return await _monitor_pv(pv_name, duration, max_events)


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@translate_epics_errors
async def discover_pvs(
    pattern: Annotated[
        str,
        Field(
            description=(
                "Concrete PV name, or a wildcard glob (* ?) resolved via ChannelFinder, the glob "
                "is ANCHORED and CASE-INSENSITIVE: a bare substring matches nothing, wrap it in * "
                "to match inside a name"
            )
        ),
    ],
    timeout: Annotated[
        float | None,
        Field(description="Timeout in seconds (default: EPICS_MCP_DEFAULT_TIMEOUT)", gt=0),
    ] = None,
) -> DiscoverPvsResult:
    """Discover PVs by name.

    A CONCRETE name is connected via p4p (status found/not_found/timeout/error, plus the value on a
    hit). A WILDCARD pattern (* ? [ ]) is delegated to ChannelFinder, the runtime PV registry: each
    hit is a REGISTERED channel with status 'registered', registry membership, NOT a live connect,
    so use get_pvs/get_pv_value for liveness, carrying ioc_name/host_name and an honest 'capped'
    when a broad glob truncates the registry. The wildcard glob is interpreted by the ChannelFinder
    SERVER and is ANCHORED + CASE-INSENSITIVE (measured live 2026-07-15): a bare substring matches
    nothing, so wrap it in *. Wildcard discovery needs ChannelFinder (EPICS_MCP_CHANNELFINDER_URL);
    with it unset the wildcard branch returns an honest 'requires ChannelFinder' note rather than a
    bare empty result that would read as 'no such PV'.
    """
    return await _discover_pvs(pattern, timeout)


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@translate_epics_errors
async def find_channels(
    name_pattern: Annotated[
        str,
        Field(
            description=(
                "Channel/PV name glob (ChannelFinder syntax: * and ?). ANCHORED and "
                "CASE-INSENSITIVE: a bare substring matches nothing, wrap it in * to search "
                "inside a name."
            )
        ),
    ],
    max_results: Annotated[
        int,
        # ge=1: same reason as the siblings, and here the empty response would be actively
        # misleading. The service over-fetches by one to report `capped` honestly, so a cap of 0
        # returns NO channels together with capped=true, i.e. "there is more" attached to nothing.
        Field(
            description="Cap on returned channels (a broad glob can match a whole site)",
            ge=1,
            le=100000,
        ),
    ] = 500,
    timeout: Annotated[float, Field(description="Timeout in seconds", gt=0)] = 5.0,
    has_properties: Annotated[
        dict[str, str] | None,
        Field(
            description=(
                "Property filter {name: value-glob}; use '*' as the value for 'property present, "
                "any value'. Distinct properties are AND-ed. Gated to the safe-property allowlist."
            )
        ),
    ] = None,
    lacks_properties: Annotated[
        list[str] | None,
        Field(description="Property names that must be ABSENT (e.g. PVs with no 'aa_policy')."),
    ] = None,
    not_property_values: Annotated[
        dict[str, str] | None,
        Field(
            description=(
                "Filter {name: value}: has the property with value != value. NOTE: a channel that "
                "LACKS the property does NOT match (combine with lacks_properties for the true "
                "complement, which needs two calls)."
            )
        ),
    ] = None,
    has_tags: Annotated[
        list[str] | None,
        Field(
            description=(
                "Required tags: ANY-of / OR (a channel with any listed tag matches). "
                "Tag-filter semantics UNVERIFIED server-side until a live probe."
            )
        ),
    ] = None,
    lacks_tags: Annotated[
        list[str] | None,
        Field(
            description=(
                "Excluded tags, a channel with any listed tag is dropped. "
                "Tag-filter semantics UNVERIFIED server-side until a live probe."
            )
        ),
    ] = None,
    count_only: Annotated[
        bool,
        Field(
            description=(
                "Return only the exact match count, {enabled, match_count} (plus a note when "
                "ChannelFinder is unconfigured), via the /count endpoint, instead of the channel "
                "list, for 'how many match' without pulling them."
            )
        ),
    ] = False,
) -> ChannelQueryResult:
    """Query ChannelFinder: which IOC/host serves a PV, plus its tags/properties.

    Read-only. Disabled by default (set EPICS_MCP_CHANNELFINDER_URL to enable).

    The two modes return DISJOINT fields, and the four paths differ further: configured list =
    {enabled, channels, total, capped}, configured count = {enabled, match_count}; with
    ChannelFinder UNCONFIGURED it is {enabled, channels, total, note} and
    {enabled, match_count, note}, i.e. `note` is added and `capped` is NOT emitted. Only
    `enabled` is present on every path; read the advertised outputSchema rather than assuming a
    field is there.

    The glob is matched by the SERVER, and both of its properties bite silently (measured live
    2026-07-15). It is ANCHORED: 'Ctrl-EVR-01' matches 0 channels while '*Ctrl-EVR-01*' matches
    them all, so a bare substring reads as 'no such channel' rather than as a syntax mistake.
    And it is CASE-INSENSITIVE: '*temp*', '*Temp*' and '*TEMP*' return the identical set, so a
    hit may differ in case from what was asked (e.g. '*Temp*' matching '...MorTemPrd').

    Optional MA-2 property/tag filters narrow the search server-side, and count_only returns the
    exact match count. CAVEATS: (1) property filtering is gated to the DS-privacy safe-property
    allowlist (a redacted property like accessGroup is refused, filtering it would reconstruct the
    partition the projection hides). EPICS_MCP_CHANNELFINDER_SAFE_PROPERTY_NAMES REPLACES that
    allowlist rather than extending it, and it also decides which properties the results carry, so
    naming one extra property silently drops the built-in ones: list them alongside it to keep them.
    (2) A silent 0 is not the same thing as a refusal. A property name NOT on the allowlist,
    which every misspelling is, is refused client-side with INVALID_INPUT before the request
    leaves; an ALLOWLISTED name this instance does not carry is NOT a server error, it narrows
    the result to 0, indistinguishable from a genuinely empty match. Tag names are never
    allowlisted, so a merely misspelled tag is that same silent 0; only a MALFORMED one is
    refused (blank, leading ~, trailing !, or the same tag in has_tags and lacks_tags), which is
    a syntax check rather than the allowlist. list_channel_vocabulary names the
    keys this instance actually accepts. (3) The PROPERTY filters and count_only were
    differentially live-verified (2026-07-22); the TAG filters (has_tags/lacks_tags) remain
    UNVERIFIED against a live server until a probe exercises them.

    A malformed registry record (a non-dict element, or one without a usable name) raises a loud
    error, records are never silently dropped into a smaller, fabricated answer.
    """
    return await _find_channels(
        name_pattern,
        max_results,
        timeout,
        has_properties=has_properties,
        lacks_properties=lacks_properties,
        not_property_values=not_property_values,
        has_tags=has_tags,
        lacks_tags=lacks_tags,
        count_only=count_only,
    )


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@translate_epics_errors
async def list_channel_vocabulary(
    timeout: Annotated[float, Field(description="Timeout in seconds", gt=0)] = 5.0,
) -> ChannelVocabularyResult:
    """List which property keys and tag names you can filter find_channels on.

    Read-only. Disabled by default, returns enabled=false unless EPICS_MCP_CHANNELFINDER_URL is
    set. Answers "what can I hand to find_channels' has_properties / lacks_properties /
    not_property_values (property keys) and has_tags / lacks_tags (tag names)?" as
    {enabled, properties, tags}, NAMES only (the DS-privacy owner and value are never surfaced).

    properties is the allowlisted subset that actually exists in this ChannelFinder: it lists only
    the safe-property names find_channels accepts as filters (a non-allowlisted, person-bearing
    property like ENGINEER is excluded and would be refused anyway).
    EPICS_MCP_CHANNELFINDER_SAFE_PROPERTY_NAMES REPLACES that allowlist rather than extending it, so
    a comma-list naming one extra property silently drops the built-in ones: list them alongside it
    to keep them. tags is the full, ungated server
    tag set. An empty list means the CF instance has no such names; enabled=false (with a note)
    means CF is not configured, the two are distinct. An unreadable/unreachable listing raises
    loudly rather than reporting an empty vocabulary.
    """
    return await _list_channel_vocabulary(timeout)


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
            description="ESS device name (the device part of a PV, without the trailing property, "
            "e.g. DEV-TEST01:Ctrl-EVR-01)"
        ),
    ],
    timeout: Annotated[float, Field(description="Timeout in seconds", gt=0)] = 5.0,
) -> NameLookupResult:
    """Look up an ESS device name in the Naming Service: is it registered and ACTIVE?

    Read-only. Disabled by default, returns enabled=false unless EPICS_MCP_NAMING_URL is set (no
    ESS egress otherwise). A reachable service answering "not registered", HTTP 204, the signal
    the real service actually sends (measured 2026-07-16), or HTTP 404, yields registered=false, a
    DEFINITIVE answer, but ONLY once the responder proves it is the Naming Service via its
    /rest/swagger.json identity beacon (S13): a foreign/misconfigured URL whose 404 cannot be
    identity-confirmed is WITHHELD, not minted into a false registered=false (which would surface
    downstream as a spurious name_typo in diagnose_connection). OBSOLETE/DELETED also yield
    registered=false with the status preserved. A service/URL failure (unreachable, 5xx, bad JSON,
    timeout), a 2xx record without a readable status, AND an unverified identity are all WITHHELD
    (registered=null + withheld=true). A registered/ACTIVE answer is returned WITHOUT an identity
    probe (the measured hazard is a foreign 404, not a foreign ACTIVE record, out of scope, S13).
    Surfaces only registered/status/message. Unlike diagnose_connection this needs no live PV probe.
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
    pv_name: Annotated[str, Field(description="EPICS PV name")],
    timeout: Annotated[float, Field(description="Timeout in seconds", gt=0)] = 5.0,
) -> ArchiveStatusResult:
    """Report whether a PV is being archived (EPICS Archiver Appliance MGMT getPVStatus).

    Read-only. Disabled by default, returns enabled=false unless EPICS_MCP_ARCHIVER_URL is set.
    Beyond archived/status the result surfaces the MGMT record's connection_state (source IOC
    connected now?), last_event (time of the last archived sample), is_monitored, sampling_period
    and appliance when present, plus the connection-history cluster connection_loss_regain_count
    ("does it flap?"), connection_first_established and connection_last_restablished (first/last
    (re)connect time, "Never" if it never dropped) for an archived PV, one getPVStatus call, no
    extra cost. An unreadable getPVStatus payload raises a loud error, never a fabricated
    archived=false: the appliance answers even an UNKNOWN pv with a real record (status "Not being
    archived", measured), so that record is the only definitive negative.

    is_archived answers only for a NAMED PV. To ENUMERATE the archived PVs use list_archived_pvs.
    See the epics-pv://guide resource.
    """
    return await _is_archived(pv_name, timeout)


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
    pv_name: Annotated[str, Field(description="EPICS PV name")],
    start: Annotated[
        str, Field(description="Window start, ISO-8601 (e.g. 2026-06-01T00:00:00.000Z)")
    ],
    end: Annotated[str, Field(description="Window end, ISO-8601")],
    max_points: Annotated[
        int,
        # ge=1: a non-positive cap is meaningless, it would empty a valid response and the client
        # would then mislabel it "withheld". le caps an absurd inline pull (page by window instead).
        Field(
            description="Cap on returned samples (a wide window on a fast PV is unbounded)",
            ge=1,
            le=100000,
        ),
    ] = 5000,
    timeout: Annotated[float, Field(description="Timeout in seconds", gt=0)] = 5.0,
) -> ArchiverHistoryResult:
    """Fetch archived samples for a PV over an ISO-8601 window (Archiver retrieval getData.json).

    Read-only. Disabled by default, returns enabled=false unless EPICS_MCP_ARCHIVER_URL is set. The
    result includes the getData.json meta block (PV metadata such as EGU units and PREC precision)
    alongside the samples. capped is true when the window held more than max_points samples. status
    disambiguates an empty result: "ok" (samples returned), "empty" (a valid but sample-less window)
    or "withheld" (an unreadable response, with a withheld_reason), so an empty samples list is
    never mistaken for "no data" when the truth is "could not read". A single unreadable sample
    in the data array withholds the WHOLE result (it is never silently skipped or zero-filled
    into a plausible sample).
    """
    return await _get_pv_history(pv_name, start, end, max_points, timeout)


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@translate_epics_errors
async def get_archive_info(
    pv_name: Annotated[str, Field(description="EPICS PV name")],
    timeout: Annotated[float, Field(description="Timeout in seconds", gt=0)] = 5.0,
) -> ArchiveInfoResult:
    """Report HOW a PV is archived, its archive configuration (Archiver MGMT getPVTypeInfo).

    Read-only. Disabled by default, returns enabled=false unless EPICS_MCP_ARCHIVER_URL is set.
    Complements is_archived (live connection state) and get_pv_history (the samples): surfaces the
    archive CONFIGURATION, sampling method/period, retention (the STS/MTS/LTS data_stores),
    computed event/storage rates, dbr_type, archived fields, source host_name and creation_time,
    the alarm/display/control limits + units/precision (upper_alarm_limit=HIHI, upper_warning_limit=
    HIGH, lower_* likewise, *_display_limit=HOPR/LOPR, *_ctrl_limit=DRVH/DRVL, precision, units=EGU)
    and controlling_pv/policy_name/modification_time. NOTE: the nine numeric limits are always
    present and read "0.0" when the PV had no ctrl info, "0.0" may mean "no limit configured", not
    a literal zero.
    found is false when the appliance has no type-info record for the PV (unknown / never
    archived), it signals that with HTTP 404 and ONLY that; an unreadable 2xx raises a loud
    error instead of a false found (neither a fabricated "not archived" nor, for an unrelated
    body, a fabricated found=true).

    get_archive_info answers only for a NAMED PV; list_archived_pvs enumerates them.
    See epics-pv://guide.
    """
    return await _get_archive_info(pv_name, timeout)


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@translate_epics_errors
async def get_appliance_info(
    timeout: Annotated[float, Field(description="Timeout in seconds", gt=0)] = 5.0,
) -> ApplianceInfoResult:
    """Report the Archiver Appliance's own topology (Archiver MGMT getApplianceInfo).

    Read-only. Disabled by default, returns enabled=false unless EPICS_MCP_ARCHIVER_URL is set.
    Names WHICH appliance this is (identity) and where each plane is served (mgmt_url/engine_url/
    etl_url/retrieval_url/data_retrieval_url, cluster_inet_port) plus a version string. Answers two
    questions the per-PV archiver tools cannot: "am I pointed at the intended cluster before I trust
    list_archived_pvs / get_pv_history (enumerating the wrong cluster silently yields a
    complete-looking list of the WRONG PVs)?" and "is this a split/proxied deployment, which plane
    is served where (the mgmt- vs retrieval-webapp question)?". No pv arg and no found key: the
    appliance answers or the call errors. version is OMITTED when the appliance lacks it (not
    "always present"); a 404 here means the WRONG endpoint (retrieval serves /retrieval/bpl, not
    /mgmt/bpl), not "no appliance". See epics-pv://guide.
    """
    return await _get_appliance_info(timeout)


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@translate_epics_errors
async def list_archived_pvs(
    pattern: Annotated[
        str | None,
        Field(
            description="Optional PV-name glob (e.g. 'DEV-TEST01:*'); omit to list all. "
            "Cannot be combined with this_appliance=true, that endpoint has no name filter"
        ),
    ] = None,
    this_appliance: Annotated[
        bool,
        Field(
            description="List only THIS cluster member (getPVsForThisAppliance) instead of all. "
            "This endpoint cannot filter by name, leave pattern unset"
        ),
    ] = False,
    limit: Annotated[
        int,
        # ge=1: a non-positive cap is meaningless, a negative limit would make the client's
        # names[:limit] slice silently DROP names and falsely report capped. le caps an absurd pull.
        Field(
            description="Cap on returned PV names (a whole appliance can hold tens of thousands)",
            ge=1,
            le=100000,
        ),
    ] = 5000,
    timeout: Annotated[float, Field(description="Timeout in seconds", gt=0)] = 5.0,
) -> ArchivedPvsResult:
    """List the PV names the Archiver Appliance archives (Archiver MGMT getAllPVs).

    Read-only. Disabled by default, returns enabled=false unless EPICS_MCP_ARCHIVER_URL is set.
    Uses getAllPVs (whole appliance) or, with this_appliance=true, getPVsForThisAppliance (this
    cluster member), NOT getMatchingPVs, which 404s on split/proxied deployments.

    pattern is an optional name glob and works ONLY with this_appliance=false (it maps to getAllPVs'
    pv param). getPVsForThisAppliance has NO name filter at all, so pattern together with
    this_appliance=true is REFUSED (INVALID_ARGUMENT) rather than ignored, the endpoint would
    otherwise return a full, plausible list of the WRONG PVs. To filter by name, drop
    this_appliance.

    capped is true when the appliance held more than limit names (honest over-fetch). PV names carry
    no person data, no redaction needed.
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
    pv_name: Annotated[str, Field(description="EPICS PV name")],
    config_name: Annotated[
        str,
        Field(
            description=(
                "Alarm config-tree name: REQUIRED, no default (the trees are site-specific, so "
                "there is no correct universal default; a guessed one silently matches nothing). "
                "Top-level topic selecting the ES index. CASE-SENSITIVE: a wrong or mis-cased name "
                "yields configured=null (withheld), never false. Names unknown? Call "
                "get_alarm_history WITHOUT root and read the first path segment of each event's "
                "config field; see the guide recipe 'Discover the alarm config-tree names', which "
                "also says why a correct name can still be withheld."
            )
        ),
    ],
    timeout: Annotated[float, Field(description="Timeout in seconds", gt=0)] = 5.0,
) -> AlarmConfiguredResult:
    """Report whether a PV has an alarm configuration (Phoebus Alarm Logger /search/alarm/config).

    Read-only. Disabled by default, returns enabled=false unless EPICS_MCP_ALARM_URL is set.
    A hit proves the PV is configured in the alarm tree; a miss is a real negative only when the
    Alarm Logger was running at config-import time (else the config change never reached its index).

    configured is true / false / null, and null means WITHHELD, the tree itself returned nothing,
    so 'this PV is not configured' cannot be told apart from 'that is not the tree name'; a note
    then says so. An unreadable payload or record raises a loud error instead of falling through
    to the tree probe as a false negative. config_name is CASE-SENSITIVE even though the server
    lower-cases it to pick the index (measured live 2026-07-15: 'accelerator' selects the right
    index and matches nothing, exactly like an unconfigured PV). The returned
    config field echoes your input, it is NOT the server confirming the tree exists.
    """
    return await _is_alarm_configured(pv_name, config_name, timeout)


# MA-2b(c): the 9 EPICS alarm severities (Phoebus SeverityLevel), the definitional value set of the
# alarm-logger `severity`/`current_severity` keyword fields. A Literal at the boundary is Tier 1
# (structurally rejects a typo the server would otherwise silently ignore, broadening the result).
_AlarmSeverity = Literal[
    "OK",
    "MINOR",
    "MAJOR",
    "INVALID",
    "UNDEFINED",
    "MINOR_ACK",
    "MAJOR_ACK",
    "INVALID_ACK",
    "UNDEFINED_ACK",
]
# MA-2b(b): the alarm-logger `command` param honours ONLY Enabled/Disabled (mapped to the `enabled`
# field true/false); any other value is a silent server-side no-op, so restrict it structurally.
_AlarmCommand = Literal["Enabled", "Disabled"]


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@translate_epics_errors
async def get_alarm_history(
    pv_name: Annotated[
        str,
        Field(
            description="EPICS PV / device name (matched as a wildcard substring on the alarm "
            "config path; each event carries its own pv/config so over-matches stay visible)"
        ),
    ],
    start: Annotated[
        str,
        Field(
            description="Window start (REQUIRED), absolute (ISO-8601, e.g. 2026-06-01T00:00:00Z) "
            "or a single relative amount (e.g. '8 hours', '2 days'). No months/years, use days "
            "or weeks."
        ),
    ],
    end: Annotated[
        str,
        Field(
            description="Window end (REQUIRED), absolute (ISO-8601) or a single relative amount "
            "(e.g. 'now')"
        ),
    ],
    max_events: Annotated[
        int,
        # le=999, not 1000: the client requests size=max_events+1 so `capped` is an honest
        # fetched>max_events. The Alarm Logger's default es_max_size is 1000 and it clamps
        # size=min(es_max_size, requested); capping max_events at 999 keeps size<=1000 so the +1
        # probe still fits under the DEFAULT ceiling. (A backend configured with a lower es_max_size
        # can still under-report capped, documented on AlarmClient.get_alarm_history.)
        Field(description="Cap on returned events, newest first", ge=1, le=999),
    ] = 100,
    root: Annotated[
        str | None,
        Field(
            description=(
                "Filter to alarm config tree(s), comma-separated (top-level topic names). "
                "SERVER-SIDE + UNVERIFIED: a logger that does not support it silently IGNORES it "
                "and broadens the result (no error). Omit (None) to search all trees."
            )
        ),
    ] = None,
    command: Annotated[
        _AlarmCommand | None,
        Field(
            description=(
                "Filter to config changes that Enabled/Disabled the alarm (maps to the `enabled` "
                "field; results are restricted to config-change docs so state events do not swamp "
                "them). SERVER-SIDE + UNVERIFIED. Omit (None) for no command filter."
            )
        ),
    ] = None,
    severity: Annotated[
        _AlarmSeverity | None,
        Field(
            description=(
                "Filter on the alarm severity field (one of the 9 EPICS severities). SERVER-SIDE + "
                "UNVERIFIED (an unsupported value is silently ignored and broadens). Omit for any."
            )
        ),
    ] = None,
    current_severity: Annotated[
        _AlarmSeverity | None,
        Field(
            description=(
                "Filter on the CURRENT alarm severity field (9 EPICS severities). SERVER-SIDE + "
                "UNVERIFIED. Omit for any."
            )
        ),
    ] = None,
    timeout: Annotated[float, Field(description="Timeout in seconds", gt=0)] = 5.0,
) -> AlarmHistoryResult:
    """Fetch the alarm state history of a PV over a window (Phoebus Alarm Logger /search/alarm).

    Read-only. Disabled by default, returns enabled=false unless EPICS_MCP_ALARM_URL is set. start
    and end are required (a defaultless query must not pull the whole history). The stream carries
    alarm STATE changes and also alarm-CONFIG-change messages (the config field prefix
    state:/config: distinguishes them). Events are newest first and carry the known
    AlarmLogMessage fields, incl. user/host (who acknowledged/enabled/disabled), command and
    config_msg; a field a future logger version adds is dropped (known-field allowlist). capped
    is true when more than max_events matched. An unreadable payload or record raises a loud
    error, never an empty result that reads as 'nothing alarmed'.

    Time window: an absolute value is normalized to zone-explicit UTC before sending (a naive one
    is read as UTC); a single relative amount ('8 hours', 'now') passes through. A value the server
    would misread is rejected before any request rather than sent: the Alarm Logger does not reject
    an unreadable time, it silently takes it as 'now' and answers 200 with an empty list that is
    indistinguishable from 'nothing alarmed'.

    Optional server-side filters narrow the search: root = alarm config tree(s); command =
    Enabled/Disabled config changes (restricted to config-change docs); severity/current_severity =
    the alarm severity. ⚠ These are SERVER-DECIDED and UNVERIFIED, the Alarm Logger silently
    IGNORES a filter it does not support and BROADENS the result instead of erroring, so a returned
    set can be wider than the filter implies until the running server's support is probed. The
    command values (Enabled/Disabled) and the 9 severities ARE the definitional value set and are
    Literal-enforced at this boundary.
    """
    return await _get_alarm_history(
        pv_name,
        start,
        end,
        max_events,
        timeout,
        root=root,
        command=command,
        severity=severity,
        current_severity=current_severity,
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
            description="Window start, an absolute time (ISO-8601, e.g. '2026-07-15T10:00:00Z') "
            "or a single amount ('7 days', '90 min'). No months/years, use days or weeks."
        ),
    ] = None,
    end: Annotated[
        str | None,
        Field(
            description="Window end, an absolute time (ISO-8601) or a single amount. "
            "Omit to search up to now."
        ),
    ] = None,
    size: Annotated[int, Field(description="Cap on returned entries", ge=1, le=200)] = 50,
    offset: Annotated[
        int, Field(description="0-based pagination offset, read past the first page", ge=0)
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
    level: Annotated[
        str | None,
        Field(
            description=(
                "Triage level(s) to filter by, e.g. 'Problem', comma/semicolon/pipe-separated "
                "for OR. Case-insensitive; '*' wildcards are honoured. Site-configurable: call "
                "list_log_levels for the valid values. An UNKNOWN level is not rejected by Olog, "
                "it returns 0 hits."
            )
        ),
    ] = None,
    title: Annotated[
        str | None,
        Field(
            description=(
                "Word(s) to match in the entry TITLE (not the body, that is `text`). "
                "Case-insensitive, whole words only: a word fragment matches nothing unless "
                "wildcarded ('att*'); several words are AND-ed. Quote a phrase to match in "
                "order."
            )
        ),
    ] = None,
    timeout: Annotated[float, Field(description="Timeout in seconds", gt=0)] = 5.0,
) -> OlogSearchResult:
    """Search the Phoebus Olog electronic logbook (Olog REST /logs/search).

    Read-only. Disabled by default, returns enabled=false unless EPICS_MCP_OLOG_URL is set.
    Entries come back WHOLE: id, dates, level, state, title, description, owner (the author),
    source and properties, plus the derived name-only logbook/tag lists and attachment_count.
    A body arrives in two shapes that are NOT interchangeable: source is what an author wrote,
    while description is the server's rendered plain text of it. Rendering is one-way, so whatever
    it dropped cannot be recovered from the result. To feed a body back into update_log_entry, read
    source, not description. An entry written by an old client can carry no source at all.

    Time window: start/end take an absolute time (ISO-8601, normalized to UTC before sending;
    a naive value is read as UTC) or a single relative amount ('7 days', '90 min', 'now'). Months
    and years are NOT supported by Olog, use days or weeks. A value Olog could not read is
    rejected before any request rather than sent: Olog does not reject an unreadable time, it
    silently reads it as 'now' and answers 200 with an empty result that is indistinguishable
    from 'nothing matched'.

    Page the history with offset (0-based; Olog wire 'from') and order with sort ('down'=newest
    first, the default; 'up'=oldest first). sort only accepts those two values and is rejected
    otherwise, because Olog does not reject an unrecognized order: it silently applies 'up':
    the REVERSE of the documented default, and answers 200 with a well-formed page (measured
    live 2026-07-15: 'newest' and 'garbage' both returned oldest-first). total is the number of
    entries returned; total_matches is the true total across all pages (Olog hitCount); capped is
    true when more than size matched on this page. An unreadable payload or entry raises a loud
    error, never an empty result that reads as 'nothing matched'.

    Filter by triage level and by title with level/title. Both ARE honoured by the server and both
    are case-insensitive, probed differentially 2026-07-19 against a running Olog with a positive
    AND a negative control, plus a control showing that an ignored parameter returns the unfiltered
    count (Olog silently drops parameters it does not know, so "it returned results" proves
    nothing). level ORs over comma/semicolon/pipe; title matches whole WORDS, not substrings, a
    fragment finds nothing unless wildcarded with '*', several words are AND-ed, and it is a
    SEPARATE axis from text, which searches the body only and never the title.

    Caveat that the boundary cannot enforce: Olog does not reject a level it does not know, it
    answers 0 hits, so 'this level does not exist' and 'no entries have this level' look identical.
    A result where NOTHING matched therefore carries a note when the value does not name a
    configured level; call list_log_levels for the valid values. The note states a fact about the
    VALUE and does not claim to be the cause, another filter in the same search can produce the
    same 0, an OR-ed list still runs on its recognised parts, and a wildcard level is honoured by
    the server and so cannot be checked against the name list at all.

    A blank level/title is rejected before any request, because blank is never 'no filter' here and
    the two possible outcomes disagree: an empty-string level matches nothing (0 hits), while a
    separators-only value, or any blank title, makes Olog DROP the filter and return the
    UNFILTERED set as though it were filtered. Note that title splits on whitespace and on a literal
    '+' as well, so '+' is a blank title but an ordinary level.
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
        level=level,
        title=title,
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
) -> OlogEntryResult:
    """Fetch one Phoebus Olog entry by id (Olog REST /logs/{id}).

    Read-only. Disabled by default, returns enabled=false with found=null (the plane was NOT
    checked) unless EPICS_MCP_OLOG_URL is set. Same whole-entry shape as search_logbook (title,
    description, owner, source, properties, raw attachments list + attachment_count), carrying the
    same distinction between the body shapes: description is the server's rendered plain text,
    source is the raw body, so to edit this entry through update_log_entry, read source, not
    description. An entry written by an old client can carry no source at all.

    found is false ONLY on the service's definitive HTTP 404; an unreadable 2xx raises a loud
    error (it is neither a "not found" nor projected as a fabricated entry). NOTE: a real Olog
    answers 401 for an unknown id on this anonymous read path (measured 2026-07-16, its error
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
) -> OlogLogbooksResult:
    """List the valid Phoebus Olog logbook names (Olog REST /logbooks).

    Read-only. Disabled by default, returns enabled=false unless EPICS_MCP_OLOG_URL is set. Returns
    the logbook NAMES only (owners dropped), the valid values for search_logbook(logbooks=...).
    An unreadable listing raises a loud error, never an empty 'there are none'.
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
) -> OlogTagsResult:
    """List the valid Phoebus Olog tag names (Olog REST /tags).

    Read-only. Disabled by default, returns enabled=false unless EPICS_MCP_OLOG_URL is set. Returns
    the tag NAMES only, the valid values for search_logbook(tags=...). Tags carry no owner.
    An unreadable listing raises a loud error, never an empty 'there are none'.
    """
    return await _list_tags(timeout)


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
@translate_epics_errors
async def list_log_levels(
    timeout: Annotated[float, Field(description="Timeout in seconds", gt=0)] = 5.0,
) -> OlogLevelsResult:
    """List the valid Phoebus Olog log levels (Olog REST /levels).

    Read-only. Disabled by default, returns enabled=false unless EPICS_MCP_OLOG_URL is set. Levels
    are the logbook's TRIAGE axis (Info / Problem / Request / ... ) and are SITE-CONFIGURABLE, not a
    fixed enum, so this is the only way to learn the valid values for search_logbook(level=...) and
    for every write that takes one: create_log_entry, reply_to_log and update_log_entry. A Level
    carries no owner, so this is PII-free like list_tags.

    Call this BEFORE filtering a search by level: Olog does not reject a level it does not know, it
    answers 0 hits, so a typo reads exactly like 'there are no such entries'.

    default_level is the level a create uses when none is given. It is null, with a note saying why,
    whenever the server does not state it unambiguously (no level flagged, more than one flagged, or
    the flag unreadable), never guessed. An unreadable listing raises a loud error, never an empty
    'there are none'.
    """
    return await _list_log_levels(timeout)


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
        str, Field(description="Comma-separated target logbook name(s), must already exist")
    ],
    description: Annotated[str | None, Field(description="Log body / description text")] = None,
    level: Annotated[
        str | None,
        Field(
            description=(
                "Entry triage level, e.g. 'Info', server default when omitted. Site-configurable: "
                "call list_log_levels for the valid values. An unknown or blank level is REFUSED "
                "here (INVALID_INPUT) before the write: Olog itself stores it and answers 200, "
                "after which no level filter finds the entry. Matched exactly, no OR-separators, "
                "no wildcards, no case-folding (those are search semantics)."
            )
        ),
    ] = None,
    tags: Annotated[
        str | None, Field(description="Comma-separated tag name(s), must already exist")
    ] = None,
    attachments: Annotated[
        str | None,
        Field(
            description="Comma-separated workspace file path(s) to upload with the entry, any "
            "file type, up to EPICS_MCP_OLOG_ATTACH_MAX_BYTES total (default 50 MiB; "
            "create-with-attachments, PUT /logs/multipart)"
        ),
    ] = None,
    embed_image_base64: Annotated[
        str | None,
        Field(
            description="A single small base64-encoded image, uploaded and embedded inline in the "
            "body via ![](attachment/<id>), e.g. an opi-live take_screenshot PNG"
        ),
    ] = None,
    timeout: Annotated[float, Field(description="Timeout in seconds", gt=0)] = 5.0,
) -> OlogCreateResult:
    """Post a new entry to the Phoebus Olog electronic logbook (Olog REST PUT /logs).

    MUTATING. Disabled by default and behind its OWN gate (separate from set_pv_value), SIX checks
    in fixed order: at least one named target logbook AND EPICS_MCP_ALLOW_OLOG_WRITE=true AND a
    test-server URL boundary (only a loopback Olog, or an
    allowlisted https URL with EPICS_MCP_OLOG_WRITE_ALLOW_REMOTE=true) AND a logbook allowlist
    (EPICS_MCP_OLOG_WRITE_LOGBOOKS) AND an attachment size cap AND a rate limit; ALLOW_PV_WRITE is
    untouched. The first four are refused before any I/O, so a denied write leaves no trace on the
    server and never learns whether a file exists. The author
    (owner) is the configured write service account, set server-side; a caller cannot spoof it. The
    returned entry is the created entry WHOLE, so a write can verify what it just wrote. A write
    response that is not the created entry raises a loud error, it is never reported as a
    fabricated confirmation.
    With EPICS_MCP_OLOG_URL unset the tool returns enabled=false and makes no network call.

    With attachments (workspace file paths, any type/size) the entry is sent as multipart; their
    total size is capped (EPICS_MCP_OLOG_ATTACH_MAX_BYTES) and only HEIC is refused server-side. The
    response echoes attachments_uploaded (the {id, filename} of each).
    """
    return await _create_log_entry(
        title=title,
        logbooks=logbooks,
        description=description,
        level=level,
        tags=tags,
        attachments=attachments,
        embed_image_base64=embed_image_base64,
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
        str, Field(description="Comma-separated target logbook name(s), must already exist")
    ],
    description: Annotated[str | None, Field(description="Reply body / description text")] = None,
    level: Annotated[
        str | None,
        Field(
            description=(
                "Entry triage level, e.g. 'Info', server default when omitted. Site-configurable: "
                "call list_log_levels for the valid values. An unknown or blank level is REFUSED "
                "here (INVALID_INPUT) before the write: Olog itself stores it and answers 200, "
                "after which no level filter finds the entry. Matched exactly, no OR-separators, "
                "no wildcards, no case-folding (those are search semantics)."
            )
        ),
    ] = None,
    tags: Annotated[
        str | None, Field(description="Comma-separated tag name(s), must already exist")
    ] = None,
    attachments: Annotated[
        str | None,
        Field(
            description="Comma-separated workspace file path(s) to upload with the reply "
            "(any type, up to EPICS_MCP_OLOG_ATTACH_MAX_BYTES total, default 50 MiB)"
        ),
    ] = None,
    embed_image_base64: Annotated[
        str | None,
        Field(description="A single small base64-encoded image to embed inline in the reply body"),
    ] = None,
    timeout: Annotated[float, Field(description="Timeout in seconds", gt=0)] = 5.0,
) -> OlogCreateResult:
    """Reply to an existing Phoebus Olog entry (Olog REST PUT /logs?inReplyTo=log_id).

    MUTATING. Same gate, service account, and whole-entry response as create_log_entry, it threads
    the new entry to log_id via the Olog Log Entry Group. A reply is its own entry, so it carries
    its
    OWN attachments (workspace file paths, any type, capped by EPICS_MCP_OLOG_ATTACH_MAX_BYTES). A
    log_id that identifies no existing entry returns a clear HTTP 400 error. Disabled by default
    (needs EPICS_MCP_OLOG_URL + EPICS_MCP_ALLOW_OLOG_WRITE).
    """
    return await _reply_to_log(
        log_id=log_id,
        title=title,
        logbooks=logbooks,
        description=description,
        level=level,
        tags=tags,
        attachments=attachments,
        embed_image_base64=embed_image_base64,
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
async def add_log_attachment(
    log_id: Annotated[
        str, Field(description="Numeric id of the EXISTING Olog entry to attach the file(s) to")
    ],
    attachments: Annotated[
        str | None,
        Field(
            description="Comma-separated workspace file path(s) to attach, any type, up to "
            "EPICS_MCP_OLOG_ATTACH_MAX_BYTES total (default 50 MiB)"
        ),
    ] = None,
    embed_image_base64: Annotated[
        str | None,
        Field(description="A single small base64-encoded image to embed inline in the entry body"),
    ] = None,
    timeout: Annotated[float, Field(description="Timeout in seconds", gt=0)] = 5.0,
) -> OlogAddAttachmentResult:
    """Attach one or more files to an EXISTING Phoebus Olog entry (Olog REST POST /logs/multipart).

    MUTATING. Olog's update endpoint is destructive, it prunes any attachment
    not resubmitted and overwrites the entry's fields, so a safe attach round-trips the target
    entry's full content (read first). Same gate as
    create_log_entry, the same six checks (named target logbooks + env gate + test-server URL
    boundary + logbook allowlist + size cap + rate limit): the env gate
    and URL boundary are checked BEFORE the round-trip read, and the logbook
    allowlist is keyed on the TARGET entry's OWN logbooks. The attach is purely ADDITIVE:
    existing attachments and every CONTENT field are preserved, but the entry's OWNER is
    re-stamped with the write service account, because this endpoint IS the destructive update
    (the original author then survives only in the server-side archived version, which no tool
    here can read). Needs at least one attachment (attachments
    or embed_image_base64). With EPICS_MCP_OLOG_URL unset returns enabled=false.
    """
    return await _add_log_attachment(
        log_id=log_id,
        attachments=attachments,
        embed_image_base64=embed_image_base64,
        timeout=timeout,
    )


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False,
        # Destructive in the honest sense: it OVERWRITES fields of an existing entry (the previous
        # version survives only in the server's archive), unlike create/attach which only add.
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    )
)
@translate_epics_errors
async def update_log_entry(
    log_id: Annotated[str, Field(description="Numeric id of the EXISTING Olog entry to edit")],
    title: Annotated[
        str | None, Field(description="New title. Omit to leave unchanged; must not be empty")
    ] = None,
    description: Annotated[
        str | None,
        Field(
            description="New body text (CommonMark). Omit to leave unchanged; REPLACES the whole "
            "body. Editing an entry you just read: read source, not description. An entry's "
            "description is the server's RENDERED plain text of its body, so writing that value "
            "back drops the markup and the inline images the raw source carries, and the previous "
            "version is not reachable from this server. A body that starts with the entry's own "
            "rendering is reported back as a warning, but only that shape is detectable"
        ),
    ] = None,
    level: Annotated[
        str | None,
        Field(
            description=(
                "New entry triage level. Omit to leave unchanged. Site-configurable: call "
                "list_log_levels for the valid values. An unknown or blank level is REFUSED here "
                "(INVALID_INPUT) before the write: Olog validates neither, so an unknown one would "
                "be stored as a value no filter matches and a blank one would silently CLEAR the "
                "entry's level. Matched exactly, no OR-separators, no wildcards, no case-folding."
            )
        ),
    ] = None,
    logbooks: Annotated[
        str | None,
        Field(
            description="Comma-separated logbook names that REPLACE the entry's current logbooks "
            "(not merged). Omit to leave unchanged; at least one is required"
        ),
    ] = None,
    tags: Annotated[
        str | None,
        Field(
            description="Comma-separated tag names that REPLACE the entry's current tags (not "
            "merged). Omit to leave unchanged; pass an empty string to clear"
        ),
    ] = None,
    timeout: Annotated[float, Field(description="Timeout in seconds", gt=0)] = 5.0,
) -> OlogUpdateResult:
    """Edit an EXISTING Phoebus Olog entry's fields (Olog REST POST /logs/multipart).

    MUTATING. Olog's update is destructive, it prunes any attachment not
    resubmitted and NULLS any field not sent, so a safe edit round-trips the target entry's
    full content. This tool does
    that round-trip for you: any field you omit stays EXACTLY as it was, and attachments and
    properties are preserved. Same gate as create_log_entry, all six checks (two of them, the env
    gate and the test-server URL boundary,
    checked BEFORE the round-trip read), with the logbook allowlist keyed on the
    UNION of the entry's current and resulting logbooks (moving an entry in or out is a write to
    both).

    Server behaviours worth knowing: the entry's OWNER is re-set to the write service account
    on every edit (the original author survives only in the server's archived version, which
    is NOT reachable from this server, so recovery is manual, by someone with direct Olog
    access); editing a
    legacy entry that has no raw body source makes the server re-render its visible text (reported
    back as a warning); and an entry whose attachments have duplicate or missing filenames is
    REFUSED, because Olog matches attachments by filename and would silently drop one. Needs at
    least one field to change. With EPICS_MCP_OLOG_URL unset returns enabled=false.
    """
    return await _update_log_entry(
        log_id=log_id,
        title=title,
        description=description,
        level=level,
        logbooks=logbooks,
        tags=tags,
        timeout=timeout,
    )


@mcp.tool(
    annotations=ToolAnnotations(
        # NOT read-only: with output_path it writes a file to the workspace (it refuses to
        # overwrite,
        # so not destructive; and a re-download to the same path then fails, so not idempotent).
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    )
)
@translate_epics_errors
async def download_log_attachment(
    log_id: Annotated[
        str | None,
        Field(description="Id of the Olog entry the attachment belongs to (with filename)"),
    ] = None,
    filename: Annotated[
        str | None,
        Field(description="Attachment filename to download (needs log_id), the primary route"),
    ] = None,
    attachment_id: Annotated[
        str | None,
        Field(
            description="Attachment GridFS id (the by-id route inline images use), an alternative "
            "to log_id + filename"
        ),
    ] = None,
    output_path: Annotated[
        str | None,
        Field(
            description="Workspace file path to write the bytes to (a NEW file), the default "
            "handover, up to EPICS_MCP_OLOG_ATTACH_MAX_BYTES (default 50 MiB)"
        ),
    ] = None,
    as_base64: Annotated[
        bool,
        Field(
            description="Return the bytes base64-encoded in the result instead (small files only)"
        ),
    ] = False,
    timeout: Annotated[float, Field(description="Timeout in seconds", gt=0)] = 5.0,
) -> OlogDownloadResult:
    """Download one Phoebus Olog attachment's raw bytes (GET /logs/attachments/{id}/{name} or
    /attachment/{id}).

    Identify it by (log_id + filename) or by attachment_id. Bytes cross the boundary written to
    output_path (a NEW workspace file, EPICS_MCP_ALLOWED_ROOTS-checked) or base64 in the result
    (as_base64, small files), pass exactly one, not both. Either way the body is capped by
    EPICS_MCP_OLOG_ATTACH_MAX_BYTES (default 50 MiB; a base64 result is capped smaller still). With
    EPICS_MCP_OLOG_URL unset returns enabled=false.
    """
    return await _download_log_attachment(
        log_id=log_id,
        filename=filename,
        attachment_id=attachment_id,
        output_path=output_path,
        as_base64=as_base64,
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
async def list_log_attachments(
    log_id: Annotated[str, Field(description="Id of the Olog entry whose attachments to list")],
    timeout: Annotated[float, Field(description="Timeout in seconds", gt=0)] = 5.0,
) -> OlogListAttachmentsResult:
    """List one Phoebus Olog entry's attachments.

    Returns each attachment's id, filename and fileMetadataDescription. found=false for a
    definitive
    404. With EPICS_MCP_OLOG_URL unset returns enabled=false. Use the ids/filenames with
    download_log_attachment.
    """
    return await _list_log_attachments(log_id, timeout)


@mcp.tool(
    output_schema=None,
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@translate_epics_errors
async def diagnose_connection(
    pv_name: Annotated[str, Field(description="The PV to diagnose")],
    timeout: Annotated[
        float | None,
        Field(
            description="Live-probe timeout in seconds (default: config diagnose_timeout, 5.0)",
            gt=0,
        ),
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
            "device. Default False + gated on EPICS_MCP_NAMING_URL, no ESS egress unless enabled."
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

    Read-only. The live p4p connect is the ONLY truth for connected/disconnected, a disconnected
    PV is a NORMAL input (this does NOT raise). ChannelFinder/Naming/Archiver/Alarm are explanatory
    only: they give a likely_cause + evidence, never flip the verdict, and a disabled/errored plane
    is 'withheld' (never a false negative). likely_cause is one of healthy, ioc_down, name_typo,
    unregistered, indeterminate; 'indeterminate' is first-class and honest. On a PVA name-server a
    typo and a dead IOC both time out (PV_NOT_FOUND only under UDP broadcast), so cause is keyed on
    ChannelFinder/Naming, never the transport error code. No collision/uniqueness claim is made
    (multi-responder detection is out of scope). Naming is off by default (no ESS egress); a Naming
    URL that cannot prove its identity (its /rest/swagger.json beacon) withholds rather than
    fabricating name_typo (S13), so a foreign/misconfigured URL yields indeterminate, not a typo.
    """
    return await _diagnose_connection(
        pv_name,
        timeout=timeout,
        check_channelfinder=check_channelfinder,
        check_naming=check_naming,
        check_archiver=check_archiver,
        check_alarm=check_alarm,
    )


# Register the display-aware tools (validate_pvs / crossplane_check / coverage_audit / find_device),
# loaded above as the single capability truth. Runs now that mcp + the core tools exist. When the
# registrar is None (extra absent, or installed-but-broken and already logged at ERROR), the core
# server runs standalone and no surface over-claims the display tools.
if _display_registrar is not None:
    _display_registrar(mcp)


# === Resources ===


@mcp.resource("epics-pv://health")
def health() -> dict[str, object]:
    """What this process is and what it may write: status, p4p version, planes, both write gates.

    any_write_gate_armed is the whole write answer. write_enabled is the PV gate ALONE, so a server
    with the logbook gate armed reports it as false while it can still create entries. One boolean
    per service plane says which are configured; pv_search says how far a PV search can travel
    without naming an address. Which address, and the audit path, stay with epics-doctor.

    The posture group after the planes answers what an approver asks next: whether the REST planes
    verify certificates, computed so a CA bundle beats a disabled switch instead of mirroring the
    switch, and beside it whether any plane speaks https at all, because verification being on says
    nothing where there is no certificate; whether the REST GET throttle is on, which is never a PV
    read; whether the file boundary variable holds a root, never which; and how much the
    ChannelFinder allowlists DISCLOSE, where zero is the most private posture rather than a broken
    one. Which accounts those are stays with epics-doctor; the bundle path and the roots are on no
    surface at all, they are in the environment the server was started with.
    """
    return get_health()


@mcp.resource("epics-pv://config")
def epics_config() -> dict[str, object]:
    """Non-secret configuration values this process was started with.

    The service URLs here are the ones whose host may be disclosed. Three are withheld: the naming
    and logbook ones, which appear as booleans in epics-pv://health instead, and the archiver
    retrieval one, whose plane is reported there through the mgmt URL it falls back to. An unset PV
    write pattern is null, never a placeholder string.

    A userinfo (user:password@) is removed from a service URL; every other character is kept, so
    the value stays comparable with the block in a client configuration file. "(disabled)" means
    the plane is not configured, and null means it is configured but could not be shown without
    risking a credential.
    """
    return get_epics_config()


@mcp.resource("epics-pv://guide")
def guide() -> str:
    """Agent-readable operational cookbook: service planes, recipes, error signatures.

    The same text is served by the ``get_guide`` TOOL, which is the channel a MODEL fetches from;
    a resource is application-controlled and has to be asked for. This one stays because it costs
    nothing and is the right shape for a human or an application reading the whole document.
    """
    # ``_guide_text``, not ``get_guide``: since the tool of that name exists, the module namespace
    # carries both, and this handler wants the packaged file.
    return _guide_text()


# === Prompts ===


@mcp.prompt()
def diagnose_pv(pv_name: str) -> str:
    """Step-by-step PV diagnosis workflow."""
    return _diagnose_pv(pv_name)


@mcp.prompt()
def setup_epics_mcp() -> str:
    """Configure this server for a new facility, one service plane at a time."""
    # Thread the real preset table rather than letting the prompt hold its own copy: a preset
    # added to epics_mcp.presets has to show up here without anyone remembering to edit a string.
    # PRESETS is keyword-only and has no default on the other side, so a wrapper that stopped
    # passing it is a TypeError in the tests, not a prompt that quietly offers a stale set.
    return _setup_epics_mcp(presets=PRESETS)


@mcp.prompt()
def compare_machine_state(pv_prefix: str, reference_file: str = "") -> str:
    """Compare current machine state to expected values."""
    # Thread the actual capability so the rendered prompt never instructs the LLM to call the
    # display-gated validate_pvs tool on a core-only install (S26/N05). The LLM-facing signature
    # stays (pv_prefix, reference_file), display_tools_available is NOT an exposed prompt argument.
    return _compare_machine_state(
        pv_prefix, reference_file, display_tools_available=_DISPLAY_TOOLS_AVAILABLE
    )


def main(argv: Sequence[str] | None = None) -> None:
    """Entry point for the MCP server.

    Parses the command line FIRST, then validates the write-safety config at boot (fail-fast)
    whenever a write gate is enabled, the postures where the pattern / rate-limit / audit-sink
    config is used. A read-only deploy (every write gate off, the default) skips all of it, so a
    stray audit path is harmless there.

    The order matters (QA-41): a write-enabled install without a durable audit sink refuses to
    boot, and asking for help is exactly what an operator does next. A parser placed after that
    check would make the one command that explains the usage the one command they cannot run.
    An MCP client starts this with no options at all, which parses to an empty namespace and falls
    through to the transport unchanged.
    """
    # Before the parser (QA-8), as in the diagnostic CLIs: argparse prints ``--help`` inside
    # ``parse_args``, so a non-ASCII character in any help text would die on a cp1252 console if
    # the reconfigure came later. It is a no-op on a stream already UTF-8 (see cli_common).
    configure_stdout()
    parser = argparse.ArgumentParser(
        # prog is pinned because argparse's default is interpreter dependent: 3.12/3.13 use
        # basename(sys.argv[0]), 3.14 derives it from __main__.__spec__ and prints an absolute
        # path for a console script. A fixed prog is the only answer stable across requires-python.
        prog="epics-mcp",
        description=(
            "Run the EPICS MCP server on stdio. Started by an MCP client, not usually by hand; "
            "configure it through EPICS_MCP_* environment variables (see .env.example)."
        ),
    )
    # Through the shared helper since QA-46, which gave the same flag to the four diagnostic
    # commands. The line lived here first and was the only one of its kind; one home means the
    # version source and the prog prefix cannot differ between the console scripts.
    add_version_argument(parser)
    parser.parse_args(argv)

    config = get_config()
    # A write-enabled instance whose audit sink is ephemeral stderr (no EPICS_MCP_AUDIT_LOG_FILE)
    # loses every ATTEMPT/ALLOW/DENY/READBACK/BOUNDS_DENY record on restart, the one trail meant to
    # surface a wrong write after the fact. Refuse to start without a DURABLE sink, symmetric with
    # the empty-pattern / reach (E8) refusals and the unwritable-path refusal below. Covers BOTH the
    # PV gate and the Olog write gate: they write to the same FILE (config.audit_log_file), each
    # through its own logger (epics_mcp.audit / epics_mcp.olog_audit), the sink is shared,
    # the logger is not.
    if (config.allow_pv_write or config.allow_olog_write) and not config.audit_log_file:
        raise SafetyConfigError(
            "A write gate is ENABLED (EPICS_MCP_ALLOW_PV_WRITE / EPICS_MCP_ALLOW_OLOG_WRITE) "
            "but EPICS_MCP_AUDIT_LOG_FILE is empty, the audit trail would go to stderr and "
            "vanish on restart. Set a durable audit log path so a wrong write stays "
            "reconstructable.",
            details={
                "allow_pv_write": config.allow_pv_write,
                "allow_olog_write": config.allow_olog_write,
                "audit_log_file": "",
            },
        )
    # Building the PV safety layer refuses to start on a bad PV write gate (empty allowlist pattern,
    # a non-loopback reach, or an unwritable audit path) rather than on the first write.
    # Only the PV gate has an eager layer; the Olog gate is built lazily on first use. Deliberate
    # asymmetry, not an oversight: an Olog-only deployment with an unwritable sink is caught at the
    # first write instead of at boot, but still BEFORE any write I/O, because all three Olog write
    # paths call get_olog_safety() ahead of their first mutating request. Detection is later; an
    # un-audited write stays impossible.
    if config.allow_pv_write:
        get_safety()
    mcp.run()


if __name__ == "__main__":
    main()
