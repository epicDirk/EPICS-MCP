"""The live-test skip gate: silent by default, LOUD on demand (S30).

Why this exists: every live module in this repo gated itself with a ``pytest.mark.skipif``
on its plane's env vars. CI calls bare ``uv run pytest`` with those vars unset, so the
live tests skip silently, and ``uv run pytest -m live`` in an empty environment reported
``51 skipped, exit 0`` when S30 was written: a DEMANDED live run was indistinguishable from a
fulfilled one. A silently skipped gate looks like a passed gate in every report.
The count is deliberately left at its historical value and dated rather than tracked: it grows
with every live module (75 as of 2026-08-27) and nothing watches it, so a number stated in the
present tense here would rot. What the sentence needs is the SHAPE of the report, not its size.

The mechanism is deliberately NOT a CI gate (CI has no live stack, and no CI guard can
prove a probe RAN, see CLAUDE.md, "Server-decided parameters"). Instead: whoever
explicitly DEMANDS a live run gets red instead of a skip when a prerequisite is missing.

Demanded via the env var ``EPICS_MCP_REQUIRE_LIVE=1``. The demand applies to the SELECTED
test set: scope a partial live run with ``-m live -k <plane>`` (a deselected test never
reaches its gate); every selected probe whose prerequisite is missing goes red. Data-
dependent skips INSIDE a running live probe (e.g. "fixture carries only one level") are a
separate class and stay skips.

The ``live`` marker (declared in ``pyproject.toml``) stays orthogonal: it SELECTS
(``-m live``), it does not demand.

The SKIP gate is ONE function, :func:`assert_live_available`, called from an autouse
fixture (module prerequisites) or as the first statement of a fixture/test (extra
prerequisites). It replaces the former ``pytest.mark.skipif`` decorators deliberately:
those were evaluated ONCE at collection time, while the prerequisite can change until
setup time.

A SECOND guard sits beside it and answers a different question. ``assert_live_available``
asks whether a prerequisite is THERE; :func:`assert_write_target_is_local` asks where it
POINTS, and refuses UNCONDITIONALLY when a module that writes into a service with no delete
is aimed at anything but a local sandbox. The two are not variants of each other: a missing
prerequisite is a skip, a forbidden target never is.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from epics_mcp.services._http import is_loopback_url, url_host

REQUIRE_LIVE_ENV = "EPICS_MCP_REQUIRE_LIVE"

# Read generously so a "true"/"yes" does not silently count as "not demanded", a
# false-negative gate would be exactly the defect this module removes.
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def live_demanded(env: Mapping[str, str]) -> bool:
    """Was a live run explicitly demanded?

    The environment is INJECTED instead of read globally, the decision stays
    deterministic and offline-testable (the only half of this gate CI can check at all).
    """
    return env.get(REQUIRE_LIVE_ENV, "").strip().lower() in _TRUTHY


def assert_live_available(available: bool, reason: str, *, demanded: bool) -> None:
    """The gate. Belongs BEFORE any connection attempt (fixture start / first test line).

    - prerequisite present   -> returns, the test runs
    - missing, not demanded  -> ``skip`` (default/CI: silent, green, as before)
    - missing but DEMANDED   -> ``fail`` with the reason in plain text

    ``pytrace=False`` because a stack trace explains nothing here: the message is "the
    prerequisite is missing", not "this code is broken".
    """
    if available:
        return
    if demanded:
        pytest.fail(f"live run demanded ({REQUIRE_LIVE_ENV}) but {reason}", pytrace=False)
    pytest.skip(reason)


def assert_write_target_is_local(url: str | None, *, variable: str) -> None:
    """The TARGET guard. Belongs BEFORE any client is built, in a module that WRITES.

    :func:`assert_live_available` asks whether a prerequisite is THERE. This one asks where it
    POINTS, and the two are different questions: a module whose write stack is fully configured
    and aimed at a production Olog passes the first one and must not run. Olog has no delete, so
    an entry a probe leaves there can be labelled but never removed.

    Three outcomes, and the first one is why this guard is order-independent:

    - empty or ``None``  -> returns. That is the UNCONFIGURED case, which
      :func:`assert_live_available` already owns, and an empty URL reaches no server. Handling it
      here means this guard can never turn an unconfigured run red, whatever order the autouse
      fixtures of a module happen to run in.
    - a loopback target  -> returns.
    - anything else      -> ``fail``, UNCONDITIONALLY, not through the live gate.

    The refusal deliberately does not go through ``assert_live_available``: a non-loopback target
    is not a MISSING prerequisite, it is a FORBIDDEN one, and a silent skip would hide exactly
    what this guard exists to make visible. Same reasoning and same shape as
    ``tests/test_write_gate_live.py::_refuse_to_pollute_a_real_audit_trail``.

    RESOLUTION-FREE, which is a repository contract rather than a shortcut.
    ``docs/write-gate-contract.md`` and ``docs/safety.md`` both require of this family of target
    boundaries that "a hostname is never trusted as loopback", because resolving would make the
    verdict depend on the resolver's answer at assert time instead of on the configuration
    (:mod:`epics_mcp.epics_address` carries the reason). So the decision runs through
    :func:`~epics_mcp.services._http.is_loopback_url`, the same hardened primitive the production
    gate uses, and a hostname that happens to point at this machine is refused like any other.
    The consequence is named where it bites: ``tests/test_olog_remote_https_live.py`` aims at a
    hostname on purpose, so its proxy URL is not what this guard is handed.

    The URL is an ARGUMENT and is never read from ``os.environ`` here. The write modules read
    their targets once at import so that gate and body share one snapshot; a guard that looked
    the value up again would judge a different state from the one the test uses.
    """
    if not url:
        return
    if is_loopback_url(url):
        return
    host = url_host(url)
    named = repr(host) if host is not None else "a host this parser cannot read"
    pytest.fail(
        f"{variable} points at {named}, which is not a loopback host. This module WRITES to Olog, "
        "and Olog has no delete, so a write probe may only target a local sandbox. The check is "
        "resolution-free (docs/write-gate-contract.md), so a hostname is never trusted as "
        "loopback: point the variable at a loopback literal or at localhost.",
        pytrace=False,
    )
