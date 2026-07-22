"""Shared low-level plumbing for the ``epics-*`` command-line entry points.

Currently just console-encoding setup — the home for any future cross-CLI helper that must not live
inside a single command module (and be copy-pasted into the others).
"""

from __future__ import annotations

import contextlib
import io
import sys


def configure_stdout() -> None:
    """Force UTF-8 on ``sys.stdout`` so a report's Unicode does not crash a legacy console.

    WHY: the CLI reports carry Unicode — emoji (``✓``/``✗``), en-dashes, the ``⇒`` arrow. A Windows
    console defaults to cp1252, on which a bare ``print`` of those characters raises
    ``UnicodeEncodeError`` and aborts the command. Reconfiguring the stream to UTF-8 fixes it at the
    source; it is a no-op on a stream already UTF-8 (a POSIX terminal), so it is safe to call
    unconditionally at every CLI entry point.

    ``reconfigure`` exists only on :class:`io.TextIOWrapper`; under pytest capture or a redirected
    pipe ``sys.stdout`` is a different object without it, so we narrow by type first (rather than
    catching ``AttributeError``) and suppress only the ``ValueError`` a detached buffer would raise.
    """
    stream = sys.stdout
    if isinstance(stream, io.TextIOWrapper):
        with contextlib.suppress(ValueError):
            stream.reconfigure(encoding="utf-8")
