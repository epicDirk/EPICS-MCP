"""EPICS PV MCP Server — main entry point."""

from typing import Annotated

from fastmcp.exceptions import ToolError
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from opi_navigation.pv_analysis.lookup import MatchMode
from pydantic import Field

from epics_pv_mcp import __version__
from epics_pv_mcp.errors import EpicsError
from epics_pv_mcp.prompts import compare_machine_state as _compare_machine_state
from epics_pv_mcp.prompts import diagnose_pv as _diagnose_pv
from epics_pv_mcp.resources import get_epics_config, get_health
from epics_pv_mcp.services.inventory_adapter import DEFAULT_PV_CONTEXT_CAP
from epics_pv_mcp.tools.archiver import _get_pv_history, _is_archived
from epics_pv_mcp.tools.channelfinder import _find_channels
from epics_pv_mcp.tools.crossplane import _crossplane_check
from epics_pv_mcp.tools.discover import _discover_pvs
from epics_pv_mcp.tools.find_device import _find_device
from epics_pv_mcp.tools.info import _get_pv_info
from epics_pv_mcp.tools.monitor import _monitor_pv
from epics_pv_mcp.tools.read import _get_pv_value, _get_pvs
from epics_pv_mcp.tools.validate import _validate_pvs
from epics_pv_mcp.tools.write import _set_pv_value

# Keep in sync with the epics-pv posture in SKILL.md
mcp = FastMCP(
    "epics-pv-mcp",
    instructions=(
        "Read-only EPICS PV access by default: read live values and metadata, monitor, "
        "discover, validate the PVs of a .bob display, cross-plane provenance, device lookup "
        "(screens + live + source IOC), ChannelFinder lookups and Archiver history. The only "
        "mutating tool, set_pv_value, is gated OFF by "
        "default and additionally requires EPICS_MCP_ALLOW_PV_WRITE=true plus a regex allowlist, "
        "a rate limit and an audit log. The REST-backed tools (find_channels, is_archived, "
        "get_pv_history) stay disabled until their *_URL env vars are set; an empty URL means "
        "no client and no network call. Network reach is localhost-isolated by default: the "
        "server opens no non-local connection unless its launcher widens the EPICS address-list "
        "environment (EPICS_PVA_ADDR_LIST / EPICS_CA_ADDR_LIST and the matching *_AUTO_ADDR_LIST); "
        "until then it does NOT reach ESS production. See .env.example for the commented template."
    ),
)
mcp._mcp_server.version = __version__

