"""Readback verification for PV writes (O3): read the written value back, compare it against the
value written within a tolerance, and carry a structured, validated result.

Pure, deterministic, I/O-free. :func:`verify_readback` takes the value that was written (the raw
string the write tool received) and the dict a readback ``pv_get`` returned, and decides
``verified`` = True / False / None. It never touches the network, the clock, or randomness, so it
is isolated-unit-testable; the caller (the write tool) owns the actual ``pv_get`` and the audit
emission.

Design notes (the "why"):

* **Tri-state ``verified``.** A readback that could NOT be obtained, a timeout, or the p4p
  value-extraction fallback that yields ``value=None`` plus a ``note``, is not a measurement, so
  it is NEVER a mismatch: it is *not verifiable* (``None``). Collapsing "could not verify" into
  "verified wrong" (``False``) would manufacture false alarms.
* **``math.isclose`` for the numeric compare.** It combines a relative and an absolute tolerance
  in one predicate, so it is magnitude-safe (a flat absolute epsilon is blind to the value's
  scale) AND value≈0-safe (a purely relative tolerance collapses to 0 at 0). The tolerance source
  is the record's own ``control.min_step`` when the IOC provides one (> 0); otherwise a configured
  epsilon feeds both ``isclose`` axes.
* **The written value arrives as a string** (``set_pv_value(value: str)``) while the readback is
  typed (e.g. float ``81.0`` for ``"81"``). A numeric readback compares after coercing the string
  to float; a string that cannot be coerced to a numeric readback is *not verifiable* (never a
  false mismatch). A non-numeric readback (a string PV) compares exactly.
* **An enum readback is compared by INDEX, because the index is what the write path resolves to.**
  ``services.epics_client._extract_value`` keeps the numeric index in ``value`` and the labels in a
  separate ``enum`` block, so a written LABEL reaches neither compare above: ``float("On")`` raises,
  and the exact compare never sees a string. Both a landed and a not-landed write came back "not
  verifiable", which is the same answer for opposite facts. A written LABEL is therefore resolved
  against the record's own ``choices`` the way ``p4p.nt.NTEnum.assign`` resolves it on the put:
  case-sensitive, first match wins. Mirroring the write path is the point; resolving differently
  here would verify something the IOC never saw. The mirror is deliberately partial and stops at
  the label branch: ``assign`` falls back to ``int(py, 0)``, this module to ``float(written)``, so
  a radix-prefixed index (``"0x1"``) lands at the IOC and is still reported as not verifiable,
  exactly as before this stage existed. Closing that would mean mirroring a second p4p branch for
  a spelling nobody was measured to use.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict

# The key ``services.epics_client._format_value`` sets alongside ``value=None`` when p4p value
# extraction failed. That None is explicitly declared NOT to be a live reading.
_NOTE_KEY = "note"


class ReadbackVerification(BaseModel):
    """The verdict of comparing a readback against the written value (the intermediate result)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: True = readback matches within tolerance; False = a genuine mismatch; None = not verifiable
    #: (readback missing/None, not a measurement, so never a mismatch).
    verified: bool | None
    #: The value read back after the write; None when it could not be obtained.
    readback: object | None = None
    #: The absolute tolerance actually applied to a numeric compare; None for an exact
    #: (non-numeric) compare or when not verifiable.
    tolerance: float | None = None
    #: Human/agent-facing reason, why a compare was not verifiable, or how it mismatched.
    note: str | None = None


