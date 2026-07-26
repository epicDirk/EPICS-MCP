"""Offline tests for the Archiver Appliance client + tools (no network)."""

from unittest.mock import Mock

import pytest
import requests

from epics_pv_mcp.config import EpicsConfig
from epics_pv_mcp.errors import EpicsConnectionError, EpicsError
from epics_pv_mcp.services._http import http_status, is_ssl_error
from epics_pv_mcp.services._time_window import TimeWindowFormatError
from epics_pv_mcp.services.alarm_time import normalize_alarm_time
from epics_pv_mcp.services.archiver_client import ArchiverClient, HistoryResult, Sample
from epics_pv_mcp.services.archiver_exceptions import (
    ArchiverConnectionError,
    ArchiverError,
    ArchiverResponseError,
)
from epics_pv_mcp.services.archiver_time import normalize_archiver_time
from epics_pv_mcp.services.checkers import _archiver_error_code
from epics_pv_mcp.tools.archiver import (
    _DISABLED_NOTE,
    _get_appliance_info,
    _get_archive_info,
    _get_pv_history,
    _is_archived,
    _list_archived_pvs,
)

# A valid window for the tests that are about something else (payload shapes, caps, errors).
# These were "a"/"b" until the client gained a time contract, placeholders that no layer ever
# looked at, and that a real Archiver answers with an HTTP 500. They stayed green only because
# nothing validated them, which is the same blind spot the normalization closes.
_T0 = "2026-06-01T00:00:00Z"
_T1 = "2026-06-02T00:00:00Z"


def _resp(payload: object, *, ok: bool = True) -> Mock:
    resp = Mock()
    resp.json.return_value = payload
    if ok:
        resp.raise_for_status.return_value = None
    else:
        resp.raise_for_status.side_effect = requests.exceptions.HTTPError("500")
    return resp


# --- client: is_archived / get_archive_status ---


def test_is_archived_true(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ArchiverClient("http://arch:17665")
    monkeypatch.setattr(
        client.session,
        "get",
        Mock(return_value=_resp([{"pvName": "X", "status": "Being archived"}])),
    )
    archived, status = client.is_archived("X")
    assert archived is True
    assert status == "Being archived"


def test_is_archived_false(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ArchiverClient("http://arch")
    monkeypatch.setattr(
        client.session, "get", Mock(return_value=_resp([{"pvName": "X", "status": "Paused"}]))
    )
    archived, status = client.is_archived("X")
    assert archived is False
    assert status == "Paused"


def test_is_archived_unknown_pv_record_is_the_definitive_negative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive control for the measured definitive signal: the appliance answers an UNKNOWN pv
    on getPVStatus with a REAL record (measured live, ESS 2.2.1:
    ``[{"pvName": ..., "status": "Not being archived"}]``), never with ``[]`` or an empty body.
    That record is the definitive negative and stays one; only unreadable payloads raise (S11).
    """
    client = ArchiverClient("http://arch")
    monkeypatch.setattr(
        client.session,
        "get",
        Mock(return_value=_resp([{"pvName": "ZZZ:X", "status": "Not being archived"}])),
    )
    archived, status = client.is_archived("ZZZ:X")
    assert archived is False
    assert status == "Not being archived"


# --- client: strict response schema (S11), unreadable 2xx is NEVER a definitive answer ---


@pytest.mark.parametrize(
    "payload",
    [{}, [], "nope", 123, [123], [{"unexpected": "shape"}], [{"status": 7}], [{"status": ""}]],
    ids=[
        "dict",
        "empty-list",
        "string",
        "number",
        "non-dict-element",
        "record-without-status",
        "non-str-status",
        "empty-status",
    ],
)
def test_is_archived_unreadable_2xx_raises(
    monkeypatch: pytest.MonkeyPatch, payload: object
) -> None:
    """S11: an unreadable getPVStatus payload must RAISE, it used to become a synthetic
    ``{"status": "Unknown"}`` record and thus the definitive ``(False, "Unknown")`` (auditor
    probe ARCHIVER_IS_ARCHIVED_BAD_2XX). Measured: even an unknown PV gets a real record, so
    ``[]`` is out of contract too."""
    client = ArchiverClient("http://arch")
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(payload)))
    with pytest.raises(ArchiverResponseError):
        client.is_archived("X")


@pytest.mark.parametrize("payload", [{}, [{"unexpected": "shape"}]], ids=["dict", "no-status"])
def test_get_archive_status_unreadable_2xx_raises(
    monkeypatch: pytest.MonkeyPatch, payload: object
) -> None:
    """S11: same guard through the enriched sibling (``archived: false, status: "Unknown"`` was
    the fabricated tool answer, auditor probe ARCHIVER_STATUS_BAD_2XX)."""
    client = ArchiverClient("http://arch")
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(payload)))
    with pytest.raises(ArchiverResponseError):
        client.get_archive_status("X")


# --- client: get_pv_history (DS-4B extended return contract) ---


def test_get_pv_history_status_ok_projects_and_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = [
        {
            "meta": {"name": "X"},
            "data": [
                {"secs": 1, "nanos": 0, "val": 1.0, "severity": 0, "status": 0},
                {"secs": 2, "nanos": 0, "val": 2.0, "severity": 1, "status": 0},
                {"secs": 3, "nanos": 0, "val": 3.0, "severity": 0, "status": 0},
            ],
        }
    ]
    client = ArchiverClient("http://arch")
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(raw)))
    result = client.get_pv_history(
        "X", "2026-06-01T00:00:00Z", "2026-06-02T00:00:00Z", max_points=2
    )
    assert result["status"] == "ok"
    assert result["withheld_reason"] is None
    assert result["capped"] is True
    assert len(result["samples"]) == 2
    assert result["samples"][0]["secs"] == 1
    assert result["samples"][1]["val"] == 2.0
    assert result["meta"] == {"name": "X"}  # DS-4A: getData.json meta block (EGU/PREC)


def test_get_pv_history_status_empty_keeps_meta(monkeypatch: pytest.MonkeyPatch) -> None:
    """DS-4B: a VALID response with an empty data array is genuinely-empty history (status
    'empty', no withheld_reason), NOT conflated with a malformed response. The meta block
    (units/precision) is still surfaced for the window."""
    raw = [{"meta": {"name": "X", "EGU": "V", "PREC": "2"}, "data": []}]
    client = ArchiverClient("http://arch")
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(raw)))
    result = client.get_pv_history("X", _T0, _T1)
    assert result["status"] == "empty"
    assert result["withheld_reason"] is None
    assert result["samples"] == []
    assert result["capped"] is False
    assert result["meta"] == {"name": "X", "EGU": "V", "PREC": "2"}


@pytest.mark.parametrize(
    "payload",
    [
        [],  # malformed top-level (empty)
        "nope",  # malformed top-level (not a list)
        [123],  # top-level list whose element is not a dict
        [{"meta": {"name": "X"}}],  # block present but no 'data' key
        [{"meta": {"name": "X"}, "data": "not-a-list"}],  # 'data' is not a list
    ],
)
def test_get_pv_history_withheld_unexpected_payload(
    monkeypatch: pytest.MonkeyPatch, payload: object
) -> None:
    """DS-4B: an uninterpretable response is WITHHELD (status 'withheld',
    withheld_reason 'unexpected_payload'), a bare [] must never masquerade as 'empty history'
    when the truth is 'could not read'."""
    client = ArchiverClient("http://arch")
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(payload)))
    result = client.get_pv_history("X", _T0, _T1)
    assert result["status"] == "withheld"
    assert result["withheld_reason"] == "unexpected_payload"
    assert result["samples"] == []
    assert result["capped"] is False


def test_get_pv_history_meta_none_and_nondict_coerced(monkeypatch: pytest.MonkeyPatch) -> None:
    """A present-but-non-dict meta (JSON null / list) coerces to {} and does NOT make the result
    withheld, the data array is still the source of truth for empty/ok."""
    payloads: list[object] = [[{"meta": None, "data": []}], [{"meta": ["x"], "data": []}]]
    for payload in payloads:
        client = ArchiverClient("http://arch")
        monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(payload)))
        result = client.get_pv_history("X", _T0, _T1)
        assert result["meta"] == {}
        assert result["status"] == "empty"
        assert result["withheld_reason"] is None


def test_get_pv_history_withheld_unexpected_sample_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DS-4B: a non-empty data array holding no readable {secs,...} dicts is withheld
    ('unexpected_sample_shape'), not silently reported as ok with zero samples."""
    raw = [{"meta": {"name": "X"}, "data": ["junk", 42, None]}]
    client = ArchiverClient("http://arch")
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(raw)))
    result = client.get_pv_history("X", _T0, _T1)
    assert result["status"] == "withheld"
    assert result["withheld_reason"] == "unexpected_sample_shape"
    assert result["samples"] == []


