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
        patch("epics_mcp.tools.validate.analyze_pv_inventory", spy),
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


def test_our_display_suffix_is_the_engines_own_constant() -> None:
    """The COUPLING, nailed to the engine's own rule rather than to a sample of file names.

    The first version of this guard wrote five names into a tmp directory and compared the
    collected set. That is blind in the direction that matters: widening the engine to also accept
    ``.opi`` leaves every one of those five names classified exactly as before, so we would start
    refusing files the inventory reads and no test would notice.

    Reading the engine's private constant is deliberate. A test may reach where production code
    should not, and this is the only assertion that goes red for a widening. If the engine ever
    replaces the constant with a set, the import fails loudly here, which is the correct outcome:
    the refusal in ``_run_validate`` would then need rewriting anyway.
    """
    from opi_navigation.discovery import _BOB_SUFFIX

    from epics_mcp.display_files import DISPLAY_SUFFIX

    assert DISPLAY_SUFFIX == _BOB_SUFFIX, (
        "the display-PV engine no longer selects files by this suffix; the refusal in "
        "_run_validate now rejects files the inventory would read"
    )


def test_the_engine_really_ignores_every_suffix_but_bob(tmp_path: Path) -> None:
    """The same coupling from the behaviour side: what the engine actually collects.

    Complements the constant check above, which cannot see a change in HOW the suffix is compared
    (a mutant making ``find_bob_files`` case-sensitive keeps the constant equal). Deliberately
    calls the genuine ``find_bob_files`` rather than restating its logic: a test that
    re-implements the rule it checks proves only that the author is consistent.
    """
    from opi_navigation.discovery import find_bob_files

    from epics_mcp.display_files import DISPLAY_SUFFIX

    for name in ("kept.bob", "KEPT2.BOB", "skipped.txt", "skipped.opi", "skipped.bob.bak", "x"):
        (tmp_path / name).write_text(_FRAGMENT, encoding="utf-8")

    collected = set(find_bob_files(tmp_path))
    assert collected == {"kept.bob", "KEPT2.BOB"}, (
        "the engine's file selection changed; DISPLAY_SUFFIX and the refusal in _run_validate "
        f"have to follow it (collected: {sorted(collected)})"
    )
    assert all(Path(name).suffix.lower() == DISPLAY_SUFFIX for name in collected)


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
    """``pv_names`` short-circuits the file, so the view fields have nothing to describe."""
    root = _views_dataset(tmp_path)
    spy = Mock(side_effect=AssertionError("the inventory must not run when a list is given"))
    with (
        patch("epics_mcp.tools.validate.analyze_pv_inventory", spy),
        patch("epics_mcp.tools.validate.pv_get_batch", side_effect=_connect_all),
    ):
        result = await _validate_pvs(
            pvs=["SIM:EXPLICIT:Val"],
            file_path=str(root / "owner.bob"),
            view="display",
        )

    assert result["total"] == 1
    assert "shown_by_display" not in result, "no display was consulted, so nothing may be claimed"
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


def _capped_inventory(rel: str, *, declared: bool, capped: tuple[str, ...]) -> object:
    """An inventory whose display *rel* embeds a fragment, with *capped* naming what was capped.

    *declared*: whether *rel* also owns a channel, i.e. whether the call takes the normal path
    (True) or the empty-result path (False).

    *capped* goes into ``diagnostics.context_capped`` verbatim, because WHICH path is in there
    decides which of the two cap tests can see it: the pre-existing flag matches on the events'
    ``top_level_display`` (so it needs *rel*), the new one also matches the origins of the
    display's own events (so ``frag.bob`` alone is enough for it, and invisible to the old one).
    """
    from opi_navigation.pv_analysis import (
        DisplayPvInventory,
        ExpandedPv,
        PvDiagnostics,
        PvInventory,
    )

    def _pv(name: str, origin: str) -> object:
        return ExpandedPv(
            pv=f"ca://{name}",
            raw_pv="$(P):X",
            resolution="resolved",
            role="read",
            protocol="ca",
            top_level_display=rel,
            origin_file=origin,
        )

    pvs = [_pv("SYSX:FROM_FRAGMENT", "frag.bob")]
    if declared:
        pvs.insert(0, _pv("SYSX:OWN", rel))
    return PvInventory(
        repo_root="/nowhere",
        displays=(
            DisplayPvInventory(display_path=rel, operator_facing=True, pvs=tuple(pvs)),  # type: ignore[arg-type]
        ),
        # The engine records the capped TARGET, not the top it was capped under.
        diagnostics=PvDiagnostics(context_capped=capped),
    )


async def test_capped_fragment_makes_the_display_figure_a_lower_bound(tmp_path: Path) -> None:
    """Normal path with BOTH paths capped: the new note says "at least", the old one survives.

    Both are listed as capped on purpose. The pre-existing note needs ``d.bob`` (it matches on
    ``top_level_display``); the new one would fire on ``frag.bob`` alone. Listing only the fragment
    is a different case, and it is the next test.

    ``notes[0]`` is pinned because order is what a model reads first, and nothing else would
    notice the two swapping.
    """
    root = tmp_path / "ds"
    root.mkdir()
    (root / "d.bob").write_text('<display version="2.0.0"><name>D</name></display>', "utf-8")
    with (
        patch(
            "epics_mcp.tools.validate.analyze_pv_inventory",
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
    """The one place the new cap test and the pre-existing ``capped`` flag disagree.

    The old flag is only ever set inside the ``origin_file``-filtered loop, so when nothing passes
    that filter it stays False no matter what was capped. Measured on a real dataset: 12 of the 42
    files taking this path are genuinely capped and every one of them reported ``capped=False``.
    Without this test, replacing the new check with the old flag is invisible.
    """
    root = tmp_path / "ds"
    root.mkdir()
    (root / "d.bob").write_text('<display version="2.0.0"><name>D</name></display>', "utf-8")
    with patch(
        "epics_mcp.tools.validate.analyze_pv_inventory",
        return_value=_capped_inventory("d.bob", declared=False, capped=("frag.bob",)),
    ):
        result = await _validate_pvs(file_path=str(root / "d.bob"), displays_dir=str(root))

    assert result["total"] == 0, "nothing passed the origin_file filter, so the old flag is False"
    assert result["shown_by_display_capped"] is True
    notes = result["notes"]
    assert isinstance(notes, list) and len(notes) == 1, "only the new note, the old one cannot fire"
    assert "at least 1 further channel(s)" in str(notes[0]), notes


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
