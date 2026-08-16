"""The port rule in :mod:`epics_mcp.epics_address` against the REAL installed client.

Why this file exists at all. ``doctor._live_search_posture`` prints the effective search
destinations WITHOUT building a p4p Context, deliberately: its no-probe path promises no default
egress, and constructing a Context binds sockets, starts worker threads and resolves names. That
promise is only affordable because the port arithmetic it re-implements is pinned HERE against the
library that really decides it. Take this file away and the reach line becomes an opinion.

⚠️ This is the one test module in the repository that BUILDS a client Context. That is the point,
not an oversight: a pin against a hand-written model of pvxs would pin the model. It stays cheap
and egress-free by construction, never by luck:

* ``useenv=False`` with an explicit ``conf`` dict, so the ambient environment cannot leak in and
  the answer is a function of the argument;
* IP LITERALS only, so no name resolution happens (a hostname would make this test do DNS);
* ``EPICS_PVA_AUTO_ADDR_LIST=NO``, so no subnet broadcast destination is added;
* nothing is ever searched for, so no packet leaves.

⚠️ pvxs writes C-level ``ERR pvxs.config ... ignoring invalid`` lines to stderr for the refusal
cases below. pytest cannot capture those. They are the EXPECTED output of the dropped-token cases
and not a failure.

The comparison is PER ENTRY and never over the whole string: measured, pvxs reorders the list and
rewrites a multicast interface to a platform identifier (a GUID on Windows), so a whole-string
equality would be red for a reason that is not the rule.
"""

from __future__ import annotations

import pytest
from p4p.client.thread import Context

from epics_mcp.epics_address import (
    DEFAULT_PORT_VARS,
    DROPPED,
    UNMODELLED,
    effective_default_port,
    effective_search_entry,
    split_port,
)

#: One token per case. Kept to IP literals for the reason in the module docstring.
#:
#: ⚠️ The last six were added AFTER a post-build review falsified the rendering on them, and the
#: order of that discovery is the lesson: the corpus held only well-formed literals, the code
#: answered for every shape anyway, and it was wrong in BOTH directions just outside the corpus.
#: A shape not in this tuple must produce no claim at all, which is what
#: ``test_an_unmodelled_shape_makes_no_claim`` holds.
_TOKENS = (
    "10.0.0.5",  # no port: the list default applies
    "10.0.0.6:5077",  # its own port, which the default must not overwrite
    "10.0.0.5:0",  # a written zero is NOT a port, pvxs leaves the default in place
    "10.0.0.5:65536",  # wraps to 0, so also the default
    "10.0.0.5:70000",  # wraps to 4464, silently, with no diagnostic
    "[::1]",  # bracketed IPv6, no port
    "[::1]:5077",  # bracketed IPv6 with a port
    "::1",  # bare IPv6: several colons, none of them a port separator
    "10.0.0.5:abc",  # refused: becomes NO entry
    "10.0.0.5:",  # refused for the same reason
    "[2001:db8::1]x",  # trailing junk: pvxs IGNORES it and searches the address
    "[2001:db8::1",  # unclosed bracket: pvxs refuses the whole entry
    "10.0.0.5,",  # a bare trailing comma: the entry disappears
    "10.0.0.5,255",  # a ttl without an interface: the entry survives
    "10.0.0.5,255@127.0.0.1",  # full multicast: survives, interface rewritten
    ":5076",  # no host: PLATFORM-dependent, so nothing is claimed (see the empty-host test below)
    "[]",  # no host either, through the BRACKET branch: the same non-claim by a different route
    ":abc",  # no host AND an unreadable port: the port refusal decides, and it is platform-blind
)


def _effective_entries(var: str, value: str, port_var: str | None, port: str | None) -> list[str]:
    """What the installed client really makes of *value*, as the entries of *var*."""
    conf = {var: value, "EPICS_PVA_AUTO_ADDR_LIST": "NO"}
    if port_var and port:
        conf[port_var] = port
    with Context("pva", conf=conf, useenv=False) as ctx:
        # p4p is untyped, so conf() is Any; narrowed here rather than at four call sites.
        effective: str = ctx.conf()[var]
    return effective.split()


def _port_of(entry: str) -> str:
    """The port of one effective entry, for the shapes pvxs emits.

    Handles ``host:port``, ``[v6]:port`` and the multicast ``addr:port,ttl@iface``.
    """
    head = entry.split(",", 1)[0]
    if head.startswith("["):
        return head[head.rindex("]") + 2 :]
    return head.rsplit(":", 1)[1]


