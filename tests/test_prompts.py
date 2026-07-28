"""Tests for MCP prompts."""

from epics_mcp.prompts import compare_machine_state, diagnose_pv


def test_diagnose_pv_contains_pv_name() -> None:
    result = diagnose_pv("MPS:VAC:Pressure")
    assert "MPS:VAC:Pressure" in result


def test_diagnose_pv_contains_steps() -> None:
    result = diagnose_pv("TEST:PV")
    assert "get_pv_info" in result
    assert "get_pv_value" in result
    assert "monitor_pv" in result


def test_compare_machine_state_with_file() -> None:
    # Positive control: with the display tools available the reference_file path still uses the
    # display-gated validate_pvs tool, the correct full-install behaviour (NOT a bug pin).
    result = compare_machine_state(
        "MPS:", reference_file="status.bob", display_tools_available=True
    )
    assert "status.bob" in result
    assert "validate_pvs" in result


def test_compare_machine_state_without_file() -> None:
    result = compare_machine_state("MPS:", display_tools_available=True)
    assert "MPS:" in result
    assert "ask the user" in result.lower() or "PV list" in result


def test_compare_machine_state_core_only_omits_validate_pvs() -> None:
    # Guard B1 (S26/N05): core-only (no [displays] extra) the reference_file path must NOT instruct
    # the LLM to call the display-gated validate_pvs tool, that would be an impossible plan.
    result = compare_machine_state(
        "MPS:", reference_file="status.bob", display_tools_available=False
    )
    assert "validate_pvs" not in result
    assert "status.bob" in result  # the file is still referenced, via the client-read path
    assert "yourself" in result or "ask the user" in result.lower()
