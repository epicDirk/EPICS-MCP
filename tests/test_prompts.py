"""Tests for MCP prompts."""

from __future__ import annotations

import pytest

from epics_mcp.presets import PRESETS, Preset
from epics_mcp.prompts import compare_machine_state, diagnose_pv, setup_epics_mcp


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


class TestSetupEpicsMcp:
    """The onboarding prompt: the conversational half of ``epics-init``."""

    def test_it_offers_every_preset_the_package_actually_has(self) -> None:
        """The prompt names the shapes the user can pick from, so a shape missing here is one they
        will never be offered."""
        result = setup_epics_mcp(presets=PRESETS)

        for name in PRESETS:
            assert name in result

    def test_a_new_preset_appears_without_editing_the_prompt(self) -> None:
        """WHY the table is threaded rather than written into the text. A second copy of the
        catalogue would drift the day someone adds a shape, and it would drift SILENTLY: the prompt
        would keep working and keep offering the old set."""
        extended = {
            **PRESETS,
            "gateway-only": Preset(
                name="gateway-only", summary="A shape invented by this test.", env={}
            ),
        }

        result = setup_epics_mcp(presets=extended)

        assert "gateway-only" in result
        assert "A shape invented by this test." in result

    def test_the_preset_table_is_required(self) -> None:
        """Keyword-only and no default, the same posture as ``display_tools_available``. A default
        would fail OPEN: a wrapper that stopped threading the real table would leave the prompt
        naming whatever set was frozen in this module, and nothing would say so. Required turns
        that into a loud TypeError here."""
        with pytest.raises(TypeError):
            setup_epics_mcp()  # type: ignore[call-arg]

    def test_it_ends_at_a_command_rather_than_at_a_variable_list(self) -> None:
        """The prompt works out WHICH shape; ``epics-init`` knows what each shape needs. Emitting
        the variables from inside the prompt would be a second, unguarded copy of the preset
        table."""
        result = setup_epics_mcp(presets=PRESETS)

        assert "epics-init --preset" in result

    def test_it_says_a_pv_less_check_confirms_nothing(self) -> None:
        """The same honesty the CLI prints. Without ``--probe-pv`` the live plane is reported and
        never contacted, so a clean report on a PV-only setup is not a confirmation, and a prompt
        that let the assistant call it one would undo the warning."""
        result = setup_epics_mcp(presets=PRESETS)

        assert "not that anything works" in result.lower()

    def test_it_names_the_three_states_that_look_healthy_and_are_not(self) -> None:
        """``?``, ``!`` and ``~`` are exit-code 0 or 3 states that a reader skims past as fine.
        They are the whole reason the doctor has more than two outcomes."""
        result = setup_epics_mcp(presets=PRESETS)

        assert "unverified" in result
        assert "identity probe failed" in result
        assert "no ingest" in result
