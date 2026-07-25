"""MCP Prompts for the EPICS PV MCP Server."""


def diagnose_pv(pv_name: str) -> str:
    """Step-by-step PV diagnosis workflow."""
    return (
        f"Diagnose EPICS PV: {pv_name}\n\n"
        "Follow these steps:\n"
        f'1. get_pv_info("{pv_name}") — check connection state, data type, alarm status\n'
        f'2. get_pv_value("{pv_name}") — read current value\n'
        f'3. monitor_pv("{pv_name}", duration=5) — watch for value changes over 5 seconds\n'
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
    (no ``[displays]`` extra) must not be told to call ``validate_pvs`` — that would be an
    impossible plan. A default here would fail OPEN: if the server wrapper forgot to thread the
    real capability, the prompt would silently re-instruct the missing tool. Required → a mis-wired
    wrapper is a loud TypeError in a test, not a silent regression.
    """
    if reference_file and display_tools_available:
        # S1-3: pass the dataset ROOT as displays_dir too — without it validate_pvs walks the
        # file's own directory and under-resolves embedded fragments (consistent with the tool's
        # own description).
        file_note = (
            f'\n1. Extract PVs from "{reference_file}" using '
            f'validate_pvs(file_path="{reference_file}", displays_dir="<dataset ROOT>") '
            "(displays_dir = the project ROOT; without it embedded fragments under-resolve)\n"
        )
    elif reference_file:
        # Core-only: no MCP tool parses a .bob here (the .bob-parsing tool is display-gated and not
        # registered). Do NOT name an unavailable tool — tell the client to read the file itself.
        file_note = (
            f'\n1. Read "{reference_file}" yourself and collect the PV names it references '
            "(or ask the user for the PV list) — no MCP tool parses a .bob in this core-only "
            "install\n"
        )
    else:
        file_note = (
            f'\n1. Collect PVs with prefix "{pv_prefix}" '
            "— ask the user for the PV list or .bob file\n"
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
