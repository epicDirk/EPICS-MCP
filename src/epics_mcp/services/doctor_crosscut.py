"""Patterns in a doctor report that NO single plane can see (QA-96).

``epics-doctor`` probes each plane on its own and reports each on its own. Three failure shapes
only become visible in the COMPARISON of several results, and an operator sitting in front of a
broken installation is asking about exactly those: not "is plane X healthy" but "what is going on
here".

A pure function of ``(config, planes)``. No network, no client, no clock, nothing to catch. That
is why it is a module of its own rather than a section of ``services/doctor.py``: every gatherer
there is TOTAL by contract (it catches its own errors and returns a ``PlaneCheck``, never raises),
and mixing two correctness disciplines in one file is how the next reader applies the wrong one.
``write_posture.py`` was carved out of the same file for a comparable reason.

⚠️ NAMING. "cross-plane" is already taken in this package and means something else entirely:
``services/crossplane.py`` and the ``crossplane_check`` tool join a PV across Display, IOC and the
Naming Service. Nothing here is called ``crossplane``; the report field is ``installation``, after
the question this module answers.

⛔ WHAT THIS DOES NOT DO. It never moves ``ok``, the verdict category or the exit code. Every
status it triggers on is already a failure (exit 1) or already inconclusive (exit 3), so a finding
is always the EXPLANATION of a verdict the report has already reached, never a new one.
⚠️ That is not the same as "a finding cannot appear beside ``ok: true``". It can, and legally:
``identity_probe_failed`` leaves ``ok`` True while driving exit 3, and it is deliberately a
trigger, because the origin story of this whole check (a dead container whose neighbour answered
401 for every path) produces exactly that status. A guard asserting the stronger claim would be
asserting something false.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

from epics_mcp.services._http import is_https_url, url_host

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps the runtime edge one-way
    from epics_mcp.config import EpicsConfig
    from epics_mcp.services.doctor import PlaneCheck, PlaneStatus


class _Model(BaseModel):
    """Frozen, closed value object (deterministic; unknown fields rejected).

    A second declaration rather than an import from ``services/doctor.py``, for the reason
    ``write_posture.py`` states about the same two lines: importing it would create exactly the
    dependency this module exists not to have.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


#: The statuses a pattern may key on: a hard failure, or the inconclusive one.
#:
#: ⚠️ ``identity_probe_failed`` belongs here even though it leaves ``ok`` True. The S4 shape this
#: server was hardened against, a host whose fronting proxy answers for every path while the
#: service behind it is dead, produces that status and nothing stronger. A trigger set of hard
#: failures alone would miss the constellation this module exists for.
_TRIGGERS: frozenset[PlaneStatus] = frozenset(
    {"api_error", "unreachable", "ca_error", "identity_probe_failed"}
)

#: The statuses deliberately OUTSIDE every pattern, so the two sets tile ``PlaneStatus`` exactly.
#: Declared rather than derived, so a new status is red until someone decides which side it is on
#: (``test_the_crosscut_sorts_every_plane_status``). ``config_error`` is here because it is a
#: finding about the configuration with no probe behind it, ``disconnected`` because it is the live
#: plane, which has no URL and therefore no host to group by.
_NEVER_TRIGGERS: frozenset[PlaneStatus] = frozenset(
    {
        "ok",
        "disabled",
        "info",
        "unverified",
        "no_ingest",
        "config_error",
        "backend_down",
        "disconnected",
    }
)


class InstallationFinding(_Model):
    """One pattern that matched, with what to change and how strongly it is known."""

    pattern: Literal["archiver_url_pair", "host_down", "trust_root", "ca_bundle"]
    #: ``signature`` = matched on statuses and configuration alone, so it is a HYPOTHESIS that
    #: other causes can produce. ``measured`` = a probe confirmed it. Machine-readable on purpose:
    #: it is the evidence discipline of this repository expressed as a field rather than as prose,
    #: and it keeps a future confirming probe an additive change instead of a rewrite.
    evidence: Literal["signature", "measured"]
    #: The report plane names involved, in the spelling ``planes[].plane`` uses.
    planes: list[str]
    #: The ``EPICS_MCP_*`` variables to edit. Planes are not actionable; variables are.
    variables: list[str]
    #: The host this concerns, empty when the pattern is not host-scoped.
    host: str
    #: Observation plus what to check. No positional wording ("above", "below"): this is rendered
    #: into lines of very different lengths, so a direction is a promise the layout does not keep.
    detail: str


