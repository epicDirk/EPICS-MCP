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
from typing import NoReturn

from epics_mcp import __version__

#: Exit code for a usage error, matching the ``positive_timeout`` posture below and argparse's own.
_USAGE_ERROR = 2

#: Exit code for a command that FAILED. Distinct from ``_USAGE_ERROR`` on purpose: an engine that is
#: absent is a capability that does not apply here (2), an engine that is present and does not
#: import is something broken (1). QA-14.
#:
#: Since QA-42 this code also answers a USAGE error on the two display-aware commands whenever the
#: engine is installed and will not load, because :class:`DisplayEngineAwareParser` lets the engine
#: decide. That is deliberate: it is the same code the same command returns on the same install with
#: correct arguments, so one cause keeps one code. A reader who typed a flag wrong on a broken
#: install is told about the engine rather than the flag, which is the problem they can act on.
_COMMAND_FAILED = 1

#: The engine module whose import decides "usable", NOT the top package. The top package is a
#: docstring with no imports, so importing it proves only that a directory of that name exists;
#: measured, an engine with a missing transitive dependency passed a top-package probe and then
#: failed in the caller. This is the module ``services/inventory_adapter.py`` imports, and that
#: file is the only importer of the engine, so one name is enough to make the probe mean something.
_ENGINE_ENTRY_POINT = "opi_navigation.pv_analysis"


def _report_engine_absent(command: str) -> int:
    """The optional capability is not installed: say what still works and how to get the rest."""
    sys.stderr.write(
        f"{command}: needs the opi_navigation display engine, which is not installed.\n"
        "\n"
        "This command joins operator-display PVs with the runtime planes, so it cannot\n"
        "run without that engine. The engine is not part of the published package (it\n"
        "lives in a separate, private repository), so an install from a package index\n"
        "will not have it. The other five commands (epics-mcp, epics-init,\n"
        "epics-testpv, epics-doctor, epics-diagnose) and every non-display MCP\n"
        "tool work without it.\n"
        "\n"
        "From a checkout with access: uv sync --extra dev --group displays\n"
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
        "uv sync --extra dev --group displays, and if that does not clear it, the\n"
        "message above is the failure to report.\n"
    )
    return _COMMAND_FAILED


def require_display_engine(command: str) -> int | None:
    """Return an exit code when the ``opi_navigation`` engine is unusable, else ``None``.

    WHY this exists at all: two of the console scripts (``epics-crossplane``,
    ``epics-coverage``) are display-aware and need an engine that is NOT part of the published
    package. Installed from an index, both used to die on the module-level import chain with a bare
    ``ModuleNotFoundError`` traceback, which reads as a broken package rather than as a missing
    optional capability. Measured by installing the built wheel into a clean environment, on a
    package that declared five console scripts AT THE TIME: three worked, two produced a traceback.
    The counts are dated on purpose. Seven are declared today, and an undated count in a docstring
    is how the refusal above came to name three of five for two releases.

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

    The probe imports :data:`_ENGINE_ENTRY_POINT`, not the bare top package, and that distinction
    is the whole value of the check. Measured: this engine's ``__init__.py`` is a docstring with no
    imports in it, so importing the top package executes nothing that a missing transitive
    dependency could break. A probe that stopped there answered "usable" for an engine whose
    submodule could not load, and the caller then met the bare ``ModuleNotFoundError`` this helper
    exists to prevent.

    Naming ONE submodule is affordable here because there is only one to name:
    ``services/inventory_adapter.py`` states, and is the only module that does it, that it is the
    sole importer of the engine, and it imports ``opi_navigation.pv_analysis``. So this does not
    accumulate a list of every submodule some CLI happens to need, which would indeed be worse.

    Honest limit that remains: a submodule OUTSIDE the probed entry point could still be broken
    alone, and that failure would surface in the caller. Closing that would mean importing the
    engine exhaustively, which is not what an availability check is for.

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
    # caller. The import is cached, so doing it here costs the caller nothing. It is the ENTRY
    # POINT that is imported, not the top package: the top package is a docstring and cannot fail.
    try:
        importlib.import_module(_ENGINE_ENTRY_POINT)
    except Exception as exc:  # noqa: BLE001 (an installed-but-broken engine is a reportable failure)
        return _report_engine_broken(command, exc)
    return None


class DisplayEngineAwareParser(argparse.ArgumentParser):
    """An ``ArgumentParser`` that answers a usage error with the ENGINE refusal where that is the
    real obstacle, used by the two display-aware commands.

    WHY (QA-42). Those two commands used to run :func:`require_display_engine` BEFORE building their
    parser, which made ``--help`` and ``--version`` unreachable on every install from a package
    index: the first thing a reader met was an instruction to install something they do not need in
    order to read a help text. Moving the check after ``parse_args`` fixes that, and it would have
    cost the other half of the same courtesy: on a core-only install ``epics-coverage`` with no
    arguments would have answered "the following arguments are required: --displays", advice that
    cannot be followed, because supplying the argument would not make the command run either.

    So the refusal is kept exactly where it still helps. ``argparse`` funnels EVERY usage error
    through :meth:`error` (measured against the stdlib in use: three call sites, and ``error`` is
    the only path to exit 2), while ``--help`` and ``--version`` print through ``parser.exit`` and
    never reach here. That asymmetry is the whole mechanism.

    The command name comes from ``self.prog`` rather than from a constructor argument: ``prog`` is
    pinned to the declared command name at every entry point of this package (QA-41) and
    ``tests/test_cli_version.py`` holds it there for every command ``[project.scripts]`` declares
    (its population is derived from that table, so it grows with it), and there is nothing a second
    parameter could say that this does not.

    THE EXIT CODE IS THE ENGINE'S, not argparse's 2: absent gives 2 (which agrees with argparse
    anyway) and installed-but-broken gives 1. One cause, one code; see :data:`_COMMAND_FAILED`.
    """

    def error(self, message: str) -> NoReturn:
        """Refuse with the engine's message when the engine is unusable, else argparse's own.

        ``require_display_engine`` WRITES its message itself and returns only a code, so nothing is
        printed here: doing both would hand the reader the eight-line refusal twice, and a
        containment-style assertion in a test could not see it.
        """
        code = require_display_engine(self.prog)
        if code is not None:
            self.exit(code)
        super().error(message)


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


def add_version_argument(parser: argparse.ArgumentParser) -> None:
    """Give *parser* the ``--version`` action every console script of this package shares.

    ONE home, so the console scripts cannot drift apart. The flag used to exist on ``epics-mcp``
    alone (the bug-report template asks reporters for that version) and the others had no way to
    answer the same question. Copying the line into each was the alternative, and then the version
    source, the ``%(prog)s`` prefix and the exit code would each be free to differ per command,
    which is what ``tests/test_cli_version.py`` now pins across the whole set. That test derives
    the set from ``[project.scripts]``, so a command added later joins it without anyone
    remembering to, and joins it RED until it calls this.

    ``%(prog)s`` resolves to the parser's OWN pinned ``prog``, so the line names the command the
    reader typed instead of an interpreter path (QA-41), which also means the outputs differ by
    that first token by design: what has to agree is the version after it.

    ``action="version"`` prints inside ``parse_args`` and raises ``SystemExit(0)``, so the console
    has to be reconfigured BEFORE the parser is built (QA-8), exactly as every entry point already
    does. It prints through ``parser.exit``, never through ``parser.error``, which is why it answers
    on every command in every environment since QA-42: on the two display-aware ones the engine
    check now runs AFTER parsing, and :class:`DisplayEngineAwareParser` intercepts only usage
    errors.
    """
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")


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
