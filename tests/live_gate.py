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


def assert_write_target_is_local(
    url: str | None,
    *,
    variable: str,
    ack: str | None = None,
    ack_variable: str | None = None,
) -> None:
    """The TARGET guard. Belongs BEFORE any client is built, in a module that WRITES.

    :func:`assert_live_available` asks whether a prerequisite is THERE. This one asks where it
    POINTS, and the two are different questions: a module whose write stack is fully configured
    and aimed at a production Olog passes the first one and must not run. Olog has no delete, so
    an entry a probe leaves there can be labelled but never removed.

    Four outcomes, and the first is why this guard is order-independent:

    - empty or ``None``   -> returns. That is the UNCONFIGURED case, which
      :func:`assert_live_available` already owns, and an empty URL reaches no server. Handling it
      here means this guard can never turn an unconfigured run red, whatever order the autouse
      fixtures of a module happen to run in.
    - a loopback target   -> returns.
    - a target the operator DECLARED local, by repeating it verbatim in *ack_variable* -> returns.
      See "The declared-local lane" below.
    - anything else       -> ``fail``, UNCONDITIONALLY, not through the live gate.

    The refusal deliberately does not go through ``assert_live_available``: a non-loopback target
    is not a MISSING prerequisite, it is a FORBIDDEN one, and a silent skip would hide exactly
    what this guard exists to make visible. Same reasoning and same shape as
    ``tests/test_write_gate_live.py::_refuse_to_pollute_a_real_audit_trail``.

    RESOLUTION-FREE, and the attribution matters because the contract is worded per FAMILY.
    ``docs/write-gate-contract.md`` states "a hostname is never trusted as loopback" in its bullet
    about a target FIXED AT CONSTRUCTION (a client search reach), and ``docs/safety.md`` repeats it
    for the EPICS write reach. The bullet that actually covers THIS case, a per-write HTTP URL,
    demands something else: take the host from the same parser the client will connect with. Both
    are satisfied here, and neither is satisfied by accident: the decision runs through
    :func:`~epics_mcp.services._http.is_loopback_url`, which is the very function the production
    gate applies to the same question (``epics_mcp.olog_safety``), and which parses with urllib3,
    the parser ``requests`` connects with. Resolving would give this guard a different notion of
    locality from the gate standing beside it, and would make the verdict depend on a resolver at
    assert time rather than on the configuration (the reason is spelled out in
    :mod:`epics_mcp.epics_address`).

    THE DECLARED-LOCAL LANE, and what it is worth. A rig can be local and still not look loopback:
    a TLS reverse proxy in front of a sandbox is reached under a hostname, and this repository pins
    that such a name is NOT loopback (``tests/test_http.py`` does it for a compose service name).
    Refusing it outright would forbid a legitimate rig; resolving it is out. So the operator may
    DECLARE the target by repeating the URL verbatim in a second variable whose name says so. What
    that buys is precise: an inherited or mistyped target cannot match a declaration nobody typed
    for it. What it does not buy: it cannot stop somebody who deliberately declares a production
    URL. It converts an unguarded target into a declared one, not into a proven one.

    TWO LIMITS THIS GUARD CANNOT LIFT, stated because a guard read as wider than it is misleads:

    * loopback is not the same thing as a sandbox. The host is checked, never the port, so a
      tunnel or a local reverse proxy on ``127.0.0.1`` that fronts a real facility passes.
    * this is STRICTER than the production boundary next to it, deliberately. That one also admits
      an exactly-allowlisted remote https URL; this one does not, because a test that lays down
      artifacts is not the place to exercise that lane.

    The URL is an ARGUMENT and is never read from ``os.environ`` here, so the caller decides which
    value is judged and a test can ask the question without mutating process state. ⚠️ The caller
    must hand over the value its BODY connects with, and that is a real trap rather than a
    formality: one module here used to re-read the variable at call time while its gate judged the
    import-time constant, so the guard would have been judging a value nothing used. It was
    changed to connect with the constant; all four now do.
    """
    if not url:
        return
    if is_loopback_url(url):
        return
    if ack_variable and ack and ack.strip() == url.strip():
        return
    host = url_host(url)
    if host is None:
        repair = (
            f"{variable} cannot be parsed as a URL at all (a base URL needs a scheme, so "
            f"'localhost:8080/Olog' is not one and 'http://localhost:8080/Olog' is)"
        )
    else:
        repair = f"{variable} points at {host!r}, which is not a loopback host"
    lane = (
        f" A rig that is local under a hostname is declared by repeating the URL verbatim in "
        f"{ack_variable}."
        if ack_variable
        else ""
    )
    pytest.fail(
        f"{repair}. This module WRITES to Olog, and Olog has no delete, so a write probe may only "
        "target a local sandbox. The check is resolution-free, so a hostname is never trusted as "
        f"loopback however it resolves.{lane}",
        pytrace=False,
    )
