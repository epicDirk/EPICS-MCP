"""Tests for epics_mcp.tools.validate."""

from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastmcp.exceptions import ToolError

from epics_mcp.errors import EpicsError
from epics_mcp.tools.validate import _validate_pvs

# An operator-facing parent that embeds a fragment and binds its $(PRP) macro; the
# fragment's PV is templated on $(PRP), so its resolved value is LIFTED to the parent
# display, display_path-keying on the fragment would miss it; origin_file recovers it.
_PARENT = (
    '<display version="2.0.0"><name>Overview</name>'
    '<widget type="embedded"><name>e</name>'
    "<file>frag.bob</file>"
    "<macros><PRP>DEV-TEST01:Spu01</PRP></macros>"
    "</widget></display>"
)
_FRAGMENT = (
    '<display version="2.0.0"><name>Fragment</name>'
    '<widget type="textupdate"><name>s</name>'
    "<pv_name>$(PRP):Val</pv_name></widget></display>"
)


def _dataset(tmp_path: Path) -> tuple[Path, Path]:
    """Write an operator parent embedding a templated fragment; return (root, fragment)."""
    root = tmp_path / "ds"
    root.mkdir()
    (root / "overview.bob").write_text(_PARENT, encoding="utf-8")
    fragment = root / "frag.bob"
    fragment.write_text(_FRAGMENT, encoding="utf-8")
    return root, fragment


async def test_validate_pvs_all_connected() -> None:
    async def fake_batch(names: list[str], timeout: float | None = None) -> dict[str, object]:
        return {"results": [{"pv_name": n, "value": 1} for n in names], "errors": []}

    with patch("epics_mcp.tools.validate.pv_get_batch", side_effect=fake_batch):
        result = await _validate_pvs(pvs=["PV:1", "PV:2"])

    assert result["connected"] == 2
    assert result["disconnected"] == 0
    assert result["total"] == 2


async def test_validate_pvs_mixed() -> None:
    async def fake_batch(names: list[str], timeout: float | None = None) -> dict[str, object]:
        # M6: validate now delegates classification to pv_get_batch (results vs errors).
        return {
            "results": [{"pv_name": n, "value": 1} for n in names if n == "PV:1"],
            "errors": [{"pv_name": n, "error": "Timeout"} for n in names if n != "PV:1"],
        }

    with patch("epics_mcp.tools.validate.pv_get_batch", side_effect=fake_batch):
        result = await _validate_pvs(pvs=["PV:1", "PV:2"])

    assert result["connected"] == 1
    assert result["disconnected"] == 1


async def test_validate_pvs_chunks_by_max_batch_size() -> None:
    """M6 coverage gap: with more PVs than ``max_batch_size`` the multi-chunk loop must call
    pv_get_batch once per chunk and accumulate connected/disconnected ACROSS chunks. All existing
    tests pass ≤2 PVs (one iteration), so the loop body was never exercised for >1 chunk. Here 5
    PVs at max_batch_size=2 → 3 chunks [2,2,1]; PV:2 and PV:4 disconnect → connected 3, disc. 2."""
    import epics_mcp.config as config_module
    from epics_mcp.config import EpicsConfig

    async def fake_batch(names: list[str], timeout: float | None = None) -> dict[str, object]:
        return {
            "results": [{"pv_name": n, "value": 1} for n in names if n not in ("PV:2", "PV:4")],
            "errors": [{"pv_name": n, "error": "Timeout"} for n in names if n in ("PV:2", "PV:4")],
        }

    mock = AsyncMock(side_effect=fake_batch)
    config_module._config = EpicsConfig(max_batch_size=2)
    try:
        with patch("epics_mcp.tools.validate.pv_get_batch", mock):
            result = await _validate_pvs(pvs=["PV:1", "PV:2", "PV:3", "PV:4", "PV:5"])
    finally:
        config_module._config = None

    assert mock.await_count == 3  # 5 PVs / max_batch_size 2 → chunks of 2, 2, 1
    assert [call.args[0] for call in mock.await_args_list] == [
        ["PV:1", "PV:2"],
        ["PV:3", "PV:4"],
        ["PV:5"],
    ]
    assert result["total"] == 5
    assert result["connected"] == 3
    assert result["disconnected"] == 2


