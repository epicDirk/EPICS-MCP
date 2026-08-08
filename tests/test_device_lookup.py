"""Offline tests for the pure device-lookup assembly (in-test PvLookupResult + injected fakes).

The merge (:func:`build_device_report`) does NO I/O: it stitches a ``find_displays`` result, a
``pv_get_batch``-shaped live dict and a ``_find_channels``-shaped IOC dict into the report. These
tests build all three by hand for full determinism. The wired path (real .bob → inventory → p4p
read) is covered in ``test_find_device_tool.py``.
"""

import pytest
from opi_navigation.pv_analysis.lookup import DisplayMatch, PvLookupResult

from epics_mcp.services.device_lookup import (
    _VALUE_CAP,
    DeviceLookupReport,
    _format_channel_value,
    build_device_report,
    collect_channels,
    render_markdown,
)


def _lookup() -> PvLookupResult:
    """Two operator screens; the device's channels carry mixed protocol prefixes + a bare one."""
    return PvLookupResult(
        query="DEV-TEST01:Ctrl-EVR-01",
        match="prefix",
        total_pvs_matched=2,
        displays=(
            DisplayMatch(
                display_path="dln01_overview.bob",
                name="DLN01 Overview",
                matched_pvs=("pva://DEV-TEST01:Ctrl-EVR-01:status",),
                roles=("read",),
                count=1,
            ),
            DisplayMatch(
                display_path="dln01_ctrl.bob",
                name="DLN01 Control",
                matched_pvs=("DEV-TEST01:Ctrl-EVR-01:Cmd",),
                roles=("write",),
                count=1,
            ),
        ),
    )


def test_collect_channels_strips_protocol_distinct_sorted() -> None:
    lookup = PvLookupResult(
        query="x",
        match="prefix",
        total_pvs_matched=1,
        displays=(
            DisplayMatch(
                display_path="a.bob",
                matched_pvs=(
                    "pva://DEV:X",
                    "DEV:X",
                    "ca://DEV:Y",
                ),  # pva://DEV:X and DEV:X collapse
                roles=("read",),
                count=2,
            ),
        ),
    )
    assert collect_channels(lookup) == ("DEV:X", "DEV:Y")


def test_build_device_report_merges_screens_live_and_iocs() -> None:
    """One connected channel (with alarm + source IOC) and one disconnected channel are merged
    correctly; screens are listed in full; channelfinder_enabled flows through."""
    live = {
        "results": [
            {
                "pv_name": "DEV-TEST01:Ctrl-EVR-01:status",
                "value": 1,
                "alarm": {"severity_text": "MINOR"},
            }
        ],
        "errors": [{"pv_name": "DEV-TEST01:Ctrl-EVR-01:Cmd", "error": "Timeout"}],
    }
    iocs = {
        "enabled": True,
        "channels": [
            {
                "name": "DEV-TEST01:Ctrl-EVR-01:status",
                "owner": "",
                "ioc_name": "IOC-EVR-01",
                "host_name": "dln01-host",
                "properties": {},
                "tags": (),
            }
        ],
    }
    report = build_device_report(
        _lookup(),
        live,
        iocs,
        total_matched=2,
        live_read=2,
        live_capped=False,
        channelfinder_enabled=True,
    )

    assert tuple(s.display_path for s in report.screens) == ("dln01_overview.bob", "dln01_ctrl.bob")
    assert report.screens[0].matched_channels == (
        "DEV-TEST01:Ctrl-EVR-01:status",
    )  # pva:// stripped
    assert report.total_matched_channels == 2
    assert report.channelfinder_enabled is True

    by_channel = {c.channel: c for c in report.channels}
    connected = by_channel["DEV-TEST01:Ctrl-EVR-01:status"]
    assert connected.connected is True
    assert connected.value == 1
    assert connected.severity == "MINOR"
    assert connected.source_ioc == "IOC-EVR-01"
    assert connected.source_host == "dln01-host"
    dead = by_channel["DEV-TEST01:Ctrl-EVR-01:Cmd"]
    assert dead.connected is False
    assert dead.error == "Timeout"
    assert dead.source_ioc is None  # not in the ChannelFinder result


