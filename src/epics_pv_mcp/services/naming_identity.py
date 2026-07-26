"""Naming Service identity probe (read-only), the single owner of the swagger-beacon fact.

The ESS Naming Service has no ``{"name": ...}`` beacon like the Phoebus trio, but its OpenAPI
contract at ``/rest/swagger.json`` is a static, anonymous 200 whose ``info.title`` names the
service, and the endpoint DISCRIMINATES (measured: Olog answers 401 there, ChannelFinder 404).

Two consumers share this ONE home so they can never drift (the multi-home drift class this repo
keeps fighting):

* ``epics-doctor``'s naming plane (:func:`epics_pv_mcp.services.doctor._identify_naming`) imports
  the title + path constants.
* :meth:`epics_pv_mcp.services.naming_client.NamingServiceClient._get_device_name`'s S13 gate calls
  :func:`probe_naming_identity` to decide whether a definitive "not registered" (204/404) may be
  trusted or must be withheld.

Leaf module by design: it imports only the shared HTTP substrate (:mod:`_http`) and the REST
exception roots, **never** ``doctor`` or ``naming_client``, so ``naming_client → naming_identity``
adds no import cycle (``doctor → naming_client`` already exists).
"""

from __future__ import annotations

from typing import Literal

from epics_pv_mcp.services._http import build_retrying_session, rest_get_json
from epics_pv_mcp.services.rest_exceptions import RestConnectionError, RestResponseError

#: The Naming Service identifies itself in its swagger contract's ``info.title`` (measured live).
#: The title is documentation prose and MAY be reworded by a future release, so a mismatch is
#: ``unverified`` (recognisable-but-unproven), never a hard failure (the honesty ``epics-doctor``
#: applies since S14). Single-sourced HERE so the doctor plane and the client gate cannot diverge.
NAMING_SWAGGER_TITLE = "Naming service API documentation"

#: The anonymous static identity endpoint, appended to the (rstripped) base URL.
NAMING_SWAGGER_PATH = "/rest/swagger.json"

#: ``verified``, proved it is the Naming Service; ``unverified``, answered 2xx but not nameably;
#: ``probe_failed``, the identity request never got a usable answer.
IdentityVerdict = Literal["verified", "unverified", "probe_failed"]


def probe_naming_identity(
    base_url: str, auth_header: str | None = None, timeout: float = 5.0
) -> IdentityVerdict:
    """Ask the configured Naming URL to prove it is the Naming Service via its swagger. TOTAL.

    * ``verified``: ``GET <base>/rest/swagger.json`` answered 2xx and ``info.title`` equals
      :data:`NAMING_SWAGGER_TITLE`.
    * ``unverified``: it answered 2xx but the body is not readable JSON, carries no title, or names
      something else (a title reword, or a non-Naming service that happens to serve *some* swagger).
    * ``probe_failed``: the probe never got a usable 2xx: a served non-2xx (401/404/5xx), a
      transport error, or a refused redirect. ``allow_redirects=False`` keeps the RESPONDING host
      the one we configured, origin integrity, mirroring ``epics-doctor``'s identity beacon; a
      redirect would let another host answer for the URL under test, exactly the confusion S13 rules
      out.

    NEVER raises (``except Exception`` → ``probe_failed``): the S13 gate that calls this rides
    consumers whose NARROWEST catch is ``NamingServiceResponseError`` (crossplane), so a raw
    exception escaping here would crash a best-effort provenance report instead of withholding.
    Classification mirrors ``epics-doctor``'s ``_beacon_reached_but_unreadable``
    split so the two surfaces cannot drift; it returns a plain verdict, not a doctor ``PlaneCheck``.
    """
    url = f"{base_url.rstrip('/')}{NAMING_SWAGGER_PATH}"
    session = build_retrying_session(auth_header=auth_header)
    try:
        payload = rest_get_json(
            session,
            url,
            None,
            timeout,
            conn_exc=RestConnectionError,
            resp_exc=RestResponseError,
            allow_redirects=False,
        )
    except Exception as exc:  # noqa: BLE001 (TOTAL: any failure is a verdict, never a raise)
        # A REACHED-but-unreadable 2xx (a non-JSON body → a ``JSONDecodeError``, which is a
        # ``ValueError`` subclass, raw on the requests<2.27 floor or wrapped as ``__cause__`` on
        # modern requests) is honest ``unverified``; a served non-2xx / transport error / refused
        # redirect never reached a 2xx body and is ``probe_failed``. This is exactly
        # ``epics-doctor``'s ``_beacon_reached_but_unreadable`` predicate, kept in lockstep.
        if isinstance(exc, ValueError) or isinstance(getattr(exc, "__cause__", None), ValueError):
            return "unverified"
        return "probe_failed"
    info = payload.get("info") if isinstance(payload, dict) else None
    title = info.get("title") if isinstance(info, dict) else None
    return "verified" if title == NAMING_SWAGGER_TITLE else "unverified"
