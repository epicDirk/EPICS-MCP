"""The Alarm Logger's half of the time-window contract: zone-explicit ISO.

WHY (measured live against a real Alarm Logger, 2026-07-15 — not inferred)
--------------------------------------------------------------------------
The Alarm Logger uses the REAL Phoebus ``TimestampFormats`` (its pom depends on ``core-util``; no
vendored copy), so unlike Olog it reads ISO-8601 **with a zone** natively. That much of the earlier
"the alarm plane is fine" assessment held. What it missed is that the same *silent* fallback is
still there for everything else. Measured, same 7-day window, same PV:

    start=2026-07-08T12:45:58Z   ->  events                (zone present: read correctly)
    start=2026-07-08T12:45:58    ->  HTTP 200, 0 events    (no zone: silently read as 'now')

One character. No error. A 7-day window is far too wide for a mere zone shift to empty, so this is
the collapse-to-now, not an offset — the same failure Olog has, reached by a different road. And it
is the road a caller is most likely to take: ``datetime.now().isoformat()`` emits exactly that
naive form.

The server's own range check offers no backstop — it is an empty ``if`` branch carrying a
``// TODO check that the start is before the end``: an inverted window is logged server-side and
the query runs anyway.

So absolute values are anchored and emitted WITH the zone, and the shared classifier
(:mod:`epics_pv_mcp.services._time_window`) refuses what it cannot vouch for. No ``tz`` parameter
is needed here (unlike Olog): the ``Z`` carries the zone itself, which is why this is the format to
prefer wherever the server can read it.
"""

from __future__ import annotations

from datetime import datetime

from epics_pv_mcp.services._time_window import classify_time_value


def _format_wire(moment: datetime) -> str:
    """Render *moment* (UTC) as ``yyyy-MM-ddTHH:mm:ss.SSSZ`` — what ``ISO_INSTANT`` parses.

    Millisecond precision is kept deliberately: truncating to the second would move a caller's
    window boundary by up to a second without saying so, which is the same class of quiet
    inaccuracy this module exists to remove.

    Built explicitly rather than via ``isoformat()``: that emits ``+00:00`` rather than ``Z`` and
    drops the fraction when it is zero (so the wire form would vary with the input), and ``%Y``
    under ``strftime`` is not portably zero-padded below year 1000.
    """
    return (
        f"{moment.year:04d}-{moment.month:02d}-{moment.day:02d}T"
        f"{moment.hour:02d}:{moment.minute:02d}:{moment.second:02d}."
        f"{moment.microsecond // 1000:03d}Z"
    )


def normalize_alarm_time(value: str, *, param: str) -> str:
    """Return the wire value for an Alarm Logger ``start``/``end`` — or raise.

    Absolute values become zone-explicit ISO in UTC; relative amounts pass through verbatim.
    Raises :class:`~epics_pv_mcp.services._time_window.TimeWindowFormatError` for anything the
    shared classifier cannot vouch for — see that module for each trap and why refusing beats
    sending.
    """
    moment = classify_time_value(value, param=param)
    if moment is None:
        return value.strip()
    return _format_wire(moment)
