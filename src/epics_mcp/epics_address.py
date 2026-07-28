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
