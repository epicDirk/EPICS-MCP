"""Shared fixtures for EPICS PV MCP tests."""

import importlib.util

import pytest

from epics_pv_mcp.config import EpicsConfig
from epics_pv_mcp.safety import SafetyLayer

# The display-aware tools and their opi_navigation-coupled tests need the optional
# `[displays]` extra. When opi_navigation is not installed (a standalone core install),
# skip those test modules at collection so the core suite still runs — mirroring
# server.py, which registers the display tools only when opi_navigation is importable.
if importlib.util.find_spec("opi_navigation") is None:
    collect_ignore = [
        "test_validate.py",
        "test_crossplane_tool.py",
        "test_coverage_tool.py",
        "test_find_device_tool.py",
        "test_device_lookup.py",
        "test_inventory_adapter.py",
    ]


@pytest.fixture
def config() -> EpicsConfig:
    """Default test config."""
    return EpicsConfig()


@pytest.fixture
def write_config() -> EpicsConfig:
    """Config with writes enabled. An explicit allow-all pattern ('.*') stands in for the former
    implicit empty default — writes-on now REQUIRES a non-empty pattern (S22)."""
    return EpicsConfig(allow_pv_write=True, pv_write_pattern=r".*", write_rate_limit=5)


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
