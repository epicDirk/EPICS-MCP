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
from epics_pv_mcp.services.naming_identity import NAMING_SWAGGER_PATH, NAMING_SWAGGER_TITLE
from epics_pv_mcp.services.olog_client import OlogClient
from epics_pv_mcp.services.rest_exceptions import RestConnectionError, RestResponseError

#: Every status a plane can carry. A ``Literal`` rather than a bare ``str`` on purpose: the exit
#: verdict below is computed from status ALLOWLISTS (the three frozensets), so a typo in a status
#: string is a type error at the boundary, not a silent pass.
PlaneStatus = Literal[
    "ok",
    "disabled",
    "info",
    "unverified",
    "identity_probe_failed",
    "config_error",
    "ca_error",
    "api_error",
    "unreachable",
    "disconnected",
]

#: Statuses that are honestly clean → exit 0. An ALLOWLIST, not a failure denylist: with a denylist
#: an unforeseen or mistyped status silently lands on "not failing" and yields exit 0 — fail-OPEN,
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
#: served the real ChannelFinder API while the base GET answered as Olog — a foreign name cannot
#: prove a misconfiguration. All three ANSWERED 2xx; none is a failure. A probe that actually FAILED
#: (a served non-2xx, a transport error, a refused redirect) is NOT here — it is
#: ``identity_probe_failed``, which the exit code notices. It is all reported honestly, and
#: ``DoctorReport.verification_complete`` tells a machine reader identity was not established.
_NON_FAILING_STATUSES: frozenset[str] = frozenset({"ok", "disabled", "info", "unverified"})

#: Reachable, but the identity probe itself FAILED — a served non-2xx (401/404/5xx), a transport
#: error, or a refused redirect on the identity endpoint — as opposed to ``unverified``, where the
#: endpoint ANSWERED 2xx and we merely could not name it. Not a hard failure: a 401 on an INFO
#: endpoint does not prove the plane's TOOL endpoints are broken, so it never claims "plane failed"
#: (exit 1). But it is not a silent all-clear either — it drives its own inconclusive exit (3) and
#: never renders "OK". This is the S4 origin story (a URL at a dead container whose neighbour
#: answered 401), which used to collapse to a silent exit 0 via ``unverified``.
_INCONCLUSIVE_STATUSES: frozenset[str] = frozenset({"identity_probe_failed"})

#: Statuses that ARE a hard failure → exit 1. Listed explicitly (rather than "everything else")
#: only so ``test_status_partition_is_total_and_disjoint`` can prove the three sets tile
#: ``PlaneStatus`` exactly. The fail-closed guarantee still comes from ``ok`` being an allowlist of
#: the OTHER two sets (an unclassified status is in neither, so it is not clean and not inconclusive
#: → it fails), never from this denylist.
_FAILING_STATUSES: frozenset[str] = frozenset(
    {"config_error", "ca_error", "api_error", "unreachable", "disconnected"}
)


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
    #: True iff no configured plane HARD-FAILED (nothing in ``_FAILING_STATUSES``). Note what this
    #: does NOT say: a plane can be reachable with its identity ``unverified`` (still exit 0) OR its
    #: identity probe ``identity_probe_failed`` (exit 3) and still leave ``ok`` True — ``ok`` alone
    #: does not map to the exit code. Read ``inconclusive_identity_planes`` (exit 3 driver) and
    #: ``verification_complete`` before treating this as "everything is confirmed".
    ok: bool
    #: True iff no configured plane was left ``unverified`` AND none had its identity probe fail
    #: (``inconclusive_identity_planes`` empty). ⚠️ This is NOT "every configured plane's identity
    #: was established": a HARD-failed plane (``unreachable`` / ``api_error`` / ``ca_error``) is
    #: never identity-probed, so it lands in ``ok`` (which goes False), NOT here — this flag can be
    #: True while a plane hard-failed (read ``ok`` / ``identified_planes`` for that). ``ok`` alone
    #: is not enough for a machine reader either: an unverified/inconclusive plane is honest, not
    #: healthy, and a CI job that only looks at ``ok`` would read "nothing hard-failed" as
    #: "everything is confirmed" — exactly the conflation this whole check exists to remove.
    #: ⚠️ Vacuously True when nothing ran an identity probe at all (e.g. an empty config) — a reader
    #: wanting POSITIVE confirmation asserts ``identified_planes`` is non-empty, not this flag.
    verification_complete: bool
    #: The planes that ANSWERED 2xx but could not prove their identity — anonymous, an unreadable
    #: body, or a foreign name (empty when none). Honest, not a failure → exit 0.
    unverified_planes: list[str]
    #: The planes whose identity probe FAILED (a served non-2xx / transport error / refused
    #: redirect) — reachable but suspect, distinct from ``unverified`` (empty when none). Drives the
    #: inconclusive exit 3. A machine reader reads this ALONGSIDE ``unverified_planes``: a failed
    #: probe lands HERE, not in ``unverified_planes``.
    inconclusive_identity_planes: list[str]
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

