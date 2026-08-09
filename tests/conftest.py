"""Shared fixtures for EPICS MCP tests."""

import collections
import os
import threading
import time
from collections.abc import Callable, Iterator
from typing import Protocol

import pytest

from epics_mcp.config import EpicsConfig
from epics_mcp.errors import RateLimitError
from epics_mcp.safety import SafetyLayer
from epics_mcp.services._concurrency import reset_monitor_executor
from epics_mcp.services._http import clear_shared_sessions, reset_read_throttle
from tests.engine_gate import (
    REQUIRE_DISPLAYS_ENV,
    displays_demanded,
    engine_available,
    engine_collection_decision,
)

# The display-aware tools and their opi_navigation-coupled tests need the optional
# `displays` dependency group. When opi_navigation is not installed (a standalone core install),
# skip those test modules at collection so the core suite still runs, mirroring
# server.py, which registers the display tools only when opi_navigation is importable.
#
# Through engine_gate rather than a bare find_spec (QA-48): this line runs at COLLECTION time, so
# a finder that RAISES took the entire run down with an ImportError on this conftest instead of
# skipping the modules below. Measured before the repair: exit 4, nothing collected.
ENGINE_COUPLED_MODULES = (
    "test_validate.py",
    "test_crossplane_tool.py",
    "test_coverage_tool.py",
    "test_find_device_tool.py",
    "test_device_lookup.py",
    "test_inventory_adapter.py",
    "test_diagnostics_tail.py",
)

_DECISION = engine_collection_decision(
    available=engine_available(), demanded=displays_demanded(os.environ)
)

if _DECISION == "ignore":
    collect_ignore = list(ENGINE_COUPLED_MODULES)


def pytest_configure(config: pytest.Config, decision: str = _DECISION) -> None:
    """Refuse a DEMANDED display run that cannot happen, instead of reporting it green (GB-27).

    Without this, ``EPICS_MCP_REQUIRE_DISPLAYS=1`` on an engine-less checkout produced exactly the
    report of a healthy run: the modules above were dropped at collection and nothing said so.
    A hundred tests, silently absent. ``UsageError`` rather than an assertion because that is what
    it is, a run asking for something this environment cannot deliver; it prints one line, without
    a traceback, and exits 4.

    ``decision`` defaults to this run's frozen ``_DECISION`` and exists so a test can ask about the
    OTHER branches without owning the environment that produces them. It costs nothing at run time:
    pluggy sorts a parameter WITH a default into ``kwargnames`` and validates only ``argnames``
    against the hook spec, so pytest calls this exactly as before (measured on pytest 9.1.0 /
    pluggy 1.6.0). See the counter-probe in ``tests/test_engine_gate.py`` for why it had to become
    injectable: it used to assert ``engine_available()`` first, which is ``assert False`` on the
    engine-less checkout, so the GB-27 commit turned CI red for six runs before anyone noticed.
    """
    del config  # the hook signature is pytest's, the decision needs nothing from it
    if decision == "fail":
        raise pytest.UsageError(
            f"{REQUIRE_DISPLAYS_ENV} demands the display-coupled tests, but the opi_navigation "
            f"engine is not importable, so these {len(ENGINE_COUPLED_MODULES)} modules would be "
            f"skipped silently: {', '.join(ENGINE_COUPLED_MODULES)}. "
            "Install it with: uv sync --extra dev --group displays"
        )


def pytest_report_header(decision: str = _DECISION) -> list[str] | None:
    """Print the gap into the header of every run that PRINTS a header (GB-27).

    This is the half that needs no switch and no credential, and it is the one that fixes the
    actual damage. CI syncs without ``--group displays`` on purpose, so that it tests exactly the
    standalone core a public user gets; that is a decision, not an oversight. What was wrong is
    that its green report said nothing about the hundred tests it did not run, so a reader had no
    way to tell a full run from a partial one. Now the run carries its own gap.

    ⚠️ "every run that prints a header" is the honest reach, and it used to say "EVERY run":
    ``-q`` and ``--no-header`` suppress the header block entirely, and this hook is called from
    inside it (measured on the installed pytest 9.1.0: ``_pytest/terminal.py``, ``showheader`` is
    ``verbosity >= 0``, and the ``pytest_report_header`` call sits behind both that and
    ``no_header``). No real invocation here loses the line (CI runs without ``-q``, and the one
    ``-q`` caller in this suite already passes ``quiet=False`` for exactly this reason), so the
    reach is stated rather than worked around.

    ``decision`` is injectable for the same reason as in :func:`pytest_configure`; see there.
    """
    if decision != "ignore":
        return None
    return [
        f"opi_navigation engine absent: {len(ENGINE_COUPLED_MODULES)} display-coupled test "
        f"modules NOT collected ({', '.join(ENGINE_COUPLED_MODULES)}); "
        f"set {REQUIRE_DISPLAYS_ENV}=1 to make that a refusal instead of a silent skip"
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


@pytest.fixture(autouse=True)
def _clear_shared_session_cache() -> Iterator[None]:
    """K5: the HTTP read-session factory memoises per config in a PROCESS-global lru_cache. Clear it
    around every test so a session built under one test's monkeypatched ``get_config`` never leaks
    into the next, deterministic sessions regardless of test order."""
    clear_shared_sessions()
    yield
    clear_shared_sessions()


@pytest.fixture(autouse=True)
def _reset_monitor_executor() -> Iterator[None]:
    """K4: the dedicated monitor executor is a process-global singleton sized at first use from
    config. Reset it around every test so a test setting ``monitor_max_concurrency`` never
    inherits a pool sized by an earlier one; the next test rebuilds it under its own config."""
    reset_monitor_executor()
    yield
    reset_monitor_executor()


@pytest.fixture(autouse=True)
def _reset_read_throttle() -> Iterator[None]:
    """S3: the read throttle is a process-global singleton built at first use from config. Reset it
    around every test so a test setting ``read_rate_limit`` never inherits a bucket from an earlier
    one; the next test rebuilds it under its own config (default: disabled)."""
    reset_read_throttle()
    yield
    reset_read_throttle()


# The loopback lane a write-enabled SafetyLayer accepts (E8): both providers' search reach
# pinned to loopback, subnet broadcast parser-faithfully OFF. Tests that construct a
# writes-on SafetyLayer against the process env opt in via
# ``pytestmark = pytest.mark.usefixtures("loopback_write_env")`` (the reach assert would
# otherwise fire on the stripped env, unset *_AUTO_ADDR_LIST means broadcast ON).
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
    """Config with writes enabled. An explicit permissive pattern ('.*', allow-all for any valid
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
    """SafetyLayer with writes enabled (needs the loopback lane, E8 reach assert)."""
    return SafetyLayer(write_config)


@pytest.fixture
def safety_locked(config: EpicsConfig) -> SafetyLayer:
    """SafetyLayer with writes disabled (default)."""
    return SafetyLayer(config)


# --- S28: deterministic rate-limiter atomicity harness (OlogWriteGate + SafetyLayer) ----------


class _RateLimitOwner(Protocol):
    """Anything with a sliding-window timestamp deque, both write gates satisfy this."""

    _timestamps: collections.deque[float]


class _RendezvousDeque(collections.deque[float]):
    """A deque whose FIRST ``append`` opens the len-check -> append window that exposes a
    non-atomic rate limiter: it signals ``checked`` (so a second thread may start) and then sleeps a
    BOUNDED time before recording. Bounded sleep (not a Barrier) never deadlocks the locked code:
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
    the window open without a Barrier, so a properly locked limiter admits exactly 1 (no deadlock)
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
