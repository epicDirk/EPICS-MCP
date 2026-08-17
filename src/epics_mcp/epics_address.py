"""EPICS client search-reach primitives, address-list parsing + loopback classification (E8).

The EPICS *client* search env decides where a PV search (and therefore a PV write) can go:
``EPICS_PVA/CA_ADDR_LIST`` (UDP search targets), ``EPICS_PVA/CA_NAME_SERVERS`` (TCP unicast, not
subnet-bound) and ``EPICS_PVA/CA_AUTO_ADDR_LIST`` (subnet broadcast, which EPICS defaults to **ON**
when unset). This module is the ONE parser for those values, the write-reach startup assert in
:mod:`epics_mcp.safety` and any external lane validator import from here (build-once), so the
two can never drift apart.

Deliberately fail-closed and resolution-free: a hostname (``some-gateway``,
``host.docker.internal``, any FQDN) is **never** classified loopback, even if DNS would resolve it
to 127.0.0.1, resolving would make the safety verdict depend on the resolver's answer at assert
time, not on the config.

Note on server-side vars: ``EPICS_CAS_*`` / ``EPICS_PVAS_*`` (incl. ``*_BEACON_ADDR_LIST``) are
IOC-**server**-side knobs, they do not exist in an EPICS *client* process like this server, so the
reach check deliberately does not look for them.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping

#: The six EPICS client search-reach env vars, per provider. The list vars are enumerated
#: individually (never or-folded, a fallback chain would mask all but the first); the
#: ``*_AUTO_ADDR_LIST`` pair is special because its UNSET state means ON (broadcast).
CLIENT_REACH_PROVIDERS = ("pva", "ca")


class _Dropped:
    """The client REFUSES this token: it becomes no search destination at all.

    A distinct type rather than ``None`` or an empty string, because the three outcomes of
    :func:`split_port` are genuinely three and collapsing any two of them is how a report starts
    claiming reach it does not have. ``None`` already means "no port written here, the list
    default applies", which is the opposite of a refusal.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return "DROPPED"


#: The single instance; compare with ``is``, never by equality.
DROPPED = _Dropped()


