"""MCP Resources for the EPICS MCP server."""

import importlib.resources
import os
import sys
import time
from functools import lru_cache

from epics_mcp import __version__
from epics_mcp.config import get_config
from epics_mcp.paths import path_boundary_configured
from epics_mcp.services._http import url_without_userinfo
from epics_mcp.services.channelfinder_client import (
    resolve_safe_owner_accounts,
    resolve_safe_property_names,
)
from epics_mcp.write_posture import (
    olog_write_gate_report,
    pv_search_posture,
    pv_write_gate_report,
)

_start_time = time.monotonic()


@lru_cache(maxsize=1)
def get_guide() -> str:
    """The operational cookbook served as ``epics://guide``.

    Reads the package-data file ``operator_guide.md`` (a sibling of ``py.typed`` inside the
    package, so hatchling ships it in the wheel and ``importlib.resources`` finds it in both an
    editable and an installed layout). Only invoked at resource-read time, so a missing file
    surfaces as a read-time error, never an import crash; ``lru_cache`` does not cache exceptions,
    so a genuinely absent file re-raises on each call.
    """
    return (
        importlib.resources.files("epics_mcp")
        .joinpath("operator_guide.md")
        .read_text(encoding="utf-8")
    )


def get_health() -> dict[str, object]:
    """Server health status, including what this process is permitted to write.

    ⚠️ ``write_enabled`` and ``write_pattern`` are the PV gate ALONE, and reading them as the whole
    write posture is the defect this payload was extended to remove: a server with the Olog gate
    armed reports ``write_enabled: false`` while it can create logbook entries.
    ``any_write_gate_armed`` is the question an approver is actually asking, so it is answered in
    one field.

    The gate blocks are projected field by field from :mod:`epics_mcp.write_posture`, never dumped
    wholesale. That report also carries the raw Olog URL and the search-reach violation strings,
    which name hosts and raw environment values verbatim; those belong in an operator's own terminal
    through ``epics-doctor``, not in a payload a client keeps. Projecting by hand is what makes a
    later field added to that report a deliberate disclosure rather than an automatic one.

    The posture blocks at the end answer what an approver asks after the gates: does this process
    verify TLS on its REST planes, does it throttle its REST reads, is the opt-in file boundary
    set, and how much does the ChannelFinder redaction let through. Each is a boolean or a count,
    and each is named for what it MEASURES rather than for the question that brings a reader to
    it: the same rule that gave ``write_pattern_is_a_known_allow_all_spelling`` its length. Two of
    them compute something rather than mirroring a setting, and the comment beside each says which
    precedence it resolves, because a payload carries no prose to qualify itself with.

    ⚠️ Where the withheld half lives differs per field, and saying "epics-doctor" for all of them
    would be wrong: that report prints the ChannelFinder allowlist ENTRIES, but it does not print
    the CA-bundle path or the allowed roots at all. Those two are in the server's environment and
    on no surface, which is a thing to say plainly rather than to point at the wrong command for.
    """
    cfg = get_config()
    p4p_version = "unknown"
    try:
        import p4p

        p4p_version = p4p.__version__
    except (ImportError, AttributeError):
        pass

    # Both are pure configuration and touch no file, which is why they are safe in a synchronous
    # resource handler; the doctor's composition of them is not, see write_posture's docstring.
    pv_gate = pv_write_gate_report(cfg)
    olog_gate = olog_write_gate_report(cfg)
    search = pv_search_posture(os.environ)

    return {
        # The registered key, the same answer serverInfo gives (see server.py's FastMCP call),
        # NOT the distribution name: one process must not name itself two ways.
        "server": "epics-pv",
        "version": __version__,
        "status": "ok",
        "provider": cfg.provider,
        # True iff EITHER gate is armed. Derivable, and here anyway: the reported defect IS that an
        # approver derived it from write_enabled and got it wrong. Armed says the gate is armed,
        # never that a write would succeed.
        "any_write_gate_armed": pv_gate.armed or olog_gate.armed,
        "write_enabled": cfg.allow_pv_write,
        # null, not a placeholder string. The placeholder claimed a state the server refuses
        # to start in: an armed gate with an empty pattern raises SafetyConfigError, so
        # write_enabled true beside no pattern cannot exist. It was also ambiguous, since
        # nothing distinguished it from a pattern whose text happens to be that word.
        "write_pattern": cfg.pv_write_pattern or None,
        "write_rate_limit": cfg.write_rate_limit,
        # ⚠️ The NAME says spelling, not semantics, and that is the whole point. It is decided by
        # comparing the pattern against a closed set of allow-everything spellings, never by
        # reading the expression, because deciding whether an arbitrary regex matches every name
        # is not reliably doable. So true means certainly wide, while FALSE DOES NOT MEAN NARROW:
        # measured, '.*|', '(?s).*', '|.*' and '^$|.*' each admit every PV under the gate's own
        # fullmatch and none of them is in the set. The CLI prints that caveat beside the flag;
        # here it has to live in the field name, because a payload carries no prose.
        "write_pattern_is_a_known_allow_all_spelling": pv_gate.pattern_allows_every_name,
        # The Olog gate, which had NO representation here at all. Its URL is deliberately absent for
        # the reason the olog_enabled comment below gives; the two predicates say what an approver
        # needs from it, and target_allowed is ALSO true for an allowlisted remote https target, so
        # the loopback bit is separate rather than folded in.
        "olog_write": {
            "armed": olog_gate.armed,
            "logbooks": olog_gate.logbooks,
            "rate_limit_per_minute": olog_gate.rate_limit_per_minute,
            "target_allowed": olog_gate.target_allowed,
            "target_is_loopback": olog_gate.target_is_loopback,
        },
        "uptime_seconds": round(time.monotonic() - _start_time, 1),
        "python_version": sys.version.split()[0],
        "p4p_version": p4p_version,
        "channelfinder_enabled": bool(cfg.channelfinder_url),
        "archiver_enabled": bool(cfg.archiver_url),
        # ⚠️ NOT bool(archiver_retrieval_url), and the naive spelling would be wrong in BOTH
        # directions. An empty retrieval URL falls back to the mgmt one, because a single-JVM
        # appliance serves both webapps on one port, so the naive field would report false for a
        # deployment whose history retrieval works. And a retrieval URL WITHOUT a mgmt URL is a
        # config_error the doctor reports, because every archiver tool gates on the mgmt one, so
        # the naive field would report true for a plane that is never used. Both cases reduce to
        # the mgmt URL, which is why this reads the same as archiver_enabled: the two planes are
        # one service, and only their probes differ.
        "archiver_retrieval_enabled": bool(cfg.archiver_url),
        "alarm_enabled": bool(cfg.alarm_url),
        # The naming plane, absent until now. A boolean and never the URL: unlike the other REST
        # planes it has no built-in default host, so its URL is the most identifying single value
        # in the configuration.
        "naming_enabled": bool(cfg.naming_url),
        # How far a PV search from this process can travel, said without naming an address. The
        # REST planes follow their URL variables above; the live plane follows this, and its
        # default is the one that is easy to miss, because an unset AUTO_ADDR_LIST means the
        # broadcast is ON. Which subnet stays with epics-doctor and the operator's own terminal.
        "pv_search": {
            "auto_addr_broadcast": search.auto_addr_broadcast,
            "search_lists_set": search.search_lists_set,
            "loopback_only": search.loopback_only,
        },
        # olog as an enabled-boolean only (never the URL, an ESS host, name-capable plane).
        "olog_enabled": bool(cfg.olog_url),
        # ⚠️ The REST planes' TLS posture, with the precedence COMPUTED rather than mirrored.
        # ca_bundle wins over tls_verify wherever a session is built (services/_http.py, three
        # factories, each spelling verify = cfg.ca_bundle or cfg.tls_verify), so a server
        # configured with tls_verify false AND a bundle path does verify. A field mirroring
        # cfg.tls_verify alone would report that deployment as unverified: a false alarm, the
        # mirror image of an over-claim and just as wrong.
        # verification_enabled is TRUE BY DEFAULT and says nothing about whether there is a
        # certificate to verify, which on a plain-http deployment makes it read like an all-clear
        # it does not mean. That is answerable from the configured URLs without naming one, so it
        # is answered beside it rather than folded in: two settings, two fields, each true to
        # itself. What stays unanswered, because the payload cannot prove it: when
        # ca_bundle_configured is false, WHICH trust store is in force. The READ sessions keep
        # trust_env on at the plain default, so a REQUESTS_CA_BUNDLE in the environment can still
        # replace it there, and that variable is not ours to report (the Olog write session never
        # honours it, a difference this field does not carry either).
        "rest_tls": {
            "verification_enabled": bool(cfg.ca_bundle) or cfg.tls_verify,
            "ca_bundle_configured": bool(cfg.ca_bundle),
            "https_plane_configured": any(
                url.startswith("https://")
                for url in (
                    cfg.channelfinder_url,
                    cfg.archiver_url,
                    cfg.archiver_retrieval_url,
                    cfg.alarm_url,
                    cfg.naming_url,
                    cfg.olog_url,
                )
            ),
        },
        # ⚠️ rest_, because the throttle is consulted on the REST GET paths only: the shared
        # rest_get_json / rest_get_bytes chokepoint, plus the Naming lookup, which asks it itself
        # before its own GET. A p4p PV read and a monitor run past it, and so do the per-plane
        # HEAD liveness probes. Without that prefix the field would read as an all-clear for
        # exactly the reads that load an IOC.
        # A block rather than a bare number, because 0 is the DISABLED default and a lone
        # "read_rate_limit: 0" reads as "no reads permitted", which is its opposite. Its sibling
        # write_rate_limit stays a bare number: that one has no off state (ge=1).
        "rest_read_rate_limit": {
            "enabled": cfg.read_rate_limit > 0,
            "per_minute": cfg.read_rate_limit,
        },
        # The opt-in file boundary as a boolean, NEVER the root list: those are filesystem paths,
        # the class this payload withholds. Decided by the same predicate the boundary itself asks
        # (paths.path_boundary_configured), because bool(cfg.allowed_roots) is true for ";" and
        # for "   ", neither of which holds a single file argument to anything.
        # ⚠️ The NAME says the variable is SET, in the spelling-not-semantics sense the write
        # pattern field above uses: a root of "." satisfies it, and how WIDE a configured boundary
        # is cannot be answered without naming the roots.
        "allowed_roots_set": path_boundary_configured(cfg.allowed_roots),
        # The ChannelFinder redaction, counted through the SAME resolvers the client redacts with
        # (services/channelfinder_client), so the two cannot disagree about the ALLOWLIST. What a
        # query returns is the intersection of that list with the channel, and a channel's NAME and
        # TAGS are outside both counters because they are not gated at all.
        # ⚠️ An allowlist is the set of what is DISCLOSED, so these counters run OPPOSITE
        # to privacy: zero is the most private posture (every owner and property redacted), not a
        # broken one. They are named for that, because a "safe_*_count" under a "privacy" heading
        # invites the reading that more is safer. The entries themselves stay out: an owner is a
        # service account name and both lists come from the site's own environment verbatim, so
        # which entries they are stays with epics-doctor.
        "channelfinder_redaction": {
            "disclosed_owner_account_count": len(resolve_safe_owner_accounts(cfg)),
            "disclosed_property_name_count": len(resolve_safe_property_names(cfg)),
            # Whether the built-in default posture still holds, which a count alone cannot say:
            # unset means the default, and an explicitly empty value means redact everything.
            "owner_allowlist_site_configured": cfg.channelfinder_safe_owner_accounts is not None,
            "property_allowlist_site_configured": cfg.channelfinder_safe_property_names is not None,
        },
    }


