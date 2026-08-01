"""Tests for the epics-init CLI and the preset data behind it.

Two properties carry most of the weight here and neither is obvious from reading the code:

* the emitted block goes to STDOUT and everything the command says goes to STDERR, which is what
  makes ``epics-init --preset X > .mcp.json`` produce a usable file rather than one with a warning
  paragraph in it;
* every preset disables the EPICS auto-address search EXPLICITLY. Omitting those two lines is
  silent: EPICS defaults that search to ON, so the config would broadcast PV searches into the
  local subnets while looking narrower than it is. A test is the only thing that notices.
"""

from __future__ import annotations

import json

import pytest

from epics_mcp import cli_init
from epics_mcp.presets import (
    PRESETS,
    SERVER_COMMAND,
    SERVER_KEY,
    Preset,
    open_placeholders,
    render_client_config,
    with_overrides,
)


def _emitted_env(capsys: pytest.CaptureFixture[str]) -> dict[str, str]:
    """Parse the captured stdout as the client config and return its env block."""
    document = json.loads(capsys.readouterr().out)
    return dict(document["mcpServers"][SERVER_KEY]["env"])


class TestPresetData:
    """The four shapes as data, without going through the command line."""

    def test_all_presets_are_keyed_by_their_own_name(self) -> None:
        """A preset looked up under one name and calling itself another would make every error
        message point at the wrong thing."""
        mismatched = sorted(key for key, preset in PRESETS.items() if preset.name != key)

        assert mismatched == []

    @pytest.mark.parametrize("preset", PRESETS.values(), ids=lambda preset: preset.name)
    def test_every_preset_disables_the_auto_address_search(self, preset: Preset) -> None:
        """The silent one. EPICS defaults ``*_AUTO_ADDR_LIST`` to ON when unset, so a preset that
        omits these two lines broadcasts PV searches into the local subnets, and nothing in the
        emitted block would hint at it. Both protocols, because the server can be pointed at
        either."""
        assert preset.env.get("EPICS_PVA_AUTO_ADDR_LIST") == "NO"
        assert preset.env.get("EPICS_CA_AUTO_ADDR_LIST") == "NO"

    def test_sandbox_is_the_only_preset_that_needs_no_editing(self) -> None:
        """``sandbox`` is the one shape that is complete as emitted, which is what makes it the
        workshop preset; the other three describe a facility nobody here can know the hosts of.
        Pinned in both directions so neither half can drift alone."""
        needs_editing = sorted(
            name for name, preset in PRESETS.items() if open_placeholders(preset.env)
        )

        assert needs_editing == ["full", "ioc-archiver", "ioc-only"]
        assert open_placeholders(PRESETS["sandbox"].env) == []

    def test_open_placeholders_names_the_entry_not_just_the_variable(self) -> None:
        """The caller prints these verbatim, so the value has to travel with the name: a reader
        told only ``EPICS_MCP_ARCHIVER_URL`` still has to go and look at what it says."""
        pending = open_placeholders({"A": "http://<archiver-host>:17665", "B": "http://real:1"})

        assert pending == ["A=http://<archiver-host>:17665"]

    def test_a_filled_value_is_not_a_placeholder(self) -> None:
        """The counter-direction: without it, a detector that flagged everything would pass the
        test above and refuse every check forever."""
        assert open_placeholders({"A": "http://archiver.example.org:17665"}) == []

    def test_overrides_replace_in_place_and_additions_append(self) -> None:
        """Key order is the reading order of the emitted block, so an override must not move the
        variable it replaces; a genuinely new variable has nowhere to go but the end."""
        merged = with_overrides({"A": "1", "B": "2"}, {"A": "9", "C": "3"})

        assert list(merged.items()) == [("A", "9"), ("B", "2"), ("C", "3")]

    def test_rendering_is_deterministic(self) -> None:
        """Same input, same bytes: the block is meant to be diffed against a checked-in file."""
        assert render_client_config(PRESETS["full"].env) == render_client_config(
            PRESETS["full"].env
        )


