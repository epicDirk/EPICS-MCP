"""Shared classification of a ``start``/``end`` time-window value for the Phoebus REST planes.

Olog and the Alarm Logger both parse a time window with Phoebus' ``TimeParser``, and both share a
failure mode that is the reason this module exists: **an unreadable value is not an error**. The
parser falls back to a unit-regex scan; a string with no unit token yields a **zero** duration,
which the server subtracts from *now*. The window collapses, the range query matches nothing, and
the answer is a well-formed **HTTP 200 with an empty list**, indistinguishable from "nothing
matched". Both were measured live (2026-07-15) and both were shipping this behaviour.

So: classify here, and refuse anything we cannot classify BEFORE it is sent. A loud client-side
error is strictly better than a plausible empty answer.

The traps below are not pedantry: each was observed live returning a wrong answer silently:

* ``2026-07-08T12:45:58``: ISO **without a zone**. The zone-aware parser needs an offset and the
  others want a space separator, so a naive ISO matches nothing → *now*. This is what
  ``datetime.now().isoformat()`` emits, which makes it the most likely wrong value of all.
* ``500 millis``: the unit dispatch tests ``startsWith("mi")`` before ``equals("ms")``, so this
  means 500 **minutes**. Measured: it returns data, just the wrong window. Use ``ms``.
* ``5 m``: there is no bare ``m`` unit → no match → *now*.
* ``1 day 20 seconds``: once a week/month unit is present all sub-day parts are dropped
  (``2 weeks 3 hours`` == ``2 weeks``), so a compound amount cannot be honoured faithfully.
* ``1767225600`` (epoch), ``garbage``, no unit token → *now*.
* months/years, subtracted from a point in time, which cannot carry them. The two planes differ
  in how loudly they fail; both are refused here so the caller gets one message naming the fix.

WHAT DIFFERS PER PLANE: the wire FORMAT, and each plane's module owns the CHOICE of it (plus any
extra param that choice implies, like Olog's ``tz``), see ``olog_time`` / ``alarm_time`` /
``archiver_time``. Olog cannot read ISO at all (its vendored parser has ISO support stripped out)
and needs a space-separated wall clock; the Alarm Logger and the Archiver both read zone-explicit
ISO, because both parse a real ``ISO_INSTANT``, an agreement on a standard, not a coincidence, so
:func:`format_iso_z` lives HERE and is shared by those two. Olog's wall-clock emitter stays in
``olog_time``: one user, one home. This module therefore carries the classifier PLUS the format
primitives more than one plane needs, never a plane's choice.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

# The relative-amount grammar, restricted to units that work AND carry an explicit quantity.
# Deliberately narrower than the server's own alternation (see the module docstring for each
# exclusion). Anchored, because the server's parser substring-scans and would silently accept
# trailing junk, 'garbage 7 days' parses there, and we refuse to inherit that.
_RELATIVE_RE = re.compile(
    r"\s*\d+(?:\.\d+)?\s*"
    r"(?:ms|seconds|second|secs|sec|s"
    r"|minutes|minute|mins|min"
    r"|hours|hour|h"
    r"|days|day|d"
    r"|weeks|week|w)\s*",
    re.IGNORECASE,
)

# The server's own literal for a zero offset; resolves correctly on both planes.
_RELATIVE_LITERALS = frozenset({"now"})

# Matches the units that parse but cannot be subtracted from a point in time.
_PERIOD_UNIT_RE = re.compile(r"^\s*\d*\.?\d*\s*(?:months?|mon|mo|years?|y)\s*$", re.IGNORECASE)


class TimeWindowFormatError(ValueError):
    """A ``start``/``end`` value the server would not honour as written. Raised BEFORE any request.

    Deliberately a :class:`ValueError` and NOT a per-plane ``RestClientError``: nothing was sent,
    so this is a bad ARGUMENT, not a service failure. ``services.checkers`` maps plane errors to
    ``EpicsConnectionError`` ("cannot reach it"), which would be a lie here, do not "tidy" this
    class into one of the ``*_exceptions`` modules.
    """


def classify_time_value(value: str, *, param: str) -> datetime | None:
    """Classify a window value: a UTC :class:`datetime` if absolute, ``None`` if a relative amount.

    ``None`` means "a valid relative amount, pass it through verbatim": resolving it here would
    substitute OUR clock for the server's, and the server's clock owns its data. Raises
    :class:`TimeWindowFormatError` for anything else.

    Absolute is tried first, mirroring the server's own order. The two sets provably do not
    overlap, ``fromisoformat`` rejects every relative form, so the order is a faithful mirror
    rather than a tie-break.
    """
    text = value.strip()
    if not text:
        raise TimeWindowFormatError(
            f"{param} is empty, give an absolute time or an amount like '7 days'."
        )

    moment = _as_utc(text)
    if moment is not None:
        return moment

    if text.lower() in _RELATIVE_LITERALS:
        return None

    if _PERIOD_UNIT_RE.match(text):
        raise TimeWindowFormatError(
            f"{param}={value!r}: months and years cannot be applied to a point in time, "
            f"use days or weeks instead (e.g. '90 days' for ~3 months)."
        )

    if _RELATIVE_RE.fullmatch(text):
        return None

    raise TimeWindowFormatError(
        f"{param}={value!r} is not a time this service can read. Use an absolute time "
        f"(e.g. '2026-07-15T10:00:00Z', include the zone) or a single amount (e.g. '7 days', "
        f"'90 min', '30 s'). Note: an ISO time WITHOUT a zone is not read as UTC, it is not read "
        f"at all; bare 'm' is not a unit (use 'min'); 'millis' means minutes (use 'ms'); compound "
        f"amounts like '1 day 20 s' are unreliable; a raw epoch number is not accepted. The server "
        f"would silently take any of these as 'now' and answer with an empty result, not an error."
    )


def require_absolute_time(value: str, *, param: str) -> datetime:
    """Like :func:`classify_time_value`, but for a plane that CANNOT take a relative amount.

    A deliberate sibling rather than a ``allow_relative=False`` flag on the classifier: that
    function returns ``datetime | None``, and a runtime bool cannot narrow a static return type:
    every caller would need an ``assert moment is not None`` (a lie: it is a contract) or the
    classifier would need ``@overload`` machinery to avoid it. And semantically ``None`` means
    "a valid relative amount, pass it through"; a plane with no pass-through is best expressed by a
    function that CANNOT return it.

    Every trap message still comes from the one classifier, this only removes an option.
    """
    moment = classify_time_value(value, param=param)
    if moment is None:
        raise TimeWindowFormatError(
            f"{param}={value!r}: this service reads only an absolute time, a relative amount is "
            f"not supported here (unlike the alarm/logbook searches). Give an absolute time WITH a "
            f"zone, e.g. '2026-07-15T10:00:00Z'."
        )
    return moment


def format_iso_z(moment: datetime) -> str:
    """Render *moment* (UTC) as ``yyyy-MM-ddTHH:mm:ss.SSSZ``, what ``ISO_INSTANT`` parses.

    Shared by every plane whose server reads zone-explicit ISO. Millisecond precision is kept
    deliberately: truncating to the second would move a caller's window boundary by up to a second
    without saying so, which is the same class of quiet inaccuracy this module exists to remove.

    Built explicitly rather than via ``isoformat()``: that emits ``+00:00`` rather than ``Z`` and
    drops the fraction when it is zero (so the wire form would vary with the input), and ``%Y``
    under ``strftime`` is not portably zero-padded below year 1000.
    """
    return (
        f"{moment.year:04d}-{moment.month:02d}-{moment.day:02d}T"
        f"{moment.hour:02d}:{moment.minute:02d}:{moment.second:02d}."
        f"{moment.microsecond // 1000:03d}Z"
    )


def _as_utc(value: str) -> datetime | None:
    """Return *value* as a UTC datetime, or ``None`` if it is not an absolute time.

    Accepts everything :func:`datetime.fromisoformat` does: ISO-8601 with ``Z`` or an offset, a
    naive timestamp, a bare date, and the space-separated form, so normalization is idempotent on
    both planes' native formats. A naive value is read as UTC; the alternative is the server's own
    zone, which the client cannot know, and guessing it is a silent offset.
    """
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)
