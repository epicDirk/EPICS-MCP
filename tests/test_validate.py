"""Tests for epics_mcp.tools.validate."""

import os
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastmcp.exceptions import ToolError

from epics_mcp.display_files import INVENTORY_SUFFIXES
from epics_mcp.errors import EpicsError
from epics_mcp.tools.validate import _display_view_is_capped, _validate_pvs

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

#: A Data Browser trend configuration: the second file kind the engine collects, next to the
#: displays. Since the GB-79 pin move the inventory READS it, so this fixture carries the two
#: coupling guards below AND the behaviour tests that prove the refusal was really opened.
_TREND = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    "<databrowser><title>Trend</title><pvlist><pv>"
    "<name>SIM:PS-01:Cur-RB</name><visible>true</visible><axis>0</axis>"
    "</pv></pvlist></databrowser>"
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
    # The display view has to apply the same protocol filter. Without it the loc:// channel counts
    # as something the display "also resolves", and this file, which hides nothing, gets a note
    # telling the caller to go look at one more channel that is not a channel. Asserted here
    # because the two assertions above stay green either way.
    assert result["shown_by_display"] == 0
    assert "notes" not in result


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


@pytest.mark.parametrize("name", ["notes.txt", "legacy.opi"])
async def test_an_uncollected_suffix_is_refused_without_running_the_inventory(
    tmp_path: Path, name: str
) -> None:
    """QA-33: the walk is the expensive half, and for a file it never opens the outcome is settled.

    Asserted on the CAUSE (the inventory was never run), not on elapsed time: a duration
    assertion would be flaky on a loaded machine and would still pass if the walk merely got
    faster. The spy also proves the refusal sits BEFORE the walk rather than merely somewhere.
    That this patch reaches the code despite ``asyncio.to_thread`` is not assumed either, the
    pre-existing ``test_validate_pvs_file_path_context_capped_note`` patches the same name.

    THE NEGATIVE CONTROL OF GB-79, which is why it is parametrised over two suffixes now. Opening
    the refusal to trend files is one edit away from opening it to everything, and "everything is
    accepted" would satisfy every positive test in this file while destroying the guarantee. The
    ``.opi`` case is the sharper of the two: it is a real CS-Studio display format that this engine
    still does not collect, so accepting it would look plausible rather than obviously wrong.

    The message must name BOTH collected kinds, not just the one the caller missed: a reader who
    passed a ``.csv`` needs to know that a trend would have worked too.
    """
    root, _ = _dataset(tmp_path)
    other = root / name
    other.write_text(_FRAGMENT, encoding="utf-8")  # valid display XML, uncollected suffix

    spy = Mock(side_effect=AssertionError("the inventory walk must not run for this file"))
    with (
        patch("epics_mcp.services.inventory_adapter.analyze_pv_inventory", spy),
        pytest.raises(EpicsError) as exc_info,
    ):
        await _validate_pvs(file_path=str(other), displays_dir=str(root))

    assert exc_info.value.error_code == "INVALID_INPUT"
    for suffix in INVENTORY_SUFFIXES:
        assert suffix in str(exc_info.value), f"the message must name {suffix}, the kind it reads"
    assert "pv_names" in str(exc_info.value), "the message must name the way out"
    spy.assert_not_called()


async def test_uppercase_bob_suffix_is_accepted_because_the_engine_accepts_it(
    tmp_path: Path,
) -> None:
    """The suffix comparison folds case, because the engine's collection does (``suffix.lower()``).

    ⚠️ This line used to credit ``find_bob_files`` for the rule. That was misleading in the one
    direction that matters: the PV surface stopped calling it, and it still returns only ``.bob``,
    so a reader would have taken reassurance about the case fold from a function that has nothing
    to do with this answer. The rule lives in the engine's own collection walk.

    Measured before this was written: a file named ``UPPER.BOB`` IS collected by the engine, so
    rejecting it would refuse a file the inventory happily reads. The naive ``endswith('.bob')``
    is exactly the mutant this pins.

    The file declares a CONCRETE channel, and the assertion is that channel. An earlier version
    used the macro fragment and asserted ``total == 0``, which was worthless in the direction that
    matters: 0 is what you get whether the file was READ or IGNORED, so it stayed green under a
    mutant that made the engine case-sensitive. A non-empty expected value can only be produced by
    actually reading the file.
    """
    root = tmp_path / "ds"
    root.mkdir()
    shouty = root / "UPPER.BOB"
    shouty.write_text(
        '<display version="2.0.0"><name>U</name><widget type="textupdate">'
        "<name>s</name><pv_name>SIM:PROBE-01:Val</pv_name></widget></display>",
        encoding="utf-8",
    )

    mock = AsyncMock(
        return_value={"results": [{"pv_name": "SIM:PROBE-01:Val", "value": 1}], "errors": []}
    )
    with patch("epics_mcp.tools.validate.pv_get_batch", mock):
        result = await _validate_pvs(file_path=str(shouty), displays_dir=str(root))

    assert result["total"] == 1, "the .BOB was not read at all, so the case fold did not happen"
    mock.assert_awaited_once_with(["SIM:PROBE-01:Val"], None)


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
        patch("epics_mcp.services.inventory_adapter.analyze_pv_inventory", spy),
        pytest.raises(EpicsError) as exc_info,
    ):
        await _validate_pvs(file_path=str(outside), displays_dir=str(root))

    assert exc_info.value.error_code == "INVALID_INPUT"
    assert "not under displays_dir" in str(exc_info.value), (
        "any early INVALID_INPUT satisfies the two assertions above (a missing fixture file "
        "raises one from resolve_user_path without ever reaching the code under test), so the "
        "reason has to be asserted too"
    )
    spy.assert_not_called()


