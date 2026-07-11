"""Read-only config self-check ("doctor") — is this deployment wired up correctly? (E2)

``run_doctor`` probes every CONFIGURED plane once, read-only, and reports whether it is reachable,
whether the CA bundle works, whether the service answers, and what the ChannelFinder privacy
redaction is set to. It is the ``flutter doctor`` of this server: a new user in a fresh facility
runs ``epics-doctor`` and gets an immediate "is my config right?" without asking us.

Design (mirrors :mod:`epics_pv_mcp.services.diagnose`):

* One :func:`asyncio.gather` fans out all planes; each gatherer is TOTAL (catches its own errors →
  a :class:`PlaneCheck`, never raises), so one dead plane cannot abort the report.
* An empty service URL means the plane is DISABLED — no client is built and no network call is
  made (the empty-URL-disables discipline). A disabled plane is not a failure.
* Reachability is proven by the client's ``check_connectivity`` probe. Its failure is classified
  into THREE buckets, not two, so a *reachable but wrong-endpoint* Archiver (a served non-2xx, e.g.
  ``EPICS_MCP_ARCHIVER_URL`` pointing at the retrieval webapp) is reported ``api_error``
  (reachable), NOT the misleading ``unreachable`` — the CA/HTTP-status cause predicates in
  ``_http`` tell them apart. The HEAD-based CF/Alarm/Olog planes never hit ``api_error``.
* The live/PVA plane has no URL (only ``provider`` + the EPICS address-list env). By default it is
  an INFO line (no pass/fail); ``--probe-pv NAME`` turns it into a real connectivity pass/fail and
  is the ONLY path that makes a live p4p call (no default egress).
* The privacy report resolves the ChannelFinder allowlists through the SAME ``resolve_safe_*``
  helpers the client uses, so what doctor reports and what the client redacts cannot drift.
"""

from __future__ import annotations

import asyncio
import os

from pydantic import BaseModel, ConfigDict

from epics_pv_mcp.config import EpicsConfig, get_config
from epics_pv_mcp.errors import EpicsError
from epics_pv_mcp.services._http import http_status, is_retry_error, is_ssl_error
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

#: Statuses that count as a doctor FAILURE (drive exit code 1 / ``ok=False``). A disabled or
#: info-only plane is deliberately NOT here — an honestly-off plane is not a misconfiguration.
_FAILING_STATUSES = frozenset({"ca_error", "api_error", "unreachable", "disconnected"})


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
    #: ``disabled`` / ``ok`` / ``ca_error`` / ``api_error`` / ``unreachable`` / ``info`` /
    #: ``disconnected`` (the last only for the live plane with ``--probe-pv``).
    status: str
    detail: str | None = None


class PrivacyReport(_Model):
    """What the ChannelFinder redaction surfaces vs. drops (the effective, site-configured sets)."""

    cf_safe_owner_accounts: list[str]
    cf_safe_property_names: list[str]
    #: Olog free-text (title/description) is ALWAYS withheld — a static guarantee, for clarity.
    olog_freetext_withheld: bool = True


class DoctorReport(_Model):
    """The full self-check: every plane + the privacy posture + an overall pass/fail."""

    planes: list[PlaneCheck]
    privacy: PrivacyReport
    #: True iff no configured plane failed (all planes are ok / disabled / info).
    ok: bool


def _classify_failure(exc: Exception) -> tuple[bool | None, bool | None, str, str]:
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
    return (False, None, "unreachable", f"could not reach the service: {exc}")


def _disabled(plane: str, env_var: str) -> PlaneCheck:
    """A plane whose URL is unset: honestly off, no client built, no network call, not a failure."""
    return PlaneCheck(
        plane=plane,
        configured=False,
        status="disabled",
        detail=f"disabled — set {env_var} to enable",
    )


async def _run_probe(plane: str, run: object) -> PlaneCheck:
    """Run a sync ``check_connectivity`` off the event loop; classify success/failure. TOTAL."""
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
    return PlaneCheck(plane=plane, configured=True, reachable=True, ca_ok=True, status="ok")


async def _check_channelfinder(cfg: EpicsConfig, timeout: float) -> PlaneCheck:
    if not cfg.channelfinder_url:
        return _disabled("channelfinder", "EPICS_MCP_CHANNELFINDER_URL")

    def _run() -> None:
        ChannelFinderClient(
            cfg.channelfinder_url, timeout=timeout, auth_header=cfg.channelfinder_auth or None
        ).check_connectivity()

    return await _run_probe("channelfinder", _run)


async def _check_archiver(cfg: EpicsConfig, timeout: float) -> PlaneCheck:
    if not cfg.archiver_url:
        return _disabled("archiver", "EPICS_MCP_ARCHIVER_URL")

    def _run() -> None:
        ArchiverClient(
            cfg.archiver_url, timeout=timeout, auth_header=cfg.archiver_auth or None
        ).check_connectivity()

    return await _run_probe("archiver", _run)


async def _check_alarm(cfg: EpicsConfig, timeout: float) -> PlaneCheck:
    if not cfg.alarm_url:
        return _disabled("alarm", "EPICS_MCP_ALARM_URL")

    def _run() -> None:
        AlarmClient(
            cfg.alarm_url, timeout=timeout, auth_header=cfg.alarm_auth or None
        ).check_connectivity()

    return await _run_probe("alarm", _run)


async def _check_naming(cfg: EpicsConfig, timeout: float) -> PlaneCheck:
    if not cfg.naming_url:
        return _disabled("naming", "EPICS_MCP_NAMING_URL")

    def _run() -> None:
        NamingServiceClient(base_url=cfg.naming_url, timeout=timeout).check_connectivity()

    return await _run_probe("naming", _run)


async def _check_olog(cfg: EpicsConfig, timeout: float) -> PlaneCheck:
    if not cfg.olog_url:
        return _disabled("olog", "EPICS_MCP_OLOG_URL")

    def _run() -> None:
        OlogClient(
            cfg.olog_url, timeout=timeout, auth_header=cfg.olog_auth or None
        ).check_connectivity()

    return await _run_probe("olog", _run)


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
    """The effective ChannelFinder redaction, resolved through the SAME helpers the client uses."""
    return PrivacyReport(
        cf_safe_owner_accounts=sorted(resolve_safe_owner_accounts(cfg)),
        cf_safe_property_names=sorted(resolve_safe_property_names(cfg)),
        olog_freetext_withheld=True,
    )


async def run_doctor(*, probe_pv: str | None = None, timeout: float | None = None) -> DoctorReport:
    """Probe every configured plane read-only and report reachability + CA + privacy posture.

    Read-only and localhost-isolated by default: a disabled plane makes NO network call; no plane
    is reached unless its URL (or the EPICS address list, for ``probe_pv``) points there. ``ok`` is
    True iff every configured plane is healthy — a disabled/info plane never fails the check.
    """
    cfg = get_config()
    probe_timeout = timeout if timeout is not None else cfg.diagnose_timeout
    live, channelfinder, archiver, alarm, naming, olog = await asyncio.gather(
        _check_live(cfg, probe_pv, probe_timeout),
        _check_channelfinder(cfg, probe_timeout),
        _check_archiver(cfg, probe_timeout),
        _check_alarm(cfg, probe_timeout),
        _check_naming(cfg, probe_timeout),
        _check_olog(cfg, probe_timeout),
    )
    planes = [live, channelfinder, archiver, alarm, naming, olog]
    ok = not any(plane.status in _FAILING_STATUSES for plane in planes)
    return DoctorReport(planes=planes, privacy=_privacy_report(cfg), ok=ok)
