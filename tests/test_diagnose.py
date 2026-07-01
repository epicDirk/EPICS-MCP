"""Offline tests for the diagnose_connection service (no network).

Two layers: the PURE :func:`derive_cause` decision tree (the whole branch matrix, no I/O) and the
async :func:`diagnose` shell (mocked live probe + planes) — especially the exception-catching
inversion (a disconnect is caught, never raised) and the diagnose-level Naming gate (empty URL →
withheld, no client).
"""

from __future__ import annotations

import pytest

from epics_pv_mcp.config import EpicsConfig
from epics_pv_mcp.errors import EpicsConnectionError, PVTimeoutError
from epics_pv_mcp.services.diagnose import (
    AlarmEvidence,
    ArchiverEvidence,
    ChannelFinderEvidence,
    DiagnoseEvidence,
    LiveEvidence,
    NamingEvidence,
    State,
    derive_cause,
    diagnose,
)
from epics_pv_mcp.services.naming_client import NameStatus
from epics_pv_mcp.services.naming_exceptions import NamingServiceConnectionError

# --- evidence builders ---


def _live(
    *,
    connected: bool = False,
    error_code: str | None = None,
    severity: str | None = None,
    value: object | None = None,
) -> LiveEvidence:
    return LiveEvidence(connected=connected, error_code=error_code, severity=severity, value=value)


def _cf(
    *,
    consulted: bool = False,
    registered: bool | None = None,
    pv_status: str | None = None,
    ioc_name: str | None = None,
    host_name: str | None = None,
    capped: bool = False,
    withheld: bool = False,
) -> ChannelFinderEvidence:
    return ChannelFinderEvidence(
        consulted=consulted,
        registered=registered,
        pv_status=pv_status,
        ioc_name=ioc_name,
        host_name=host_name,
        capped=capped,
        withheld=withheld,
    )


def _nm(
    *, consulted: bool = False, registered: bool | None = None, status: str | None = None
) -> NamingEvidence:
    return NamingEvidence(consulted=consulted, registered=registered, status=status)


def _ar(*, consulted: bool = False, archived: bool | None = None) -> ArchiverEvidence:
    return ArchiverEvidence(consulted=consulted, archived=archived)


def _al(*, consulted: bool = False, configured: bool | None = None) -> AlarmEvidence:
    return AlarmEvidence(consulted=consulted, configured=configured)


def _ev(
    live: LiveEvidence,
    *,
    cf: ChannelFinderEvidence | None = None,
    nm: NamingEvidence | None = None,
    ar: ArchiverEvidence | None = None,
    al: AlarmEvidence | None = None,
) -> DiagnoseEvidence:
    return DiagnoseEvidence(
        live=live,
        channelfinder=cf or _cf(),
        naming=nm or _nm(),
        archiver=ar or _ar(),
        alarm=al or _al(),
    )


# ---------------------------------------------------------------------------
# PURE derive_cause — the full branch matrix
# ---------------------------------------------------------------------------


def test_connected_is_healthy_no_uniqueness_claim() -> None:
    res = derive_cause("connected", _ev(_live(connected=True, value=12)))
    assert res.likely_cause == "healthy"
    assert res.confidence == "confirmed"
    # honest: no "only source" / uniqueness claim
    joined = " ".join(res.next_steps).lower()
    assert "not uniqueness" in joined or "one responder" in joined
    assert res.notes == ()


def test_connected_in_alarm_notes_data_not_connection() -> None:
    res = derive_cause("connected", _ev(_live(connected=True, severity="MAJOR")))
    assert res.likely_cause == "healthy"
    assert any("alarm" in n.lower() for n in res.notes)


def test_connected_no_alarm_has_no_alarm_note() -> None:
    res = derive_cause("connected", _ev(_live(connected=True, severity="NO_ALARM")))
    assert res.likely_cause == "healthy"
    assert res.notes == ()


def test_connected_with_stale_cf_status_stays_healthy() -> None:
    # live wins: CF pvStatus=Inactive is stale, the PV is live → healthy + honest 'stale' note.
    res = derive_cause(
        "connected",
        _ev(_live(connected=True), cf=_cf(consulted=True, registered=True, pv_status="Inactive")),
    )
    assert res.likely_cause == "healthy"
    assert any("stale" in n.lower() for n in res.notes)


