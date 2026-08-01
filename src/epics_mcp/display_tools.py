"""Display-aware MCP tools, the optional ``displays`` tool group.

These four tools (``validate_pvs``, ``crossplane_check``, ``coverage_audit``,
``find_device``) join live EPICS PVs with the *display* plane: the macro-expanded,
per-instance PV inventory of ``.bob`` operator screens. That inventory comes from the
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

from epics_mcp.services.inventory_adapter import DEFAULT_PV_CONTEXT_CAP
from epics_mcp.tool_errors import translate_epics_errors
from epics_mcp.tools.coverage_audit import _coverage_audit
from epics_mcp.tools.crossplane import _crossplane_check
from epics_mcp.tools.find_device import _find_device
from epics_mcp.tools.validate import _validate_pvs

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
        Field(description="List of PV names to validate"),
    ] = None,
    file_path: Annotated[
        str | None,
        Field(
            description="Path to a .bob file. Extracts the concrete, macro-resolved "
            "ca/pva channels it references (via the opi_navigation inventory) and "
            "checks their connectivity."
        ),
    ] = None,
    displays_dir: Annotated[
        str | None,
        Field(
            description="Dataset ROOT for file_path mode, needed to resolve display "
            "macros (esp. for embedded fragments). Without it the file's own directory "
            "is used, which under-resolves fragments. NOTE: a full inventory walk is "
            "~60 s for a large dataset; do not call per-file in a loop."
        ),
    ] = None,
    timeout: Annotated[
        float | None,
        Field(description="Timeout in seconds per PV (default: EPICS_MCP_DEFAULT_TIMEOUT)"),
    ] = None,
) -> dict[str, object]:
    """Check PV connectivity. Provide a PV list or a .bob file path (+ displays_dir ROOT)."""
    return await _validate_pvs(
        pvs=pv_names, file_path=file_path, displays_dir=displays_dir, timeout=timeout
    )


@translate_epics_errors
async def crossplane_check(
    displays_dir: Annotated[
        str,
        Field(
            description="Project/dataset ROOT directory of .bob displays (searched recursively). "
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
            "= more complete, slower; a large dataset like fbis takes ~60 s at the default). "
            "Capped displays are reported as a lower bound in 'displays_incomplete'.",
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
    'broken'; non-channel protocols (loc/sim/sys/other) are excluded from the join. A 'broken'
    verdict (linked PV absent from the IOC .db) is produced only when 'module_db_root' supplies a
    provably complete IOC .db set; otherwise it is withheld.
    """
    return await _crossplane_check(
        displays_dir,
        st_cmd_path,
        query_naming=query_naming,
        query_channelfinder=query_channelfinder,
        context_cap=context_cap,
        windows_paths=windows_paths,
        module_db_root=module_db_root,
    )


@translate_epics_errors
async def coverage_audit(
    displays_dir: Annotated[str, Field(description="project/dataset ROOT of .bob displays")],
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
                "without naming a tree is a loud INVALID_INPUT, not a silent wrong scan"
            )
        ),
    ] = None,
    context_cap: Annotated[
        int,
        Field(description="max per-display reachability contexts the PV-inventory explores", ge=1),
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
    return await _coverage_audit(
        displays_dir,
        scope,
        query_channelfinder,
        query_archiver,
        query_alarm,
        alarm_config,
        context_cap,
        windows_paths,
    )


@translate_epics_errors
async def find_device(
    query: Annotated[str, Field(description="Device / PV channel (protocol prefix optional)")],
    displays_dir: Annotated[
        str, Field(description="Project/dataset ROOT holding the .bob displays")
    ],
    match: Annotated[
        MatchMode, Field(description="Match mode against the protocol-stripped channel")
    ] = "prefix",
    timeout: Annotated[float, Field(description="Live-read timeout in seconds")] = 5.0,
    context_cap: Annotated[
        int,
        Field(description="Per-display macro-context cap (higher = more complete, slower)", ge=1),
    ] = DEFAULT_PV_CONTEXT_CAP,
    windows_paths: Annotated[
        bool, Field(description="Resolve embedded <file> refs case-insensitively (Windows host)")
    ] = False,
) -> dict[str, object]:
    """Find which operator screens show device X, read its channels live, and join the serving IOC.

    Read-only (Wedge-2 live counterpart of the offline find_screen). The reverse-lookup, which
    operator screens reference the device, is offline + macro-aware. Live values come from p4p;
    reach follows the launcher's EPICS search env (address lists / name servers / auto-addr search,
    run epics-doctor for the effective posture); the live read is capped to max_batch_size
    channels (honest note; screens stay complete). Source IOC comes from ChannelFinder, disabled
    by default (empty EPICS_MCP_CHANNELFINDER_URL → no source IOC, honest note); a CAPPED
    ChannelFinder fetch adds a
    note that the source-IOC join may be incomplete, a channel without source_ioc may simply have
    fallen past the cap, not be unregistered (F16). ca-only PVs are not read under the
    single pva provider. Returns
    {"report": <DeviceLookupReport JSON>, "markdown": <rendered report>}.
    """
    return await _find_device(query, displays_dir, match, timeout, context_cap, windows_paths)


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