async def test_a_bad_displays_dir_is_still_reported_as_such_for_a_non_bob_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The suffix refusal must not MASK a path-boundary failure of the other argument.

    Placed between the two ``resolve_user_path`` calls, the new check answered a caller whose
    ``displays_dir`` was also bad with the suffix instead of the boundary error the previous
    release gave: measured, ``PATH_OUTSIDE_WORKSPACE`` silently became ``INVALID_INPUT``, and it
    named the wrong argument. Validating every user path first keeps both diagnoses intact.
    """
    import epics_mcp.config as config_module

    root = tmp_path / "ds"
    root.mkdir()
    (root / "notes.txt").write_text("not a display", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setenv("EPICS_MCP_ALLOWED_ROOTS", str(root))
    config_module._config = None
    try:
        with pytest.raises(EpicsError) as exc_info:
            await _validate_pvs(file_path=str(root / "notes.txt"), displays_dir=str(elsewhere))
        assert exc_info.value.error_code == "PATH_OUTSIDE_WORKSPACE", (
            "the suffix refusal is masking the displays_dir boundary error"
        )
        assert "displays_dir" in str(exc_info.value)
    finally:
        config_module._config = None


def test_our_suffixes_are_exactly_the_engines_collecting_suffixes() -> None:
    """The COUPLING, as a set equality in BOTH directions.

    ⚠️ **Two earlier shapes of this guard were wrong, in opposite ways, and the second one is
    the interesting failure.** The first compared ``DISPLAY_SUFFIX == _BOB_SUFFIX`` and claimed
    to be "the only assertion that goes red for a widening": both constants stay ``".bob"``
    through a widening that puts a second suffix NEXT TO the first, so it survived exactly the
    change it advertised. The second asked whether ours was the engine's ONLY collecting suffix.
    That one DID go red for the widening, which is how GB-79 was signalled at all, but it was
    unrepairable by construction: once we legitimately collect two, "is there only one" has no
    correct answer, and keeping it would have meant deleting a guard rather than fixing it.

    What is asked now is neither, and it survives the next widening as well: our set IS the
    engine's set. A suffix the ENGINE gains reddens it, because we would then refuse files the
    inventory reads, which is the GB-79 defect itself. A suffix WE accept and the engine does not
    reddens it too, because we would run a full walk for an answer that was already settled. Only
    an equality states both halves, and only the second half is new.

    Measuring the module's attributes rather than importing named constants is deliberate: a
    further suffix arrives under a name this test cannot know in advance, and a named import
    would have to be edited before it could notice anything.

    Red-proven in both directions before this was committed, not merely reasoned about: removing
    ``TREND_SUFFIX`` from ``INVENTORY_SUFFIXES`` fails naming ``.plt`` as the engine's surplus,
    and adding a suffix the engine does not collect fails naming it as ours.
    """
    import opi_navigation.discovery as discovery

    engine_suffixes = {
        name: getattr(discovery, name)
        for name in dir(discovery)
        if name.endswith("_SUFFIX") and isinstance(getattr(discovery, name), str)
    }
    assert set(engine_suffixes.values()) == set(INVENTORY_SUFFIXES), (
        "the display-PV engine and this server disagree about which files the inventory reads. "
        f"engine: {sorted(engine_suffixes.items())}, ours: {sorted(INVENTORY_SUFFIXES)}. "
        "A suffix only the ENGINE has means the refusal in _run_validate rejects files the "
        "inventory would read; a suffix only WE have means it accepts files that can only ever "
        "come back empty after a full walk."
    )


def test_the_inventory_reads_exactly_the_suffixes_we_accept(tmp_path: Path) -> None:
    """The same coupling from the behaviour side: what the INVENTORY actually reads.

    ⚠️ **This guard used to measure the wrong function, and it was blind three times over.**
    It called ``find_bob_files``, which (a) the PV surface no longer calls at all, (b) still
    returns only ``.bob`` even in the widened engine, so it stayed green through the widening,
    and (c) was checked against a name list that contained no trend file, so even feeding it the
    widened function would have changed nothing.

    ``analyze_pv_inventory`` is the function whose behaviour the refusal in ``_run_validate``
    actually cites, so it is the one asked here. Its sibling above compares CONSTANTS, which a
    reader could satisfy by editing two files in step while the engine does something else
    entirely; this one compares what the walk really returns.

    THE EXPECTATION IS DERIVED FROM ``INVENTORY_SUFFIXES``, not typed out, and that is what makes
    the pair red-provable from one edit: shrink our set and the derived expectation stops naming
    the trend while the engine keeps collecting it. A typed ``{"kept.bob", "KEPT2.BOB",
    "trend.plt"}`` would go green again the moment someone "fixed" it to match the shrunken set.

    Every fixture file declares a PV on purpose. The inventory only holds files that yielded at
    least one PV occurrence, so a silent, PV-less file would drop out of ``collected`` for a
    reason that has nothing to do with its suffix and would read as a suffix verdict.

    The case folding stays measured rather than restated (``UPPER.BOB`` is a display), because
    a test that re-implements the rule it checks proves only that the author is consistent.
    """
    from opi_navigation.pv_analysis import analyze_pv_inventory

    written = {
        "kept.bob": _FRAGMENT,
        "KEPT2.BOB": _FRAGMENT,
        "trend.plt": _TREND,
        "skipped.txt": _FRAGMENT,
        "skipped.opi": _FRAGMENT,
    }
    for name, body in written.items():
        (tmp_path / name).write_text(body, encoding="utf-8")
    expected = {name for name in written if Path(name).suffix.lower() in INVENTORY_SUFFIXES}

    inventory = analyze_pv_inventory(tmp_path)
    collected = {entry.display_path for entry in inventory.displays}
    assert collected == expected, (
        "the display-PV inventory does not read the set of suffixes this server accepts "
        f"(collected: {sorted(collected)}, accepted: {sorted(expected)}). A surplus on the "
        "inventory side means _run_validate refuses files it would read."
    )

    # A trend is COLLECTED and is still not a display, and the engine says which is which itself.
    # Asserting that here keeps the widening from being read as "a .plt is a screen now": the
    # kind comes back as a field, never from the suffix, because a .plt is not even a reliable
    # candidate (measured upstream: the .plt files under an epics-base checkout are Perl scripts).
    kinds = {entry.display_path: entry.node_kind for entry in inventory.displays}
    assert kinds["trend.plt"] == "trend", (
        f"the trend file came back as {kinds['trend.plt']!r}; the server's prose distinguishes a "
        "display from a trend on this field, so it must not silently become 'display'"
    )
    assert kinds["kept.bob"] == "display"


async def test_a_standalone_trend_is_answered_rather_than_refused(tmp_path: Path) -> None:
    """GB-79, the whole point: a trend file comes back with its traces instead of INVALID_INPUT.

    The two guards above compare a constant and a walk. Neither of them calls the tool, so both
    would stay green if the refusal itself were left untouched, and that refusal is the thing the
    widening breaks. This is the end-to-end half.

    A CONCRETE channel is asserted, never ``total == 0``: zero is what you get whether the file
    was READ or IGNORED, which is the trap an earlier sibling in this file fell into. Only a
    non-empty expected value can be produced by actually parsing the trend.

    Red-proven on the pre-fix code: it raised ``EpicsError(INVALID_INPUT)`` naming ``.bob``.
    """
    root = tmp_path / "ds"
    root.mkdir()
    trend = root / "trend.plt"
    trend.write_text(_TREND, encoding="utf-8")

    mock = AsyncMock(
        return_value={"results": [{"pv_name": "SIM:PS-01:Cur-RB", "value": 1}], "errors": []}
    )
    with patch("epics_mcp.tools.validate.pv_get_batch", mock):
        result = await _validate_pvs(file_path=str(trend), displays_dir=str(root))

    assert result["total"] == 1, "the trend was not parsed at all"
    mock.assert_awaited_once_with(["SIM:PS-01:Cur-RB"], None)


async def test_an_embedded_trend_is_found_through_the_file_view(tmp_path: Path) -> None:
    """The OTHER route into a trend, and the one that could have needed a code change.

    A trend reaches the inventory two ways, and they are not the same event. Opened by an
    ``open_file`` button it becomes a top level of its own, which is the sibling above. Embedded
    in a screen through a ``databrowser`` widget it never becomes one: its traces are attributed
    to the EMBEDDING screen and keep the trend only as their ``origin_file``. The file view is
    built on exactly that field, so this is the case that decides whether ``_sweep_display_file``
    needed widening at all.

    Measured before the refusal was opened: it did not. The sweep matches on ``origin_file``
    without ever looking at a suffix, so the roll-up carried the trace through untouched. This
    test is what keeps that true, because a future sweep that starts filtering by kind would
    break this route while leaving the standalone one green.
    """
    root = tmp_path / "ds"
    root.mkdir()
    (root / "parent.bob").write_text(
        '<display version="2.0.0"><name>Parent</name>'
        '<widget type="databrowser" version="2.0.0"><name>db</name>'
        "<file>trend.plt</file></widget></display>",
        encoding="utf-8",
    )
    trend = root / "trend.plt"
    trend.write_text(_TREND, encoding="utf-8")

    mock = AsyncMock(
        return_value={"results": [{"pv_name": "SIM:PS-01:Cur-RB", "value": 1}], "errors": []}
    )
    with patch("epics_mcp.tools.validate.pv_get_batch", mock):
        result = await _validate_pvs(file_path=str(trend), displays_dir=str(root), view="file")

    assert result["total"] == 1, (
        "the file view lost the trace of an EMBEDDED trend; its PVs are attributed to the parent "
        "screen and only origin_file points back here"
    )
    mock.assert_awaited_once_with(["SIM:PS-01:Cur-RB"], None)


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


# --- The two views (GB-4) ---------------------------------------------------------------------
# A dataset that separates the cases the two views disagree on. Deliberately richer than
# ``_dataset``: that one's parent declares no PV of its own, so it can only ever exercise the
# empty-result path, and a mutation that only fires on the normal path would survive it.
_OWNING_PARENT = (  # declares one channel itself AND embeds a fragment whose macro it binds
    '<display version="2.0.0"><name>Owner</name>'
    '<widget type="textupdate"><name>own</name><pv_name>SIM:OWNER:Val</pv_name></widget>'
    '<widget type="embedded"><name>e</name><file>bound.bob</file>'
    "<macros><PRP>DEV-TEST02:Spu01</PRP></macros>"
    "</widget></display>"
)
_BOUND_FRAGMENT = (
    '<display version="2.0.0"><name>Bound</name>'
    '<widget type="textupdate"><name>s</name><pv_name>$(PRP):Val</pv_name></widget></display>'
)
_DISTRACTOR = (  # resolved channels, but embedded by nobody and embedding nothing
    '<display version="2.0.0"><name>Distractor</name>'
    '<widget type="textupdate"><name>d1</name><pv_name>SIM:DISTRACT:One</pv_name></widget>'
    '<widget type="textupdate"><name>d2</name><pv_name>SIM:DISTRACT:Two</pv_name></widget>'
    "</display>"
)
_LEAF = (  # own channels, no fragments: both views agree, so no note may fire
    '<display version="2.0.0"><name>Leaf</name>'
    '<widget type="textupdate"><name>l</name><pv_name>SIM:LEAF:Val</pv_name></widget></display>'
)
_TWO_FORMS = (  # ONE channel written two ways; the engine yields two events for it
    '<display version="2.0.0"><name>TwoForms</name>'
    '<widget type="textupdate"><name>a</name><pv_name>SIM:FORMS:Val</pv_name></widget>'
    '<widget type="textupdate"><name>b</name><pv_name>ca://SIM:FORMS:Val</pv_name></widget>'
    "</display>"
)


def _views_dataset(tmp_path: Path) -> Path:
    """Write the view dataset and return its root."""
    root = tmp_path / "views"
    root.mkdir()
    for name, body in (
        ("owner.bob", _OWNING_PARENT),
        ("bound.bob", _BOUND_FRAGMENT),
        ("container.bob", _PARENT),  # composes only, declares nothing
        ("frag.bob", _FRAGMENT),
        ("distractor.bob", _DISTRACTOR),
        ("leaf.bob", _LEAF),
        ("two_forms.bob", _TWO_FORMS),
    ):
        (root / name).write_text(body, encoding="utf-8")
    return root


async def _connect_all(names: list[str], timeout: float | None = None) -> dict[str, object]:
    """A batch read where every channel answers, so tests can assert on selection, not on IO."""
    return {"results": [{"pv_name": n, "value": 1} for n in names], "errors": []}


async def test_owning_parent_file_view_notes_what_the_display_view_adds(tmp_path: Path) -> None:
    """The normal path: the file view finds the parent's OWN channel and says one more exists.

    ``_dataset``'s parent cannot exercise this: with no channel of its own it always takes the
    empty-result return, so a note wired only into the normal path would still look correct.
    """
    root = _views_dataset(tmp_path)
    with patch("epics_mcp.tools.validate.pv_get_batch", side_effect=_connect_all):
        result = await _validate_pvs(file_path=str(root / "owner.bob"), displays_dir=str(root))

    assert result["total"] == 1, "the file view holds the parent's own channel only"
    assert result["shown_by_display"] == 2
    assert result["shown_by_display_capped"] is False
    notes = result["notes"]
    assert isinstance(notes, list)
    assert any("1 further channel(s)" in str(n) for n in notes), notes
    assert any('view="display"' in str(n) for n in notes), notes


async def test_owning_parent_display_view_returns_the_larger_set(tmp_path: Path) -> None:
    """The point of the parameter: the other view is reachable in the same call, no second tool."""
    root = _views_dataset(tmp_path)
    batch = AsyncMock(side_effect=_connect_all)
    with patch("epics_mcp.tools.validate.pv_get_batch", batch):
        result = await _validate_pvs(
            file_path=str(root / "owner.bob"), displays_dir=str(root), view="display"
        )

    assert result["total"] == 2
    checked = result["pvs"]
    assert isinstance(checked, list)
    assert sorted(p["pv_name"] for p in checked) == ["DEV-TEST02:Spu01:Val", "SIM:OWNER:Val"]
    # The display view is what was asked for, so nothing is being withheld and no note fires.
    assert "notes" not in result


async def test_pure_container_reports_zero_and_still_says_what_it_hides(tmp_path: Path) -> None:
    """The dominant case (measured: 42 of 54 affected files in one dataset).

    A display that only composes fragments declares nothing itself, so the file view answers
    ``total: 0`` and returns BEFORE the connectivity read. Without the note in that branch the
    single most misleading answer the tool gives would stay silent.
    """
    root = _views_dataset(tmp_path)
    spy = AsyncMock(side_effect=_connect_all)
    with patch("epics_mcp.tools.validate.pv_get_batch", spy):
        result = await _validate_pvs(file_path=str(root / "container.bob"), displays_dir=str(root))

    assert result["total"] == 0
    assert result["shown_by_display"] == 1
    notes = result["notes"]
    assert isinstance(notes, list)
    assert any("1 further channel(s)" in str(n) for n in notes), notes
    spy.assert_not_awaited()  # total 0 means no PV is read at all


async def test_fragment_keeps_the_file_view_and_reports_an_honest_zero(tmp_path: Path) -> None:
    """The counter-direction, and the reason the ``origin_file`` filter must stay.

    A fragment's macros are unbound when it is seeded standalone, so its DISPLAY view really is
    empty while its file view is not. ``shown_by_display: 0`` next to ``total: 1`` is therefore a
    fact about the file, not a defect, and it is asserted here so it cannot be "fixed" away.
    """
    root = _views_dataset(tmp_path)
    with patch("epics_mcp.tools.validate.pv_get_batch", side_effect=_connect_all):
        result = await _validate_pvs(file_path=str(root / "bound.bob"), displays_dir=str(root))

    assert result["total"] == 1
    assert result["shown_by_display"] == 0
    assert "notes" not in result, "the display view is smaller here, there is nothing to add"


async def test_fragment_display_view_is_empty_not_the_lifted_set(tmp_path: Path) -> None:
    """``view="display"`` on a fragment answers about the fragment, not about its parent."""
    root = _views_dataset(tmp_path)
    with patch("epics_mcp.tools.validate.pv_get_batch", side_effect=_connect_all):
        result = await _validate_pvs(
            file_path=str(root / "bound.bob"), displays_dir=str(root), view="display"
        )

    assert result["total"] == 0
    assert result["pvs"] == []


async def test_a_leaf_display_gets_no_note_and_still_reports_the_field(tmp_path: Path) -> None:
    """Both views agree: no note. But the field is still there.

    The second assertion is the one that matters: an implementation that only sets
    ``shown_by_display`` inside the note branch passes every other test here and makes the field
    unusable for exactly the comparison it exists for.
    """
    root = _views_dataset(tmp_path)
    with patch("epics_mcp.tools.validate.pv_get_batch", side_effect=_connect_all):
        result = await _validate_pvs(file_path=str(root / "leaf.bob"), displays_dir=str(root))

    assert result["total"] == 1
    assert "notes" not in result
    assert result["shown_by_display"] == result["total"]


async def test_the_distractor_display_does_not_leak_into_the_display_view(tmp_path: Path) -> None:
    """The display view keys on THIS display, not on the whole inventory.

    ``distractor.bob`` carries two resolved channels and is unrelated to ``leaf.bob``. Counting the
    display view over all displays instead of the matching one would show them here.
    """
    root = _views_dataset(tmp_path)
    with patch("epics_mcp.tools.validate.pv_get_batch", side_effect=_connect_all):
        result = await _validate_pvs(
            file_path=str(root / "leaf.bob"), displays_dir=str(root), view="display"
        )

    assert result["total"] == 1
    checked = result["pvs"]
    assert isinstance(checked, list)
    assert [p["pv_name"] for p in checked] == ["SIM:LEAF:Val"]


async def test_one_channel_written_two_ways_counts_once(tmp_path: Path) -> None:
    """The display view normalises the protocol prefix, as the file view has always done.

    Measured on the engine: ``SIM:X`` and ``ca://SIM:X`` in one display produce TWO events whose
    ``pv`` differs and whose ``channel_name`` is the same. Deduplicating on the raw ``pv`` would
    make the display view look bigger than the file view on a display with no fragments at all,
    firing the note on a file that hides nothing.
    """
    root = _views_dataset(tmp_path)
    with patch("epics_mcp.tools.validate.pv_get_batch", side_effect=_connect_all):
        result = await _validate_pvs(file_path=str(root / "two_forms.bob"), displays_dir=str(root))

    assert result["total"] == 1
    assert result["shown_by_display"] == 1
    assert "notes" not in result, "the two spellings are one channel, so nothing is hidden"


async def test_an_explicit_list_wins_over_file_path_and_view(tmp_path: Path) -> None:
    """``pv_names`` short-circuits the file, so the file-mode fields have nothing to describe."""
    root = _views_dataset(tmp_path)
    spy = Mock(side_effect=AssertionError("the inventory must not run when a list is given"))
    with (
        patch("epics_mcp.services.inventory_adapter.analyze_pv_inventory", spy),
        patch("epics_mcp.tools.validate.pv_get_batch", side_effect=_connect_all),
    ):
        result = await _validate_pvs(
            pvs=["SIM:EXPLICIT:Val"],
            file_path=str(root / "owner.bob"),
            view="display",
        )

    assert result["total"] == 1
    assert "shown_by_display" not in result, "no display was consulted, so nothing may be claimed"
    # Same rule for the path echo, and this is the half that is easy to get wrong: file_path WAS
    # supplied here, it just lost to the list. Echoing it would say the answer came from that file
    # when the spy above proves the file was never opened.
    assert "file_path" not in result, "the file was not read, so the answer is not about it"
    spy.assert_not_called()


async def test_the_wire_default_is_the_file_view(tmp_path: Path) -> None:
    """Over the REGISTERED tool, not the inner function: omitting ``view`` must not change anything.

    Every other test here calls ``_validate_pvs`` directly and therefore cannot see the default
    declared at the tool boundary. Measured: flipping that default to "display" left all of them
    green, which would have shipped a silent breaking change on the wire.

    Asserted on the parent that owns one channel and embeds one more, because that is the display
    where the two views differ by exactly one and the numbers cannot be confused.
    """
    from fastmcp import Client

    from epics_mcp.server import _DISPLAY_TOOLS_AVAILABLE, mcp

    if not _DISPLAY_TOOLS_AVAILABLE:  # pragma: no cover - core-only install, tool not registered
        pytest.skip("validate_pvs is display-gated and this install has no displays group")

    root = _views_dataset(tmp_path)
    with patch("epics_mcp.tools.validate.pv_get_batch", side_effect=_connect_all):
        async with Client(mcp) as client:
            call = await client.call_tool(
                "validate_pvs",
                {"file_path": str(root / "owner.bob"), "displays_dir": str(root)},
            )

    payload = call.data
    assert payload["total"] == 1, "the wire default must still be the file view"
    assert payload["shown_by_display"] == 2
    # The only place any test looks at ``notes`` ACROSS the tool boundary. The honesty notes are a
    # user-visible part of the answer, and the registered wrapper is a layer the inner tests never
    # execute; without this line a wrapper that dropped them would pass the whole suite.
    assert "notes" in payload, "the honesty notes must survive the registered tool wrapper"
    # Same argument for the path echo: the two tests below assert it on the inner function, which
    # cannot see a wrapper that drops or rewrites a key on its way out.
    assert payload["file_path"] == str(root / "owner.bob")


async def test_file_path_is_echoed_on_the_normal_path(tmp_path: Path) -> None:
    """GB-28: the answer names the file it is about, and it does so on BOTH file-mode returns.

    This one is the repair. The echo used to sit on the empty-result return only, so one mode
    answered with two different key sets and a caller reading them had no stable one to key on.
    Nothing pinned that: there is no output schema for this tool (registered with
    ``output_schema=None``) and no test looked at the key, so the split could not go red.

    Asserted on the parent that owns a channel, because that is the display which takes the normal
    return; the sibling test below takes the other one. Provably red on the pre-fix code, where
    the normal return carried no ``file_path`` at all.
    """
    root = _views_dataset(tmp_path)
    owner = root / "owner.bob"
    with patch("epics_mcp.tools.validate.pv_get_batch", side_effect=_connect_all):
        result = await _validate_pvs(file_path=str(owner), displays_dir=str(root))

    assert result["total"] == 1, "the fixture must take the NORMAL return, not the empty one"
    assert result["file_path"] == str(owner)


async def test_file_path_is_echoed_on_the_empty_path_too(tmp_path: Path) -> None:
    """The other file-mode return keeps the echo, which is the half that was already right.

    A pure container declares nothing itself, so it answers ``total: 0`` and lands on the
    empty-result return. Since GB-4 that is the PRIMARY answer for such a screen and it carries the
    note that matters most, which is why the two returns disagreeing was worth repairing rather
    than leaving as cosmetics. Kept as its own test rather than folded into the one above: the
    fields now come from one shared dict, and a single test would not say which return it walked.
    """
    root = _views_dataset(tmp_path)
    container = root / "container.bob"
    result = await _validate_pvs(file_path=str(container), displays_dir=str(root))

    assert result["total"] == 0, "the fixture must take the EMPTY return, not the normal one"
    assert result["pvs"] == []
    assert result["file_path"] == str(container)


def _capped_inventory(
    rel: str,
    *,
    declared: bool,
    capped: tuple[str, ...],
    own_unresolved: bool = False,
    foreign_top: str | None = None,
    foreign_first: bool = False,
    glob_capped: tuple[tuple[str, str], ...] = (),
) -> object:
    """An inventory whose display *rel* embeds a fragment, with *capped* naming what was capped.

    *declared*: whether *rel* also owns a RESOLVED channel, i.e. whether the call takes the normal
    path (True) or the empty-result path (False).

    *own_unresolved*: give *rel* an occurrence of its own that does NOT pass the resolution filter.
    That is the shape of the two real files this defect was measured on: they declare PVs, none of
    which resolve at the default cap. It is what separates "declares nothing" from "declares
    something that has not resolved yet", and the cap verdict must tell those apart.

    *foreign_top*: append (or, with *foreign_first*, prepend) a SECOND display of that name, whose
    events belong to itself. It serves two purposes: a top in ``capped`` that must NOT count for
    *rel*, and a position control, because a verdict collected per display instead of across the
    whole inventory is invisible in a one-display fixture.

    *capped* goes into ``diagnostics.context_capped`` verbatim, because WHICH path is in there
    decides which test can see it: the top term needs a path that is a ``top_level_display`` of
    *rel*'s own events, the ``rel`` term needs *rel* itself and fires with no event at all, and the
    display view's own term also matches the origins of the display's events.

    *glob_capped* goes into the field of the same name, engine-shaped as ``(source, raw_target)``
    pairs. It is a THIRD axis and no predicate reads it, so unlike *capped* its contents do not
    have to line up with *rel*; only the count reaches the answer.
    """
    from opi_navigation.pv_analysis import (
        DisplayPvInventory,
        ExpandedPv,
        PvDiagnostics,
        PvInventory,
    )

    def _pv(name: str, origin: str, top: str | None = None) -> object:
        return ExpandedPv(
            pv=f"ca://{name}",
            raw_pv="$(P):X",
            resolution="resolved",
            role="read",
            protocol="ca",
            top_level_display=top or rel,
            origin_file=origin,
        )

    pvs = [_pv("SYSX:FROM_FRAGMENT", "frag.bob")]
    if declared:
        pvs.insert(0, _pv("SYSX:OWN", rel))
    if own_unresolved:
        # Engine-shaped: an unbound macro leaves the string as written, so ``pv`` still carries the
        # macro and the protocol falls back to the raw one (expansion.py). Building it as a
        # concrete ``ca://`` name with resolution="dynamic" would be a state the engine cannot
        # produce.
        pvs.insert(
            0,
            ExpandedPv(
                pv="$(P):X",
                raw_pv="$(P):X",
                resolution="dynamic",
                role="read",
                protocol="ca",
                top_level_display=rel,
                origin_file=rel,
            ),
        )
    displays = [
        DisplayPvInventory(display_path=rel, operator_facing=True, pvs=tuple(pvs)),  # type: ignore[arg-type]
    ]
    if foreign_top is not None:
        foreign = DisplayPvInventory(
            display_path=foreign_top,
            operator_facing=True,
            pvs=(_pv("SYSX:FOREIGN", foreign_top, top=foreign_top),),  # type: ignore[arg-type]
        )
        displays.insert(0 if foreign_first else len(displays), foreign)
    return PvInventory(
        repo_root="/nowhere",
        displays=tuple(displays),
        # The engine records the capped TARGET, not the top it was capped under.
        diagnostics=PvDiagnostics(context_capped=capped, glob_capped=glob_capped),
    )


async def test_glob_cap_is_reported_although_no_context_cap_fired(tmp_path: Path) -> None:
    """GB-29: the second incompleteness source reaches the caller, and it does so on its own.

    ``context_capped`` is deliberately EMPTY here. That is the whole point: before this, a caller
    seeing no lower-bound note read the list as complete, and a capped glob makes that false while
    leaving both context-cap verdicts at False. So the silence was the defect, not a missing detail
    in an existing sentence.

    Measured, and this is why it is not a hypothetical: on a 2878-display dataset the default glob
    cap fires 16 times across 4 source displays, one of them a synoptic overview that embeds its
    sections through a glob. On four smaller datasets (13 to 485 displays) it never fires, which is
    what the earlier "damage is zero" reading was based on.

    The two flags must stay untouched: the glob count carries no per-file verdict, so letting it
    move ``shown_by_display_capped`` would claim something the engine did not say. Provably red:
    drop the ``glob_capped_count`` block from ``_validate_pvs``.
    """
    root = tmp_path / "ds"
    root.mkdir()
    (root / "d.bob").write_text('<display version="2.0.0"><name>D</name></display>', "utf-8")
    with (
        patch(
            "epics_mcp.services.inventory_adapter.analyze_pv_inventory",
            return_value=_capped_inventory(
                "d.bob",
                declared=True,
                capped=(),
                glob_capped=(("ov.bob", "sections/*.bob"), ("ov.bob", "elements/*.bob")),
            ),
        ),
        patch("epics_mcp.tools.validate.pv_get_batch", side_effect=_connect_all),
    ):
        result = await _validate_pvs(file_path=str(root / "d.bob"), displays_dir=str(root))

    assert result["shown_by_display_capped"] is False, "the glob count is not a display verdict"
    notes = result["notes"]
    assert isinstance(notes, list)
    glob_notes = [str(n) for n in notes if "glob cap" in str(n)]
    assert len(glob_notes) == 1, notes
    assert "2 globbed <file> reference(s)" in glob_notes[0], glob_notes
    # Said as a property of the walk, not of this file. The count is a total over the dataset, so a
    # sentence blaming the queried file would be a claim the engine never made.
    assert "about the walk, not about this file" in glob_notes[0], glob_notes
    assert not any("context cap" in str(n) for n in notes), (
        "no context cap fired here, so no note may say one did"
    )


async def test_both_caps_at_once_keep_their_order_and_the_glob_note_reaches_the_empty_path(
    tmp_path: Path,
) -> None:
    """The one case where the new note can displace an existing one, on the return it is scarcest.

    Two guards in one fixture, because they need the same rare setup. FIRST, ordering: the sibling
    test below pins ``notes[0]``/``notes[1]`` with the reasoning that order is what a model reads
    first, and the glob note inserts itself between the context-cap note and the view note. Nothing
    saw that, because every ordering test defaults ``glob_capped`` to empty and this test is the
    only one that sets both. SECOND, the empty-result return: ``declared=False`` takes it, and the
    glob note has to survive there too, which is exactly the asymmetry GB-28 removed for
    ``file_path`` one function up.

    ``own_unresolved=True`` is load-bearing rather than decoration, and getting it wrong is how
    this test first failed: on the empty path with no occurrence of its own,
    :func:`_file_view_is_capped` answers False by design (GB-26), so the context-cap note never
    fires and there is no ordering to check. The file has to declare something that did not
    resolve.

    Provably red both ways: swap the two ``notes.append`` blocks in ``_validate_pvs`` (order), or
    move the glob block behind the ``if not extracted:`` return (empty path).
    """
    root = tmp_path / "ds"
    root.mkdir()
    (root / "d.bob").write_text('<display version="2.0.0"><name>D</name></display>', "utf-8")
    with patch(
        "epics_mcp.services.inventory_adapter.analyze_pv_inventory",
        return_value=_capped_inventory(
            "d.bob",
            declared=False,
            own_unresolved=True,
            capped=("d.bob", "frag.bob"),
            glob_capped=(("ov.bob", "sections/*.bob"),),
        ),
    ):
        result = await _validate_pvs(file_path=str(root / "d.bob"), displays_dir=str(root))

    assert result["total"] == 0, "the fixture must take the empty-result return"
    assert result["file_path"] == str(root / "d.bob")
    notes = result["notes"]
    assert isinstance(notes, list) and len(notes) == 3, notes
    assert "context cap" in str(notes[0]), "the pre-existing cap note must keep coming first"
    assert "glob cap" in str(notes[1]), "the glob note sits between the cap note and the view note"
    assert "further channel(s)" in str(notes[2]), notes


async def test_file_path_is_echoed_exactly_as_passed_not_resolved(tmp_path: Path) -> None:
    """The echo is a correlation key, so it must come back byte-identical, not canonicalised.

    Every other echo test hands in an already absolute, already resolved ``tmp_path`` string, where
    the raw and the resolved form are equal and a mutant echoing ``str(f)`` stays green. A RELATIVE
    path separates them, and it is not a contrived one: ``examples/README.md`` teaches exactly that
    call. The refusal in ``_run_validate`` deliberately reports the RESOLVED path instead, so the
    two readings live side by side in one function and this is the guard that keeps them apart.

    Provably red: echo ``str(f)`` (or ``str(resolve_user_path(file_path, ...))``) instead of the
    argument.
    """
    root = tmp_path / "ds"
    root.mkdir()
    (root / "d.bob").write_text('<display version="2.0.0"><name>D</name></display>', "utf-8")
    relative = os.path.relpath(root / "d.bob", Path.cwd())
    assert not Path(relative).is_absolute(), "the fixture only proves anything on a relative path"

    with patch(
        "epics_mcp.services.inventory_adapter.analyze_pv_inventory",
        return_value=_capped_inventory("d.bob", declared=False, capped=()),
    ):
        result = await _validate_pvs(file_path=relative, displays_dir=str(root))

    assert result["file_path"] == relative, "the argument, not the canonicalised path"
    assert result["file_path"] != str(root / "d.bob"), "the two forms must actually differ here"


async def test_capped_fragment_makes_the_display_figure_a_lower_bound(tmp_path: Path) -> None:
    """Normal path with BOTH paths capped: the display note says "at least", the file note survives.

    Both are listed as capped on purpose. The file note needs ``d.bob``; the display one would fire
    on ``frag.bob`` alone. Listing only the fragment is a different case, and it is the next test.

    ⚠ Since GB-26 the file note here is OVER-determined: ``d.bob`` satisfies both of
    ``_file_view_is_capped``'s terms at once, so deleting either one leaves this test green. The
    term isolation lives in ``test_validate_pvs_file_path_context_capped_note`` (top term only) and
    in ``test_capped_file_alone_is_a_lower_bound_on_the_normal_path`` (rel term only).

    ``notes[0]`` is pinned because order is what a model reads first, and nothing else would
    notice the two swapping.
    """
    root = tmp_path / "ds"
    root.mkdir()
    (root / "d.bob").write_text('<display version="2.0.0"><name>D</name></display>', "utf-8")
    with (
        patch(
            "epics_mcp.services.inventory_adapter.analyze_pv_inventory",
            return_value=_capped_inventory("d.bob", declared=True, capped=("d.bob", "frag.bob")),
        ),
        patch("epics_mcp.tools.validate.pv_get_batch", side_effect=_connect_all),
    ):
        result = await _validate_pvs(file_path=str(root / "d.bob"), displays_dir=str(root))

    assert result["total"] == 1
    assert result["shown_by_display_capped"] is True
    notes = result["notes"]
    assert isinstance(notes, list) and len(notes) == 2
    assert "lower bound" in str(notes[0]), "the pre-existing cap note must keep coming first"
    assert "at least 1 further channel(s)" in str(notes[1]), notes


async def test_capped_is_seen_on_the_empty_path_where_the_old_flag_is_blind(
    tmp_path: Path,
) -> None:
    """The one place the display cap test and the file one disagree, and why they must.

    ``frag.bob`` is capped, ``d.bob`` is not, and no occurrence of ``d.bob``'s own exists here. The
    display view is fed BY the fragment, so it is a lower bound; the file view of ``d.bob`` is not,
    because ``d.bob`` declares nothing whose enumeration a larger budget could extend.

    Since GB-26 this is the guard against handing the file view the display view's predicate:
    ``_display_view_is_capped`` matches the ORIGINS feeding the display (``frag.bob``, a hit),
    ``_file_view_is_capped`` matches ``rel`` plus the tops of ``rel``'s OWN occurrences (neither,
    no hit, and the occurrence guard settles it first). Swapping one for the other turns the single
    note below into two.
    """
    root = tmp_path / "ds"
    root.mkdir()
    (root / "d.bob").write_text('<display version="2.0.0"><name>D</name></display>', "utf-8")
    with patch(
        "epics_mcp.services.inventory_adapter.analyze_pv_inventory",
        return_value=_capped_inventory("d.bob", declared=False, capped=("frag.bob",)),
    ):
        result = await _validate_pvs(file_path=str(root / "d.bob"), displays_dir=str(root))

    assert result["total"] == 0, "nothing passed the origin_file filter"
    assert result["shown_by_display_capped"] is True
    notes = result["notes"]
    assert isinstance(notes, list) and len(notes) == 1, "only the view note, the file one must not"
    assert "at least 1 further channel(s)" in str(notes[0]), notes
    assert not any("per-display context cap" in str(n) for n in notes), (
        "the file view declares nothing here, so calling it a lower bound would be a false "
        "statement; 'lower bound' alone cannot be asserted on, BOTH notes carry that phrase"
    )


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
        patch("epics_mcp.services.inventory_adapter.analyze_pv_inventory", return_value=fake),
        patch("epics_mcp.tools.validate.pv_get_batch", mock_batch),
    ):
        result = await _validate_pvs(file_path=str(frag), displays_dir=str(root))

    assert result["total"] == 1
    notes = result["notes"]
    assert isinstance(notes, list)
    assert any("lower bound" in str(n) for n in notes)
    mock_batch.assert_awaited_once_with(["SYSX:X"], None)


# --- GB-26: the file-view cap verdict, its guard, and its two negative controls ------------------
#
# ⚠ ``"lower bound"`` is a FORBIDDEN assertion fragment in this block. Both notes carry it (the cap
# note says "PV list is a lower bound", the view note ends with "That figure is a lower bound"), so
# a test asserting on it would stay green with the cap note gone. The cap note is identified by
# ``"per-display context cap"``, which the view note does not contain. For the same reason
# ``"notes" not in result`` is never the negative assertion here: the view note fires in all four
# fixtures below.


def _own_pv_inventory(rel: str, *, capped: tuple[str, ...], foreign_first: bool) -> object:
    """A one-occurrence inventory shaped like ``test_validate_pvs_file_path_context_capped_note``.

    *rel* is expected to sit in a SUBDIRECTORY, and the single event is attributed to a top of a
    different name, so the ``rel`` term of ``_file_view_is_capped`` is the only one that can fire.
    The extra display exists purely as a position control (see the note in the test).

    ⚠ This is a state the ENGINE cannot produce, and knowingly so: it seeds every file that declares
    anything as a top of its own, so a real inventory always holds a display for *rel* as well, and
    the ``rel`` term is then redundant with the top term (see ``_file_view_is_capped``). The fixture
    isolates that term deliberately, the way the pre-existing
    ``test_validate_pvs_file_path_context_capped_note`` isolates the other one. A test built on it
    pins the CONTRACT of the function, not a reachable inventory.
    """
    from opi_navigation.pv_analysis import (
        DisplayPvInventory,
        ExpandedPv,
        PvDiagnostics,
        PvInventory,
    )

    owner = DisplayPvInventory(
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
                origin_file=rel,
            ),
        ),
    )
    other = DisplayPvInventory(
        display_path="zz_other.bob",
        operator_facing=True,
        pvs=(
            ExpandedPv(
                pv="ca://SYSX:FOREIGN",
                raw_pv="$(P):Y",
                resolution="resolved",
                role="read",
                protocol="ca",
                top_level_display="zz_other.bob",
                origin_file="zz_other.bob",
            ),
        ),
    )
    displays = (other, owner) if foreign_first else (owner, other)
    return PvInventory(
        repo_root="/nowhere",
        displays=displays,  # type: ignore[arg-type]
        diagnostics=PvDiagnostics(context_capped=capped),
    )


async def test_capped_file_alone_is_a_lower_bound_on_the_normal_path(tmp_path: Path) -> None:
    """The new term carrying ALONE, on a non-empty result: contexts into the file were dropped.

    The file resolves one channel and is itself a capped target, while the top it contributes to is
    not. The pre-existing top term therefore cannot fire and the note can only come from
    ``rel in capped_targets``. That also pins the effect as NOT confined to the empty path: a
    variant that applied the new term only when the channel list came back empty is red here.

    ⚠ The file sits in a subdirectory and ``context_capped`` names the same root-relative posix
    path, so a verdict computed from the bare file name or from the absolute path is red too. (A
    comparison that merely FOLDS the directory away on both sides stays green; that mutation is not
    covered, and no cheap fixture covers it.)

    ⚠ The foreign display comes FIRST on purpose. Every other cap fixture in this file holds a
    single display, so a verdict collected from ``inventory.displays[0]`` alone would be invisible
    in all of them while being silently wrong on a real dataset (fbis: 257 displays).
    """
    root = tmp_path / "ds"
    (root / "sub").mkdir(parents=True)
    frag = root / "sub" / "frag.bob"
    frag.write_text('<display version="2.0.0"><name>F</name></display>', encoding="utf-8")
    mock_batch = AsyncMock(
        return_value={"results": [{"pv_name": "SYSX:X", "value": 1}], "errors": []}
    )
    with (
        patch(
            "epics_mcp.services.inventory_adapter.analyze_pv_inventory",
            return_value=_own_pv_inventory(
                "sub/frag.bob", capped=("sub/frag.bob",), foreign_first=True
            ),
        ),
        patch("epics_mcp.tools.validate.pv_get_batch", mock_batch),
    ):
        result = await _validate_pvs(file_path=str(frag), displays_dir=str(root))

    assert result["total"] == 1, "the file view holds its own channel"
    notes = result["notes"]
    # No display carries display_path == rel, so the view note cannot fire and the cap note is
    # alone. Pinning the count as well as the content keeps a second note from sneaking in unread.
    assert isinstance(notes, list) and len(notes) == 1, notes
    assert "per-display context cap" in str(notes[0]), notes


async def test_capped_file_with_only_unresolved_pvs_of_its_own_says_it_is_a_lower_bound(
    tmp_path: Path,
) -> None:
    """GB-26 itself: ``total: 0`` on a file that declares PVs which have not resolved yet.

    This is the measured shape of the two files the previous flag stayed silent on (one of them
    resolving 0 channels at the default cap and 5576 at four times the cap): they DO declare PVs,
    but not one of those resolves under the cap, so nothing passes the resolution filter and the
    old in-loop flag could never be set.

    ⚠ It is also the guard against putting the occurrence test BEHIND that filter, which reads like
    a harmless simplification and measurably reinstates the whole defect: both real files have zero
    resolved channels at the default cap.

    ⚠ The foreign display comes LAST, so a verdict reset per display (rather than accumulated over
    the inventory) loses the evidence and goes red.
    """
    root = tmp_path / "ds"
    root.mkdir()
    (root / "d.bob").write_text('<display version="2.0.0"><name>D</name></display>', "utf-8")
    spy = AsyncMock(side_effect=_connect_all)
    with (
        patch(
            "epics_mcp.services.inventory_adapter.analyze_pv_inventory",
            return_value=_capped_inventory(
                "d.bob",
                declared=False,
                own_unresolved=True,
                capped=("d.bob",),
                foreign_top="zz_other.bob",
            ),
        ),
        patch("epics_mcp.tools.validate.pv_get_batch", spy),
    ):
        result = await _validate_pvs(file_path=str(root / "d.bob"), displays_dir=str(root))

    assert result["total"] == 0, "the file's own occurrence does not resolve at this cap"
    spy.assert_not_awaited()
    notes = result["notes"]
    # Two notes on purpose: the cap note AND the view note, because the display view does resolve
    # the embedded fragment's channel. Asserting len == 1 here (the shape of the sibling test just
    # above) would be red on correct code.
    assert isinstance(notes, list) and len(notes) == 2, notes
    assert "per-display context cap" in str(notes[0]), notes


async def test_capped_file_that_declares_no_pv_at_all_is_not_called_a_lower_bound(
    tmp_path: Path,
) -> None:
    """The negative control the guard exists for, and the one the naive repair got wrong.

    Same constellation as the test above with ONE difference: the file declares no occurrence of
    its own. Its file view is then exactly complete at every cap, and calling it a lower bound
    would be a false statement, however capped the file is as an embed target.

    Measured on a 257-display dataset: without this guard the verdict fires on 73 files instead of
    49, and 20 of the 24 newly flagged ones declare no PV whatsoever, against 2 genuinely silenced
    files that the repair recovers.
    """
    root = tmp_path / "ds"
    root.mkdir()
    (root / "d.bob").write_text('<display version="2.0.0"><name>D</name></display>', "utf-8")
    with patch(
        "epics_mcp.services.inventory_adapter.analyze_pv_inventory",
        return_value=_capped_inventory("d.bob", declared=False, capped=("d.bob",)),
    ):
        result = await _validate_pvs(file_path=str(root / "d.bob"), displays_dir=str(root))

    assert result["total"] == 0
    notes = result["notes"]
    assert isinstance(notes, list) and len(notes) == 1, notes
    assert not any("per-display context cap" in str(n) for n in notes), (
        "the file declares nothing, so its total: 0 is exact and not a lower bound"
    )


async def test_a_capped_top_of_a_foreign_file_does_not_make_this_file_a_lower_bound(
    tmp_path: Path,
) -> None:
    """The second negative control: the verdict is scoped to THIS file's own occurrences.

    ``zz_other.bob`` is a capped top, but of a display this file contributes nothing to; and
    ``frag.bob`` is a capped ORIGIN of this display, which is a statement about the display view,
    not about the file view. Neither may reach the file verdict.

    Two mutations are red here and nowhere else: collecting the tops without the ``origin_file``
    scoping (the foreign top leaks in), and matching the display view's origins instead of the
    file's tops (``frag.bob`` leaks in).
    """
    root = tmp_path / "ds"
    root.mkdir()
    (root / "d.bob").write_text('<display version="2.0.0"><name>D</name></display>', "utf-8")
    with (
        patch(
            "epics_mcp.services.inventory_adapter.analyze_pv_inventory",
            return_value=_capped_inventory(
                "d.bob",
                declared=True,
                capped=("frag.bob", "zz_other.bob"),
                foreign_top="zz_other.bob",
            ),
        ),
        patch("epics_mcp.tools.validate.pv_get_batch", side_effect=_connect_all),
    ):
        result = await _validate_pvs(file_path=str(root / "d.bob"), displays_dir=str(root))

    assert result["total"] == 1, "the file's own channel, and only that one"
    notes = result["notes"]
    assert isinstance(notes, list) and len(notes) == 1, notes
    assert not any("per-display context cap" in str(n) for n in notes), (
        "neither a foreign top nor an origin of the DISPLAY view is a statement about this file"
    )


# --- GB-31: the two cap verdicts, their difference, and the view switch between them -------------
#
# Four mutations survived the whole suite before this block existed (each proven green on its own,
# full-suite, restored from a copy in between): dropping the ``rel`` term of
# ``_display_view_is_capped``; nailing the view switch to ``file_capped``; moving the display
# view's ``origins`` in front of the resolution filter; and answering the file view's macro test
# with a constant True. The same forbidden-fragment rule as the block above applies here:
# ``"lower bound"`` appears in BOTH notes, so the cap note is identified by
# ``"per-display context cap"`` and never by that phrase.


def _unresolved_only_inventory(rel: str, *, capped: tuple[str, ...]) -> object:
    """A display whose single own occurrence does NOT resolve, and which embeds nothing.

    Its DISPLAY view is therefore empty, and so is ``origins``, because that set is filled behind
    the same resolution filter that fills the view. The display's cap verdict can then come from
    the ``rel`` term and from nothing else, which is the shape this file otherwise never builds:
    every cap fixture above hands the display a RESOLVED fragment channel, so its origins are
    never empty and the ``rel`` term never decides alone.

    Engine-shaped: an unbound macro leaves the string as written, so ``pv`` still carries the macro
    and the protocol falls back to the raw one (``pv_analysis/expansion.py``).
    """
    from opi_navigation.pv_analysis import (
        DisplayPvInventory,
        ExpandedPv,
        PvDiagnostics,
        PvInventory,
    )

    return PvInventory(
        repo_root="/nowhere",
        displays=(
            DisplayPvInventory(
                display_path=rel,
                operator_facing=True,
                pvs=(
                    ExpandedPv(
                        pv="$(P):X",
                        raw_pv="$(P):X",
                        resolution="dynamic",
                        role="read",
                        protocol="ca",
                        top_level_display=rel,
                        origin_file=rel,
                    ),
                ),
            ),
        ),  # type: ignore[arg-type]
        diagnostics=PvDiagnostics(context_capped=capped),
    )


def _unresolved_fragment_inventory(rel: str, *, capped: tuple[str, ...]) -> object:
    """A display that resolves a channel of its OWN and holds an UNRESOLVED fragment occurrence.

    The fragment's file is what *capped* names. Collected behind the resolution filter, as the code
    does, the fragment never reaches ``origins`` and the display view is NOT called a lower bound.
    Collected in front of it, which is what the file view deliberately does one function away, it
    would. That single difference is the whole point of this fixture.

    Engine-shaped for the same reason as the sibling above: the fragment's macro is unbound here.
    """
    from opi_navigation.pv_analysis import (
        DisplayPvInventory,
        ExpandedPv,
        PvDiagnostics,
        PvInventory,
    )

    return PvInventory(
        repo_root="/nowhere",
        displays=(
            DisplayPvInventory(
                display_path=rel,
                operator_facing=True,
                pvs=(
                    ExpandedPv(
                        pv="ca://SYSX:OWN",
                        raw_pv="$(P):OWN",
                        resolution="resolved",
                        role="read",
                        protocol="ca",
                        top_level_display=rel,
                        origin_file=rel,
                    ),
                    ExpandedPv(
                        pv="$(Q):X",
                        raw_pv="$(Q):X",
                        resolution="dynamic",
                        role="read",
                        protocol="ca",
                        top_level_display=rel,
                        origin_file="frag.bob",
                    ),
                ),
            ),
        ),  # type: ignore[arg-type]
        diagnostics=PvDiagnostics(context_capped=capped),
    )


def _single_occurrence_inventory(rel: str, *, capped: tuple[str, ...], raw_pv: str) -> object:
    """A display with exactly ONE resolved occurrence of its own, written as *raw_pv*.

    The negative control for the macro test is *raw_pv* without a macro: such a file resolves the
    same channel at every cap and is present at every cap, so its file view is exact however capped
    the file is as an embed target. No other fixture in this module can serve as one, because all
    five cap fixtures above write a macro-templated ``raw_pv``, which is why answering the macro
    test with a constant True left the entire suite green.

    ⚠ For a macro-FREE occurrence the engine yields ``pv == raw_pv`` byte for byte: nothing can
    inject a protocol prefix into a string that has no macro to expand, and that is the very
    invariant the macro test rests on. Writing a ``ca://`` prefix beside a concrete ``raw_pv``
    would contradict it inside the fixture that exists to pin it, so this builder derives ``pv``
    from the spelling instead of taking it as a second, freely settable argument.
    """
    from opi_navigation.pv_analysis import (
        DisplayPvInventory,
        ExpandedPv,
        PvDiagnostics,
        PvInventory,
    )

    templated = "$(" in raw_pv or "${" in raw_pv
    return PvInventory(
        repo_root="/nowhere",
        displays=(
            DisplayPvInventory(
                display_path=rel,
                operator_facing=True,
                pvs=(
                    ExpandedPv(
                        pv="ca://SIM:EXPANDED:Val" if templated else raw_pv,
                        raw_pv=raw_pv,
                        resolution="resolved",
                        role="read",
                        protocol="ca",
                        top_level_display=rel,
                        origin_file=rel,
                    ),
                ),
            ),
        ),  # type: ignore[arg-type]
        diagnostics=PvDiagnostics(context_capped=capped),
    )


def test_the_display_cap_verdict_reads_the_rel_term_on_its_own() -> None:
    """The predicate itself, called directly, because the tool-level sibling can be masked.

    Two mutations TOGETHER (dropping this term and moving ``origins`` in front of the resolution
    filter) leave ``test_a_capped_display_with_an_empty_view_is_still_a_lower_bound`` GREEN: the
    unresolved own occurrence then reaches ``origins``, its file is the capped one, and the
    intersection answers True for the wrong reason. Measured. A direct call is immune, because it
    takes the origins as an argument.

    ⚠ Do not read that as "only this test catches the pair". The neighbour that pins the filter
    placement goes red on the same combination, so the pair is caught either way; what this test
    adds is that it names WHICH of the two terms was lost, rather than leaving a reader to infer it
    from a second test's failure.
    """
    assert _display_view_is_capped("d.bob", set(), frozenset({"d.bob"})) is True, (
        "contexts INTO this display were dropped, which is what the rel term is for"
    )
    assert _display_view_is_capped("d.bob", set(), frozenset({"other.bob"})) is False, (
        "a capped target this display neither is nor is fed by says nothing about it"
    )
    assert _display_view_is_capped("d.bob", {"frag.bob"}, frozenset({"frag.bob"})) is True, (
        "and the origins term still carries on its own"
    )


async def test_a_capped_display_with_an_empty_view_is_still_a_lower_bound(tmp_path: Path) -> None:
    """The same verdict through the tool, on the one real shape where the rel term decides alone.

    Measured on a 257-display dataset: 42 displays answer an empty display view and are flagged by
    this term alone, and quadrupling the cap grows exactly one of them, from 0 to 5576 channels.
    That one case is why the term stays; the price is recorded in the predicate's docstring.

    ⚠ The assertion sits on ``shown_by_display_capped`` and NOT on ``notes``: the FILE view of this
    fixture is capped too (its own occurrence is macro-templated and its file is the capped
    target), so a note fires either way and would hide the mutation.
    """
    root = tmp_path / "ds"
    root.mkdir()
    (root / "d.bob").write_text('<display version="2.0.0"><name>D</name></display>', "utf-8")
    with patch(
        "epics_mcp.services.inventory_adapter.analyze_pv_inventory",
        return_value=_unresolved_only_inventory("d.bob", capped=("d.bob",)),
    ):
        result = await _validate_pvs(file_path=str(root / "d.bob"), displays_dir=str(root))

    assert result["total"] == 0, "nothing of this display resolves at this cap"
    assert result["shown_by_display"] == 0, "and its display view is empty for the same reason"
    assert result["shown_by_display_capped"] is True


async def test_an_unresolved_capped_fragment_does_not_reach_the_display_verdict(
    tmp_path: Path,
) -> None:
    """``origins`` is collected BEHIND the resolution filter, and that placement is a decision.

    Mirroring what the file view does one function away, i.e. collecting in front of the filter,
    is the obvious-looking repair and was measured to be a pure precision loss: 164 displays
    flagged instead of 93 on a 257-display dataset, with not one additional case of the 11 that
    provably grow. Without this test that mutation survives the whole suite and the decision has
    no holder.
    """
    root = tmp_path / "ds"
    root.mkdir()
    (root / "d.bob").write_text('<display version="2.0.0"><name>D</name></display>', "utf-8")
    with (
        patch(
            "epics_mcp.services.inventory_adapter.analyze_pv_inventory",
            return_value=_unresolved_fragment_inventory("d.bob", capped=("frag.bob",)),
        ),
        patch("epics_mcp.tools.validate.pv_get_batch", side_effect=_connect_all),
    ):
        result = await _validate_pvs(file_path=str(root / "d.bob"), displays_dir=str(root))

    assert result["total"] == 1, "the display's own channel resolves"
    assert result["shown_by_display_capped"] is False, (
        "the capped fragment contributed no resolved channel here, so it is not evidence that a "
        "larger budget would extend THIS display's view"
    )


async def test_the_view_switch_answers_with_the_display_verdict(tmp_path: Path) -> None:
    """``view="display"`` must report the DISPLAY view's cap verdict, not the file view's.

    The fixture makes the two disagree: the file is not a capped target and its own top is not
    capped (so the file view is exact), while the fragment feeding the display IS capped. No cap
    fixture in this file called with ``view="display"`` before, so nailing the switch to
    ``file_capped`` survived the entire suite.

    ⚠ The ``view="file"`` half below is NOT new coverage: four tests above already redden a switch
    nailed the other way. It is kept because the two directions belong on one screen, and saying so
    here is cheaper than a reader re-deriving it.
    """
    root = tmp_path / "ds"
    root.mkdir()
    (root / "d.bob").write_text('<display version="2.0.0"><name>D</name></display>', "utf-8")
    inventory = _capped_inventory("d.bob", declared=True, capped=("frag.bob",))
    with (
        patch("epics_mcp.services.inventory_adapter.analyze_pv_inventory", return_value=inventory),
        patch("epics_mcp.tools.validate.pv_get_batch", side_effect=_connect_all),
    ):
        shown = await _validate_pvs(
            file_path=str(root / "d.bob"), displays_dir=str(root), view="display"
        )
        by_file = await _validate_pvs(file_path=str(root / "d.bob"), displays_dir=str(root))

    assert shown["total"] == 2, "the display view holds its own channel and the fragment's"
    notes = shown["notes"]
    assert isinstance(notes, list) and len(notes) == 1, notes
    assert "per-display context cap" in str(notes[0]), notes

    assert by_file["total"] == 1
    file_notes = by_file["notes"]
    assert isinstance(file_notes, list)
    assert not any("per-display context cap" in str(n) for n in file_notes), (
        "the file view of this fixture is exact, and the switch must not hand it the other verdict"
    )


async def test_a_file_whose_pvs_carry_no_macro_is_never_called_a_lower_bound(
    tmp_path: Path,
) -> None:
    """The macro test, and the only fixture in this file that can go red when it disappears.

    A concrete occurrence expands to itself under every binding and is already present at every
    cap, so no budget can make it contribute a channel it does not contribute now: the file view is
    exact however capped the file is as an embed target. Answering the macro test with a constant
    True left all 1904 tests green before this one existed, because every other cap fixture here
    writes a macro-templated ``raw_pv``.

    Measured on a 257-display dataset: the test drops the verdict from 53 files to 49, the four it
    silences answer identically at cap 256 and at 1024, and none of the 9 files that provably grow
    is lost.
    """
    root = tmp_path / "ds"
    root.mkdir()
    (root / "d.bob").write_text('<display version="2.0.0"><name>D</name></display>', "utf-8")
    with (
        patch(
            "epics_mcp.services.inventory_adapter.analyze_pv_inventory",
            return_value=_single_occurrence_inventory(
                "d.bob", capped=("d.bob",), raw_pv="SIM:CONCRETE:Val"
            ),
        ),
        patch("epics_mcp.tools.validate.pv_get_batch", side_effect=_connect_all),
    ):
        result = await _validate_pvs(file_path=str(root / "d.bob"), displays_dir=str(root))

    assert result["total"] == 1
    # Deliberately narrower than ``"notes" not in result``: the view note is structurally silent in
    # this fixture (one channel in both views), so the broader form would also go red for a reason
    # that has nothing to do with the macro test.
    notes = result.get("notes", [])
    assert isinstance(notes, list)
    assert not any("per-display context cap" in str(n) for n in notes), (
        "the file declares only concrete PVs, so its answer is exact at every cap"
    )
    assert result["shown_by_display_capped"] is True, (
        "and the DISPLAY axis is deliberately untouched by this: it stays over-cautious, so a "
        "change that leaked across the two verdicts is red here"
    )


@pytest.mark.parametrize("raw_pv", ["$(P):X", "${P}:X"])
async def test_both_macro_spellings_keep_the_lower_bound_note(tmp_path: Path, raw_pv: str) -> None:
    """The positive control, over BOTH macro spellings the engine understands.

    ``contains_macros`` recognises ``$(NAME)`` and ``${NAME}``, and every other fixture in this
    module writes only the first. Measured: replacing the call with ``"$(" in ev.raw_pv`` left the
    whole suite green, so a file templated exclusively in the brace form would have lost its note
    in silence. This parametrisation is what makes that mutation red.
    """
    root = tmp_path / "ds"
    root.mkdir()
    (root / "d.bob").write_text('<display version="2.0.0"><name>D</name></display>', "utf-8")
    with (
        patch(
            "epics_mcp.services.inventory_adapter.analyze_pv_inventory",
            return_value=_single_occurrence_inventory("d.bob", capped=("d.bob",), raw_pv=raw_pv),
        ),
        patch("epics_mcp.tools.validate.pv_get_batch", side_effect=_connect_all),
    ):
        result = await _validate_pvs(file_path=str(root / "d.bob"), displays_dir=str(root))

    assert result["total"] == 1
    notes = result.get("notes", [])
    assert isinstance(notes, list)
    assert any("per-display context cap" in str(n) for n in notes), (
        f"{raw_pv} is macro-templated, so a larger budget could extend this file's list"
    )