class InstallationReport(_Model):
    """Everything the comparison of the planes says. Empty on a healthy or singly-broken run."""

    findings: list[InstallationFinding]


def _authority(url: str) -> tuple[str, str] | None:
    """``(host, host-with-port)`` of *url*, or None when it cannot be read.

    Fail-closed through :func:`url_host`, which parses with the library that actually connects.
    A URL this cannot read is excluded from every host-scoped pattern rather than grouped under a
    guessed key: an unparseable URL is not evidence that two planes share a host.
    """
    host = url_host(url)
    if not host:
        return None
    # The port is part of the authority, and that is what keeps a single-JVM appliance (two planes
    # on ONE address) from ever looking like several dead services on one host.
    tail = url.split("//", 1)[-1]
    authority = tail.split("/", 1)[0].rsplit("@", 1)[-1]
    return host, authority.lower()


def _plane_urls(cfg: EpicsConfig) -> dict[str, str]:
    """Which configured URL each REST plane was probed at.

    ``archiver_retrieval`` mirrors the CLIENT's own resolution (``retrieval_url or archiver_url``),
    which is the one seam that must not be re-derived by eye: a single-JVM appliance leaves the
    retrieval variable empty and is served on the mgmt URL, and a map that missed that would report
    a plane against a URL nobody probed.
    """
    urls = {
        "channelfinder": cfg.channelfinder_url,
        "archiver": cfg.archiver_url,
        "archiver_retrieval": cfg.archiver_retrieval_url or cfg.archiver_url,
        "alarm": cfg.alarm_url,
        "naming": cfg.naming_url,
        "olog": cfg.olog_url,
    }
    return {plane: url for plane, url in urls.items() if url}


#: Which variable an operator edits per plane. ``archiver_retrieval`` is decided by the caller,
#: because which of the two variables it read depends on the configuration.
_PLANE_VARS = {
    "channelfinder": "EPICS_MCP_CHANNELFINDER_URL",
    "archiver": "EPICS_MCP_ARCHIVER_URL",
    "alarm": "EPICS_MCP_ALARM_URL",
    "naming": "EPICS_MCP_NAMING_URL",
    "olog": "EPICS_MCP_OLOG_URL",
}


def _var_for(plane: str, cfg: EpicsConfig) -> str:
    if plane == "archiver_retrieval":
        return (
            "EPICS_MCP_ARCHIVER_RETRIEVAL_URL"
            if cfg.archiver_retrieval_url
            else "EPICS_MCP_ARCHIVER_URL"
        )
    return _PLANE_VARS.get(plane, "")


def _archiver_pair(cfg: EpicsConfig, failing: dict[str, PlaneStatus]) -> InstallationFinding | None:
    """Both archiver webapps erroring while pointing at DIFFERENT URLs.

    An Archiver Appliance serves mgmt and retrieval as separate webapps, usually on separate
    ports, and each is read from a variable of its own. Exchange the two values and both identity
    probes hit a route the other webapp does not serve.

    ⛔ REQUIRES BOTH VARIABLES SET AND THE TWO URLS DIFFERENT, which is what makes the single-JVM
    deployment structurally unable to trigger it: there the retrieval variable is empty and both
    planes resolve to the same URL, so a swap is neither possible nor harmful.

    ⚠️ It stays a SIGNATURE. A gateway answering 5xx for both webapps, or two URLs that are each
    one path segment too deep, produce the same evidence, and nothing here separates them. The
    detail says so and names the CHECK rather than the change: told to swap two values on this
    evidence alone, an operator whose real problem was the gateway would turn a correct
    configuration into a wrong one, quietly, because the retrieval fallback keeps answering.
    """
    both = {"archiver", "archiver_retrieval"} <= failing.keys()
    if not (both and cfg.archiver_url and cfg.archiver_retrieval_url):
        return None
    if cfg.archiver_url == cfg.archiver_retrieval_url:
        return None
    return InstallationFinding(
        pattern="archiver_url_pair",
        evidence="signature",
        planes=["archiver", "archiver_retrieval"],
        variables=["EPICS_MCP_ARCHIVER_URL", "EPICS_MCP_ARCHIVER_RETRIEVAL_URL"],
        host="",
        detail=(
            "Both archiver webapps answered with an error while pointing at different URLs. "
            "Check that each variable names the webapp it is for: mgmt serves /mgmt/bpl and "
            "retrieval serves /retrieval/bpl, and a pair of values exchanged between the two "
            "variables produces exactly this. So does a gateway erroring for both, and so do two "
            "URLs that are each one path segment too deep. Nothing here has told those apart, so "
            "verify a route before you change a value."
        ),
    )


