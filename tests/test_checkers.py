"""Tests for services/checkers — the services-layer edge to the four REST planes (M8/M9/C8).

Covers the branches the tool/crossplane/coverage tests don't reach: the checker adapters'
error→RuntimeError translation (so the pure cores WITHHOLD a cell, never false-flag it), the
build_* config gates, and the query_* error→EpicsConnectionError translation.
"""

from __future__ import annotations

import pytest

from epics_pv_mcp.config import EpicsConfig
from epics_pv_mcp.errors import EpicsConnectionError
from epics_pv_mcp.services import checkers
from epics_pv_mcp.services.alarm_exceptions import AlarmConnectionError
from epics_pv_mcp.services.archiver_exceptions import ArchiverConnectionError
from epics_pv_mcp.services.channelfinder_exceptions import ChannelFinderConnectionError

# --- checker adapters: error → RuntimeError (the pure core withholds, never false-flags) ---


def test_archiver_checker_translates_error_to_runtimeerror(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FailClient:
        def __init__(self, *args: object, **kwargs: object) -> None: ...

        def is_archived(self, pv: str) -> tuple[bool, str]:
            raise ArchiverConnectionError("down")

    monkeypatch.setattr(checkers, "ArchiverClient", _FailClient)
    checker = checkers.ArchiverChecker("http://arch", None)
    with pytest.raises(RuntimeError, match="Archiver query failed"):
        checker.is_archived("X")


def test_archiver_checker_success_returns_bool(monkeypatch: pytest.MonkeyPatch) -> None:
    class _OkClient:
        def __init__(self, *args: object, **kwargs: object) -> None: ...

        def is_archived(self, pv: str) -> tuple[bool, str]:
            return True, "Being archived"

    monkeypatch.setattr(checkers, "ArchiverClient", _OkClient)
    assert checkers.ArchiverChecker("http://arch", None).is_archived("X") is True


def test_alarm_checker_translates_error_to_runtimeerror(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FailClient:
        def __init__(self, *args: object, **kwargs: object) -> None: ...

        def is_alarm_configured(
            self, pv: str, config_name: str = "Accelerator"
        ) -> tuple[bool, dict[str, object]]:
            raise AlarmConnectionError("down")

    monkeypatch.setattr(checkers, "AlarmClient", _FailClient)
    checker = checkers.AlarmConfigChecker("http://alarm", None)
    with pytest.raises(RuntimeError, match="Alarm query failed"):
        checker.is_alarm_configured("X")


def test_alarm_checker_success_returns_bool(monkeypatch: pytest.MonkeyPatch) -> None:
    class _OkClient:
        def __init__(self, *args: object, **kwargs: object) -> None: ...

        def is_alarm_configured(
            self, pv: str, config_name: str = "Accelerator"
        ) -> tuple[bool, dict[str, object]]:
            return True, {}

    monkeypatch.setattr(checkers, "AlarmClient", _OkClient)
    assert checkers.AlarmConfigChecker("http://alarm", None).is_alarm_configured("X") is True


# --- build_* factories: config gates (not requested / URL unset → None; both set → built) ---


def test_build_archiver_checker_gates_on_request_and_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(checkers, "get_config", lambda: EpicsConfig(archiver_url=""))
    assert checkers.build_archiver_checker(False) is None  # not requested
    assert checkers.build_archiver_checker(True) is None  # requested but URL unset
    monkeypatch.setattr(checkers, "get_config", lambda: EpicsConfig(archiver_url="http://arch"))
    assert checkers.build_archiver_checker(True) is not None


def test_build_alarm_checker_gates_on_request_and_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(checkers, "get_config", lambda: EpicsConfig(alarm_url=""))
    assert checkers.build_alarm_checker(False, "Accelerator") is None  # not requested
    assert checkers.build_alarm_checker(True, "Accelerator") is None  # requested but URL unset
    monkeypatch.setattr(checkers, "get_config", lambda: EpicsConfig(alarm_url="http://alarm"))
    assert checkers.build_alarm_checker(True, "Accelerator") is not None


# --- query_* error branches: the per-service error → EpicsConnectionError translation ---


async def test_query_archived_translates_error_to_epics_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(checkers, "get_config", lambda: EpicsConfig(archiver_url="http://arch"))

    class _FailClient:
        def __init__(self, *args: object, **kwargs: object) -> None: ...

        def is_archived(self, pv: str) -> tuple[bool, str]:
            raise ArchiverConnectionError("down")

    monkeypatch.setattr(checkers, "ArchiverClient", _FailClient)
    with pytest.raises(EpicsConnectionError, match="Archiver"):
        await checkers.query_archived("X")


async def test_query_alarm_configured_translates_error_to_epics_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(checkers, "get_config", lambda: EpicsConfig(alarm_url="http://alarm"))

    class _FailClient:
        def __init__(self, *args: object, **kwargs: object) -> None: ...

        def is_alarm_configured(
            self, pv: str, config_name: str = "Accelerator"
        ) -> tuple[bool, dict[str, object]]:
            raise AlarmConnectionError("down")

    monkeypatch.setattr(checkers, "AlarmClient", _FailClient)
    with pytest.raises(EpicsConnectionError, match="Alarm Logger"):
        await checkers.query_alarm_configured("X")


async def test_query_channels_translates_error_to_epics_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(checkers, "get_config", lambda: EpicsConfig(channelfinder_url="http://cf"))

    class _FailClient:
        def __init__(self, *args: object, **kwargs: object) -> None: ...

        def find_channels(self, name_pattern: str, max_results: int = 500) -> list[object]:
            raise ChannelFinderConnectionError("down")

    monkeypatch.setattr(checkers, "ChannelFinderClient", _FailClient)
    with pytest.raises(EpicsConnectionError, match="ChannelFinder"):
        await checkers.query_channels("X*")