# === Tools ===


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
async def get_pv_value(
    pv_name: Annotated[str, Field(description="EPICS PV name")],
    timeout: Annotated[
        float,
        Field(description="Timeout in seconds"),
    ] = 5.0,
) -> dict[str, object]:
    """Get the current value of an EPICS Process Variable.

    The result carries the same best-effort metadata as get_pv_info
    (alarm/timestamp/display/control/value_alarm/enum)."""
    try:
        return await _get_pv_value(pv_name, timeout)
    except EpicsError as e:
        raise ToolError(f"[{e.error_code}] {e}") from e
    except Exception as e:
        raise ToolError(f"[INTERNAL] {type(e).__name__}: {e}") from e


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
async def get_pvs(
    names: Annotated[
        list[str],
        Field(description="List of PV names to read (max 100)"),
    ],
    timeout: Annotated[
        float,
        Field(description="Timeout in seconds per PV"),
    ] = 5.0,
) -> dict[str, object]:
    """Batch-read multiple EPICS PVs in a single call.

    Each result carries the same best-effort metadata as get_pv_info
    (alarm/timestamp/display/control/value_alarm/enum)."""
    try:
        return await _get_pvs(names, timeout)
    except EpicsError as e:
        raise ToolError(f"[{e.error_code}] {e}") from e
    except Exception as e:
        raise ToolError(f"[INTERNAL] {type(e).__name__}: {e}") from e


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    )
)
async def set_pv_value(
    pv_name: Annotated[str, Field(description="EPICS PV name")],
    value: Annotated[str, Field(description="New value to set")],
    timeout: Annotated[
        float,
        Field(description="Timeout in seconds"),
    ] = 5.0,
) -> dict[str, object]:
    """Set a PV value. Requires EPICS_MCP_ALLOW_PV_WRITE=true.

    Protected by safety layer: environment gate, regex allowlist,
    rate-limit (10/min default), and audit logging.
    """
    try:
        return await _set_pv_value(pv_name, value, timeout)
    except EpicsError as e:
        raise ToolError(f"[{e.error_code}] {e}") from e
    except Exception as e:
        raise ToolError(f"[INTERNAL] {type(e).__name__}: {e}") from e


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
async def get_pv_info(
    pv_name: Annotated[str, Field(description="EPICS PV name")],
    timeout: Annotated[
        float,
        Field(description="Timeout in seconds"),
    ] = 5.0,
) -> dict[str, object]:
    """Get detailed PV metadata: value, alarm (severity/status incl. text + message),
    timestamp, display (units/limits/precision OR format/description), control (drive
    limits), value_alarm (active + HIHI/HIGH/LOW/LOLO limits and per-level severities,
    only when active), and enum index/label/choices for enum PVs. Unset (zero-width)
    display/control limit pairs are omitted; DBR_CHAR waveforms come back as int lists."""
    try:
        return await _get_pv_info(pv_name, timeout)
    except EpicsError as e:
        raise ToolError(f"[{e.error_code}] {e}") from e
    except Exception as e:
        raise ToolError(f"[INTERNAL] {type(e).__name__}: {e}") from e


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    )
)
async def monitor_pv(
    name: Annotated[str, Field(description="EPICS PV name to monitor")],
    duration: Annotated[
        float,
        Field(description="Duration in seconds to monitor (max 60)"),
    ] = 10.0,
    max_events: Annotated[
        int,
        Field(description="Maximum events to collect (max 1000)"),
    ] = 100,
) -> dict[str, object]:
    """Subscribe to PV changes for a given duration and return collected events.

    Each event carries the same best-effort metadata as get_pv_info
    (alarm/timestamp/display/control/value_alarm/enum)."""
    try:
        return await _monitor_pv(name, duration, max_events)
    except EpicsError as e:
        raise ToolError(f"[{e.error_code}] {e}") from e
    except Exception as e:
        raise ToolError(f"[INTERNAL] {type(e).__name__}: {e}") from e


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
async def validate_pvs(
    pvs: Annotated[
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
            description="Dataset ROOT for file_path mode — needed to resolve display "
            "macros (esp. for embedded fragments). Without it the file's own directory "
            "is used, which under-resolves fragments. NOTE: a full inventory walk is "
            "~60 s for a large dataset; do not call per-file in a loop."
        ),
    ] = None,
    timeout: Annotated[
        float,
        Field(description="Timeout in seconds per PV"),
    ] = 5.0,
) -> dict[str, object]:
    """Check PV connectivity. Provide a PV list or a .bob file path (+ displays_dir ROOT)."""
    try:
        return await _validate_pvs(
            pvs=pvs, file_path=file_path, displays_dir=displays_dir, timeout=timeout
        )
    except EpicsError as e:
        raise ToolError(f"[{e.error_code}] {e}") from e
    except Exception as e:
        raise ToolError(f"[INTERNAL] {type(e).__name__}: {e}") from e


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
async def discover_pvs(
    pattern: Annotated[
        str,
        Field(description="PV name or pattern to search for"),
    ],
    timeout: Annotated[
        float,
        Field(description="Timeout in seconds"),
    ] = 5.0,
) -> dict[str, object]:
    """Discover PVs by name. Wildcard patterns require ChannelFinder infrastructure."""
    try:
        return await _discover_pvs(pattern, timeout)
    except EpicsError as e:
        raise ToolError(f"[{e.error_code}] {e}") from e
    except Exception as e:
        raise ToolError(f"[INTERNAL] {type(e).__name__}: {e}") from e


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
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
    context_cap: Annotated[
        int,
        Field(
            description="Max per-display reachability contexts the PV-inventory explores (higher "
            "= more complete, slower; a large dataset like fbis takes ~60 s at the default). "
            "Capped displays are reported as a lower bound in 'displays_incomplete'."
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
            "is emitted ONLY if that set is provably complete + fully resolved (else withheld — no "
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
    try:
        return await _crossplane_check(
            displays_dir, st_cmd_path, query_naming, context_cap, windows_paths, module_db_root
        )
    except EpicsError as e:
        raise ToolError(f"[{e.error_code}] {e}") from e
    except Exception as e:
        raise ToolError(f"[INTERNAL] {type(e).__name__}: {e}") from e


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
async def find_channels(
    name_pattern: Annotated[
        str,
        Field(description="Channel/PV name glob (ChannelFinder syntax: * and ?)"),
    ],
    max_results: Annotated[
        int,
        Field(description="Cap on returned channels (a broad glob can match a whole site)"),
    ] = 500,
    timeout: Annotated[float, Field(description="Timeout in seconds")] = 5.0,
) -> dict[str, object]:
    """Query ChannelFinder: which IOC/host serves a PV, plus its tags/properties.

    Read-only. Disabled by default (set EPICS_MCP_CHANNELFINDER_URL to enable).
    """
    try:
        return await _find_channels(name_pattern, max_results, timeout)
    except EpicsError as e:
        raise ToolError(f"[{e.error_code}] {e}") from e
    except Exception as e:
        raise ToolError(f"[INTERNAL] {type(e).__name__}: {e}") from e


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
async def is_archived(
    pv: Annotated[str, Field(description="EPICS PV name")],
    timeout: Annotated[float, Field(description="Timeout in seconds")] = 5.0,
) -> dict[str, object]:
    """Report whether a PV is being archived (EPICS Archiver Appliance MGMT getPVStatus).

    Read-only. Disabled by default — returns enabled=false unless EPICS_MCP_ARCHIVER_URL is set.
    """
    try:
        return await _is_archived(pv, timeout)
    except EpicsError as e:
        raise ToolError(f"[{e.error_code}] {e}") from e
    except Exception as e:
        raise ToolError(f"[INTERNAL] {type(e).__name__}: {e}") from e


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
async def get_pv_history(
    pv: Annotated[str, Field(description="EPICS PV name")],
    start: Annotated[
        str, Field(description="Window start, ISO-8601 (e.g. 2026-06-01T00:00:00.000Z)")
    ],
    end: Annotated[str, Field(description="Window end, ISO-8601")],
    max_points: Annotated[
        int,
        Field(description="Cap on returned samples (a wide window on a fast PV is unbounded)"),
    ] = 5000,
    timeout: Annotated[float, Field(description="Timeout in seconds")] = 5.0,
) -> dict[str, object]:
    """Fetch archived samples for a PV over an ISO-8601 window (Archiver retrieval getData.json).

    Read-only. Disabled by default — returns enabled=false unless EPICS_MCP_ARCHIVER_URL is set.
    """
    try:
        return await _get_pv_history(pv, start, end, max_points, timeout)
    except EpicsError as e:
        raise ToolError(f"[{e.error_code}] {e}") from e
    except Exception as e:
        raise ToolError(f"[INTERNAL] {type(e).__name__}: {e}") from e


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
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
        int, Field(description="Per-display macro-context cap (higher = more complete, slower)")
    ] = DEFAULT_PV_CONTEXT_CAP,
    windows_paths: Annotated[
        bool, Field(description="Resolve embedded <file> refs case-insensitively (Windows host)")
    ] = False,
) -> dict[str, object]:
    """Find which operator screens show device X, read its channels live, and join the serving IOC.

    Read-only (Wedge-2 live counterpart of the offline find_screen). The reverse-lookup — which
    operator screens reference the device — is offline + macro-aware. Live values come from p4p,
    localhost-isolated by default (does NOT reach ESS production until the launcher widens the EPICS
    address list); the live read is capped to max_batch_size channels (honest note; screens stay
    complete). Source IOC comes from ChannelFinder, disabled by default (empty
    EPICS_MCP_CHANNELFINDER_URL → no source IOC, honest note). ca-only PVs are not read under the
    single pva provider. displays_dir is the project/dataset ROOT. Returns
    {"report": <DeviceLookupReport JSON>, "markdown": <rendered report>}.
    """
    try:
        return await _find_device(query, displays_dir, match, timeout, context_cap, windows_paths)
    except EpicsError as e:
        raise ToolError(f"[{e.error_code}] {e}") from e
    except Exception as e:
        raise ToolError(f"[INTERNAL] {type(e).__name__}: {e}") from e


# === Resources ===


@mcp.resource("epics-pv://health")
def health() -> dict[str, object]:
    """Server status, p4p version, write configuration."""
    return get_health()


@mcp.resource("epics-pv://config")
def epics_config() -> dict[str, object]:
    """Non-secret configuration values."""
    return get_epics_config()


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
