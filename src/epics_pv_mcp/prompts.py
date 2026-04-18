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


def compare_machine_state(pv_prefix: str, reference_file: str = "") -> str:
    """Compare current machine state to expected values."""
    if reference_file:
        file_note = (
            f'\n1. Extract PVs from "{reference_file}" '
            f'using validate_pvs(file_path="{reference_file}")\n'
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
        "2. Read all current values with get_pvs(names=[...])\n"
        "3. Compare to expected/nominal values\n"
        "4. Report deviations with severity:\n"
        "   - CRITICAL: Alarm severity > 0 or value out of range\n"
        "   - WARNING: Value changed but within limits\n"
        "   - OK: Value matches expected"
    )
