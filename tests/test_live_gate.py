"""Offline tests of the live gate (tests/live_gate.py).

The point of this file: CI can NEVER run the live half (no live stack), but it CAN pin
the DECISION about it. Exactly that half is pinned here, deterministic and offline.

Counter-probe discipline: every positive case has its negative. "Skips when the
prerequisite is missing" alone would also stay green if the gate ALWAYS skipped, so it
is equally measured that it does NOT hold anyone up when the prerequisite is present and
does NOT stay silent when demanded.
"""

from __future__ import annotations

import ast
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.live_gate import (
    REQUIRE_LIVE_ENV,
    assert_live_available,
    assert_write_target_is_local,
    live_demanded,
)

REASON = "the live stack is not configured"


def _outcome_of(gate_call: Callable[[], None]) -> BaseException | None:
    """CAPTURE the gate's outcome instead of letting it propagate.

    Critical, and learned the expensive way (sister repo, decision IW): a propagated
    ``Skipped`` turns THIS test into a skip, the watchdog vanishes exactly as silently
    as the defect it guards. Measured there: without this capture, the mutant "demanded
    branch removed" stayed green (2 skipped instead of 2 failed), so the watchdog proved
    nothing.

    ``None`` = the gate let the test through.
    """
    try:
        gate_call()
    except BaseException as exc:  # noqa: BLE001 (Skipped/Failed inherit from BaseException)
        return exc
    return None


# --- live_demanded: what counts as "demanded"? -------------------------------


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "True", "yes", "on", "  1  "])
def test_live_demanded_accepts_truthy_spellings(value: str) -> None:
    assert live_demanded({REQUIRE_LIVE_ENV: value}) is True


@pytest.mark.parametrize("value", ["", "   ", "0", "false", "no", "off", "maybe"])
def test_live_demanded_rejects_everything_else(value: str) -> None:
    assert live_demanded({REQUIRE_LIVE_ENV: value}) is False


def test_live_demanded_is_false_when_var_absent() -> None:
    """The default run and CI do not set the variable, that MUST stay silent."""
    assert live_demanded({}) is False


def test_live_demanded_ignores_other_variables() -> None:
    assert live_demanded({"EPICS_MCP_REQUIRE_LIVE_XYZ": "1", "REQUIRE_LIVE": "1"}) is False


# --- assert_live_available: the three branches -------------------------------


@pytest.mark.parametrize("demanded", [False, True])
def test_available_lets_the_test_run(demanded: bool) -> None:
    """Positive control: with the prerequisite present, the gate holds nobody up."""
    outcome = _outcome_of(lambda: assert_live_available(True, REASON, demanded=demanded))
    assert outcome is None, f"the gate held despite a met prerequisite: {outcome!r}"


def test_missing_and_not_demanded_skips() -> None:
    """The default path: silently skipped, so CI stays green."""
    outcome = _outcome_of(lambda: assert_live_available(False, REASON, demanded=False))
    assert isinstance(outcome, pytest.skip.Exception), f"expected Skipped, got {outcome!r}"
    assert REASON in str(outcome)


def test_missing_but_demanded_fails_loudly() -> None:
    """The core of S30: a DEMANDED live run no longer skips, it goes red."""
    outcome = _outcome_of(lambda: assert_live_available(False, REASON, demanded=True))
    assert isinstance(outcome, pytest.fail.Exception), f"expected Failed, got {outcome!r}"
    # No skip: that is the entire difference the order stands on.
    assert not isinstance(outcome, pytest.skip.Exception)
    # The message must name BOTH: that it was demanded and what was missing, otherwise
    # the reader hunts the cause in the test code instead of their environment.
    message = str(outcome)
    assert REQUIRE_LIVE_ENV in message
    assert REASON in message


def test_demand_changes_the_outcome_class() -> None:
    """Demanding must CHANGE the outcome class, not just the text.

    Without this comparison both branches could raise the same class and the tests
    above would be tautologically green.
    """
    silent = _outcome_of(lambda: assert_live_available(False, REASON, demanded=False))
    loud = _outcome_of(lambda: assert_live_available(False, REASON, demanded=True))
    assert type(silent) is not type(loud)


