"""Read-only config self-check ("doctor") — is this deployment wired up correctly? (E2)

``run_doctor`` probes every CONFIGURED plane read-only — a transport probe, refined on success by
an identity probe, so a healthy plane answers up to TWO requests — and reports whether it is
reachable, whether the CA bundle works, whether the service **identifies itself as the service we
configured**, and what the ChannelFinder privacy redaction is set to. It is the ``flutter doctor``
of this server: a new user in a fresh facility runs ``epics-doctor`` and gets an immediate "is my
config right?" without asking us.

Design (mirrors :mod:`epics_pv_mcp.services.diagnose`):

* One :func:`asyncio.gather` fans out all planes; each gatherer is TOTAL (catches its own errors →
  a :class:`PlaneCheck`, never raises), so one dead plane cannot abort the report.
* An empty service URL means the plane is DISABLED — no client is built and no network call is
  made (the empty-URL-disables discipline). A disabled plane is not a failure.
* Reachability is proven by the client's ``check_connectivity`` probe. Its failure is classified
  into THREE buckets, not two, so a *reachable but wrong-endpoint* Archiver (a served non-2xx, e.g.
  ``EPICS_MCP_ARCHIVER_URL`` pointing at the retrieval webapp) is reported ``api_error``
  (reachable), NOT the misleading ``unreachable`` — the CA/HTTP-status cause predicates in
  ``_http`` tell them apart.
* **Reachable is not identified.** ``check_connectivity`` is a HEAD for CF/Alarm/Olog/Naming and
  counts ANY HTTP response as reachable — by design, it is a transport probe. That made ``ok`` mean
  only "the probe did not raise": measured, a ChannelFinder URL pointing at a DEAD container
  reported ``✓ channelfinder ok`` because a different service on that port answered 401 (its blanket
  auth answers 401 for any path, so the status carried no information about CF at all). So each
  REST plane is refined by an IDENTITY probe — see :func:`_identify`. What a plane cannot prove is
  ``unverified``, never ``ok``.
* The live/PVA plane has no URL (only ``provider`` + the EPICS address-list env). By default it is
  an INFO line (no pass/fail); ``--probe-pv NAME`` turns it into a real connectivity pass/fail and
  is the ONLY path that makes a live p4p call (no default egress).
* The privacy report resolves the ChannelFinder allowlists through the SAME ``resolve_safe_*``
  helpers the client uses, so what doctor reports and what the client redacts cannot drift.
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict

from epics_pv_mcp.config import EpicsConfig, get_config
from epics_pv_mcp.errors import EpicsError
from epics_pv_mcp.services._http import (
    build_retrying_session,
    http_status,
    is_loopback_url,
    is_retry_error,
    is_ssl_error,
    rest_get_json,
)
from epics_pv_mcp.services.alarm_client import AlarmClient
from epics_pv_mcp.services.archiver_client import ArchiverClient
from epics_pv_mcp.services.channelfinder_client import (
    ChannelFinderClient,
    resolve_safe_owner_accounts,
    resolve_safe_property_names,
)
from epics_pv_mcp.services.epics_client import pv_get
from epics_pv_mcp.services.naming_client import NamingServiceClient
from epics_pv_mcp.services.olog_client import OlogClient
from epics_pv_mcp.services.rest_exceptions import RestConnectionError, RestResponseError

#: Every status a plane can carry. A ``Literal`` rather than a bare ``str`` on purpose: the verdict
#: below is computed from a NON-FAILING allowlist, so a typo in a status string must be a type
#: error at the boundary, not a silent pass.
PlaneStatus = Literal[
    "ok",
    "disabled",
    "info",
    "unverified",
    "wrong_service",
    "config_error",
    "ca_error",
    "api_error",
    "unreachable",
    "disconnected",
]

#: Statuses that do NOT count as a doctor failure. An ALLOWLIST, not a failure denylist: with a
#: denylist an unforeseen or mistyped status silently lands on "not failing" and yields exit 0 —
#: fail-OPEN, in the one tool whose job is to notice a misconfiguration. Anything not listed here
#: fails (fail-closed), so the cost of forgetting to classify a new status is a false alarm rather
#: than a false all-clear.
#:
#: ``unverified`` is deliberately non-failing: that a healthy service answers its info endpoint
#: ANONYMOUSLY is measured at exactly one site (n=1), and turning that into a hard failure for every
#: site would be the same overclaim this server keeps finding in other people's code. It is reported
#: honestly instead — and ``DoctorReport.verification_complete`` tells a machine reader it happened.
_NON_FAILING_STATUSES: frozenset[str] = frozenset({"ok", "disabled", "info", "unverified"})


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
    #: identity could not be established; ``None`` when no identity probe applies (disabled, the
    #: live/PVA plane, or a plane the transport probe never got past). ``False`` is NOT a failure —
    #: it is an honest "reachable, identity unverified" (see :data:`_NON_FAILING_STATUSES`).
    identified: bool | None = None


class PrivacyReport(_Model):
    """What the ChannelFinder redaction surfaces vs. drops (the effective, site-configured sets)."""

    cf_safe_owner_accounts: list[str]
    cf_safe_property_names: list[str]
    #: Whether Olog free text (title/description) is withheld — the EFFECTIVE posture, resolved from
    #: ``olog_url``, not a static promise: entries come back whole from a loopback sandbox (ESS-spec
    #: pending, see olog_client). This is the tool used to CHECK the posture, so it must never claim
    #: a guarantee it does not have; ``True`` for a disabled plane (nothing is read at all).
    olog_freetext_withheld: bool


class DoctorReport(_Model):
    """The full self-check: every plane + the privacy posture + an overall pass/fail."""

    planes: list[PlaneCheck]
    privacy: PrivacyReport
    #: True iff no configured plane FAILED. Note what this does NOT say: a plane can be reachable
    #: with its identity unverified and still leave ``ok`` True — read ``verification_complete``
    #: before treating this as "everything is confirmed".
    ok: bool
    #: True iff no configured plane was left ``unverified``. ``ok`` alone is not enough for a
    #: machine reader: an unverified plane is honest, not healthy, and a CI job that only looks at
    #: ``ok`` (or the exit code) would read "nothing failed" as "everything is confirmed" — exactly
    #: the conflation this whole check exists to remove. ⚠️ Vacuously True when nothing ran an
    #: identity probe at all (e.g. an empty config) — a reader wanting POSITIVE confirmation
    #: asserts ``identified_planes`` is non-empty, not this flag alone.
    verification_complete: bool
    #: The planes that are reachable but could not prove their identity (empty when none).
    unverified_planes: list[str]
    #: The planes that PROVED their identity — the positive counterpart to ``unverified_planes``.
    #: Empty on an empty config, which is how a machine reader tells "everything was confirmed"
    #: from "nothing ran at all" (``verification_complete`` is vacuously True in both).
    identified_planes: list[str]


def _classify_failure(exc: Exception) -> tuple[bool | None, bool | None, PlaneStatus, str]:
    """Map a failed connectivity probe to ``(reachable, ca_ok, status, detail)``.

    Three buckets, keyed off the chained cause:

    * a TLS/CA failure (:func:`is_ssl_error`) → ``ca_error`` (reachable False, ca_ok False);
    * a *served* non-2xx (:func:`http_status` gives a code) → ``api_error`` — the host answered, so
      transport + CA are fine (reachable True, ca_ok True), but the endpoint is wrong / erroring;
    * anything else (a transport failure, no chained HTTP response) → ``unreachable``.
    """
    if is_ssl_error(exc):
        return (
            False,
            False,
            "ca_error",
            "TLS/CA verification failed — set EPICS_MCP_CA_BUNDLE to a PEM that trusts this host "
            "(combine internal + public CA roots when planes differ). See docs/deployment.md.",
        )
    code = http_status(exc)
    if code is not None:
        return (
            True,
            True,
            "api_error",
            f"reachable, but the service returned HTTP {code} — check the URL points at the right "
            "service/webapp (e.g. the Archiver mgmt port, not retrieval).",
        )
    if is_retry_error(exc):
        # A retry-exhausted 502/503/504: the host answered (repeatedly, with a 5xx), so it is
        # reachable-but-erroring, not unreachable. RetryError has no .response, so no exact code.
        return (
            True,
            True,
            "api_error",
            "reachable, but the service kept returning a retryable 5xx (502/503/504) until the "
            "retry budget was exhausted — the service is up but erroring. Check its health.",
        )
    return (False, None, "unreachable", f"could not reach the service: {_safe(str(exc))}")


#: The ``name`` each Phoebus-family service reports at its base URL, measured (they answer
#: ANONYMOUSLY with a JSON body — under ``content-type: text/plain``, so the body is parsed and the
#: content type deliberately ignored). The match is EXACT, not a substring: a substring would let a
#: service calling itself "Not Olog Service" pass as Olog.
_SERVICE_NAMES: dict[str, str] = {
    "channelfinder": "ChannelFinder Service",
    "olog": "Olog Service",
    "alarm": "Alarm logging Service",
}

#: The Naming Service has no ``{"name": ...}`` beacon, but ``/rest/swagger.json`` is an anonymous
#: static 200 whose ``info.title`` names it (measured; Olog answers 401 there, ChannelFinder 404).
_NAMING_SWAGGER_TITLE = "Naming service API documentation"

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


#: ``scheme://user:password@`` anywhere inside free text. Applied to what doctor PRINTS, not to
#: what it sends — this is an output guard, not a transport change.
_URL_CREDENTIALS = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@")


def _safe(text: str) -> str:
    """Redact ``user:password@`` out of anything doctor is about to print.

    Not cosmetic: requests' error text embeds the full request URL, and ``epics-doctor`` output is
    precisely what an operator pastes into a ticket when something is already going wrong.
    Credentials do not belong in a config URL (the documented path is ``EPICS_MCP_*_AUTH``, a
    header) — but a URL that carries them anyway must not be echoed back verbatim. This is a local
    output guard; the shared ``services/redact.py`` barrier is a different contract and untouched.
    """
    return _URL_CREDENTIALS.sub(r"\g<scheme>***@", text)


def _fetch_beacon(url: str, auth_header: str | None, timeout: float) -> object | Exception:
    """GET *url* and return the parsed body, or the Exception that stopped us. Never raises.

    The one place every identity probe issues its request, so the redirect posture and the
    error-to-``unverified`` translation cannot drift between planes.

    ``allow_redirects=False`` because the RESPONDING host is the whole point: a redirect would let
    another host answer for the one we configured, which is exactly the confusion being ruled out.
    Note a caller only ever sees a 2xx body — ``rest_get_json`` raises on a non-2xx BEFORE parsing,
    so an auth wall or a 404 can never reach a payload check.
    """
    session = build_retrying_session(auth_header=auth_header)
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
    except Exception as exc:  # noqa: BLE001 — TOTAL: any failure is an answer, never a raise
        return exc


def _identify(plane: str, base_url: str, auth_header: str | None, timeout: float) -> PlaneCheck:
    """Ask a Phoebus-family service to name itself; map the answer to a verdict. TOTAL.

    Returns ``ok`` (it named itself correctly), ``wrong_service`` (it named itself as a DIFFERENT
    known service — a misconfiguration that is unambiguous at any site, so it fails), or
    ``unverified`` (anything else: an auth wall, HTML, a body without a usable ``name``, a redirect,
    a 5xx). ``unverified`` is honest, not a failure — see :data:`_NON_FAILING_STATUSES`.
    """
    expected = _SERVICE_NAMES[plane]
    payload = _fetch_beacon(base_url, auth_header, timeout)
    if isinstance(payload, Exception):
        return _unverified(
            plane, f"transport reachable, but the identity probe failed: {_safe(str(payload))}"
        )

    name = payload.get("name") if isinstance(payload, dict) else None
    if not isinstance(name, str) or not name.strip():
        return _unverified(
            plane, "transport reachable, but the response carries no service name to check"
        )
    if name == expected:
        return PlaneCheck(
            plane=plane, configured=True, reachable=True, ca_ok=True, status="ok", identified=True
        )
    for other, other_name in _SERVICE_NAMES.items():
        if name == other_name:
            return PlaneCheck(
                plane=plane,
                configured=True,
                reachable=True,
                ca_ok=True,
                status="wrong_service",
                identified=False,
                detail=(
                    f"this URL is served by {other_name!r}, not {expected!r} — the "
                    f"{plane} URL points at the {other} service."
                ),
            )
    return _unverified(
        plane,
        f"transport reachable, but it identifies as {name!r}, not {expected!r} — unknown service",
    )


def _unverified(plane: str, detail: str) -> PlaneCheck:
    """Reachable, identity not established. Honest — NOT ``ok``, and NOT a failure."""
    return PlaneCheck(
        plane=plane,
        configured=True,
        reachable=True,
        ca_ok=True,
        status="unverified",
        identified=False,
        detail=detail,
    )


def _disabled(plane: str, env_var: str) -> PlaneCheck:
    """A plane whose URL is unset: honestly off, no client built, no network call, not a failure."""
    return PlaneCheck(
        plane=plane,
        configured=False,
        status="disabled",
        detail=f"disabled — set {env_var} to enable",
    )


async def _run_probe(plane: str, run: object, identify: object = None) -> PlaneCheck:
    """Run a sync ``check_connectivity`` off the event loop; classify success/failure. TOTAL.

    On success the verdict is REFINED by *identify* when the plane has an identity probe. Without
    it, "ok" would mean only "the transport probe did not raise" — which is how a URL pointing at a
    dead container earned a ✓ from a neighbouring service's 401. ``check_connectivity`` itself is
    left untouched: it is the shared transport probe (``lookup_device_name`` and
    ``diagnose_connection`` depend on its exact semantics), so identity is layered on here rather
    than pushed down into it.
    """
    try:
        await asyncio.to_thread(run)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001 — TOTAL: any failure → classified PlaneCheck, never raises
        reachable, ca_ok, status, detail = _classify_failure(exc)
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
    if not cfg.channelfinder_url:
        return _disabled("channelfinder", "EPICS_MCP_CHANNELFINDER_URL")

    def _run() -> None:
        ChannelFinderClient(
            cfg.channelfinder_url, timeout=timeout, auth_header=cfg.channelfinder_auth or None
        ).check_connectivity()

    def _id() -> PlaneCheck:
        return _identify(
            "channelfinder", cfg.channelfinder_url, cfg.channelfinder_auth or None, timeout
        )

    return await _run_probe("channelfinder", _run, _id)


def _identify_archiver(base_url: str, auth_header: str | None, timeout: float) -> PlaneCheck:
    """The appliance names itself in ``getApplianceInfo`` — but only if we look at the body.

    ``ArchiverClient.check_connectivity`` already demands a 2xx with parseable JSON from
    ``/mgmt/bpl/getApplianceInfo`` (stronger than the HEAD planes), yet it DISCARDS the payload:
    an empty ``{}`` passes. The appliance's own ``identity`` field is what turns "something served
    JSON here" into "an Archiver appliance served it", so it is checked rather than assumed.
    """
    session = build_retrying_session(auth_header=auth_header)
    try:
        payload = rest_get_json(
            session,
            f"{base_url}/mgmt/bpl/getApplianceInfo",
            None,
            timeout,
            conn_exc=RestConnectionError,
            resp_exc=RestResponseError,
            allow_redirects=False,
        )
    except Exception as exc:  # noqa: BLE001 — TOTAL: any failure → unverified, never raises
        return _unverified(
            "archiver", f"transport reachable, but the identity probe failed: {_safe(str(exc))}"
        )

    identity = payload.get("identity") if isinstance(payload, dict) else None
    if not isinstance(identity, str) or not identity.strip():
        return _unverified(
            "archiver",
            "transport reachable, but getApplianceInfo carries no 'identity' — this may not be an "
            "Archiver appliance MGMT endpoint",
        )
    return PlaneCheck(
        plane="archiver",
        configured=True,
        reachable=True,
        ca_ok=True,
        status="ok",
        identified=True,
        detail=f"appliance identity: {identity}",
    )


async def _check_archiver(cfg: EpicsConfig, timeout: float) -> PlaneCheck:
    if not cfg.archiver_url:
        return _disabled("archiver", "EPICS_MCP_ARCHIVER_URL")

    def _run() -> None:
        ArchiverClient(
            cfg.archiver_url, timeout=timeout, auth_header=cfg.archiver_auth or None
        ).check_connectivity()

    def _id() -> PlaneCheck:
        return _identify_archiver(cfg.archiver_url, cfg.archiver_auth or None, timeout)

    return await _run_probe("archiver", _run, _id)


def _identify_retrieval_plane(base_url: str, auth_header: str | None, timeout: float) -> PlaneCheck:
    """The retrieval webapp names itself in ``/retrieval/bpl/getVersion``.

    Note the base PATH: retrieval serves ``/retrieval/bpl``, not ``/mgmt/bpl`` — probing the latter
    on the retrieval port 404s and says nothing. The version string STARTS with the product name,
    so the check is anchored there up to a word boundary (see ``_is_archiver_version_string``);
    only the release number after the full name is variable and must not be pinned (an appliance
    upgrade is not a misconfiguration).
    """
    payload = _fetch_beacon(
        f"{base_url.rstrip('/')}/retrieval/bpl/getVersion", auth_header, timeout
    )
    if isinstance(payload, Exception):
        return _unverified(
            "archiver_retrieval",
            f"transport reachable, but the identity probe failed: {_safe(str(payload))}",
        )

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
        f"{_ARCHIVER_PRODUCT} — this may not be the retrieval webapp",
    )


async def _check_retrieval_plane(cfg: EpicsConfig, timeout: float) -> PlaneCheck:
    """The archiver RETRIEVAL webapp — its own line, because nothing else probes it.

    ``get_pv_history`` talks to RETRIEVAL, while the archiver plane only ever probed MGMT: a dead
    retrieval endpoint read as ``archiver ok`` while every history call 404'd.

    The URL mirrors the CLIENT's own resolution (``retrieval_url or base_url``): a single-JVM
    appliance serves every webapp on one port and leaves ``EPICS_MCP_ARCHIVER_RETRIEVAL_URL``
    empty, so treating an empty var as "plane off" would report ``disabled`` for a retrieval
    endpoint that is very much live and being queried — the same false all-clear this check exists
    to remove, wearing the word "disabled".

    The INVERSE pair — retrieval URL set, archiver URL empty — is dead config, not a plane to
    probe: every archiver tool gates on ``EPICS_MCP_ARCHIVER_URL`` (tools/archiver.py,
    checkers.py), so nothing ever queries that retrieval URL. The first fallback fix probed it
    anyway and reported the STRONGEST all-clear the tool knows (``ok=True,
    verification_complete=True``) for a configuration none of the tools can use — a fix against
    false-green that produced false-green. It is a loud ``config_error`` now, without a network
    call: the config itself is the finding, and an ``ok`` line next to it would only muddy what
    the operator has to change.
    """
    if cfg.archiver_retrieval_url and not cfg.archiver_url:
        return PlaneCheck(
            plane="archiver_retrieval",
            configured=True,
            status="config_error",
            detail=(
                "EPICS_MCP_ARCHIVER_RETRIEVAL_URL is set but EPICS_MCP_ARCHIVER_URL is empty — "
                "every archiver tool gates on EPICS_MCP_ARCHIVER_URL, so this retrieval URL is "
                "never used. Set EPICS_MCP_ARCHIVER_URL (the MGMT webapp URL)."
            ),
        )
    url = cfg.archiver_retrieval_url or cfg.archiver_url
    if not url:
        return _disabled("archiver_retrieval", "EPICS_MCP_ARCHIVER_URL")

    def _run() -> None:
        # Probe RETRIEVAL's own endpoint, and through rest_get_json so the failure arrives with its
        # cause chained: that is what lets _classify_failure tell a CA problem from a wrong webapp
        # from a dead host. A raw session.head here would collapse all three into "unreachable".
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

    return await _run_probe("archiver_retrieval", _run, _id)


async def _check_alarm(cfg: EpicsConfig, timeout: float) -> PlaneCheck:
    if not cfg.alarm_url:
        return _disabled("alarm", "EPICS_MCP_ALARM_URL")

    def _run() -> None:
        AlarmClient(
            cfg.alarm_url, timeout=timeout, auth_header=cfg.alarm_auth or None
        ).check_connectivity()

    def _id() -> PlaneCheck:
        return _identify("alarm", cfg.alarm_url, cfg.alarm_auth or None, timeout)

    return await _run_probe("alarm", _run, _id)


def _identify_naming(base_url: str, timeout: float) -> PlaneCheck:
    """The Naming Service names itself in its Swagger contract.

    It has no ``{"name": ...}`` beacon like the Phoebus trio, but ``/rest/swagger.json`` is a
    static, anonymous 200 whose ``info.title`` identifies the service — and it DISCRIMINATES
    (measured: Olog answers 401 there, ChannelFinder 404).

    The title is documentation prose and may be reworded by a future release, so a mismatch is
    ``unverified``, never ``wrong_service``: we can recognise this service, but we cannot conclude
    from an unfamiliar title that some *other* known service is answering.
    """
    payload = _fetch_beacon(f"{base_url.rstrip('/')}/rest/swagger.json", None, timeout)
    if isinstance(payload, Exception):
        return _unverified(
            "naming", f"transport reachable, but the identity probe failed: {_safe(str(payload))}"
        )

    info = payload.get("info") if isinstance(payload, dict) else None
    title = info.get("title") if isinstance(info, dict) else None
    if title == _NAMING_SWAGGER_TITLE:
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
        f"{_NAMING_SWAGGER_TITLE!r} — this may not be the Naming Service (or its title changed)",
    )


async def _check_naming(cfg: EpicsConfig, timeout: float) -> PlaneCheck:
    if not cfg.naming_url:
        return _disabled("naming", "EPICS_MCP_NAMING_URL")

    def _run() -> None:
        NamingServiceClient(base_url=cfg.naming_url, timeout=timeout).check_connectivity()

    def _id() -> PlaneCheck:
        return _identify_naming(cfg.naming_url, timeout)

    return await _run_probe("naming", _run, _id)


async def _check_olog(cfg: EpicsConfig, timeout: float) -> PlaneCheck:
    if not cfg.olog_url:
        return _disabled("olog", "EPICS_MCP_OLOG_URL")

    def _run() -> None:
        OlogClient(
            cfg.olog_url, timeout=timeout, auth_header=cfg.olog_auth or None
        ).check_connectivity()

    def _id() -> PlaneCheck:
        return _identify("olog", cfg.olog_url, cfg.olog_auth or None, timeout)

    return await _run_probe("olog", _run, _id)


async def _check_live(cfg: EpicsConfig, probe_pv: str | None, timeout: float) -> PlaneCheck:
    """The live/PVA plane. INFO-only by default (no p4p call); a real pass/fail with ``probe_pv``.

    The live plane has no URL — its config is ``provider`` + the EPICS address-list env. Without a
    probe PV there is nothing to connect to, so this reports the posture (no default egress). Only
    ``probe_pv`` triggers a live read.
    """
    addr = os.environ.get("EPICS_PVA_ADDR_LIST") or os.environ.get("EPICS_CA_ADDR_LIST")
    posture = f"address list set ({addr})" if addr else "localhost-isolated (no address list set)"
    base = f"provider={cfg.provider}, {posture}"
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
        detail=f"{base}; {probe_pv} did NOT connect ({error_code or 'internal error'}).",
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
    except Exception as exc:  # noqa: BLE001 — internal probe failure → not connected, keep doctor total
        return (False, type(exc).__name__)
    return (True, None)


def _privacy_report(cfg: EpicsConfig) -> PrivacyReport:
    """The effective redaction posture, resolved through the SAME helpers the clients use.

    ``olog_freetext_withheld`` mirrors ``OlogClient._redact`` exactly — BOTH conditions, or this
    tool would report a posture the client does not have. An unconfigured plane reads nothing, so
    True is honest there.
    """
    olog_full = bool(cfg.olog_url) and is_loopback_url(cfg.olog_url) and cfg.olog_assume_test_data
    return PrivacyReport(
        cf_safe_owner_accounts=sorted(resolve_safe_owner_accounts(cfg)),
        cf_safe_property_names=sorted(resolve_safe_property_names(cfg)),
        olog_freetext_withheld=not olog_full,
    )


async def run_doctor(*, probe_pv: str | None = None, timeout: float | None = None) -> DoctorReport:
    """Probe every configured plane read-only and report reachability + CA + privacy posture.

    Read-only — it probes, never writes. It reaches exactly what is CONFIGURED and nothing else: a
    disabled plane makes NO network call, and no plane is touched unless its URL (or the EPICS
    address list, for ``probe_pv``) points there — which, on a configured deployment, may well be a
    real facility. ``ok`` is True iff no configured plane FAILED — a disabled/info plane never fails
    the check, and neither does an ``unverified`` one, so read ``verification_complete`` alongside
    ``ok`` before concluding that everything was confirmed.
    """
    cfg = get_config()
    probe_timeout = timeout if timeout is not None else cfg.diagnose_timeout
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
    # Fail-CLOSED: anything not explicitly known to be non-failing counts as a failure, so a new or
    # mistyped status cannot quietly yield exit 0 from the tool whose job is to catch bad config.
    ok = all(plane.status in _NON_FAILING_STATUSES for plane in planes)
    unverified = [plane.plane for plane in planes if plane.status == "unverified"]
    return DoctorReport(
        planes=planes,
        privacy=_privacy_report(cfg),
        ok=ok,
        verification_complete=not unverified,
        unverified_planes=unverified,
        identified_planes=[plane.plane for plane in planes if plane.identified],
    )
