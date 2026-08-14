"""One audit record is one line, and this module is what makes that true.

Both write gates append to the same kind of durable, line-oriented trail, and both build their
records out of values a CALLER chose: a PV name for the PV gate, logbook and level names for the
Olog gate. A record separator inside such a value is therefore not a formatting curiosity, it is a
way to write records nobody emitted.

Measured against the shipped 0.6.0 artefact, on a server with the PV write gate OFF, so with a
caller who is permitted to write nothing at all: a ``pv_name`` carrying a newline plus a
well-formed second line was refused with ``PVWriteDeniedError`` before any network access, and left
THREE lines in the audit file, the middle one a complete, timestamp-bearing ``event=ALLOW`` record
naming a different PV. Nothing distinguishes it from a genuine one. The ``%r`` fields were never
the hole (``repr`` escapes a newline); the ``%s`` identifier fields were.

The check is on the RENDERED record and needs no knowledge of which field was the hole, which is
the property that matters: a gate that grows an eighth emitter, or a field somebody later decides
to interpolate, is covered without anyone remembering this file. It ESCAPES rather than strips,
because an audit that quietly rewrites what a caller sent is a different defect: the record still
says exactly what arrived, in a form that cannot end it early.
"""

from __future__ import annotations

import re

#: Every C0 control character plus DEL. ``\n`` and ``\r`` are the two that end a record; the rest
#: are here because a log is also READ, and an escape sequence in a terminal viewer is a second way
#: for caller-chosen bytes to change what a reader sees. A space is deliberately NOT escaped: it
#: separates the fields of a record, so escaping it would rewrite every legitimate line.
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def as_one_record(record: str) -> str:
    """*record* with every control character escaped, so it occupies exactly one line.

    Idempotent on any record that has none, which is every legitimate one: the audit's fields are
    identifiers, error codes and ``repr``-formatted scalars.
    """
    return _CONTROL.sub(lambda match: f"\\x{ord(match.group()):02x}", record)
