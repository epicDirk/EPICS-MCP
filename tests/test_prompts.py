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


def test_diagnose_pv_teaches_the_posture_step_through_a_channel_a_client_can_use() -> None:
    """GB-64 (a). The chain taught three steps and no posture step at all, so a client could reach
    a confident answer without ever establishing which world it read from.

    The assertion is in two halves and the SECOND is the load-bearing one. Naming the reach is
    easy; the failure this repairs is that the rest of this repository's prose says "run
    epics-doctor", which is a console script a client with no shell cannot run. A prompt that
    repeated it would hand the client an impossible plan, the exact class this repository has
    already been bitten by twice (the pv_names rename, then QA-33), and it would fail silently
    because an unexecutable step looks like a skipped one.

    Provably red: put ``epics-doctor`` back into the prompt body, or drop the ``reach`` sentence.
    """
    rendered = diagnose_pv("SIM:PS-01:Cur-RB")
    assert "reach" in rendered
    assert "probed" in rendered
    assert "epics-doctor" not in rendered


def test_diagnose_pv_teaches_a_counter_check_across_planes() -> None:
    """GB-64 (a). The incident behind the item left its open question unanswered while two tools
    that would have answered it went unused, so the chain now ends in corroboration rather than
    in a monitor.

    Each named tool is asserted, not the sentence, because the sentence can be reworded and the
    plan still has to name a plane that can actually answer a negative.

    Provably red: delete step 4.
    """
    rendered = diagnose_pv("SIM:PS-01:Cur-RB")
    for named in ("find_device", "find_channels", "get_pv_history", "search_logbook"):
        assert named in rendered, f"the counter-check no longer names {named}"


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


@pytest.mark.parametrize("reference_file", ["expected_state.csv", "snapshot.json", "notes"])
def test_compare_machine_state_does_not_teach_validate_pvs_for_a_non_display(
    reference_file: str,
) -> None:
    """QA-33 follow-up: the prompt must not name a call the server is CERTAIN to refuse.

    Since ``validate_pvs`` refuses a non-``.bob`` ``file_path`` outright, teaching it for a CSV or
    a snapshot hands the client an impossible plan, the same failure this prompt had once before
    (the ``names`` to ``pv_names`` rename, see the changelog). The display tools ARE registered
    here, they just do not parse this kind of file, so the fallback has to be the read-it-yourself
    branch rather than the core-only wording about a missing tool.
    """
    result = compare_machine_state(
        "MPS:", reference_file=reference_file, display_tools_available=True
    )
    assert "validate_pvs(" not in result, (
        f"the prompt teaches a validate_pvs call for {reference_file!r}, which the tool refuses"
    )
    assert reference_file in result, "the file must still be part of the plan"
    assert "yourself" in result


@pytest.mark.parametrize("reference_file", ["status.bob", "STATUS.BOB", "beam.plt", "BEAM.PLT"])
def test_compare_machine_state_still_teaches_validate_pvs_for_a_readable_file(
    reference_file: str,
) -> None:
    """The negative control for the test above: a file the tool READS, in either case, keeps it.

    Without this, dropping the branch entirely would look like a fix.

    The ``.plt`` rows are GB-79: the tool now answers a Data Browser trend instead of refusing it,
    so sending the client down the read-it-yourself path would waste a call the server can make.
    The prompt reaches this branch through ``display_files.is_inventory_file``, so the two stay in
    step by construction rather than by anyone remembering to edit both.

    This module is deliberately NOT engine-coupled and must stay that way: ``prompts.py`` imports
    only ``display_files``, which imports nothing, so these rows keep running in the core-only CI
    lane where ``opi_navigation`` is absent.
    """
    result = compare_machine_state(
        "MPS:", reference_file=reference_file, display_tools_available=True
    )
    assert f'validate_pvs(file_path="{reference_file}"' in result


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

    def test_the_taught_command_line_carries_probe_pv(self) -> None:
        """BG-DOC: the EXAMPLE, not just the warning about it.

        The prompt explained what a run without ``--probe-pv`` does and does not prove, and then
        taught a command line without the flag. An assistant copies the line, not the paragraph
        underneath it, so the honest warning arrived attached to the very call that earns it.

        This pins the line rather than the flag's presence anywhere in the text, which is the
        distinction that failed before: the warning already mentioned ``--probe-pv``, so a test for
        the bare string was green while the example was wrong. Provably red: take the flag off the
        ``epics-init`` line in ``prompts.py`` and leave the surrounding prose alone.
        """
        result = setup_epics_mcp(presets=PRESETS)

        taught = [ln for ln in result.splitlines() if "epics-init --preset" in ln]
        assert taught, "the prompt no longer teaches an epics-init command line"
        assert all("--probe-pv" in ln for ln in taught), (
            f"a taught epics-init line omits --probe-pv: {taught}"
        )

    def test_it_names_the_three_states_that_look_healthy_and_are_not(self) -> None:
        """``?``, ``!`` and ``~`` are exit-code 0 or 3 states that a reader skims past as fine.
        They are the whole reason the doctor has more than two outcomes."""
        result = setup_epics_mcp(presets=PRESETS)

        assert "unverified" in result
        assert "identity probe failed" in result
        assert "no ingest" in result
