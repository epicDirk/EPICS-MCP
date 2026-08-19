"""Read-only config self-check ("doctor"), is this deployment wired up correctly? (E2)

``run_doctor`` probes every CONFIGURED plane read-only, a transport probe, refined on success by
an identity probe, so a healthy plane answers up to TWO requests (THREE on the archiver, whose
identified appliance is also asked whether it is actually ingesting), and reports whether it is
reachable, whether the CA bundle works, whether the service **identifies itself as the service we
configured**, what the ChannelFinder privacy redaction is set to, and what the two write gates
would allow and where. It is the ``flutter doctor`` of this server: a new user in a fresh facility
runs ``epics-doctor`` and gets an immediate "is my config right?" without asking us.

Design (mirrors :mod:`epics_mcp.services.diagnose`):

* One :func:`asyncio.gather` fans out all planes; each gatherer is TOTAL (catches its own errors →
  a :class:`PlaneCheck`, never raises), so one dead plane cannot abort the report.
* An empty service URL means the plane is DISABLED, no client is built and no network call is
  made (the empty-URL-disables discipline). A disabled plane is not a failure. ⚠️ ONE variable is
  outside that rule and the rule used to be stated here as universal: an empty
  ``EPICS_MCP_ARCHIVER_RETRIEVAL_URL`` does NOT disable the retrieval plane, it falls back to the
  mgmt URL and is probed, because a single-JVM appliance legitimately leaves it empty (see
  :func:`_check_retrieval_plane`, which spells out why treating it as "off" would report a live
  endpoint as disabled).
* Reachability is proven by the client's ``check_connectivity`` probe. Its failure is classified
  into THREE buckets, not two, so a *reachable but wrong-endpoint* Archiver (a served non-2xx, e.g.
  ``EPICS_MCP_ARCHIVER_URL`` pointing at the retrieval webapp) is reported ``api_error``
  (reachable), NOT the misleading ``unreachable``, the CA/HTTP-status cause predicates in
  ``_http`` tell them apart.
* **Reachable is not identified.** ``check_connectivity`` is a HEAD for CF/Alarm/Olog/Naming and
  counts ANY HTTP response as reachable, by design, it is a transport probe. That made ``ok`` mean
  only "the probe did not raise": measured, a ChannelFinder URL pointing at a DEAD container
  reported ``✓ channelfinder ok`` because a different service on that port answered 401 (its blanket
  auth answers 401 for any path, so the status carried no information about CF at all). So each
  REST plane is refined by an IDENTITY probe, see :func:`_identify`. What a plane cannot prove is
  ``unverified``, never ``ok``.
* The live/PVA plane has no URL (only ``provider`` + the EPICS address-list env). By default it is
  an INFO line (no pass/fail); ``--probe-pv NAME`` turns it into a real connectivity pass/fail and
  is the ONLY path that makes a live p4p call (no default egress).
* The privacy report resolves the ChannelFinder allowlists through the SAME ``resolve_safe_*``
  helpers the client uses, so what doctor reports and what the client redacts cannot drift.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import stat
from typing import Literal

from pydantic import BaseModel, ConfigDict

from epics_mcp.config import EpicsConfig, get_config
from epics_mcp.epics_address import (
    DEFAULT_PORT_VARS,
    auto_addr_search_disabled,
    effective_default_port,
    effective_search_entry,
    is_ip_literal,
    split_host,
)
from epics_mcp.errors import EpicsError
from epics_mcp.services._http import (
    build_retrying_session,
    get_read_throttle,
    http_status,
    is_ca_bundle_error,
    is_read_throttle_error,
    is_retry_error,
    is_ssl_error,
    rest_get_json,
    shown_cause,
)
from epics_mcp.services.alarm_client import AlarmClient
from epics_mcp.services.archiver_client import ArchiverClient
from epics_mcp.services.channelfinder_client import (
    ChannelFinderClient,
    resolve_safe_owner_accounts,
    resolve_safe_property_names,
)
from epics_mcp.services.doctor_crosscut import InstallationReport, installation_findings
from epics_mcp.services.epics_client import effective_provider, pv_get
from epics_mcp.services.naming_client import NamingServiceClient
from epics_mcp.services.naming_identity import NAMING_SWAGGER_PATH, NAMING_SWAGGER_TITLE
from epics_mcp.services.olog_client import OlogClient
from epics_mcp.services.rest_exceptions import RestConnectionError, RestResponseError
from epics_mcp.write_posture import (
    OlogWriteGateReport,
    PvWriteGateReport,
    olog_write_gate_report,
    pv_write_gate_report,
)

#: Every status a plane can carry. A ``Literal`` rather than a bare ``str`` on purpose: the exit
#: verdict below is computed from status ALLOWLISTS (the three frozensets), so a typo in a status
#: string is a type error at the boundary, not a silent pass.
PlaneStatus = Literal[
    "ok",
    "disabled",
    "info",
    "unverified",
    "no_ingest",
    "identity_probe_failed",
    "throttled",
    "config_error",
    "ca_error",
    "api_error",
    "unreachable",
    "disconnected",
    "backend_down",
]

#: Statuses that are honestly clean → exit 0. An ALLOWLIST, not a failure denylist: with a denylist
#: an unforeseen or mistyped status silently lands on "not failing" and yields exit 0, fail-OPEN,
#: in the one tool whose job is to notice a misconfiguration. ``ok`` counts a plane clean iff its
#: status is in an allowlist (this set OR ``_INCONCLUSIVE_STATUSES``); anything in neither fails
#: (fail-closed), so the cost of forgetting to classify a new status is a false alarm rather than a
#: false all-clear.
#:
#: ``unverified`` is deliberately here: that a healthy service answers its info endpoint ANONYMOUSLY
#: is measured at exactly one site (n=1), and turning that into a hard failure for every site would
#: be the same overclaim this server keeps finding in other people's code. The same holds for a 2xx
#: whose body is not the JSON we can name it by (e.g. a 200 HTML login page), and when the beacon
#: carries a DIFFERENT known service's name (S14, measured 2026-07-16): a path-based reverse proxy
#: served the real ChannelFinder API while the base GET answered as Olog, a foreign name cannot
#: prove a misconfiguration. All three ANSWERED 2xx; none is a failure. A probe that actually FAILED
#: (a served non-2xx, a transport error, a refused redirect) is NOT here, it is
#: ``identity_probe_failed``, which the exit code notices. It is all reported honestly, and
#: ``DoctorReport.verification_complete`` tells a machine reader identity was not established.
#:
#: ``no_ingest`` is here by an explicit product decision, not by default: an archiver appliance
#: that is reachable, named itself, and is archiving NOTHING is a real finding, but it is not a
#: broken configuration. A site that has just stood an appliance up, or paused every channel,
#: is in exactly this state legitimately, and a hard failure would make ``epics-doctor`` cry wolf
#: in every CI job that runs it. It is reported honestly instead: its own glyph, its own verdict
#: line, and its own report field (``degraded_planes``), so nothing about it is silent, while the
#: exit code stays 0 for every existing caller. Contrast ``backend_down``, which IS a failure: an
#: alarm logger with a dead Elasticsearch cannot serve its tools at all.
_NON_FAILING_STATUSES: frozenset[str] = frozenset(
    {"ok", "disabled", "info", "unverified", "no_ingest"}
)

#: The plane never got a usable answer, and neither "failed" nor "fine" would be true of it. Not a
#: hard failure, so it never claims "plane failed" (exit 1); not a silent all-clear either, so it
#: drives its own inconclusive exit (3) and never renders "OK".
#:
#: ``identity_probe_failed`` is the S4 origin story (a URL at a dead container whose neighbour
#: answered 401), which used to collapse to a silent exit 0 via ``unverified``: reachable, but the
#: identity probe itself FAILED, a served non-2xx (401/404/5xx), a transport error, or a refused
#: redirect on the identity endpoint, as opposed to ``unverified``, where the endpoint ANSWERED 2xx
#: and we merely could not name it. A 401 on an INFO endpoint does not prove the plane's TOOL
#: endpoints are broken.
#:
#: ``throttled`` (BG-DTHR) reaches the same exit from the opposite direction: the probe never went
#: out at all, because THIS command's own read throttle refused it. Nothing was measured, so every
#: statement the other statuses make about a service would be a claim about something that was
#: never contacted. It sits here rather than in ``_NON_FAILING_STATUSES`` because a run that could
#: not ask is not a run that got a clean answer, and rather than in ``_FAILING_STATUSES`` because
#: no plane failed: the refusal was issued by this process about its own budget. It carries its own
#: report list and its own verdict sentence, see :data:`_THROTTLED_STATUSES`.
_INCONCLUSIVE_STATUSES: frozenset[str] = frozenset({"identity_probe_failed", "throttled"})

#: The inconclusive statuses whose cause is THIS command's own read throttle rather than anything
#: about the service. A strict SUBSET of :data:`_INCONCLUSIVE_STATUSES`, the same shape
#: :data:`_DEGRADED_STATUSES` has inside ``_NON_FAILING_STATUSES``, and for a reason of the same
#: kind: the exit CLASS is shared, the SENTENCE is not.
#:
#: Measured, that distinction is not cosmetic. ``run_doctor`` used to derive
#: ``inconclusive_identity_planes`` from ``_INCONCLUSIVE_STATUSES`` whole, and
#: ``cli_doctor._render`` builds the exit-3 headline out of that field with fixed wording ("N
#: identity probe(s) FAILED (reachable, but the identity endpoint did not return a usable
#: response)"). For a plane whose TRANSPORT probe was refused, all three of those claims are
#: false: no identity probe ran, nothing established that it was reachable, and no response was
#: involved. Splitting the list here is what lets that sentence stay true for the planes it was
#: written about.
#:
#: A set rather than an equality test, so a second "we never asked" cause is carried automatically.
_THROTTLED_STATUSES: frozenset[str] = frozenset({"throttled"})

#: Statuses that ARE a hard failure → exit 1. Listed explicitly (rather than "everything else")
#: only so ``test_status_partition_is_total_and_disjoint`` can prove the three sets tile
#: ``PlaneStatus`` exactly. The fail-closed guarantee still comes from ``ok`` being an allowlist of
#: the OTHER two sets (an unclassified status is in neither, so it is not clean and not inconclusive
#: → it fails), never from this denylist.
#:
#: ``backend_down`` is here (MA-2b(e)): the plane's transport is reachable AND it named itself, but
#: a backend it depends on is measurably down (the alarm logger reporting its Elasticsearch as not
#: ``"Connected"``). That is a real failure, the plane's tools will not work, which the blind
#: HEAD probe used to hide as ``ok``. Distinct from ``unverified`` (identity unproven, an honest
#: "don't know", exit 0): here identity IS proven and the service reports its OWN backend broken.
_FAILING_STATUSES: frozenset[str] = frozenset(
    {"config_error", "ca_error", "api_error", "unreachable", "disconnected", "backend_down"}
)

#: Not a failure (exit 0), but not healthy either: the plane answered, PROVED its identity, and is
#: measurably not doing its job. A strict subset of :data:`_NON_FAILING_STATUSES`, surfaced in its
#: own report field (``degraded_planes``) for one measured reason: every signal this tool's own
#: documentation tells scripts to read stays clean for such a plane. ``ok`` is True,
#: ``verification_complete`` is True, ``unverified_planes`` and ``inconclusive_identity_planes`` are
#: empty, and ``identified_planes`` even lists it, since its identity IS proven. Without a field of
#: its own a machine reader would have to walk ``planes[].status``, which no documentation asks for.
#: A set rather than an equality test so a future degraded status is carried automatically.
_DEGRADED_STATUSES: frozenset[str] = frozenset({"no_ingest"})

#: One remedy per status that reports a PROBLEM: what the operator has to CHANGE. Keyed by status
#: and STATIC, because the remedy follows from the status; everything that varies per call (the
#: HTTP code, the exception, the appliance figures, the variable a plane reads) belongs in
#: ``detail``, which each site writes itself. :func:`_with_remedy` appends this to that
#: observation and never replaces it: a reader needs both what was seen and what to do about it.
#: Every entry opens with an imperative from :data:`_REMEDY_IMPERATIVES`, which is what
#: ``test_every_problem_status_names_a_remedy`` can check; a table of empty strings would satisfy
#: a bare set comparison AND every containment assertion built on it, while changing the output by
#: not one character.
#:
#: Two statuses are deliberately absent, and neither omission is an oversight:
#:
#: * ``unverified`` CAN be a misconfiguration, and four sites say so themselves ("if the config
#:   IS wrong, the name here is the clue", "may not be an Archiver appliance MGMT endpoint", "may
#:   not be the retrieval webapp", "may not be the Naming Service"). Each already carries a remedy
#:   for the SPECIFIC thing it measured, which a status-wide sentence could only dilute. The
#:   exclusion is about precision, not about the state being harmless.
#: * ``no_ingest`` is not an ``EPICS_MCP_*`` problem at all: the appliance is reachable and named
#:   itself, and the wiring that is missing sits INSIDE it. This file describes that state in two
#:   voices, "not a broken configuration" at the status sets above and "a WIRING fault" at the
#:   ingest verdict below. Both are true of different things, and neither makes it a variable
#:   settable here.
#: ⚠️ No remedy refers to a POSITION ("named above", "printed above"). Measured on the rendered
#: output: an ``unreachable`` observation ends in a urllib3 exception several hundred characters
#: long, and the remedy is appended after it on the SAME line, so "above" pointed backwards across
#: all of that at a variable name the reader had long lost. Each remedy names what it means, or
#: describes it, and stands on its own.
_REMEDY: dict[str, str] = {
    "config_error": (
        "Set the variable named at the start of this finding; nothing was probed here, the "
        "configuration itself is the finding."
    ),
    "ca_error": (
        "Set EPICS_MCP_CA_BUNDLE to a PEM that trusts this host, combining your internal CA roots "
        "with the public ones when planes present different trust roots. See docs/deployment.md."
    ),
    # This entry used to end "for an Archiver Appliance the mgmt port and not retrieval", and a
    # status-keyed remedy is read by EVERY plane: on the retrieval plane, whose whole reason to
    # exist is the OTHER webapp, that named the webapp it had just probed as the right one. It now
    # states the QUESTION rather than answering it for one plane, which is what a shared remedy can
    # honestly do; which variable a plane reads is in its own observation.
    "api_error": (
        "Check the URL names the right service AND the right webapp for THIS plane: one product "
        "can serve several webapps on different ports, an Archiver Appliance serves mgmt and "
        "retrieval, and each of them is read from a variable of its own. A service that answers "
        "every attempt with a 5xx is up and erroring instead, and then the URL is right and the "
        "service is what needs looking at."
    ),
    "unreachable": (
        "Check that the host and port in the URL this plane reads are right, and that the service "
        "there is up and reachable from here. The variable to edit is named at the start of this "
        "finding, and all of them are listed per plane in docs/deployment.md."
    ),
    "disconnected": (
        "Check the PV name, that its IOC is running, and that the EPICS search path this finding "
        "reports can reach it."
    ),
    "backend_down": (
        "Repair the backend this finding names, or disable this plane. The configuration here is "
        "not the cause: the plane answered and proved its identity."
    ),
    # The third cause is the one this status was BUILT for (S4, see _INCONCLUSIVE_STATUSES above):
    # a URL at a dead container whose neighbour answered 401. Naming only the sub-path and the auth
    # wall sends exactly that operator to configure authentication on a host that is not theirs.
    "identity_probe_failed": (
        "Check the URL is the service ROOT rather than a sub-path, that it reaches the host you "
        "mean rather than a neighbour answering for a dead one, and that its info endpoint is "
        "not behind authentication. The tool endpoints of this plane may still work."
    ),
    # The only remedy here that names a variable of THIS command's environment rather than a
    # property of the deployment, because the finding is about this command's own budget: nothing
    # left the process, so there is nothing about the service to check yet.
    "throttled": (
        "Set EPICS_MCP_READ_RATE_LIMIT higher, or to 0 to switch the throttle off, and run again. "
        "This command's own read throttle refused the probe before it left, so nothing here "
        "describes the service. The operator guide sizes a limit for a WHOLE run rather than for "
        "one plane, which is the figure this needs."
    ),
}

#: The verbs a remedy may open with. A remedy has to tell the reader to DO something, and this is
#: the cheapest mechanical form of that: it goes red on an empty entry and on a description that
#: merely restates the finding ("The host is unreachable."), which is the failure mode a length
#: check misses.
_REMEDY_IMPERATIVES: frozenset[str] = frozenset({"Change", "Check", "Repair", "Set"})


def _with_remedy(status: PlaneStatus, detail: str) -> str:
    """Append the remedy for a PROBLEM *status* to *detail*; return *detail* unchanged when none.

    ONE seam, so the two halves of a problem report cannot drift apart. A status with no entry
    (``ok``, ``unverified``, ``no_ingest``, ``disabled``, ``info``) comes back byte-identical, which
    is what makes this safe to call from a site that handles several statuses at once, and what
    ``test_a_healthy_status_gets_no_remedy_appended`` pins.

    Joined with a space rather than a newline on purpose: ``cli_doctor._render`` prints ``detail``
    as one indented line, so a newline here would break that indentation for every problem report.
    """
    remedy = _REMEDY.get(status)
    return f"{detail} {remedy}" if remedy else detail


class _Model(BaseModel):
    """Frozen, closed value object (deterministic; unknown fields rejected)."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class PlaneCheck(_Model):
    """The outcome of probing one plane."""

    plane: str
    #: True iff configured (its URL is set / the live plane always counts as configured).
    configured: bool
    #: True/False when a probe ran; ``None`` when not probed (disabled or the info-only live plane).
    reachable: bool | None = None
    #: True iff transport + TLS succeeded; ``False`` only on a CA/TLS failure; ``None`` otherwise.
    ca_ok: bool | None = None
    #: See :data:`PlaneStatus` (``disconnected`` only for the live plane with ``--probe-pv``).
    status: PlaneStatus
    detail: str | None = None
    #: True when the service PROVED it is the one we configured (it named itself); False when the
    #: identity could not be established, which includes the case where the probe was never SENT
    #: because this command's own read throttle refused it (``throttled``); ``None`` when no
    #: identity probe APPLIES at all (disabled, the live/PVA plane, or a plane the transport probe
    #: never got past). ``False`` is NOT a failure:
    #: it is an honest "reachable, identity unverified" (see :data:`_NON_FAILING_STATUSES`).
    identified: bool | None = None


