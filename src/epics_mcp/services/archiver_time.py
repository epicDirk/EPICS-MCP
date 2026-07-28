"""The Archiver's half of the time-window contract: zone-explicit ISO, and nothing else.

WHY (measured live against a real Archiver Appliance, 2026-07-15, not inferred)
--------------------------------------------------------------------------------
This is the third plane with a time window, and the one that behaves BEST, which is why its
docstring is worth reading before the other two. Measured accept-set of ``getData.json``:

    2026-07-08T00:00:00.000Z   -> 200        (also fine without the millis)
    2026-07-08T00:00:00        -> HTTP 500   (naive ISO, no zone)
    2026-07-08 00:00:00        -> HTTP 500   (the wall clock Olog requires)
    2026-07-08                 -> HTTP 500   (bare date)
    7 days / now               -> HTTP 500   (the amount the other two planes take)
    garbage                    -> HTTP 500
    start after end            -> HTTP 400

**Nothing is silent here.** Unlike Olog and the Alarm Logger, which read an unparseable time as
*now* and answer 200 with an empty list, this server rejects what it cannot read. So there is no
wrong-answer bug to fix, and deliberately NO client-side inverted-window guard either: Olog needs
one only because its 400 reaches an anonymous reader as a 401, whereas this 400 arrives as a 400.

What IS worth fixing is that the accept-set is the NARROWEST of the three: ISO with a zone, full
stop. A caller who learned ``7 days`` on ``get_alarm_history`` gets a 500 here, and so does
``datetime.now().isoformat()`` (which emits a naive ISO). Normalizing removes the whole class:
every absolute notation the sibling planes accept is converted to the one this server reads, and a
relative amount, which this plane genuinely cannot express, is refused by name rather than sent
into a 500 (see :func:`~epics_mcp.services._time_window.require_absolute_time`).
"""

from __future__ import annotations

from epics_mcp.services._time_window import format_iso_z, require_absolute_time


def normalize_archiver_time(value: str, *, param: str) -> str:
    """Return the wire value for an Archiver ``from``/``to``, or raise.

    Absolute values become zone-explicit ISO in UTC; a relative amount raises, because this server
    has no way to read one. Raises
    :class:`~epics_mcp.services._time_window.TimeWindowFormatError` for anything the shared
    classifier cannot vouch for.
    """
    return format_iso_z(require_absolute_time(value, param=param))