# --- assert_write_target_is_local: where a write probe may point --------------


#: A refused target that can reach nothing even if the guard were broken: RFC 5737 TEST-NET-1 is
#: reserved for documentation and is not routed. Preferred over a `.invalid` name because it needs
#: no resolution at all, so the message reads the same on a network that hijacks NXDOMAIN.
FORBIDDEN_URL = "https://192.0.2.10:8443/Olog"


@pytest.mark.parametrize(
    "url",
    ["http://localhost:8080/Olog", "http://127.0.0.1:8080/Olog", "https://[::1]/Olog"],
)
def test_a_loopback_target_is_let_through(url: str) -> None:
    """Positive control. Without it, a guard that refused everything would look identical."""
    outcome = _outcome_of(lambda: assert_write_target_is_local(url, variable="X"))
    assert outcome is None, f"a local sandbox was refused: {outcome!r}"


@pytest.mark.parametrize("url", [None, ""])
def test_an_unset_target_is_left_to_the_skip_gate(url: str | None) -> None:
    """Unconfigured is the SKIP gate's case, and an empty URL reaches no server.

    This is what makes the guard order-independent inside a module: it can never turn an
    unconfigured run red, whichever autouse fixture happens to run first.
    """
    outcome = _outcome_of(lambda: assert_write_target_is_local(url, variable="X"))
    assert outcome is None, f"an unset target was treated as forbidden: {outcome!r}"


@pytest.mark.parametrize(
    "url",
    [
        FORBIDDEN_URL,
        "https://olog.invalid/Olog",  # a name that cannot resolve is still not a loopback LITERAL
        "https://olog.example.org/Olog",
        "http://10.0.0.5/Olog",  # RFC1918 private is where a production Olog lives
        # A spoofed authority: urllib3, the parser requests connects with, reads the host as
        # evil.example.org. Refusing is the fail-closed direction, and it is the one a write needs.
        "http://127.0.0.1@evil.example.org/Olog",
        "not-a-url",
    ],
)
def test_a_non_local_target_is_refused(url: str) -> None:
    outcome = _outcome_of(lambda: assert_write_target_is_local(url, variable="X"))
    assert isinstance(outcome, pytest.fail.Exception), f"expected Failed, got {outcome!r}"


def test_the_refusal_is_never_a_skip() -> None:
    """The whole point: a forbidden target must not be silently skipped.

    A skip would hide the near miss it exists to make visible, and it would do so on exactly the
    run where somebody had write credentials in their shell. Compared by CLASS, not by text, so a
    reworded message cannot make this tautological.
    """
    outcome = _outcome_of(lambda: assert_write_target_is_local(FORBIDDEN_URL, variable="X"))
    assert not isinstance(outcome, pytest.skip.Exception), f"the guard skipped: {outcome!r}"


def test_the_refusal_names_the_variable_the_host_and_the_reason() -> None:
    """The message must let the reader repair their environment without reading this file."""
    outcome = _outcome_of(
        lambda: assert_write_target_is_local(FORBIDDEN_URL, variable="EPICS_MCP_OLOG_URL")
    )
    message = str(outcome)
    assert "EPICS_MCP_OLOG_URL" in message, message
    assert "192.0.2.10" in message, message
    assert "no delete" in message, message
    assert "resolution-free" in message, message


