"""Tests for epics_mcp.resources."""

import pytest

from epics_mcp.config import EpicsConfig
from epics_mcp.resources import get_epics_config, get_health


def _with_config(monkeypatch: pytest.MonkeyPatch, **kwargs: object) -> None:
    """Point both resources at one explicit configuration.

    A module-local rebind, the pattern the rest of the suite uses, rather than surgery on the
    ``get_config`` singleton: ``resources`` imports the name, so replacing it there is complete and
    leaves no state for the next test to inherit.
    """
    cfg = EpicsConfig(**kwargs)  # type: ignore[arg-type]
    monkeypatch.setattr("epics_mcp.resources.get_config", lambda: cfg)


#: Every top-level key the two resources put on the wire, declared rather than derived: an
#: expectation read off the payload is satisfied by whatever the payload happens to say, which is
#: the one thing these two cannot check. Both URIs are documented in ``docs/tools.md`` and are what
#: ``docs/deployment.md`` sends an approver to, so a key appearing or vanishing is a contract change
#: and has to show up in a diff rather than in a support question.
_HEALTH_KEYS = frozenset(
    {
        "server",
        "version",
        "status",
        "provider",
        "any_write_gate_armed",
        "write_enabled",
        "write_pattern",
        "write_pattern_allows_every_name",
        "write_rate_limit",
        "olog_write",
        "uptime_seconds",
        "python_version",
        "p4p_version",
        "channelfinder_enabled",
        "archiver_enabled",
        "alarm_enabled",
        "olog_enabled",
    }
)

_CONFIG_KEYS = frozenset(
    {
        "provider",
        "default_timeout",
        "max_batch_size",
        "max_monitor_duration",
        "max_monitor_events",
        "allow_pv_write",
        "pv_write_pattern",
        "write_rate_limit",
        "channelfinder_url",
        "archiver_url",
        "alarm_url",
    }
)


def test_health_key_set_is_exact() -> None:
    """The key set was unpinned, so a key could be added or dropped with the whole suite green.

    ``test_health_shape`` below asserts a SUBSET deliberately, which is the right shape for "these
    are mandatory" and the wrong one for "this is the wire contract": it cannot see an addition at
    all, and that is how the payload came to describe four of the seven planes without anyone
    deciding to. This one is the second half, and it is meant to go red on every future edit.
    """
    assert set(get_health()) == set(_HEALTH_KEYS)


def test_an_armed_olog_gate_is_visible_in_health(monkeypatch: pytest.MonkeyPatch) -> None:
    """A server that may create logbook entries reported that it does not write, and nothing else.

    That is the whole ticket. The PV gate was the only writing this payload could describe, so the
    field an approver reads first said "false" about a server whose Olog gate is armed, with a
    service account, an allowlist and a durable audit trail behind it. The CONTRAST is asserted
    rather than only the new field, so the test states the misreading it exists to prevent: a reader
    who stops at write_enabled is still wrong, and now something else in the payload says so.
    """
    _with_config(
        monkeypatch,
        olog_url="http://localhost:8080/Olog",
        allow_olog_write=True,
        olog_write_logbooks="Commissioning, Operations",
        audit_log_file="audit.log",
    )
    health = get_health()

    assert health["write_enabled"] is False, "the PV gate is off, and that much was always right"
    assert health["any_write_gate_armed"] is True, "but this server writes, which was invisible"
    assert health["olog_write"] == {
        "armed": True,
        "logbooks": ["Commissioning", "Operations"],
        "rate_limit_per_minute": 5,
        "target_allowed": True,
        "target_is_loopback": True,
    }


def test_an_armed_olog_gate_with_no_logbooks_is_not_read_as_unrestricted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An EMPTY allowlist on an armed gate is deny-all, and the naive reading is its exact opposite.

    Worth its own test because the two gates are fail-closed in DIFFERENT shapes: an empty PV
    pattern refuses the start, an empty logbook list starts and denies every write. A payload that
    showed ``armed: true`` beside an empty list without anything else distinguishing the two would
    invite the reading that nothing constrains this gate.
    """
    _with_config(
        monkeypatch,
        olog_url="http://localhost:8080/Olog",
        allow_olog_write=True,
        audit_log_file="audit.log",
    )
    olog = get_health()["olog_write"]
    assert isinstance(olog, dict)

    assert olog["armed"] is True
    assert olog["logbooks"] == []


def test_config_key_set_is_exact() -> None:
    """The same pin for the configuration payload, and it is the more exposed of the two.

    Its keys carry service URLs, so an addition here is not only a contract change but a disclosure
    decision, and ``test_config_no_secrets`` cannot make it: that one reads key NAMES, never values.
    """
    assert set(get_epics_config()) == set(_CONFIG_KEYS)


def test_health_shape() -> None:
    result = get_health()
    expected_keys = {
        "server",
        "version",
        "status",
        "provider",
        "write_enabled",
        "uptime_seconds",
        "python_version",
        "p4p_version",
    }
    assert expected_keys.issubset(result.keys())


def test_health_values() -> None:
    result = get_health()
    assert result["server"] == "epics-mcp"
    assert result["status"] == "ok"
    assert result["write_enabled"] is False
    # REST services are disabled by default (localhost isolation preserved).
    assert result["channelfinder_enabled"] is False
    assert result["archiver_enabled"] is False
    assert result["alarm_enabled"] is False


def test_health_version_matches_package_version() -> None:
    """S1-2: the health resource must report the real package ``__version__`` (locks the wiring:
    the old shape test only checked the key was present, not its value)."""
    from epics_mcp import __version__

    assert get_health()["version"] == __version__


def test_low_level_server_version_attribute_exists() -> None:
    """S1-2 early-warning guard: since the standalone-fastmcp migration (6bd12c6), server.py sets
    the handshake version through the PUBLIC constructor:
    ``FastMCP("epics-mcp", version=__version__)``: which standalone FastMCP mirrors onto the
    PRIVATE low-level attribute ``mcp._mcp_server.version`` (the value that reaches
    ``serverInfo.version`` on the wire, independently confirmed via an ``initialize`` handshake).
    This asserts that private mirror still exists and carries ``__version__``, so a FastMCP upgrade
    that stops mirroring the constructor version turns a silently-unset handshake version into a
    loud test failure. (The old code reached into ``_mcp_server`` directly to SET the version; that
    private-write path is gone, the constructor arg replaced it.)"""
    from epics_mcp import __version__
    from epics_mcp.server import mcp

    low_level = getattr(mcp, "_mcp_server", None)
    assert low_level is not None, (
        "mcp._mcp_server vanished (FastMCP upgrade?), version-set is a no-op"
    )
    assert low_level.version == __version__


def test_config_no_secrets() -> None:
    result = get_epics_config()
    assert "provider" in result

    secret_keywords = {"secret", "password", "key", "token"}
    for k in result:
        assert k.lower() not in secret_keywords, f"Config exposes secret-like key: {k}"
