"""Tests for MCP prompts."""

from epics_pv_mcp.prompts import compare_machine_state, diagnose_pv


def test_diagnose_pv_contains_pv_name() -> None:
    result = diagnose_pv("MPS:VAC:Pressure")
    assert "MPS:VAC:Pressure" in result


def test_diagnose_pv_contains_steps() -> None:
    result = diagnose_pv("TEST:PV")
    assert "get_pv_info" in result
    assert "get_pv_value" in result
    assert "monitor_pv" in result


def test_compare_machine_state_with_file() -> None:
    result = compare_machine_state("MPS:", reference_file="status.bob")
    assert "status.bob" in result
    assert "validate_pvs" in result


def test_compare_machine_state_without_file() -> None:
    result = compare_machine_state("MPS:")
    assert "MPS:" in result
    assert "ask the user" in result.lower() or "PV list" in result