class TestCommandLine:
    """The CLI surface: streams, exit codes, and the usage errors."""

    def test_list_describes_every_preset_and_exits_zero(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        exit_code = cli_init.main(["--list"])
        out = capsys.readouterr().out

        assert exit_code == 0
        for name in PRESETS:
            assert name in out

    def test_the_block_is_valid_json_naming_the_server_and_its_command(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        exit_code = cli_init.main(["--preset", "sandbox"])
        document = json.loads(capsys.readouterr().out)

        assert exit_code == 0
        assert document["mcpServers"][SERVER_KEY]["command"] == SERVER_COMMAND

    def test_a_complete_preset_says_nothing_on_stderr(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``sandbox`` needs no editing, so warning about it would train the reader to ignore the
        warning that matters."""
        cli_init.main(["--preset", "sandbox"])

        assert capsys.readouterr().err == ""

    def test_placeholders_are_reported_on_stderr_and_leave_stdout_pipeable(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """THE stream contract. Redirecting stdout has to yield a parseable config even while the
        command is complaining, otherwise ``> .mcp.json`` produces a broken file."""
        exit_code = cli_init.main(["--preset", "full"])
        captured = capsys.readouterr()

        assert exit_code == 0
        json.loads(captured.out)  # would raise if the warning had leaked into stdout
        assert "EPICS_MCP_ARCHIVER_URL=http://<archiver-host>:17665" in captured.err

    def test_set_replaces_a_preset_value(self, capsys: pytest.CaptureFixture[str]) -> None:
        cli_init.main(
            ["--preset", "ioc-archiver", "--set", "EPICS_MCP_ARCHIVER_URL=http://arch:17665"]
        )

        assert _emitted_env(capsys)["EPICS_MCP_ARCHIVER_URL"] == "http://arch:17665"

    def test_set_can_add_a_variable_no_preset_carries(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The presets cover the common shapes, not every variable the config reads, so ``--set``
        has to be able to introduce one rather than reject it."""
        cli_init.main(["--preset", "sandbox", "--set", "EPICS_MCP_CA_BUNDLE=/etc/ssl/combined.pem"])

        assert _emitted_env(capsys)["EPICS_MCP_CA_BUNDLE"] == "/etc/ssl/combined.pem"

    def test_filling_every_placeholder_silences_the_warning(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Closes the loop on the warning: it has to be answerable, not just present."""
        cli_init.main(
            [
                "--preset",
                "ioc-only",
                "--set",
                "EPICS_PVA_ADDR_LIST=10.0.0.1",
                "--set",
                "EPICS_CA_ADDR_LIST=10.0.0.1",
            ]
        )

        assert capsys.readouterr().err == ""

    def test_a_value_may_contain_an_equals_sign(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Split on the FIRST separator only: a regex allowlist or a URL with a query string
        legitimately carries more."""
        cli_init.main(["--preset", "sandbox", "--set", "EPICS_MCP_PV_WRITE_PATTERN=^A=B$"])

        assert _emitted_env(capsys)["EPICS_MCP_PV_WRITE_PATTERN"] == "^A=B$"

    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param([], id="nothing-asked-for"),
            pytest.param(["--preset", "sandbox", "--list"], id="both-at-once"),
            pytest.param(["--preset", "no-such-shape"], id="unknown-preset"),
            pytest.param(["--preset", "sandbox", "--set", "no-equals-sign"], id="malformed-set"),
            pytest.param(["--preset", "sandbox", "--set", "=value"], id="empty-name"),
        ],
    )
    def test_usage_errors_exit_two(self, argv: list[str]) -> None:
        """argparse's own code, and the same one the doctor uses, so a script sees one meaning of
        "you asked for something impossible" across the whole command set."""
        with pytest.raises(SystemExit) as exit_info:
            cli_init.main(argv)

        assert exit_info.value.code == 2