def _host_down(
    cfg: EpicsConfig, failing: dict[str, PlaneStatus], configured: dict[str, str]
) -> list[InstallationFinding]:
    """Every plane on ONE host failing: one host gone, not N broken services.

    ⛔ ONLY ``unreachable`` counts here, and that is a correction a post-build review paid for.
    The sentence this finding prints is "none on it answered", so the only evidence that earns it
    is a plane that genuinely did not answer. The other three triggers all fail that test:
    ``api_error`` and ``identity_probe_failed`` mean the host DID answer (the per-plane line for
    the latter literally reads "transport reachable"), and a ``ca_error`` from an unreadable
    bundle is raised before a socket is opened at all, so it says nothing about any host.
    Measured, keying on all four produced two contradictions of the report's own plane lines: a
    host that answered every request was reported as one where nothing answered, and an
    unreadable ``EPICS_MCP_CA_BUNDLE`` made the block name an innocent host it had never
    contacted, complete with the reassurance that the rest of the deployment was fine.

    ⛔ SUPPRESSED as well when EVERY configured plane on EVERY host is failing. That shape is
    measured in this repository twice and BOTH times the cause was on the caller's side, not on
    any host: an unreadable CA bundle, and an ``HTTP_PROXY``. Printing one confident "this host is
    dead" finding per host would then name several innocent hosts and, worse, print them INSTEAD
    of the one true statement, which is that nothing left this machine at all.

    ⚠️ Needs at least two distinct AUTHORITIES on the host, not two planes. A single-JVM appliance
    puts two planes on one address, and a path-based reverse proxy several more; those are one
    service, and calling them "several dead services" would be a false alarm. Conservative on
    purpose: this block exists to remove false all-clears, not to add false alarms.
    """
    # "None on it answered" is only earned by planes that did not answer. See the docstring: the
    # other three triggers all describe a host that DID answer, or a failure raised before any
    # socket existed.
    silent = {plane for plane, status in failing.items() if status == "unreachable"}
    if not silent:
        return []
    # The caller-side shape: nothing that was configured survived.
    if len(failing) >= len(configured) and configured:
        return []
    by_host: dict[str, set[str]] = defaultdict(set)
    planes_by_host: dict[str, list[str]] = defaultdict(list)
    healthy_hosts: set[str] = set()
    for plane, url in configured.items():
        parsed = _authority(url)
        if parsed is None:
            continue  # unparseable: not evidence of a shared host, so it joins nothing
        host, authority = parsed
        if plane in silent:
            by_host[host].add(authority)
            planes_by_host[host].append(plane)
        elif plane not in failing:
            healthy_hosts.add(host)
        # A plane that failed in some OTHER way answered, so it neither supports the finding nor
        # counts as a healthy neighbour: it is evidence about a service, not about a host.
    findings: list[InstallationFinding] = []
    for host, authorities in sorted(by_host.items()):
        if len(authorities) < 2 or host in healthy_hosts:
            continue
        planes = sorted(planes_by_host[host])
        others = sorted(healthy_hosts)
        elsewhere = (
            f"Other configured hosts answered ({', '.join(others)}), so this is one host rather "
            "than your whole deployment."
            if others
            else (
                "No other host is configured, so this is your whole deployment being unreachable "
                "from here rather than one host of several. A proxy, a resolver or a VPN on THIS "
                "machine produces the same picture."
            )
        )
        findings.append(
            InstallationFinding(
                pattern="host_down",
                evidence="signature",
                planes=planes,
                variables=sorted({_var_for(plane, cfg) for plane in planes} - {""}),
                host=host,
                detail=(
                    f"{len(authorities)} services on {host} failed and none on it answered, so "
                    f"check the host before the services. {elsewhere}"
                ),
            )
        )
    return findings