def test_unknown_is_indeterminate() -> None:
    res = derive_cause("unknown", _ev(_live()))
    assert res.likely_cause == "indeterminate"
    assert res.confidence == "indeterminate"


def test_disconnected_cf_hit_status_down_is_ioc_down_likely() -> None:
    res = derive_cause(
        "disconnected",
        _ev(
            _live(error_code="PV_TIMEOUT"),
            cf=_cf(consulted=True, registered=True, pv_status="Inactive", ioc_name="IOC1"),
        ),
    )
    assert res.likely_cause == "ioc_down"
    assert res.confidence == "likely"  # no archiver corroboration
    assert any("IOC1" in s for s in res.next_steps)  # ioc spliced in, None-safe


def test_disconnected_cf_hit_status_down_plus_archiver_is_confirmed() -> None:
    res = derive_cause(
        "disconnected",
        _ev(
            _live(error_code="PV_TIMEOUT"),
            cf=_cf(consulted=True, registered=True, pv_status="Inactive"),
            ar=_ar(consulted=True, archived=True),
        ),
    )
    assert res.likely_cause == "ioc_down"
    assert res.confidence == "confirmed"


def test_disconnected_cf_hit_status_active_timeout_is_indeterminate() -> None:
    # pvStatus healthy + timeout → NOT network_unreachable (no transport probe) → indeterminate.
    res = derive_cause(
        "disconnected",
        _ev(
            _live(error_code="PV_TIMEOUT"),
            cf=_cf(consulted=True, registered=True, pv_status="Active"),
        ),
    )
    assert res.likely_cause == "indeterminate"


def test_disconnected_cf_hit_status_absent_timeout_is_indeterminate() -> None:
    res = derive_cause(
        "disconnected",
        _ev(_live(error_code="PV_TIMEOUT"), cf=_cf(consulted=True, registered=True)),
    )
    assert res.likely_cause == "indeterminate"


def test_disconnected_cf_hit_not_found_is_ioc_down() -> None:
    # PV_NOT_FOUND only under UDP broadcast (never a name-server); CF knows it → ioc down.
    res = derive_cause(
        "disconnected",
        _ev(_live(error_code="PV_NOT_FOUND"), cf=_cf(consulted=True, registered=True)),
    )
    assert res.likely_cause == "ioc_down"


def test_disconnected_cf_miss_naming_active_is_unregistered() -> None:
    res = derive_cause(
        "disconnected",
        _ev(
            _live(error_code="PV_TIMEOUT"),
            cf=_cf(consulted=True, registered=False),
            nm=_nm(consulted=True, registered=True, status="ACTIVE"),
        ),
    )
    assert res.likely_cause == "unregistered"


def test_disconnected_cf_miss_naming_active_plus_alarm_corroborates() -> None:
    res = derive_cause(
        "disconnected",
        _ev(
            _live(error_code="PV_TIMEOUT"),
            cf=_cf(consulted=True, registered=False),
            nm=_nm(consulted=True, registered=True),
            al=_al(consulted=True, configured=True),
        ),
    )
    assert res.likely_cause == "unregistered"
    assert any("alarm" in n.lower() for n in res.notes)


def test_disconnected_cf_miss_naming_miss_is_name_typo_candidate() -> None:
    res = derive_cause(
        "disconnected",
        _ev(
            _live(error_code="PV_TIMEOUT"),
            cf=_cf(consulted=True, registered=False),
            nm=_nm(consulted=True, registered=False),
        ),
    )
    assert res.likely_cause == "name_typo"
    assert any(
        "candidate" in n.lower() for n in res.notes
    )  # honest: not confirmable on name-server


def test_disconnected_cf_miss_naming_withheld_is_indeterminate() -> None:
    # No disambiguator → do NOT guess typo vs unregistered.
    res = derive_cause(
        "disconnected",
        _ev(
            _live(error_code="PV_TIMEOUT"),
            cf=_cf(consulted=True, registered=False),
            nm=_nm(consulted=False),
        ),
    )
    assert res.likely_cause == "indeterminate"


def test_disconnected_cf_miss_but_capped_is_indeterminate() -> None:
    # A capped CF query cannot assert 'not registered' → indeterminate, not name_typo.
    res = derive_cause(
        "disconnected",
        _ev(
            _live(error_code="PV_TIMEOUT"),
            cf=_cf(consulted=True, registered=False, capped=True),
            nm=_nm(consulted=True, registered=False),
        ),
    )
    assert res.likely_cause == "indeterminate"


