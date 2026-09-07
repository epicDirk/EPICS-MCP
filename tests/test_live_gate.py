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


_TESTS_DIR = Path(__file__).parent


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


def test_the_refusal_is_a_failure_and_never_a_skip() -> None:
    """A forbidden target must go red, and it must not go red as a SKIP.

    Both halves are asserted, and the second alone would be a trap: with the refusal replaced by a
    bare ``return`` the outcome is ``None``, and ``None`` is not a Skipped either, so a test that
    only forbade the skip class would stay green over a guard that does nothing. Measured on that
    mutant.
    """
    outcome = _outcome_of(lambda: assert_write_target_is_local(FORBIDDEN_URL, variable="X"))
    assert isinstance(outcome, pytest.fail.Exception), f"the guard did not fail: {outcome!r}"
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


def test_the_refusal_does_not_echo_credentials() -> None:
    """The message names the HOST, never the URL, because a URL can carry a password.

    This repository has already paid for that class once, which is why ``url_without_credentials``
    and ``shown_url`` exist. A message built from the raw URL would pass the assertions above and
    print the secret into every CI log that captures the failure.
    """
    outcome = _outcome_of(
        lambda: assert_write_target_is_local(
            "https://svc:hunter2@10.0.0.5/Olog", variable="EPICS_MCP_OLOG_URL"
        )
    )
    message = str(outcome)
    assert "10.0.0.5" in message, message
    assert "hunter2" not in message, "the refusal echoed a credential from the URL"
    assert "svc" not in message, "the refusal echoed the user name from the URL"


def test_an_unparseable_target_gets_its_own_repair_instruction() -> None:
    """A URL without a scheme has no host, and "point it at localhost" would be useless advice.

    ``localhost:8080/Olog`` IS localhost to a human and is not a URL to any parser, so the reader
    needs to be told the scheme is missing, not the host.
    """
    outcome = _outcome_of(
        lambda: assert_write_target_is_local("localhost:8080/Olog", variable="EPICS_MCP_OLOG_URL")
    )
    message = str(outcome)
    assert isinstance(outcome, pytest.fail.Exception), f"expected Failed, got {outcome!r}"
    assert "scheme" in message, message


# --- the declared-local lane --------------------------------------------------


DECLARED_URL = "https://olog.rig.example:8443/Olog"


def test_a_verbatim_declaration_lets_a_hostname_rig_through() -> None:
    """A local rig reached under a hostname is admitted when the operator repeats its URL."""
    outcome = _outcome_of(
        lambda: assert_write_target_is_local(
            DECLARED_URL, variable="P", ack=DECLARED_URL, ack_variable="P_IS_LOCAL"
        )
    )
    assert outcome is None, f"a declared rig was refused: {outcome!r}"


@pytest.mark.parametrize(
    ("ack", "why"),
    [
        (None, "no declaration at all"),
        ("", "an empty declaration"),
        ("true", "a switch instead of the URL"),
        ("https://olog.rig.example:8444/Olog", "a different port"),
        ("https://olog.other.example:8443/Olog", "a different host"),
    ],
)
def test_a_declaration_that_is_not_the_url_verbatim_does_not_count(
    ack: str | None, why: str
) -> None:
    """The declaration must BE the URL, so it cannot degrade into an on switch.

    That is what makes it worth anything: an inherited or mistyped target cannot match a
    declaration nobody typed for it, whereas a boolean would be set once and forgotten.
    """
    outcome = _outcome_of(
        lambda: assert_write_target_is_local(
            DECLARED_URL, variable="P", ack=ack, ack_variable="P_IS_LOCAL"
        )
    )
    assert isinstance(outcome, pytest.fail.Exception), f"{why} was accepted: {outcome!r}"


def test_a_declaration_is_ignored_where_no_lane_was_opened() -> None:
    """Without an *ack_variable* the caller has not opened the lane, so a matching *ack* is inert.

    Negative control for the lane itself: a guard that honoured a bare *ack* would let any caller
    self-authorise.
    """
    outcome = _outcome_of(
        lambda: assert_write_target_is_local(DECLARED_URL, variable="P", ack=DECLARED_URL)
    )
    assert isinstance(outcome, pytest.fail.Exception), f"the lane opened itself: {outcome!r}"