async def test_validate_pvs_preserves_input_order() -> None:
    """The ``pvs`` output must follow the caller's input order, NOT connected-then-disconnected
    grouping. Input [A(up), B(down), C(up)] → output in that exact order, each with its own status.
    Before the fix the output was grouped [A, C, B] (all connected first)."""

    async def fake_batch(names: list[str], timeout: float | None = None) -> dict[str, object]:
        return {
            "results": [{"pv_name": n, "value": 1} for n in names if n != "PV:B"],
            "errors": [{"pv_name": n, "error": "Timeout"} for n in names if n == "PV:B"],
        }

    with patch("epics_mcp.tools.validate.pv_get_batch", side_effect=fake_batch):
        result = await _validate_pvs(pvs=["PV:A", "PV:B", "PV:C"])

    pvs = result["pvs"]
    assert isinstance(pvs, list)
    assert [(p["pv_name"], p["status"]) for p in pvs] == [
        ("PV:A", "connected"),
        ("PV:B", "disconnected"),
        ("PV:C", "connected"),
    ]
    assert result["connected"] == 2
    assert result["disconnected"] == 1


async def test_validate_pvs_no_input() -> None:
    with pytest.raises(EpicsError, match="Provide either pvs list or file_path") as exc_info:
        await _validate_pvs(pvs=None, file_path=None)

    assert exc_info.value.error_code == "INVALID_INPUT"


async def test_validate_pvs_file_path_fragment_resolves_via_origin_file(tmp_path: Path) -> None:
    """G1: an embedded fragment's macro PV resolves (lifted to its parent) and is
    recovered via origin_file aggregation, the exact case display_path-keying returns
    0 for. The concrete, macro-resolved channel is what gets connectivity-checked."""
    root, fragment = _dataset(tmp_path)
    mock = AsyncMock(
        return_value={"results": [{"pv_name": "DEV-TEST01:Spu01:Val", "value": 1}], "errors": []}
    )
    with patch("epics_mcp.tools.validate.pv_get_batch", mock):
        result = await _validate_pvs(file_path=str(fragment), displays_dir=str(root))

    assert result["total"] == 1
    assert result["connected"] == 1
    # The resolved channel DEV-TEST01:Spu01:Val, NOT the raw $(PRP):Val, read as one batch.
    mock.assert_awaited_once_with(["DEV-TEST01:Spu01:Val"], None)


async def test_validate_pvs_file_path_not_under_displays_dir(tmp_path: Path) -> None:
    """A file_path outside displays_dir is a clean INVALID_INPUT, not an [INTERNAL] leak."""
    root, _ = _dataset(tmp_path)
    outside = tmp_path / "outside.bob"
    outside.write_text(_FRAGMENT, encoding="utf-8")
    with pytest.raises(EpicsError) as exc_info:
        await _validate_pvs(file_path=str(outside), displays_dir=str(root))
    assert exc_info.value.error_code == "INVALID_INPUT"


async def test_validate_pvs_file_path_zero_real_pvs_is_total_zero(tmp_path: Path) -> None:
    """A file with no resolved ca/pva channels (only loc://) is total:0, NOT an error."""
    root = tmp_path / "ds"
    root.mkdir()
    local = root / "local.bob"
    local.write_text(
        '<display version="2.0.0"><name>L</name>'
        '<widget type="textupdate"><name>s</name>'
        "<pv_name>loc://x(0)</pv_name></widget></display>",
        encoding="utf-8",
    )
    result = await _validate_pvs(file_path=str(local), displays_dir=str(root))
    assert result["total"] == 0
    assert result["pvs"] == []


async def test_validate_pvs_file_path_outside_allowed_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G3: file_path mode honors the opt-in allowed_roots boundary too."""
    import epics_mcp.config as config_module

    root, fragment = _dataset(tmp_path)  # fragment is inside root, but outside `allowed`
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setenv("EPICS_MCP_ALLOWED_ROOTS", str(allowed))
    config_module._config = None
    try:
        with pytest.raises(EpicsError) as exc_info:
            await _validate_pvs(file_path=str(fragment), displays_dir=str(root))
        assert exc_info.value.error_code == "PATH_OUTSIDE_WORKSPACE"
    finally:
        config_module._config = None


async def test_validate_pvs_file_path_without_displays_dir_walks_parent(tmp_path: Path) -> None:
    """displays_dir=None walks the file's own directory (the G3 walked-root path)."""
    root, _ = _dataset(tmp_path)

    async def fake_batch(names: list[str], timeout: float | None = None) -> dict[str, object]:
        return {"results": [{"pv_name": n, "value": 1} for n in names], "errors": []}

    with patch("epics_mcp.tools.validate.pv_get_batch", side_effect=fake_batch):
        # The parent display 'overview.bob' is operator-facing in root; querying it
        # without displays_dir uses file.parent (== root) as the walked root.
        result = await _validate_pvs(file_path=str(root / "overview.bob"))
    assert isinstance(result["total"], int)  # resolves (no MISSING_DEPENDENCY / crash)


