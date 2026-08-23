"""Display-aware MCP tools, the optional ``displays`` tool group.

These four tools (``validate_pvs``, ``crossplane_check``, ``coverage_audit``,
``find_device``) join live EPICS PVs with the *display* plane: the macro-expanded,
per-instance PV inventory of ``.bob`` operator screens AND the ``.plt`` Data Browser trends
reached from them (GB-79). A trend is not a screen and the inventory does not pretend it is: it
reports the kind on its own field, and a trend opened by a button counts as a top level of its
own while one embedded in a screen contributes to that screen. That inventory comes from the
``opi_navigation`` package (the build-once Wedge-0 PV engine), which is an **optional**
dependency: the ``displays`` dependency group (``uv sync --extra dev --group displays``), a
local-checkout surface that never reaches the published package.

Keeping them in their own module lets :mod:`epics_mcp.server` load them lazily via one
capability probe (``_load_display_registrar``): it skips them silently when the group is absent
(``find_spec`` gate), so the core PV server (read/write/monitor/discover/diagnose + the REST
planes) installs and starts standalone, for any EPICS user who does not have the display layer,
and it degrades loud (an ERROR log, core tools kept) if an *installed* group fails to import.

A dedicated CS-Studio / Phoebus MCP that complements these tools is in the works.
"""

from typing import Annotated

from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from opi_navigation.pv_analysis.lookup import MatchMode
from pydantic import Field

from epics_mcp.provenance import Plane, reach_of
from epics_mcp.services.inventory_adapter import DEFAULT_PV_CONTEXT_CAP
from epics_mcp.tool_errors import translate_epics_errors
from epics_mcp.tools.coverage_audit import _coverage_audit
from epics_mcp.tools.crossplane import _crossplane_check
from epics_mcp.tools.find_device import _find_device
from epics_mcp.tools.validate import PvView, _validate_pvs

# All four display tools share the same read-only, side-effect-free posture.
_READONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)


@translate_epics_errors
async def validate_pvs(
    pv_names: Annotated[
        list[str] | None,
        Field(
            description="List of PV names to validate. Takes precedence: supply a NON-EMPTY list "
            "together with file_path and the list wins, the file is not looked at (and not "
            "refused), and the answer then carries no file_path/shown_by_display fields either, "
            "because no file was opened to report about. An EMPTY list does not win: the file is "
            "read as usual and those fields come back."
        ),
    ] = None,
    file_path: Annotated[
        str | None,
        Field(
            description="Path to a .bob display or a .plt Data Browser trend. Extracts the "
            "concrete, macro-resolved "
            "ca/pva channels it references (via the opi_navigation inventory) and "
            "checks their connectivity; which channels those are depends on view. A path with "
            "any other suffix, or one that lies outside "
            "displays_dir, is refused straight away with INVALID_INPUT: the inventory reads "
            "only those two kinds, so such a call can only ever come back empty, and it used to "
            "take a full inventory walk to say so. A trend answers under both views but by "
            "different routes: embedded in a screen through a databrowser widget its traces are "
            "attributed to that screen and only the file view finds them here, while a trend "
            "opened by an open_file button is a top level of its own. Echoed back as the "
            "file_path field of the answer, as passed rather than resolved, on every file-mode "
            "result."
        ),
    ] = None,
    view: Annotated[
        PvView,
        Field(
            description="Which question about file_path to answer. 'file' (default) = the "
            "channels the file itself declares, attributed across every display that embeds it. "
            "'display' = the channels the file resolves to when opened as a display, its embedded "
            "fragments included. These differ a lot: a parent that only composes fragments "
            "declares nothing itself and answers total 0 under 'file' while resolving thousands "
            "under 'display'; a fragment is the reverse, because its macros are unbound when it "
            "stands alone. Every file-mode result reports shown_by_display (and file_path), and "
            "under 'file' a note says how many channels the display view adds. Ignored when a "
            "non-empty pv_names is given, which drops those fields with it. "
            "A 'lower bound' note means the macro expansion hit the per-display context cap, and "
            "it carries the verdict of the view you asked for: the FILE verdict additionally "
            "requires that the file declares a macro-templated PV of its own (one with no macro "
            "resolves the same at every cap, so its list cannot grow), while the DISPLAY verdict, "
            "also reported as shown_by_display_capped under both views, has no such test and is "
            "deliberately pessimistic, so read a true there as 'cannot be ruled out'. A SEPARATE "
            "note reports the glob cap, which leaves embedded screens out of the expansion and "
            "can shrink either view while both context-cap verdicts stay false; it counts pairs "
            "across the whole walk and names no file, so it is a statement about the dataset and "
            "the absence of notes never means 'complete'. Neither cap is adjustable from this "
            "tool."
        ),
    ] = "file",
    displays_dir: Annotated[
        str | None,
        Field(
            description="Dataset ROOT for file_path mode, needed to resolve display "
            "macros (esp. for embedded fragments). Without it the file's own directory "
            "is used, which under-resolves fragments. NOTE: a full inventory walk is "
            "~60 s for a large dataset; do not call per-file in a loop."
        ),
    ] = None,
    # gt=0 (QA-71): both display-gated tools answered a zero timeout with a plausible-looking
    # result rather than an error. validate_pvs reported the PV as disconnected and find_device
    # reported that nothing operator-facing references the device (the note then read "No
    # operator-facing screen references this device", reworded by GQ-21), which is the same class
    # of fabricated answer QA-65 removed from the caps, not the honest failure it was assumed to be.
    timeout: Annotated[
        float | None,
        Field(description="Timeout in seconds per PV (default: EPICS_MCP_DEFAULT_TIMEOUT)", gt=0),
    ] = None,
) -> dict[str, object]:
    """Check PV connectivity. Provide a PV list, or a .bob/.plt path (+ displays_dir ROOT)."""
    # GB-64: the file half has no plane, the connection half is the live one, so the answer
    # names exactly the plane it consulted.
    answer = await _validate_pvs(
        pvs=pv_names,
        file_path=file_path,
        displays_dir=displays_dir,
        timeout=timeout,
        view=view,
    )
    answer.setdefault("reach", reach_of("live-pv"))
    return answer


