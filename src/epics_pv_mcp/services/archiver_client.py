"""Read-only client for the EPICS Archiver Appliance REST API.

Three read-only jobs, verified against the Archiver Appliance docs
(epicsarchiver.readthedocs.io / archiver-appliance user guide):

  GET {root}/mgmt/bpl/getPVStatus?pv={pv}                              — is a PV archived?
  GET {root}/mgmt/bpl/getPVTypeInfo?pv={pv}                            — HOW is it archived?
  GET {root}/retrieval/data/getData.json?pv={pv}&from={iso}&to={iso}  — historical samples

Times are **ISO-8601 with a zone** (e.g. ``2026-06-01T00:00:00.000Z``) — NOT epoch milliseconds,
and NOT any of the other notations the sibling planes take. MEASURED (2026-07-15, live): a naive
ISO, a space-separated wall clock, a bare date and a relative amount (``7 days``) each answer
**HTTP 500** — loudly, unlike Olog/Alarm, which read an unreadable time as *now* and answer 200
with an empty list. :func:`~epics_pv_mcp.services.archiver_time.normalize_archiver_time` converts
every absolute notation to the one form this server reads and refuses a relative amount by name.
``archiver_url`` is the appliance root (e.g. ``http://archiver:17665``); the
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

from epics_pv_mcp.services._http import get_shared_session, is_http_404, rest_get_json
from epics_pv_mcp.services.archiver_exceptions import (
    ArchiverConnectionError,
    ArchiverResponseError,
)
from epics_pv_mcp.services.archiver_time import normalize_archiver_time

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
    # AR-D: alarm/display/control limits + unit/precision — fetched in the SAME getPVTypeInfo call
    # and previously discarded. The appliance renames the EPICS dbr fields to its own camelCase
    # (PVTypeInfo.java) and serializes every numeric limit as a STRING (JSONEncoder .toString()).
    # ⚠️ These nine numeric limits are autoboxed ``double`` (never null) → ALWAYS present, reading
    # ``"0.0"`` when the PV had no ctrl info. So ``"0.0"`` can mean "no limit configured", not a
    # literal zero — the guard cannot omit them. ``units`` (a String) IS omitted when null.
    ("upperAlarmLimit", "upper_alarm_limit"),  # HIHI
    ("upperWarningLimit", "upper_warning_limit"),  # HIGH
    ("lowerWarningLimit", "lower_warning_limit"),  # LOW
    ("lowerAlarmLimit", "lower_alarm_limit"),  # LOLO
    ("upperDisplayLimit", "upper_display_limit"),  # HOPR
    ("lowerDisplayLimit", "lower_display_limit"),  # LOPR
    ("upperCtrlLimit", "upper_ctrl_limit"),  # DRVH
    ("lowerCtrlLimit", "lower_ctrl_limit"),  # DRVL
    ("precision", "precision"),  # PREC
    ("units", "units"),  # EGU
    # AR-D: cheap owner-relevant config from the same record (omitted when the field is null).
    ("controllingPV", "controlling_pv"),  # the PV that gates/decimates this one's archiving, if any
    ("policyName", "policy_name"),  # which archiving policy applies
    ("modificationTime", "modification_time"),  # config last changed (pairs with creation_time)
)

# getApplianceInfo field -> surfaced snake_case key (Fundort 3). The MGMT ``getApplianceInfo`` body
# is the appliance's own topology: its ``identity`` plus the per-plane root URLs (mgmt/engine/etl/
# retrieval/dataRetrieval) and the ``clusterInetPort``, plus a ``version`` string the handler adds
# from version.txt. doctor reads only ``identity`` and discards the rest; this surfaces the whole
# body so an agent can tell WHICH appliance/cluster it is on (enumerating the wrong cluster yields a
# complete-looking list of the wrong PVs) and WHICH plane is served WHERE (the split/proxied-vs-
# single-JVM question behind the mgmt-:17665 / retrieval-:17668 confusion). Kept as a projection
# allowlist so an unexpected field is never surfaced. Field names are the vendor getApplianceInfo
# JSON body (GetApplianceInfo.java / ApplianceInfo.java: JSONEncoder introspects the ApplianceInfo
# bean, so the wire keys equal the field names) — a JSON CONTRACT, not a live measurement.
_APPLIANCE_INFO_FIELDS: tuple[tuple[str, str], ...] = (
    ("identity", "identity"),
    ("mgmtURL", "mgmt_url"),
    ("engineURL", "engine_url"),
    ("retrievalURL", "retrieval_url"),
    ("etlURL", "etl_url"),
    ("dataRetrievalURL", "data_retrieval_url"),
    ("clusterInetPort", "cluster_inet_port"),
    # Added by the handler from ui/comm/version.txt; OMIT-WHEN-NULL (an older appliance may lack
    # it), so never promised always-present. Distinct from the retrieval-probe product-version.
    ("version", "version"),
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


def _readable_sample(point: object) -> Sample | None:
    """Parse one getData.json sample point, or ``None`` when it is unreadable (S11).

    Measured (ESS appliance 2.2.1, 50 live samples): every sample carries
    ``secs``/``nanos``/``val``/``severity``/``status``. The REQUIRED anchors are ``secs`` and
    ``val``; the int fields must be int-coercible when present (an absent
    ``nanos``/``severity``/``status`` defaults to 0 — deliberately not required, so a sparse
    server variant stays readable). The old code accepted ANY dict and minted missing fields as
    0/None — two different broken dicts both became the same plausible sample (auditor probe) —
    and a non-int-coercible field crashed with an uncaught ValueError.
    """
    if not isinstance(point, dict) or "secs" not in point or "val" not in point:
        return None
    try:
        return Sample(
            secs=int(point["secs"]),
            nanos=int(point.get("nanos", 0)),
            val=point["val"],
            severity=int(point.get("severity", 0)),
            status=int(point.get("status", 0)),
        )
    except (TypeError, ValueError):
        return None


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
        self.session = get_shared_session(auth_header=auth_header)

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
        """Return the MGMT status record for *pv* (``getPVStatus`` returns a 1-element list).

        S11: the payload must be a non-empty list whose first element is a dict carrying a
        string ``status``. Measured (ESS appliance 2.2.1): even an UNKNOWN pv gets a REAL record
        (``{"pvName": …, "status": "Not being archived"}``) — so ``[]``/junk is out of contract
        and RAISES. The old code minted a synthetic ``{"status": "Unknown"}`` record, which the
        callers turned into the definitive ``(False, "Unknown")`` / ``archived: false`` (auditor
        probe ARCHIVER_IS_ARCHIVED_BAD_2XX).
        """
        data = self._get(f"{self.base_url}/mgmt/bpl/getPVStatus", {"pv": pv})
        if (
            not isinstance(data, list)
            or not data
            or not isinstance(data[0], dict)
            or not isinstance(data[0].get("status"), str)
            or not data[0]["status"]  # an EMPTY status is no measured value either
        ):
            raise ArchiverResponseError(
                "getPVStatus returned an unreadable payload (expected a 1-element list whose "
                f"record carries a non-empty string 'status', got {type(data).__name__}); "
                "the archive status is not readable — this is NOT a 'not archived'."
            )
        return data[0]

    def is_archived(self, pv: str) -> tuple[bool, str]:
        """Return ``(is_archived, status_string)`` for *pv* (active iff 'Being archived')."""
        status = str(self.get_pv_status(pv)["status"])  # validated by get_pv_status (S11)
        return status == ARCHIVING_STATUS, status

    def get_archive_status(self, pv: str) -> dict[str, object]:
        """Return the archive status of *pv* enriched with the already-fetched MGMT record fields.

        DS-4A: ``getPVStatus`` already returns the FULL MGMT record but ``is_archived`` reads only
        ``status``. This surfaces the useful extra fields at ~zero cost (the SAME single call):
        ``connection_state`` (is the source IOC connected right now), ``last_event`` (time of the
        last archived sample), ``is_monitored`` (monitored vs. scanned), ``sampling_period`` and
        ``appliance``. AR-D adds the connection-history diagnostic cluster from the same record:
        ``connection_loss_regain_count`` (how often the IOC connection dropped — "does this PV
        flap?"), ``connection_first_established`` and ``connection_last_restablished`` (first/last
        (re)connect time; ``"Never"`` if the connection never dropped). Fields the record does not
        carry (e.g. the ``Unknown``/paused fallback, or the whole cluster for a non-archived PV) are
        OMITTED rather than surfaced as ``null`` noise. ``archived``/``status`` are always present.
        The Archiver MGMT record carries no person data, so no allowlist redaction is needed here.

        Field names + the all-string value contract are the Archiver Appliance getPVStatus response
        (vendor source ``EngineChannelStatus.java``); ``connection_last_restablished`` mirrors the
        appliance's upstream typo verbatim so the projection key matches the wire key.
        """
        record = self.get_pv_status(pv)
        status = str(record["status"])  # validated by get_pv_status (S11)
        result: dict[str, object] = {"archived": status == ARCHIVING_STATUS, "status": status}
        for source_key, out_key in (
            ("connectionState", "connection_state"),
            ("lastEvent", "last_event"),
            ("isMonitored", "is_monitored"),
            ("samplingPeriod", "sampling_period"),
            ("appliance", "appliance"),
            # AR-D: the connection-history cluster — already fetched in the SAME getPVStatus record,
            # answers "does this PV flap, and when did it last (re)connect?". Present only for
            # actively-archived PVs (the ``if source_key in record`` guard omits it otherwise).
            # ``connectionLastRestablished`` carries an UPSTREAM TYPO (missing the second 'e') — the
            # left-hand source key must match the appliance verbatim (EngineChannelStatus.java:127).
            ("connectionLossRegainCount", "connection_loss_regain_count"),
            ("connectionFirstEstablished", "connection_first_established"),
            ("connectionLastRestablished", "connection_last_restablished"),
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

        AR-D also surfaces the alarm/display/control limits + unit/precision (``upper_alarm_limit``
        = HIHI, ``upper_warning_limit`` = HIGH, ``lower_*`` too, ``upper_display_limit`` = HOPR,
        ``*_ctrl_limit`` = DRVH/DRVL, ``precision``, ``units`` = EGU) and cheap config
        (``controlling_pv``, ``policy_name``, ``modification_time``). ⚠️ The nine numeric limits are
        ALWAYS present and read ``"0.0"`` when the PV lacks ctrl info — ``"0.0"`` may mean "no limit
        configured", not a literal zero. Field names + the all-string values are the Archiver
        Appliance getPVTypeInfo response (vendor source ``PVTypeInfo.java``).

        Returns ``{"found": True, ...}`` when the appliance has a type-info record for *pv*, or
        ``{"found": False}`` when it has none (unknown / never-archived PV). The appliance signals
        the latter with **HTTP 404** on this endpoint — unlike ``getPVStatus``, which 200s "Not
        being archived" — and ONLY with that 404 (S11): a 2xx whose body is not a readable
        type-info record RAISES like every other failure (5xx, bad JSON, an unreachable
        appliance), so a could-not-read is never silently reported as "not archived" — and an
        unrelated non-empty dict is never projected as a fabricated ``found: True``. The MGMT
        record carries no person data, so no allowlist redaction beyond the field projection is
        needed.
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
        record = self._require_type_info_record(data)
        result: dict[str, object] = {"found": True}
        for source_key, out_key in _TYPE_INFO_FIELDS:
            if source_key in record:
                result[out_key] = record[source_key]
        return result

    @staticmethod
    def _require_type_info_record(data: object) -> dict[str, object]:
        """Normalize the getPVTypeInfo payload to its record dict, or RAISE (S11).

        Appliance versions return either a bare object or a 1-element list (like getPVStatus);
        the measured record (ESS 2.2.1) always carries ``pvName`` — the schema anchor. The old
        ``_unwrap_type_info`` mapped ``{}``/``[]``/junk to ``found: False`` (conflating
        "could not read" with the appliance's definitive 404) and accepted ANY non-empty dict as
        the record — the auditor probe ``{"unexpected": "shape"}`` came back as a fabricated
        ``found: True``. The unknown-PV signal on this endpoint is HTTP 404 and only that.
        """
        record: object = data
        if isinstance(record, list) and len(record) == 1:
            record = record[0]
        if isinstance(record, dict) and isinstance(record.get("pvName"), str) and record["pvName"]:
            return record
        raise ArchiverResponseError(
            "getPVTypeInfo returned an unreadable payload (expected a type-info record carrying "
            f"a non-empty 'pvName', got {type(data).__name__}); "
            "this is NOT the 404 'no record' signal."
        )

    def get_appliance_info(self) -> dict[str, object]:
        """Return the appliance's own topology (Archiver MGMT ``getApplianceInfo``) — Fundort 3.

        The MGMT ``getApplianceInfo`` body names WHICH appliance this is (``identity``) and where
        each plane is served (``mgmt_url``/``engine_url``/``etl_url``/``retrieval_url``/
        ``data_retrieval_url``, ``cluster_inet_port``), plus a ``version`` string. doctor's
        ``_identify_archiver`` fetches the SAME body but keeps only ``identity`` and throws the rest
        away; this surfaces the whole body via the :data:`_APPLIANCE_INFO_FIELDS` projection
        allowlist (an unexpected field is never surfaced; a field the body lacks — e.g. ``version``
        on a pre-version.txt appliance — is OMITTED, not surfaced as ``null``). It answers the two
        questions doctor's identity-only check cannot serve to a tool caller: "am I pointed at the
        intended cluster (before I trust list_archived_pvs / get_pv_history)?" and "is this a
        split/proxied deployment — which plane is served where?".

        No not-found duality (there is no PV): a served non-2xx PROPAGATES as an ArchiverError —
        deliberately UNLIKE getPVTypeInfo, whose 404 means "PV not archived". A 404 here means the
        WRONG endpoint (the retrieval webapp serves ``/retrieval/bpl``, not ``/mgmt/bpl``), not
        "no appliance". A 2xx whose body carries no non-empty ``identity`` string RAISES rather than
        fabricating an empty success (the getPVStatus/getPVTypeInfo S11 anchor discipline).

        The URLs embed internal cluster-member host names; this is surfaced un-redacted BY DESIGN —
        the body is pure technical infra (no person data), consistent with the shipped
        ``host_name``/``data_stores`` fields of :meth:`get_pv_type_info`. Field names + the
        all-string value contract are the vendor getApplianceInfo JSON body
        (``GetApplianceInfo.java`` / ``ApplianceInfo.java``) — a JSON CONTRACT, not a measurement.
        """
        record = self._require_appliance_record(
            self._get(f"{self.base_url}/mgmt/bpl/getApplianceInfo", {})
        )
        result: dict[str, object] = {}
        for source_key, out_key in _APPLIANCE_INFO_FIELDS:
            if source_key in record:
                result[out_key] = record[source_key]
        return result

    @staticmethod
    def _require_appliance_record(data: object) -> dict[str, object]:
        """Return the getApplianceInfo record dict, or RAISE (S11 anchor discipline).

        The vendor body is a single JSON object whose ``identity`` is the always-present anchor
        (the appliance names itself; doctor requires it non-empty too). A non-dict, a list wrapper
        (out of contract — unlike getPVTypeInfo, getApplianceInfo is never list-wrapped), or a dict
        without a non-empty string ``identity`` is not a getApplianceInfo record and RAISES — a
        wrong-endpoint 200 must never be projected as a valid-but-empty appliance.
        """
        if isinstance(data, dict) and isinstance(data.get("identity"), str) and data["identity"]:
            return data
        raise ArchiverResponseError(
            "getApplianceInfo returned an unreadable payload (expected a single object carrying a "
            f"non-empty 'identity', got {type(data).__name__}); the MGMT endpoint may be wrong "
            "(the retrieval webapp serves /retrieval/bpl, not /mgmt/bpl)."
        )

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
        # S11: a non-string item must raise — str() used to mint junk (dicts, numbers) into
        # plausible PV "names". Measured (ESS 2.2.1): a bare array of strings, nothing else.
        names: list[str] = []
        for item in data:
            if not isinstance(item, str):
                raise ArchiverResponseError(
                    f"{endpoint} returned a non-string item (got {type(item).__name__}) — "
                    "refusing to mint it into a PV name."
                )
            names.append(item)
        return (names[:limit], len(names) > limit)

    def get_pv_history(
        self,
        pv: str,
        start: str,
        end: str,
        max_points: int = DEFAULT_MAX_POINTS,
    ) -> HistoryResult:
        """Fetch samples for *pv* in [*start*, *end*] (ISO-8601), capped at *max_points*.

        *start*/*end* are normalized to zone-explicit ISO first — this server reads NOTHING else
        (measured: a naive ISO, a wall clock, a bare date and ``7 days`` are each an HTTP 500), so
        a relative amount is refused here by name instead of becoming an opaque server error.

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
            {
                "pv": pv,
                # param= names the TOOL argument (start/end), not the wire key (from/to): this is
                # the one plane where they differ, and an error must name what the caller typed.
                "from": normalize_archiver_time(start, param="start"),
                "to": normalize_archiver_time(end, param="end"),
            },
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
        capped = len(raw_samples) > max_points
        samples: list[Sample] = []
        for point in raw_samples[:max_points]:
            sample = _readable_sample(point)
            if sample is None:
                # S11: ONE unreadable element withholds the WHOLE result — silently skipping junk
                # fabricated a smaller history that read as the complete answer, and any dict at
                # all used to be accepted with missing fields minted as 0/None. ``capped``
                # reflects the real truncation of the raw array.
                return self._withheld_history(
                    meta,
                    "unexpected_sample_shape",
                    "The Archiver 'data' array held an unreadable sample; the history is "
                    "WITHHELD, not reported as empty or partial.",
                    capped=capped,
                )
            samples.append(sample)
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