@pytest.mark.parametrize("max_points", [0, -1])
def test_get_pv_history_nonpositive_max_points_not_withheld(
    monkeypatch: pytest.MonkeyPatch, max_points: int
) -> None:
    """DS-4B regression: a non-positive cap must NOT turn a valid, readable response into
    status='withheld' / drop samples. The cap is clamped to >=1, so one sample survives and the
    result is honest ('ok'), never the false 'unexpected_sample_shape'."""
    raw = [
        {
            "meta": {"name": "X"},
            "data": [{"secs": 1, "nanos": 0, "val": 1.5, "severity": 0, "status": 0}],
        }
    ]
    client = ArchiverClient("http://arch")
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(raw)))
    result = client.get_pv_history("X", _T0, _T1, max_points=max_points)
    assert result["status"] == "ok"
    assert result["withheld_reason"] is None
    assert len(result["samples"]) == 1
    assert result["samples"][0]["val"] == 1.5


def test_get_pv_history_mixed_valid_and_junk_withholds(monkeypatch: pytest.MonkeyPatch) -> None:
    """S11 (flips the former 'mixed junk is ok' pin): ONE unreadable element in the data array
    withholds the WHOLE result. Silently skipping junk fabricated a smaller history that read as
    the complete answer, the auditor fed two DIFFERENT broken arrays and both came back
    ``status=ok``. Withheld ≠ no: the caller learns the window could not be read."""
    raw = [
        {
            "meta": {"name": "X"},
            "data": [
                {"secs": 1, "nanos": 0, "val": 1.0, "severity": 0, "status": 0},
                "junk",
                None,
                {"secs": 2, "nanos": 0, "val": 2.0, "severity": 0, "status": 0},
            ],
        }
    ]
    client = ArchiverClient("http://arch")
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(raw)))
    result = client.get_pv_history("X", _T0, _T1)
    assert result["status"] == "withheld"
    assert result["withheld_reason"] == "unexpected_sample_shape"
    assert result["samples"] == []


@pytest.mark.parametrize(
    "sample",
    [
        {"nanos": 0, "val": 1.0},  # missing secs
        {"secs": 1, "nanos": 0},  # missing val
        {"secs": "x", "val": 1.0},  # secs not int-coercible (used to CRASH: uncaught ValueError)
        {"secs": 1, "val": 1.0, "severity": "bad"},  # present field not int-coercible
        {"unexpected": "shape"},  # the auditor probe: ANY dict was accepted as a Sample
    ],
    ids=["no-secs", "no-val", "junk-secs", "junk-severity", "unrelated-dict"],
)
def test_get_pv_history_unreadable_sample_withholds(
    monkeypatch: pytest.MonkeyPatch, sample: object
) -> None:
    """S11: a sample must carry the measured anchors ``secs`` AND ``val``, and every present
    int field must be coercible, anything else withholds the WHOLE result. The old code
    accepted ANY dict and filled missing fields with 0/None: two different broken dicts both
    became ``status=ok, sample={secs:0, nanos:0, val:null, ...}`` (a fabricated sample)."""
    raw = [{"meta": {"name": "X"}, "data": [sample]}]
    client = ArchiverClient("http://arch")
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(raw)))
    result = client.get_pv_history("X", _T0, _T1)
    assert result["status"] == "withheld"
    assert result["withheld_reason"] == "unexpected_sample_shape"
    assert result["samples"] == []


def test_get_pv_history_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ArchiverClient("http://arch")
    monkeypatch.setattr(
        client.session, "get", Mock(side_effect=requests.exceptions.ConnectionError())
    )
    with pytest.raises(ArchiverConnectionError):
        client.get_pv_history("X", _T0, _T1)


# --- client: get_pv_type_info (DS-4B, archive configuration via getPVTypeInfo) ---


def test_get_pv_type_info_projects_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """DS-4B: getPVTypeInfo surfaces the archive CONFIGURATION, sampling (method/period),
    retention (the STS/MTS/LTS data stores), computed rates, DBRType, archived fields, source
    host and creation time, projected onto snake_case keys."""
    record = {
        "pvName": "X",
        "DBRType": "DBR_SCALAR_DOUBLE",
        "samplingMethod": "MONITOR",
        "samplingPeriod": "1.0",
        "computedEventRate": "0.5",
        "computedStorageRate": "4.0",
        "computedBytesPerEvent": "8",
        "elementCount": "1",
        "archiveFields": ["HIHI", "LOLO"],
        "dataStores": ["pb://localhost?name=STS", "pb://localhost?name=MTS"],
        "hostName": "ioc-host",
        "creationTime": "2026-06-01T00:00:00.000Z",
        "applianceIdentity": "appliance0",
        "paused": "false",
        # AR-D: alarm/display/control limits, the appliance renames the dbr fields to camelCase
        # and serializes every numeric limit as a STRING (PVTypeInfo.java, JSONEncoder .toString()).
        "upperAlarmLimit": "90.0",  # HIHI
        "upperWarningLimit": "80.0",  # HIGH
        "lowerWarningLimit": "20.0",  # LOW
        "lowerAlarmLimit": "10.0",  # LOLO
        "upperDisplayLimit": "100.0",  # HOPR
        "lowerDisplayLimit": "0.0",  # LOPR
        "upperCtrlLimit": "100.0",  # DRVH
        "lowerCtrlLimit": "0.0",  # DRVL
        "precision": "3",  # PREC
        "units": "V",  # EGU
        # AR-D: cheap owner-relevant config from the same record
        "controllingPV": "MASTER:PV",
        "policyName": "Default",
        "modificationTime": "2026-06-02T00:00:00.000Z",
        "userParams": "SHOULD NOT be surfaced",
    }
    client = ArchiverClient("http://arch:17665")
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(record)))
    result = client.get_pv_type_info("X")
    # Exact-equality pins the invariant "surface ONLY the _TYPE_INFO_FIELDS allowlist", it excludes
    # userParams (free text) AND pvName (a non-allowlisted field in the input) AND any future
    # non-allowlisted field a denylist refactor might leak, not merely "the one named field".
    assert result == {
        "found": True,
        "dbr_type": "DBR_SCALAR_DOUBLE",
        "sampling_method": "MONITOR",
        "sampling_period": "1.0",
        "event_rate": "0.5",
        "storage_rate": "4.0",
        "bytes_per_event": "8",
        "element_count": "1",
        "archive_fields": ["HIHI", "LOLO"],
        "data_stores": ["pb://localhost?name=STS", "pb://localhost?name=MTS"],
        "host_name": "ioc-host",
        "creation_time": "2026-06-01T00:00:00.000Z",
        "appliance": "appliance0",
        "paused": "false",
        "upper_alarm_limit": "90.0",
        "upper_warning_limit": "80.0",
        "lower_warning_limit": "20.0",
        "lower_alarm_limit": "10.0",
        "upper_display_limit": "100.0",
        "lower_display_limit": "0.0",
        "upper_ctrl_limit": "100.0",
        "lower_ctrl_limit": "0.0",
        "precision": "3",
        "units": "V",
        "controlling_pv": "MASTER:PV",
        "policy_name": "Default",
        "modification_time": "2026-06-02T00:00:00.000Z",
    }
    assert "pvName" not in result  # non-allowlisted input field must not leak
    assert "userParams" not in result  # free-text field deliberately dropped