class PrivacyReport(_Model):
    """What the ChannelFinder redaction surfaces vs. drops (the effective, site-configured sets).

    Olog is deliberately absent: its reads return the whole entry since the read redaction was
    removed (decision PI, 2026-08-01), so there is no Olog posture left to report.
    """

    cf_safe_owner_accounts: list[str]
    cf_safe_property_names: list[str]


# PvWriteGateReport, OlogWriteGateReport and their builders moved to epics_mcp.write_posture
# (BG-DRES): the MCP resources describe the RUNNING server and are read synchronously, so
# they need the posture WITHOUT the audit probe below, which opens a file handle. They are
# re-exported through this module's imports, so every caller here is unchanged.


class AuditSinkReport(_Model):
    """Where the audit trail of BOTH gates goes, and whether it can be appended to.

    One object rather than a field per gate, because the sink genuinely IS shared: the two gates
    write to the same file through two loggers of their own (see ``server.main``).
    """

    #: ``EPICS_MCP_AUDIT_LOG_FILE`` verbatim; empty means stderr, which no restart survives.
    path: str
    #: The path the probe actually examined, which is ``path`` resolved against the CURRENT
    #: DIRECTORY exactly as ``logging.FileHandler`` resolves it. Equal to ``path`` for an absolute
    #: one. Reported separately because the two processes have different working directories: this
    #: command runs in an operator's shell, the server is started by an MCP client elsewhere, so a
    #: relative path names two different files and a verdict that did not say which one it checked
    #: would be unusable. Empty when no path is configured.
    resolved_path: str
    #: True/False when it could be decided, ``None`` when it could not. See
    #: :func:`_probe_audit_sink`, which also says what it cannot see and why. ⚠️ THREE states: a
    #: reader who treats this as a boolean turns "I could not tell" into "no".
    writable: bool | None
    #: Why. Never an empty string: an empty note would satisfy every containment assertion a test
    #: could make while telling the reader nothing.
    note: str | None = None


class WriteSafetyReport(_Model):
    """Can this server write anywhere, and if so, exactly where?

    INFORMATIVE: it changes neither ``ok``, nor the verdict category, nor the exit code. It is not
    a plane and carries no :data:`PlaneStatus`, deliberately, so it stays outside the status,
    glyph, plane-name and legend guards that exist for the per-plane half of the report.

    ⚠️ It describes the environment of THIS process. ``epics-doctor`` runs in its own process and
    reads ``os.environ``, while a running server was started by an MCP client from a different env
    block and built its gates from the config captured at ITS start. The render says so in its
    heading, because reading this block as a statement about the running server is the one mistake
    that turns it into a false all-clear.

    Nested rather than flat for three reasons, each measured rather than preferred: the two gates
    are not the same shape (a regex versus a name set plus a URL boundary), the audit sink is
    genuinely shared, and the flat configuration names would carry an old defect into a new wire
    contract (``write_rate_limit`` reads global and is PV-only).
    """

    pv: PvWriteGateReport
    olog: OlogWriteGateReport
    audit: AuditSinkReport


class DoctorReport(_Model):
    """The full self-check: every plane + the privacy posture + the write posture + a pass/fail."""

    planes: list[PlaneCheck]
    privacy: PrivacyReport
    #: What the two write gates would allow, and where. Informative: it never moves ``ok``, the
    #: verdict category or the exit code.
    write_safety: WriteSafetyReport
    #: Patterns visible only in the COMPARISON of several planes (QA-96). Empty on a healthy run
    #: and on most broken ones. Informative in the same sense as ``write_safety``: it never moves
    #: ``ok``, the verdict category or the exit code, because every status it keys on already
    #: drives one of them. ⚠️ A finding CAN stand beside ``ok: true``: ``identity_probe_failed``
    #: leaves ``ok`` True while driving exit 3, and it is deliberately a trigger.
    installation: InstallationReport
    #: True iff no configured plane HARD-FAILED (nothing in ``_FAILING_STATUSES``). Note what this
    #: does NOT say: a plane can be reachable with its identity ``unverified`` (still exit 0) OR its
    #: identity probe ``identity_probe_failed`` (exit 3) and still leave ``ok`` True, ``ok`` alone
    #: does not map to the exit code. Read ``inconclusive_identity_planes`` and
    #: ``throttled_planes`` (the two exit 3 drivers),
    #: ``degraded_planes`` (proven, reachable, not doing its job) and ``verification_complete``
    #: before treating this as "everything is confirmed".
    ok: bool
    #: True iff nothing about this run was left unestablished: no configured plane was left
    #: ``unverified``, none had its identity probe fail (``inconclusive_identity_planes`` empty),
    #: none was left unprobed by this command's own read throttle (``throttled_planes`` empty), and
    #: no read was refused at all (``reads_denied`` zero). The last term is not implied by the
    #: third: a refusal can hit a SUB-probe whose plane stays healthy.
    #: ⚠️ This is NOT "every configured plane's identity was established": a HARD-failed plane
    #: (``unreachable`` / ``api_error`` / ``ca_error``) is
    #: never identity-probed, so it lands in ``ok`` (which goes False), NOT here, this flag can be
    #: True while a plane hard-failed (read ``ok`` / ``identified_planes`` for that). ``ok`` alone
    #: is not enough for a machine reader either: an unverified/inconclusive plane is honest, not
    #: healthy, and a CI job that only looks at ``ok`` would read "nothing hard-failed" as
    #: "everything is confirmed", exactly the conflation this whole check exists to remove.
    #: ⚠️ Vacuously True when nothing ran an identity probe at all (e.g. an empty config), a reader
    #: wanting POSITIVE confirmation asserts ``identified_planes`` is non-empty, not this flag.
    #: ⚠️ It says nothing about whether an identified plane WORKS: a plane in ``degraded_planes``
    #: proved its identity, so it leaves this flag True while being measurably broken at its job.
    verification_complete: bool
    #: The planes that answered, PROVED their identity, and are measurably NOT DOING THEIR JOB, e.g.
    #: an archiver appliance holding channels with none connected (empty when none). Honest, not a
    #: failure: ``ok`` stays True and the exit code stays 0, deliberately, because such a state is
    #: legitimate on a freshly commissioned site. ⚠️ No other field shows it. A degraded plane is
    #: NOT in ``unverified_planes`` (its identity is proven) and IS in ``identified_planes``, so
    #: neither of those substitutes for reading this one.
    degraded_planes: list[str]
    #: The planes THIS COMMAND never asked, because its own read throttle refused the probe
    #: (empty when none). Not a statement about those services: no request left this process, so
    #: nothing here is evidence about a deployment. They drive the inconclusive exit 3 alongside
    #: ``inconclusive_identity_planes`` and they are deliberately NOT in that list, because the
    #: verdict sentence built from it says "identity probe(s) FAILED (reachable, but the identity
    #: endpoint did not return a usable response)", and all three of those claims are false for a
    #: plane whose TRANSPORT probe was refused before any socket existed.
    #: ⚠️ A run can be ``ok`` True with this list non-empty: nothing failed, some of it was simply
    #: never measured. ``verification_complete`` is False whenever it is non-empty, which is the
    #: signal a script reads to tell "confirmed" from "not asked".
    throttled_planes: list[str]
    #: How many reads this run's own throttle REFUSED, counted at the chokepoint rather than
    #: derived from the plane statuses. It exists because a refusal is not always visible in the
    #: plane it belongs to, and that gap was the most dangerous state this whole check could reach:
    #: the archiver spends a third token on an ingest SUB-probe whose failure maps to "ingest not
    #: measured" while the plane itself stays ``ok``, so at a limit one below what a full run needs
    #: the report came back with every plane healthy, every list empty, ``verification_complete``
    #: True and exit 0, under the strongest sentence this tool can print. No per-plane status can
    #: carry that, because nothing about that plane is wrong. Non-zero closes
    #: ``verification_complete`` and drives the inconclusive exit 3.
    #: ⚠️ It is >= the length of ``throttled_planes`` and never a second spelling of it: a plane
    #: there accounts for one refusal, and a refusal counted here may belong to no plane at all.
    reads_denied: int
    #: The planes that ANSWERED 2xx but could not prove their identity, anonymous, an unreadable
    #: body, or a foreign name (empty when none). Honest, not a failure → exit 0.
    unverified_planes: list[str]
    #: The planes whose identity probe FAILED (a served non-2xx / transport error / refused
    #: redirect), reachable but suspect, distinct from ``unverified`` (empty when none). Drives the
    #: inconclusive exit 3. A machine reader reads this ALONGSIDE ``unverified_planes``: a failed
    #: probe lands HERE, not in ``unverified_planes``.
    inconclusive_identity_planes: list[str]
    #: The planes that PROVED their identity, the positive counterpart to ``unverified_planes``.
    #: Empty on an empty config, which is how a machine reader tells "everything was confirmed"
    #: from "nothing ran at all" (``verification_complete`` is vacuously True in both).
    #: ⚠️ "Identified" is not "healthy": a plane in ``degraded_planes`` is listed HERE too, because
    #: it did name itself. Reading this list alone would count a non-archiving archiver as
    #: positively confirmed.
    identified_planes: list[str]


