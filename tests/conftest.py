"""Shared fixtures for EPICS PV MCP tests."""

import pytest

from epics_pv_mcp.config import EpicsConfig
from epics_pv_mcp.safety import SafetyLayer


@pytest.fixture
def config() -> EpicsConfig:
    """Default test config."""
    return EpicsConfig()


@pytest.fixture
def write_config() -> EpicsConfig:
    """Config with writes enabled."""
    return EpicsConfig(allow_pv_write=True, write_rate_limit=5)


@pytest.fixture
def pattern_config() -> EpicsConfig:
    """Config with writes enabled and pattern allowlist."""
    return EpicsConfig(
        allow_pv_write=True,
        pv_write_pattern=r"^TEST:.*$",
        write_rate_limit=10,
    )


@pytest.fixture
def safety(write_config: EpicsConfig) -> SafetyLayer:
    """SafetyLayer with writes enabled."""
    return SafetyLayer(write_config)


@pytest.fixture
def safety_locked(config: EpicsConfig) -> SafetyLayer:
    """SafetyLayer with writes disabled (default)."""
    return SafetyLayer(config)