@translate_epics_errors
async def crossplane_check(
    displays_dir: Annotated[
        str,
        Field(
            description="Project/dataset ROOT directory of .bob displays (searched recursively; "
            ".plt Data Browser trends found there contribute their trace PVs too). "
            "Must be the root, not a narrow per-IOC subdirectory: display macros are bound by the "
            "operator top-levels found here, so a too-narrow scope leaves PVs unresolved."
        ),
    ],
    st_cmd_path: Annotated[
        str,
        Field(description="Path to an e3 IOC st.cmd startup script"),
    ],
    query_naming: Annotated[
        bool,
        Field(
            description="Query the ESS Naming Service (read-only GET) for the IOC device "
            "name. Default False keeps the check fully offline and deterministic."
        ),
    ] = False,
    query_channelfinder: Annotated[
        bool,
        Field(
            description="Check each concrete linked PV against ChannelFinder (the runtime PV "
            "directory) and report those NOT registered as 'cf_unregistered', a separate plane "
            "from 'broken' (CF runtime registry vs. static .db). Needs "
            "EPICS_MCP_CHANNELFINDER_URL; unset → an honest 'skipped' note (no network call). "
            "Default False stays offline. Withheld (never false-flagged) on a truncated registry."
        ),
    ] = False,
    context_cap: Annotated[
        int,
        Field(
            description="Max per-display reachability contexts the PV-inventory explores (higher "
            "= more complete, slower; a large dataset takes ~60 s at the default). "
            "Capped displays are reported as a lower bound in 'displays_incomplete', and the "
            "notes call this limit the per-display context cap. The walk "
            "has a SECOND limit this argument cannot raise, the glob cap: a <file> reference "
            "still carrying a macro is resolved by globbing the known displays, and past that "
            "cap the surplus matches are dropped, which leaves out whole embedded screens. Each "
            "cap fires its own notes entry naming its count; the absence of such an entry means "
            "no cap fired on this run, never 'complete'.",
            ge=1,
        ),
    ] = DEFAULT_PV_CONTEXT_CAP,
    windows_paths: Annotated[
        bool,
        Field(
            description="Resolve embedded <file> references case-insensitively (Windows host). "
            "Default False = Linux/ESS-console semantics (deterministic); set True on Windows if "
            "embed chains under-resolve due to filename case mismatch."
        ),
    ] = False,
    module_db_root: Annotated[
        str,
        Field(
            description="Opt-in: local directory holding the IOC's e3 module .db files. When set, "
            "concrete linked PVs are checked against the loaded IOC .db set and a 'broken' verdict "
            "is emitted ONLY if that set is provably complete + fully resolved (else withheld, no "
            "false alarm; e3 IOCs that load records via iocshLoad/dbLoadTemplate withhold). "
            "Empty (default) keeps the check at prefix/Naming level (no 'broken' verdict)."
        ),
    ] = "",
) -> dict[str, object]:
    """Cross-plane PV provenance: join macro-expanded display PVs ↔ e3 IOC (st.cmd) ↔ ESS Naming.

    Read-only. Returns a structured report plus a Markdown rendering. The display PVs come from the
    macro-expanded, per-instance PV-inventory (operator-facing displays only); concrete PVs sharing
    the IOC prefix are 'linked' (writable subset surfaced), others 'other_prefix'. PVs the inventory
    cannot resolve to a concrete channel are 'indeterminate' (dynamic/unresolved) and never judged
    'broken'; non-channel protocols (loc/sim/sys/other) are excluded from the join and reported
    with a per-protocol count that partitions their total. A 'broken'
    verdict (linked PV absent from the IOC .db) is produced only when 'module_db_root' supplies a
    provably complete IOC .db set; otherwise it is withheld.
    """
    answer = await _crossplane_check(
        displays_dir,
        st_cmd_path,
        query_naming=query_naming,
        query_channelfinder=query_channelfinder,
        context_cap=context_cap,
        windows_paths=windows_paths,
        module_db_root=module_db_root,
    )
    # GB-64: the planes are named from the FLAGS, never statically. A tool that lists a plane it
    # was told not to query would state that the plane answered, which is the same class of
    # untrue claim the field exists to remove, one level down.
    consulted: list[Plane] = []
    if query_channelfinder:
        consulted.append("channelfinder")
    if query_naming:
        consulted.append("naming")
    answer.setdefault("reach", reach_of(*consulted))
    return answer