def _classify_failure(
    exc: Exception, url_var: str
) -> tuple[bool | None, bool | None, PlaneStatus, str]:
    """Map a failed connectivity probe to ``(reachable, ca_ok, status, detail)``.

    FIVE buckets (this said "three" for as long as the retry bucket existed, and "four" until the
    throttle bucket was added; a remedy guard parametrized over statuses rather than over these
    returns would have inherited either miscount, since two of them are ``api_error``). The first
    is keyed off the exception ITSELF, the rest off the chained cause:

    * THIS command's own read throttle refusing the probe (:func:`is_read_throttle_error`) →
      ``throttled`` (reachable None, ca_ok None). It is tested FIRST because nothing else here can
      recognise it: the refusal is raised before a socket exists, chains nothing, and is not a
      ``RequestException``, so every predicate below answers False and it used to fall through to
      the catch-all. Measured, that reported a running service as ``unreachable`` (exit 1) with a
      remedy telling the operator to check a host and a port that were never contacted. Both
      answers are ``None`` rather than False on purpose: ``False`` would be a claim, and nothing
      was measured. See BG-DTHR;
    * a TLS/CA failure (:func:`is_ssl_error`), or the configured CA bundle being unreadable in the
      first place (:func:`is_ca_bundle_error`) → ``ca_error`` (reachable False, ca_ok False);
    * a *served* non-2xx (:func:`http_status` gives a code) → ``api_error``, the host answered, so
      transport + CA are fine (reachable True, ca_ok True), but the endpoint is wrong / erroring;
    * a retry-exhausted 5xx (:func:`is_retry_error`) → ``api_error`` as well, for the same reason;
    * anything else (a transport failure, no chained HTTP response) → ``unreachable``.

    *url_var* is the environment variable this plane reads its URL from, named in the observation so
    the reader knows WHICH variable to edit. It is threaded from the caller rather than looked
    up here: each ``_check_*`` already holds that literal once (it passes the same one to
    :func:`_disabled`), and a second table keyed by plane would be a copy free to drift from it.
    ``ca_error`` leaves it out deliberately: its remedy is about the CA bundle, not about this
    URL.
    """
    if is_read_throttle_error(exc):
        # Nothing left this process, so nothing here is evidence about the service. The observation
        # names the VARIABLE to change rather than the plane's URL variable, because the finding is
        # about this command's own budget; ``_REMEDY["throttled"]`` carries the instruction.
        return (
            None,
            None,
            "throttled",
            _with_remedy(
                "throttled",
                "not probed: this command's own read throttle refused the request before it left, "
                "so nothing was measured about this plane.",
            ),
        )
    if is_ca_bundle_error(exc):
        # Not a verification failure: the bundle itself could not be READ, so no handshake was ever
        # attempted. Same verdict on purpose, because the remedy is the same variable and this
        # fails every https plane at once, where "unreachable" would send the operator to check
        # six services that are fine. The cause names the variable, never the path.
        return (
            False,
            False,
            "ca_error",
            _with_remedy("ca_error", f"TLS/CA setup failed: {shown_cause(exc)}."),
        )
    if is_ssl_error(exc):
        return (False, False, "ca_error", _with_remedy("ca_error", "TLS/CA verification failed."))
    code = http_status(exc)
    if code is not None:
        return (
            True,
            True,
            "api_error",
            _with_remedy("api_error", f"reachable, but {url_var} returned HTTP {code}."),
        )
    if is_retry_error(exc):
        # A retry-exhausted 502/503/504: the host answered (repeatedly, with a 5xx), so it is
        # reachable-but-erroring, not unreachable. RetryError has no .response, so no exact code.
        return (
            True,
            True,
            "api_error",
            _with_remedy(
                "api_error",
                f"reachable, but {url_var} kept returning a retryable 5xx (502/503/504) until the "
                "retry budget was exhausted, so the service is up but erroring.",
            ),
        )
    return (
        False,
        None,
        "unreachable",
        _with_remedy(
            "unreachable", f"could not reach the service at {url_var}: {shown_cause(exc)}"
        ),
    )


#: The ``name`` each Phoebus-family service reports at its base URL, measured (they answer
#: ANONYMOUSLY with a JSON body, under ``content-type: text/plain``, so the body is parsed and the
#: content type deliberately ignored). The match is EXACT, not a substring: a substring would let a
#: service calling itself "Not Olog Service" pass as Olog.
_SERVICE_NAMES: dict[str, str] = {
    "channelfinder": "ChannelFinder Service",
    "olog": "Olog Service",
    "alarm": "Alarm logging Service",
}

#: The Naming Service's swagger beacon (title + path) is single-sourced in
#: :mod:`epics_mcp.services.naming_identity` (imported above), the ONE home shared with
#: ``naming_client``'s S13 definitive-negative gate, so the two identity surfaces cannot drift.

#: The product name the archiver's ``getVersion`` string STARTS with, up to a word boundary.
#: Anchored on BOTH sides of the name, for the same reason ``_SERVICE_NAMES`` above matches
#: exactly: a containment test let a service calling itself "Not Archiver Appliance" pass
#: (measured), and a bare ``startswith`` still let "Archiver ApplianceX" pass (caught by the
#: adversarial review of that fix). Only the release number AFTER the full product name is the
#: variable part (an upgrade is not a misconfiguration); measured live on two real deployments:
#: "Archiver Appliance Version 2.2.1".
_ARCHIVER_PRODUCT = "Archiver Appliance"


def _is_archiver_version_string(version: str) -> bool:
    """True iff *version* is the archiver product name, optionally followed by a release part."""
    return version == _ARCHIVER_PRODUCT or version.startswith(_ARCHIVER_PRODUCT + " ")


# There is deliberately no local redaction helper here any more, and the deletion is the point.
# This module carried a ``_safe`` that substituted ``scheme://***@`` into free text. It read like
# a guard and was not one, in two independent ways. It matched only ``user:password@`` and only up
# to the FIRST ``@``, so measured it left ``ter2`` standing in ``http://svc:hun@ter2@host/x``,
# passed ``http://loneuser@host/x`` through untouched, and left ``ss/w0rd`` in
# ``https://svc:p@ss/w0rd@host/x``. And a substitution cannot answer the question a printed value
# raises: ``***@`` is still an ``@``, so nothing about the result says it was ever proven to be an
# address. The cause texts here now go through :func:`~epics_mcp.services._http.shown_cause`, the
# same output-side barrier every REST client uses, which passes a proven-clean text verbatim and
# withholds anything carrying an ``@`` rather than rewriting it.


def _fetch_beacon(
    url: str, auth_header: str | None, timeout: float, *, retries: int = 3
) -> object | Exception:
    """GET *url* and return the parsed body, or the Exception that stopped us. Never raises.

    The one place every identity probe issues its request, so the redirect posture and the
    error-to-``unverified`` translation cannot drift between planes.

    ``allow_redirects=False`` because the RESPONDING host is the whole point: a redirect would let
    another host answer for the one we configured, which is exactly the confusion being ruled out.
    Note a caller only ever sees a 2xx body: ``rest_get_json`` raises on a non-2xx BEFORE parsing,
    so an auth wall or a 404 can never reach a payload check.

    ``retries`` is passed through to ``build_retrying_session`` so a caller can ask for its "one
    attempt, long timeout" shape (``retries=0``). It matters because urllib3 applies the timeout
    PER ATTEMPT: a retrying session's worst case is about 4x the timeout plus backoff, with no
    wall-clock deadline (measured on the archiver ingest probe: 23.3 s against a route that
    answers in 7.3 s). That is the wrong trade for a probe whose failure is mapped to a
    non-verdict anyway, so it buys nothing and only costs wall-clock. The default is unchanged,
    so every existing caller keeps the retrying session.
    """
    session = build_retrying_session(auth_header=auth_header, retries=retries)
    try:
        return rest_get_json(
            session,
            url,
            None,
            timeout,
            conn_exc=RestConnectionError,
            resp_exc=RestResponseError,
            allow_redirects=False,
        )
    except Exception as exc:  # noqa: BLE001 (TOTAL: any failure is an answer, never a raise)
        return exc


def _identify(plane: str, base_url: str, auth_header: str | None, timeout: float) -> PlaneCheck:
    """Ask a Phoebus-family service to name itself; map the answer to a verdict. TOTAL.

    FOUR outcomes: ``ok`` (it named itself correctly); ``unverified``, it ANSWERED 2xx but we
    could not name it: an unreadable/HTML body, a body without a usable ``name``, or a body naming a
    DIFFERENT known service (with that name in the detail); ``identity_probe_failed``, the probe
    itself FAILED (a served non-2xx like a 401 auth wall or a 404, a transport error, a refused
    redirect); or ``throttled``, the probe was never SENT because this command's own read throttle
    refused it. The last two are routed via :func:`_identity_fetch_failure`. A foreign name is
    deliberately NOT a failure (S14): the earlier ``wrong_service``+exit-1 verdict rested on
    "unambiguous at any site", refuted by measurement (2026-07-16), a path-based reverse proxy
    served the REAL ChannelFinder API while the base GET answered as Olog, so the hard
    failure flagged a WORKING configuration.
    ``unverified`` is honest (exit 0); ``identity_probe_failed`` is inconclusive (exit 3, never a
    silent all-clear), see :data:`_NON_FAILING_STATUSES` / :data:`_INCONCLUSIVE_STATUSES`.
    """
    payload = _fetch_beacon(base_url, auth_header, timeout)
    if isinstance(payload, Exception):
        return _identity_fetch_failure(plane, payload)
    verdict = _classify_phoebus_name(plane, payload)
    if verdict is not None:
        return verdict
    return PlaneCheck(
        plane=plane, configured=True, reachable=True, ca_ok=True, status="ok", identified=True
    )