@pytest.mark.parametrize("token", _TOKENS)
@pytest.mark.parametrize(
    ("var", "port_var", "default"),
    [
        ("EPICS_PVA_ADDR_LIST", "EPICS_PVA_BROADCAST_PORT", "5076"),
        ("EPICS_PVA_NAME_SERVERS", "EPICS_PVA_SERVER_PORT", "5075"),
    ],
)
def test_our_port_rule_is_total_over_the_token_corpus(
    token: str, var: str, port_var: str, default: str
) -> None:
    """For EVERY token: we name the client's port, or we say DROPPED and the client made no entry.

    TOTAL on purpose. An earlier draft of the rule answered "port unknown" for four of these
    shapes, and a pin that accepted "unknown" as an outcome would have verified only the two cases
    nobody gets wrong (``host`` and ``host:port``) while blessing an answer that was measurably
    false for the other four. There is therefore no third branch here: ``unknown`` is not a
    permitted result of :func:`split_port`, and if one is ever introduced this assertion fails.
    """
    entries = _effective_entries(var, token, None, None)
    written = split_port(token)
    if written is UNMODELLED:
        # No claim is made for this shape, so there is nothing to be wrong about. What IS pinned
        # is that the renderer stays silent too, which is the other half of the same promise.
        assert effective_search_entry(token, default) == token
        return
    if written is DROPPED:
        assert entries == [], (
            f"we say the client DROPS {token!r}, but it kept {entries!r}. A refusal we invent is "
            "worse than the raw token: it hides a live destination."
        )
        return
    assert len(entries) == 1, f"expected exactly one effective entry for {token!r}, got {entries!r}"
    expected = written if isinstance(written, str) else default
    assert _port_of(entries[0]) == expected


@pytest.mark.parametrize("token", _TOKENS)
@pytest.mark.parametrize(
    ("var", "default"),
    [("EPICS_PVA_ADDR_LIST", "5076"), ("EPICS_PVA_NAME_SERVERS", "5075")],
)
def test_what_the_doctor_prints_is_what_the_client_uses(token: str, var: str, default: str) -> None:
    """The pin over the RENDERING, not only over the port helper underneath it.

    The distinction cost a whole round. The previous version pinned ``split_port`` alone, so the
    function the doctor actually calls was never held against the library, and four token shapes
    rendered into the report as destinations the client did not have (a bracketed address with
    trailing junk reported DROPPED while pvxs searched it, a trailing comma reported as live while
    pvxs dropped it). "There is a pin for that rule" is a claim about which FUNCTION it covers.

    Three permitted outcomes, and no fourth: the rendered endpoint equals the client's, or we said
    DROPPED and the client kept nothing, or we made no claim at all (the token comes back verbatim).

    ⚠️ There used to be a fourth, ``host replaced by a local interface address``, for a token
    written without a host. It is gone with the claim it qualified: that claim was measured true on
    Windows and false on Linux, where the client makes no entry at all, so the shape is now a
    non-claim and leaves through the first branch. A branch kept for a rendering nothing produces
    looks like coverage and is none.
    """
    rendered = effective_search_entry(token, default)
    entries = _effective_entries(var, token, None, None)
    if rendered == token:
        return  # no claim was made
    if "DROPPED" in rendered:
        assert entries == [], f"we invented a refusal for {token!r}; the client kept {entries!r}"
        return
    assert entries == [rendered], (
        f"the report would print {rendered!r} for {token!r}, the client really uses {entries!r}"
    )


@pytest.mark.parametrize("token", [":5076", "[]", "[]:5077"])
def test_a_token_without_a_host_is_never_claimed_on_any_platform(token: str) -> None:
    """The empty host is decided by the RESOLVER, so no endpoint is claimed for it anywhere.

    This is the assertion that made CI red and this file honest. Measured on one token, both
    platforms: ``EPICS_PVA_ADDR_LIST=":5076"`` gives one of this machine's own interface addresses
    on Windows and NOTHING on Linux (``Error resolving ""``, and the context reports it has no
    search destination at all). The report used to state the port and say the host had been
    replaced, which was true on the first platform and invented reach on the second.

    ⚠️ It is a TRADE and not a pure win: a statement that was true on Windows is given up to avoid
    one that is false on Linux. What replaces it is not silence, it is ``doctor._EMPTY_HOST_NOTE``,
    which names the way back to a claimable endpoint (write the host out).

    All three spellings, because the gate is on :func:`split_host` and not on the ``:`` partition:
    ``[]`` and ``[]:5077`` reach the empty host through the BRACKET branch, and a gate placed at
    the partition would render an endpoint for them while claiming to have fixed the shape.

    ⚠️ BOTH halves are asserted, and the first is why: on ``:5076`` the RENDERING alone is red-proof
    against nothing. Measured on the mutant with the gate deleted, ``split_port`` claims ``5076``
    again while the renderer still prints ``:5076``, because an empty host plus ``":" + "5076"``
    happens to reproduce the token. The string is a coincidence; the decision is what this pins.

    Provably red on every platform: delete the ``if not split_host(token)`` gate at the top of
    ``split_port``. Measured on that mutant, all three fail on the first assertion and the two
    bracketed spellings fail on the second as well.
    """
    assert split_port(token) is UNMODELLED, (
        f"a claim is being made about {token!r}, whose host the platform resolver decides"
    )
    assert effective_search_entry(token, "5076") == token