@translate_epics_errors
async def coverage_audit(
    displays_dir: Annotated[
        str,
        Field(
            description="project/dataset ROOT of .bob displays (.plt Data Browser trends found "
            "there contribute their trace PVs too)"
        ),
    ],
    scope: Annotated[
        str,
        Field(
            description="record-name prefix narrowing the ChannelFinder query AND the display set "
            "(e.g. DEV-TEST01:Ctrl-EVR-01:); '' = whole site (CF cap risk, small-scope only)"
        ),
    ] = "",
    query_channelfinder: Annotated[
        bool, Field(description="query ChannelFinder for delivered PVs (the coverage anchor)")
    ] = False,
    query_archiver: Annotated[
        bool, Field(description="add the archive plane (per-PV is_archived)")
    ] = False,
    query_alarm: Annotated[
        bool, Field(description="add the alarm plane (per-PV is_alarm_configured)")
    ] = False,
    alarm_config: Annotated[
        str | None,
        Field(
            description=(
                "alarm config-tree name, REQUIRED when the alarm plane is active (query_alarm AND "
                "its URL set); no default (site-specific trees), so opting into the alarm plane "
                "without naming a tree is a loud INVALID_INPUT, not a silent wrong scan. Names "
                "unknown? Call get_alarm_history WITHOUT root and read the first path segment of "
                "each event's config field (guide: Discover the alarm config-tree names)"
            )
        ),
    ] = None,
    context_cap: Annotated[
        int,
        Field(
            description="max per-display reachability contexts the PV-inventory explores; the "
            "notes call this limit the per-display context cap. The "
            "walk has a SECOND limit this argument cannot raise, the glob cap: a <file> "
            "reference still carrying a macro is resolved by globbing the known displays, and "
            "past that cap the surplus matches are dropped, leaving out whole embedded screens "
            "and making the display set D a lower bound. Each cap fires its own notes entry "
            "naming its count; the absence of such an entry means no cap fired on this run, "
            "never 'complete'.",
            ge=1,
        ),
    ] = DEFAULT_PV_CONTEXT_CAP,
    windows_paths: Annotated[
        bool, Field(description="resolve embedded <file> refs case-insensitively (Windows host)")
    ] = False,
) -> dict[str, object]:
    """Cross-plane coverage audit: which delivered PV has no display/archive/alarm, and back.

    Read-only. Joins the Wedge-0 display-PV index (PV→[screens]) with ChannelFinder (delivered PVs,
    the anchor), the Archiver and the Phoebus Alarm config. Each runtime plane is queried only when
    requested AND its *_URL is set; a missing URL withholds that plane (never a false 'no'). Returns
    the cross-coverage matrix (cf_and_display / cf_only=blind-spots / display_only) + verdicts
    + critical_uncovered (delivered AND a proven gap), with honest lower-bound notes.
    """
    answer = await _coverage_audit(
        displays_dir,
        scope,
        query_channelfinder,
        query_archiver,
        query_alarm,
        alarm_config,
        context_cap,
        windows_paths,
    )
    # From the flags, for the reason stated at crossplane_check.
    consulted: list[Plane] = []
    if query_channelfinder:
        consulted.append("channelfinder")
    if query_archiver:
        consulted.append("archiver")
    if query_alarm:
        consulted.append("alarm")
    answer.setdefault("reach", reach_of(*consulted))
    return answer