# --- resolution-free, asserted rather than asserted about ---------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://olog.example.org/Olog",
        # `*.localtest.me` is the shape this repository's own remote rig uses, and
        # docs/known-limits.md records that it resolves to 127.0.0.1 (with the caveat that a network
        # intercepting DNS breaks that). Whatever it resolves to, it must be REFUSED: local in fact
        # is not the same as local in the configuration.
        "https://olog.localtest.me:8443/Olog",
    ],
)
def test_the_guard_does_not_resolve_late_bound_names(
    url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Half one of the resolution-free proof: the attribute lookup is removed.

    Same guard shape as ``tests/test_provenance.py::test_nothing_here_opens_a_socket``, and faked
    at the ``socket`` module rather than at a client class for the reason stated there: a
    class-level double would remove the very call it is meant to forbid.

    ⚠️ This half alone is not the proof, and measuring showed why: a ``from socket import
    getaddrinfo`` in ``live_gate`` binds the name at import and walks straight past this
    monkeypatch. That mutation is caught by the test below, not by this one.
    """

    def _refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the write-target guard resolved a name")

    monkeypatch.setattr(socket, "socket", _refuse)
    monkeypatch.setattr(socket, "getaddrinfo", _refuse)
    monkeypatch.setattr(socket, "gethostbyname", _refuse)

    outcome = _outcome_of(lambda: assert_write_target_is_local(url, variable="X"))
    assert isinstance(outcome, pytest.fail.Exception), f"{url}: {outcome!r}"


def test_the_gate_module_cannot_resolve_at_all() -> None:
    """Half two: ``live_gate`` names nothing that could resolve, however it was imported.

    Read from the source rather than from behaviour, because that is the only way to catch an
    early-bound name: ``from socket import getaddrinfo`` survives every monkeypatch of the module
    attribute. Since the module imports no resolver under any name, the guard cannot call one.

    Provably red: add ``import socket`` to ``tests/live_gate.py``.
    """
    tree = ast.parse((_TESTS_DIR / "live_gate.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
            imported |= {alias.name for alias in node.names}
    forbidden = imported & {
        "socket",
        "getaddrinfo",
        "gethostbyname",
        "getfqdn",
        "create_connection",
    }
    assert not forbidden, (
        f"live_gate imports a resolver, so it can no longer promise not to: {forbidden}"
    )


# --- the population: every live module that WRITES must carry both guards -----


#: The Olog operations that MUTATE, named at every layer a test can actually reach them. Three,
#: because a test may enter at any of them and this tree already does at two:
#:
#: * the tool layer (``epics_mcp.tools.olog``), what the MCP tools call,
#: * the service layer (``epics_mcp.services.checkers_olog``), used by two modules here,
#: * the client (``epics_mcp.services.olog_client``) and the raw transport under it
#:   (``epics_mcp.services._http``), used by three.
#:
#: Deliberately NOT the four MCP tool names on their own. Measured 2026-09-07: the client method is
#: ``add_attachment`` rather than ``add_log_attachment``, and ``add_log_attachment`` appears in one
#: live module that never calls it, so a list of tool names alone matches this tree through
#: docstrings and test names instead of through calls.
_WRITE_CALLS = frozenset(
    {
        # tool layer
        "_create_log_entry",
        "_reply_to_log",
        "_add_log_attachment",
        "_update_log_entry",
        # service layer
        "query_olog_create",
        "query_olog_add_attachment",
        "query_olog_update",
        # client
        "create_log_entry",
        "add_attachment",
        "update_log_entry",
        # raw transport
        "rest_put_json",
        "rest_put_multipart",
        "rest_post_multipart",
    }
)


def _call_names(node: ast.AST) -> set[str]:
    """Every function or method name called anywhere under *node*."""
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Attribute):
                names.add(func.attr)
            elif isinstance(func, ast.Name):
                names.add(func.id)
    return names


def _is_autouse_fixture(node: ast.FunctionDef) -> bool:
    """Whether *node* is decorated ``@pytest.fixture(autouse=True)``.

    Autouse is what makes a call RUN. A guard call sitting in an ordinary function, or in a fixture
    nothing requests, is present in the source and absent from the run, and the difference is the
    whole point of the two tests below.
    """
    for dec in node.decorator_list:
        if not isinstance(dec, ast.Call):
            continue
        target = dec.func
        name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
        if name != "fixture":
            continue
        for kw in dec.keywords:
            if (
                kw.arg == "autouse"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value is True
            ):
                return True
    return False


def _module_facts(path: Path) -> tuple[set[str], set[str]]:
    """(everything called in *path*, everything called from an autouse fixture of *path*)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    autouse: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and _is_autouse_fixture(node):
            autouse |= _call_names(node)
    return _call_names(tree), autouse


def _live_modules() -> dict[str, tuple[set[str], set[str]]]:
    """Every ``*_live.py`` beside this file, mapped to its two call sets."""
    return {path.name: _module_facts(path) for path in sorted(_TESTS_DIR.glob("*_live.py"))}


def test_every_live_module_that_writes_guards_its_target() -> None:
    """A live module that mutates Olog must call the target guard FROM AN AUTOUSE FIXTURE.

    Olog has no delete, so this is the half that survives the author who forgets. Which modules
    write is DERIVED rather than listed, so a fifth one is covered the day it is written, subject
    to the limits below.

    Autouse is checked rather than mere presence, and that is not pedantry: dropping the
    ``autouse=True`` from a fixture removes the guard from every run while leaving the call in the
    file. A guard a one-word edit can switch off silently is the defect it was written against.

    WHAT THIS DOES NOT SEE, so nobody reads it as more than it is:

    * it checks THAT the guard is called and that the call can run, never WITH WHICH target. A
      module could hand it a local-looking constant. The guard's own tests above cover the
      decision, this one covers the wiring, and that gap has been real in this tree: the first
      version of the remote-https fixture passed a variable with a loopback DEFAULT.
    * its population is every ``*_live.py`` beside this file. A write probe under another name, or
      in another directory, is invisible to it.
    * a module that writes through a helper, its own or another module's, whose name is not in
      ``_WRITE_CALLS``, escapes it. So does a dispatch through ``getattr``.
    * ``_WRITE_CALLS`` is a list of names across three layers, and the server can grow a mutating
      operation without this file appearing in the diff. It is re-measured by hand or not at all.
    * it says nothing about the ORDER in which fixtures run. Today every fixture in every write
      module is function-scoped, so the autouse ones run first; a module-scoped client fixture
      would run before them, and nothing here would notice.

    Provably red: delete the ``assert_write_target_is_local`` call from any module it names, or
    merely drop the ``autouse=True`` from the fixture that holds it.
    """
    modules = _live_modules()
    writing = {name: facts for name, facts in modules.items() if facts[0] & _WRITE_CALLS}
    # Idle-run anchor: an empty population would make the assertion below vacuously true, which is
    # the failure mode of every guard built on a glob.
    assert writing, "no live module calls a mutating Olog operation, the population broke"
    unguarded = sorted(
        name
        for name, (_all, autouse) in writing.items()
        if "assert_write_target_is_local" not in autouse
    )
    assert not unguarded, (
        f"these live modules write to Olog without an autouse target guard: {unguarded}. "
        "Olog has no delete. Add an autouse fixture calling assert_write_target_is_local."
    )


def test_every_live_module_gates_itself() -> None:
    """Every live module must call the SKIP gate from an autouse fixture, not only the writers.

    Two reasons it is pinned rather than left to review. A live module without it skips SILENTLY,
    which is the defect ``live_gate.py`` was written against in the first place. And the target
    guard DEPENDS on it: an unconfigured module is supposed to skip at the skip gate, which is why
    the target guard treats an unset URL as none of its business.

    Autouse again, and here it was measured rather than assumed: the first attempt at a red proof
    for this test removed one of two ``assert_live_available`` calls from a module and stayed
    GREEN. Counting only autouse calls makes that mutation, and the ``autouse=True`` one, red.

    Provably red: delete the ``assert_live_available`` call from any live module's autouse fixture,
    or drop the ``autouse=True``.
    """
    modules = _live_modules()
    assert modules, "no live modules found, the population broke"
    ungated = sorted(
        name for name, (_all, autouse) in modules.items() if "assert_live_available" not in autouse
    )
    assert not ungated, (
        f"these live modules never call the skip gate from an autouse fixture: {ungated}"
    )
