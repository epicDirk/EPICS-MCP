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
#: Both measured (``:70000`` resolves to ``:4464``, ``:65536`` to the default, ``:0`` to the
#: default). Named rather than inlined because the two call sites below have to agree.
_PORT_MODULUS = 65536


def split_port(token: str) -> str | None | _Dropped:
    """The port written INTO one address-list token, if any.

    Three outcomes, and the third is the one a two-valued answer would have got wrong:

    * ``None``: the token carries no port of its own, so the list's default applies.
    * ``str``: the port this token resolves to, already through pvxs' own uint16 arithmetic.
    * :data:`DROPPED`: the client REFUSES this token and searches nowhere for it.

    That third outcome is not a nuance. Measured, ``10.0.0.5:abc`` does not become an entry with
    an unknown port, it becomes NO ENTRY: the effective address list comes back empty and pvxs
    logs ``ignoring invalid`` on stderr, where a report never sees it. An earlier draft of this
    function rendered such a token as "port unknown", which would have described a destination
    the client had thrown away as a live one. A report about reach must not invent reach.

    Everything here is about the PORT half. Two token shapes have a HOST half this function
    cannot predict either, and they are handled by the caller rather than swallowed here: an
    empty host (``:5076``) is replaced by one of this machine's own interface addresses, and a
    multicast ``@interface`` suffix is rewritten to a platform identifier. Both measured.
    """
    # A multicast token is ``addr[:port],ttl@iface``: only the head can carry a port.
    head = token.split(",", 1)[0]
    if head.startswith("["):
        end = head.find("]")
        if end == -1:
            return DROPPED  # unclosed bracket, the client cannot read it either
        rest = head[end + 1 :]
        if not rest:
            return None
        if not rest.startswith(":"):
            return DROPPED
        return _port_value(rest[1:])
    if head.count(":") != 1:
        return None  # bare host, or a bare IPv6 literal (several colons, no port)
    _, _, port = head.partition(":")
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


def effective_search_entry(token: str, default_port: str) -> str:
    """One address-list token as this client will really use it, or as a refusal.

    The rendering the doctor's reach line prints. It states a port for every token that keeps
    one, says DROPPED for a token the client discards, and flags the two shapes whose HOST it
    cannot predict rather than printing a host that is not the one dialled.
    """
    port = split_port(token)
    if port is DROPPED:
        return f"{token} (DROPPED by this client, it is not searched)"
    resolved = port if isinstance(port, str) else default_port
    head = token.split(",", 1)[0]
    host = split_host(head)
    if not host:
        # ``:5076``: measured, pvxs substitutes one of THIS machine's interface addresses, which
        # is neither loopback nor predictable from the configuration.
        return f"{token} (port {resolved}, host replaced by a local interface address)"
    if "," in token:
        # Multicast ``addr,ttl@iface``: the address and the port are predictable, the interface
        # is rewritten (measured: to a GUID on Windows), so the suffix is shown as written.
        return f"{host}:{resolved},{token.split(',', 1)[1]} (interface rewritten by the client)"
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