def test_build_device_report_channelfinder_disabled_note() -> None:
    """Disabled ChannelFinder → no source IOC on any channel + an honest note (no false data)."""
    live = {"results": [{"pv_name": "DEV:X", "value": 0}], "errors": []}
    iocs = {"enabled": False, "channels": [], "total": 0, "note": "ChannelFinder is disabled."}
    report = build_device_report(
        PvLookupResult(
            query="DEV",
            match="prefix",
            total_pvs_matched=1,
            displays=(
                DisplayMatch(
                    display_path="a.bob", matched_pvs=("DEV:X",), roles=("read",), count=1
                ),
            ),
        ),
        live,
        iocs,
        total_matched=1,
        live_read=1,
        live_capped=False,
        channelfinder_enabled=False,
    )
    assert report.channelfinder_enabled is False
    assert report.channels[0].source_ioc is None
    assert any("ChannelFinder disabled" in note for note in report.notes)


def test_build_device_report_channelfinder_unreachable_note() -> None:
    """An enabled CF carrying a best-effort 'note' (failure marker) surfaces it, distinct from the
    'disabled' note, so 'unreachable' is not conflated with 'no entry' (Impl-QA M2)."""
    live = {"results": [{"pv_name": "DEV:X", "value": 0}], "errors": []}
    iocs = {
        "enabled": True,
        "channels": [],
        "note": "ChannelFinder unreachable, source IOC not resolved.",
    }
    report = build_device_report(
        PvLookupResult(
            query="DEV",
            match="prefix",
            total_pvs_matched=1,
            displays=(
                DisplayMatch(
                    display_path="a.bob", matched_pvs=("DEV:X",), roles=("read",), count=1
                ),
            ),
        ),
        live,
        iocs,
        total_matched=1,
        live_read=1,
        live_capped=False,
        channelfinder_enabled=True,
    )
    assert any("unreachable" in note.lower() for note in report.notes)
    assert not any("disabled" in note.lower() for note in report.notes)


def test_build_device_report_cf_capped_note() -> None:
    """F16: a CAPPED ChannelFinder fetch must surface a note, the honest
    ``capped`` computed by query_channels was silently discarded here, so a >max_results device
    quietly joined against a TRUNCATED registry and some channels showed ``source_ioc=None``
    with no explanation (silent degradation, indistinguishable from 'CF has no entry')."""
    live = {"results": [{"pv_name": "DEV:X", "value": 0}], "errors": []}
    iocs = {
        "enabled": True,
        "channels": [{"name": "DEV:X", "ioc_name": "ioc-1", "host_name": "h1"}],
        "total": 1,
        "capped": True,
    }
    report = build_device_report(
        PvLookupResult(
            query="DEV",
            match="prefix",
            total_pvs_matched=1,
            displays=(
                DisplayMatch(
                    display_path="a.bob", matched_pvs=("DEV:X",), roles=("read",), count=1
                ),
            ),
        ),
        live,
        iocs,
        total_matched=1,
        live_read=1,
        live_capped=False,
        channelfinder_enabled=True,
    )
    assert any("capped" in note.lower() for note in report.notes)
    assert any("source" in note.lower() for note in report.notes)


def test_build_device_report_live_capped_note() -> None:
    """When fewer channels were read live than matched, an honest 'N of M' note appears."""
    live = {"results": [{"pv_name": "DEV:X", "value": 0}], "errors": []}
    iocs = {"enabled": False, "channels": []}
    report = build_device_report(
        PvLookupResult(
            query="DEV",
            match="prefix",
            total_pvs_matched=500,
            displays=(
                DisplayMatch(
                    display_path="a.bob", matched_pvs=("DEV:X",), roles=("read",), count=1
                ),
            ),
        ),
        live,
        iocs,
        total_matched=500,
        live_read=1,
        live_capped=True,
        channelfinder_enabled=False,
    )
    assert report.live_capped is True
    assert any("1 of 500 matched channels" in note for note in report.notes)


