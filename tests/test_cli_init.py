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
import os
import subprocess
import sys

import pytest

from epics_mcp import cli_doctor, cli_init
from epics_mcp.presets import (
    PRESETS,
    SERVER_COMMAND,
    SERVER_KEY,
    Preset,
    configures_a_rest_plane,
    open_placeholders,
    render_client_config,
    stale_config_vars,
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
    """The CLI surface: streams, exit codes, and the usage errors.

    The check is stubbed out for the whole class, for two reasons that are easy to miss. It would
    make these tests do real work they are not about, and, worse, ``_run_check`` MUTATES
    ``os.environ``: an unstubbed run here would strip this process's own EPICS configuration and
    leave it stripped for every test that follows.
    """

    @pytest.fixture(autouse=True)
    def _stubbed_check(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli_init, "_run_check", lambda env, probe_pv: 0)

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

    def test_a_complete_preset_is_not_reported_as_needing_edits(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``sandbox`` needs no editing, so warning about it would train the reader to ignore the
        warning that matters.

        Asserts the ABSENCE of that specific warning rather than an empty stream: this command has
        more than one thing to say on stderr (the doctor's report, the empty-check warning), and a
        blanket "stderr is silent" would fail on either while claiming something about
        placeholders.
        """
        cli_init.main(["--preset", "sandbox"])

        assert "still placeholders" not in capsys.readouterr().err

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

    def test_filling_every_placeholder_silences_that_warning(
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

        assert "still placeholders" not in capsys.readouterr().err

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


class TestEnvironmentIsolation:
    """``stale_config_vars`` as data: which of the caller's variables must not survive a preset."""

    def test_every_config_prefixed_variable_is_stale(self) -> None:
        """``EpicsConfig`` binds the whole ``EPICS_MCP_`` prefix, so any of them left standing
        would be read as part of the preset."""
        assert stale_config_vars(["EPICS_MCP_OLOG_URL", "EPICS_MCP_CA_BUNDLE"]) == [
            "EPICS_MCP_CA_BUNDLE",
            "EPICS_MCP_OLOG_URL",
        ]

    def test_the_search_path_variables_are_stale_although_they_lack_the_prefix(self) -> None:
        """The doctor reads these six straight from the environment, not through EpicsConfig, so a
        prefix-only rule would leave the caller's PV search reach in place while the emitted block
        claims a different one."""
        search = [
            "EPICS_PVA_ADDR_LIST",
            "EPICS_CA_ADDR_LIST",
            "EPICS_PVA_NAME_SERVERS",
            "EPICS_CA_NAME_SERVERS",
            "EPICS_PVA_AUTO_ADDR_LIST",
            "EPICS_CA_AUTO_ADDR_LIST",
        ]

        assert stale_config_vars(search) == sorted(search)

    def test_unrelated_epics_variables_are_left_alone(self) -> None:
        """The counter-direction, and a real boundary rather than politeness: probing a preset
        answers "is this CONFIGURATION sound", so transport tuning outside the config and the
        search path stays untouched. Stripping every ``EPICS_`` variable would test the preset in
        an environment no client ever runs in."""
        assert stale_config_vars(["EPICS_BASE", "EPICS_CA_MAX_ARRAY_BYTES", "PATH"]) == []


class TestTheCheck:
    """The doctor hand-off. ``cli_doctor.main`` is replaced throughout, so nothing here touches a
    network: what is under test is the seam, not the doctor."""

    @pytest.fixture
    def seen(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
        """Run against a throwaway environment and capture what the doctor would have seen.

        ``os.environ`` is swapped for a plain dict because the code under test DELETES from it;
        monkeypatch's own setenv/delenv could not restore what it never recorded.
        """
        record: dict[str, object] = {"exit_code": 0}
        monkeypatch.setattr(
            os,
            "environ",
            {
                "PATH": "/usr/bin",
                "EPICS_MCP_OLOG_URL": "http://inherited-olog:8080/Olog",
                "EPICS_PVA_ADDR_LIST": "10.9.9.9",
            },
        )

        def fake_doctor(argv: list[str] | None = None) -> int:
            record["argv"] = list(argv or [])
            record["environ"] = dict(os.environ)
            sys.stdout.write("doctor report\n")
            code = record["exit_code"]
            assert isinstance(code, int)
            return code

        monkeypatch.setattr(cli_doctor, "main", fake_doctor)
        return record

    def test_the_preset_reaches_the_doctor(self, seen: dict[str, object]) -> None:
        """The doctor takes no config argument and reads the process environment, so the preset has
        to BE the environment by the time it runs."""
        cli_init.main(["--preset", "sandbox"])
        environ = seen["environ"]

        assert isinstance(environ, dict)
        assert environ["EPICS_PVA_ADDR_LIST"] == "127.0.0.1"
        assert environ["EPICS_PVA_AUTO_ADDR_LIST"] == "NO"

    def test_an_inherited_plane_does_not_survive_into_the_check(
        self, seen: dict[str, object]
    ) -> None:
        """THE correctness property of probing a preset. ``sandbox`` enables no Olog, so a report
        mentioning one would describe a configuration that exists nowhere: half preset, half shell.
        For an operator whose shell points at a production facility, that is the difference between
        a report about localhost and a report about the facility."""
        cli_init.main(["--preset", "sandbox"])
        environ = seen["environ"]

        assert isinstance(environ, dict)
        assert "EPICS_MCP_OLOG_URL" not in environ

    def test_unrelated_variables_survive_into_the_check(self, seen: dict[str, object]) -> None:
        """The counter-direction: clearing everything would be a different bug in the same place."""
        cli_init.main(["--preset", "sandbox"])
        environ = seen["environ"]

        assert isinstance(environ, dict)
        assert environ["PATH"] == "/usr/bin"

    def test_the_report_goes_to_stderr_so_the_block_stays_pipeable(
        self, seen: dict[str, object], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The doctor writes its report to stdout by design; here the payload is the config block,
        so redirecting stdout must still yield parseable JSON."""
        cli_init.main(["--preset", "sandbox"])
        captured = capsys.readouterr()

        json.loads(captured.out)
        assert "doctor report" in captured.err

    def test_probe_pv_is_threaded_through(self, seen: dict[str, object]) -> None:
        cli_init.main(["--preset", "sandbox", "--probe-pv", "SIM:PS-01:Cur-RB"])

        assert seen["argv"] == ["--probe-pv", "SIM:PS-01:Cur-RB"]

    @pytest.mark.parametrize("code", [0, 1, 3])
    def test_the_doctors_exit_code_is_passed_through(
        self, seen: dict[str, object], code: int
    ) -> None:
        """Whether the setup works is the scriptable question, and answering it with a second set
        of numbers would help nobody."""
        seen["exit_code"] = code

        assert cli_init.main(["--preset", "sandbox"]) == code

    def test_no_check_skips_the_doctor_entirely(self, seen: dict[str, object]) -> None:
        cli_init.main(["--preset", "sandbox", "--no-check"])

        assert "environ" not in seen

    def test_open_placeholders_refuse_the_check(self, seen: dict[str, object]) -> None:
        """Probing ``<archiver-host>`` yields a DNS failure shaped exactly like a real finding, and
        the reader cannot tell it from one. A named refusal beats a report to be discounted."""
        cli_init.main(["--preset", "full"])

        assert "environ" not in seen

    def test_filling_the_placeholders_lets_the_check_run(self, seen: dict[str, object]) -> None:
        """The refusal has to be answerable, otherwise those presets could never be checked."""
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

        assert "environ" in seen


class TestTheEmptyCheckWarning:
    """A clean report that confirmed nothing has to say so."""

    @pytest.fixture(autouse=True)
    def _silent_doctor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(os, "environ", {})
        monkeypatch.setattr(cli_doctor, "main", lambda argv=None: 0)

    def test_a_rest_less_preset_without_a_probe_pv_is_called_out(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Without ``--probe-pv`` the live plane is reported and never contacted, so ``sandbox``
        produces a clean run in which nothing was probed. Exit 0 next to the word OK reads as
        confirmation to someone who came here to have their setup confirmed."""
        cli_init.main(["--preset", "sandbox"])

        assert "NOT that anything works" in capsys.readouterr().err

    def test_a_probe_pv_removes_the_warning(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Because then something WAS contacted."""
        cli_init.main(["--preset", "sandbox", "--probe-pv", "SIM:PS-01:Cur-RB"])

        assert "NOT that anything works" not in capsys.readouterr().err

    def test_a_preset_with_a_rest_plane_does_not_get_the_warning(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A REST plane gives the doctor an identity to probe, so the report means something on
        its own."""
        cli_init.main(
            [
                "--preset",
                "ioc-archiver",
                "--set",
                "EPICS_PVA_ADDR_LIST=10.0.0.1",
                "--set",
                "EPICS_CA_ADDR_LIST=10.0.0.1",
                "--set",
                "EPICS_MCP_ARCHIVER_URL=http://arch:17665",
            ]
        )

        assert "NOT that anything works" not in capsys.readouterr().err

    def test_an_empty_url_does_not_count_as_a_configured_plane(self) -> None:
        """An empty value disables a plane, which is how ``--set VAR=`` removes one. Counting the
        KEY rather than the value would silence the warning for a preset that probes nothing."""
        assert not configures_a_rest_plane({"EPICS_MCP_ARCHIVER_URL": ""})
        assert configures_a_rest_plane({"EPICS_MCP_ARCHIVER_URL": "http://arch:17665"})


def test_no_import_builds_the_config_singleton() -> None:
    """The assumption the whole seam rests on, pinned in a FRESH interpreter.

    ``get_config`` caches into a process global on first call, so if any import along the way built
    it, the doctor would answer from the environment as it was at import time and every variable
    this command sets afterwards would be ignored. Nothing would fail loudly; the report would
    simply describe the wrong configuration. A subprocess is required because by the time this test
    runs, the suite has long since built the singleton.
    """
    probe = (
        "import epics_mcp.config as c;"
        "import epics_mcp.cli_init, epics_mcp.cli_doctor, epics_mcp.services.doctor;"
        "print(c._config)"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )

    assert result.stdout.strip() == "None"