def test_get_pv_type_info_omits_absent_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """A sparse record surfaces found=True but omits the fields it lacks (no null noise).

    The fixture carries ``pvName``, the measured always-present anchor (S11); the assertion is
    unchanged because ``pvName`` is deliberately NOT allowlisted into the output.
    """
    client = ArchiverClient("http://arch")
    monkeypatch.setattr(
        client.session,
        "get",
        Mock(return_value=_resp({"pvName": "X", "DBRType": "DBR_SCALAR_INT"})),
    )
    result = client.get_pv_type_info("X")
    assert result == {"found": True, "dbr_type": "DBR_SCALAR_INT"}


def test_get_pv_type_info_unwraps_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """Some appliance versions wrap the record in a 1-element list (like getPVStatus)."""
    client = ArchiverClient("http://arch")
    monkeypatch.setattr(
        client.session,
        "get",
        Mock(return_value=_resp([{"pvName": "X", "samplingMethod": "SCAN"}])),
    )
    result = client.get_pv_type_info("X")
    assert result == {"found": True, "sampling_method": "SCAN"}


@pytest.mark.parametrize(
    "payload",
    [
        {},
        [{}],
        [],
        "nope",
        None,
        [123],
        {"unexpected": "shape"},
        [{"unexpected": "shape"}],
        {"pvName": ""},
    ],
    ids=[
        "empty-dict",
        "empty-dict-in-list",
        "empty-list",
        "string",
        "null",
        "non-dict-in-list",
        "dict-without-pvName",
        "listed-dict-without-pvName",
        "empty-pvName",
    ],
)
def test_get_pv_type_info_unreadable_2xx_raises(
    monkeypatch: pytest.MonkeyPatch, payload: object
) -> None:
    """S11 (replaces the former ``...not_found`` pin, which cemented the defect): a 2xx whose body
    is not a type-info record must RAISE. The old code mapped ``{}``/junk to ``found:False``
    (conflated with the appliance's definitive 404) and, worse, projected ANY non-empty dict
    as ``found:True`` (auditor probe ``{"unexpected":"shape"}`` → a fabricated archive record).
    Measured (ESS appliance 2.2.1): the record always carries ``pvName``; the unknown-PV signal
    on this endpoint is HTTP 404 and ONLY that (see ``test_get_pv_type_info_404_is_not_found``).
    """
    client = ArchiverClient("http://arch")
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(payload)))
    with pytest.raises(ArchiverResponseError):
        client.get_pv_type_info("X")


def test_get_pv_type_info_404_is_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """The appliance answers getPVTypeInfo with HTTP 404 for a never-archived PV (unlike
    getPVStatus, which 200s). That 404 must map to found:False, NOT a raised error, otherwise a
    normal "not archived" PV is indistinguishable from an unreachable appliance."""
    client = ArchiverClient("http://arch")
    http_error = requests.exceptions.HTTPError("404")
    http_error.response = Mock(status_code=404)
    resp = Mock()
    resp.raise_for_status.side_effect = http_error
    monkeypatch.setattr(client.session, "get", Mock(return_value=resp))
    result = client.get_pv_type_info("X")
    assert result == {"found": False}


def test_get_pv_type_info_non_404_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """A NON-404 failure (5xx / unreachable) must PROPAGATE as an ArchiverError, a could-not-read
    is never silently reported as "not archived" (the inverse of the 404 case)."""
    client = ArchiverClient("http://arch")
    http_error = requests.exceptions.HTTPError("500")
    http_error.response = Mock(status_code=500)
    resp = Mock()
    resp.raise_for_status.side_effect = http_error
    monkeypatch.setattr(client.session, "get", Mock(return_value=resp))
    with pytest.raises(ArchiverError):
        client.get_pv_type_info("X")


# --- client: get_appliance_info (Fundort 3, the getApplianceInfo body doctor discards) ---


