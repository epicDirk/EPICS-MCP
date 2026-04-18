"""Tests for epics_pv_mcp.resources."""

from epics_pv_mcp.resources import get_epics_config, get_health


def test_health_shape():
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


def test_health_values():
    result = get_health()
    assert result["server"] == "epics-pv-mcp"
    assert result["status"] == "ok"
    assert result["write_enabled"] is False


def test_config_no_secrets():
    result = get_epics_config()
    assert "provider" in result

    secret_keywords = {"secret", "password", "key", "token"}
    for k in result:
        assert k.lower() not in secret_keywords, f"Config exposes secret-like key: {k}"