class _Unmodelled:
    """This token shape has NOT been held against the real client, so nothing is claimed about it.

    Distinct from :data:`DROPPED`, which is a measured statement that the client throws the token
    away. This one is the absence of a statement, and it exists because answering for every shape
    produced two wrong answers in opposite directions (see :func:`split_port`).
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return "UNMODELLED"


#: The single instance; compare with ``is``, never by equality.
UNMODELLED = _Unmodelled()


def auto_addr_search_disabled(provider: str, value: str) -> bool:
    """Whether *value* actually disables the auto-addr search for THIS provider's parser.

    Deliberately parser-faithful, not generous: honouring a spelling the real parser
    rejects (e.g. ``false``, or a padded ``"0 "``) would claim isolation while the client
    keeps broadcasting, the exact false claim BG14 removed. The two parsers differ:

    * pvxs ``parse_bool`` (repos/pvxs/src/config.cpp) accepts ONLY case-insensitive
      ``NO`` or exactly ``0``, untrimmed (PickOne hands over the raw getenv value);
      anything else is a parse error that keeps the DEFAULT, and the default is ON.
    * libca (epics-base modules/ca/src/client/iocinf.cpp) disables only when the value
      CONTAINS the substring ``no`` or ``NO`` (case-sensitive strstr), ``false``, ``0``
      and even ``No`` keep broadcasting.
    """
    if provider == "ca":
        return "no" in value or "NO" in value
    return value == "0" or value.upper() == "NO"


def split_host(token: str) -> str:
    """Extract the host part of one EPICS address-list token (``host[:port]``).

    Handles bracketed IPv6 (``[::1]:5075`` → ``::1``) and bare IPv6 (``::1``, more than one
    colon, no port split). Anything malformed (unclosed bracket, non-numeric port) is returned
    whole, so the loopback check downstream fails closed on it instead of misreading it.
    """
    if token.startswith("["):
        end = token.find("]")
        if end != -1:
            return token[1:end]
        return token  # unclosed bracket, fail closed downstream
    if token.count(":") == 1:
        host, _, port = token.partition(":")
        if port.isdigit():
            return host
        return token  # not a port suffix, fail closed downstream
    return token  # bare host, or bare IPv6 (multiple colons)


def parse_address_list(value: str) -> list[str]:
    """Split a space-separated EPICS address list into its host parts (ports stripped)."""
    return [split_host(token) for token in value.split()]


#: The port variable each search LIST reads its default from, with that variable's own default.
#:
#: MEASURED for the two PVA rows against the installed client (p4p 4.2.2 / PVXS 1.5.1), by reading
#: back ``Context("pva", conf=..., useenv=False).conf()``: the default lands PER ENTRY and only on
#: entries carrying no port of their own, and the two variables do NOT cross over
#: (``EPICS_PVA_BROADCAST_PORT`` leaves ``EPICS_PVA_NAME_SERVERS`` alone). Pinned, both
#: directions, by ``tests/test_epics_address_ports_pinned.py``.
#:
#: ⚠️ The two CA rows are SOURCE-DERIVED ONLY (epics-base ``modules/ca/src/client/iocinf.cpp``,
#: ``addAddrToChannelAccessAddressList(..., port, ...)`` with ``port`` from
#: ``EPICS_CA_SERVER_PORT``). They cannot be measured here and are not pinned, because this p4p
#: offers no CA provider at all: there is no client to read them back from. Reading source is not
#: a measurement, so they are labelled rather than presented as equals.
DEFAULT_PORT_VARS: Mapping[str, tuple[str, str]] = {
    "EPICS_PVA_ADDR_LIST": ("EPICS_PVA_BROADCAST_PORT", "5076"),
    "EPICS_PVA_NAME_SERVERS": ("EPICS_PVA_SERVER_PORT", "5075"),
    "EPICS_CA_ADDR_LIST": ("EPICS_CA_SERVER_PORT", "5064"),
    "EPICS_CA_NAME_SERVERS": ("EPICS_CA_SERVER_PORT", "5064"),
}

#: pvxs parses a port with ``parseTo<uint16_t>``, so a number past the 16-bit range WRAPS instead
#: of being rejected, and a wrap landing on 0 then takes the default like an absent port does.
#: Measured on the PVA side (``:70000`` resolves to ``:4464``, ``:65536`` and ``:0`` to the
#: default), in a token and in the port variable alike.
#: ⚠️ NOT measured for the CA rows of :data:`DEFAULT_PORT_VARS`, and they get this arithmetic
#: anyway. libca range-checks rather than wraps, so a CA entry past the range is probably rendered
#: with a port libca would refuse. It cannot be measured here at all (this p4p has no CA provider
#: to read back from), and it is stated rather than quietly inherited, because the ``[inert]``
#: note tells the reader those variables matter to a differently built client.
_PORT_MODULUS = 65536


def split_port(token: str) -> str | None | _Dropped | _Unmodelled:
    """The port written INTO one address-list token, if any.

    FOUR outcomes, and the fourth is a correction paid for by a post-build review:

    * ``None``: the token carries no port of its own, so the list's default applies.
    * ``str``: the port this token resolves to, already through pvxs' own uint16 arithmetic.
    * :data:`DROPPED`: the client REFUSES this token and searches nowhere for it.
    * :data:`UNMODELLED`: this shape is outside what has been measured against the real client,
      so NOTHING is claimed about it and the caller prints the token as written.

    ⛔ WHY THE FOURTH EXISTS, and it is the whole lesson of this function. The first version had
    only the first three, i.e. it answered for EVERY token. Measured afterwards against the real
    client, it was wrong in both directions on shapes just outside its corpus: ``[2001:db8::1]x``
    was reported ``DROPPED`` while pvxs ignores the trailing junk and really searches that
    globally routable address, and ``10.0.0.5,`` was reported as a live destination while pvxs
    drops it. An invented refusal HIDES a destination; an invented destination CLAIMS reach that
    does not exist. Both are the dishonesty this whole line exists to remove, so a shape that has
    not been held against the client is now a NON-CLAIM rather than a guess.

    ⚠️ The corpus that earns a claim is exactly the one ``tests/test_epics_address_ports_pinned.py``
    runs against the installed library. Widen the corpus first, then widen this function; the other
    order is how the three wrong answers above got written.

    THE HOST HALF ENTERS ONLY HERE, and only to withhold a claim. An EMPTY host (``:5076``, ``[]``,
    ``[]:5077``) is resolved by the platform, and this module is deliberately resolution-free (see
    the module docstring). Measured on ``:5076``, both platforms: pvxs substitutes one of this
    machine's own interface addresses on Windows, and refuses the entry outright on Linux
    (``Error resolving ""``; on a list holding nothing else, the context then has no search
    destination at all). Naming the port would therefore be false on the platform where no entry
    exists to carry it.

    ⚠️ THAT IS A TRADE, not a free win, and it is written down rather than quietly enjoyed: the
    Windows answer was true, and it is given up to avoid the Linux one that is not. The way back
    to a claimable endpoint belongs to the operator and is named in the report itself, not only
    here: spell the host out (``127.0.0.1:5076``).

    ⛔ AND THE ORDER IS LOAD-BEARING: a DROPPED port beats the empty host, never the other way
    round. The first version asked about the host FIRST, and a post-build review measured what that
    cost: ``[]:abc`` and ``[]:`` stopped being DROPPED and became non-claims, so the report withheld
    a refusal it had measured AND told the operator a platform resolver might hand them an address,
    while the real client keeps no entry for either. A port the client cannot read is refused before
    any host is looked up, which makes that verdict platform-blind and therefore the stronger claim.
    """
    written = _port_written(token)
    if written is DROPPED:
        return DROPPED
    if not split_host(token):
        return UNMODELLED
    return written


def _port_written(token: str) -> str | None | _Dropped | _Unmodelled:
    """The PORT half of *token* alone, with no question asked about its host.

    Split out from :func:`split_port` so the host question can be asked AFTER this one rather than
    before it; see the order paragraph there for what asking it first cost.
    """
    if "," in token or "@" in token:
        # Multicast ``addr[:port],ttl@iface``. Measured: ``10.0.0.5,`` disappears entirely while
        # ``10.0.0.5,255`` survives, and the interface is rewritten to a platform identifier.
        # None of that is in the pinned corpus, so nothing is claimed. ⚠️ Tested for the COMMA,
        # not for a non-empty tail: ``"10.0.0.5,".partition(",")`` gives an empty tail, so a
        # trailing comma slipped through the first version of this guard and was rendered
        # ``10.0.0.5,:5076``, an address the client does not have.
        return UNMODELLED
    if token.startswith("["):
        end = token.find("]")
        if end == -1:
            return UNMODELLED  # unclosed bracket: never held against the client
        rest = token[end + 1 :]
        if not rest:
            return None
        if not rest.startswith(":"):
            # Trailing junk after the bracket. pvxs IGNORES it and keeps the address; reporting a
            # refusal here hid a live destination until a review caught it.
            return UNMODELLED
        return _port_value(rest[1:])
    if token.count(":") != 1:
        return None  # bare host, or a bare IPv6 literal (several colons, no port)
    _, _, port = token.partition(":")
    return _port_value(port)


def _port_value(port: str) -> str | None | _Dropped:
    """One written port through pvxs' own arithmetic. ``None`` means "the default applies"."""
    if not port.isdigit():
        return DROPPED  # empty or non-numeric: measured, the whole entry disappears
    wrapped = int(port) % _PORT_MODULUS
    # A written 0, and a wrap that lands on 0, both leave the default in place: pvxs only
    # OVERRIDES udp_port/tcp_port with a non-zero value (src/config.cpp).
    return str(wrapped) if wrapped else None