def test_get_appliance_info_projects_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fundort 3: get_appliance_info surfaces the WHOLE getApplianceInfo body that doctor discards
    (doctor reads only ``identity``). Exact-equality pins two invariants: all 8 vendor fields ARE
    projected onto snake_case keys, AND any non-allowlisted extra is dropped, even a plausible
    future field like ``serverStartEpochSeconds`` that this appliance version does not send. Field
    names + the all-string contract are the vendor getApplianceInfo JSON body (GetApplianceInfo.java
    / ApplianceInfo.java), not a live measurement."""
    client = ArchiverClient("http://archiver:17665")
    body = {
        "identity": "appliance0",
        "mgmtURL": "http://archiver:17665/mgmt/bpl",
        "engineURL": "http://archiver:17665/engine/bpl",
        "retrievalURL": "http://archiver:17665/retrieval/bpl",
        "etlURL": "http://archiver:17665/etl/bpl",
        "dataRetrievalURL": "http://archiver:17665/retrieval",
        "clusterInetPort": "archiver:16670",
        "version": "Archiver Appliance 2.2.1",
        "serverStartEpochSeconds": "1717200000",  # not in this version's body, must NOT leak
        "someUnknownField": "SHOULD NOT be surfaced",
    }
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(body)))
    result = client.get_appliance_info()
    assert result == {
        "identity": "appliance0",
        "mgmt_url": "http://archiver:17665/mgmt/bpl",
        "engine_url": "http://archiver:17665/engine/bpl",
        "retrieval_url": "http://archiver:17665/retrieval/bpl",
        "etl_url": "http://archiver:17665/etl/bpl",
        "data_retrieval_url": "http://archiver:17665/retrieval",
        "cluster_inet_port": "archiver:16670",
        "version": "Archiver Appliance 2.2.1",
    }
    assert "server_start_epoch_seconds" not in result
    assert "serverStartEpochSeconds" not in result
    assert "someUnknownField" not in result


def test_get_appliance_info_omits_absent_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """A body missing optional keys (``version`` on a pre-version.txt appliance, a plane URL) omits
    them, no null noise. ``identity`` is the always-present anchor: the appliance names itself."""
    client = ArchiverClient("http://archiver:17665")
    body = {"identity": "appliance0", "mgmtURL": "http://archiver:17665/mgmt/bpl"}
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(body)))
    result = client.get_appliance_info()
    assert result == {"identity": "appliance0", "mgmt_url": "http://archiver:17665/mgmt/bpl"}
    assert "version" not in result


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"identity": ""},
        {"mgmtURL": "http://archiver:17665/mgmt/bpl"},
        [{"identity": "appliance0"}],
        "nope",
        None,
        123,
    ],
    ids=[
        "empty-dict",
        "empty-identity",
        "no-identity",
        "list-wrapped",
        "string",
        "null",
        "int",
    ],
)
def test_get_appliance_info_unreadable_2xx_raises(
    monkeypatch: pytest.MonkeyPatch, payload: object
) -> None:
    """A 2xx whose body is not a getApplianceInfo record (no non-empty ``identity``, wrapped in a
    list, or non-dict) RAISES rather than fabricating an empty success, a wrong-endpoint 200 (e.g.
    the retrieval webapp answering on /mgmt/bpl) must never read as a valid-but-empty appliance.
    Mirrors the getPVStatus/getPVTypeInfo S11 anchor discipline; ``identity`` is the anchor. The
    vendor body is a single object, so a list wrapper is out of contract (unlike getPVTypeInfo)."""
    client = ArchiverClient("http://archiver:17665")
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(payload)))
    with pytest.raises(ArchiverResponseError):
        client.get_appliance_info()


@pytest.mark.parametrize("status", [404, 500])
def test_get_appliance_info_served_error_propagates(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    """Unlike getPVTypeInfo (where 404 = 'PV not archived' → found:False), a no-arg getApplianceInfo
    has no not-found duality: a served non-2xx, INCLUDING 404, means the WRONG endpoint (the
    retrieval webapp serves /retrieval/bpl, not /mgmt/bpl) and PROPAGATES as an ArchiverError, never
    a swallowed empty answer."""
    client = ArchiverClient("http://archiver:17665")
    http_error = requests.exceptions.HTTPError(str(status))
    http_error.response = Mock(status_code=status)
    resp = Mock()
    resp.raise_for_status.side_effect = http_error
    monkeypatch.setattr(client.session, "get", Mock(return_value=resp))
    with pytest.raises(ArchiverError):
        client.get_appliance_info()


# --- two-URL routing (split deployment: mgmt :17665 vs retrieval :17668) ---


def test_two_url_routing_mgmt_vs_retrieval(monkeypatch: pytest.MonkeyPatch) -> None:
    """is_archived / get_pv_type_info hit the MGMT base_url; get_pv_history the retrieval_url.

    In a split deployment /mgmt and /retrieval live on different Tomcats/ports,
    so the calls must NOT share one base URL.
    """
    client = ArchiverClient("http://arch:17665", retrieval_url="http://arch:17668")
    captured: list[str] = []

    def _get(url: str, params: object = None, timeout: object = None, **_: object) -> Mock:
        captured.append(url)
        if "getPVStatus" in url:
            return _resp([{"pvName": "X", "status": "Being archived"}])
        if "getPVTypeInfo" in url:
            return _resp({"pvName": "X", "DBRType": "DBR_SCALAR_DOUBLE"})  # S11 anchor
        return _resp([{"meta": {"name": "X"}, "data": []}])

    monkeypatch.setattr(client.session, "get", _get)
    client.is_archived("X")
    client.get_pv_type_info("X")
    client.get_pv_history("X", _T0, _T1)
    assert captured[0] == "http://arch:17665/mgmt/bpl/getPVStatus"
    assert captured[1] == "http://arch:17665/mgmt/bpl/getPVTypeInfo"
    assert captured[2] == "http://arch:17668/retrieval/data/getData.json"


def test_retrieval_url_defaults_to_base(monkeypatch: pytest.MonkeyPatch) -> None:
    """Single-JVM appliance: no retrieval_url -> get_pv_history falls back to base_url."""
    client = ArchiverClient("http://arch:17665")
    assert client.retrieval_url == "http://arch:17665"
    captured: list[str] = []

    def _get(url: str, params: object = None, timeout: object = None, **_: object) -> Mock:
        captured.append(url)
        return _resp([{"meta": {"name": "X"}, "data": []}])

    monkeypatch.setattr(client.session, "get", _get)
    client.get_pv_history("X", _T0, _T1)
    assert captured[0] == "http://arch:17665/retrieval/data/getData.json"


# --- client: get_archive_status (DS-4A, enriched getPVStatus fields) ---


def test_get_archive_status_enriches_present_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """DS-4A: get_archive_status surfaces the useful extra MGMT-record fields
    (connectionState/lastEvent/isMonitored/...) at ~zero cost, plus archived/status."""
    client = ArchiverClient("http://arch")
    record = {
        "pvName": "X",
        "status": "Being archived",
        "connectionState": True,
        "lastEvent": "Jun/01/2026 10:00:00 UTC",
        "isMonitored": True,
        "samplingPeriod": "1.0",
        "appliance": "appliance0",
    }
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp([record])))
    result = client.get_archive_status("X")
    assert result["archived"] is True
    assert result["status"] == "Being archived"
    assert result["connection_state"] is True
    assert result["last_event"] == "Jun/01/2026 10:00:00 UTC"
    assert result["is_monitored"] is True
    assert result["sampling_period"] == "1.0"
    assert result["appliance"] == "appliance0"


def test_get_archive_status_omits_absent_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the MGMT record lacks the extra fields, they are omitted (not null noise)."""
    client = ArchiverClient("http://arch")
    monkeypatch.setattr(
        client.session, "get", Mock(return_value=_resp([{"pvName": "X", "status": "Paused"}]))
    )
    result = client.get_archive_status("X")
    assert result["archived"] is False
    assert result["status"] == "Paused"
    for absent in (
        "connection_state",
        "last_event",
        "is_monitored",
        "sampling_period",
        "appliance",
        "connection_loss_regain_count",
        "connection_first_established",
        "connection_last_restablished",
    ):
        assert absent not in result


