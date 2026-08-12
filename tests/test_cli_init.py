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
import pathlib
import shutil
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

    @pytest.mark.parametrize(
        "value",
        [
            "http://<ARCHIVER-HOST>:17665",
            "http://<Archiver-Host>:17665",
            "http://<archiver_host>:17665",
        ],
    )
    def test_a_placeholder_is_seen_whatever_case_the_reader_typed(self, value: str) -> None:
        """The presets spell theirs lower case, the VALUES do not have to (QA-68).

        ``open_placeholders`` is applied to everything a caller passes with ``--set``, not only to
        what a preset ships. Measured before this: ``--set
        EPICS_MCP_ARCHIVER_URL=http://<ARCHIVER-HOST>:17665`` was not seen, the check ran, and the
        report said ``Failed to resolve '%3carchiver-host%3e'`` - a DNS failure wearing the shape of
        a genuine finding, which is the exact outcome the refusal exists to prevent.
        """
        assert open_placeholders({"A": value}) == [f"A={value}"]

    @pytest.mark.parametrize(
        "pattern",
        [
            r"^SIM:(?P<dev>PS-01):Cur-SP$",
            r"^SIM:(?<dev>PS-01):Cur-SP$",
            r"^(?P<d>SIM):\k<d>$",
        ],
    )
    def test_a_named_regex_group_is_not_mistaken_for_a_placeholder(self, pattern: str) -> None:
        """The counter-direction of the one above, and not hypothetical: this configuration has a
        value that is a REGEX (QA-68).

        ``EPICS_MCP_PV_WRITE_PATTERN`` guards the PV write gate, and a named group carries angle
        brackets. Measured before this: passing one refused the check of a COMPLETE configuration
        and exited 0, so the loudest thing about that setup was a warning that nothing was wrong
        with it.
        """
        assert open_placeholders({"EPICS_MCP_PV_WRITE_PATTERN": pattern}) == []

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

    def test_the_cleared_search_variables_track_the_ones_the_doctor_actually_reads(self) -> None:
        """The list above is a LITERAL, and on its own it guards only one direction.

        WHY this second assertion exists, measured rather than argued (QA-68). ``presets`` and
        ``services.doctor`` each hold their own copy of the search-path variables, and
        ``presets._SEARCH_VARS`` says in its comment that it mirrors the doctor's. Nothing held
        that sentence: adding a seventh entry to ``doctor._SEARCH_LIST_VARS`` and leaving
        ``presets`` untouched left the WHOLE suite green (1805 passed, byte-identical to the clean
        run). The test above cannot catch it either, because it compares the literal to itself.

        That is the direction code actually grows in. A variable the doctor learns to read but the
        preset does not clear survives ``_run_check`` from the caller's shell, and the report then
        describes a reach the emitted block does not state, which is the one failure
        ``stale_config_vars`` exists to prevent.

        The two ``*_AUTO_ADDR_LIST`` names stay spelled out here rather than imported: the doctor
        picks ONE of them per provider (``doctor._live_search_posture``), so there is no collection
        on that side to compare against, and a preset has to clear both regardless of which
        provider it names. Duplication is not derived away, it is BOUND, which is the shape
        ``tests/test_presets_match_examples.py`` uses for the same class of problem.
        """
        from epics_mcp.presets import _SEARCH_VARS
        from epics_mcp.services.doctor import _SEARCH_LIST_VARS

        auto_switches = {"EPICS_PVA_AUTO_ADDR_LIST", "EPICS_CA_AUTO_ADDR_LIST"}

        assert set(_SEARCH_VARS) == set(_SEARCH_LIST_VARS) | auto_switches

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


