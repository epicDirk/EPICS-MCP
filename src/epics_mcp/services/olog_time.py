"""Olog's half of the time-window contract: the wall-clock wire format.

Olog cannot parse ISO-8601. This module is the reason that no longer matters.

WHY (measured live against an Olog sandbox, 2026-07-15, not inferred)
----------------------------------------------------------------------
Olog parses ``start``/``end`` with a **vendored, stripped** ``TimestampFormats`` whose accepted
patterns are all SPACE-separated (``yyyy-MM-dd HH:mm:ss.SSS`` and shorter). Upstream Phoebus' own
copy of that class supports ``ISO_INSTANT``, the Olog fork deleted it. This is what separates Olog
from the Alarm Logger, which uses the real class and reads ISO with a zone directly.

An unreadable value does not raise; it degrades to *now* and the window collapses:

    start=2026-01-01T00:00:00Z & end=2027-01-01T00:00:00Z  ->  HTTP 200, 0 results
    start=2026-01-01 00:00:00.000 & end=2027-01-01 ...       ->  HTTP 200, 9 results

Same window, same data, different format. The ISO answer is not an error, it is a **plausible
empty result**. The shared classifier (:mod:`epics_mcp.services._time_window`) decides what a
value IS and refuses what it cannot classify; this module only renders what Olog can read.
"""

from __future__ import annotations

from datetime import datetime

from epics_mcp.services._time_window import classify_time_value

# Sent alongside any absolute value so Olog interprets our wall-clock strings in a known zone
# rather than its own default. Relative amounts need no tz: Olog resolves them by instant
# arithmetic, on which the zone is provably a no-op.
OLOG_WIRE_TZ = "UTC"


def _format_wire(moment: datetime) -> str:
    """Render *moment* (UTC) in Olog's millisecond wall-clock pattern ``yyyy-MM-dd HH:mm:ss.SSS``.

    Built explicitly rather than via ``strftime``: ``%Y`` is not portably zero-padded below year
    1000. This pattern is Olog's first parse candidate, so it is the one most likely to stay.
    """
    return (
        f"{moment.year:04d}-{moment.month:02d}-{moment.day:02d} "
        f"{moment.hour:02d}:{moment.minute:02d}:{moment.second:02d}."
        f"{moment.microsecond // 1000:03d}"
    )


def normalize_olog_time(value: str, *, param: str) -> tuple[str, bool]:
    """Return ``(wire_value, is_absolute)`` for an Olog ``start``/``end``, or raise.

    Absolute values become Olog's wall clock in UTC (the caller then sends :data:`OLOG_WIRE_TZ`);
    relative amounts pass through verbatim. Raises
    :class:`~epics_mcp.services._time_window.TimeWindowFormatError` for anything the shared
    classifier cannot vouch for, see that module for each trap and why refusing beats sending.
    """
    moment = classify_time_value(value, param=param)
    if moment is None:
        return value.strip(), False
    return _format_wire(moment), True