def test_get_archive_status_surfaces_connection_cluster_and_drops_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AR-D: get_archive_status also surfaces the getPVStatus connection-history cluster
    (connectionLossRegainCount / connectionFirstEstablished / connectionLastRestablished) alongside
    the DS-4A fields, the already-fetched-but-discarded diagnostic bytes ("does this PV flap? when
    did it last reconnect?"). Exact-equality pins TWO invariants the DS-4A per-key asserts missed:
    the new cluster IS surfaced, AND any non-allowlisted extra (``lastRotateLogs`` epoch-0 noise,
    ``someUnknownField``) is dropped. Source keys + the all-string value contract are the Archiver
    Appliance getPVStatus response (vendor source EngineChannelStatus.java:119-128); note the
    upstream typo ``connectionLastRestablished`` (missing the second 'e')."""
    client = ArchiverClient("http://arch:17665")
    record = {
        "pvName": "X",
        "status": "Being archived",
        "connectionState": "true",
        "lastEvent": "Jun/01/2026 10:00:00 UTC",
        "isMonitored": "true",
        "samplingPeriod": "1.0",
        "appliance": "appliance0",
        "connectionLossRegainCount": "3",
        "connectionFirstEstablished": "Jun/01/2026 09:00:00 UTC",
        "connectionLastRestablished": "Jun/01/2026 09:30:00 UTC",
        "lastRotateLogs": "Jan/01/1970 00:00:00 UTC",  # epoch-0 noise, deliberately NOT surfaced
        "someUnknownField": "SHOULD NOT be surfaced",
    }
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp([record])))
    result = client.get_archive_status("X")
    assert result == {
        "archived": True,
        "status": "Being archived",
        "connection_state": "true",
        "last_event": "Jun/01/2026 10:00:00 UTC",
        "is_monitored": "true",
        "sampling_period": "1.0",
        "appliance": "appliance0",
        "connection_loss_regain_count": "3",
        "connection_first_established": "Jun/01/2026 09:00:00 UTC",
        "connection_last_restablished": "Jun/01/2026 09:30:00 UTC",
    }
    assert "last_rotate_logs" not in result  # epoch-0 noise, deliberately not allowlisted
    assert "someUnknownField" not in result  # non-allowlisted field must not leak


# --- tools: is_archived / get_pv_history / get_archive_info ---


@pytest.mark.asyncio
async def test_is_archived_tool_disabled_no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    # Gating + client construction live in services/checkers.query_archived (M9); patch there.
    monkeypatch.setattr(
        "epics_pv_mcp.services.checkers.get_config", lambda: EpicsConfig(archiver_url="")
    )

    def _boom(*args: object, **kwargs: object) -> ArchiverClient:
        raise AssertionError("client must not be constructed when disabled")

    monkeypatch.setattr("epics_pv_mcp.services.checkers.ArchiverClient", _boom)
    result = await _is_archived("X")
    assert result["enabled"] is False
    assert result["archived"] is None


@pytest.mark.asyncio
async def test_get_pv_history_tool_disabled_no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "epics_pv_mcp.tools.archiver.get_config", lambda: EpicsConfig(archiver_url="")
    )

    def _boom(*args: object, **kwargs: object) -> ArchiverClient:
        raise AssertionError("client must not be constructed when disabled")

    monkeypatch.setattr("epics_pv_mcp.tools.archiver.ArchiverClient", _boom)
    result = await _get_pv_history("X", _T0, _T1)
    assert result["enabled"] is False
    assert result["total"] == 0


@pytest.mark.asyncio
async def test_get_archive_info_tool_disabled_no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "epics_pv_mcp.tools.archiver.get_config", lambda: EpicsConfig(archiver_url="")
    )

    def _boom(*args: object, **kwargs: object) -> ArchiverClient:
        raise AssertionError("client must not be constructed when disabled")

    monkeypatch.setattr("epics_pv_mcp.tools.archiver.ArchiverClient", _boom)
    result = await _get_archive_info("X")
    assert result["enabled"] is False
    # found is None (NOT checked) when disabled, a disabled plane must never masquerade as a
    # definitive "no archive record" (found:False), mirroring the sibling archived/configured None.
    assert result["found"] is None


@pytest.mark.asyncio
async def test_is_archived_tool_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "epics_pv_mcp.services.checkers.get_config",
        lambda: EpicsConfig(archiver_url="http://arch"),
    )

    class _Fake:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def get_archive_status(self, pv: str) -> dict[str, object]:
            return {
                "archived": True,
                "status": "Being archived",
                "connection_state": True,
                "last_event": "Jun/01/2026 10:00:00 UTC",
                "is_monitored": True,
                "connection_loss_regain_count": "3",
            }

    monkeypatch.setattr("epics_pv_mcp.services.checkers.ArchiverClient", _Fake)
    result = await _is_archived("X")
    assert result["enabled"] is True
    assert result["archived"] is True
    assert result["status"] == "Being archived"
    assert result["connection_state"] is True  # DS-4A: enriched getPVStatus fields surfaced
    assert result["is_monitored"] is True
    assert result["connection_loss_regain_count"] == "3"  # AR-D: cluster reaches the tool


@pytest.mark.asyncio
async def test_get_pv_history_tool_passes_retrieval_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """_get_pv_history must construct ArchiverClient with the configured retrieval URL."""
    monkeypatch.setattr(
        "epics_pv_mcp.tools.archiver.get_config",
        lambda: EpicsConfig(
            archiver_url="http://arch:17665",
            archiver_retrieval_url="http://arch:17668",
        ),
    )
    captured: dict[str, object] = {}

    class _Fake:
        def __init__(self, base_url: str, *args: object, **kwargs: object) -> None:
            captured["base_url"] = base_url
            captured["retrieval_url"] = kwargs.get("retrieval_url")

        def get_pv_history(
            self, pv: str, start: str, end: str, max_points: int = 5000
        ) -> HistoryResult:
            return HistoryResult(
                samples=[Sample(secs=1, nanos=0, val=1.0, severity=0, status=0)],
                capped=False,
                meta={},
                status="ok",
                note="",
                withheld_reason=None,
            )

    monkeypatch.setattr("epics_pv_mcp.tools.archiver.ArchiverClient", _Fake)
    result = await _get_pv_history("X", _T0, _T1)
    assert result["enabled"] is True
    assert captured["base_url"] == "http://arch:17665"
    assert captured["retrieval_url"] == "http://arch:17668"


@pytest.mark.asyncio
async def test_get_pv_history_tool_surfaces_meta_and_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DS-4A: the tool surfaces the meta block (EGU/PREC); DS-4B: it surfaces status/withheld."""
    monkeypatch.setattr(
        "epics_pv_mcp.tools.archiver.get_config",
        lambda: EpicsConfig(archiver_url="http://arch"),
    )

    class _Fake:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def get_pv_history(
            self, pv: str, start: str, end: str, max_points: int = 5000
        ) -> HistoryResult:
            return HistoryResult(
                samples=[Sample(secs=1, nanos=0, val=1.0, severity=0, status=0)],
                capped=False,
                meta={"EGU": "V", "PREC": "2"},
                status="ok",
                note="",
                withheld_reason=None,
            )

    monkeypatch.setattr("epics_pv_mcp.tools.archiver.ArchiverClient", _Fake)
    result = await _get_pv_history("X", _T0, _T1)
    assert result["enabled"] is True
    assert result["total"] == 1
    assert result["meta"] == {"EGU": "V", "PREC": "2"}
    assert result["status"] == "ok"
    assert "withheld_reason" not in result  # omitted on non-withheld results


@pytest.mark.asyncio
async def test_get_pv_history_tool_surfaces_withheld(monkeypatch: pytest.MonkeyPatch) -> None:
    """DS-4B: a withheld client result surfaces status + withheld_reason + note at the tool."""
    monkeypatch.setattr(
        "epics_pv_mcp.tools.archiver.get_config",
        lambda: EpicsConfig(archiver_url="http://arch"),
    )

    class _Fake:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def get_pv_history(
            self, pv: str, start: str, end: str, max_points: int = 5000
        ) -> HistoryResult:
            return HistoryResult(
                samples=[],
                capped=False,
                meta={},
                status="withheld",
                note="Archiver returned an unexpected payload.",
                withheld_reason="unexpected_payload",
            )

    monkeypatch.setattr("epics_pv_mcp.tools.archiver.ArchiverClient", _Fake)
    result = await _get_pv_history("X", _T0, _T1)
    assert result["status"] == "withheld"
    assert result["withheld_reason"] == "unexpected_payload"
    assert result["note"] == "Archiver returned an unexpected payload."
    assert result["total"] == 0


@pytest.mark.asyncio
async def test_get_archive_info_tool_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """_get_archive_info builds the client (MGMT base) and surfaces the projected type info."""
    monkeypatch.setattr(
        "epics_pv_mcp.tools.archiver.get_config",
        lambda: EpicsConfig(archiver_url="http://arch:17665"),
    )

    class _Fake:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def get_pv_type_info(self, pv: str) -> dict[str, object]:
            return {
                "found": True,
                "dbr_type": "DBR_SCALAR_DOUBLE",
                "sampling_method": "MONITOR",
                "data_stores": ["pb://localhost?name=STS"],
            }

    monkeypatch.setattr("epics_pv_mcp.tools.archiver.ArchiverClient", _Fake)
    result = await _get_archive_info("X")
    assert result["enabled"] is True
    assert result["pv"] == "X"
    assert result["found"] is True
    assert result["dbr_type"] == "DBR_SCALAR_DOUBLE"
    assert result["sampling_method"] == "MONITOR"
    assert result["data_stores"] == ["pb://localhost?name=STS"]


@pytest.mark.asyncio
async def test_get_appliance_info_tool_disabled_no_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disabled (EPICS_MCP_ARCHIVER_URL unset) → {enabled:false, note} and NO client construction.
    The shape has no ``found``/``pv`` key: getApplianceInfo has no PV present/absent duality."""
    monkeypatch.setattr(
        "epics_pv_mcp.tools.archiver.get_config", lambda: EpicsConfig(archiver_url="")
    )

    def _boom(*args: object, **kwargs: object) -> ArchiverClient:
        raise AssertionError("client must not be constructed when disabled")

    monkeypatch.setattr("epics_pv_mcp.tools.archiver.ArchiverClient", _boom)
    result = await _get_appliance_info()
    assert result == {"enabled": False, "note": _DISABLED_NOTE}


@pytest.mark.asyncio
async def test_get_appliance_info_tool_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """_get_appliance_info builds the client (MGMT base) and surfaces the projected body under
    enabled:true, exact-equality pins the {enabled, **projection} shape (no pv, no found)."""
    monkeypatch.setattr(
        "epics_pv_mcp.tools.archiver.get_config",
        lambda: EpicsConfig(archiver_url="http://archiver:17665"),
    )

    class _Fake:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def get_appliance_info(self) -> dict[str, object]:
            return {
                "identity": "appliance0",
                "mgmt_url": "http://archiver:17665/mgmt/bpl",
                "version": "Archiver Appliance 2.2.1",
            }

    monkeypatch.setattr("epics_pv_mcp.tools.archiver.ArchiverClient", _Fake)
    result = await _get_appliance_info()
    assert result == {
        "enabled": True,
        "identity": "appliance0",
        "mgmt_url": "http://archiver:17665/mgmt/bpl",
        "version": "Archiver Appliance 2.2.1",
    }


# --- check_connectivity (E2 doctor probe: getApplianceInfo, 2xx required) ---


def test_check_connectivity_probes_appliance_info(monkeypatch: pytest.MonkeyPatch) -> None:
    """The probe hits /mgmt/bpl/getApplianceInfo (no PV) and returns True on a 2xx."""
    client = ArchiverClient("http://arch:17665")
    captured: list[str] = []

    def fake_get(url: str, params: object = None, timeout: object = None, **_: object) -> Mock:
        captured.append(url)
        return _resp([{"identity": "appliance0"}])

    monkeypatch.setattr(client.session, "get", fake_get)
    assert client.check_connectivity() is True
    assert captured[0] == "http://arch:17665/mgmt/bpl/getApplianceInfo"


def test_check_connectivity_served_non2xx_raises_response_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A served non-2xx (e.g. ARCHIVER_URL points at the wrong webapp) → ArchiverResponseError,
    NOT ArchiverConnectionError: doctor reads it as 'api_error' (reachable), not 'unreachable'."""
    client = ArchiverClient("http://arch:17665")
    resp = Mock()
    http_error = requests.exceptions.HTTPError("404")
    http_error.response = Mock(status_code=404)
    resp.raise_for_status.side_effect = http_error
    monkeypatch.setattr(client.session, "get", Mock(return_value=resp))
    with pytest.raises(ArchiverResponseError) as excinfo:
        client.check_connectivity()
    # The from-exc chain the doctor classifier depends on: __cause__ carries the served HTTP status,
    # so a dropped `from exc` would silently mark this reachable-but-erroring host unreachable.
    assert http_status(excinfo.value) == 404


def test_check_connectivity_ssl_error_chains_cause(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real TLS/CA failure chains the SSLError so doctor buckets it ca_error, not unreachable."""
    client = ArchiverClient("http://arch:17665")
    monkeypatch.setattr(
        client.session, "get", Mock(side_effect=requests.exceptions.SSLError("self-signed"))
    )
    with pytest.raises(ArchiverConnectionError) as excinfo:
        client.check_connectivity()
    assert is_ssl_error(excinfo.value) is True


def test_check_connectivity_transport_failure_raises_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ArchiverClient("http://arch:17665")
    monkeypatch.setattr(
        client.session, "get", Mock(side_effect=requests.exceptions.ConnectionError())
    )
    with pytest.raises(ArchiverConnectionError):
        client.check_connectivity()


# --- client: get_all_pvs / get_pvs_for_this_appliance (E2 list_archived_pvs) ---


def test_get_all_pvs_returns_names_not_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ArchiverClient("http://arch:17665")
    captured: dict[str, object] = {}

    def fake_get(url: str, params: object = None, timeout: object = None, **_: object) -> Mock:
        captured["url"] = url
        return _resp(["SYS:PV1", "SYS:PV2"])

    monkeypatch.setattr(client.session, "get", fake_get)
    names, capped = client.get_all_pvs()
    assert names == ["SYS:PV1", "SYS:PV2"]
    assert capped is False
    assert captured["url"] == "http://arch:17665/mgmt/bpl/getAllPVs"


def test_get_all_pvs_forwards_pattern_and_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    """A name glob maps to the ``pv`` param; over-fetch by one makes ``capped`` honest."""
    client = ArchiverClient("http://arch")
    captured: dict[str, object] = {}

    def fake_get(
        url: str, params: dict[str, str] | None = None, timeout: object = None, **_: object
    ) -> Mock:
        captured["params"] = params or {}
        return _resp([f"PV{i}" for i in range(5)])  # 5 names, limit 3 → capped

    monkeypatch.setattr(client.session, "get", fake_get)
    names, capped = client.get_all_pvs(pattern="DEV-TEST01:*", limit=3)
    params = captured["params"]
    assert isinstance(params, dict)
    assert params["pv"] == "DEV-TEST01:*"
    assert params["limit"] == "4"  # limit + 1 over-fetch
    assert names == ["PV0", "PV1", "PV2"]  # sliced to limit
    assert capped is True


def test_get_pvs_for_this_appliance(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ArchiverClient("http://arch:17665")
    captured: dict[str, object] = {}

    def fake_get(url: str, params: object = None, timeout: object = None, **_: object) -> Mock:
        captured["url"] = url
        return _resp(["M:PV1"])

    monkeypatch.setattr(client.session, "get", fake_get)
    names, capped = client.get_pvs_for_this_appliance()
    assert names == ["M:PV1"]
    assert capped is False
    assert captured["url"] == "http://arch:17665/mgmt/bpl/getPVsForThisAppliance"


def test_get_all_pvs_non_list_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ArchiverClient("http://arch")
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp({"oops": 1})))
    with pytest.raises(ArchiverResponseError):
        client.get_all_pvs()


def test_get_all_pvs_capped_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exactly `limit` names → capped False; limit+1 → capped True (pins `len(names) > limit`)."""
    client = ArchiverClient("http://arch")
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(["A", "B", "C"])))
    names, capped = client.get_all_pvs(limit=3)
    assert names == ["A", "B", "C"]
    assert capped is False  # exactly limit, an off-by-one (>=) regression would flip this
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(["A", "B", "C", "D"])))
    names, capped = client.get_all_pvs(limit=3)
    assert names == ["A", "B", "C"]  # sliced to limit
    assert capped is True


