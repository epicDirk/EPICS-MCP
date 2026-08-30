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
  a value against ``[DRVL, DRVH]``; a step/jump policy is a separate, deliberate extension, and
  :func:`check_step_within_limit` below is it. It is deliberately NOT derived from the drive
  limits: a permitted step is a property of how an operator wants the plant driven, not of the
  record, which is precisely why ``control_t`` has no field for it. It therefore comes from
  ``EPICS_MCP_MAX_WRITE_STEP``, is OFF by default, and asks a different question from this one,
  not "may the value BE there" but "may it GET there in one write".
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
    """A control limit, or a live value, as a finite float; None when missing / wrong type / not
    finite.

    Mirrors ``readback._control_min_step``'s type guard: bool is an int subclass but a boolean limit
    is meaningless, so exclude it explicitly; NaN/inf limits are treated as "no usable bound". The
    step check below reuses it for the CURRENT value, where the same three rejections mean the same
    thing: you cannot measure a distance from something that is missing, boolean, or undefined.
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


class StepVerdict(BaseModel):
    """The verdict of checking how FAR one write moves a value (the second post-admission check)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: True = the distance is within the configured limit; False = it exceeds it (the ONLY deny);
    #: None = not step-checked, either because no limit is configured or because the distance is
    #: not measurable. The write proceeds on None.
    within_limit: bool | None
    #: The measured distance from the current value, when one could be measured.
    step: float | None = None
    #: The limit actually applied; None when no limit is configured.
    limit: float | None = None
    #: Why a write was refused, or why the distance was not checked. Deliberately None BOTH when the
    #: check passed and when no limit is configured: a note on every write in the shipping posture
    #: (the limit is off by default) would be noise a caller cannot act on. The distinction that
    #: matters to a caller, whether the write was refused, is carried by the raise.
    note: str | None = None


def check_step_within_limit(
    written: str, readback: Mapping[str, object], max_step: float
) -> StepVerdict:
    """Check how far one write moves the value, against the configured step limit. Pure and total.

    Args:
        written: the raw string the write tool received, the same value
            :func:`check_value_in_bounds` is given.
        readback: what the write's pre-read ``pv_get`` returned. The CURRENT value is read from its
            ``value`` key, so this needs no ``pv_get`` of its own, and an ``enum`` block, when the
            record has one, is what makes the record not step-checkable.
        max_step: ``EpicsConfig.max_write_step``. ``0`` (the default) means the check is OFF.

    Returns a :class:`StepVerdict`; the caller raises + audits only on ``within_limit is False``.

    The fail-open cases, each with its reason, because a silent pass is the thing to avoid:

    * **No limit configured** (``max_step <= 0``). Not fail-open so much as OFF, and it is the
      shipping posture: an existing deployment behaves exactly as before this field existed.
    * **An enum record** (the pre-read carries an ``enum`` block). Its ``value`` is an INDEX, so a
      compare would succeed and mean nothing: the distance between two enum choices is not a
      physical distance, and a limit below 1 would block a command record from ever being set.
      Measured on this server's own read path: ``epics_client._format_value`` puts the index in
      ``value`` and sets ``enum`` beside it, so a bo/mbbo comes back numeric and WOULD be compared
      without this branch.
    * **A current value that is missing, boolean or non-finite.** You cannot measure a distance
      from something undefined, and letting ``nan`` through would compare False and pass silently.
    * **A written value that is not numeric** (an enum label, an arbitrary string), the same
      reasoning :func:`check_value_in_bounds` gives for its own coercion failure.

    Fail-CLOSED on a non-finite written value, but ONLY once the earlier branches have passed:
    the enum and unusable-current-value checks run first, so ``nan`` written to a record this
    function cannot measure at all still fails OPEN. That ordering is deliberate (a record whose
    distance is not measurable is not measurable for garbage either), and it is stated here because
    "fail-closed on a non-finite value" without the qualifier would promise more than the order
    delivers. On that branch ``step`` stays None, since there is no distance to report; the caller
    must build its message from :attr:`StepVerdict.note` rather than interpolating the field.
    """
    if max_step <= 0:
        return StepVerdict(within_limit=None)

    if "enum" in readback:
        return StepVerdict(
            within_limit=None,
            limit=max_step,
            note="enum record; an index distance is not a step, so the write was not step-checked",
        )

    current = _finite_limit(readback.get("value"))
    if current is None:
        return StepVerdict(
            within_limit=None,
            limit=max_step,
            note="no usable current value; distance not measurable, so the write was not "
            "step-checked",
        )

    try:
        value = float(written)
    except (TypeError, ValueError):
        return StepVerdict(
            within_limit=None,
            limit=max_step,
            note=f"written {written!r} is not numeric; the write was not step-checked",
        )

    if not math.isfinite(value):
        return StepVerdict(
            within_limit=False,
            limit=max_step,
            note=f"written value {written!r} is not finite, so the step is not a step",
        )

    step = abs(value - current)
    within = step <= max_step
    return StepVerdict(
        within_limit=within,
        step=step,
        limit=max_step,
        note=None
        if within
        else (
            f"step {step!r} from {current!r} to {value!r} exceeds the configured limit {max_step!r}"
        ),
    )
