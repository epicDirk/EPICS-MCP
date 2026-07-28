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
