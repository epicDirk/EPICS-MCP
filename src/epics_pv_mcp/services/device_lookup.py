"""Pure assembly for the Wedge-2 device lookup: reverse-lookup screens + live values + source IOC.

Composes three ALREADY-FETCHED inputs into one deterministic :class:`DeviceLookupReport` — there is
**no I/O here**, so the merge is fully offline-testable (the tool wrapper :mod:`~.tools.find_device`
runs the macro-aware inventory, the p4p batch read and the ChannelFinder GET, then hands the raw
results to :func:`build_device_report`). Mirrors the pure :func:`crossplane_check` next to its thin
wrapper, and the build-once discipline: the reverse-lookup itself is ``opi_navigation``'s
``find_displays`` (consumed, never rebuilt); this module only stitches its result to the live plane.

The reused models expose RAW fields only, so two report fields are **derived** here (kept explicit):
``matched_channels`` = ``channel_name`` of each ``DisplayMatch.matched_pvs`` (the protocol-stripped
channel the p4p read uses), and ``connected`` = membership in the ``pv_get_batch`` ``results``
(else the ``errors`` entry) — ``pv_get_batch`` carries no ``connected`` field.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, cast

from opi_navigation.pv_analysis import channel_name
from opi_navigation.pv_analysis.lookup import PvLookupResult
from pydantic import BaseModel, ConfigDict

PvRole = Literal["read", "write"]


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
    #: ChannelFinder source IOC / host — ``None`` when ChannelFinder is disabled or has no entry.
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
    live_capped: bool = False
    channelfinder_enabled: bool = False
    notes: tuple[str, ...] = ()


def collect_channels(lookup: PvLookupResult) -> tuple[str, ...]:
    """Distinct, sorted protocol-stripped channels across all matched screens (the p4p read set).

    ``DisplayMatch.matched_pvs`` are raw (carry the ``pva://``/``ca://`` prefix as stored); the live
    p4p read needs the bare channel, so each is normalized via the shared ``channel_name`` (the same
    strip used by the cross-plane adapter — one source, no drift).
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
    live_capped: bool,
    channelfinder_enabled: bool,
) -> DeviceLookupReport:
    """Merge reverse-lookup + p4p batch read + ChannelFinder result — pure, deterministic.

    *lookup* is the ``find_displays`` result; *live_results* is the ``pv_get_batch`` dict
    (``{"results": [...], "errors": [...]}``) of the LIVE-QUERIED (capped) channel subset;
    *ioc_channels* is the ``_find_channels`` dict (``{"enabled": ..., "channels": [...]}``). The
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
        notes.append(
            f"Live status shown for {len(channels)} of {total_matched} matched channels "
            "(read capped) — refine the query for full live coverage. The screen list is complete."
        )
    if not channelfinder_enabled:
        notes.append(
            "ChannelFinder disabled — source IOC not resolved (set EPICS_MCP_CHANNELFINDER_URL)."
        )
    else:
        # An enabled-but-failing CF carries a "note" (set best-effort at the edge); a successful
        # enabled query has none. Surface it so "unreachable" is not conflated with "no entry".
        cf_note = ioc_channels.get("note")
        if isinstance(cf_note, str) and cf_note:
            notes.append(cf_note)

    return DeviceLookupReport(
        query=lookup.query,
        match=lookup.match,
        screens=_screen_matches(lookup),
        channels=tuple(channels),
        total_matched_channels=total_matched,
        live_capped=live_capped,
        channelfinder_enabled=channelfinder_enabled,
        notes=tuple(notes),
    )


def render_markdown(report: DeviceLookupReport) -> str:
    """Render a :class:`DeviceLookupReport` as deterministic Markdown."""
    lines = ["# Device Lookup", ""]
    lines.append(f"- **Query:** `{report.query}` (match: {report.match})")
    lines.append(f"- **Operator screens showing it:** {len(report.screens)}")
    for screen in report.screens:
        roles = "/".join(report_roles(screen.roles))
        lines.append(f"  - `{screen.display_path}` — {screen.count} channel(s) [{roles}]")
    lines.append(
        f"- **Live channels:** {len(report.channels)} of {report.total_matched_channels} matched"
    )
    for channel in report.channels:
        if channel.connected:
            alarm = f", {channel.severity}" if channel.severity else ""
            status = f"connected (value: {channel.value}{alarm})"
        else:
            status = f"disconnected ({channel.error or 'no value'})"
        ioc = f" — IOC `{channel.source_ioc}`" if channel.source_ioc else ""
        lines.append(f"  - `{channel.channel}` — {status}{ioc}")
    if report.notes:
        lines.append("")
        lines.append("## Notes")
        lines.extend(f"- {note}" for note in report.notes)
    return "\n".join(lines)


def report_roles(roles: tuple[PvRole, ...]) -> tuple[str, ...]:
    """Stable role labels for the Markdown (empty roles render as ``read``-implied ``—``)."""
    return roles if roles else ("—",)
