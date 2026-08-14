"""One audit record is one line, and this module is what makes that true.

Both write gates append to the same kind of durable, line-oriented trail, and both build their
records out of values a CALLER chose: a PV name for the PV gate, logbook, level and reply-target
names for the Olog gate. A record separator inside such a value is therefore not a formatting
curiosity, it is a way to write records nobody emitted.

Measured against the shipped 0.6.0 artefact, on a server with the PV write gate OFF, so with a
caller who is permitted to write nothing at all: a ``pv_name`` carrying a newline plus a
well-formed second line was refused with ``PVWriteDeniedError`` before any network access, and left
THREE lines in the audit file, the middle one a complete, timestamp-bearing ``event=ALLOW`` record
naming a different PV. Nothing distinguishes it from a genuine one. The ``%r`` fields were never
the hole (``repr`` escapes a newline); the ``%s`` identifier fields were.

WHAT COUNTS AS A LINE BREAK IS DECIDED BY THE READER, not by the writer, and the first version of
this module got that wrong: it escaped C0 plus DEL, which is what ends a line for a byte-oriented
reader such as ``grep``, and stopped there. An adversarial review found the gap the same day.
``str.splitlines()`` also breaks on U+0085 (NEL, itself a control character, category ``Cc``),
U+2028 (LINE SEPARATOR) and U+2029 (PARAGRAPH SEPARATOR), and ``splitlines`` is not a hypothetical
reader here: it is the method this repository's own guards use to count records, and any Python
log-analysis tool that reads the file as text and splits it is in the same position. The escape
therefore covers the WIDEST definition in reach, C0 plus C1 plus DEL plus the two Unicode
separators, so that no reader in either family sees two records where one gate spoke once.

Nothing legitimate is lost by the widening: an audit field is an identifier, an error code or a
``repr``-formatted scalar, and the non-ASCII characters a real record does carry (a micro sign, an
ohm sign, an accented name) all sit above U+009F.

WHERE the escaping happens is the second decision, and it is deliberate: in a
:class:`OneRecordFormatter` attached to the audit handler, i.e. INSIDE ``Handler.emit``, rather
than at the gate's ``_emit``. Doing it at the gate meant rendering ``message % args`` there, which
took the rendering out of the logging layer's own error handling: a value whose ``__repr__`` raises
would then propagate out of the audit sink and replace the very refusal or failure the record was
about, and ``docs/write-gate-contract.md`` promises the opposite in as many words. Inside the
formatter, ``logging`` absorbs it through ``Handler.handleError`` exactly as it did before, and the
gates keep passing their arguments lazily.

It ESCAPES rather than strips, because an audit that quietly rewrites what a caller sent is a
different defect: the record still says exactly what arrived, in a form that cannot end it early.
"""

from __future__ import annotations

import logging
import re

#: Everything a caller-supplied field could carry that some reader treats as the end of a line:
#: the C0 block and DEL (a byte-oriented reader), the C1 block, which contains U+0085 NEL, plus
#: U+2028 and U+2029 (``str.splitlines`` and any Unicode-aware line reader). C1 is escaped whole
#: rather than U+0085 alone: the rest of that block is unprintable control codes that no audit
#: field has any business carrying, and a range is one fewer thing to get wrong later.
#: A space is deliberately NOT escaped: it separates the fields of a record, so escaping it would
#: rewrite every legitimate line. What that leaves open is written down in ``docs/known-limits.md``.
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")


def _escaped(match: re.Match[str]) -> str:
    """One character as a printable escape, in the shortest form that stays unambiguous."""
    code = ord(match.group())
    return f"\\x{code:02x}" if code <= 0xFF else f"\\u{code:04x}"


def as_one_record(record: str) -> str:
    """*record* with every line-breaking character escaped, so it occupies exactly one line.

    Idempotent on any record that has none, which is every legitimate one: the audit's fields are
    identifiers, error codes and ``repr``-formatted scalars.
    """
    return _CONTROL.sub(_escaped, record)


class OneRecordFormatter(logging.Formatter):
    """The audit formatter: whatever it renders, it renders on one line.

    A formatter rather than a wrapper at the call site, so the guarantee holds for every record the
    audit logger ever carries, including one emitted by a future caller that never heard of this
    module, and so that the rendering stays inside ``Handler.emit`` where ``logging`` absorbs its
    own errors (see the module docstring).
    """

    def format(self, record: logging.LogRecord) -> str:
        return as_one_record(super().format(record))
