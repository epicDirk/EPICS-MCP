"""Tests for epics_pv_mcp.tools.validate."""

from unittest.mock import AsyncMock, patch

import pytest

from epics_pv_mcp.errors import EpicsError, PVTimeoutError
from epics_pv_mcp.tools.validate import _validate_pvs


async def test_validate_pvs_all_connected() -> None:
    with patch(
        "epics_pv_mcp.tools.validate.pv_get",
        new_callable=AsyncMock,
        return_value={"pv_name": "X", "value": 1},
    ):
        result = await _validate_pvs(pvs=["PV:1", "PV:2"])

    assert result["connected"] == 2
    assert result["disconnected"] == 0
    assert result["total"] == 2


async def test_validate_pvs_mixed() -> None:
    async def _mock_pv_get(name: str, timeout: float | None = None) -> dict[str, object]:
        if name == "PV:1":
            return {"pv_name": "PV:1", "value": 1}
        raise PVTimeoutError(f"Timeout getting PV '{name}'")

    with patch(
        "epics_pv_mcp.tools.validate.pv_get",
        side_effect=_mock_pv_get,
    ):
        result = await _validate_pvs(pvs=["PV:1", "PV:2"])

    assert result["connected"] == 1
    assert result["disconnected"] == 1


async def test_validate_pvs_no_input() -> None:
    with pytest.raises(EpicsError, match="Provide either pvs list or file_path") as exc_info:
        await _validate_pvs(pvs=None, file_path=None)

    assert exc_info.value.error_code == "INVALID_INPUT"


async def test_validate_pvs_file_path_no_core() -> None:
    """file_path mode without phoebus_mcp_core raises MISSING_DEPENDENCY."""
    with (
        patch.dict("sys.modules", {"phoebus_mcp_core": None, "phoebus_mcp_core.bob_parser": None}),
        pytest.raises(EpicsError, match="phoebus-mcp-core") as exc_info,
    ):
        await _validate_pvs(file_path="test.bob")

    assert exc_info.value.error_code == "MISSING_DEPENDENCY"


async def test_validate_pvs_file_path_with_core() -> None:
    """file_path mode mit (gefaktem) phoebus_mcp_core: extract_pvs liefert die
    PV-Liste, die dann validiert wird. (Belegt, dass der Pfad jetzt überhaupt
    funktioniert — vorher importierte er ein nicht existentes parse_bob.)"""
    import types

    fake_parser = types.ModuleType("phoebus_mcp_core.bob_parser")
    fake_parser.extract_pvs = lambda _path: ["PV:1", "PV:2"]  # type: ignore[attr-defined]
    fake_core = types.ModuleType("phoebus_mcp_core")
    fake_core.bob_parser = fake_parser  # type: ignore[attr-defined]

    with (
        patch.dict(
            "sys.modules",
            {"phoebus_mcp_core": fake_core, "phoebus_mcp_core.bob_parser": fake_parser},
        ),
        patch(
            "epics_pv_mcp.tools.validate.pv_get",
            new_callable=AsyncMock,
            return_value={"pv_name": "X", "value": 1},
        ),
    ):
        result = await _validate_pvs(file_path="test.bob")

    assert result["total"] == 2
    assert result["connected"] == 2
