"""Pure assembly for the Wedge-2 device lookup: reverse-lookup screens + live values + source IOC.

Composes three ALREADY-FETCHED inputs into one deterministic :class:`DeviceLookupReport`, there is
**no I/O here**, so the merge is fully offline-testable (the tool wrapper :mod:`~.tools.find_device`
runs the macro-aware inventory, the p4p batch read and the ChannelFinder GET, then hands the raw
results to :func:`build_device_report`). Mirrors the pure :func:`crossplane_check` next to its thin
wrapper, and the build-once discipline: the reverse-lookup itself is ``opi_navigation``'s
``find_displays`` (consumed, never rebuilt); this module only stitches its result to the live plane.

The reused models expose RAW fields only, so two report fields are **derived** here (kept explicit):
``matched_channels`` = ``channel_name`` of each ``DisplayMatch.matched_pvs`` (the protocol-stripped
channel the p4p read uses), and ``connected`` = membership in the ``pv_get_batch`` ``results``
(else the ``errors`` entry), ``pv_get_batch`` carries no ``connected`` field.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, cast

from opi_navigation.pv_analysis import channel_name
from opi_navigation.pv_analysis.lookup import PvLookupResult
from pydantic import BaseModel, ConfigDict

PvRole = Literal["read", "write"]

#: Longest RENDERED scalar value in the report, the trailing ellipsis included. Named rather than
#: spelled twice, because the two spellings are what drifted: the comparison said 80 and the slice
#: produced 82 (QA-12).
_VALUE_CAP = 80


class _Model(BaseModel):
    """Frozen, closed value object (deterministic tuples; typos rejected)."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ScreenMatch(_Model):
    """One operator-facing screen that references the queried device (from ``find_displays``)."""

    display_path: str
    name: str = ""
    #: Distinct, sorted protocol-stripped channels of this screen that matched the query.
    matched_channels: tuple[str, ...] = ()
    roles: tuple[PvRole, ...] = ()
    count: int = 0


class ChannelStatus(_Model):
    """Live + provenance status of one matched channel (live-queried subset)."""

    channel: str
    #: True iff the channel was in the p4p ``results`` (value came back); else it is in ``errors``.
    connected: bool
    value: object | None = None
    #: Alarm severity text, when the live read carried alarm metadata.
    severity: str | None = None
    #: The read error (timeout / not-found / connection) when ``connected`` is False.
    error: str | None = None
    #: ChannelFinder source IOC / host, ``None`` when ChannelFinder is disabled or has no entry.
    source_ioc: str | None = None
    source_host: str | None = None


