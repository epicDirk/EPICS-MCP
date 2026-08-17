"""GQ-18 (c): the find_device claim, split off because it needs the display engine.

WHY A SECOND MODULE. The sibling ``test_gq18_channel_claims.py`` holds the pins that run in every
lane. This one cannot: ``find_device`` is registered only when ``opi_navigation`` is importable, so
in a core-only install ``mcp.list_tools()`` does not carry it at all and a wire pin here would fail
for the wrong reason. It is therefore listed in ``conftest.ENGINE_COUPLED_MODULES`` and dropped at
collection when the engine is absent, exactly like ``test_find_device_tool.py``.

⛔ **The honest cost of that split, stated rather than discovered later: CI never runs this file.**
``.github/workflows/ci.yml`` syncs with ``--extra dev`` and no ``--group displays``, on purpose, so
that it tests the standalone core a public user gets. A green CI run is therefore no evidence about
the two assertions below; the evidence is a local run with the engine installed, or a run with
``EPICS_MCP_REQUIRE_DISPLAYS=1``, which turns the silent skip into a refusal.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

# MODULE LEVEL on purpose, not inside the tests. ``test_engine_gate.py`` derives the coupled-module
# list from module-level imports that REACH the engine, so a lazy import here would leave this file
# listed in ``ENGINE_COUPLED_MODULES`` while the guard sees no coupling and calls the entry stale.
# Measured: it did, on the first run. The honest shape is the one the guard can see.
from epics_mcp.server import mcp
from epics_mcp.tools.find_device import _find_device

#: One operator screen that both READS and WRITES the same device: a ``textupdate`` on its status
#: and a ``textentry`` on its command. That pairing IS the claim under test, so the fixture cannot
#: be simplified to one widget without the test losing its subject.
_BOB = (
    '<display version="2.0.0"><name>DLN01</name>'
    '<widget type="textupdate"><name>s</name>'
    "<pv_name>DEV-TEST01:Ctrl-EVR-01:status</pv_name></widget>"
    '<widget type="textentry"><name>c</name>'
    "<pv_name>DEV-TEST01:Ctrl-EVR-01:Cmd</pv_name></widget>"
    "</display>"
)


def _displays(tmp_path: Path) -> Path:
    displays = tmp_path / "displays"
    displays.mkdir()
    (displays / "panel.bob").write_text(_BOB, encoding="utf-8")
    return displays


@pytest.mark.asyncio
async def test_find_device_description_names_the_roles() -> None:
    """Wire pin (GQ-18 (c), unit 4): the description says a screen comes back with its roles.

    Measured before it was written: "role" had ZERO hits across the whole delivered channel, the
    tool descriptions and the operator guide together, while the answer had carried
    ``screens[].roles`` all along. So the question "which screens can WRITE this device?" was
    answerable and unadvertised, which is the same as unanswerable for a caller who reads the
    channel rather than the source.
    """
    tools = {t.name: t for t in [_t.to_mcp_tool() for _t in await mcp.list_tools()]}
    description = " ".join((tools["find_device"].description or "").split())
    assert "roles it uses the device in, read and/or write" in description, (
        "the roles half of the answer is unadvertised again"
    )
    assert "screens[].roles" in description, "the field name is gone, so the claim is unactionable"


@pytest.mark.asyncio
@patch("epics_mcp.tools.find_device.pv_get_batch", new_callable=AsyncMock)
async def test_a_write_role_reaches_both_halves_of_the_answer(
    mock_batch: AsyncMock, tmp_path: Path
) -> None:
    """Measuring proof for the same claim, over BOTH halves the tool answers with.

    ``find_device`` answers ``{report, markdown}``, and the guide warns that the rendered half is
    the poorer one. A claim about what "comes back" is therefore only true if it holds for the
    half a caller works from AND the half a human reads, so both are asserted here rather than
    the convenient one.

    Provably red: drop ``roles=display.roles`` from the ``ScreenMatch`` construction in
    ``services/device_lookup.py``. The report half then carries an empty tuple and the markdown
    half renders the roles-implied placeholder.
    """
    mock_batch.return_value = {"results": [], "errors": []}

    result = await _find_device("DEV-TEST01:Ctrl-EVR-01", str(_displays(tmp_path)))

    report = result["report"]
    assert isinstance(report, dict)
    screens = report["screens"]
    assert isinstance(screens, list) and len(screens) == 1, "the fixture screen was not found"
    assert set(screens[0]["roles"]) == {"read", "write"}, (
        "the report half no longer carries both roles, so the description's promise is false"
    )

    markdown = result["markdown"]
    assert isinstance(markdown, str)
    assert "[read/write]" in markdown, (
        "the rendered half no longer shows the roles; a human reading it cannot see that this "
        "screen can write the device"
    )