#: The Naming Service's swagger beacon (title + path) is single-sourced in
#: :mod:`epics_pv_mcp.services.naming_identity` (imported above) — the ONE home shared with
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

    Three outcomes: ``ok`` (it named itself correctly); ``unverified`` — it ANSWERED 2xx but we
    could not name it: an unreadable/HTML body, a body without a usable ``name``, or a body naming a
    DIFFERENT known service (with that name in the detail); or ``identity_probe_failed`` — the probe
    itself FAILED (a served non-2xx like a 401 auth wall or a 404, a transport error, a refused
    redirect), routed via :func:`_identity_fetch_failure`. A foreign name is deliberately NOT a
    failure (S14): the earlier ``wrong_service``+exit-1 verdict rested on "unambiguous at any site",
    refuted by measurement (2026-07-16) — a path-based reverse proxy served the REAL ChannelFinder
    API while the base GET answered as Olog, so the hard failure flagged a WORKING configuration.
    ``unverified`` is honest (exit 0); ``identity_probe_failed`` is inconclusive (exit 3, never a
    silent all-clear) — see :data:`_NON_FAILING_STATUSES` / :data:`_INCONCLUSIVE_STATUSES`.
    """
    expected = _SERVICE_NAMES[plane]
    payload = _fetch_beacon(base_url, auth_header, timeout)
    if isinstance(payload, Exception):
        return _identity_fetch_failure(plane, payload)

    name = payload.get("name") if isinstance(payload, dict) else None
    if not isinstance(name, str) or not name.strip():
        return _unverified(
            plane, "transport reachable, but the response carries no service name to check"
        )
    if name == expected:
        return PlaneCheck(
            plane=plane, configured=True, reachable=True, ca_ok=True, status="ok", identified=True
        )
    # A KNOWN foreign name keeps its plane mapping in the detail — that is the actionable clue
    # when the config really is cross-wired (status stays unverified either way, S14).
    hint = next(
        (f" (the name of the {other} service)" for other, o in _SERVICE_NAMES.items() if name == o),
        "",
    )
    return _unverified(
        plane,
        f"transport reachable, but this URL answers as {name!r}{hint}, not {expected!r} — cannot "
        f"confirm it is the {plane} service. Not a failure: a path-based reverse proxy can "
        "serve the real API behind a base URL that names another service (measured); if the "
        "config IS wrong, the name here is the clue.",
    )


def _unverified(plane: str, detail: str) -> PlaneCheck:
    """Reachable, endpoint ANSWERED 2xx, identity not established. Honest — NOT ``ok``, and NOT a
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
    (:data:`_INCONCLUSIVE_STATUSES`) — reported as inconclusive (exit 3), never a silent
    all-clear."""
    return PlaneCheck(
        plane=plane,
        configured=True,
        reachable=True,
        ca_ok=True,
        status="identity_probe_failed",
        identified=False,
        detail=detail,
    )


def _beacon_reached_but_unreadable(exc: BaseException) -> bool:
    """True iff a failed identity fetch actually REACHED a 2xx response whose body was unreadable.

    :func:`~epics_pv_mcp.services._http.rest_get_json` calls ``raise_for_status()`` BEFORE
    ``resp.json()``, so the only way a 2xx is reached and the call still raises is a body that is
    not JSON — a ``JSONDecodeError`` (a ``ValueError`` subclass). On ``requests>=2.27`` that is a
    ``requests`` ``JSONDecodeError``, wrapped by ``rest_get_json`` and read here as the
    ``__cause__``; on the older ``requests>=2.25`` floor it is the STDLIB ``json.JSONDecodeError`` —
    a ``ValueError`` but NOT a ``RequestException``, so ``rest_get_json`` does not wrap it and it
    arrives raw (hence we check the exception ITSELF too). A served non-2xx chains an ``HTTPError``,
    a transport failure a ``ConnectionError``, a refused redirect chains nothing — none is a
    ``ValueError``. So this cleanly separates "answered 2xx, just not nameably" (honest
    ``unverified``, e.g. a 200 HTML login page) from "the probe FAILED" (``identity_probe_failed``).
    Null-safe."""
    return isinstance(exc, ValueError) or isinstance(getattr(exc, "__cause__", None), ValueError)


def _identity_fetch_failure(plane: str, exc: BaseException) -> PlaneCheck:
    """Map a FAILED identity fetch to a verdict, shared by every identity probe so the split cannot
    drift: a REACHED-but-unreadable 2xx (a body that is not JSON) is honest :func:`_unverified`;
    anything else — a served non-2xx, a transport error, a refused redirect — is
    :func:`_identity_probe_failed`."""
    if _beacon_reached_but_unreadable(exc):
        return _unverified(
            plane,
            "transport reachable; the endpoint answered 2xx but its body was not readable JSON, so "
            f"its identity could not be checked: {_safe(str(exc))}",
        )
    return _identity_probe_failed(
        plane, f"transport reachable, but the identity probe FAILED: {_safe(str(exc))}"
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
    except Exception as exc:  # noqa: BLE001 — TOTAL: any failure → classified, never raises
        return _identity_fetch_failure("archiver", exc)

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
        #
        # Deliberate redirect asymmetry (do NOT add allow_redirects=False here): this TRANSPORT
        # probe answers "is the webapp reachable", so it FOLLOWS redirects exactly as
        # get_pv_history's real ArchiverClient does (its _get omits allow_redirects → default True).
        # Refusing here would report api_error/exit-1 for an endpoint the tool actually reaches.
        # Origin integrity is the IDENTITY probe's job — _identify_retrieval_plane's _fetch_beacon
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
    ``unverified``: we can recognise this service, but an unfamiliar title proves nothing about
    what is answering (since S14 the same honesty applies to every identity probe — even a
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
        f"{NAMING_SWAGGER_TITLE!r} — this may not be the Naming Service (or its title changed)",
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


# The EPICS client-search env vars, i.e. every way a PV search can leave this host. The list
# vars are enumerated individually (never or-folded — a fallback chain would mask all but the
# first); the *_AUTO_ADDR_LIST pair is handled separately because its UNSET state means ON.
_SEARCH_LIST_VARS = (
    "EPICS_PVA_ADDR_LIST",
    "EPICS_CA_ADDR_LIST",
    "EPICS_PVA_NAME_SERVERS",  # TCP unicast to named servers — NOT subnet-bound
    "EPICS_CA_NAME_SERVERS",
)
# Spellings that pvxs/libca parse as an explicit "no auto search". Anything else (including
# unset and unparseable) leaves the auto search ON — that is the EPICS default.
_AUTO_ADDR_OFF = frozenset({"no", "false", "0"})


def _live_search_posture(provider: str) -> str:
    """The PV search-path posture of THIS process, read from the same env pvxs reads.

    Read from ``os.environ``, deliberately NOT from a p4p ``Context.conf()``: the doctor's
    no-probe path guarantees no default egress, and merely building a Context binds sockets
    and starts worker threads. The env interpretation is pinned to the pvxs sources instead:
    ``EPICS_PVA_NAME_SERVERS`` alone is a full search path (TCP unicast, not subnet-bound —
    pvxs ``src/client.cpp`` ``startNS()``), and ``autoAddrList`` DEFAULTS TO TRUE (pvxs
    ``pvxs/client.h``), so even a null environment broadcasts searches into the local
    subnets. ``localhost-isolated`` is therefore claimed ONLY when every search list is
    unset AND the active provider's auto-addr search is explicitly disabled — never as a
    default posture.
    """
    paths: list[str] = []
    for var in _SEARCH_LIST_VARS:
        value = os.environ.get(var, "").strip()
        if value:
            paths.append(f"{var} ({value})")
    auto_var = "EPICS_PVA_AUTO_ADDR_LIST" if provider == "pva" else "EPICS_CA_AUTO_ADDR_LIST"
    auto_value = os.environ.get(auto_var, "").strip()
    if auto_value.lower() not in _AUTO_ADDR_OFF:
        state = f"={auto_value}" if auto_value else " unset, default ON"
        paths.append(f"auto-addr subnet broadcast ({auto_var}{state})")
    if paths:
        return "search paths: " + "; ".join(paths)
    return "localhost-isolated (no search list set, auto-addr search explicitly disabled)"


async def _check_live(cfg: EpicsConfig, probe_pv: str | None, timeout: float) -> PlaneCheck:
    """The live/PVA plane. INFO-only by default (no p4p call); a real pass/fail with ``probe_pv``.

    The live plane has no URL — its config is ``provider`` + the EPICS search-path env. Without a
    probe PV there is nothing to connect to, so this reports the posture (no default egress). Only
    ``probe_pv`` triggers a live read.
    """
    base = f"provider={cfg.provider}, {_live_search_posture(cfg.provider)}"
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
    real facility. ``ok`` is True iff no configured plane HARD-FAILED — a disabled/info plane never
    fails the check, and neither does an ``unverified`` (exit 0) nor an ``identity_probe_failed``
    (exit 3) one, so read ``inconclusive_identity_planes`` and ``verification_complete`` alongside
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
    # Fail-CLOSED via an ALLOWLIST union: a plane is "not a hard failure" only if its status is
    # honestly clean (_NON_FAILING_STATUSES, exit 0) OR inconclusive (_INCONCLUSIVE_STATUSES,
    # exit 3). Anything else — a hard failure, or a new/mistyped status — makes ``ok`` False →
    # exit 1, so an unclassified status cannot quietly yield exit 0 from the tool whose job is to
    # catch bad config. ``ok`` alone does NOT map to the exit code; the exit code also reads
    # ``inconclusive``.
    ok = all(plane.status in _NON_FAILING_STATUSES | _INCONCLUSIVE_STATUSES for plane in planes)
    unverified = [plane.plane for plane in planes if plane.status == "unverified"]
    inconclusive = [plane.plane for plane in planes if plane.status in _INCONCLUSIVE_STATUSES]
    return DoctorReport(
        planes=planes,
        privacy=_privacy_report(cfg),
        ok=ok,
        verification_complete=not unverified and not inconclusive,
        unverified_planes=unverified,
        inconclusive_identity_planes=inconclusive,
        identified_planes=[plane.plane for plane in planes if plane.identified],
    )