@translate_epics_errors
async def find_device(
    query: Annotated[str, Field(description="Device / PV channel (protocol prefix optional)")],
    displays_dir: Annotated[
        str,
        Field(
            description="Project/dataset ROOT holding the .bob displays. A .plt Data Browser "
            "trend opened by a button is a top level in its own right, so it can be returned "
            "among the matches; it comes back marked as a trend, never as a screen."
        ),
    ],
    match: Annotated[
        MatchMode, Field(description="Match mode against the protocol-stripped channel")
    ] = "prefix",
    timeout: Annotated[float, Field(description="Live-read timeout in seconds", gt=0)] = 5.0,
    context_cap: Annotated[
        int,
        Field(
            description="Per-display context cap: max reachability contexts the PV-inventory "
            "explores per display (higher = more complete, slower). The walk has a SECOND limit "
            "this argument cannot raise, the glob cap; both are named in their own notes entry.",
            ge=1,
        ),
    ] = DEFAULT_PV_CONTEXT_CAP,
    windows_paths: Annotated[
        bool, Field(description="Resolve embedded <file> refs case-insensitively (Windows host)")
    ] = False,
) -> dict[str, object]:
    """Find where device X is shown, read its channels live, and join the serving IOC.

    NOT every match is a screen. A Data Browser trend opened by a button is operator-facing too
    and is returned, marked by screens[].node_kind ("display" or "trend"), with display_count and
    trend_count splitting the list; an empty answer denies both kinds rather than screens alone.
    Read-only (Wedge-2 live counterpart of the offline find_screen). The reverse-lookup, which
    operator-facing files reference the device, is offline + macro-aware. Each match comes back
    with the roles it uses the device in, read and/or write (screens[].roles), so a screen that can
    WRITE the device is visible without opening one. Live values come from p4p;
    reach follows the launcher's EPICS search env (address lists / name servers / auto-addr search,
    run epics-doctor for the effective posture); the live read is capped to max_batch_size
    channels (honest note; the match list is not shortened by that cap). The match list has its
    own two limits, the inventory walk's per-display context cap and its glob cap, and each fires
    its own notes entry naming the count; the absence of such a note means no cap fired on this
    run, never "complete". Source IOC comes from
    ChannelFinder, disabled
    by default (empty EPICS_MCP_CHANNELFINDER_URL → no source IOC, honest note); a CAPPED
    ChannelFinder fetch adds a
    note that the source-IOC join may be incomplete, a channel without source_ioc may simply have
    fallen past the cap, not be unregistered (F16). ca-only PVs are not read under the
    single pva provider. Returns
    {"report": <DeviceLookupReport JSON>, "markdown": <rendered report>}.
    """
    answer = await _find_device(query, displays_dir, match, timeout, context_cap, windows_paths)
    # Unconditional here: this tool has no per-plane flags, it always asks all three (each
    # withholds itself when its URL is unset, which the classification then reports as
    # not-configured rather than hiding).
    answer.setdefault("reach", reach_of("live-pv", "channelfinder", "naming"))
    return answer


def register_display_tools(mcp: FastMCP) -> None:
    """Register the four display-aware tools on *mcp*.

    Called from :mod:`epics_mcp.server` only after ``_load_display_registrar`` has confirmed the
    optional ``displays`` group is installed and imports cleanly (the single capability truth), so
    the core server installs and starts standalone without it.
    """
    # ``output_schema=None`` is the explicit opt-out that keeps an information-empty accept-all
    # schema off the wire, these four still return ``dict[str, object]``. It is NOT a claim that
    # they are unsuitable for typing: they are simply the four S29 has not reached, and they are
    # absent from the core lane, so a typed shape here would only ever be advertised on a
    # displays-group install. Typing one means DELETING its kwarg here (``None`` overrides the
    # annotation-derived schema) and adding it to the conformance whitelist. Whether they get typed
    # at all is an open product question, deliberately recorded here rather than in a status file.
    mcp.tool(annotations=_READONLY, output_schema=None)(validate_pvs)
    mcp.tool(annotations=_READONLY, output_schema=None)(crossplane_check)
    mcp.tool(annotations=_READONLY, output_schema=None)(coverage_audit)
    mcp.tool(annotations=_READONLY, output_schema=None)(find_device)