def test_coerce_pv_names_nonpositive_limit_clamped() -> None:
    """A non-positive limit is clamped to >=1, never a negative slice that silently drops the last
    name (`names[:-1]`) or empties the list, with a falsely-True capped."""
    for bad in (-1, 0):
        names, capped = ArchiverClient._coerce_pv_names(["A", "B", "C"], bad, "getAllPVs")
        assert names == ["A"]  # clamped to limit 1, NOT names[:-1]==["A","B"] nor names[:0]==[]
        assert capped is True  # honestly capped (3 > 1)


@pytest.mark.parametrize(
    "payload",
    [["PV:A", 123], [{"name": "PV:A"}], ["PV:A", None]],
    ids=["number-item", "dict-item", "null-item"],
)
def test_coerce_pv_names_non_string_item_raises(payload: object) -> None:
    """S11: a list item that is not a string must RAISE, ``str()`` used to mint junk into
    plausible PV "names" (``{'name': 'PV:A'}`` became the literal name ``"{'name': 'PV:A'}"``).
    Measured (ESS 2.2.1): getAllPVs returns a bare array of strings, nothing else."""
    with pytest.raises(ArchiverResponseError):
        ArchiverClient._coerce_pv_names(payload, 10, "getAllPVs")


# --- tool: list_archived_pvs ---


