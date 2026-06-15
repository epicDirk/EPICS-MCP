"""Tests für epics_client-Hilfsfunktionen (ohne EPICS-Verbindung)."""

from epics_pv_mcp.errors import EpicsConnectionError, PVNotFoundError
from epics_pv_mcp.services.epics_client import _classify_p4p_error


def test_classify_not_found() -> None:
    err = _classify_p4p_error("X:Y", Exception("PV not found"), action="accessing")
    assert isinstance(err, PVNotFoundError)


def test_classify_search_is_not_found() -> None:
    err = _classify_p4p_error("X:Y", Exception("search failed for channel"), action="accessing")
    assert isinstance(err, PVNotFoundError)


def test_classify_other_is_connection() -> None:
    err = _classify_p4p_error("X:Y", Exception("broken pipe"), action="writing")
    assert isinstance(err, EpicsConnectionError)
    assert "X:Y" in str(err)
    assert "writing" in str(err)