def _classify_phoebus_name(plane: str, payload: object) -> PlaneCheck | None:
    """Classify a Phoebus-family beacon body by the ``name`` it reports, the shared identity core
    of :func:`_identify` and :func:`_identify_alarm`, so the S14 foreign-name handling cannot drift.

    Returns the verdict when the name is unusable or foreign (:func:`_unverified`, honest doubt,
    exit 0), or ``None`` when the name matches this plane EXACTLY. ``None`` means "this IS the
    service": the caller may then trust it and inspect further body fields (the alarm plane reads
    ``elastic.status`` from here). *payload* is an already-fetched body, never an Exception, the
    fetch failure is handled by the caller before this."""
    expected = _SERVICE_NAMES[plane]
    name = payload.get("name") if isinstance(payload, dict) else None
    if not isinstance(name, str) or not name.strip():
        return _unverified(
            plane, "transport reachable, but the response carries no service name to check"
        )
    if name == expected:
        return None
    # A KNOWN foreign name keeps its plane mapping in the detail, that is the actionable clue
    # when the config really is cross-wired (status stays unverified either way, S14).
    hint = next(
        (f" (the name of the {other} service)" for other, o in _SERVICE_NAMES.items() if name == o),
        "",
    )
    return _unverified(
        plane,
        f"transport reachable, but this URL answers as {name!r}{hint}, not {expected!r}, cannot "
        f"confirm it is the {plane} service. Not a failure: a path-based reverse proxy can "
        "serve the real API behind a base URL that names another service (measured); if the "
        "config IS wrong, the name here is the clue.",
    )


def _unverified(plane: str, detail: str) -> PlaneCheck:
    """Reachable, endpoint ANSWERED 2xx, identity not established. Honest, NOT ``ok``, and NOT a
    failure (exit 0). The sibling of :func:`_identity_probe_failed`, which is for a probe that never
    got a usable answer."""
    return PlaneCheck(
        plane=plane,
        configured=True,
        reachable=True,
        ca_ok=True,
        status="unverified",
        identified=False,
        detail=detail,
    )


def _identity_probe_failed(plane: str, detail: str) -> PlaneCheck:
    """Reachable, but the identity probe itself FAILED (a served non-2xx / transport error / refused
    redirect). Distinct from :func:`_unverified`: there the endpoint ANSWERED 2xx and we merely
    could not name it; here the identity request never got a usable answer. Not a hard failure
    (:data:`_INCONCLUSIVE_STATUSES`), reported as inconclusive (exit 3), never a silent
    all-clear."""
    return PlaneCheck(
        plane=plane,
        configured=True,
        reachable=True,
        ca_ok=True,
        status="identity_probe_failed",
        identified=False,
        # Appended INSIDE the constructor rather than at the five identity probes that route
        # through _identity_fetch_failure: every one of them reaches this status through here, so
        # there is no site left that could forget the remedy. (The five are that neighbour's call
        # sites, not this function's, which has one; the figure sat on the wrong function.)
        detail=_with_remedy("identity_probe_failed", detail),
    )


def _throttled(plane: str, detail: str) -> PlaneCheck:
    """The probe never went out: THIS command's own read throttle refused it (BG-DTHR).

    The sibling of :func:`_unverified` and :func:`_identity_probe_failed`, and the difference from
    both is that neither the endpoint nor the network was involved at all. Inconclusive (exit 3),
    never a hard failure and never a silent all-clear, see :data:`_THROTTLED_STATUSES`.

    ``reachable`` and ``ca_ok`` stay True here, unlike the transport-probe arm in
    :func:`_classify_failure`, which reports both as ``None``. That is not an inconsistency but the
    measurement: this function is only ever reached AFTER the transport probe succeeded, so
    transport and TLS were genuinely proven for this plane and dropping that to ``None`` would
    discard something the run actually established.

    ``identified`` is ``False``, not ``None``. ``None`` on that field means no identity probe
    APPLIES (a disabled plane, the live plane, or one the transport probe never got past); here one
    applies and simply never ran, which is what ``False`` says.

    The remedy is appended inside the constructor, for the reason given at
    :func:`_identity_probe_failed`.
    """
    return PlaneCheck(
        plane=plane,
        configured=True,
        reachable=True,
        ca_ok=True,
        status="throttled",
        identified=False,
        detail=_with_remedy("throttled", detail),
    )


def _backend_down(plane: str, detail: str) -> PlaneCheck:
    """Transport reachable AND the service named itself, but a backend it depends on is measurably
    DOWN: so the plane's tools will fail even though the endpoint answered. A hard failure
    (:data:`_FAILING_STATUSES`, exit 1). Distinct from :func:`_unverified` (identity unproven, an
    honest "don't know", exit 0): here identity IS established (``identified`` stays True) and the
    service reports its OWN backend broken. The specific reason travels in *detail*."""
    return PlaneCheck(
        plane=plane,
        configured=True,
        reachable=True,
        ca_ok=True,
        status="backend_down",
        identified=True,
        # Inside the constructor, for the reason given at _identity_probe_failed.
        detail=_with_remedy("backend_down", detail),
    )


def _beacon_reached_but_unreadable(exc: BaseException) -> bool:
    """True iff a failed identity fetch actually REACHED a 2xx response whose body was unreadable.

    :func:`~epics_mcp.services._http.rest_get_json` calls ``raise_for_status()`` BEFORE
    ``resp.json()``, so the only way a 2xx is reached and the call still raises is a body that is
    not JSON, a ``JSONDecodeError`` (a ``ValueError`` subclass). On ``requests>=2.27`` that is a
    ``requests`` ``JSONDecodeError``, wrapped by ``rest_get_json`` and read here as the
    ``__cause__``; on the older ``requests>=2.25`` floor it is the STDLIB ``json.JSONDecodeError``:
    a ``ValueError`` but NOT a ``RequestException``, so ``rest_get_json`` does not wrap it and it
    arrives raw (hence we check the exception ITSELF too). A served non-2xx chains an ``HTTPError``,
    a transport failure a ``ConnectionError``, a refused redirect chains nothing, none is a
    ``ValueError``. So this cleanly separates "answered 2xx, just not nameably" (honest
    ``unverified``, e.g. a 200 HTML login page) from "the probe FAILED" (``identity_probe_failed``).
    Null-safe."""
    return isinstance(exc, ValueError) or isinstance(getattr(exc, "__cause__", None), ValueError)


def _identity_fetch_failure(plane: str, exc: BaseException) -> PlaneCheck:
    """Map a FAILED identity fetch to a verdict, shared by every identity probe so the split cannot
    drift. THREE ways out, tested in this order: a refusal by this command's own read throttle is
    :func:`_throttled`, because no request was made and nothing about the endpoint is in evidence;
    a REACHED-but-unreadable 2xx (a body that is not JSON) is honest :func:`_unverified`; anything
    else, a served non-2xx, a transport error, a refused redirect, is
    :func:`_identity_probe_failed`."""
    if is_read_throttle_error(exc):
        # FIRST, for the reason spelled out at the same arm in _classify_failure: this refusal is
        # not a transport event at all, and every other branch here would describe it as one. It is
        # also the larger half of BG-DTHR by count: the four HEAD-probed planes spend no token on
        # transport, so under a tight limit it is their identity beacon that is refused, and the
        # plane line then read "the identity probe FAILED" with a remedy about auth walls and
        # sub-paths for a request that was never sent.
        return _throttled(
            plane,
            "transport reachable, but the identity probe was NOT SENT: this command's own read "
            "throttle refused it, so this plane's identity is unmeasured rather than in doubt.",
        )
    if _beacon_reached_but_unreadable(exc):
        return _unverified(
            plane,
            "transport reachable; the endpoint answered 2xx but its body was not readable JSON, so "
            f"its identity could not be checked: {shown_cause(exc)}",
        )
    return _identity_probe_failed(
        plane, f"transport reachable, but the identity probe FAILED: {shown_cause(exc)}"
    )


def _disabled(plane: str, env_var: str) -> PlaneCheck:
    """A plane whose URL is unset: honestly off, no client built, no network call, not a failure."""
    return PlaneCheck(
        plane=plane,
        configured=False,
        status="disabled",
        detail=f"disabled, set {env_var} to enable",
    )


async def _run_probe(
    plane: str, run: object, identify: object = None, *, url_var: str
) -> PlaneCheck:
    """Run a sync ``check_connectivity`` off the event loop; classify success/failure. TOTAL.

    On success the verdict is REFINED by *identify* when the plane has an identity probe. Without
    it, "ok" would mean only "the transport probe did not raise", which is how a URL pointing at a
    dead container earned a ✓ from a neighbouring service's 401. ``check_connectivity`` itself is
    left untouched: it is the shared transport probe (``lookup_device_name`` and
    ``diagnose_connection`` depend on its exact semantics), so identity is layered on here rather
    than pushed down into it.
    """
    try:
        await asyncio.to_thread(run)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001 (TOTAL: any failure → classified PlaneCheck, never raises)
        reachable, ca_ok, status, detail = _classify_failure(exc, url_var)
        return PlaneCheck(
            plane=plane,
            configured=True,
            reachable=reachable,
            ca_ok=ca_ok,
            status=status,
            detail=detail,
        )
    if identify is not None:
        # Sync HTTP, so off the event loop exactly like the probe above.
        refined: PlaneCheck = await asyncio.to_thread(identify)  # type: ignore[arg-type]
        return refined
    return PlaneCheck(plane=plane, configured=True, reachable=True, ca_ok=True, status="ok")


async def _check_channelfinder(cfg: EpicsConfig, timeout: float) -> PlaneCheck:
    # Bound once and used by BOTH exits (disabled here, named in the unreachable observation via
    # _run_probe), so a rename touches one line instead of two. Every other _check_* below shares
    # this shape except _check_retrieval_plane, whose variable depends on which URL was probed and
    # whose config_error message names both by hand.
    url_var = "EPICS_MCP_CHANNELFINDER_URL"
    if not cfg.channelfinder_url:
        return _disabled("channelfinder", url_var)

    def _run() -> None:
        ChannelFinderClient(
            cfg.channelfinder_url, timeout=timeout, auth_header=cfg.channelfinder_auth or None
        ).check_connectivity()

    def _id() -> PlaneCheck:
        return _identify(
            "channelfinder", cfg.channelfinder_url, cfg.channelfinder_auth or None, timeout
        )

    return await _run_probe("channelfinder", _run, _id, url_var=url_var)


#: The exact ``status`` an appliance reports when all three of its own webapps answered. Measured
#: in the appliance source (``ApplianceMetrics.java``): the field is set to this literal only when
#: engine AND etl AND retrieval replied, and to a ``"Stopped - <what>"`` string otherwise. The
#: match is EXACT for the same reason the service names are: anything that is not this sentinel is,
#: by construction, a failure string. Live n=17 (1 sandbox + 16 production rows): all ``"Working"``.
_ARCHIVER_WORKING = "Working"

#: Floor for the ingest probe's timeout. ``getApplianceMetrics`` is not a cheap read: the appliance
#: fans out to three internal requests PER cluster member, which measured 7.3 s against a 16-member
#: production cluster versus 0.03 s for ``getApplianceInfo``. At the plane's configured timeout
#: (5.0 s by default) a perfectly healthy cluster would always land in "not measured".
_ARCHIVER_METRICS_MIN_TIMEOUT = 15.0


def _archiver_verdict(identity: str, note: str, *, ingesting: bool) -> PlaneCheck:
    """The archiver plane's final verdict. Identity is proven either way, only ingest differs.

    ``identified`` stays True in both branches, the same call ``_backend_down`` makes: the appliance
    DID name itself, what is in question is whether it archives. The identity therefore stays in the
    detail line in both branches, because it is this plane's identifying evidence.
    """
    return PlaneCheck(
        plane="archiver",
        configured=True,
        reachable=True,
        ca_ok=True,
        status="ok" if ingesting else "no_ingest",
        identified=True,
        detail=f"appliance identity: {identity}; {note}",
    )