@pytest.mark.asyncio
async def test_list_archived_pvs_tool_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "epics_pv_mcp.tools.archiver.get_config",
        lambda: EpicsConfig(archiver_url="http://arch:17665"),
    )

    class _Fake:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def get_all_pvs(
            self, pattern: str | None = None, limit: int = 5000
        ) -> tuple[list[str], bool]:
            return (["A:PV1", "A:PV2"], False)

    monkeypatch.setattr("epics_pv_mcp.tools.archiver.ArchiverClient", _Fake)
    result = await _list_archived_pvs()
    assert result["enabled"] is True
    assert result["pvs"] == ["A:PV1", "A:PV2"]
    assert result["total"] == 2
    assert result["capped"] is False


@pytest.mark.asyncio
async def test_list_archived_pvs_this_appliance_variant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "epics_pv_mcp.tools.archiver.get_config",
        lambda: EpicsConfig(archiver_url="http://arch"),
    )

    class _Fake:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def get_all_pvs(
            self, pattern: str | None = None, limit: int = 5000
        ) -> tuple[list[str], bool]:
            raise AssertionError("this_appliance=True must use getPVsForThisAppliance")

        def get_pvs_for_this_appliance(self, limit: int = 5000) -> tuple[list[str], bool]:
            return (["M:PV1"], True)

    monkeypatch.setattr("epics_pv_mcp.tools.archiver.ArchiverClient", _Fake)
    result = await _list_archived_pvs(this_appliance=True)
    assert result["pvs"] == ["M:PV1"]
    assert result["capped"] is True


@pytest.mark.asyncio
async def test_list_archived_pvs_tool_disabled_no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "epics_pv_mcp.tools.archiver.get_config", lambda: EpicsConfig(archiver_url="")
    )

    def _boom(*args: object, **kwargs: object) -> ArchiverClient:
        raise AssertionError("client must not be constructed when disabled")

    monkeypatch.setattr("epics_pv_mcp.tools.archiver.ArchiverClient", _boom)
    result = await _list_archived_pvs()
    assert result["enabled"] is False
    assert result["pvs"] == []
    assert result["total"] == 0


@pytest.mark.asyncio
async def test_list_archived_pvs_forwards_pattern_and_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tool forwards pattern + limit to the client's get_all_pvs (the tool→client wire)."""
    monkeypatch.setattr(
        "epics_pv_mcp.tools.archiver.get_config", lambda: EpicsConfig(archiver_url="http://arch")
    )
    captured: dict[str, object] = {}

    class _Fake:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def get_all_pvs(
            self, pattern: str | None = None, limit: int = 5000
        ) -> tuple[list[str], bool]:
            captured["pattern"] = pattern
            captured["limit"] = limit
            return (["X"], False)

    monkeypatch.setattr("epics_pv_mcp.tools.archiver.ArchiverClient", _Fake)
    await _list_archived_pvs(pattern="DEV-TEST01:*", limit=7)
    assert captured == {"pattern": "DEV-TEST01:*", "limit": 7}


# --- get_pv_history: the time window on the wire (live-established contract) ---
#
# The Archiver reads zone-explicit ISO and NOTHING else, measured live: a naive ISO, a
# space-separated wall clock, a bare date and '7 days' are each an HTTP 500. It never answers one
# of them wrongly (unlike Olog/Alarm), but it is the narrowest of the three planes, so the
# notations a caller learned elsewhere are normalized here rather than turned into a server error.


def _history_params(
    client: ArchiverClient, monkeypatch: pytest.MonkeyPatch, **kwargs: str
) -> dict[str, object]:
    """Capture the query params get_pv_history puts on the wire."""
    captured: dict[str, object] = {}

    def _get(url: str, params: object = None, timeout: object = None, **_: object) -> Mock:
        captured["params"] = params
        return _resp([{"meta": {}, "data": []}])

    monkeypatch.setattr(client.session, "get", _get)
    client.get_pv_history("SIM:PS-01:Cur-RB", kwargs["start"], kwargs["end"])
    params = captured["params"]
    assert isinstance(params, dict)
    return params