async def test_validate_pvs_no_displays_dir_honors_allowed_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G3: even in file_path-only mode (displays_dir=None) the allowed_roots boundary
    is enforced, a file_path outside the allowed roots is rejected before any walk."""
    import epics_mcp.config as config_module

    _, fragment = _dataset(tmp_path)
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setenv("EPICS_MCP_ALLOWED_ROOTS", str(allowed))
    config_module._config = None
    try:
        with pytest.raises(EpicsError) as exc_info:
            await _validate_pvs(file_path=str(fragment))  # no displays_dir
        assert exc_info.value.error_code == "PATH_OUTSIDE_WORKSPACE"
    finally:
        config_module._config = None


async def test_non_bob_file_path_is_refused_without_running_the_inventory(tmp_path: Path) -> None:
    """QA-33: the walk is the expensive half, and for a non-.bob file its outcome is settled.

    Asserted on the CAUSE (the inventory was never run), not on elapsed time: a duration
    assertion would be flaky on a loaded machine and would still pass if the walk merely got
    faster. The spy also proves the refusal sits BEFORE the walk rather than merely somewhere.
    That this patch reaches the code despite ``asyncio.to_thread`` is not assumed either, the
    pre-existing ``test_validate_pvs_file_path_context_capped_note`` patches the same name.
    """
    root, _ = _dataset(tmp_path)
    other = root / "notes.txt"
    other.write_text(_FRAGMENT, encoding="utf-8")  # valid display XML, wrong suffix

    spy = Mock(side_effect=AssertionError("the inventory walk must not run for a non-.bob file"))
    with (
        patch("epics_mcp.tools.validate.analyze_pv_inventory", spy),
        pytest.raises(EpicsError) as exc_info,
    ):
        await _validate_pvs(file_path=str(other), displays_dir=str(root))

    assert exc_info.value.error_code == "INVALID_INPUT"
    assert ".bob" in str(exc_info.value), "the message must name the suffix it wants"
    assert "pv_names" in str(exc_info.value), "the message must name the way out"
    spy.assert_not_called()


async def test_uppercase_bob_suffix_is_accepted_because_the_engine_accepts_it(
    tmp_path: Path,
) -> None:
    """The suffix comparison folds case, because ``find_bob_files`` does (``suffix.lower()``).

    Measured before this was written: a file named ``UPPER.BOB`` IS collected by the engine, so
    rejecting it would refuse a file the inventory happily reads. The naive ``endswith('.bob')``
    is exactly the mutant this pins, and it fails here while every other test stays green.
    """
    root = tmp_path / "ds"
    root.mkdir()
    shouty = root / "UPPER.BOB"
    shouty.write_text(_FRAGMENT, encoding="utf-8")

    # No displays_dir: the file's own directory is walked. The fragment's $(PRP) stays unbound, so
    # nothing resolves and the honest answer is total 0, the point is that it is NOT a refusal.
    result = await _validate_pvs(file_path=str(shouty))
    assert result["total"] == 0


async def test_bob_outside_displays_dir_is_refused_without_running_the_inventory(
    tmp_path: Path,
) -> None:
    """The second input that settles the answer on its own, and the one the entry did not name.

    A .bob outside the walked root can never match an ``origin_file``, yet the check used to run
    AFTER the inventory: on a large dataset that is ~40 s spent to reach a verdict that was fixed
    before the first file was opened. Same spy as above; the pre-existing
    ``test_validate_pvs_file_path_not_under_displays_dir`` keeps guarding the error code itself.
    """
    root, _ = _dataset(tmp_path)
    outside = tmp_path / "outside.bob"
    outside.write_text(_FRAGMENT, encoding="utf-8")

    spy = Mock(side_effect=AssertionError("the inventory walk must not run for an outside file"))
    with (
        patch("epics_mcp.tools.validate.analyze_pv_inventory", spy),
        pytest.raises(EpicsError) as exc_info,
    ):
        await _validate_pvs(file_path=str(outside), displays_dir=str(root))

    assert exc_info.value.error_code == "INVALID_INPUT"
    spy.assert_not_called()


def test_the_engine_really_ignores_every_suffix_but_bob(tmp_path: Path) -> None:
    """The COUPLING, against the real engine: our rule must be the engine's rule.

    ``_DISPLAY_SUFFIX`` is a local constant, so nothing but this test stops the two from drifting
    apart, and a drift is silent in the dangerous direction (we refuse a file the engine would
    have read). Deliberately calls the genuine ``find_bob_files`` rather than restating its logic:
    a test that re-implements the rule it checks proves only that the author is consistent.
    """
    from opi_navigation.discovery import find_bob_files

    from epics_mcp.tools.validate import _DISPLAY_SUFFIX

    for name in ("kept.bob", "KEPT2.BOB", "skipped.txt", "skipped.bob.bak", "no_suffix"):
        (tmp_path / name).write_text(_FRAGMENT, encoding="utf-8")

    collected = set(find_bob_files(tmp_path))
    assert collected == {"kept.bob", "KEPT2.BOB"}, (
        "the engine's file selection changed; _DISPLAY_SUFFIX and the refusal in _run_validate "
        f"have to follow it (collected: {sorted(collected)})"
    )
    assert all(Path(name).suffix.lower() == _DISPLAY_SUFFIX for name in collected)


async def test_the_refusal_reaches_a_client_over_the_wire(tmp_path: Path) -> None:
    """What the USER sees, not what the internal function raises.

    A unit test on ``_validate_pvs`` cannot show how the refusal is packaged: the error crosses
    ``translate_epics_errors``, which turns it into a ``ToolError`` carrying the code as a tag.
    The in-memory transport routes through the same registration a real client uses, so the
    message asserted here is the message that arrives.
    """
    from fastmcp import Client

    from epics_mcp.server import _DISPLAY_TOOLS_AVAILABLE, mcp

    if not _DISPLAY_TOOLS_AVAILABLE:  # pragma: no cover - core-only install, tool not registered
        pytest.skip("validate_pvs is display-gated and this install has no displays group")

    root, _ = _dataset(tmp_path)
    other = root / "notes.txt"
    other.write_text(_FRAGMENT, encoding="utf-8")

    with pytest.raises(ToolError) as exc_info:
        async with Client(mcp) as client:
            await client.call_tool(
                "validate_pvs", {"file_path": str(other), "displays_dir": str(root)}
            )

    message = str(exc_info.value)
    assert message.startswith("[INVALID_INPUT]"), (
        f"the client must receive the tagged, curated refusal, got: {message!r}"
    )
    assert ".bob" in message


async def test_validate_pvs_file_path_context_capped_note(tmp_path: Path) -> None:
    """G1: when the file's macro expansion hit the per-display context cap, the result
    carries an honest 'lower bound' note (a minimal inventory is mocked to flag it)."""
    from opi_navigation.pv_analysis import (
        DisplayPvInventory,
        ExpandedPv,
        PvDiagnostics,
        PvInventory,
    )

    root = tmp_path / "ds"
    root.mkdir()
    frag = root / "frag.bob"
    frag.write_text('<display version="2.0.0"><name>F</name></display>', encoding="utf-8")
    # One resolved ca PV whose origin is frag.bob, attributed to a top-level the
    # diagnostics report as context-capped → the extracted list is a lower bound.
    fake = PvInventory(
        repo_root=str(root),
        displays=(
            DisplayPvInventory(
                display_path="ov.bob",
                operator_facing=True,
                pvs=(
                    ExpandedPv(
                        pv="ca://SYSX:X",
                        raw_pv="$(P):X",
                        resolution="resolved",
                        role="read",
                        protocol="ca",
                        top_level_display="ov.bob",
                        origin_file="frag.bob",
                    ),
                ),
            ),
        ),
        diagnostics=PvDiagnostics(context_capped=("ov.bob",)),
    )
    mock_batch = AsyncMock(
        return_value={"results": [{"pv_name": "SYSX:X", "value": 1}], "errors": []}
    )
    with (
        patch("epics_mcp.tools.validate.analyze_pv_inventory", return_value=fake),
        patch("epics_mcp.tools.validate.pv_get_batch", mock_batch),
    ):
        result = await _validate_pvs(file_path=str(frag), displays_dir=str(root))

    assert result["total"] == 1
    notes = result["notes"]
    assert isinstance(notes, list)
    assert any("lower bound" in str(n) for n in notes)
    mock_batch.assert_awaited_once_with(["SYSX:X"], None)
