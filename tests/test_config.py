"""Tests for EpicsConfig and get_config singleton."""

import warnings

import pytest
from pydantic import ValidationError

import epics_pv_mcp.config as config_module
from epics_pv_mcp.config import EpicsConfig, get_config


class TestEpicsConfigDefaults:
    """Verify default configuration values."""

    def test_allow_pv_write_default_false(self) -> None:
        cfg = EpicsConfig()
        assert cfg.allow_pv_write is False

    def test_provider_default_pva(self) -> None:
        cfg = EpicsConfig()
        assert cfg.provider == "pva"

    def test_default_timeout(self) -> None:
        cfg = EpicsConfig()
        assert cfg.default_timeout == 5.0

    def test_max_batch_size_default(self) -> None:
        cfg = EpicsConfig()
        assert cfg.max_batch_size == 100

    def test_write_rate_limit_default(self) -> None:
        cfg = EpicsConfig()
        assert cfg.write_rate_limit == 10

    def test_pv_write_pattern_default_empty(self) -> None:
        cfg = EpicsConfig()
        assert cfg.pv_write_pattern == ""

    def test_audit_log_file_default_empty(self) -> None:
        cfg = EpicsConfig()
        assert cfg.audit_log_file == ""

    def test_max_monitor_duration_default(self) -> None:
        cfg = EpicsConfig()
        assert cfg.max_monitor_duration == 60.0

    def test_max_monitor_events_default(self) -> None:
        cfg = EpicsConfig()
        assert cfg.max_monitor_events == 1000

    def test_ca_bundle_default_empty(self) -> None:
        cfg = EpicsConfig()
        assert cfg.ca_bundle == ""

    def test_tls_verify_default_true(self) -> None:
        cfg = EpicsConfig()
        assert cfg.tls_verify is True


class TestEpicsConfigEnvOverride:
    """Verify environment variable overrides via monkeypatch."""

    def test_allow_pv_write_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EPICS_MCP_ALLOW_PV_WRITE", "true")
        cfg = EpicsConfig()
        assert cfg.allow_pv_write is True

    def test_provider_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EPICS_MCP_PROVIDER", "ca")
        cfg = EpicsConfig()
        assert cfg.provider == "ca"

    def test_default_timeout_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EPICS_MCP_DEFAULT_TIMEOUT", "10.0")
        cfg = EpicsConfig()
        assert cfg.default_timeout == 10.0

    def test_write_rate_limit_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EPICS_MCP_WRITE_RATE_LIMIT", "20")
        cfg = EpicsConfig()
        assert cfg.write_rate_limit == 20

    def test_ca_bundle_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EPICS_MCP_CA_BUNDLE", "/etc/ssl/ess-root-ca.pem")
        cfg = EpicsConfig()
        assert cfg.ca_bundle == "/etc/ssl/ess-root-ca.pem"

    def test_tls_verify_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EPICS_MCP_TLS_VERIFY", "false")
        cfg = EpicsConfig()
        assert cfg.tls_verify is False


class TestEpicsConfigValidation:
    """G2: nonsensical / out-of-range values are rejected, not silently accepted."""

    def test_invalid_provider_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EPICS_MCP_PROVIDER", "nonsense")
        with pytest.raises(ValidationError):
            EpicsConfig()

    def test_uppercase_provider_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # provider is a lowercase Literal, "CA" must fail, not silently mis-provider.
        monkeypatch.setenv("EPICS_MCP_PROVIDER", "CA")
        with pytest.raises(ValidationError):
            EpicsConfig()

    def test_negative_write_rate_limit_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EPICS_MCP_WRITE_RATE_LIMIT", "-1")
        with pytest.raises(ValidationError):
            EpicsConfig()

    def test_zero_max_batch_size_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EPICS_MCP_MAX_BATCH_SIZE", "0")
        with pytest.raises(ValidationError):
            EpicsConfig()

    def test_nonpositive_default_timeout_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EPICS_MCP_DEFAULT_TIMEOUT", "0")
        with pytest.raises(ValidationError):
            EpicsConfig()

    def test_zero_max_monitor_events_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EPICS_MCP_MAX_MONITOR_EVENTS", "0")
        with pytest.raises(ValidationError):
            EpicsConfig()

    def test_nonpositive_max_monitor_duration_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EPICS_MCP_MAX_MONITOR_DURATION", "0")
        with pytest.raises(ValidationError):
            EpicsConfig()

    def test_whitespace_padded_provider_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # pydantic does not strip a Literal, " ca" must fail, not silently pass.
        monkeypatch.setenv("EPICS_MCP_PROVIDER", " ca")
        with pytest.raises(ValidationError):
            EpicsConfig()

    def test_valid_lowercase_provider_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EPICS_MCP_PROVIDER", "ca")
        assert EpicsConfig().provider == "ca"


class TestUnknownEnvVarGuard:
    """R2: pydantic-settings drops an unknown ``EPICS_MCP_*`` var silently (extra=ignore default),
    so a typo like ``EPICS_MCP_CHANNELFINDR_URL`` leaves the real setting at its default with no
    hint. The guard warns on the likely typo; a correct var and a reserved test-harness var stay
    quiet."""

    def test_unknown_var_warns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EPICS_MCP_CHANNELFINDR_URL", "x")  # typo of CHANNELFINDER_URL
        with pytest.warns(UserWarning, match="EPICS_MCP_CHANNELFINDR_URL"):
            EpicsConfig()

    def test_known_var_does_not_warn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EPICS_MCP_CHANNELFINDER_URL", "http://cf:8080/ChannelFinder")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            cfg = EpicsConfig()
        assert cfg.channelfinder_url == "http://cf:8080/ChannelFinder"
        assert not [w for w in caught if "EPICS_MCP_CHANNELFINDER_URL" in str(w.message)]

    def test_reserved_live_test_var_does_not_warn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # EPICS_MCP_LIVE_* / _OLOG_TEST_* / _REQUIRE_LIVE are read by the TEST harness (live_gate,
        # the *_live.py modules), never by the server, they are intentionally not config fields and
        # must not trip the guard during an opt-in live run.
        monkeypatch.setenv("EPICS_MCP_LIVE_ALARM_PV", "SIM:X")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            EpicsConfig()
        assert not [w for w in caught if "EPICS_MCP_LIVE_ALARM_PV" in str(w.message)]


class TestGetConfigSingleton:
    """Verify get_config returns a singleton."""

    def test_returns_same_instance(self) -> None:
        # Reset the module-level singleton
        config_module._config = None
        try:
            first = get_config()
            second = get_config()
            assert first is second
        finally:
            # Clean up so other tests aren't affected
            config_module._config = None

    def test_returns_epics_config_instance(self) -> None:
        config_module._config = None
        try:
            cfg = get_config()
            assert isinstance(cfg, EpicsConfig)
        finally:
            config_module._config = None