class WriteResult(BaseModel):
    """Structured result of a PV write (O3); serialised to the tool's dict via ``model_dump``.

    Keeps the pre-O3 keys (``status`` / ``pv_name`` / ``old_value`` / ``new_value``) so existing
    callers are unaffected, and adds the readback verdict. ``status`` stays ``"success"`` whenever
    the put itself executed and was ALLOW-audited, even on a readback mismatch: the write DID
    happen (a wrong value may now sit at the IOC), which is a fact distinct from "verified". The
    loud signal for a mismatch is ``verified=false`` plus the ``READBACK_MISMATCH`` audit event,
    NOT an exception (which would discard this structured old→new→readback triple).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str
    pv_name: str
    old_value: object | None
    new_value: str
    #: The value read back after the write; None when the readback could not be obtained.
    readback: object | None = None
    #: True matched within tolerance / False mismatch / None not verifiable (see
    #: :class:`ReadbackVerification`).
    verified: bool | None = None
    #: The absolute tolerance applied to a numeric compare (None = exact or not verifiable).
    tolerance: float | None = None
    #: Reason for a not-verifiable or mismatched readback.
    note: str | None = None
    #: O2 value-bounds note: None when the value was checked in-range, else the honest reason it
    #: was not bounds-checked (the record declares no drive limits, a deliberate fail-open).
    bounds_note: str | None = None


def _control_min_step(readback: Mapping[str, object]) -> float | None:
    """The record's ``control.min_step`` if the readback carries a usable numeric one, else None.

    ``min_step`` is the IOC's own drive resolution; a difference smaller than it is noise. Many
    records leave it unset (surfaced as ``0.0``), which is handled by the caller's epsilon fallback.
    """
    control = readback.get("control")
    if isinstance(control, dict):
        min_step = control.get("min_step")
        # bool is an int subclass; a boolean min_step is meaningless, exclude it explicitly.
        if isinstance(min_step, (int, float)) and not isinstance(min_step, bool):
            return float(min_step)
    return None


def _enum_labels(readback: Mapping[str, object]) -> list[str] | None:
    """The record's enum labels if the readback carries a usable ``enum`` block, else None.

    Type-guarded like its siblings (:func:`_control_min_step`, ``bounds._finite_limit``) because
    :func:`verify_readback` promises never to raise and its caller (``tools.write``) invokes it
    bare, AFTER the ALLOW audit: an exception there would tear the terminal ``READBACK_*`` line out
    of a completed write's audit trail.

    Neither shape check is cosmetic, and each fails a different way (both measured). A ``choices``
    that is a bare string is iterated CHARACTER by character, so a one-character label would resolve
    against a record description that does not exist. And dropping a non-string entry instead of
    refusing the whole block would SHIFT every label after it by one position, turning a landed
    write into a reported mismatch. Both cases withhold rather than guess.
    """
    enum = readback.get("enum")
    if not isinstance(enum, Mapping):
        return None
    choices = enum.get("choices")
    if not isinstance(choices, list):
        return None
    labels = [choice for choice in choices if isinstance(choice, str)]
    # A partially non-string choices list is not a record description we can resolve against;
    # withholding beats guessing which entries to trust.
    return labels if len(labels) == len(choices) else None


def _verify_enum_label(written: str, index: int, labels: list[str]) -> ReadbackVerification | None:
    """Verdict for a written enum LABEL, or None when *written* is not one of *labels*.

    Returning None (rather than a verdict) is what keeps this additive: an index spelling such as
    ``"1"`` is not a label, falls through, and is still answered by the numeric track exactly as
    before.

    The comparison is against the resolved INDEX, not against ``enum["label"]``: an index outside
    the record's choices really does land (measured over PVA), and the readback then carries
    ``label=None``, where a label-to-label compare would report "not verifiable" for what is a
    genuine mismatch. Whether the index is read from ``value`` or from ``enum["index"]`` is not
    observable, because ``_extract_value`` sets both from one local; no test can pin that choice
    without hand-building a shape the client never emits, so it is stated here instead.
    """
    if written not in labels:
        return None
    expected = labels.index(written)  # first match wins, mirroring NTEnum.assign
    matched = expected == index
    actual = labels[index] if 0 <= index < len(labels) else None
    # The note carries the label in BOTH directions, unlike the other tracks, which stay silent on
    # a match. Here it is the only place the two alphabets meet: the result and the audit line pair
    # a written label with a numeric readback ("written='On' readback=1"), and nothing else in
    # either says that 1 IS 'On'.
    return ReadbackVerification(
        verified=matched,
        readback=index,
        note=(
            f"written label {written!r} resolves to index {expected} (exact enum compare)"
            if matched
            else f"readback {actual!r} (index {index}) != written {written!r} (index {expected})"
        ),
    )


def verify_readback(
    written: str, readback: Mapping[str, object], tolerance: float
) -> ReadbackVerification:
    """Compare a readback dict against the written value. Pure and total (never raises).

    Args:
        written: the raw string the write tool received (``set_pv_value(value: str)``).
        readback: what a readback ``pv_get`` returned (``services.epics_client._format_value``
            shape, always a ``value`` key, an optional ``control.min_step``, and on extraction
            failure a ``note`` with ``value=None``).
        tolerance: the configured epsilon fallback; feeds BOTH ``isclose`` axes when the record
            carries no usable ``min_step`` (> 0).
    """
    value = readback.get("value")

    # Not a reading: the extraction-failure fallback (value None + note) or an absent value.
    if value is None:
        note = readback.get(_NOTE_KEY)
        reason = str(note) if note else "readback value was None (not a live reading)"
        return ReadbackVerification(verified=None, readback=None, note=f"not verifiable: {reason}")

    # Enum readback, BEFORE the numeric track: an enum value IS an int index, and every branch of
    # that track returns, so a stage placed after it would be unreachable for exactly the records
    # this one exists for. The int guard IS load-bearing: a float index would raise on the label
    # lookup. The bool exclusion beside it is deliberate rather than load-bearing, and no test pins
    # it, because it changes no answer: a bool readback carries no enum block, and forced together
    # the two paths agree anyway (True == 1).
    if isinstance(value, int) and not isinstance(value, bool):
        labels = _enum_labels(readback)
        if labels is not None:
            verdict = _verify_enum_label(written, value, labels)
            if verdict is not None:
                return verdict

    # Numeric readback (bool is an int subclass → compared by value: True==1.0, False==0.0).
    if isinstance(value, (int, float)):
        try:
            written_num = float(written)
        except (TypeError, ValueError):
            return ReadbackVerification(
                verified=None,
                readback=value,
                note=f"not verifiable: written {written!r} is not numeric like readback {value!r}",
            )
        # Tolerance: the IOC's own resolution when it provides one (min_step > 0), else the
        # configured epsilon on BOTH isclose axes (relative → magnitude-safe, absolute → 0-safe).
        min_step = _control_min_step(readback)
        if min_step is not None and min_step > 0.0:
            abs_tol, rel_tol = min_step, 0.0
        else:
            abs_tol, rel_tol = tolerance, tolerance
        matched = math.isclose(float(value), written_num, rel_tol=rel_tol, abs_tol=abs_tol)
        return ReadbackVerification(
            verified=matched,
            readback=value,
            tolerance=abs_tol,
            note=(
                None
                if matched
                else f"readback {value!r} != written {written_num!r} (abs_tol={abs_tol}, "
                f"rel_tol={rel_tol})"
            ),
        )

    # Non-numeric readback (a string PV): exact compare, no tolerance. An enum LABEL never arrives
    # here, an enum readback carries an int index and is answered by the enum stage above.
    matched = str(value) == written
    return ReadbackVerification(
        verified=matched,
        readback=value,
        note=None if matched else f"readback {value!r} != written {written!r} (exact)",
    )