def _service_url(configured: str) -> str | None:
    """One service URL as this payload may show it: no userinfo, otherwise unchanged.

    Three states, and they are three different answers rather than shades of one. The string
    ``"(disabled)"`` means the plane is not configured at all. A URL means it is configured and is
    printed CHARACTER FOR CHARACTER apart from a userinfo that was removed, which is what makes it
    comparable with the block in a client's configuration file, the use ``docs/deployment.md``
    sends a reader here for. ``None`` means it is configured and could not be shown without
    risking a credential, see :func:`~epics_mcp.services._http.url_without_userinfo`; the value
    then stays with the operator's own environment.

    Why a redaction at all: these fields may carry ``https://user:password@host/path``, the config
    model does not validate them, and a resource payload is kept by the client, so a password
    written into one of them would land in a conversation transcript.
    """
    return url_without_userinfo(configured) if configured else "(disabled)"


def get_epics_config() -> dict[str, object]:
    """Non-secret configuration values, with any userinfo removed from the three service URLs.

    "Non-secret" is a property of the KEYS chosen here, and it used to be an incomplete claim: the
    three URL fields are the only ones that disclose a host, and a host is exactly where a
    credential is written when someone spells a service as ``https://user:password@host/path``.
    They go through :func:`_service_url`, which removes a userinfo and otherwise changes nothing.
    """
    cfg = get_config()
    return {
        "provider": cfg.provider,
        "default_timeout": cfg.default_timeout,
        "max_batch_size": cfg.max_batch_size,
        "max_monitor_duration": cfg.max_monitor_duration,
        "max_monitor_events": cfg.max_monitor_events,
        "allow_pv_write": cfg.allow_pv_write,
        # null rather than a placeholder, see the same field in get_health.
        "pv_write_pattern": cfg.pv_write_pattern or None,
        "write_rate_limit": cfg.write_rate_limit,
        "channelfinder_url": _service_url(cfg.channelfinder_url),
        "archiver_url": _service_url(cfg.archiver_url),
        "alarm_url": _service_url(cfg.alarm_url),
    }