class DeviceLookupReport(_Model):
    """Device lookup: which screens show the device, is it live, and which IOC serves it.

    ``channels`` covers only the LIVE-QUERIED (capped) channel subset; ``screens`` is complete (the
    reverse-lookup is cheap). ``live_capped`` + the matching note flag when the device matched more
    channels than were read live (``total_matched_channels`` is the full count).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    query: str
    match: str
    screens: tuple[ScreenMatch, ...] = ()
    channels: tuple[ChannelStatus, ...] = ()
    total_matched_channels: int = 0
    # ``live_read`` = channels the live read ATTEMPTED (the capped coverage count);
    # ``len(channels)`` = the subset that returned a status row. A degraded live read (an empty
    # envelope from find_device's fallback on a provider-contract breach) can make the latter
    # smaller. Header and capped note both report ``live_read`` so they never disagree.
    live_read: int = 0
    live_capped: bool = False
    channelfinder_enabled: bool = False
    notes: tuple[str, ...] = ()


def collect_channels(lookup: PvLookupResult) -> tuple[str, ...]:
    """Distinct, sorted protocol-stripped channels across all matched screens (the p4p read set).

    ``DisplayMatch.matched_pvs`` are raw (carry the ``pva://``/``ca://`` prefix as stored); the live
    p4p read needs the bare channel, so each is normalized via the shared ``channel_name`` (the same
    strip used by the cross-plane adapter, one source, no drift).
    """
    channels: set[str] = set()
    for display in lookup.displays:
        for pv in display.matched_pvs:
            channels.add(channel_name(pv))
    return tuple(sorted(channels))


def _screen_matches(lookup: PvLookupResult) -> tuple[ScreenMatch, ...]:
    """One :class:`ScreenMatch` per matched screen (order preserved from the ranked lookup)."""
    return tuple(
        ScreenMatch(
            display_path=display.display_path,
            name=display.name,
            matched_channels=tuple(sorted({channel_name(pv) for pv in display.matched_pvs})),
            roles=display.roles,
            count=display.count,
        )
        for display in lookup.displays
    )


def _index_by_pv(rows: object) -> dict[str, dict[str, object]]:
    """Index a ``pv_get_batch`` results/errors list by its ``pv_name`` (defensive on shape)."""
    indexed: dict[str, dict[str, object]] = {}
    if not isinstance(rows, list):
        return indexed
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("pv_name"), str):
            indexed[cast("str", row["pv_name"])] = row
    return indexed


def _index_iocs(ioc_channels: Mapping[str, object]) -> dict[str, dict[str, object]]:
    """Index ChannelFinder channels by exact ``name`` (the join key for the matched channels)."""
    indexed: dict[str, dict[str, object]] = {}
    channels = ioc_channels.get("channels")
    if not isinstance(channels, list):
        return indexed
    for channel in channels:
        if isinstance(channel, dict) and isinstance(channel.get("name"), str):
            indexed[cast("str", channel["name"])] = channel
    return indexed


def _str_or_none(value: object) -> str | None:
    """Narrow an arbitrary value to ``str`` (or ``None``) for the optional report fields."""
    return value if isinstance(value, str) else None


def build_device_report(
    lookup: PvLookupResult,
    live_results: Mapping[str, object],
    ioc_channels: Mapping[str, object],
    *,
    total_matched: int,
    live_read: int,
    live_capped: bool,
    channelfinder_enabled: bool,
) -> DeviceLookupReport:
    """Merge reverse-lookup + p4p batch read + ChannelFinder result, pure, deterministic.

    *lookup* is the ``find_displays`` result; *live_results* is the ``pv_get_batch`` dict
    (``{"results": [...], "errors": [...]}``) of the LIVE-QUERIED (capped) channel subset;
    *ioc_channels* is the ``_find_channels`` dict, typed at its source as
    :class:`~epics_pv_mcp.services.checkers.ChannelQueryResult` (``{"enabled": ..., "channels":
    [...]}`` on the list path); it is taken as a plain ``Mapping`` here so this pure merge stays
    independent of that shape. The
    per-channel ``channels`` list is derived from the read set (results and errors), joined to its
    serving IOC by exact channel name.
    """
    results = _index_by_pv(live_results.get("results"))
    errors = _index_by_pv(live_results.get("errors"))
    iocs = _index_iocs(ioc_channels)

    channels: list[ChannelStatus] = []
    for channel in sorted(set(results) | set(errors)):
        ioc = iocs.get(channel)
        result = results.get(channel)
        if result is not None:
            alarm = result.get("alarm")
            severity = alarm.get("severity_text") if isinstance(alarm, dict) else None
            channels.append(
                ChannelStatus(
                    channel=channel,
                    connected=True,
                    value=result.get("value"),
                    severity=_str_or_none(severity),
                    source_ioc=_str_or_none(ioc.get("ioc_name")) if ioc else None,
                    source_host=_str_or_none(ioc.get("host_name")) if ioc else None,
                )
            )
        else:
            channels.append(
                ChannelStatus(
                    channel=channel,
                    connected=False,
                    error=_str_or_none((errors.get(channel) or {}).get("error")),
                    source_ioc=_str_or_none(ioc.get("ioc_name")) if ioc else None,
                    source_host=_str_or_none(ioc.get("host_name")) if ioc else None,
                )
            )

    notes: list[str] = []
    if not lookup.displays:
        notes.append("No operator-facing screen references this device/query.")
    if live_capped:
        # S7-5: report the number of channels the live read ATTEMPTED (live_read, known in
        # find_device as len(read)), not len(channels), the latter counts p4p RESPONSES and
        # undercounts when fewer come back than were attempted (e.g. a degraded live read).
        notes.append(
            f"Live status shown for {live_read} of {total_matched} matched channels "
            "(read capped), refine the query for full live coverage. The screen list is complete."
        )
    # A degraded live read (best-effort at the find_device edge) carries a "note" on the live
    # envelope. Surface it so an empty channel list is explained (mirrors the ChannelFinder note).
    live_note = live_results.get("note")
    if isinstance(live_note, str) and live_note:
        notes.append(live_note)
    if not channelfinder_enabled:
        notes.append(
            "ChannelFinder disabled, source IOC not resolved (set EPICS_MCP_CHANNELFINDER_URL)."
        )
    else:
        # An enabled-but-failing CF carries a "note" (set best-effort at the edge); a successful
        # enabled query has none. Surface it so "unreachable" is not conflated with "no entry".
        cf_note = ioc_channels.get("note")
        if isinstance(cf_note, str) and cf_note:
            notes.append(cf_note)
        # F16 (S11): query_channels computes an honest `capped`, discarding it here meant a
        # >max_results device silently joined against a TRUNCATED registry, and a channel whose
        # entry fell past the cap showed source_ioc=None indistinguishably from "no CF entry".
        if ioc_channels.get("capped"):
            notes.append(
                "ChannelFinder result capped, the source-IOC join may be incomplete: a channel "
                "without source_ioc may simply have fallen past the cap, not be unregistered."
            )

    return DeviceLookupReport(
        query=lookup.query,
        match=lookup.match,
        screens=_screen_matches(lookup),
        channels=tuple(channels),
        total_matched_channels=total_matched,
        live_read=live_read,
        live_capped=live_capped,
        channelfinder_enabled=channelfinder_enabled,
        notes=tuple(notes),
    )


def _format_channel_value(value: object) -> str:
    """Render a live value compactly, a waveform/array is summarised, not dumped (S7-1).

    A p4p waveform value arrives here as a (potentially multi-thousand-element) list; rendering it
    raw would produce an unreadable line. Summarise an array as ``[N values: a, b, ...]`` and cap a
    long scalar string at :data:`_VALUE_CAP` characters, ellipsis included, to keep the
    operator-facing report readable.
    """
    if isinstance(value, (list, tuple)):
        count = len(value)
        if count == 0:
            return "[0 values]"
        head = ", ".join(str(v) for v in value[:2])
        return f"[{count} values: {head}{', ...' if count > 2 else ''}]"
    text = str(value)
    # The cap is on the RENDERED string, ellipsis included: 77 characters plus the three dots. The
    # earlier `text[:79] + "..."` produced 82 and quietly broke the promise the line above makes
    # (QA-12), which is the kind of arithmetic no reader re-does and no test was covering.
    return text if len(text) <= _VALUE_CAP else text[: _VALUE_CAP - 3] + "..."


def render_markdown(report: DeviceLookupReport) -> str:
    """Render a :class:`DeviceLookupReport` as deterministic Markdown."""
    lines = ["# Device Lookup", ""]
    lines.append(f"- **Query:** `{report.query}` (match: {report.match})")
    lines.append(f"- **Operator screens showing it:** {len(report.screens)}")
    for screen in report.screens:
        roles = "/".join(report_roles(screen.roles))
        lines.append(f"  - `{screen.display_path}`, {screen.count} channel(s) [{roles}]")
    # Use live_read (channels ATTEMPTED), not len(channels) (channels that returned), so this
    # header agrees with the capped note, which S7-5 anchored to live_read. They diverge only on a
    # degraded live read (an empty envelope); the per-channel rows below still list what returned.
    lines.append(
        f"- **Live channels:** {report.live_read} of {report.total_matched_channels} matched"
    )
    for channel in report.channels:
        if channel.connected:
            alarm = f", {channel.severity}" if channel.severity else ""
            status = f"connected (value: {_format_channel_value(channel.value)}{alarm})"
        else:
            status = f"disconnected ({channel.error or 'no value'})"
        ioc = f", IOC `{channel.source_ioc}`" if channel.source_ioc else ""
        lines.append(f"  - `{channel.channel}`, {status}{ioc}")
    if report.notes:
        lines.append("")
        lines.append("## Notes")
        lines.extend(f"- {note}" for note in report.notes)
    return "\n".join(lines)


def report_roles(roles: tuple[PvRole, ...]) -> tuple[str, ...]:
    """Stable role labels for the Markdown (empty roles render as ``read``-implied ``, ``)."""
    return roles if roles else (", ",)