@pytest.mark.parametrize("token", ["[2001:db8::1]x", "10.0.0.5,", "10.0.0.5,255@127.0.0.1"])
def test_an_unmodelled_shape_makes_no_claim(token: str) -> None:
    """A shape outside the measured corpus is printed as written, with nothing asserted about it.

    Red-provable by deleting the ``UNMODELLED`` branch in ``split_port``: each of these then gets
    an answer, and ``test_what_the_doctor_prints_is_what_the_client_uses`` goes red on it, in one
    direction or the other.
    """
    assert effective_search_entry(token, "5076") == token


@pytest.mark.parametrize(
    ("var", "port_var", "other_port_var"),
    [
        ("EPICS_PVA_ADDR_LIST", "EPICS_PVA_BROADCAST_PORT", "EPICS_PVA_SERVER_PORT"),
        ("EPICS_PVA_NAME_SERVERS", "EPICS_PVA_SERVER_PORT", "EPICS_PVA_BROADCAST_PORT"),
    ],
)
def test_each_list_reads_its_own_port_variable_and_not_the_other(
    var: str, port_var: str, other_port_var: str
) -> None:
    """The two variables do not cross over, asserted in BOTH directions.

    The direction that matters is the second one. A test that only set the right variable and saw
    the right port would stay green if :data:`DEFAULT_PORT_VARS`' two rows were swapped, because
    each list would then read a variable that simply happened to be unset. Two DIFFERENT non-default
    values are used for the same reason: with one value the swap is invisible.
    """
    mine, theirs = "5099", "5080"
    with_mine = _effective_entries(var, "10.0.0.5", port_var, mine)
    assert _port_of(with_mine[0]) == mine

    with_theirs = _effective_entries(var, "10.0.0.5", other_port_var, theirs)
    assert _port_of(with_theirs[0]) == DEFAULT_PORT_VARS[var][1], (
        f"{other_port_var} moved {var}, so the two rows of DEFAULT_PORT_VARS are crossed"
    )


def test_the_pinned_defaults_are_the_clients_own() -> None:
    """The fallbacks in :data:`DEFAULT_PORT_VARS` are read back from the client, not asserted.

    Only the two PVA rows: the CA rows are source-derived and unmeasurable here, which the table's
    own comment states. A test that pretended to verify them would be the sham this repository's
    audit exists to find.
    """
    for var in ("EPICS_PVA_ADDR_LIST", "EPICS_PVA_NAME_SERVERS"):
        entries = _effective_entries(var, "10.0.0.5", None, None)
        assert _port_of(entries[0]) == DEFAULT_PORT_VARS[var][1]


@pytest.mark.parametrize(
    ("var", "port_var", "fallback"),
    [
        ("EPICS_PVA_ADDR_LIST", "EPICS_PVA_BROADCAST_PORT", "5076"),
        ("EPICS_PVA_NAME_SERVERS", "EPICS_PVA_SERVER_PORT", "5075"),
    ],
)
@pytest.mark.parametrize("written", ["0", "65536", "70000", "abc", ""])
def test_the_port_VARIABLE_goes_through_the_same_arithmetic_as_a_token(
    var: str, port_var: str, fallback: str, written: str
) -> None:
    """The default port a list really gets, held against the client for INVALID values too.

    This whole family was missing, and a post-build review found what that cost: the variable's
    value was printed raw, so ``EPICS_PVA_BROADCAST_PORT=70000`` made the line say every port-less
    entry went to ``:70000`` while the client dialled ``:4464``, and ``=abc`` rendered them as
    ``10.0.0.5:abc``, a live endpoint on a port that cannot exist. The old pin used only ``5099``
    and ``5080``, i.e. exactly the values that cannot expose the gap. Invalid values are the point.
    """
    ours = effective_default_port(written, fallback, zero_keeps_fallback="ADDR_LIST" in var)
    entries = _effective_entries(var, "10.0.0.5", port_var or None, written or None)
    if ours is None:
        # We refuse to name a port. Whatever the client then does (its own fallback, or no port at
        # all on the name-server list) the report states nothing, which is the only honest answer.
        return
    assert ours == _port_of(entries[0])


def test_the_harness_observes_a_change_at_all() -> None:
    """Negative control. Without it every assertion above could be comparing a constant to itself.

    Proves two things the other tests assume: the effective list follows the ADDRESS we pass, and
    it follows the PORT variable we pass.
    """
    assert _effective_entries("EPICS_PVA_ADDR_LIST", "10.0.0.5", None, None) != _effective_entries(
        "EPICS_PVA_ADDR_LIST", "10.0.0.7", None, None
    )
    assert _effective_entries(
        "EPICS_PVA_ADDR_LIST", "10.0.0.5", "EPICS_PVA_BROADCAST_PORT", "5099"
    ) != _effective_entries("EPICS_PVA_ADDR_LIST", "10.0.0.5", None, None)