def is_ip_literal(token: str) -> bool:
    """Whether *token*'s host is an IP literal rather than a name the client has to resolve.

    The distinction is not cosmetic. Measured: pvxs resolves each name at context construction
    and DROPS the entry when resolution fails, logging ``ignoring invalid`` to stderr only, so a
    list holding a name can shrink between what is configured and what is searched. A report that
    prints resolved endpoints therefore has to say which of them still depend on a resolver.
    """
    try:
        ipaddress.ip_address(split_host(token.split(",", 1)[0]))
    except ValueError:
        return False
    return True


def effective_default_port(written: str, fallback: str, *, zero_keeps_fallback: bool) -> str | None:
    """The default port a LIST really gets from its port variable, or None when nothing is claimed.

    The same arithmetic :func:`split_port` applies to a port written in a token, applied to a port
    written in the VARIABLE. That it is the same rule is the point: measured, a review found the
    variable's value being printed raw, so ``EPICS_PVA_BROADCAST_PORT=70000`` rendered every
    port-less entry as ``:70000`` while the client dialled ``:4464``, and ``=abc`` rendered them
    as ``:abc``, a live endpoint on a port that cannot exist. One rule, one place, both sources.

    ⚠️ *zero_keeps_fallback* is a MEASURED asymmetry between the two lists, not a preference, and
    it comes from an ordering quirk in pvxs' own parsing (``src/config.cpp``): the
    ``tcp_port == 0`` fallback is evaluated while ``nameServers`` is still empty, so it never
    fires. Read back from the installed client:

    * ``EPICS_PVA_ADDR_LIST`` with ``EPICS_PVA_BROADCAST_PORT=0`` gives ``10.0.0.5:5076``.
    * ``EPICS_PVA_NAME_SERVERS`` with ``EPICS_PVA_SERVER_PORT=0`` gives ``10.0.0.5``, with NO port.

    ``None`` means no claim: the variable is unreadable, or it resolves to "no port" on a list
    where that is not the fallback. The caller then prints the entries as written, because naming
    either number would be a guess.
    """
    if not written:
        return fallback
    value = _port_value(written)
    if value is DROPPED:
        return None  # unreadable: pvxs warns and keeps its own default, which we do not restate
    if isinstance(value, str):
        return value
    return fallback if zero_keeps_fallback else None


