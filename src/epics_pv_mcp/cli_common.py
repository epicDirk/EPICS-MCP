"""Shared low-level plumbing for the ``epics-*`` command-line entry points.

Currently just console-encoding setup, the home for any future cross-CLI helper that must not live
inside a single command module (and be copy-pasted into the others).
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import math
import sys

#: Exit code for a usage error, matching the ``positive_timeout`` posture below and argparse's own.
_USAGE_ERROR = 2


def require_display_engine(command: str) -> int | None:
    """Return an exit code when the ``opi_navigation`` engine is absent, ``None`` when it is there.

    WHY this exists at all: two of the five console scripts (``epics-crossplane``,
    ``epics-coverage``) are display-aware and need an engine that is NOT part of the published
    package. Installed from an index, both used to die on the module-level import chain with a bare
    ``ModuleNotFoundError`` traceback, which reads as a broken package rather than as a missing
    optional capability. Measured by installing the built wheel into a clean environment: three of
    the five entry points worked, two produced a traceback.

    ``server.py`` already had the right posture for the MCP surface (register the display tools only
    when the engine imports, keep the core server up). This is the same decision for the CLI
    surface, which had never been given it.

    Returns rather than raising, and the caller returns it in turn, so the exit code stays visible
    at the entry point instead of being decided inside a helper. A missing optional capability is a
    USAGE error (2), not a failure of the command (1): nothing went wrong, the command simply
    cannot apply here.
    """
    # find_spec normally answers None for a missing module, but it PROPAGATES whatever a meta-path
    # finder raises, and a broken or restricted import system is exactly the situation where this
    # check matters most. An availability probe that crashes instead of answering "unavailable" is
    # the defect it was meant to prevent, so anything raised here counts as absent.
    try:
        found = importlib.util.find_spec("opi_navigation") is not None
    except Exception:  # noqa: BLE001 (any finder failure means "cannot use it", never a crash)
        found = False
    if found:
        return None
    sys.stderr.write(
        f"{command}: needs the opi_navigation display engine, which is not installed.\n"
        "\n"
        "This command joins operator-display PVs with the runtime planes, so it cannot\n"
        "run without that engine. The engine is not part of the published package (it\n"
        "lives in a separate, private repository), so an install from a package index\n"
        "will not have it. The other three commands (epics-mcp, epics-doctor,\n"
        "epics-diagnose) and every non-display MCP tool work without it.\n"
        "\n"
        "From a checkout with access: uv sync --extra all --group displays\n"
    )
    return _USAGE_ERROR


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
