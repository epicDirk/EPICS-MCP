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
    # The only place any test looks at ``notes`` ACROSS the tool boundary. The honesty notes are a
    # user-visible part of the answer, and the registered wrapper is a layer the inner tests never
    # execute; without this line a wrapper that dropped them would pass the whole suite.
    assert "notes" in payload, "the honesty notes must survive the registered tool wrapper"


def _capped_inventory(
    rel: str,
    *,
    declared: bool,
    capped: tuple[str, ...],
    own_unresolved: bool = False,
    foreign_top: str | None = None,
    foreign_first: bool = False,
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
        diagnostics=PvDiagnostics(context_capped=capped),
    )


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
        "epics_mcp.tools.validate.analyze_pv_inventory",
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
        patch("epics_mcp.tools.validate.analyze_pv_inventory", return_value=fake),
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
            "epics_mcp.tools.validate.analyze_pv_inventory",
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
            "epics_mcp.tools.validate.analyze_pv_inventory",
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
        "epics_mcp.tools.validate.analyze_pv_inventory",
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
            "epics_mcp.tools.validate.analyze_pv_inventory",
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
