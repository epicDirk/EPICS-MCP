"""Shared fixtures for EPICS PV MCP tests."""

import collections
import importlib.util
import threading
import time
from collections.abc import Callable
from typing import Protocol

import pytest

from epics_pv_mcp.config import EpicsConfig
from epics_pv_mcp.errors import RateLimitError
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


# --- EPICS search-path env isolation (BG14) --------------------------------------------------
# The live-plane posture (services/doctor._check_live) reads the process env directly. Without
# isolation, posture tests would measure the MACHINE (a developer's EPICS_PVA_ADDR_LIST or
# EPICS_PVA_NAME_SERVERS leaks into the assertion) instead of the code. No test in this suite
# legitimately consumes a pre-set search var (measured: zero hits across tests/); a test that
# needs one sets it explicitly via monkeypatch. The live REST modules key on EPICS_MCP_* URLs,
# which are untouched here.

_EPICS_SEARCH_ENV_VARS = (
    "EPICS_PVA_ADDR_LIST",
    "EPICS_CA_ADDR_LIST",
    "EPICS_PVA_NAME_SERVERS",
    "EPICS_CA_NAME_SERVERS",
    "EPICS_PVA_AUTO_ADDR_LIST",
    "EPICS_CA_AUTO_ADDR_LIST",
)


@pytest.fixture(autouse=True)
def _isolate_epics_search_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip the EPICS search-path vars so posture assertions measure the code, not the machine."""
    for var in _EPICS_SEARCH_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


# The loopback lane a write-enabled SafetyLayer accepts (E8): both providers' search reach
# pinned to loopback, subnet broadcast parser-faithfully OFF. Tests that construct a
# writes-on SafetyLayer against the process env opt in via
# ``pytestmark = pytest.mark.usefixtures("loopback_write_env")`` (the reach assert would
# otherwise fire on the stripped env — unset *_AUTO_ADDR_LIST means broadcast ON).
_LOOPBACK_WRITE_LANE = {
    "EPICS_PVA_ADDR_LIST": "127.0.0.1",
    "EPICS_PVA_NAME_SERVERS": "127.0.0.1:5075",
    "EPICS_PVA_AUTO_ADDR_LIST": "NO",
    "EPICS_CA_ADDR_LIST": "127.0.0.1",
    "EPICS_CA_AUTO_ADDR_LIST": "NO",
}


@pytest.fixture
def loopback_write_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the EPICS search env to the loopback write lane (runs after the autouse strip)."""
    for var, value in _LOOPBACK_WRITE_LANE.items():
        monkeypatch.setenv(var, value)


@pytest.fixture
def config() -> EpicsConfig:
    """Default test config."""
    return EpicsConfig()


@pytest.fixture
def write_config() -> EpicsConfig:
    """Config with writes enabled. An explicit permissive pattern ('.*' — allow-all for any valid
    PV name) stands in for the former implicit empty default; writes-on now REQUIRES a non-empty
    pattern (S22). Note: '.*' is marginally stricter than the old empty default (fullmatch does not
    cross newlines), but PV names never contain newlines, so the tests are unaffected."""
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
def safety(write_config: EpicsConfig, loopback_write_env: None) -> SafetyLayer:
    """SafetyLayer with writes enabled (needs the loopback lane — E8 reach assert)."""
    return SafetyLayer(write_config)


@pytest.fixture
def safety_locked(config: EpicsConfig) -> SafetyLayer:
    """SafetyLayer with writes disabled (default)."""
    return SafetyLayer(config)


# --- S28: deterministic rate-limiter atomicity harness (OlogWriteGate + SafetyLayer) ----------


class _RateLimitOwner(Protocol):
    """Anything with a sliding-window timestamp deque — both write gates satisfy this."""

    _timestamps: collections.deque[float]


class _RendezvousDeque(collections.deque[float]):
    """A deque whose FIRST ``append`` opens the len-check -> append window that exposes a
    non-atomic rate limiter: it signals ``checked`` (so a second thread may start) and then sleeps a
    BOUNDED time before recording. Bounded sleep (not a Barrier) never deadlocks the locked code —
    the lock holder just sleeps briefly and releases. See S28."""

    def __init__(self, maxlen: int | None) -> None:
        super().__init__(maxlen=maxlen)
        self.checked = threading.Event()

    def append(self, item: float, /) -> None:
        if not self.checked.is_set():
            self.checked.set()  # thread 1 passed its len-check; hold the window open for thread 2
            time.sleep(0.15)
        super().append(item)


@pytest.fixture
def concurrent_admit_count() -> Callable[[_RateLimitOwner, Callable[[], None]], int]:
    """Return a driver that runs ``call`` from TWO threads through ``owner``'s rate check->append
    seam and returns the number of admitted (non-``RateLimitError``) calls. Deterministic: thread 2
    starts only after thread 1 has passed its len-check (``checked``), and the bounded sleep keeps
    the window open without a Barrier — so a properly locked limiter admits exactly 1 (no deadlock)
    while a non-atomic one admits 2. Swap in a limit=1 config for the sharpest signal."""

    def _run(owner: _RateLimitOwner, call: Callable[[], None]) -> int:
        hooked = _RendezvousDeque(maxlen=owner._timestamps.maxlen)
        owner._timestamps = hooked
        admits = 0
        admit_lock = threading.Lock()

        def worker(second: bool) -> None:
            nonlocal admits
            if second:
                hooked.checked.wait(timeout=5.0)
            try:
                call()
            except RateLimitError:
                return
            with admit_lock:
                admits += 1

        threads = [threading.Thread(target=worker, args=(s,)) for s in (False, True)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return admits

    return _run