def test_build_device_report_capped_note_counts_attempts_not_responses() -> None:
    """S7-5 regression lock: the capped note counts live_read (channels ATTEMPTED), not
    len(channels) (p4p RESPONSES). These diverge whenever fewer responses return than were
    attempted, after S27 that arises from a degraded live read (find_device's empty-envelope
    fallback on a provider-contract breach), not from a native strict=False truncation (which now
    raises). Here a hand-built 1-of-2 pins the counting invariant."""
    live = {"results": [{"pv_name": "DEV:X", "value": 0}], "errors": []}  # only 1 response returned
    report = build_device_report(
        PvLookupResult(
            query="DEV",
            match="prefix",
            total_pvs_matched=500,
            displays=(
                DisplayMatch(
                    display_path="a.bob", matched_pvs=("DEV:X",), roles=("read",), count=1
                ),
            ),
        ),
        live,
        {"enabled": False, "channels": []},
        total_matched=500,
        live_read=2,  # attempted 2, but only 1 came back → live_read != len(channels)
        live_capped=True,
        channelfinder_enabled=False,
    )
    # the honest attempt count (live_read=2), NOT the response count (len(channels)=1)
    assert any("2 of 500 matched channels" in note for note in report.notes)
    assert not any("1 of 500 matched channels" in note for note in report.notes)


def test_build_device_report_surfaces_degraded_live_note() -> None:
    """S27: when find_device degrades the live read to an empty envelope on a provider-contract
    breach it carries a 'note'; build_device_report must surface it in report.notes so the empty
    channel list is explained, not silently blank (mirrors the ChannelFinder note path). Goes RED
    without the live-note extraction (the note would be dropped)."""
    live = {
        "results": [],
        "errors": [],
        "note": "Live read unavailable, malformed provider batch.",
    }
    report = build_device_report(
        PvLookupResult(
            query="DEV",
            match="prefix",
            total_pvs_matched=2,
            displays=(
                DisplayMatch(
                    display_path="a.bob", matched_pvs=("DEV:X",), roles=("read",), count=1
                ),
            ),
        ),
        live,
        {"enabled": False, "channels": []},
        total_matched=2,
        live_read=2,
        live_capped=False,
        channelfinder_enabled=False,
    )
    assert report.channels == ()  # empty live envelope → no per-channel rows
    assert any("Live read unavailable" in note for note in report.notes)


def test_render_header_and_capped_note_agree_on_the_attempt_count() -> None:
    """Consistency (QA): in the S7-5 divergence case (2 attempted, 1 returned) the render header and
    the capped note must show the SAME quantity, both live_read (2 of 500), never 1 in the header
    and 2 in the note. Before this fix the header used len(channels)=1 while the note used
    live_read=2, so the two lines contradicted each other."""
    live = {"results": [{"pv_name": "DEV:X", "value": 0}], "errors": []}  # only 1 of 2 came back
    report = build_device_report(
        PvLookupResult(
            query="DEV",
            match="prefix",
            total_pvs_matched=500,
            displays=(
                DisplayMatch(
                    display_path="a.bob", matched_pvs=("DEV:X",), roles=("read",), count=1
                ),
            ),
        ),
        live,
        {"enabled": False, "channels": []},
        total_matched=500,
        live_read=2,
        live_capped=True,
        channelfinder_enabled=False,
    )
    markdown = render_markdown(report)
    header = next(line for line in markdown.splitlines() if "**Live channels:**" in line)
    assert "2 of 500 matched" in header  # agrees with the note (live_read), not len(channels)=1
    assert "1 of 500 matched" not in header


def test_build_device_report_no_screens_note() -> None:
    empty = PvLookupResult(query="NOPE", match="prefix", total_pvs_matched=0, displays=())
    report = build_device_report(
        empty,
        {"results": [], "errors": []},
        {"enabled": False, "channels": []},
        total_matched=0,
        live_read=0,
        live_capped=False,
        channelfinder_enabled=False,
    )
    assert report.screens == ()
    assert report.channels == ()
    assert any("No operator-facing screen" in note for note in report.notes)


def test_render_markdown_deterministic() -> None:
    live = {
        "results": [{"pv_name": "DEV-TEST01:Ctrl-EVR-01:status", "value": 1}],
        "errors": [{"pv_name": "DEV-TEST01:Ctrl-EVR-01:Cmd", "error": "Timeout"}],
    }
    iocs = {"enabled": False, "channels": []}
    report = build_device_report(
        _lookup(),
        live,
        iocs,
        total_matched=2,
        live_read=2,
        live_capped=False,
        channelfinder_enabled=False,
    )
    markdown = render_markdown(report)
    assert "# Device Lookup" in markdown
    assert "dln01_overview.bob" in markdown
    assert "connected (value: 1)" in markdown
    assert "disconnected (Timeout)" in markdown
    assert render_markdown(report) == markdown  # deterministic


