"""Shared low-level plumbing for the ``epics-*`` command-line entry points.

Currently just console-encoding setup, the home for any future cross-CLI helper that must not live
inside a single command module (and be copy-pasted into the others).
"""

from __future__ import annotations

import argparse
import contextlib
import io
import math
import sys


def positive_timeout(value: str) -> float:
    """An ``argparse`` ``type=`` for a ``--timeout``: a strictly-positive, FINITE number of seconds.

    WHY: a bare ``type=float`` accepts ``-1`` (or ``0``), which then flows into a live probe as a
    non-positive timeout and can make a HEALTHY service look ``unreachable``. The doctor/diagnose
    CLIs exist to be honest, so a nonsense timeout must surface as a **usage error** (exit 2), not
    as a misdiagnosis. The ``seconds > 0 and isfinite`` gate rejects ``0``/negatives, ``nan``
    (``nan > 0`` is False) AND ``inf`` (``inf`` parses and is > 0, but "wait forever" defeats the
    very timeout the flag sets); a genuinely large *finite* timeout is left untouched.
    """
    try:
        seconds = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a number: {value!r}") from exc
    if not (seconds > 0 and math.isfinite(seconds)):
        raise argparse.ArgumentTypeError(f"must be a finite number > 0 seconds, got {value!r}")
    return seconds


def configure_stdout() -> None:
    """Force UTF-8 on ``sys.stdout`` so a report's Unicode does not crash a legacy console.

    WHY: the CLI reports carry Unicode, emoji (``✓``/``✗``), en-dashes, the ``⇒`` arrow. A Windows
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
