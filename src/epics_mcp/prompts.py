"""MCP Prompts for the EPICS MCP server."""

from __future__ import annotations

from collections.abc import Mapping

from epics_mcp.presets import Preset


def diagnose_pv(pv_name: str) -> str:
    """Step-by-step PV diagnosis workflow."""
    return (
        f"Diagnose EPICS PV: {pv_name}\n\n"
        "Follow these steps:\n"
        f'1. get_pv_info("{pv_name}"), check connection state, data type, alarm status\n'
        f'2. get_pv_value("{pv_name}"), read current value\n'
        f'3. monitor_pv("{pv_name}", duration=5), watch for value changes over 5 seconds\n'
        "\n"
        "Report:\n"
        "- Connection status (connected/disconnected/timeout)\n"
        "- Current value and data type\n"
        "- Alarm severity and status\n"
        "- Update rate (events/second from monitor)\n"
        "- Recommended actions if issues found"
    )


def compare_machine_state(
    pv_prefix: str, reference_file: str = "", *, display_tools_available: bool
) -> str:
    """Compare current machine state to expected values.

    ``display_tools_available`` is keyword-only and REQUIRED (no default): the caller MUST state
    whether the display-gated ``validate_pvs`` tool is registered (S26/N05). A core-only install
    (no ``displays`` group) must not be told to call ``validate_pvs``, that would be an
    impossible plan. A default here would fail OPEN: if the server wrapper forgot to thread the
    real capability, the prompt would silently re-instruct the missing tool. Required → a mis-wired
    wrapper is a loud TypeError in a test, not a silent regression.
    """
    if reference_file and display_tools_available:
        # S1-3: pass the dataset ROOT as displays_dir too, without it validate_pvs walks the
        # file's own directory and under-resolves embedded fragments (consistent with the tool's
        # own description).
        file_note = (
            f'\n1. Extract PVs from "{reference_file}" using '
            f'validate_pvs(file_path="{reference_file}", displays_dir="<dataset ROOT>") '
            "(displays_dir = the project ROOT; without it embedded fragments under-resolve)\n"
        )
    elif reference_file:
        # Core-only: no MCP tool parses a .bob here (the .bob-parsing tool is display-gated and not
        # registered). Do NOT name an unavailable tool, tell the client to read the file itself.
        file_note = (
            f'\n1. Read "{reference_file}" yourself and collect the PV names it references '
            "(or ask the user for the PV list), no MCP tool parses a .bob in this core-only "
            "install\n"
        )
    else:
        file_note = (
            f'\n1. Collect PVs with prefix "{pv_prefix}" '
            "; ask the user for the PV list or .bob file\n"
        )

    return (
        f"Compare machine state for: {pv_prefix}\n\n"
        "Follow these steps:"
        f"{file_note}"
        "2. Read all current values with get_pvs(pv_names=[...])\n"
        "3. Compare to expected/nominal values\n"
        "4. Report deviations with severity:\n"
        "   - CRITICAL: Alarm severity > 0 or value out of range\n"
        "   - WARNING: Value changed but within limits\n"
        "   - OK: Value matches expected"
    )


def setup_epics_mcp(*, presets: Mapping[str, Preset]) -> str:
    """Walk a newcomer through configuring this server, conversationally.

    The same ground ``epics-init`` covers, for the reader who would rather be asked than read
    ``docs/deployment.md``. It deliberately ends at a COMMAND rather than at a block of variables:
    the assistant's job here is to work out which shape the user has, and ``epics-init`` already
    knows what each shape needs. Emitting the variables from inside a prompt would be a second,
    unguarded copy of the preset table.

    ``presets`` is keyword-only and REQUIRED (no default), the same posture as
    ``compare_machine_state``'s ``display_tools_available`` and for the same reason: the caller
    must state what actually exists rather than let this function assume. A default would fail
    OPEN. If the server wrapper stopped threading the real table, the prompt would keep naming
    whatever set was frozen here, and a preset added later would silently never be offered.
    Required means a mis-wired wrapper is a loud TypeError in a test instead.
    """
    catalogue = "\n".join(f"   - {name}: {preset.summary}" for name, preset in presets.items())
    return (
        "Help me configure the EPICS MCP server for my facility.\n\n"
        "Ask me about each service plane in turn, and do NOT guess: an unset URL disables its "
        "plane with no network call, so leaving one out is a valid answer, not a gap.\n"
        "1. Live PVs: which host do my IOCs or my gateway answer on? PVA or CA?\n"
        "2. ChannelFinder: do I have a channel registry, and at which URL?\n"
        "3. Archiver Appliance: mgmt URL, and does retrieval run on its own port?\n"
        "4. Alarm Logger: do I have a Phoebus alarm logger?\n"
        "5. Naming Service: do I have one?\n"
        "6. Olog: do I have a logbook?\n"
        "7. Internal CA: does any of the above use HTTPS with a private root certificate?\n\n"
        "Then match my answers to the closest of these shapes:\n"
        f"{catalogue}\n\n"
        "Then tell me to run, in a terminal (these are commands for ME, not tools you can call):\n"
        "   epics-init --preset <the shape you picked> --set NAME=VALUE ...\n"
        "and to save its output as the .mcp.json for my client.\n\n"
        "It runs epics-doctor against the result and prints what each plane answered. Read that "
        "report back with me, and treat these three as findings rather than noise:\n"
        "   - '?' unverified: it answered, but could not prove what it is\n"
        "   - '!' identity probe failed: reachable, but the probe got a 401/404/redirect\n"
        "   - '~' no ingest: it proved what it is and is not doing its job\n"
        "Note that without --probe-pv nothing reads a live PV, so a clean report on a "
        "PV-only setup confirms that nothing is misconfigured, not that anything works."
    )