class TestWritingTheBlock:
    """``--out``, ``--force`` and ``--absolute-command``: the one file this command may write.

    The check is stubbed for the same two reasons as ``TestCommandLine``: these tests are not about
    it, and ``_run_check`` mutates ``os.environ`` in a way that outlives the test.
    """

    @pytest.fixture(autouse=True)
    def _stubbed_check(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli_init, "_run_check", lambda env, probe_pv: 0)

    def test_out_writes_the_exact_bytes_of_the_block(self, tmp_path: pathlib.Path) -> None:
        """The whole point of the option, and it has to be asserted on BYTES.

        A text-mode assertion cannot see either half of what this fixes: ``read_text`` decodes a
        byte-order mark away without saying so, and on Windows an unspecified ``newline`` turns
        every LF into CRLF. Measured on the sandbox block, those two differ by 15 bytes, and a
        "does it parse" assertion passes on both. So the file is compared to the exact bytes, and
        the two properties are then named individually so a failure says which one broke.
        """
        target = tmp_path / "mcp.json"
        expected = (render_client_config(PRESETS["sandbox"].env) + "\n").encode("utf-8")

        exit_code = cli_init.main(["--preset", "sandbox", "--no-check", "--out", str(target)])
        written = target.read_bytes()

        assert exit_code == 0
        assert written == expected
        assert not written.startswith(b"\xef\xbb\xbf")  # no byte-order mark
        assert b"\r\n" not in written  # LF, whatever platform wrote it

    def test_the_block_stays_ascii_which_is_why_the_encoding_cannot_be_proven(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pins the premise that makes the write's explicit ``encoding`` unprovable, so the premise
        goes red if it ever changes instead of the reasoning quietly rotting.

        Measured: ``json.dumps`` escapes every non-ASCII character as a ``\\uXXXX`` sequence, so the
        block is pure ASCII even when a value is not, and removing ``encoding="utf-8"`` from the
        write changes no byte of it. A mutation sweep therefore cannot kill that parameter, and
        saying "the test proves the encoding" would be false. It stays for the day someone renders
        with ``ensure_ascii=False``, when the platform locale would otherwise decide the bytes
        silently. On that day this test fails and names the write as the place to look.

        The non-ASCII input arrives through a faked resolver, which is also the realistic route: an
        install path under an account whose name carries an accent.
        """
        resolved = str(tmp_path / "rene" / "epics-mcp").replace("rene", "rené")
        monkeypatch.setattr(cli_init, "_resolve_absolute_command", lambda: resolved)
        target = tmp_path / "mcp.json"

        exit_code = cli_init.main(
            ["--preset", "sandbox", "--no-check", "--out", str(target), "--absolute-command"]
        )
        written = target.read_bytes()

        assert exit_code == 0
        assert written.isascii()  # the premise: escaped on the way out
        assert json.loads(written.decode("ascii"))["mcpServers"][SERVER_KEY]["command"] == resolved

    def test_out_leaves_stdout_pipeable(
        self, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Writing a file does not take the block off stdout: the stream contract is unchanged, so
        an existing pipeline keeps working and the file is an addition rather than a replacement."""
        target = tmp_path / "mcp.json"

        cli_init.main(["--preset", "sandbox", "--no-check", "--out", str(target)])
        captured = capsys.readouterr()

        assert json.loads(captured.out)["mcpServers"][SERVER_KEY]["command"] == SERVER_COMMAND
        assert "wrote" in captured.err  # what it did goes to stderr, as everything else does

    def test_out_refuses_an_existing_file_and_leaves_it_untouched(
        self, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A client configuration usually holds OTHER servers, so a default overwrite would destroy
        work that has nothing to do with this one. The negative control is the point: the refusal
        is worth nothing if the file was already gone by the time it was reported."""
        target = tmp_path / "mcp.json"
        target.write_text("{}\n", encoding="utf-8")

        exit_code = cli_init.main(["--preset", "sandbox", "--no-check", "--out", str(target)])

        assert exit_code == 2
        assert target.read_text(encoding="utf-8") == "{}\n"  # NOT overwritten
        assert "--force" in capsys.readouterr().err  # and the way out is named

    def test_force_replaces_an_existing_file(self, tmp_path: pathlib.Path) -> None:
        """The positive control for the refusal above: with the flag, the write goes through."""
        target = tmp_path / "mcp.json"
        target.write_text("{}\n", encoding="utf-8")

        exit_code = cli_init.main(
            ["--preset", "sandbox", "--no-check", "--out", str(target), "--force"]
        )

        assert exit_code == 0
        assert json.loads(target.read_text(encoding="utf-8"))["mcpServers"]

    def test_out_refuses_a_missing_parent_directory(
        self, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The common typo. Without this it surfaces as a bare traceback, which reads like a defect
        in the command rather than a path the caller can correct."""
        target = tmp_path / "nowhere" / "mcp.json"

        exit_code = cli_init.main(["--preset", "sandbox", "--no-check", "--out", str(target)])

        assert exit_code == 2
        assert not target.exists()
        assert "does not exist" in capsys.readouterr().err

    def test_out_writes_nothing_while_placeholders_remain(
        self, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """THE trap this option could have introduced, and the reason the write sits after the
        placeholder branch rather than at the emit.

        Writing an unfinished block would leave a file that the obvious next command, the same run
        with ``--set``, then refuses to replace. The reader would be told to fill the values in and
        then blocked from doing it. ``sandbox`` cannot show this (it is the only preset with no
        placeholder), so the test uses one that can.
        """
        target = tmp_path / "mcp.json"

        exit_code = cli_init.main(["--preset", "ioc-archiver", "--out", str(target)])
        captured = capsys.readouterr()

        assert exit_code == 0
        assert not target.exists()
        assert "nothing was written" in captured.err
        assert "still placeholders" in captured.err  # and the reader is told what to fill in
        json.loads(captured.out)  # the block itself is still emitted, for reading

    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param(["--list", "--out", "x.json"], id="list-cannot-be-written"),
            pytest.param(["--preset", "sandbox", "--force"], id="force-without-out"),
        ],
    )
    def test_meaningless_combinations_are_usage_errors(self, argv: list[str]) -> None:
        """Refused by the parser, so they exit 2 like every other usage error and produce no output
        to clean up afterwards."""
        with pytest.raises(SystemExit) as exit_info:
            cli_init.main(argv)

        assert exit_info.value.code == 2

    def test_absolute_command_names_the_resolved_path(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The resolver is faked rather than exercised: a real lookup returns a path carrying this
        machine's user name, and the repo-wide facility guard rejects one of those in a tracked
        file. What matters here is that whatever it returns reaches the block."""
        resolved = str(tmp_path / "bin" / "epics-mcp")
        monkeypatch.setattr(cli_init, "_resolve_absolute_command", lambda: resolved)
        target = tmp_path / "mcp.json"

        exit_code = cli_init.main(
            ["--preset", "sandbox", "--no-check", "--out", str(target), "--absolute-command"]
        )
        document = json.loads(target.read_text(encoding="utf-8"))

        assert exit_code == 0
        assert document["mcpServers"][SERVER_KEY]["command"] == resolved

    def test_absolute_command_refuses_rather_than_falling_back(
        self,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A silent fallback to the bare name would emit exactly the block the caller asked not to
        have, and it would fail later, inside the client, where the cause is invisible. So an
        unresolvable command is loud, and nothing is written."""
        monkeypatch.setattr(cli_init, "_resolve_absolute_command", lambda: None)
        target = tmp_path / "mcp.json"

        exit_code = cli_init.main(
            ["--preset", "sandbox", "--no-check", "--out", str(target), "--absolute-command"]
        )

        assert exit_code == 2
        assert not target.exists()
        assert "cannot resolve" in capsys.readouterr().err

    def test_an_armed_write_gate_is_named_because_the_check_cannot_see_it(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``epics-doctor`` probes service planes and never reads the write gates, so the report
        that follows is silent about the most consequential thing in the block. It is reachable
        only through ``--set``, since no preset arms a gate."""
        cli_init.main(
            ["--preset", "sandbox", "--no-check", "--set", "EPICS_MCP_ALLOW_PV_WRITE=true"]
        )

        assert "ARMS a write gate" in capsys.readouterr().err

    def test_an_armed_write_gate_is_named_even_when_placeholders_remain(
        self, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The warning has to reach the branch it is most likely to be needed on.

        It used to be emitted after the placeholder branch had already returned, so the one preset
        family that carries `--set` overrides, the ones with placeholders, was exactly the family
        that never heard it. The block is printed on that path, so the gate is armed in text the
        reader now holds; the warning belongs with it.
        """
        cli_init.main(
            [
                "--preset",
                "ioc-archiver",
                "--out",
                str(tmp_path / "mcp.json"),
                "--set",
                "EPICS_MCP_ALLOW_OLOG_WRITE=true",
            ]
        )
        captured = capsys.readouterr()

        assert "ARMS a write gate" in captured.err
        assert "still placeholders" in captured.err  # and the other refusal is still reported

    def test_the_resolved_command_is_absolute(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The option is called --absolute-command, so a relative answer would defeat it.

        ``shutil.which`` joins each PATH entry as written, so a relative entry (or, on Windows, the
        current directory joining the search) yields a relative path that satisfies the lookup and
        then fails inside a client resolving it from a directory nobody chose. The resolver is
        faked, because the real PATH on any given machine may not contain a relative entry at all.
        """
        monkeypatch.setattr(shutil, "which", lambda _name: os.path.join(".", "epics-mcp"))

        resolved = cli_init._resolve_absolute_command()

        assert resolved is not None
        assert os.path.isabs(resolved)
        assert resolved.endswith("epics-mcp")

    def test_an_ordinary_block_carries_no_write_gate_warning(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The negative control: a warning that appears on every run is one a reader learns to
        skip, which would cost exactly the case above."""
        cli_init.main(["--preset", "sandbox", "--no-check"])

        assert "ARMS a write gate" not in capsys.readouterr().err


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