def _archiver_metrics_row(payload: object, identity: str) -> dict[str, object] | None:
    """Pick the ``getApplianceMetrics`` row belonging to the appliance we just identified.

    The body is a LIST, one row per cluster member, and a multi-member cluster is the PRODUCTION
    NORM rather than the exception (measured 2026-07-29: 16 rows against the configured production
    cluster, 1 against the local sandbox). Every row names itself in ``instance``, carrying exactly
    the ``identity`` that ``getApplianceInfo`` reported (verified on both, n=2).

    Matched by NAME, never by position, and that holds for a single-row body too: a lone row from a
    FOREIGN appliance would otherwise be read as ours, which is the same confusion the identity
    probe's ``allow_redirects=False`` rules out one layer up. No unambiguous match returns ``None``,
    which the caller maps to "not measured", never to a finding.
    """
    if not isinstance(payload, list):
        return None
    matches = [row for row in payload if isinstance(row, dict) and row.get("instance") == identity]
    return matches[0] if len(matches) == 1 else None


def _archiver_count(row: dict[str, object], field: str) -> int | None:
    """Read one COUNT field of a metrics row as an int; ``None`` when it is not readable.

    Every value in this payload is a string, but they are not all the same KIND of string: the
    count fields are plain digit runs (``pvCount="234527"``), while the rate fields are LOCALE
    formatted (``eventRate="15,431.54"``, measured on all 16 production rows; the appliance builds
    them with a grouping ``DecimalFormat``). Only counts are parsed here. Rates are quoted verbatim
    and never parsed, because ``float("15,431.54")`` raises and there is nothing to gain by it.
    """
    raw = row.get(field)
    if isinstance(raw, bool) or not isinstance(raw, (str, int)):
        return None
    try:
        return int(str(raw).strip())
    except ValueError:
        return None


def _archiver_figures(row: dict[str, object]) -> str:
    """The ingest figures of one metrics row, for the detail line.

    A missing field is named as ``absent`` rather than dropped: an ABSENT connection count is
    itself the engine-failure signal (the counts are produced by the engine webapp and merged in
    by mgmt, so a failed merge makes them vanish while ``pvCount`` survives), and a detail line
    that silently omitted them would hide exactly the state the operator needs to see.
    """
    parts = [
        f"{field}={value if value is not None else 'absent'}"
        for field, value in (
            (name, _archiver_count(row, name))
            for name in ("pvCount", "connectedPVCount", "disconnectedPVCount")
        )
    ]
    rate = row.get("eventRate")
    parts.append(f"eventRate={rate}" if isinstance(rate, str) else "eventRate=absent")
    return ", ".join(parts)


def _archiver_ingest_verdict(
    base_url: str, auth_header: str | None, timeout: float, identity: str
) -> PlaneCheck:
    """Ask the identified appliance whether it is actually INGESTING, and build the final verdict.

    Why this exists: ``getApplianceInfo`` proves that an Archiver appliance is answering, and
    nothing more. An appliance whose engine has never spoken to its IOCs answers it exactly like a
    healthy one, so ``epics-doctor`` printed ``ok`` for a deployment that was archiving nothing.
    That is a WIRING fault, the very class this tool exists to catch. Measured on the local sandbox
    (2026-07-29): identity ``appliance0`` while all five archived PVs carried
    ``lastEvent: "Never"``, ``connectionFirstEstablished: "Never"`` and ``eventRate: 0.0``.

    Two signals, either one is enough:

    * ``pvCount > 0`` with ``connectedPVCount == 0``, the appliance holds channels and has none
      connected. The threshold is exactly zero, not a ratio: on the production cluster
      ``disconnectedPVCount`` sits between 111 and 8939 in NORMAL operation (16/16 rows, all
      reporting ``"Working"``), so any percentage threshold would fire there permanently.
    * ``status`` present and not :data:`_ARCHIVER_WORKING`, the appliance's own report that one of
      its webapps is down. This is the worse fault and it is INVISIBLE to the counts: when the
      engine webapp fails to answer, mgmt cannot merge its numbers, so the connection counts vanish
      from the row entirely while ``pvCount`` survives, and the payload is still served as HTTP 200.
      Without this arm the guard could never fire in the worst state an archiver can be in.

    ``eventRate`` is deliberately NOT a condition, only context in the detail. It is too small a
    signal for slowly scanned PVs, and too large a one because the appliance accumulates it as a
    running mean that does not drop to zero when connections are lost.

    TOTAL, like the identity fetch before it: any failure to reach, read or match the metrics
    leaves the plane ``ok``. Two reasons. This REFINES a verdict that already stands, so an older
    appliance without this route must not fail a plane whose identity is proven. And on a real
    cluster this is the branch that actually fires, since the route fans out to three internal
    requests per member and can legitimately time out. What a failure does NOT do is pass silently:
    the reason travels in ``detail``, so "measured and ingesting" is never indistinguishable from
    "never measured".

    Scope, stated because a cluster invites the wrong reading: these figures describe the member
    named by *identity* ONLY. A cluster is retrieval-aware, so a query to one member returns data
    physically owned by another, but ingest is per member. The detail line says so.
    """
    payload = _fetch_beacon(
        f"{base_url}/mgmt/bpl/getApplianceMetrics",
        auth_header,
        max(timeout, _ARCHIVER_METRICS_MIN_TIMEOUT),
        retries=0,
    )
    if isinstance(payload, Exception):
        return _archiver_verdict(
            # ``payload`` is narrowed to Exception one line up: the name comes from the union
            # ``_fetch_beacon`` returns, not from a body ever reaching this branch.
            identity,
            f"ingest not measured: {shown_cause(payload)}",
            ingesting=True,
        )
    row = _archiver_metrics_row(payload, identity)
    if row is None:
        return _archiver_verdict(
            identity,
            "ingest not measured: getApplianceMetrics returned no unambiguous row for this "
            "appliance",
            ingesting=True,
        )

    figures = f"{_archiver_figures(row)} (this member only)"
    status = row.get("status")
    if isinstance(status, str) and status.strip() and status != _ARCHIVER_WORKING:
        return _archiver_verdict(
            identity,
            f"the appliance reports status={status.strip()!r}, so one of its own webapps is down "
            f"and it is not archiving: {figures}",
            ingesting=False,
        )

    held = _archiver_count(row, "pvCount")
    connected = _archiver_count(row, "connectedPVCount")
    if held is not None and connected is not None and held > 0 and connected == 0:
        return _archiver_verdict(
            identity,
            f"it holds {held} PV(s) and has none connected, so nothing is being archived: "
            f"{figures}",
            ingesting=False,
        )
    return _archiver_verdict(identity, f"ingesting: {figures}", ingesting=True)


def _identify_archiver(base_url: str, auth_header: str | None, timeout: float) -> PlaneCheck:
    """The appliance names itself in ``getApplianceInfo``, but only if we look at the body.

    ``ArchiverClient.check_connectivity`` already demands a 2xx with parseable JSON from
    ``/mgmt/bpl/getApplianceInfo`` (stronger than the HEAD planes), yet it DISCARDS the payload:
    an empty ``{}`` passes. The appliance's own ``identity`` field is what turns "something served
    JSON here" into "an Archiver appliance served it", so it is checked rather than assumed.

    Once the appliance IS named, :func:`_archiver_ingest_verdict` asks whether it is actually
    archiving, the way the alarm plane reads ``elastic.status`` out of its beacon. The difference
    from the alarm case, and the reason it costs something: that answer lives on a SECOND route,
    so a healthy archiver plane now answers three requests rather than two.

    The fetch goes through :func:`_fetch_beacon` like every other identity probe. This plane used
    to build its own session inline, which made that function's "the one place every identity
    probe issues its request" claim untrue; a second inline GET would have made it plainly false.
    """
    payload = _fetch_beacon(f"{base_url}/mgmt/bpl/getApplianceInfo", auth_header, timeout)
    if isinstance(payload, Exception):
        return _identity_fetch_failure("archiver", payload)

    identity = payload.get("identity") if isinstance(payload, dict) else None
    if not isinstance(identity, str) or not identity.strip():
        return _unverified(
            "archiver",
            "transport reachable, but getApplianceInfo carries no 'identity', this may not be an "
            "Archiver appliance MGMT endpoint",
        )
    return _archiver_ingest_verdict(base_url, auth_header, timeout, identity)


async def _check_archiver(cfg: EpicsConfig, timeout: float) -> PlaneCheck:
    url_var = "EPICS_MCP_ARCHIVER_URL"
    if not cfg.archiver_url:
        return _disabled("archiver", url_var)

    def _run() -> None:
        ArchiverClient(
            cfg.archiver_url, timeout=timeout, auth_header=cfg.archiver_auth or None
        ).check_connectivity()

    def _id() -> PlaneCheck:
        return _identify_archiver(cfg.archiver_url, cfg.archiver_auth or None, timeout)

    return await _run_probe("archiver", _run, _id, url_var=url_var)


def _identify_retrieval_plane(base_url: str, auth_header: str | None, timeout: float) -> PlaneCheck:
    """The retrieval webapp names itself in ``/retrieval/bpl/getVersion``.

    Note the base PATH: retrieval serves ``/retrieval/bpl``, not ``/mgmt/bpl``, probing the latter
    on the retrieval port 404s and says nothing. The version string STARTS with the product name,
    so the check is anchored there up to a word boundary (see ``_is_archiver_version_string``);
    only the release number after the full name is variable and must not be pinned (an appliance
    upgrade is not a misconfiguration).
    """
    payload = _fetch_beacon(
        f"{base_url.rstrip('/')}/retrieval/bpl/getVersion", auth_header, timeout
    )
    if isinstance(payload, Exception):
        return _identity_fetch_failure("archiver_retrieval", payload)

    version = payload.get("version") if isinstance(payload, dict) else None
    if isinstance(version, str) and _is_archiver_version_string(version):
        return PlaneCheck(
            plane="archiver_retrieval",
            configured=True,
            reachable=True,
            ca_ok=True,
            status="ok",
            identified=True,
            detail=f"retrieval webapp: {version}",
        )
    return _unverified(
        "archiver_retrieval",
        f"transport reachable, but getVersion reports {version!r} rather than an "
        f"{_ARCHIVER_PRODUCT}, this may not be the retrieval webapp",
    )


