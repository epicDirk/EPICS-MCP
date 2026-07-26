"""Tests for the K4 monitor bulkhead (services/_concurrency).

The dedicated executor isolates ``pv_monitor``'s up-to-60 s blocking subscriptions from the shared
asyncio default pool. That ``pv_monitor`` actually dispatches onto this executor is proven in
``test_epics_client.py::test_pv_monitor_runs_on_dedicated_executor`` (worker thread name); here we
cover the executor itself: singleton, config-driven width, the concurrency bound, and reset.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from epics_pv_mcp.config import EpicsConfig
from epics_pv_mcp.services import _concurrency
from epics_pv_mcp.services._concurrency import get_monitor_executor, reset_monitor_executor


def test_monitor_executor_is_a_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    """One executor for the process, a fresh pool per monitor would defeat the bound entirely."""
    monkeypatch.setattr(_concurrency, "get_config", lambda: EpicsConfig())
    assert get_monitor_executor() is get_monitor_executor()


def test_monitor_executor_width_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pool width is ``monitor_max_concurrency``, the knob that bounds concurrent monitors."""
    monkeypatch.setattr(_concurrency, "get_config", lambda: EpicsConfig(monitor_max_concurrency=3))
    executor = get_monitor_executor()
    assert isinstance(executor, ThreadPoolExecutor)
    assert executor._max_workers == 3


def test_monitor_executor_caps_concurrency(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bulkhead's whole point: at most ``monitor_max_concurrency`` tasks run at once. With
    width 2, a third submission cannot start until one of the first two frees a thread, so a
    ``Barrier(3)`` that only trips if all three run TOGETHER times out instead
    (``BrokenBarrierError``). An unbounded pool would trip it cleanly. Bounded rendezvous via the
    barrier timeout, no wall-clock assertion."""
    monkeypatch.setattr(_concurrency, "get_config", lambda: EpicsConfig(monitor_max_concurrency=2))
    executor = get_monitor_executor()

    barrier = threading.Barrier(3, timeout=0.5)

    def _meet() -> str:
        barrier.wait()  # BrokenBarrierError if fewer than 3 arrive within the timeout
        return "met"

    futures = [executor.submit(_meet) for _ in range(3)]
    exceptions = [f.exception() for f in futures]
    # width 2 → the barrier never sees 3 parties → every task raises BrokenBarrierError. Were the
    # cap not applied (>=3 threads), all three would meet and return "met" with no exception.
    assert all(isinstance(exc, threading.BrokenBarrierError) for exc in exceptions)


def test_reset_rebuilds_with_new_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """``reset_monitor_executor`` drops the singleton so the next ``get_*`` rebuilds under the
    current config, the mechanism that keeps a test (or a reload) from inheriting a stale width."""
    monkeypatch.setattr(_concurrency, "get_config", lambda: EpicsConfig(monitor_max_concurrency=2))
    first = get_monitor_executor()
    reset_monitor_executor()
    monkeypatch.setattr(_concurrency, "get_config", lambda: EpicsConfig(monitor_max_concurrency=5))
    second = get_monitor_executor()
    assert first is not second
    assert second._max_workers == 5