def test_disconnected_cf_withheld_is_indeterminate() -> None:
    res = derive_cause(
        "disconnected", _ev(_live(error_code="PV_TIMEOUT"), cf=_cf(consulted=False, withheld=True))
    )
    assert res.likely_cause == "indeterminate"
    assert any("channelfinder" in s.lower() for s in res.next_steps)


def test_every_branch_stays_within_the_cause_enum() -> None:
    """Every path yields one of the 5 allowed causes — network_unreachable never appears."""
    allowed = {"healthy", "ioc_down", "name_typo", "unregistered", "indeterminate"}
    states: tuple[State, ...] = ("connected", "disconnected", "unknown")
    codes = (None, "PV_TIMEOUT", "PV_NOT_FOUND", "EPICS_CONNECTION_FAILED")
    for state in states:
        for code in codes:
            for cf in (
                _cf(),
                _cf(consulted=True, registered=True, pv_status="Inactive"),
                _cf(consulted=True, registered=False),
                _cf(consulted=False, withheld=True),
            ):
                res = derive_cause(
                    state, _ev(_live(connected=state == "connected", error_code=code), cf=cf)
                )
                assert res.likely_cause in allowed


# ---------------------------------------------------------------------------
# Async shell — mocked probe + planes
# ---------------------------------------------------------------------------


def _patch(monkeypatch: pytest.MonkeyPatch, name: str, value: object) -> None:
    monkeypatch.setattr(f"epics_pv_mcp.services.diagnose.{name}", value)


@pytest.mark.asyncio
async def test_shell_connected_healthy_cf_disabled_withholds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_pv_get(name: str, timeout: float | None = None) -> dict[str, object]:
        return {"value": 12, "alarm": {"severity_text": "NO_ALARM"}}

    async def fake_find(name: str, timeout: float = 5.0) -> dict[str, object]:
        return {"enabled": False, "channels": [], "total": 0, "note": "disabled"}

    _patch(monkeypatch, "pv_get", fake_pv_get)
    _patch(monkeypatch, "_find_channels", fake_find)
    _patch(monkeypatch, "get_config", lambda: EpicsConfig())

    report = await diagnose("SYS:PV")
    assert report.state == "connected"
    assert report.likely_cause == "healthy"
    # CF requested by default but disabled → withheld (never a false negative).
    assert report.withheld == ("channelfinder",)


@pytest.mark.asyncio
async def test_shell_disconnect_is_caught_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_pv_get(name: str, timeout: float | None = None) -> dict[str, object]:
        raise PVTimeoutError("timeout after 5s")

    async def fake_find(name: str, timeout: float = 5.0) -> dict[str, object]:
        return {"enabled": True, "channels": [], "total": 0, "capped": False}

    _patch(monkeypatch, "pv_get", fake_pv_get)
    _patch(monkeypatch, "_find_channels", fake_find)
    _patch(monkeypatch, "get_config", lambda: EpicsConfig())

    report = await diagnose("SYS:PV")  # must NOT raise
    assert report.state == "disconnected"
    assert report.evidence.live.error_code == "PV_TIMEOUT"


@pytest.mark.asyncio
async def test_shell_internal_error_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_pv_get(name: str, timeout: float | None = None) -> dict[str, object]:
        raise ValueError("boom")  # a non-EpicsError internal bug

    async def fake_find(name: str, timeout: float = 5.0) -> dict[str, object]:
        return {"enabled": False, "channels": [], "total": 0}

    _patch(monkeypatch, "pv_get", fake_pv_get)
    _patch(monkeypatch, "_find_channels", fake_find)
    _patch(monkeypatch, "get_config", lambda: EpicsConfig())

    report = await diagnose("SYS:PV")
    assert report.state == "unknown"
    assert report.likely_cause == "indeterminate"
    assert any("internal probe error" in n.lower() for n in report.notes)


