"""Value-bounds checking for PV writes (O2): before a sanctioned write, verify the written value is
within the record's OWN drive limits (control_t DRVL/DRVH), and refuse an out-of-range value BEFORE
the put, the second line of defence after the name/rate gate, which never checks the value.

Pure, deterministic, I/O-free. :func:`check_value_in_bounds` takes the value that is about to be
written (the raw string the write tool received) and the dict the write's pre-read ``pv_get``
returned, and decides whether the value is in bounds. It never touches the network, the clock, or
randomness, so it is isolated-unit-testable; the caller (the write tool) owns the actual ``pv_get``,
the audit emission, and the raise.

Design notes (the "why"):

* **Fail-OPEN when the record declares no drive limits.** A record without a control block, or
  whose ``DRVL == DRVH`` pair was dropped upstream (``epics_client._drop_degenerate_limits``), or
  a non-numeric written value, is *not bounds-checkable*, the write PROCEEDS (``in_bounds=None``).
  You cannot bound what the record does not declare; refusing would break every limitless record
  and the sanctioned enum write (measured: the reset command lane carries no control block). It is
  the one deliberate fail-open; it carries an honest note so the pass is visible in the result.
* **Live ``control_t`` is the source of truth** (decision E5), NOT a static limits DB: the
  record's own drive limits cannot drift from the record. They ride the write's existing pre-read
  (``control.limit_low`` / ``control.limit_high``), so O2 needs no extra ``pv_get``.
* **Scope = min/max only.** ``control_t`` carries DRVL/DRVH (min/max) and minStep (resolution):
  no ``max_step`` jump limit and no ``writable`` flag (those are not record fields). So O2 bounds
  a value against ``[DRVL, DRVH]``; a step/jump policy would be a separate, deliberate extension.
* **The written value arrives as a string** (``set_pv_value(value: str)``). A numeric compare
  coerces it to float; a string that will not coerce is *not bounds-checkable* (fail-open). A
  coercible but non-finite value (``nan``/``inf``) against a bounded record is refused, it lies
  outside any finite range (fail-closed on garbage).
"""

from __future__ import annotations

import math
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict

# Shared note for the fail-open (no usable drive limits) verdicts.
_NO_LIMITS_NOTE = "no drive limits declared; value not bounds-checked"


class BoundsVerdict(BaseModel):
    """The verdict of checking a written value against a record's drive limits (the intermediate
    result the write tool acts on)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: True = within ``[limit_low, limit_high]``; False = out of range (the ONLY deny); None = not
    #: bounds-checkable (no drive limits / non-numeric written value) → the write proceeds.
    in_bounds: bool | None
    #: The record's drive limits actually applied; None when not bounds-checkable.
    limit_low: float | None = None
    limit_high: float | None = None
    #: Human/agent-facing reason, why a value was refused, or why it was not bounds-checkable.
    note: str | None = None


def _finite_limit(value: object) -> float | None:
    """A control limit as a finite float, or None when missing / the wrong type / non-finite.

    Mirrors ``readback._control_min_step``'s type guard: bool is an int subclass but a boolean limit
    is meaningless, so exclude it explicitly; NaN/inf limits are treated as "no usable bound".
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        limit = float(value)
        if math.isfinite(limit):
            return limit
    return None


def check_value_in_bounds(written: str, readback: Mapping[str, object]) -> BoundsVerdict:
    """Check the written value against the record's control drive limits. Pure and total.

    Args:
        written: the raw string the write tool received (``set_pv_value(value: str)``).
        readback: what the write's pre-read ``pv_get`` returned
            (``services.epics_client._format_value`` shape, an optional ``control`` block with
            ``limit_low`` / ``limit_high`` when the record declares drive limits).

    Returns a :class:`BoundsVerdict`; the caller raises + audits only on ``in_bounds is False``.
    """
    control = readback.get("control")
    if not isinstance(control, dict):
        # No control block at all (e.g. an enum record) → nothing to bound against.
        return BoundsVerdict(in_bounds=None, note=_NO_LIMITS_NOTE)

    low = _finite_limit(control.get("limit_low"))
    high = _finite_limit(control.get("limit_high"))
    if low is None or high is None or low >= high:
        # Missing limits, or a zero/negative-width or non-finite range (DRVL==DRVH is dropped
        # upstream) → unusable bound.
        return BoundsVerdict(in_bounds=None, note=_NO_LIMITS_NOTE)

    try:
        value = float(written)
    except (TypeError, ValueError):
        # A non-numeric written value (enum label, arbitrary string) cannot be range-checked.
        return BoundsVerdict(
            in_bounds=None,
            limit_low=low,
            limit_high=high,
            note=f"written {written!r} is not numeric; value not bounds-checked",
        )

    if not math.isfinite(value):
        # nan/inf ARE float()-coercible but lie outside any finite range → refuse (fail-closed).
        return BoundsVerdict(
            in_bounds=False,
            limit_low=low,
            limit_high=high,
            note=f"written value {written!r} is not finite",
        )

    in_range = low <= value <= high
    return BoundsVerdict(
        in_bounds=in_range,
        limit_low=low,
        limit_high=high,
        note=None if in_range else f"value {value!r} is outside drive limits [{low}, {high}]",
    )
