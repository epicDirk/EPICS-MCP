"""CLI that emits a ready-to-paste client configuration (``epics-init``).

The onboarding half of ``epics-doctor``. The doctor answers "is my configuration right?"; this
answers the question that comes before it, "what should my configuration say?", by printing the
``.mcp.json`` block for one of four named deployment shapes (see :mod:`epics_mcp.presets`).

NOT interactive, and that is a decision rather than an omission. An interview would be the only
part of this package that cannot be tested deterministically, and it would buy little: the thing a
newcomer needs is the set of variables their shape requires, which a preset states outright. What
an interview WOULD have added, a probe of the result, is here anyway and needs no TTY: unless
``--no-check`` says otherwise, the emitted configuration is put into this process and handed to
``epics-doctor``. That closes the loop an interview-only tool leaves open, where a configuration is
generated and nobody checks it.

Reads nothing from disk and writes nothing to it: the block goes to stdout, so redirecting it is
the caller's choice and their existing file is never silently overwritten. Anything the command has
to SAY (the doctor's report, warnings about unfilled placeholders) goes to stderr, which keeps
``epics-init --preset X > .mcp.json`` correct.

Exit code:

* ``0``: the block was emitted, and either no check ran or it found nothing wrong;
* ``1``: the check ran and a configured plane HARD-failed;
* ``2``: a usage error (unknown preset, malformed ``--set``, bad arguments);
* ``3``: the check ran and a plane is reachable but its identity probe FAILED.

Codes 0/1/3 are the doctor's own, passed through unchanged, because "did my setup work?" is the
scriptable question and answering it twice with different numbers would help nobody. Code ``2``
means the same thing on both levels, "you asked for something impossible", so the overlap with the
doctor's usage code is an agreement rather than a collision.

Usage::

    epics-init --list
    epics-init --preset sandbox
    epics-init --preset sandbox --probe-pv SIM:PS-01:Cur-RB
    epics-init --preset ioc-archiver --set EPICS_MCP_ARCHIVER_URL=http://arch:17665
    epics-init --preset full --no-check > .mcp.json
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
from collections.abc import Mapping

from epics_mcp import cli_doctor
from epics_mcp.cli_common import add_version_argument, configure_stdout
from epics_mcp.presets import (
    PRESETS,
    configures_a_rest_plane,
    format_listing,
    open_placeholders,
    render_client_config,
    stale_config_vars,
    with_overrides,
)


def name_value_pair(value: str) -> tuple[str, str]:
    """An ``argparse`` ``type=`` for ``--set VAR=VALUE``.

    Splits on the FIRST ``=`` only, because a value may legitimately contain one (a URL with a
    query string, a regex allowlist). An empty name is rejected; an empty VALUE is not, because
    clearing a variable a preset sets is a real intent ("I have no retrieval webapp") and the
    alternative would be to make the user edit the emitted block by hand, which is the chore this
    command exists to remove.
    """
    name, separator, setting = value.partition("=")
    if not separator or not name.strip():
        raise argparse.ArgumentTypeError(f"expected NAME=VALUE, got {value!r}")
    return name.strip(), setting


def _warn_about_placeholders(pending: list[str]) -> None:
    """Tell the reader, on stderr, which entries they still have to fill in.

    Named individually rather than counted: "3 placeholders remain" sends the reader back to scan
    the block, which is exactly the work this command is supposed to do for them.
    """
    sys.stderr.write(
        f"epics-init: {len(pending)} value(s) are still placeholders and have to be replaced "
        "before this configuration can work:\n"
    )
    for entry in pending:
        sys.stderr.write(f"  {entry}\n")
    sys.stderr.write(
        "Replace them in the emitted block, or pass them here, "
        "e.g. --set EPICS_MCP_ARCHIVER_URL=http://archiver.example.org:17665\n"
    )


def _run_check(env: Mapping[str, str], probe_pv: str | None) -> int:
    """Put *env* into this process and run ``epics-doctor`` against it. Return its exit code.

    THE ORDER IS THE POINT. The doctor reads ``os.environ``, so the caller's own variables are
    cleared first (see ``presets.stale_config_vars``) and the preset applied on top; otherwise the
    report would describe a blend of the preset and whatever the shell already exported. This
    matters most where it is least visible: an operator whose shell points at a production
    facility would get a report about that facility while reading a block that says localhost.

    The environment is mutated rather than passed, because there is nothing to pass it to: the
    config is a process singleton built on first access, and the doctor takes no config argument.
    Mutating is safe HERE because this is a one-shot command that exits afterwards, which is also
    why the doctor is called in-process rather than spawned: one implementation of the check, no
    second copy of its exit-code mapping.

    Its report goes to stderr, not stdout, so redirecting the emitted block stays lossless.
    """
    for name in stale_config_vars(os.environ):
        del os.environ[name]
    os.environ.update(env)

    doctor_argv = ["--probe-pv", probe_pv] if probe_pv else []
    # The doctor writes its report to stdout by design (it is that command's payload). Here the
    # payload is the config block, so the report is redirected to keep the stream contract.
    with contextlib.redirect_stdout(sys.stderr):
        return cli_doctor.main(doctor_argv)


def _warn_that_nothing_was_verified(preset_name: str) -> None:
    """Say that a clean report confirmed nothing, when that is what happened.

    The doctor's live plane reports its posture WITHOUT a network call unless it is given a PV to
    read, so a preset that enables no REST plane produces a clean run in which nothing was probed.
    The doctor is honest about this in its own verdict line, but the reader arrives here expecting
    a setup to have been confirmed, and "OK" plus an exit code of 0 is easy to read as exactly
    that. Naming it costs one line and prevents a false all-clear.
    """
    sys.stderr.write(
        f"epics-init: the {preset_name} preset enables no REST plane, and without --probe-pv the "
        "live plane is only reported, never contacted. So the check above confirmed that nothing "
        "is misconfigured, NOT that anything works. Re-run with --probe-pv NAME to actually reach "
        "a PV.\n"
    )


def main(argv: list[str] | None = None) -> int:
    """Emit the client-configuration block for one preset, then optionally probe it."""
    # Before the parser (QA-8): argparse prints ``--help`` inside ``parse_args``, so a non-ASCII
    # character in any help text would die on a cp1252 console if the reconfigure came later.
    configure_stdout()
    parser = argparse.ArgumentParser(
        # prog pinned: argparse's default is interpreter dependent and prints an absolute console
        # script path on 3.14 (QA-41). Same reason at every entry point of this package.
        prog="epics-init",
        description=(
            "Emit a ready-to-paste MCP client configuration for one of four deployment shapes. "
            "Run epics-doctor afterwards to confirm it."
        ),
    )
    add_version_argument(parser)
    # Exactly one of the two is required, so the no-argument case prints usage instead of guessing
    # a default preset. There is no obviously right default here: 'sandbox' would silently hand a
    # facility operator a loopback config, and 'full' would hand a workshop attendee six services
    # they do not have.
    what = parser.add_mutually_exclusive_group(required=True)
    what.add_argument(
        "--preset",
        choices=sorted(PRESETS),
        help="the deployment shape to emit (see --list)",
    )
    what.add_argument(
        "--list",
        action="store_true",
        help="describe the available presets and exit",
    )
    parser.add_argument(
        "--set",
        dest="overrides",
        action="append",
        type=name_value_pair,
        default=[],
        metavar="NAME=VALUE",
        help="override or add one variable; repeatable",
    )
    parser.add_argument(
        "--probe-pv",
        default=None,
        metavar="NAME",
        help="also read this PV during the check (without it the live plane is only reported)",
    )
    parser.add_argument(
        "--no-check",
        action="store_true",
        help="only emit the block, do not run epics-doctor against it",
    )
    args = parser.parse_args(argv)

    if args.list:
        sys.stdout.write(format_listing(PRESETS.values()) + "\n")
        return 0

    env = with_overrides(PRESETS[args.preset].env, dict(args.overrides))
    sys.stdout.write(render_client_config(env) + "\n")

    pending = open_placeholders(env)
    if pending:
        # Refuse the check rather than run it: probing ``<archiver-host>`` yields a DNS failure
        # shaped exactly like a real finding, and the reader cannot tell "your archiver is down"
        # from "you have not filled this in yet". A named refusal is worth more than a report
        # that has to be discounted.
        _warn_about_placeholders(pending)
        return 0
    if args.no_check:
        return 0

    exit_code = _run_check(env, args.probe_pv)
    if not args.probe_pv and not configures_a_rest_plane(env):
        _warn_that_nothing_was_verified(args.preset)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
