"""Read-only client for the EPICS Archiver Appliance REST API.

Two read-only jobs, verified against the Archiver Appliance docs
(epicsarchiver.readthedocs.io / archiver-appliance user guide):

  GET {root}/mgmt/bpl/getPVStatus?pv={pv}                              — is a PV archived?
  GET {root}/retrieval/data/getData.json?pv={pv}&from={iso}&to={iso}  — historical samples

Times are **ISO-8601** (e.g. ``2026-06-01T00:00:00.000Z``) per the docs — NOT epoch
milliseconds. ``archiver_url`` is the appliance root (e.g. ``http://archiver:17665``); the
``/mgmt`` and ``/retrieval`` paths are appended. Queries need no authentication by default;
an optional ``Authorization`` header is forwarded for secured deployments.

``get_pv_history`` REQUIRES an explicit ``from``/``to`` window and caps the number of
returned samples — a wide range on a fast PV can otherwise be enormous. Structure mirrors
:mod:`epics_pv_mcp.services.naming_client`.
"""

from __future__ import annotations

from typing import TypedDict

from epics_pv_mcp.services._http import build_retrying_session, rest_get_json
from epics_pv_mcp.services.archiver_exceptions import (
    ArchiverConnectionError,
    ArchiverResponseError,
)

# The status string the Archiver MGMT API reports for an actively-archived PV.
ARCHIVING_STATUS = "Being archived"
# Default cap on returned samples (a wide window on a fast PV is otherwise unbounded).
DEFAULT_MAX_POINTS = 5000


class Sample(TypedDict):
    """One archived sample (the getData.json ``data[]`` element)."""

    secs: int
    nanos: int
    val: object
    severity: int
    status: int


class ArchiverClient:
    """Read-only client for the EPICS Archiver Appliance REST API. GET-only."""

    def __init__(
        self,
        base_url: str,
        timeout: float = 5.0,
        auth_header: str | None = None,
        retrieval_url: str | None = None,
    ) -> None:
        # ``base_url`` is the MGMT root (serves /mgmt/bpl — getPVStatus/is_archived).
        # ``retrieval_url`` is the RETRIEVAL root (serves /retrieval/data — get_pv_history).
        # They coincide in a single-JVM appliance (one port for all webapps), so retrieval_url
        # defaults to base_url. In the ESS 4-instance topology mgmt (:17665) and retrieval (:17668)
        # are SEPARATE Tomcats, so the caller passes a distinct retrieval_url.
        self.base_url = base_url.rstrip("/")
        self.retrieval_url = (retrieval_url or base_url).rstrip("/")
        self.timeout = timeout
        self.session = build_retrying_session(auth_header=auth_header)

    def _get(self, url: str, params: dict[str, str]) -> object:
        """Issue a GET and return parsed JSON, translating failures to Archiver exceptions."""
        return rest_get_json(
            self.session,
            url,
            params,
            self.timeout,
            conn_exc=ArchiverConnectionError,
            resp_exc=ArchiverResponseError,
        )

    def get_pv_status(self, pv: str) -> dict[str, object]:
        """Return the MGMT status record for *pv* (``getPVStatus`` returns a 1-element list)."""
        data = self._get(f"{self.base_url}/mgmt/bpl/getPVStatus", {"pv": pv})
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            return {"pvName": pv, "status": "Unknown"}
        return data[0]

    def is_archived(self, pv: str) -> tuple[bool, str]:
        """Return ``(is_archived, status_string)`` for *pv* (active iff 'Being archived')."""
        status = str(self.get_pv_status(pv).get("status", "Unknown"))
        return status == ARCHIVING_STATUS, status

    def get_pv_history(
        self,
        pv: str,
        start: str,
        end: str,
        max_points: int = DEFAULT_MAX_POINTS,
    ) -> tuple[list[Sample], bool]:
        """Fetch samples for *pv* in [*start*, *end*] (ISO-8601), capped at *max_points*.

        Returns ``(samples, capped)`` where ``capped`` is True if the cap truncated the result.
        """
        data = self._get(
            f"{self.retrieval_url}/retrieval/data/getData.json",
            {"pv": pv, "from": start, "to": end},
        )
        # getData.json returns [{"meta": {...}, "data": [ {secs,nanos,val,severity,status}, ... ]}]
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            return [], False
        raw_samples = data[0].get("data")
        if not isinstance(raw_samples, list):
            return [], False
        capped = len(raw_samples) > max_points
        samples: list[Sample] = []
        for point in raw_samples[:max_points]:
            if not isinstance(point, dict):
                continue
            samples.append(
                Sample(
                    secs=int(point.get("secs", 0)),
                    nanos=int(point.get("nanos", 0)),
                    val=point.get("val"),
                    severity=int(point.get("severity", 0)),
                    status=int(point.get("status", 0)),
                )
            )
        return samples, capped
