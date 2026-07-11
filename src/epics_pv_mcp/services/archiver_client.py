"""Read-only client for the EPICS Archiver Appliance REST API.

Three read-only jobs, verified against the Archiver Appliance docs
(epicsarchiver.readthedocs.io / archiver-appliance user guide):

  GET {root}/mgmt/bpl/getPVStatus?pv={pv}                              — is a PV archived?
  GET {root}/mgmt/bpl/getPVTypeInfo?pv={pv}                            — HOW is it archived?
  GET {root}/retrieval/data/getData.json?pv={pv}&from={iso}&to={iso}  — historical samples

Times are **ISO-8601** (e.g. ``2026-06-01T00:00:00.000Z``) per the docs — NOT epoch
milliseconds. ``archiver_url`` is the appliance root (e.g. ``http://archiver:17665``); the
``/mgmt`` and ``/retrieval`` paths are appended. Queries need no authentication by default;
an optional ``Authorization`` header is forwarded for secured deployments.

``get_pv_history`` REQUIRES an explicit ``from``/``to`` window and caps the number of
returned samples — a wide range on a fast PV can otherwise be enormous. It returns a
:class:`HistoryResult` (DS-4B) whose ``status`` disambiguates the cases a bare ``[]`` used to
conflate — genuinely-empty history vs. an uninterpretable response. Structure mirrors
:mod:`epics_pv_mcp.services.naming_client`.
"""

from __future__ import annotations

from typing import Literal, TypedDict

from epics_pv_mcp.services._http import build_retrying_session, is_http_404, rest_get_json
from epics_pv_mcp.services.archiver_exceptions import (
    ArchiverConnectionError,
    ArchiverResponseError,
)

# The status string the Archiver MGMT API reports for an actively-archived PV.
ARCHIVING_STATUS = "Being archived"
# Default cap on returned samples (a wide window on a fast PV is otherwise unbounded).
DEFAULT_MAX_POINTS = 5000
# Default cap on returned PV NAMES (getAllPVs on a whole appliance can return tens of thousands).
DEFAULT_MAX_PV_NAMES = 5000

# getPVTypeInfo field -> surfaced snake_case key (the archive CONFIGURATION of a PV). Kept as a
# projection allowlist so an unexpected/free-text field (e.g. ``userParams``) is never surfaced.
_TYPE_INFO_FIELDS: tuple[tuple[str, str], ...] = (
    ("DBRType", "dbr_type"),
    ("samplingMethod", "sampling_method"),
    ("samplingPeriod", "sampling_period"),
    ("computedEventRate", "event_rate"),
    ("computedStorageRate", "storage_rate"),
    ("computedBytesPerEvent", "bytes_per_event"),
    ("elementCount", "element_count"),
    ("archiveFields", "archive_fields"),
    ("dataStores", "data_stores"),  # retention tiers (STS/MTS/LTS) encoded in the store URLs
    ("hostName", "host_name"),
    ("creationTime", "creation_time"),
    ("applianceIdentity", "appliance"),
    ("paused", "paused"),
)


class Sample(TypedDict):
    """One archived sample (the getData.json ``data[]`` element)."""

    secs: int
    nanos: int
    val: object
    severity: int
    status: int