@pytest.mark.parametrize(
    "start",
    [
        "2026-07-08T00:00:00Z",  # already correct, must stay stable (idempotence)
        "2026-07-08T00:00:00",  # naive ISO: HTTP 500 today
        "2026-07-08 00:00:00",  # the wall clock Olog requires: HTTP 500 today
        "2026-07-08",  # bare date: HTTP 500 today
        "2026-07-08T02:00:00+02:00",  # same instant, offset applied
    ],
)
def test_get_pv_history_absolute_notations_all_reach_the_wire_as_iso_z(
    start: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every absolute notation denotes the same instant and must leave as the one form the
    Archiver reads. Four of these five are an HTTP 500 without the normalization."""
    client = ArchiverClient("http://archiver:17665")
    params = _history_params(client, monkeypatch, start=start, end="2026-07-09T00:00:00Z")
    assert params["from"] == "2026-07-08T00:00:00.000Z"
    assert params["to"] == "2026-07-09T00:00:00.000Z"


def test_get_pv_history_relative_amount_makes_no_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """'7 days' works on the alarm/logbook planes and is an HTTP 500 here, so it is refused by
    name BEFORE any I/O. Asserting the message alone would still pass if we sent it."""
    client = ArchiverClient("http://archiver:17665")

    def _fail(*_a: object, **_k: object) -> Mock:
        raise AssertionError("a request was made with a value this plane cannot read")

    monkeypatch.setattr(client.session, "get", _fail)
    with pytest.raises(TimeWindowFormatError, match="only an absolute time"):
        client.get_pv_history("SIM:PS-01:Cur-RB", "7 days", "now")


def test_get_pv_history_shares_the_wire_format_with_the_alarm_plane() -> None:
    """Both planes parse a real ISO_INSTANT, so they must agree byte-for-byte, goes red if
    someone forks the emitter for one of them."""
    value = "2026-07-08T12:45:58.123456Z"
    assert normalize_archiver_time(value, param="start") == normalize_alarm_time(
        value, param="start"
    )


# --- get_pv_history: the ERROR CLASS at the tool boundary ---
#
# The Archiver 500s on a time it cannot read. Reporting that as EPICS_CONNECTION_FAILED sent the
# reader after VPN/network problems that were not happening, the appliance answered.


def _history_client_raising(exc: BaseException) -> type:
    class _Fake:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def get_pv_history(self, *args: object, **kwargs: object) -> object:
            raise exc

    return _Fake


@pytest.mark.asyncio
async def test_get_pv_history_served_error_is_not_a_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE regression: a served 500 must not claim the appliance is unreachable."""
    monkeypatch.setattr(
        "epics_pv_mcp.tools.archiver.get_config", lambda: EpicsConfig(archiver_url="http://arch")
    )
    http_error = requests.exceptions.HTTPError("500")
    http_error.response = Mock(status_code=500)
    served = ArchiverResponseError("Request failed")
    served.__cause__ = http_error
    monkeypatch.setattr(
        "epics_pv_mcp.tools.archiver.ArchiverClient", _history_client_raising(served)
    )
    with pytest.raises(EpicsError) as excinfo:
        await _get_pv_history("X", _T0, _T1)
    assert excinfo.value.error_code == "ARCHIVER_HTTP_500"
    # EpicsConnectionError subclasses EpicsError, so a bare pytest.raises(EpicsError) above would
    # still pass on the bug. This is the assertion that actually pins the fix.
    assert not isinstance(excinfo.value, EpicsConnectionError)


@pytest.mark.asyncio
async def test_get_pv_history_bad_time_is_not_a_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A window this plane cannot read is a bad ARGUMENT, nothing was ever sent."""
    monkeypatch.setattr(
        "epics_pv_mcp.tools.archiver.get_config", lambda: EpicsConfig(archiver_url="http://arch")
    )
    monkeypatch.setattr(
        "epics_pv_mcp.tools.archiver.ArchiverClient",
        _history_client_raising(TimeWindowFormatError("start='7 days': only an absolute time")),
    )
    with pytest.raises(EpicsError) as excinfo:
        await _get_pv_history("X", "7 days", "now")
    assert excinfo.value.error_code == "INVALID_TIME_WINDOW"
    assert not isinstance(excinfo.value, EpicsConnectionError)


@pytest.mark.asyncio
async def test_get_pv_history_connection_failure_still_maps_to_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Counter-test: the split must not over-reach, a real outage stays a connection error."""
    monkeypatch.setattr(
        "epics_pv_mcp.tools.archiver.get_config", lambda: EpicsConfig(archiver_url="http://arch")
    )
    monkeypatch.setattr(
        "epics_pv_mcp.tools.archiver.ArchiverClient",
        _history_client_raising(ArchiverConnectionError("no route to host")),
    )
    with pytest.raises(EpicsConnectionError) as excinfo:
        await _get_pv_history("X", _T0, _T1)
    assert excinfo.value.error_code == "EPICS_CONNECTION_FAILED"


def test_archiver_error_code_without_a_readable_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """A retry-exhausted 502/503/504 arrives as requests.RetryError, not a ConnectionError, and
    with no readable status. Generic token, but still NOT 'unreachable': the host did answer."""
    assert _archiver_error_code(ArchiverResponseError("opaque")) == "ARCHIVER_RESPONSE_ERROR"


class _NoClient:
    """A client double that fails the test if it is constructed at all."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("a client was built for a call that must be refused up front")


@pytest.mark.asyncio
async def test_list_archived_pvs_refuses_pattern_with_this_appliance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pattern + this_appliance=True must be REFUSED, not silently answered unfiltered.

    This test replaces one named ..._this_appliance_ignores_pattern, which asserted
    `captured == {"limit": 9}  # pattern silently dropped`: it pinned the bug as intended
    behaviour while four caller-facing surfaces promised the filter worked.

    Measured against a live appliance: getPVsForThisAppliance ignores pv/regex/pattern/name alike:
    every reply is byte-identical to the unfiltered one. So the old behaviour handed the caller a
    full, plausible list of the WRONG PVs behind a capped=true that reads as a fair truncation.
    """
    monkeypatch.setattr(
        "epics_pv_mcp.tools.archiver.get_config", lambda: EpicsConfig(archiver_url="http://arch")
    )
    monkeypatch.setattr("epics_pv_mcp.tools.archiver.ArchiverClient", _NoClient)
    with pytest.raises(EpicsError) as excinfo:
        await _list_archived_pvs(pattern="DEV-TEST01:*", this_appliance=True, limit=9)
    assert excinfo.value.error_code == "INVALID_ARGUMENT"
    assert not isinstance(excinfo.value, EpicsConnectionError)


@pytest.mark.asyncio
async def test_list_archived_pvs_refusal_fires_even_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bad argument is bad regardless of deployment, so the refusal precedes the config gate.

    Pins the ordering: a caller testing without an archiver still learns the call is wrong instead
    of getting a friendly enabled:false that hides it until production.
    """
    monkeypatch.setattr("epics_pv_mcp.tools.archiver.get_config", lambda: EpicsConfig())
    with pytest.raises(EpicsError) as excinfo:
        await _list_archived_pvs(pattern="DEV-TEST01:*", this_appliance=True)
    assert excinfo.value.error_code == "INVALID_ARGUMENT"


@pytest.mark.asyncio
async def test_list_archived_pvs_empty_pattern_with_this_appliance_is_fine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pattern="" means 'no pattern' on both paths (the client's own `if pattern:` convention), so
    it must NOT trip the refusal."""
    monkeypatch.setattr(
        "epics_pv_mcp.tools.archiver.get_config", lambda: EpicsConfig(archiver_url="http://arch")
    )
    captured: dict[str, object] = {}

    class _Fake:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def get_pvs_for_this_appliance(self, limit: int = 5000) -> tuple[list[str], bool]:
            captured["limit"] = limit
            return (["M"], False)

    monkeypatch.setattr("epics_pv_mcp.tools.archiver.ArchiverClient", _Fake)
    result = await _list_archived_pvs(pattern="", this_appliance=True, limit=9)
    assert result["pvs"] == ["M"]
    assert captured == {"limit": 9}


@pytest.mark.asyncio
async def test_list_archived_pvs_maps_archiver_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A client ArchiverError surfaces as EpicsConnectionError with the 'Archiver: ' prefix."""
    monkeypatch.setattr(
        "epics_pv_mcp.tools.archiver.get_config", lambda: EpicsConfig(archiver_url="http://arch")
    )

    class _Fake:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def get_all_pvs(
            self, pattern: str | None = None, limit: int = 5000
        ) -> tuple[list[str], bool]:
            raise ArchiverConnectionError("boom")

    monkeypatch.setattr("epics_pv_mcp.tools.archiver.ArchiverClient", _Fake)
    with pytest.raises(EpicsConnectionError, match="Archiver:"):
        await _list_archived_pvs()