async def _check_retrieval_plane(cfg: EpicsConfig, timeout: float) -> PlaneCheck:
    """The archiver RETRIEVAL webapp, its own line, because nothing else probes it.

    ``get_pv_history`` talks to RETRIEVAL, while the archiver plane only ever probed MGMT: a dead
    retrieval endpoint read as ``archiver ok`` while every history call 404'd.

    The URL mirrors the CLIENT's own resolution (``retrieval_url or base_url``): a single-JVM
    appliance serves every webapp on one port and leaves ``EPICS_MCP_ARCHIVER_RETRIEVAL_URL``
    empty, so treating an empty var as "plane off" would report ``disabled`` for a retrieval
    endpoint that is very much live and being queried, the same false all-clear this check exists
    to remove, wearing the word "disabled".

    The INVERSE pair, retrieval URL set, archiver URL empty, is dead config, not a plane to
    probe: every archiver tool gates on ``EPICS_MCP_ARCHIVER_URL`` (tools/archiver.py,
    checkers.py), so nothing ever queries that retrieval URL. The first fallback fix probed it
    anyway and reported the STRONGEST all-clear the tool knows (``ok=True,
    verification_complete=True``) for a configuration none of the tools can use, a fix against
    false-green that produced false-green. It is a loud ``config_error`` now, without a network
    call: the config itself is the finding, and an ``ok`` line next to it would only muddy what
    the operator has to change.
    """
    # The variable the PROBED url actually came from, which is not always the mgmt one: the split
    # deployment docs/deployment.md documents (retrieval on its own Tomcat) sets
    # EPICS_MCP_ARCHIVER_RETRIEVAL_URL, and an unreachable probe there has to name THAT variable.
    # Naming the mgmt one unconditionally would send exactly the operator who followed the
    # split-port instructions to edit a URL that is not the one that failed.
    #
    # The disabled exit below keeps EPICS_MCP_ARCHIVER_URL deliberately: it is reached only when
    # BOTH are empty, and the mgmt one is what enables the plane at all (the retrieval var alone is
    # the config_error above, never a working plane).
    url_var = (
        "EPICS_MCP_ARCHIVER_RETRIEVAL_URL"
        if cfg.archiver_retrieval_url
        else "EPICS_MCP_ARCHIVER_URL"
    )
    if cfg.archiver_retrieval_url and not cfg.archiver_url:
        return PlaneCheck(
            plane="archiver_retrieval",
            configured=True,
            status="config_error",
            # Observation only, no imperative: the remedy is the table's half, and this used to say
            # "Set EPICS_MCP_ARCHIVER_URL" itself, so the rendered line carried the instruction
            # twice. What the table cannot know is WHICH variable, so the observation names it and
            # the remedy points at it, the construction the unreachable remedy already uses.
            #
            # The variable to SET therefore LEADS the observation, because that remedy reads "the
            # variable named at the start of this finding". The reference is positional, which is
            # what makes it checkable: an earlier version pointed at "the empty variable this
            # finding names", a claim about a PROPERTY that nothing re-ran, and a one-word edit
            # here made the sentence report two of them as empty while every test stayed green.
            # The position is pinned instead, for this status and for unreachable, by
            # test_the_first_variable_a_finding_names_is_the_one_to_edit; that this stays the sole
            # site producing the status is pinned by
            # test_config_error_has_exactly_one_construction_site.
            detail=_with_remedy(
                "config_error",
                "EPICS_MCP_ARCHIVER_URL (the MGMT webapp URL) is empty while "
                "EPICS_MCP_ARCHIVER_RETRIEVAL_URL is set, and every archiver tool gates on "
                "EPICS_MCP_ARCHIVER_URL, so this retrieval URL is never used.",
            ),
        )
    url = cfg.archiver_retrieval_url or cfg.archiver_url
    if not url:
        return _disabled("archiver_retrieval", url_var)

    def _run() -> None:
        # Probe RETRIEVAL's own endpoint, and through rest_get_json so the failure arrives with its
        # cause chained: that is what lets _classify_failure tell a CA problem from a wrong webapp
        # from a dead host. A raw session.head here would collapse all three into "unreachable".
        #
        # Deliberate redirect asymmetry (do NOT add allow_redirects=False here): this TRANSPORT
        # probe answers "is the webapp reachable", so it FOLLOWS redirects exactly as
        # get_pv_history's real ArchiverClient does (its _get omits allow_redirects → default True).
        # Refusing here would report api_error/exit-1 for an endpoint the tool actually reaches.
        # Origin integrity is the IDENTITY probe's job, _identify_retrieval_plane's _fetch_beacon
        # uses allow_redirects=False, so a redirecting endpoint surfaces as identity_probe_failed
        # (exit 3), not a false failure.
        session = build_retrying_session(auth_header=cfg.archiver_auth or None)
        rest_get_json(
            session,
            f"{url.rstrip('/')}/retrieval/bpl/getVersion",
            None,
            timeout,
            conn_exc=RestConnectionError,
            resp_exc=RestResponseError,
        )

    def _id() -> PlaneCheck:
        return _identify_retrieval_plane(url, cfg.archiver_auth or None, timeout)

    probed = await _run_probe("archiver_retrieval", _run, _id, url_var=url_var)
    if cfg.archiver_retrieval_url or probed.status == "ok" or not probed.reachable:
        return probed
    # The fallback finding used to open with EPICS_MCP_ARCHIVER_URL, and both remedies that promise
    # "the variable to edit is named at the start of this finding" therefore pointed at the variable
    # that had just earned a ✓ one line above: the MGMT webapp answered, only retrieval did not.
    # Following that advice breaks the working half and leaves the broken half broken, while the one
    # setting that helps was not named at all.
    #
    # So the empty variable LEADS and the observation keeps its own text behind it. Every clause is
    # something this function measured: the retrieval variable IS empty, the fallback DID happen,
    # and the URL that failed DID come from the mgmt variable, which stays named because dropping it
    # would trade one dishonest sentence for another.
    #
    # Wrapped around the _run_probe result rather than around the function's own return, which is
    # what keeps the two earlier exits out of it: `disabled` (both variables empty, so there was no
    # fallback to report) and `config_error` (the retrieval variable is SET there) both return above
    # this line. An `is not ok` test placed on the function's return would have covered `disabled`
    # too and claimed a fallback for a plane that never had a URL.
    #
    # `reachable` is the third condition, and the first version of this change did NOT have it,
    # which made the fix move the defect instead of removing it. When the host does not answer at
    # all, the mgmt plane fails on the line above, nothing has earned a ✓, and the address or the
    # service behind EPICS_MCP_ARCHIVER_URL is what has to be repaired: leading with an EMPTY
    # variable there is the same misdirection in a different configuration. Measured against a
    # closed port. It is `reachable` rather than a list of statuses because it IS the question,
    # "did this host produce a response": measured False for exactly the transport and TLS
    # failures, True for every state in which something answered and only the WEBAPP is in doubt.
    prefix = (
        "EPICS_MCP_ARCHIVER_RETRIEVAL_URL is empty, so this plane fell back to the MGMT URL in "
        "EPICS_MCP_ARCHIVER_URL and probed retrieval there:"
    )
    return probed.model_copy(update={"detail": f"{prefix} {probed.detail or ''}".rstrip()})


#: The exact ``elastic.status`` the Alarm Logger reports when its Elasticsearch is healthy (measured
#: from the Phoebus source ``SearchController.info()``: the field is set to this literal right after
#: a successful ``client.info()`` call). A dead ES yields a string starting ``"Failed to connect to
#: elastic "`` instead. The match is EXACT (an equality, not a prefix) for the same reason the
#: service names are: anything that is not this sentinel is, by construction, a failure string.
_ALARM_ELASTIC_CONNECTED = "Connected"


def _identify_alarm(base_url: str, auth_header: str | None, timeout: float) -> PlaneCheck:
    """Name the Alarm Logger AND check the Elasticsearch it depends on, from ONE ``GET /`` body.

    The identity gate is exactly the shared :func:`_classify_phoebus_name` (the alarm logger is a
    Phoebus-family service that names itself), so a fetch failure, an unnameable body, or a foreign
    name are handled identically to :func:`_identify`. What is layered on top: once the name
    confirms this IS the alarm logger, ``elastic.status`` is read from the SAME payload (no second
    request). The logger's search/history tools are backed by Elasticsearch, a healthy transport
    with a dead ES is a real, actionable failure that the blind HEAD (``check_connectivity``) used
    to hide as ``ok`` (this is MA-2b(e)). Only a status that is PRESENT and explicitly not
    :data:`_ALARM_ELASTIC_CONNECTED` yields :func:`_backend_down`; a missing or unreadable
    ``elastic.status`` falls back to ``ok``, we never claim a failure we cannot prove (the same
    withheld-≠-no discipline as every other identity path). ``GET /`` returns HTTP 200 even when ES
    is down, so the failure is body-only and this is the only place it can be seen."""
    payload = _fetch_beacon(base_url, auth_header, timeout)
    if isinstance(payload, Exception):
        return _identity_fetch_failure("alarm", payload)
    verdict = _classify_phoebus_name("alarm", payload)
    if verdict is not None:
        return verdict

    # Named correctly → now the added check: is the Elasticsearch it searches actually up?
    elastic = payload.get("elastic") if isinstance(payload, dict) else None
    status = elastic.get("status") if isinstance(elastic, dict) else None
    if isinstance(status, str) and status != _ALARM_ELASTIC_CONNECTED:
        return _backend_down(
            "alarm",
            "transport reachable and identified as the alarm logger, but its Elasticsearch backend "
            f"is not connected (elastic.status={status!r}), alarm search and history will fail",
        )
    return PlaneCheck(
        plane="alarm", configured=True, reachable=True, ca_ok=True, status="ok", identified=True
    )


async def _check_alarm(cfg: EpicsConfig, timeout: float) -> PlaneCheck:
    url_var = "EPICS_MCP_ALARM_URL"
    if not cfg.alarm_url:
        return _disabled("alarm", url_var)

    def _run() -> None:
        AlarmClient(
            cfg.alarm_url, timeout=timeout, auth_header=cfg.alarm_auth or None
        ).check_connectivity()

    def _id() -> PlaneCheck:
        return _identify_alarm(cfg.alarm_url, cfg.alarm_auth or None, timeout)

    return await _run_probe("alarm", _run, _id, url_var=url_var)


def _identify_naming(base_url: str, timeout: float) -> PlaneCheck:
    """The Naming Service names itself in its Swagger contract.

    It has no ``{"name": ...}`` beacon like the Phoebus trio, but ``/rest/swagger.json`` is a
    static, anonymous 200 whose ``info.title`` identifies the service, and it DISCRIMINATES
    (measured: Olog answers 401 there, ChannelFinder 404).

    The title is documentation prose and may be reworded by a future release, so a mismatch is
    ``unverified``: we can recognise this service, but an unfamiliar title proves nothing about
    what is answering (since S14 the same honesty applies to every identity probe, even a
    beacon naming a different known service only ever yields ``unverified`` with the name in
    the detail).
    """
    payload = _fetch_beacon(f"{base_url.rstrip('/')}{NAMING_SWAGGER_PATH}", None, timeout)
    if isinstance(payload, Exception):
        return _identity_fetch_failure("naming", payload)

    info = payload.get("info") if isinstance(payload, dict) else None
    title = info.get("title") if isinstance(info, dict) else None
    if title == NAMING_SWAGGER_TITLE:
        return PlaneCheck(
            plane="naming",
            configured=True,
            reachable=True,
            ca_ok=True,
            status="ok",
            identified=True,
        )
    return _unverified(
        "naming",
        f"transport reachable, but /rest/swagger.json reports info.title={title!r} rather than "
        f"{NAMING_SWAGGER_TITLE!r}, this may not be the Naming Service (or its title changed)",
    )


async def _check_naming(cfg: EpicsConfig, timeout: float) -> PlaneCheck:
    url_var = "EPICS_MCP_NAMING_URL"
    if not cfg.naming_url:
        return _disabled("naming", url_var)

    def _run() -> None:
        NamingServiceClient(base_url=cfg.naming_url, timeout=timeout).check_connectivity()

    def _id() -> PlaneCheck:
        return _identify_naming(cfg.naming_url, timeout)

    return await _run_probe("naming", _run, _id, url_var=url_var)


async def _check_olog(cfg: EpicsConfig, timeout: float) -> PlaneCheck:
    url_var = "EPICS_MCP_OLOG_URL"
    if not cfg.olog_url:
        return _disabled("olog", url_var)

    def _run() -> None:
        OlogClient(
            cfg.olog_url, timeout=timeout, auth_header=cfg.olog_auth or None
        ).check_connectivity()

    def _id() -> PlaneCheck:
        return _identify("olog", cfg.olog_url, cfg.olog_auth or None, timeout)

    return await _run_probe("olog", _run, _id, url_var=url_var)


# The EPICS client-search env vars, i.e. every way a PV search can leave this host. The list
# vars are enumerated individually (never or-folded, a fallback chain would mask all but the
# first); the *_AUTO_ADDR_LIST pair is handled separately because its UNSET state means ON.
_SEARCH_LIST_VARS = (
    "EPICS_PVA_ADDR_LIST",
    "EPICS_CA_ADDR_LIST",
    "EPICS_PVA_NAME_SERVERS",  # TCP unicast to named servers, NOT subnet-bound
    "EPICS_CA_NAME_SERVERS",
)


# _auto_addr_search_disabled moved to epics_mcp.epics_address (E8): the write-reach startup
# assert in safety.py needs the SAME parser-faithful semantics, so there is exactly one source.
# The posture tests in tests/test_doctor.py stay the behaviour pin for this call site.


#: The sentence appended once when a set search list belongs to a provider this client does not
#: speak. It has to say BOTH halves, and the second half is the load-bearing one: a reader who
#: meets "inert" alone, directly above a write block reporting that the very same variable stops
#: a write-enabled server from booting, concludes that the boot assert guards a dead variable.
#: The cheapest way to make that contradiction go away is to drop the idle provider from
#: ``epics_address.CLIENT_REACH_PROVIDERS``, which would delete half the write-reach guarantee.
#: So the disagreement is stated, and stated as deliberate, rather than left to be discovered.
#:
#: No trailing full stop, for the same reason the rest of this function has none: every caller in
#: ``_check_live`` continues the sentence itself (". Pass --probe-pv ...", "; <pv> connected."), so
#: a stop here renders as "them.. Pass". Measured on the first draft.
_INERT_NOTE = (
    "The [inert] variables above steer no PV search in THIS process, but the write-reach assert "
    "still counts them and a write-enabled server still refuses to start over them, deliberately: "
    "these variables are process-global and a differently built p4p would honour them"
)