@pytest.mark.parametrize(
    "url",
    [
        "https://olog.example.org/Olog",
        # This one really does resolve to 127.0.0.1 for everyone, and must STILL be refused:
        # being local in fact is not the same as being local in the configuration.
        "https://olog.localtest.me:8443/Olog",
    ],
)
def test_the_guard_resolves_nothing(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """RESOLUTION-FREE is a contract, so it is asserted by removing the ability to resolve.

    ``docs/write-gate-contract.md`` and ``docs/safety.md`` both require of this family of target
    boundaries that a hostname is never trusted as loopback. Same guard shape as
    ``tests/test_provenance.py::test_nothing_here_opens_a_socket``, and faked at the ``socket``
    module rather than at a client class for the reason stated there: a class-level double would
    remove the very call it is meant to forbid.

    Provably red: make the guard call ``socket.getaddrinfo`` before classifying.
    """

    def _refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the write-target guard resolved a name")

    monkeypatch.setattr(socket, "socket", _refuse)
    monkeypatch.setattr(socket, "getaddrinfo", _refuse)
    monkeypatch.setattr(socket, "gethostbyname", _refuse)

    outcome = _outcome_of(lambda: assert_write_target_is_local(url, variable="X"))
    assert isinstance(outcome, pytest.fail.Exception), f"{url}: {outcome!r}"


# --- the population: every live module that WRITES must carry both guards -----


_TESTS_DIR = Path(__file__).parent

#: The Olog operations that MUTATE, named at the two levels a test can actually reach them: the
#: service layer (``epics_mcp.services.checkers_olog``) and the client
#: (``epics_mcp.services.olog_client``).
#:
#: Deliberately NOT the four MCP tool names. Measured 2026-09-07: the client method is
#: ``add_attachment``, not ``add_log_attachment``, and ``reply_to_log`` exists only as a tool, so a
#: list of tool names matches this tree through docstrings and test names instead of through calls.
_WRITE_CALLS = frozenset(
    {
        "query_olog_create",
        "query_olog_add_attachment",
        "query_olog_update",
        "create_log_entry",
        "add_attachment",
        "update_log_entry",
    }
)


def _called_names(path: Path) -> set[str]:
    """Every function or method name CALLED in *path*, read from the AST.

    The AST rather than a grep, because a grep over this tree matches PROSE: of the six names
    above, one occurs in a live module ONLY inside docstrings and test names, and never as a call.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                names.add(func.attr)
            elif isinstance(func, ast.Name):
                names.add(func.id)
    return names


def _live_modules() -> dict[str, set[str]]:
    """Every ``*_live.py`` beside this file, mapped to the names it calls."""
    return {path.name: _called_names(path) for path in sorted(_TESTS_DIR.glob("*_live.py"))}


def test_every_live_module_that_writes_guards_its_target() -> None:
    """A live module that mutates Olog must call the target guard. Olog has no delete.

    This is the half that survives the author who forgets. Which modules write is DERIVED here
    rather than listed, so a fifth one is covered the day it is written.

    WHAT THIS DOES NOT SEE, so nobody reads it as more than it is:

    * it checks THAT the guard is called, not WITH WHICH target. A module could hand it a
      constant. The guard's own tests above cover the decision; this one covers the wiring.
    * its population is every ``*_live.py`` beside this file. A write probe under another name,
      or in another directory, is invisible to it.
    * a module that writes through a HELPER of its own, whose name is not in ``_WRITE_CALLS``,
      escapes it.
    * ``_WRITE_CALLS`` is a list, and the server can grow a mutating operation without this file
      appearing in the diff. It is re-measured by hand or not at all.

    Provably red: delete the ``assert_write_target_is_local`` call from any module it names.
    """
    writing = {name: calls for name, calls in _live_modules().items() if calls & _WRITE_CALLS}
    # Idle-run anchor: an empty population would make the assertion below vacuously true, which is
    # the failure mode of every guard built on a glob.
    assert writing, "no live module calls a mutating Olog operation, the population broke"
    unguarded = sorted(
        name for name, calls in writing.items() if "assert_write_target_is_local" not in calls
    )
    assert not unguarded, (
        f"these live modules write to Olog without guarding their target: {unguarded}. "
        "Olog has no delete. Add an autouse fixture calling assert_write_target_is_local."
    )


def test_every_live_module_gates_itself() -> None:
    """Every live module must call the SKIP gate, not only the writing ones.

    Two reasons it is pinned rather than left to review. A live module without it skips SILENTLY,
    which is the defect ``live_gate.py`` was written against in the first place. And the target
    guard above DEPENDS on it: an unconfigured module is supposed to skip at the skip gate, which
    is why the target guard treats an unset URL as none of its business.

    Provably red: delete the ``assert_live_available`` call from any live module.
    """
    modules = _live_modules()
    assert modules, "no live modules found, the population broke"
    ungated = sorted(
        name for name, calls in modules.items() if "assert_live_available" not in calls
    )
    assert not ungated, f"these live modules never call the skip gate: {ungated}"
