"""Concurrency isolation for long-running background work, the K4 bulkhead.

``pv_monitor`` blocks a worker thread for up to ``max_monitor_duration`` (60 s) while its p4p
subscription runs. If those blocking calls shared the asyncio DEFAULT executor (``min(32, cpu+4)``
threads, the single pool behind every ``asyncio.to_thread``), enough concurrent monitors would
occupy all of it, and every other ``to_thread`` call: REST plane checks, PV reads/writes, the Olog
write path, would queue behind them. Nothing crashes; the server merely *appears* hung.

This module owns a DEDICATED :class:`~concurrent.futures.ThreadPoolExecutor` sized by
``monitor_max_concurrency``, so a burst of monitors is bounded to that width and can never touch the
shared default pool. It is the bulkhead pattern: isolate the one unbounded-duration operation behind
its own thread budget, and short operations keep flowing.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from epics_mcp.config import get_config

_monitor_executor: ThreadPoolExecutor | None = None
_monitor_executor_lock = threading.Lock()


def get_monitor_executor() -> ThreadPoolExecutor:
    """Return the singleton dedicated monitor executor, creating it on first call (thread-safe).

    Sized by ``monitor_max_concurrency`` at first use; the width is read once and fixed for the
    executor's lifetime (a config change takes effect only after :func:`reset_monitor_executor`,
    which is a test hook, the process never resizes it in normal operation). Threads are named
    ``epics-monitor_*`` so a stack dump makes the bulkhead visible. The lock mirrors
    ``get_config`` / ``get_context``: it prevents a double-initialisation race on concurrent access.
    """
    global _monitor_executor
    with _monitor_executor_lock:
        if _monitor_executor is None:
            _monitor_executor = ThreadPoolExecutor(
                max_workers=get_config().monitor_max_concurrency,
                thread_name_prefix="epics-monitor",
            )
    return _monitor_executor


def reset_monitor_executor() -> None:
    """Shut down and drop the singleton so the next :func:`get_monitor_executor` rebuilds it with
    the current config. Test-isolation hook (a test setting a different ``monitor_max_concurrency``
    must not inherit a pool sized by an earlier one). ``wait=False`` + ``cancel_futures=True``:
    pending work is dropped and running monitors are not awaited (tests drive them to completion
    first)."""
    # wait=False so the reset never blocks on a mid-flight 60 s monitor; cancel_futures drops only
    # QUEUED work. Tests drive their futures to completion before resetting, so no monitor is
    # orphaned in practice, a future test that asserts WHILE a monitor runs would want wait=True.
    global _monitor_executor
    with _monitor_executor_lock:
        if _monitor_executor is not None:
            _monitor_executor.shutdown(wait=False, cancel_futures=True)
            _monitor_executor = None