#: Appended once when any search entry is a NAME rather than an IP literal.
#:
#: The endpoints above are computed from the configuration alone, which is exactly right for a
#: literal and one step short for a name: the client resolves it at startup and, measured, DROPS
#: the entry outright when resolution fails, with an ``ignoring invalid`` line on stderr that no
#: report ever sees. So the printed list is an upper bound in that case, and says so. No trailing
#: full stop, for the reason recorded at :data:`_INERT_NOTE`.
_NAME_NOTE = (
    "One or more entries above are NAMES rather than IP literals, and this client DROPS an entry "
    "it cannot resolve rather than failing, so its effective list can be SHORTER than this one"
)


#: Appended once when any search entry was written with NO HOST at all (``:5076``, ``[]:5077``).
#:
#: A SEPARATE note rather than a widened :data:`_NAME_NOTE`, and the reason is that the other one
#: would be false here in both halves: such a token is not a NAME, and "DROPS an entry it cannot
#: resolve" is not what happens on every platform. Measured on the same token: pvxs substitutes one
#: of this machine's own interface addresses on Windows, and makes no entry at all on Linux. So
#: ``epics_address.split_port`` claims nothing for the shape, and this line is where the operator
#: is told why and what to do instead. Naming the way out is the point: the previous rendering
#: stated an endpoint that was true on one platform only, and dropping it without a remedy would
#: leave a reader with less than before. No trailing full stop, see :data:`_INERT_NOTE`.
#:
#: ⚠️ The remedy names a CONDITION rather than promising an outcome, and a post-build review is why:
#: "write the host out" repairs ``:5076`` and ``[]:5077``, and does nothing for ``[]x`` or ``[],1``,
#: whose tails are outside the pinned corpus with or without a host. What the report can resolve is
#: the shape this states, so the sentence stays true for every token it is appended to.
_EMPTY_HOST_NOTE = (
    "One or more entries above were written with NO HOST (for example ':5076'). What a client "
    "makes of such an entry is decided by the platform resolver: measured on that token, one of "
    "this machine's own interface addresses on Windows, and a refused entry on Linux. Nothing is "
    "claimed for it above. What this report resolves is a host with an optional numeric port, so "
    "writing the host out (for example '127.0.0.1:5076') gets you a named destination"
)


def _inert_search_prefix(effective: str) -> str:
    """The ``EPICS_<family>_`` prefix whose search list variables this process ignores.

    Derived from the EFFECTIVE provider rather than the configured one, which is the whole point
    of the caller: with a configured provider this client cannot speak, the ignored family is the
    one the operator was configuring.
    """
    return "EPICS_CA_" if effective == "pva" else "EPICS_PVA_"


def _live_search_posture(effective: str) -> str:
    """The PV search-path posture of THIS process, read from the same env pvxs reads.

    Read from ``os.environ``, deliberately NOT from a p4p ``Context.conf()``: the doctor's
    no-probe path guarantees no default egress, and merely building a Context binds sockets
    and starts worker threads. The env interpretation is pinned to the pvxs sources instead:
    ``EPICS_PVA_NAME_SERVERS`` alone is a full search path (TCP unicast, not subnet-bound:
    pvxs ``src/client.cpp`` ``startNS()``), and ``autoAddrList`` DEFAULTS TO TRUE (pvxs
    ``pvxs/client.h``), so even a null environment broadcasts searches into the local
    subnets. ``localhost-isolated`` is therefore claimed ONLY when every search list is
    unset AND the auto-addr search is explicitly disabled, never as a default posture.

    *effective* is the provider this process will REALLY speak, not the configured one, and the
    difference is a measured bug rather than a nicety: p4p 4.x offers only ``pva`` yet accepts
    ``Context("ca")``, so a ``EPICS_MCP_PROVIDER=ca`` deployment that switched off
    ``EPICS_CA_AUTO_ADDR_LIST`` used to be told ``localhost-isolated`` while the pva context it
    actually built had its own switch unset, i.e. broadcasting. A false all-clear about network
    reach, printed by the one tool whose job is to state that reach.

    ONE half moved to the effective provider, and the other deliberately did NOT. The auto-addr
    switch is per provider, so it follows the effective one. The LIST enumeration stays
    provider-blind, every variable of both families, exactly as before: narrowing it would make a
    set-but-ignored list vanish from the report and let ``localhost-isolated`` take its place,
    which is a fix that removes the message instead of the problem. A list belonging to the other
    family is therefore printed and MARKED, never dropped (pinned by
    ``test_live_posture_names_every_set_search_path``).
    """
    paths: list[str] = []
    inert_prefix = _inert_search_prefix(effective)
    saw_inert = False
    saw_name = False
    saw_empty_host = False
    for var in _SEARCH_LIST_VARS:
        value = os.environ.get(var, "").strip()
        if value:
            inert = var.startswith(inert_prefix)
            saw_inert = saw_inert or inert
            port_var, fallback = DEFAULT_PORT_VARS[var]
            written = os.environ.get(port_var, "").strip()
            # Through the SAME arithmetic a port written in a token gets. Printing the variable's
            # value raw made the line state ports nobody dials (":70000" for an effective 4464,
            # and ":abc" as a live endpoint), which is the invented reach this whole line removes.
            default_port = effective_default_port(
                written, fallback, zero_keeps_fallback="ADDR_LIST" in var
            )
            tokens = value.split()
            rendered = {t: effective_search_entry(t, default_port) for t in tokens}
            # Only an entry we actually RESOLVED to an endpoint can carry the resolution caveat.
            # A token printed as written carries no claim to qualify, and one reported DROPPED is
            # already gone: calling either a "name whose entry might disappear" was a second small
            # untruth beside the first, and it fired on ``10.0.0.9:abc``.
            saw_name = saw_name or any(
                not is_ip_literal(t) and rendered[t] != t and "DROPPED" not in rendered[t]
                for t in tokens
            )
            # Read off the TOKEN, not off its rendering: a token written without a host is printed
            # as written, so it is indistinguishable from every other non-claim once rendered.
            # DROPPED is excluded for the same reason it is excluded above: that entry is already
            # gone, the port refusal decided it before any host was looked up, and telling the
            # operator a resolver might substitute an address for it would be false (measured on
            # "[]:abc", which the client keeps no entry for on either platform).
            # ⚠️ HONEST REACH: split_host fails closed, so a host-less token whose tail is not a
            # plain numeric port (":5076,255") is not detected here and gets no caveat. That shape
            # was equally uncovered before this note existed; it is a stated limit, not a change.
            saw_empty_host = saw_empty_host or any(
                not split_host(t) and "DROPPED" not in rendered[t] for t in tokens
            )
            resolved = " ".join(rendered[token] for token in tokens)
            if default_port is None:
                origin = (
                    f"{port_var}={written} is not a port this client can read, so it falls back to "
                    f"its own default and no entry here is resolved"
                )
            else:
                state = f"={written}" if written else " unset"
                origin = f"default port {default_port} from {port_var}{state}"
            paths.append(f"{var} ({resolved}; {origin}){' [inert]' if inert else ''}")
    auto_var = "EPICS_PVA_AUTO_ADDR_LIST" if effective == "pva" else "EPICS_CA_AUTO_ADDR_LIST"
    # Raw value, deliberately NOT normalised: neither parser trims, and normalising here
    # would make the doctor honour spellings the real client rejects (see the helper).
    auto_value = os.environ.get(auto_var, "")
    if not auto_addr_search_disabled(effective, auto_value):
        state = f"={auto_value}" if auto_value else " unset, default ON"
        paths.append(f"auto-addr subnet broadcast ({auto_var}{state})")
    if paths:
        line = "search paths: " + "; ".join(paths)
        if saw_name:
            line = f"{line}. {_NAME_NOTE}"
        if saw_empty_host:
            line = f"{line}. {_EMPTY_HOST_NOTE}"
        return f"{line}. {_INERT_NOTE}" if saw_inert else line
    return "localhost-isolated (no search list set, auto-addr search explicitly disabled)"


def _provider_clause(configured: str, effective: str) -> str:
    """``provider=<what was asked for>`` plus, when they differ, what is actually spoken.

    The clause rather than a bare name, because the bare name was itself part of the false
    all-clear: ``provider=ca`` reads as a statement that this process speaks Channel Access.
    """
    if configured == effective:
        return f"provider={configured}"
    return (
        f"provider={configured}, which the installed p4p does NOT offer (it has only "
        f"{effective}), so this process builds a {effective} context and every "
        f"EPICS_{configured.upper()}_* search variable is inert here"
    )


async def _check_live(cfg: EpicsConfig, probe_pv: str | None, timeout: float) -> PlaneCheck:
    """The live/PVA plane. INFO-only by default (no p4p call); a real pass/fail with ``probe_pv``.

    The live plane has no URL: its config is ``provider`` + the EPICS search-path env. Without a
    probe PV there is nothing to connect to, so this reports the posture (no default egress). Only
    ``probe_pv`` triggers a live read.

    ``base`` is built ONCE and interpolated into all three branches below, so the reach statement
    is identical whether the plane is info-only, connected or disconnected. That is deliberate:
    the disconnected branch is the one an operator reads while something is broken, and it is the
    branch whose remedy tells them to check "the EPICS search path this finding reports". A reach
    line that appeared only in the healthy branches would send exactly that reader to a sentence
    they cannot see.
    """
    effective = effective_provider(cfg.provider)
    base = f"{_provider_clause(cfg.provider, effective)}, {_live_search_posture(effective)}"
    if not probe_pv:
        return PlaneCheck(
            plane="live",
            configured=True,
            status="info",
            detail=f"{base}. Pass --probe-pv NAME to probe a live PV.",
        )
    connected, error_code = await _probe_live_pv(probe_pv, timeout)
    if connected:
        return PlaneCheck(
            plane="live",
            configured=True,
            reachable=True,
            status="ok",
            detail=f"{base}; {probe_pv} connected.",
        )
    return PlaneCheck(
        plane="live",
        configured=True,
        reachable=False,
        status="disconnected",
        detail=_with_remedy(
            "disconnected",
            f"{base}; {probe_pv} did NOT connect ({error_code or 'internal error'}).",
        ),
    )


async def _probe_live_pv(pv_name: str, timeout: float) -> tuple[bool, str | None]:
    """Return ``(connected, error_code)``. A disconnect is normal input → caught, never raised.

    Mirrors ``diagnose._probe_live``'s inverted exception handling: an ``EpicsError`` is a coded
    disconnect; any other exception is an internal probe failure reported as not-connected.
    """
    try:
        await pv_get(pv_name, timeout=timeout)
    except EpicsError as exc:
        return (False, exc.error_code)
    except Exception as exc:  # noqa: BLE001 (internal probe failure → not connected, keep doctor total)
        return (False, type(exc).__name__)
    return (True, None)


def _privacy_report(cfg: EpicsConfig) -> PrivacyReport:
    """The effective redaction posture, resolved through the SAME helpers the clients use."""
    return PrivacyReport(
        cf_safe_owner_accounts=sorted(resolve_safe_owner_accounts(cfg)),
        cf_safe_property_names=sorted(resolve_safe_property_names(cfg)),
    )


#: POSIX only. Absent on Windows (measured: ``hasattr(os, "O_NONBLOCK")`` is False on CPython 3.14
#: there), so this is 0 and the flag word is unchanged. On POSIX it keeps a readerless FIFO from
#: blocking the open forever, which matters because :func:`_probe_audit_sink` runs OUTSIDE the
#: ``asyncio.gather`` and no timeout would rescue a hang. Annotated because a bare ``getattr`` is
#: ``Any`` and ``--strict`` propagates that into the flag expression.
_O_NONBLOCK: int = getattr(os, "O_NONBLOCK", 0)

