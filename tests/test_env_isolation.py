"""The shell's server settings never reach an offline test (GQ-399): decision and wiring.

Two halves, because either one alone proves less than it looks like:

* the DECISION, :func:`tests.env_isolation.server_config_names`, over injected environments, with a
  negative beside every positive;
* the WIRING, which no in-process test can see: by the time a test body runs, the autouse fixture in
  ``tests/conftest.py`` has already stripped whatever the shell exported, and CI exports nothing at
  all, so an in-process check is green with and without the fixture. The wiring is therefore driven
  the only way it can be, in a subprocess that is HANDED a server setting, next to a counter-run
  with ``--noconftest`` that proves the setting really arrived.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from epics_mcp.config import _RESERVED_ENV_REMAINDER_PREFIXES
from tests.env_isolation import server_config_names

_REPO = Path(__file__).resolve().parents[1]

#: A real ``EpicsConfig`` setting whose leak was measured: at limit 1 it turned 46 tests red, full
#: suite on ``4240267``, 2026-09-13.
_LEAKING_SETTING = "EPICS_MCP_READ_RATE_LIMIT"


# --- the decision ------------------------------------------------------------------------------


def test_server_settings_and_typos_are_selected() -> None:
    env = {"EPICS_MCP_READ_RATE_LIMIT", "EPICS_MCP_CHANNELFINDER_URL", "EPICS_MCP_CHANNELFINDR_URL"}
    assert server_config_names(env, live=False) == sorted(env)


def test_names_outside_the_prefix_are_left_alone() -> None:
    """The search-path variables have their own fixture; a name that merely CONTAINS the prefix, or
    shares its first letters, configures nothing here."""
    env = {"EPICS_PVA_ADDR_LIST", "EPICS_CA_AUTO_ADDR_LIST", "EPICS_MCPX", "MY_EPICS_MCP_URL"}
    assert server_config_names(env, live=False) == []


@pytest.mark.parametrize("remainder", _RESERVED_ENV_REMAINDER_PREFIXES)
def test_every_reserved_harness_family_is_kept(remainder: str) -> None:
    """Derived from the config's own reservation rather than typed out, so a family added there is
    covered here the same day. The server setting beside it is the counter-probe: a function that
    kept EVERYTHING would pass the first assertion alone."""
    harness = f"EPICS_MCP_{remainder.upper()}PROBE"
    assert server_config_names({harness, _LEAKING_SETTING}, live=False) == [_LEAKING_SETTING]


def test_the_reserved_families_are_not_empty() -> None:
    """The floor under the parametrisation above: an empty tuple would collect zero cases."""
    assert _RESERVED_ENV_REMAINDER_PREFIXES


def test_matching_ignores_case_the_way_the_model_does() -> None:
    env = {"epics_mcp_read_rate_limit", "Epics_Mcp_Live_Cf_Glob"}
    assert server_config_names(env, live=False) == ["epics_mcp_read_rate_limit"]


def test_a_live_test_keeps_everything() -> None:
    env = {_LEAKING_SETTING, "EPICS_MCP_CHANNELFINDER_URL", "EPICS_MCP_LIVE_CF_GLOB"}
    assert server_config_names(env, live=True) == []
    assert server_config_names(env, live=False) == [
        "EPICS_MCP_CHANNELFINDER_URL",
        _LEAKING_SETTING,
    ]


# --- the wiring --------------------------------------------------------------------------------


def test_probe_sees_no_server_setting() -> None:
    """The probe the subprocess test below drives. In-suite it holds because the fixture ran; it
    only becomes evidence when a setting was handed in, which is the next test's job."""
    assert server_config_names(os.environ, live=False) == []


def _run_probe(*extra: str) -> subprocess.CompletedProcess[str]:
    node = "tests/test_env_isolation.py::test_probe_sees_no_server_setting"
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *extra, node],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=_REPO,
        env={**os.environ, _LEAKING_SETTING: "1"},
        timeout=300,
        check=False,
    )


def test_an_exported_server_setting_does_not_reach_an_offline_test() -> None:
    """With conftest the probe passes; with ``--noconftest`` the same probe, handed the same
    environment, fails. The second run is what makes the first one mean anything: without it a
    child that never received the setting would pass too."""
    stripped = _run_probe()
    assert stripped.returncode == 0, (
        f"an exported {_LEAKING_SETTING} reached an offline test through tests/conftest.py:\n"
        f"{(stripped.stdout + stripped.stderr)[-1500:]}"
    )
    assert "1 passed" in stripped.stdout, stripped.stdout[-800:]

    unstripped = _run_probe("--noconftest")
    assert unstripped.returncode == 1, (
        "without conftest the probe should see the setting and fail, so the run above proves "
        f"nothing:\n{(unstripped.stdout + unstripped.stderr)[-1500:]}"
    )
    assert "1 failed" in unstripped.stdout, unstripped.stdout[-800:]
