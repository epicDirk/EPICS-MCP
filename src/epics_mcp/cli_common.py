"""Shared low-level plumbing for the ``epics-*`` command-line entry points.

Currently just console-encoding setup, the home for any future cross-CLI helper that must not live
inside a single command module (and be copy-pasted into the others).
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import importlib.util
import io
import math
import sys

#: Exit code for a usage error, matching the ``positive_timeout`` posture below and argparse's own.
_USAGE_ERROR = 2

#: Exit code for a command that FAILED. Distinct from ``_USAGE_ERROR`` on purpose: an engine that is
#: absent is a capability that does not apply here (2), an engine that is present and does not
#: import is something broken (1). QA-14.
_COMMAND_FAILED = 1


def _report_engine_absent(command: str) -> int:
    """The optional capability is not installed: say what still works and how to get the rest."""
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


def _report_engine_broken(command: str, exc: BaseException) -> int:
    """The engine IS there and does not load: name the failure instead of misreporting it.

    The advice attached to the absent case (install the engine) is worse than useless here, because
    the engine is installed; what the reader needs is the exception that stopped it.
    """
    sys.stderr.write(
        f"{command}: the opi_navigation display engine is installed but failed to load.\n"
        "\n"
        f"  {type(exc).__name__}: {exc}\n"
        "\n"
        "This is not a missing optional capability, so installing the engine will not\n"
        "help. Most often a broken or partially-installed environment: re-sync it with\n"
        "uv sync --extra all --group displays, and if that does not clear it, the\n"
        "message above is the failure to report.\n"
    )
    return _COMMAND_FAILED


def require_display_engine(command: str) -> int | None:
    """Return an exit code when the ``opi_navigation`` engine is unusable, else ``None``.

    WHY this exists at all: two of the five console scripts (``epics-crossplane``,
    ``epics-coverage``) are display-aware and need an engine that is NOT part of the published
    package. Installed from an index, both used to die on the module-level import chain with a bare
    ``ModuleNotFoundError`` traceback, which reads as a broken package rather than as a missing
    optional capability. Measured by installing the built wheel into a clean environment: three of
    the five entry points worked, two produced a traceback.

    ``server.py`` already had the right posture for the MCP surface (register the display tools only
    when the engine imports, keep the core server up, and degrade LOUD when an installed engine
    fails to import). This is the same decision for the CLI surface, which had never been given it.

    THREE outcomes, not two (QA-14). The probe used to be ``find_spec`` alone, so an engine that was
    present but did not import passed the check and then died in the CALLER with a bare traceback,
    the very thing this function exists to prevent; and any finder failure was reported as "not
    installed" with advice that could not help. Now:

    * absent (``find_spec`` answers None, or raises ``ModuleNotFoundError``): exit 2, a USAGE error.
      Nothing went wrong, the command simply cannot apply here.
    * present but not importable, or a finder that fails in any OTHER way: exit 1, the command
      failed, with the exception named.
    * usable: ``None``.

    Honest limit: the import probe loads the TOP package. A command reaching into a submodule that
    alone is broken still meets that failure in the caller. Importing every submodule each CLI
    happens to need would put this helper's knowledge of them here, which is worse; the common
    broken-install case (a missing transitive dependency, a package half-written to disk) does
    surface in the top-level import, and that is what this covers.

    Returns rather than raising, and the caller returns it in turn, so the exit code stays visible
    at the entry point instead of being decided inside a helper.
    """
    # find_spec normally answers None for a missing module, but it PROPAGATES whatever a meta-path
    # finder raises. A ModuleNotFoundError from a finder still means the module is not there (a
    # restricted import system is the measured case, tests/test_cli_without_display_engine.py pins
    # it), so it stays the absent path; anything else is a finder that could not answer, which is
    # not the same claim and must not wear the same message.
    try:
        found = importlib.util.find_spec("opi_navigation") is not None
    except ModuleNotFoundError:
        return _report_engine_absent(command)
    except Exception as exc:  # noqa: BLE001 (never a crash: report it, do not propagate)
        return _report_engine_broken(command, exc)
    if not found:
        return _report_engine_absent(command)
    # Found is not the same as usable, and the difference used to surface as a traceback in the
    # caller. The import is cached, so doing it here costs the caller nothing.
    try:
        importlib.import_module("opi_navigation")
    except Exception as exc:  # noqa: BLE001 (an installed-but-broken engine is a reportable failure)
        return _report_engine_broken(command, exc)
    return None


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