#: The spellings of "allow every PV name" this flag RECOGNISES, and the word is exact: this is a
#: comparison of strings, never a decision about a regex. Deciding universality in general cannot
#: be done reliably, and EXECUTING the pattern to find out is refused for its own reason: the
#: pattern can come from a configuration file somebody else authored, and this command is the one
#: an operator runs to review such a file, so a catastrophically backtracking pattern would hang
#: the review tool.
#:
#: The cost is stated rather than hidden, because two earlier versions of this set understated it.
#: The first knew ``^.*`` and not ``.*$``, which ``re.fullmatch`` (``safety.py``) treats
#: identically, so the loud hint stayed silent on an allow-everything gate written in the very
#: style ``safety.py``'s own error message teaches (``'^MPS:.*$'``). The second fixed that ANCHOR
#: ASYMMETRY for two families and left it standing in four more: measured, ``(.*)``, ``(?:.*)``,
#: ``.{0,}`` and ``[\s\S]*`` carried two of their four anchored spellings each and ``\A.*\Z`` one
#: of three, so the same defect the paragraph above narrates as repaired was still present four
#: times. Every family is complete below, and the completeness is a declared list in the tests
#: rather than a derivation from this set, which would be a tautology.
#:
#: What remains unrecognised is named rather than implied, because a hint that misses is worse than
#: no hint: an alternation containing an empty branch (``.*|``, ``|.*``, ``^$|.*``), an inline flag
#: (``(?s).*``), and anything else spelled outside these families. Two of those are cheap to write
#: by accident. The render therefore says WHAT it checked instead of calling the pattern narrow.


def _probe_audit_sink(configured_path: str) -> tuple[bool | None, str, str]:
    """Would the audit sink accept an append? Probed by opening a handle, never by creating one.

    This answers the question the REAL sink asks, which is not "is this file writable". Both gates
    build ``logging.FileHandler(path, encoding="utf-8")`` with the stdlib defaults, which resolves
    ``os.path.abspath`` and opens EAGERLY for append, CREATING the file when it is missing and NOT
    creating its parent. So the question is "can this be appended to, or created", and answering
    the neighbouring question is how the obvious probe gets it wrong.

    ``os.access`` is deliberately NOT used. Measured on Windows against the sink itself as ground
    truth, with both controls: it returns True for a file whose access control list denies writing
    and that a real append-open rejects with ``PermissionError``, True for a DIRECTORY, and True
    for a file whose image a running process has mapped. It reads the DOS read-only ATTRIBUTE, not
    the access control list, and on a directory that attribute means nothing at all.

    Opening a handle is read-only in the sense that matters, and that was measured rather than
    assumed: size, mtime, atime and ctime are identical before and after a successful probe, the
    content is unchanged, and the probe succeeds while a SECOND process holds the same file open
    through a real ``logging.FileHandler``, so a running server does not make its own sink look
    broken.

    What it cannot see, named rather than implied:

    * **A file that does not exist yet.** Nothing read-only on Windows predicts whether the create
      would succeed: ``os.access(parent, os.W_OK)`` and ``os.access(parent, os.W_OK | os.X_OK)``
      are both True for system directories whose access control list denies file creation, and
      ``os.supports_effective_ids`` is empty there. That branch returns ``None``, which means
      undecidable, not "no". The one half that IS decidable is the half the sink itself decides: a
      MISSING PARENT is a hard False, because the handler creates no directories.
    * **Anything past the open**: free space, a quota, a later permission change, and the encoder
      (a ``UnicodeEncodeError`` at write time is swallowed by ``logging`` and no open-time probe of
      any kind can see it). And the verdict describes the instant of the probe, nothing later.
    * **Which denial it was.** ``os.open`` goes through the C runtime on Windows, so only ``errno``
      is set and an access denial and a sharing violation arrive as the same one.
    * **A slow network path.** A sink on an unreachable network share can stall this call, and
      there is no timeout around it. The tempting repair, running it in a thread under
      ``asyncio.wait_for``, was probed and REJECTED as cosmetic: the wait would return while the
      worker thread stayed blocked, and the interpreter joins that thread at exit, so the command
      would hang anyway, one line later.

    Returns ``(verdict, note, resolved_path)`` and raises nothing, by construction. That last part
    is not decor: this runs outside the total ``asyncio.gather`` and ``cli_doctor`` catches only
    ``EpicsError``, so anything escaping here would leave the command as a bare traceback.
    """
    if not configured_path:
        return (
            None,
            "no path set, so the trail goes to stderr and does not survive a restart "
            "(EPICS_MCP_AUDIT_LOG_FILE)",
            "",
        )
    try:
        resolved = os.path.abspath(os.fspath(configured_path))
    except (OSError, ValueError, TypeError) as exc:
        # False rather than None: a measured hard failure is a finding, not an open question. The
        # sink does not survive these either, which is why both gates now catch the same three.
        return False, f"unusable path ({type(exc).__name__}: {exc})", configured_path

    # os.stat rather than os.path.exists, and that is not a style choice: the ``os.path``
    # predicates swallow ValueError and OSError and answer False, so an UNUSABLE path (a NUL byte,
    # a name the filesystem rejects) would arrive at the branch below wearing the shape of "not
    # there yet" and be reported as undecided. Measured: os.stat tells the three cases apart, a NUL
    # raising ValueError, a missing file FileNotFoundError, a rejected name OSError errno 22.
    try:
        mode = os.stat(resolved).st_mode
    except FileNotFoundError:
        parent = os.path.dirname(resolved)
        if not parent or parent == resolved:
            # A drive root, a UNC root or a device name: ``dirname`` is a fixed point there, and
            # the old wording read "the parent directory X does not exist" naming X itself.
            return False, f"{resolved} has no parent directory to be created in", resolved
        if not os.path.isdir(parent):
            return (
                False,
                f"the parent directory {parent} does not exist, and the sink creates none",
                resolved,
            )
        return (
            None,
            f"{resolved} does not exist yet and the sink would create it; whether that create "
            "succeeds cannot be decided without creating it",
            resolved,
        )
    except (OSError, ValueError, TypeError) as exc:
        return False, f"unusable path ({type(exc).__name__}: {exc})", resolved

    if stat.S_ISDIR(mode):
        # Answered from the stat result rather than from a failed open, for two reasons. The open
        # would say "Permission denied", sending the reader after an access-control list instead of
        # after the fact that the path is a directory; and refusing here keeps the probe from
        # OPENING node types where opening is not free. That is the read-only contract's real edge:
        # a character device, a serial port or a named pipe can react to being opened (asserting
        # DTR, rewinding a tape, waking a pipe server), and the type is already in hand.
        return False, f"{resolved} is a directory, and the sink needs a file", resolved
    if not stat.S_ISREG(mode):
        return (
            False,
            f"{resolved} is not a regular file, so it would keep no durable trail "
            f"(st_mode {stat.filemode(mode)})",
            resolved,
        )

    try:
        handle = os.open(resolved, os.O_WRONLY | os.O_APPEND | _O_NONBLOCK)
    except (OSError, ValueError, TypeError) as exc:
        # str(OSError) already begins with "[Errno N] ", so prepending it again doubled it.
        return False, f"cannot be opened for append: {exc}", resolved
    with contextlib.suppress(OSError):
        os.close(handle)
    return True, f"{resolved} accepts an append", resolved


def _write_safety_report(cfg: EpicsConfig) -> WriteSafetyReport:
    """The effective posture of both write gates, resolved through the SAME helpers they use.

    ⚠️ It builds NEITHER gate, and that is a requirement rather than an optimisation.
    Constructing ``SafetyLayer`` raises on configurations this report exists to describe (writes
    armed with an empty allowlist, or a search reach beyond loopback), and ``OlogWriteGate`` builds
    a file audit logger on the way. Nor is that theoretical: ``epics-init`` puts the block it has
    just composed into ``os.environ`` and runs this very command against it, so a doctor that
    constructed a gate would die on a configuration the onboarding command had just handed the
    user.

    The two gate halves live in :mod:`epics_mcp.write_posture` because they are pure
    configuration, while the audit half below is not: ``_probe_audit_sink`` opens a file handle
    and can stall on a network path. This function is the composition of the two, and it is the
    only one of the three that costs I/O. ⚠️ A caller that cannot afford a stall, an MCP resource
    handler above all, calls the two builders directly instead of this.
    """
    writable, note, resolved = _probe_audit_sink(cfg.audit_log_file)
    return WriteSafetyReport(
        pv=pv_write_gate_report(cfg),
        olog=olog_write_gate_report(cfg),
        audit=AuditSinkReport(
            path=cfg.audit_log_file, resolved_path=resolved, writable=writable, note=note
        ),
    )


async def run_doctor(*, probe_pv: str | None = None, timeout: float | None = None) -> DoctorReport:
    """Probe every plane read-only and report reachability + CA + privacy + the write posture.

    Read-only, it probes, never writes. One clause on that, because one probe touches the
    filesystem: the audit-sink check OPENS a handle for append and writes zero bytes, creating
    nothing and leaving size and every timestamp unchanged (measured). It reaches exactly what is
    CONFIGURED and nothing else: a
    disabled plane makes NO network call, and no plane is touched unless its URL (or the EPICS
    address list, for ``probe_pv``) points there, which, on a configured deployment, may well be a
    real facility. ``ok`` is True iff no configured plane HARD-FAILED, a disabled/info plane never
    fails the check, and neither does an ``unverified`` (exit 0), an ``identity_probe_failed``
    (exit 3), a ``throttled`` (exit 3, and nothing about that plane was measured at all) nor a
    ``no_ingest`` (exit 0) one, so read ``inconclusive_identity_planes``, ``throttled_planes``,
    ``degraded_planes`` and ``verification_complete`` alongside ``ok`` before concluding that
    everything was confirmed.
    """
    cfg = get_config()
    probe_timeout = timeout if timeout is not None else cfg.diagnose_timeout
    # A DELTA around this run's own fan-out, never the absolute: the counter is monotonic and
    # process-wide, so bracketing is what makes the figure belong to this report (see
    # ``ReadThrottle.denials``). Sampled before the gather rather than inside a probe, because the
    # question is about the RUN and one probe cannot see what another was refused.
    denials_before = get_read_throttle().denials
    live, channelfinder, archiver, retrieval, alarm, naming, olog = await asyncio.gather(
        _check_live(cfg, probe_pv, probe_timeout),
        _check_channelfinder(cfg, probe_timeout),
        _check_archiver(cfg, probe_timeout),
        _check_retrieval_plane(cfg, probe_timeout),
        _check_alarm(cfg, probe_timeout),
        _check_naming(cfg, probe_timeout),
        _check_olog(cfg, probe_timeout),
    )
    planes = [live, channelfinder, archiver, retrieval, alarm, naming, olog]
    # Fail-CLOSED via an ALLOWLIST union: a plane is "not a hard failure" only if its status is
    # honestly clean (_NON_FAILING_STATUSES, exit 0) OR inconclusive (_INCONCLUSIVE_STATUSES,
    # exit 3). Anything else, a hard failure, or a new/mistyped status, makes ``ok`` False →
    # exit 1, so an unclassified status cannot quietly yield exit 0 from the tool whose job is to
    # catch bad config. ``ok`` alone does NOT map to the exit code; the exit code also reads
    # ``inconclusive``.
    ok = all(plane.status in _NON_FAILING_STATUSES | _INCONCLUSIVE_STATUSES for plane in planes)
    unverified = [plane.plane for plane in planes if plane.status == "unverified"]
    # The inconclusive statuses MINUS the throttled ones, and the subtraction is the point: both
    # drive exit 3, but this list feeds a verdict sentence about a FAILED identity probe, which is
    # not what happened to a plane this command refused itself. See _THROTTLED_STATUSES.
    identity_inconclusive = _INCONCLUSIVE_STATUSES - _THROTTLED_STATUSES
    inconclusive = [plane.plane for plane in planes if plane.status in identity_inconclusive]
    throttled = [plane.plane for plane in planes if plane.status in _THROTTLED_STATUSES]
    reads_denied = get_read_throttle().denials - denials_before
    # Degraded planes deliberately do NOT touch ``ok`` or ``verification_complete``: they are exit
    # 0 by product decision (see _DEGRADED_STATUSES). Their own list is the ONLY signal a machine
    # reader gets, which is exactly why it exists.
    degraded = [plane.plane for plane in planes if plane.status in _DEGRADED_STATUSES]
    return DoctorReport(
        planes=planes,
        privacy=_privacy_report(cfg),
        write_safety=_write_safety_report(cfg),
        # Pure, no I/O, and computed AFTER the gather because it is a function of its results.
        installation=installation_findings(cfg, planes),
        ok=ok,
        # A plane nobody asked is not a plane that was verified, so it closes this flag exactly
        # like the two states that DID get an answer and could not be named by it.
        verification_complete=(
            not unverified and not inconclusive and not throttled and not reads_denied
        ),
        degraded_planes=degraded,
        throttled_planes=throttled,
        reads_denied=reads_denied,
        unverified_planes=unverified,
        inconclusive_identity_planes=inconclusive,
        identified_planes=[plane.plane for plane in planes if plane.identified],
    )