def _trust_root(
    cfg: EpicsConfig, planes: list[PlaneCheck], configured: dict[str, str]
) -> InstallationFinding | None:
    """A TLS failure on some HTTPS planes but not all, or on all of them.

    Two findings from one comparison, and they point at opposite fixes: some of the HTTPS planes
    failing means the trust problem is at those hosts, all of them failing means it is your bundle.

    ⚠️ The population is decided by the URL SCHEME, never by ``ca_ok``. ``ca_ok`` is True on every
    successful path including plain ``http``, so it means "no TLS failure happened", not "TLS was
    verified". Reading it as the latter would count an http plane as a healthy HTTPS one and fire
    this finding on a comparison that never happened.

    ⚠️ Needs at least two HTTPS planes. With one there is nothing to compare, and the two causes
    are indistinguishable; the finding is withheld rather than guessed. Withheld too when no other
    HTTPS plane completed a handshake, because "the others are fine" would then be a claim about
    handshakes that never ran.

    ⛔ It does NOT say "foreign trust root". ``ca_error`` is raised for any SSL failure, which
    includes an expired certificate, a hostname mismatch and an interception proxy. Naming a cause
    would send the operator to replace ``EPICS_MCP_CA_BUNDLE``, and that path becomes the WHOLE
    trust store: a wrong diagnosis there breaks the planes that were working.

    ⛔ BLIND with ``EPICS_MCP_TLS_VERIFY=false``: no ``ca_error`` is raised at all, so a foreign
    trust root is invisible to this comparison. Recorded in ``docs/known-limits.md``.
    """
    https = {plane for plane, url in configured.items() if is_https_url(url)}
    if len(https) < 2:
        return None
    by_name = {plane.plane: plane for plane in planes}
    failed = sorted(p for p in https if by_name[p].status == "ca_error")
    if not failed:
        return None
    verified = sorted(p for p in https if by_name[p].ca_ok is True)
    if len(failed) == len(https):
        return InstallationFinding(
            pattern="ca_bundle",
            evidence="signature",
            planes=failed,
            variables=["EPICS_MCP_CA_BUNDLE"],
            host="",
            detail=(
                f"Every configured HTTPS plane failed TLS ({', '.join(failed)}), so check the "
                "trust material this process uses before any single host: a bundle that cannot be "
                "read, or one that trusts none of them, fails them all at once."
            ),
        )
    if not verified:
        return None  # nothing completed a handshake, so there is no "the others are fine"
    return InstallationFinding(
        pattern="trust_root",
        evidence="signature",
        planes=failed,
        variables=["EPICS_MCP_CA_BUNDLE"],
        host="",
        detail=(
            f"TLS failed for {', '.join(failed)} while it succeeded for {', '.join(verified)}, so "
            "the trust material is not wholly wrong and these hosts are what differs. Check the "
            "certificate these hosts present before you touch the trust store: a missing "
            "root, an expired certificate and a hostname mismatch all look like this. If you "
            "point EPICS_MCP_CA_BUNDLE at a new PEM, combine your internal roots WITH the public "
            "ones: that path becomes the whole trust store, and a bundle holding only the "
            "internal roots would break the planes that just succeeded."
        ),
    )


def installation_findings(cfg: EpicsConfig, planes: list[PlaneCheck]) -> InstallationReport:
    """Everything the comparison of *planes* says, given the configuration they came from.

    Pure and total: no I/O, and no input produces an exception. An empty report is the normal
    case, and the render prints nothing at all for it.
    """
    configured = _plane_urls(cfg)
    failing = {
        plane.plane: plane.status
        for plane in planes
        if plane.status in _TRIGGERS and plane.plane in configured
    }
    findings: list[InstallationFinding] = []
    pair = _archiver_pair(cfg, failing)
    if pair:
        findings.append(pair)
    findings.extend(_host_down(cfg, failing, configured))
    tls = _trust_root(cfg, planes, configured)
    if tls:
        findings.append(tls)
    return InstallationReport(findings=findings)
