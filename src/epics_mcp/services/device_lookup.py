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

from opi_navigation.models import NodeKind
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
    """One operator-facing FILE that references the queried device (from ``find_displays``).

    ⚠ Not necessarily a screen, and the class name is older than that distinction. The lookup
    underneath cuts on ``operator_facing`` and never on the kind of file, so a ``.plt`` Data
    Browser trend opened by an ``open_file`` button is a top level in its own right and comes back
    here. That is the right answer to "where do I see this device" and is deliberately not
    filtered; what was wrong until GQ-21 is that it arrived NAMED and COUNTED as an operator
    screen. ``node_kind`` is what says which it is.

    The name stays as it is on purpose. It is a Python class name and never reaches the wire (the
    tool registers with ``output_schema=None`` and ``model_dump`` emits field names only), while
    the report field ``screens`` that holds these DOES: renaming either buys a caller nothing that
    ``node_kind`` does not already tell them, and renaming the field would break every one of them.
    """

    display_path: str
    name: str = ""
    #: Distinct, sorted protocol-stripped channels of this screen that matched the query.
    matched_channels: tuple[str, ...] = ()
    roles: tuple[PvRole, ...] = ()
    count: int = 0
    #: What this match IS: an operator screen (``"display"``) or a Data Browser trend
    #: (``"trend"``). Taken from the engine's own ``DisplayMatch.node_kind``, never derived from
    #: the file suffix, the same rule ``display_files.is_inventory_file`` states. Additive with a
    #: default, so an older caller keeps working and a hand-built lookup keeps parsing.
    node_kind: NodeKind = "display"


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
    """Device lookup: where the device is shown, is it live, and which IOC serves it.

    ``channels`` covers only the LIVE-QUERIED (capped) channel subset; ``screens`` is unaffected by
    that cap (the reverse-lookup is cheap). ``live_capped`` + the matching note flag when the device
    matched more channels than were read live (``total_matched_channels`` is the full count).

    ⚠ ``screens`` does not hold screens only, and GQ-21 is the record of that reading a caller
    astray. A Data Browser trend opened by a button is operator-facing, so the lookup returns it,
    and until then it arrived indistinguishable from an operator screen. Each entry now says what
    it is on ``ScreenMatch.node_kind``, and ``display_count``/``trend_count`` say how the list
    splits. The field name stays, because renaming it would break every caller to tell them
    something the entries now tell them themselves.

    ⚠ "Unaffected by that cap" is not "complete", and the wording is deliberate. This report is
    built from the same inventory walk as ``validate_pvs``, and that walk has two caps of its own
    (per-display context and glob). **Both are reported since GB-65**, as their own ``notes``
    entries built from ``context_capped``/``glob_capped_count``, which ``tools/find_device.py``
    reads off the inventory and hands over. Before that, a screen dropped by the glob cap was
    missing from ``screens`` with nothing saying so.

    ⚠ That does NOT make an unconditional "the screen list is complete" true, and it stays banned
    in this file and in ``tools/find_device.py``. Two reasons, both load-bearing: the absence of a
    note means no cap FIRED on this run, never "complete" (the walk has limits its two caps do not
    measure, the case-sensitivity of path resolution among them), and a note that DID fire is a
    statement about the run, not a verdict on this query, because neither cap records the screen a
    device lookup returns. Say what is true instead: the LIVE cap does not shorten the screen list.

    ⚠ ``PvDiagnostics`` carries a THIRD field, ``excluded_by_protocol``, and it is deliberately not
    read here: measured, it cannot shorten this list. It counts the ``loc``/``sim``/``sys``/other
    references dropped from the CO-REFERENCE, and ``find_displays`` filters every display's PVs
    through ``is_real_resolved`` before matching, so such a channel can never reach ``screens`` in
    the first place. Reporting it would answer a question this tool does not ask.
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
    #: How ``screens`` splits by kind: operator screens and Data Browser trends. POSITIVELY
    #: COUNTED, never one subtracted from the other, the rule the engine's own ``PvLookupResult``
    #: states: a third ``NodeKind`` would land silently in the display figure under a subtraction,
    #: whereas positive counters let their sum fall short and say so. Both are counted over the
    #: ``screens`` tuple THIS report carries, so a header and its list cannot disagree, the same
    #: choice ``live_read`` makes above. That is only safe while the two ways of counting mean the
    #: same thing, so ``test_find_device_tool.py`` holds ours against the engine's own on one real
    #: walk: a projection that started adding, dropping or relabelling a match goes red there.
    display_count: int = 0
    trend_count: int = 0
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
            node_kind=display.node_kind,
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
    context_capped: tuple[str, ...] = (),
    glob_capped_count: int = 0,
) -> DeviceLookupReport:
    """Merge reverse-lookup + p4p batch read + ChannelFinder result, pure, deterministic.

    *lookup* is the ``find_displays`` result; *live_results* is the ``pv_get_batch`` dict
    (``{"results": [...], "errors": [...]}``) of the LIVE-QUERIED (capped) channel subset;
    *ioc_channels* is the ``_find_channels`` dict, typed at its source as
    :class:`~epics_mcp.services.checkers.ChannelQueryResult` (``{"enabled": ..., "channels":
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

    # Built once, here, because the two kind counters below have to describe the very tuple this
    # report carries rather than a second walk of the lookup.
    screens = _screen_matches(lookup)

    notes: list[str] = []
    if not screens:
        # GQ-21: it used to say "No operator-facing screen references this device/query", which
        # named a set it had never counted on its own. A trend is operator-facing too, so an empty
        # answer has to deny both kinds or deny neither.
        notes.append(
            "Nothing operator-facing references this device/query, neither a screen nor a Data "
            "Browser trend."
        )
    # GB-65: the two caps of the inventory WALK, reported right beside the screen count because
    # that is what they shorten. They sit here rather than further down on purpose: the note above
    # ("no screen references this device") is the one a capped walk can make FALSE, so a reader who
    # stops after the first line still sees why an empty list may not mean "nothing shows it".
    # Both are statements about the RUN, not about this query: unlike validate_pvs' file view there
    # is no per-screen membership test to make (the engine records the capped TARGET and the SOURCE
    # display of a capped glob, neither of which is the screen a device lookup returns), so neither
    # cap is turned into a per-screen verdict. Deliberately notes, never a withhold.
    # Kept SHORT on purpose: render_markdown prefixes "- ", and this file's own
    # test_render_markdown_summarises_waveform_value asserts every rendered line stays under 200
    # characters. The first draft was 196 and would have crossed that at a three-digit count.
    if context_capped:
        notes.append(
            f"{len(context_capped)} display(s) hit the per-display context cap, their resolved "
            "PVs are a LOWER BOUND, so a screen showing this device can be missing (raise "
            "context_cap)."
        )
    if glob_capped_count:
        notes.append(
            f"{glob_capped_count} globbed <file> reference(s) hit the glob cap, so embedded "
            "screens were left out and the screen list is a lower bound. This cap cannot be "
            "raised from here."
        )
    if live_capped:
        # S7-5: report the number of channels the live read ATTEMPTED (live_read, known in
        # find_device as len(read)), not len(channels), the latter counts p4p RESPONSES and
        # undercounts when fewer come back than were attempted (e.g. a degraded live read).
        notes.append(
            f"Live status shown for {live_read} of {total_matched} matched channels "
            "(read capped), refine the query for full live coverage. The screen list is not "
            "shortened by that cap."
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
        screens=screens,
        channels=tuple(channels),
        total_matched_channels=total_matched,
        live_read=live_read,
        live_capped=live_capped,
        channelfinder_enabled=channelfinder_enabled,
        # Positively counted, not subtracted; see the field comments on DeviceLookupReport.
        display_count=sum(1 for screen in screens if screen.node_kind == "display"),
        trend_count=sum(1 for screen in screens if screen.node_kind == "trend"),
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
    # GQ-21: the split belongs in the HEADER, not only in the JSON fields, because this is the
    # half a person reads and the line they draw a conclusion from. It used to say "Operator
    # screens showing it" and counted a Data Browser trend among them.
    lines.append(
        f"- **Operator-facing files showing it:** {len(report.screens)} "
        f"({report.display_count} screen(s), {report.trend_count} Data Browser trend(s))"
    )
    for screen in report.screens:
        roles = "/".join(report_roles(screen.roles))
        kind = ", Data Browser trend (not a screen)" if screen.node_kind == "trend" else ""
        lines.append(f"  - `{screen.display_path}`, {screen.count} channel(s) [{roles}]{kind}")
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