def effective_search_entry(token: str, default_port: str | None) -> str:
    """One address-list token as this client will really use it, or as an honest non-claim.

    The rendering the doctor's reach line prints. It states an endpoint only for the shapes held
    against the real client in ``tests/test_epics_address_ports_pinned.py``; everything else is
    printed AS WRITTEN with no assertion attached.

    ⚠️ *default_port* is ``None`` when the list's port variable is set to something pvxs cannot
    read. No entry can then be resolved, because the fallback the client actually uses is not the
    one the operator wrote, and printing either would be a guess.
    """
    port = split_port(token)
    if port is DROPPED:
        return f"{token} (DROPPED by this client, it is not searched)"
    if port is UNMODELLED or default_port is None:
        # No claim. The token stands as written, which is what the line did before it learned to
        # resolve anything, and it is the only honest output for a shape nothing has measured.
        return token
    resolved = port if isinstance(port, str) else default_port
    # An empty host never reaches this point: split_port answers UNMODELLED for it, so the token
    # has already been printed as written above. The caveat the operator needs for that shape is
    # the doctor's own _EMPTY_HOST_NOTE, which can say what to do about it; a rendering cannot.
    host = split_host(token)
    return f"{host}:{resolved}" if ":" not in host else f"[{host}]:{resolved}"


def is_loopback_host(host: str) -> bool:
    """Whether *host* is provably loopback, ``localhost`` or a loopback IP literal.

    Any name that is not an IP literal (FQDN, ``host.docker.internal``, a bare hostname)
    is **False**: it is not resolved (fail-closed, see module docstring).
    """
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def write_reach_violations(environ: Mapping[str, str]) -> list[str]:
    """Every way *environ* lets an EPICS client search beyond loopback, as human-readable strings.

    Empty list == the client search reach is provably loopback-only: for BOTH providers the
    auto-addr subnet broadcast is parser-faithfully disabled AND every ``ADDR_LIST`` /
    ``NAME_SERVERS`` token is a loopback host. An empty/unset list var is fine (no search
    target), but an unset ``*_AUTO_ADDR_LIST`` is a violation, unset means broadcast ON.

    Both providers are checked regardless of the configured ``EPICS_MCP_PROVIDER``: the vars
    are process-global standard EPICS env, and a mis-set idle provider costs nothing to forbid.
    """
    violations: list[str] = []
    for provider in CLIENT_REACH_PROVIDERS:
        prefix = f"EPICS_{provider.upper()}"
        auto_var = f"{prefix}_AUTO_ADDR_LIST"
        # Raw value, deliberately NOT normalised: neither parser trims, and honouring a
        # spelling the real client rejects would claim isolation while it broadcasts.
        auto_value = environ.get(auto_var, "")
        if not auto_addr_search_disabled(provider, auto_value):
            state = f"={auto_value!r}" if auto_value else " unset"
            violations.append(
                f"{auto_var}{state} does not disable the subnet broadcast search "
                f"(unset/empty means ON; this provider's parser accepts only "
                f"{'a value containing no/NO' if provider == 'ca' else 'NO or 0'})"
            )
        for suffix in ("ADDR_LIST", "NAME_SERVERS"):
            var = f"{prefix}_{suffix}"
            for token in (environ.get(var) or "").split():
                host = split_host(token)
                if not is_loopback_host(host):
                    violations.append(f"{var} entry {token!r}: host {host!r} is not loopback")
    return violations