@pytest.mark.asyncio
async def test_shell_cf_error_withholds_not_false_negative(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_pv_get(name: str, timeout: float | None = None) -> dict[str, object]:
        raise PVTimeoutError("timeout")

    async def fake_find(name: str, timeout: float = 5.0) -> dict[str, object]:
        raise EpicsConnectionError("ChannelFinder down")

    _patch(monkeypatch, "pv_get", fake_pv_get)
    _patch(monkeypatch, "_find_channels", fake_find)
    _patch(monkeypatch, "get_config", lambda: EpicsConfig())

    report = await diagnose("SYS:PV")
    assert report.state == "disconnected"
    assert report.likely_cause == "indeterminate"
    assert "channelfinder" in report.withheld


@pytest.mark.asyncio
async def test_shell_naming_gate_empty_url_withholds_no_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """check_naming=True but naming_url empty → withheld, NamingServiceClient NEVER built."""

    async def fake_pv_get(name: str, timeout: float | None = None) -> dict[str, object]:
        raise PVTimeoutError("timeout")

    async def fake_find(name: str, timeout: float = 5.0) -> dict[str, object]:
        return {"enabled": True, "channels": [], "total": 0, "capped": False}

    def _boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("NamingServiceClient must not be constructed when naming_url is empty")

    _patch(monkeypatch, "pv_get", fake_pv_get)
    _patch(monkeypatch, "_find_channels", fake_find)
    _patch(monkeypatch, "NamingServiceClient", _boom)
    _patch(monkeypatch, "get_config", lambda: EpicsConfig(naming_url=""))

    report = await diagnose("SYS:PV", check_naming=True)
    assert report.evidence.naming.consulted is False
    assert "naming" in report.withheld


@pytest.mark.asyncio
async def test_shell_naming_enabled_splits_unregistered(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_pv_get(name: str, timeout: float | None = None) -> dict[str, object]:
        raise PVTimeoutError("timeout")

    async def fake_find(name: str, timeout: float = 5.0) -> dict[str, object]:
        return {"enabled": True, "channels": [], "total": 0, "capped": False}  # CF-miss

    class _FakeNaming:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def check_connectivity(self) -> bool:
            return True

        def validate_name(self, name: str) -> NameStatus:
            return NameStatus(registered=True, status="ACTIVE", message="ok")

    _patch(monkeypatch, "pv_get", fake_pv_get)
    _patch(monkeypatch, "_find_channels", fake_find)
    _patch(monkeypatch, "NamingServiceClient", _FakeNaming)
    _patch(monkeypatch, "get_config", lambda: EpicsConfig(naming_url="http://naming"))

    report = await diagnose("SYS:PV:Val", check_naming=True)
    assert report.evidence.naming.consulted is True
    assert report.likely_cause == "unregistered"


@pytest.mark.asyncio
async def test_shell_gatherer_is_total_on_unexpected_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """TOTAL invariant: a NON-EpicsError from a plane helper withholds, never crashes diagnose()."""

    async def fake_pv_get(name: str, timeout: float | None = None) -> dict[str, object]:
        return {"value": 12, "alarm": {"severity_text": "NO_ALARM"}}

    async def boom_find(name: str, timeout: float = 5.0) -> dict[str, object]:
        raise ValueError("unexpected client/projection bug")  # NOT an EpicsError

    _patch(monkeypatch, "pv_get", fake_pv_get)
    _patch(monkeypatch, "_find_channels", boom_find)
    _patch(monkeypatch, "get_config", lambda: EpicsConfig())

    report = await diagnose("SYS:PV")  # must NOT raise despite the ValueError
    assert report.state == "connected"
    assert "channelfinder" in report.withheld
    assert report.evidence.channelfinder.consulted is False


def test_naming_gate_left_shared_client_untouched() -> None:
    """QA-delta 3 guard: the diagnose naming gate lives at config level; the shared client and its
    other callers (crossplane tool + CLI) still use the bare constructor's built-in prod default."""
    from epics_pv_mcp.services.naming_client import NamingServiceClient

    # A bare NamingServiceClient() (as tools/crossplane.py:105 and cli_crossplane.py:91 call it)
    # must still default to the production URL — the diagnose gate must NOT have rewired it.
    assert NamingServiceClient().base_url == NamingServiceClient.DEFAULT_URLS["prod"]


# ---------------------------------------------------------------------------
# QA regression: the two membership planes must not turn a false negative into a
# confident wrong cause (Findings A + B) — the state stays correct either way.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shell_field_suffixed_pv_normalized_for_channelfinder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finding A: the CF plane must query/match the BARE record name, not the raw field-suffixed PV.

    ChannelFinder/RecSync register ``…:Val``, never a field reference ``…:Val.EGU``. Without
    normalization a registered field PV whose IOC is down false-misses in CF and the cause is
    mis-classified away from ioc_down (to unregistered/name_typo/indeterminate).
    """
    captured: dict[str, str] = {}

    async def fake_pv_get(name: str, timeout: float | None = None) -> dict[str, object]:
        raise PVTimeoutError("timeout")  # the field PV is disconnected (IOC down)

    async def fake_find(name: str, timeout: float = 5.0) -> dict[str, object]:
        captured["name"] = name
        # CF registers the BARE record, with a last-known status that is not up.
        return {
            "enabled": True,
            "capped": False,
            "total": 1,
            "channels": [
                {
                    "name": "SYS:DEV:Val",
                    "ioc_name": "IOC1",
                    "host_name": "host1",
                    "properties": {"pvStatus": "Inactive"},
                }
            ],
        }

    _patch(monkeypatch, "pv_get", fake_pv_get)
    _patch(monkeypatch, "_find_channels", fake_find)
    _patch(monkeypatch, "get_config", lambda: EpicsConfig())

    report = await diagnose("SYS:DEV:Val.EGU")
    assert captured["name"] == "SYS:DEV:Val"  # queried by record name (the .EGU suffix stripped)
    assert report.evidence.channelfinder.registered is True  # matched despite the field suffix
    assert report.likely_cause == "ioc_down"  # not unregistered/name_typo/indeterminate


@pytest.mark.asyncio
async def test_shell_naming_unreachable_is_withheld_not_false_typo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finding B: a Naming transport failure must WITHHOLD, never become a confident name_typo.

    The shared client's ``validate_name`` swallows a transport error into a definitive
    ``registered=False``; the gatherer must probe connectivity first so an UNREACHABLE service is
    withheld (``withheld != no``) instead of read as a spelling mistake.
    """

    async def fake_pv_get(name: str, timeout: float | None = None) -> dict[str, object]:
        raise PVTimeoutError("timeout")

    async def fake_find(name: str, timeout: float = 5.0) -> dict[str, object]:
        return {"enabled": True, "channels": [], "total": 0, "capped": False}  # CF-miss

    class _DownNaming:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def check_connectivity(self) -> bool:
            raise NamingServiceConnectionError("connection refused")

        def validate_name(self, name: str) -> NameStatus:
            # what the shared client does on an outage: swallow to a false 'not registered'.
            return NameStatus(registered=False, status="", message="not registered")

    _patch(monkeypatch, "pv_get", fake_pv_get)
    _patch(monkeypatch, "_find_channels", fake_find)
    _patch(monkeypatch, "NamingServiceClient", _DownNaming)
    _patch(monkeypatch, "get_config", lambda: EpicsConfig(naming_url="http://naming"))

    report = await diagnose("SYS:DEV:Val", check_naming=True)
    assert "naming" in report.withheld
    assert report.evidence.naming.consulted is False
    assert report.likely_cause != "name_typo"
    assert report.likely_cause == "indeterminate"


@pytest.mark.asyncio
async def test_shell_naming_reachable_unregistered_stays_name_typo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finding B guard: a REACHABLE Naming service reporting 'not registered' must still yield
    name_typo — the connectivity probe must not over-withhold a genuine negative."""

    async def fake_pv_get(name: str, timeout: float | None = None) -> dict[str, object]:
        raise PVTimeoutError("timeout")

    async def fake_find(name: str, timeout: float = 5.0) -> dict[str, object]:
        return {"enabled": True, "channels": [], "total": 0, "capped": False}  # CF-miss

    class _UpUnregNaming:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def check_connectivity(self) -> bool:
            return True

        def validate_name(self, name: str) -> NameStatus:
            return NameStatus(registered=False, status="", message="not registered")

    _patch(monkeypatch, "pv_get", fake_pv_get)
    _patch(monkeypatch, "_find_channels", fake_find)
    _patch(monkeypatch, "NamingServiceClient", _UpUnregNaming)
    _patch(monkeypatch, "get_config", lambda: EpicsConfig(naming_url="http://naming"))

    report = await diagnose("SYS:DEV:Val", check_naming=True)
    assert report.likely_cause == "name_typo"
    assert "naming" not in report.withheld