@pytest.mark.parametrize("length", [_VALUE_CAP - 1, _VALUE_CAP, _VALUE_CAP + 1, 500])
def test_a_rendered_scalar_never_exceeds_the_cap(length: int) -> None:
    """The cap is on the RENDERED string, ellipsis included (QA-12).

    ``_format_channel_value`` had no test at all, and its two spellings of the same number had
    drifted apart: the comparison admitted 80 characters while the slice emitted 79 plus a
    three-character ellipsis, so every capped value came out at 82. Nothing was wrong with the
    output to look at, which is exactly why it survived. Red on the pre-fix code at the two lengths
    above the cap, measuring 82 against an asserted 80.

    The boundary is swept rather than sampled at one point: a cap this small is normally got wrong
    by one, and one length would not tell an off-by-one from a working rule.
    """
    rendered = _format_channel_value("x" * length)

    assert len(rendered) <= _VALUE_CAP
    if length <= _VALUE_CAP:
        assert rendered == "x" * length  # untouched, no ellipsis on a value that fits
    else:
        assert rendered.endswith("...")
        assert len(rendered) == _VALUE_CAP  # a capped value uses the full width, never less


def test_a_capped_value_keeps_the_head_of_the_original() -> None:
    """The counter-direction: a cap that returned a constant, or the tail, would satisfy the
    length assertions above. What the operator needs is the START of the value."""
    original = "SIM:PS-01:Cur-RB reads " + "9" * 200

    rendered = _format_channel_value(original)

    assert original.startswith(rendered[:-3])
    assert rendered.startswith("SIM:PS-01:Cur-RB reads ")


def test_render_markdown_summarises_waveform_value() -> None:
    """S7-1: a waveform (large list) value is summarised, never dumped element-by-element."""
    big = list(range(5000))
    live = {"results": [{"pv_name": "DEV:WF", "value": big}], "errors": []}
    lookup = PvLookupResult(
        query="DEV",
        match="prefix",
        total_pvs_matched=1,
        displays=(
            DisplayMatch(display_path="a.bob", matched_pvs=("DEV:WF",), roles=("read",), count=1),
        ),
    )
    report = build_device_report(
        lookup,
        live,
        {"enabled": False, "channels": []},
        total_matched=1,
        live_read=1,
        live_capped=False,
        channelfinder_enabled=False,
    )
    markdown = render_markdown(report)
    assert "5000 values" in markdown  # element count named
    assert "4999" not in markdown  # NOT the full dump
    assert all(len(line) < 200 for line in markdown.splitlines())  # no runaway line


# --- GB-65: the inventory walk's own two caps, reported beside the screen list ---


def _report_with_caps(
    *,
    context_capped: tuple[str, ...] = (),
    glob_capped_count: int = 0,
    live_capped: bool = False,
    lookup: PvLookupResult | None = None,
) -> DeviceLookupReport:
    """A minimal report whose ONLY interesting input is the pair of walk-cap signals."""
    return build_device_report(
        lookup if lookup is not None else _lookup(),
        {"results": [], "errors": []},
        {"enabled": False, "channels": []},
        total_matched=2,
        live_read=0,
        live_capped=live_capped,
        channelfinder_enabled=False,
        context_capped=context_capped,
        glob_capped_count=glob_capped_count,
    )


def test_context_cap_reaches_the_caller_as_a_lower_bound_note() -> None:
    """The per-display context cap is reported, not swallowed.

    find_device runs the SAME inventory walk as its three sibling display tools, and until GB-65 it
    was the only one of the four that read neither of the walk's two caps: a screen whose PVs were
    cut short simply did not appear, with nothing saying so. Provably red: drop the
    ``context_capped`` block from :func:`build_device_report`.
    """
    report = _report_with_caps(context_capped=("a.bob", "b.bob"))

    assert any("context cap" in note for note in report.notes), report.notes
    assert any("2 display(s)" in note for note in report.notes), report.notes
    assert any("LOWER BOUND" in note for note in report.notes), report.notes
    # The two signals are independent: no glob cap fired here, so no glob note may appear.
    assert not any("glob cap" in note for note in report.notes), report.notes