class HistoryResult(TypedDict):
    """The disambiguated result of :meth:`ArchiverClient.get_pv_history` (DS-4B).

    ``status`` tells apart the three cases a bare ``([], False)`` tuple used to conflate:

    * ``"ok"``       — samples were returned (``capped`` says whether the cap truncated them).
    * ``"empty"``    — a VALID response whose ``data`` array was empty: genuinely no samples in
      the window. This is the ONLY empty-``samples`` case with ``withheld_reason=None``.
    * ``"withheld"`` — the response could not be interpreted; ``withheld_reason`` says why
      (``"unexpected_payload"`` / ``"unexpected_sample_shape"``). NOT "no data" — the sample
      history is unknown, not proven empty.

    ``meta`` is the getData.json ``meta`` block (EGU/PREC) when present, else ``{}``. ``note`` is a
    human-readable explanation (``""`` on ``"ok"``).
    """

    samples: list[Sample]
    capped: bool
    meta: dict[str, object]
    status: Literal["ok", "empty", "withheld"]
    note: str
    withheld_reason: str | None


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
        # defaults to base_url. In a split deployment mgmt (:17665) and retrieval (:17668) are
        # SEPARATE Tomcats, so the caller passes a distinct retrieval_url. (A retrieval-cluster-
        # aware appliance cluster proxies internally, so one URL covers all members — see the
        # epics-pv://guide resource.)
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

    def check_connectivity(self) -> bool:
        """Return True if the appliance MGMT API answers; raise on failure.

        Probes ``/mgmt/bpl/getApplianceInfo`` (no PV needed) — this proves the MGMT webapp actually
        ANSWERS, not merely that the port is open. A transport/TLS failure raises
        ArchiverConnectionError; a *served* non-2xx (e.g. ``EPICS_MCP_ARCHIVER_URL`` points at the
        RETRIEVAL webapp, or a 5xx) raises ArchiverResponseError with the HTTP status on
        ``__cause__`` — so a caller (doctor) can tell "unreachable" from "reachable but wrong
        endpoint" via :func:`is_ssl_error` / :func:`http_status`. This requires a 2xx (unlike the
        HEAD-based CF/Alarm/Olog probes) — so the archiver has a third "api_error" doctor bucket.
        """
        self._get(f"{self.base_url}/mgmt/bpl/getApplianceInfo", {})
        return True

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

    def get_archive_status(self, pv: str) -> dict[str, object]:
        """Return the archive status of *pv* enriched with the already-fetched MGMT record fields.

        DS-4A: ``getPVStatus`` already returns the FULL MGMT record but ``is_archived`` reads only
        ``status``. This surfaces the useful extra fields at ~zero cost (the SAME single call):
        ``connection_state`` (is the source IOC connected right now), ``last_event`` (time of the
        last archived sample), ``is_monitored`` (monitored vs. scanned), ``sampling_period`` and
        ``appliance``. Fields the record does not carry (e.g. the ``Unknown``/paused fallback) are
        OMITTED rather than surfaced as ``null`` noise. ``archived``/``status`` are always present.
        The Archiver MGMT record carries no person data, so no allowlist redaction is needed here.
        """
        record = self.get_pv_status(pv)
        status = str(record.get("status", "Unknown"))
        result: dict[str, object] = {"archived": status == ARCHIVING_STATUS, "status": status}
        for source_key, out_key in (
            ("connectionState", "connection_state"),
            ("lastEvent", "last_event"),
            ("isMonitored", "is_monitored"),
            ("samplingPeriod", "sampling_period"),
            ("appliance", "appliance"),
        ):
            if source_key in record:
                result[out_key] = record[source_key]
        return result

    def get_pv_type_info(self, pv: str) -> dict[str, object]:
        """Return the archive CONFIGURATION of *pv* (Archiver MGMT ``getPVTypeInfo``).

        Distinct from :meth:`get_archive_status` (which reports the live connection/last-event
        state from ``getPVStatus``): this surfaces HOW the PV is archived — sampling
        (method/period), retention (the STS/MTS/LTS ``data_stores``), computed event/storage
        rates, ``dbr_type``, the archived ``archive_fields``, source ``host_name`` and
        ``creation_time`` — projected onto snake_case keys via the :data:`_TYPE_INFO_FIELDS`
        allowlist (so a free-text field like ``userParams`` is never surfaced). Fields the record
        lacks are OMITTED, not surfaced as ``null``.

        Returns ``{"found": True, ...}`` when the appliance has a type-info record for *pv*, or
        ``{"found": False}`` when it has none (unknown / never-archived PV). The appliance signals
        the latter with **HTTP 404** on this endpoint — unlike ``getPVStatus``, which 200s "Not
        being archived" — so a 404 is mapped to ``found: False`` here; every OTHER failure (5xx,
        bad JSON, an unreachable appliance) PROPAGATES, so a could-not-read is never silently
        reported as "not archived". The MGMT record carries no person data, so no allowlist
        redaction beyond the field projection is needed.
        """
        try:
            data = self._get(f"{self.base_url}/mgmt/bpl/getPVTypeInfo", {"pv": pv})
        except ArchiverResponseError as exc:
            # rest_get_json wraps the HTTP error as ArchiverResponseError (``raise ... from exc``),
            # so a chained 404 is the appliance's definitive "no type-info record" answer. Any
            # other status / bad payload re-raises (a connection failure is an ArchiverConnection-
            # Error and never reaches this handler) — could-not-read must stay could-not-read.
            if is_http_404(exc):
                return {"found": False}
            raise
        record = self._unwrap_type_info(data)
        if record is None:
            return {"found": False}
        result: dict[str, object] = {"found": True}
        for source_key, out_key in _TYPE_INFO_FIELDS:
            if source_key in record:
                result[out_key] = record[source_key]
        return result

    @staticmethod
    def _unwrap_type_info(data: object) -> dict[str, object] | None:
        """Normalize the getPVTypeInfo payload to a record dict, or ``None`` when there is none.

        Appliance versions return either a bare object or a 1-element list (like getPVStatus). An
        empty object / empty list / non-mapping payload means "no type info for this PV".
        """
        if isinstance(data, dict):
            return data or None  # an empty {} means "unknown PV" -> not found
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0] or None
        return None

    def get_all_pvs(
        self, pattern: str | None = None, limit: int = DEFAULT_MAX_PV_NAMES
    ) -> tuple[list[str], bool]:
        """List the PV names the WHOLE appliance archives (Archiver MGMT ``getAllPVs``).

        ``getAllPVs`` returns a bare JSON array of PV-name strings. Optional *pattern* is a glob
        forwarded as the endpoint's ``pv`` param (e.g. ``DEV-TEST01:*``); *limit* caps it. Returns
        ``(names, capped)`` — ``capped`` is True when the appliance held more than *limit* (we fetch
        ``limit + 1`` then slice, so the flag is honest even if the server ignores ``limit``). The
        MGMT enumeration endpoint — NOT ``getMatchingPVs``, which 404s on split/proxied deployments.
        PV names carry no person data, so no allowlist redaction is needed.
        """
        params: dict[str, str] = {"limit": str(limit + 1)}
        if pattern:
            params["pv"] = pattern
        return self._coerce_pv_names(
            self._get(f"{self.base_url}/mgmt/bpl/getAllPVs", params), limit, "getAllPVs"
        )

    def get_pvs_for_this_appliance(
        self, limit: int = DEFAULT_MAX_PV_NAMES
    ) -> tuple[list[str], bool]:
        """List the PV names THIS appliance member archives (MGMT ``getPVsForThisAppliance``).

        Like :meth:`get_all_pvs` but scoped to the local cluster member (no pattern). Same
        ``(names, capped)`` contract; the MGMT enumeration endpoint, NOT ``getMatchingPVs``.
        """
        params = {"limit": str(limit + 1)}
        return self._coerce_pv_names(
            self._get(f"{self.base_url}/mgmt/bpl/getPVsForThisAppliance", params),
            limit,
            "getPVsForThisAppliance",
        )

    @staticmethod
    def _coerce_pv_names(data: object, limit: int, endpoint: str) -> tuple[list[str], bool]:
        """Validate a bare-list PV-name payload → ``(names[:limit], capped)``."""
        # Clamp a non-positive limit to >=1 (mirrors get_pv_history's max_points guard): a negative
        # limit would make names[:limit] silently DROP names and len(names) > limit falsely True —
        # defense-in-depth for a direct caller (the tool boundary also enforces ge=1).
        limit = max(limit, 1)
        if not isinstance(data, list):
            raise ArchiverResponseError(f"{endpoint} returned a non-list payload")
        names = [str(item) for item in data]
        return (names[:limit], len(names) > limit)

    def get_pv_history(
        self,
        pv: str,
        start: str,
        end: str,
        max_points: int = DEFAULT_MAX_POINTS,
    ) -> HistoryResult:
        """Fetch samples for *pv* in [*start*, *end*] (ISO-8601), capped at *max_points*.

        Returns a :class:`HistoryResult` (DS-4B). ``capped`` is True if the cap truncated the
        result; ``meta`` is the getData.json ``meta`` block — PV metadata (``EGU`` units, ``PREC``
        precision) that was previously discarded; ``meta`` is ``{}`` when there is none. ``status``
        disambiguates empty ``samples``: ``"empty"`` (a valid but sample-less window) vs.
        ``"withheld"`` (an uninterpretable response), so an empty ``samples`` is never silently
        read as "no data" when the truth is "could not read".
        """
        # Defensive clamp: a non-positive cap is meaningless. Without it ``raw_samples[:0] == []``
        # would empty a perfectly valid, non-empty response and the code below would then mislabel
        # it "withheld" (unexpected_sample_shape); a negative cap would silently drop the last
        # sample. The tool boundary also guards this (Field ge=1), so this is defense-in-depth for
        # any direct caller.
        max_points = max(max_points, 1)
        data = self._get(
            f"{self.retrieval_url}/retrieval/data/getData.json",
            {"pv": pv, "from": start, "to": end},
        )
        # getData.json returns [{"meta": {...}, "data": [ {secs,nanos,val,severity,status}, ... ]}]
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            return self._withheld_history(
                {},
                "unexpected_payload",
                "The Archiver returned an unreadable payload (not a [{meta, data}] block); the "
                "sample history is WITHHELD, not reported as empty.",
            )
        block = data[0]
        raw_meta = block.get("meta")
        meta: dict[str, object] = raw_meta if isinstance(raw_meta, dict) else {}
        raw_samples = block.get("data")
        if not isinstance(raw_samples, list):
            return self._withheld_history(
                meta,
                "unexpected_payload",
                "The Archiver response carried no readable 'data' array; the sample history is "
                "WITHHELD, not reported as empty.",
            )
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
        if not raw_samples:
            return HistoryResult(
                samples=[],
                capped=False,
                meta=meta,
                status="empty",
                note=(
                    "No samples in the requested window. If you expected data, verify the PV is "
                    "archived on this appliance (is_archived / get_archive_info) and check the "
                    "window and its timezone."
                ),
                withheld_reason=None,
            )
        if not samples:
            # A non-empty data array (its first ``max_points >= 1`` elements were inspected) held no
            # readable {secs,...} dicts — the shape is off. ``capped`` reflects the real truncation.
            return self._withheld_history(
                meta,
                "unexpected_sample_shape",
                "The Archiver 'data' array held no readable samples; the history is WITHHELD, "
                "not reported as empty.",
                capped=capped,
            )
        return HistoryResult(
            samples=samples, capped=capped, meta=meta, status="ok", note="", withheld_reason=None
        )

    @staticmethod
    def _withheld_history(
        meta: dict[str, object], reason: str, note: str, *, capped: bool = False
    ) -> HistoryResult:
        """Build a WITHHELD :class:`HistoryResult` (no samples proven, a reason attached).

        *capped* reflects whether the raw response held more elements than the cap (True only for
        the sample-shape case, where a large all-junk array was truncated); an unreadable payload
        counted nothing, so it stays False.
        """
        return HistoryResult(
            samples=[],
            capped=capped,
            meta=meta,
            status="withheld",
            note=note,
            withheld_reason=reason,
        )
