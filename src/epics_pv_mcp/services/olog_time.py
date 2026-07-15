"""Time-window normalization for the Phoebus Olog search API.

Olog cannot parse ISO-8601. This module is the reason that no longer matters.

WHY THIS EXISTS (measured live against an Olog sandbox, 2026-07-15 — not inferred)
---------------------------------------------------------------------------------
Olog's search accepts ``start``/``end`` as either an absolute wall-clock string or a relative
amount, and parses them with a **vendored, stripped** ``TimestampFormats`` whose accepted patterns
are all SPACE-separated (``yyyy-MM-dd HH:mm:ss.SSS`` and shorter). Upstream Phoebus' own copy of
that class supports ``ISO_INSTANT``; the Olog fork deleted it.

An unparseable value does NOT raise. Olog falls back to its relative-amount parser, whose regex
scans for a unit token (``days``/``h``/``min``/…). An ISO string contains none, so the scan yields
nothing and the parser returns a **zero duration** — which Olog then subtracts from *now*. The
window silently collapses to ``[now, now+µs]`` and matches nothing:

    start=2026-01-01T00:00:00Z & end=2027-01-01T00:00:00Z  ->  HTTP 200, 0 results
    start=2026-01-01 00:00:00.000 & end=2027-01-01 …       ->  HTTP 200, 9 results

Same window, same data, different format. The ISO answer is not an error — it is a **plausible
empty result**, indistinguishable from "nothing matched". That is the failure mode this server
exists to never produce, so the fix is to make every value we send one Olog can actually parse, and
to refuse — loudly, before any request — every value we cannot classify.

THE INVARIANT: a value we cannot classify is never sent. Each rejected form below would otherwise
degrade to *now* server-side and produce a well-formed 200 carrying the wrong answer. Turning a
silent wrong answer into a loud client-side error IS the fix; the rejections are not pedantry.

Absolute values are additionally re-anchored to UTC and accompanied by ``tz=UTC`` on the wire
(:data:`OLOG_WIRE_TZ`). Without an explicit ``tz``, Olog interprets a wall-clock string in the
SERVER's default zone — so an unqualified window is silently offset against any Olog not running
UTC, and no test running against a UTC sandbox would ever reveal it.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

# Sent alongside any absolute value so Olog interprets our wall-clock strings in a known zone
# rather than its own default. Relative amounts need no tz: Olog resolves them by instant
# arithmetic, on which the zone is provably a no-op.
OLOG_WIRE_TZ = "UTC"

# Olog's relative-amount grammar, restricted to the units that actually WORK, with an explicit
# quantity. Deliberately narrower than Olog's own alternation:
#   * 'millis' is EXCLUDED although Olog's regex matches it — Olog's unit dispatch tests
#     startsWith("mi") before equals("ms"), so 'millis' silently means MINUTES. Excluding it here
#     turns a 1000x silent error into a loud one ('ms' is the honest spelling).
#   * months/years are excluded via _PERIOD_UNIT_RE below (they fail server-side).
# Anchored (fullmatch) because Olog's own parser uses a substring scan, which silently accepts
# trailing junk — 'garbage 7 days' would parse there, and we refuse to inherit that.
_RELATIVE_RE = re.compile(
    r"\s*\d+(?:\.\d+)?\s*"
    r"(?:ms|seconds|second|secs|sec|s"
    r"|minutes|minute|mins|min"
    r"|hours|hour|h"
    r"|days|day|d"
    r"|weeks|week|w)\s*",
    re.IGNORECASE,
)

# "now" is Olog's own literal for a zero offset and resolves correctly.
_RELATIVE_LITERALS = frozenset({"now"})

# Units Olog's regex matches but which then misbehave. Months/years become a Period, and Olog
# subtracts the amount from an Instant, which cannot carry months or years -> the request is
# rejected server-side. Kept as a NAMED rejection so the caller is told what to use instead,
# rather than meeting the raw failure.
_PERIOD_UNIT_RE = re.compile(r"^\s*\d*\.?\d*\s*(?:months?|mon|mo|years?|y)\s*$", re.IGNORECASE)


class OlogTimeFormatError(ValueError):
    """A ``start``/``end`` value Olog would not honour as written. Raised BEFORE any request.

    Deliberately a :class:`ValueError` and NOT an ``OlogError``: nothing was sent, so this is a bad
    ARGUMENT, not a service failure. ``services.checkers`` maps every ``OlogError`` to
    ``EpicsConnectionError`` ("cannot reach Olog"), which would be a lie here — do not "tidy" this
    class into ``olog_exceptions``.
    """


def _format_wire(moment: datetime) -> str:
    """Render *moment* (UTC) in Olog's millisecond wall-clock pattern ``yyyy-MM-dd HH:mm:ss.SSS``.

    Built explicitly rather than via ``strftime``: ``%Y`` is not portably zero-padded below year
    1000. The pattern is Olog's first parse candidate, so it is the one most likely to stay.
    """
    return (
        f"{moment.year:04d}-{moment.month:02d}-{moment.day:02d} "
        f"{moment.hour:02d}:{moment.minute:02d}:{moment.second:02d}."
        f"{moment.microsecond // 1000:03d}"
    )


def _as_absolute(value: str) -> str | None:
    """Return *value* as an Olog wall-clock string, or ``None`` if it is not an absolute time.

    Accepts everything :func:`datetime.fromisoformat` does (ISO-8601 with ``Z`` or an offset, a
    naive timestamp, a bare date, and Olog's own space-separated form — so normalization is
    idempotent). A naive value is read as UTC: the alternative is the server's zone, which the
    client cannot know, and guessing it is the silent-offset bug this module closes.
    """
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return _format_wire(moment.astimezone(UTC))


def normalize_olog_time(value: str, *, param: str) -> tuple[str, bool]:
    """Return ``(wire_value, is_absolute)`` for an Olog ``start``/``end`` — or raise.

    *param* names the offending argument in the error message. Absolute values are converted to
    Olog's wall-clock form in UTC; relative amounts pass through verbatim, because resolving them
    here would substitute OUR clock for the server's, and for a logbook the server's is
    authoritative.

    Absolute is tried first, mirroring Olog's own order. The two sets provably do not overlap:
    every relative form is rejected by ``fromisoformat`` (verified across the full accepted
    grammar), so the order is a faithful mirror rather than a tie-break.

    Raises :class:`OlogTimeFormatError` for anything else — see the module docstring for why
    rejecting beats sending.
    """
    text = value.strip()
    if not text:
        raise OlogTimeFormatError(
            f"{param} is empty — give an absolute time or an amount like '7 days'."
        )

    absolute = _as_absolute(text)
    if absolute is not None:
        return absolute, True

    if text.lower() in _RELATIVE_LITERALS:
        return text, False

    if _PERIOD_UNIT_RE.match(text):
        raise OlogTimeFormatError(
            f"{param}={value!r}: Olog cannot apply months or years to a point in time — "
            f"use days or weeks instead (e.g. '90 days' for ~3 months)."
        )

    if _RELATIVE_RE.fullmatch(text):
        return text, False

    raise OlogTimeFormatError(
        f"{param}={value!r} is not a time Olog can read. Use an absolute time "
        f"(e.g. '2026-07-15T10:00:00Z' or '2026-07-15 10:00:00') or a single amount "
        f"(e.g. '7 days', '90 min', '30 s'). Note: bare 'm' is not a unit (use 'min'), "
        f"'millis' means minutes to Olog (use 'ms'), compound amounts like '1 day 20 s' "
        f"are unreliable, and a raw epoch number is not accepted. Olog would silently read "
        f"any of these as 'now' and answer with an empty result rather than an error."
    )