def test_glob_cap_reaches_the_caller_as_a_lower_bound_note() -> None:
    """The glob cap is reported too, and it says the screen LIST is the lower bound.

    It is the cap that removes whole embedded SCREENS rather than instances, so it is exactly the
    one a device lookup must confess: the answer to "which screens show this device" gets shorter.
    Provably red: drop the ``glob_capped_count`` block from :func:`build_device_report`.
    """
    report = _report_with_caps(glob_capped_count=3)

    assert any("glob cap" in note for note in report.notes), report.notes
    assert any("3 globbed <file> reference(s)" in note for note in report.notes), report.notes
    assert any("lower bound" in note for note in report.notes), report.notes
    assert not any("context cap" in note for note in report.notes), report.notes


def test_the_glob_note_says_globbed_not_template() -> None:
    """The word is ``globbed``, and that is a correction this repo already had to make once.

    ``05b5fc2`` replaced "template <file> reference(s)" with "globbed" in all three tools that had
    it, because the engine fills ``glob_capped`` from every OTHER glob-resolved reference and skips
    template edges in that branch. Those three fixes are pinned per file, so a FOURTH note carrying
    the old word would be caught by none of them. This is that fourth pin.
    """
    report = _report_with_caps(glob_capped_count=1)

    glob_note = next(note for note in report.notes if "glob cap" in note)
    assert "globbed <file>" in glob_note
    assert "template" not in glob_note


def test_both_walk_caps_report_side_by_side() -> None:
    """Both caps firing at once yields BOTH notes, not one that displaces the other.

    ``05b5fc2`` found the untested-combination gap on the validate_pvs side (a new note inserting
    itself at ``notes[1]`` displaced the view note, and the only test pinning that order defaulted
    the other cap to empty). Provably red: make either block an ``elif``.
    """
    report = _report_with_caps(context_capped=("a.bob",), glob_capped_count=2)

    assert any("context cap" in note for note in report.notes), report.notes
    assert any("glob cap" in note for note in report.notes), report.notes


def test_walk_cap_notes_sit_between_the_screen_note_and_the_live_note() -> None:
    """The note ORDER is pinned, because the caps explain the line above them.

    A capped walk is what can make "No operator-facing screen references this device" FALSE, so
    the cap notes must follow it immediately and precede the live-read note, which is about a
    different half of the answer. Nothing pinned any order in this report before GB-65 (measured:
    zero ``notes[...]`` accesses in either device test file), and the sibling report lost exactly
    that bet once. Provably red: move either block below the ``live_capped`` block.
    """
    empty = PvLookupResult(query="NOPE", match="prefix", total_pvs_matched=0, displays=())
    report = _report_with_caps(
        context_capped=("a.bob",), glob_capped_count=1, live_capped=True, lookup=empty
    )

    assert "No operator-facing screen" in report.notes[0]
    assert "context cap" in report.notes[1]
    assert "glob cap" in report.notes[2]
    assert "read capped" in report.notes[3]


def test_an_empty_screen_list_under_a_cap_is_not_reported_as_a_clean_no() -> None:
    """The combination that matters most: no screens found AND the walk was cut short.

    On its own, "No operator-facing screen references this device" reads as a proven absence. Under
    a fired cap it is not one, and the reader must be able to see that from the same answer.
    """
    empty = PvLookupResult(query="NOPE", match="prefix", total_pvs_matched=0, displays=())
    report = _report_with_caps(glob_capped_count=4, lookup=empty)

    assert report.screens == ()
    assert any("No operator-facing screen" in note for note in report.notes)
    assert any("lower bound" in note for note in report.notes), report.notes


def test_the_walk_cap_notes_are_rendered_into_the_markdown() -> None:
    """The notes reach the human-facing half too, not just the JSON."""
    report = _report_with_caps(context_capped=("a.bob",), glob_capped_count=1)

    markdown = render_markdown(report)
    assert "context cap" in markdown
    assert "glob cap" in markdown
