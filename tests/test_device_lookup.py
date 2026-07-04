"""Offline tests for the pure device-lookup assembly (in-test PvLookupResult + injected fakes).

The merge (:func:`build_device_report`) does NO I/O: it stitches a ``find_displays`` result, a
``pv_get_batch``-shaped live dict and a ``_find_channels``-shaped IOC dict into the report. These
tests build all three by hand for full determinism. The wired path (real .bob → inventory → p4p
read) is covered in ``test_find_device_tool.py``.
"""

from opi_navigation.pv_analysis.lookup import DisplayMatch, PvLookupResult

from epics_pv_mcp.services.device_lookup import (
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
        "note": "ChannelFinder unreachable — source IOC not resolved.",
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
    len(channels) (p4p RESPONSES). The two diverge when the native batch silently drops a name
    (pv_get_batch zips names/values with strict=False) — here 2 attempted, only 1 came back."""
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
