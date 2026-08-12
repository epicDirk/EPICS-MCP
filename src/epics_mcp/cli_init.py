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

The block goes to stdout and everything the command has to SAY (the doctor's report, warnings about
unfilled placeholders) goes to stderr, so the two never mix. It reads nothing from disk, and it
writes only where ``--out`` tells it to; an existing file is never replaced without ``--force``.

``--out`` exists because a shell redirect cannot promise an encoding, and this block is JSON that a
client has to parse. Measured in Windows PowerShell 5.1: ``>`` writes UTF-16 with a byte-order mark,
and ``Set-Content -Encoding utf8`` and ``Out-File -Encoding utf8`` write UTF-8 with one;
a strict JSON parser rejects all three, Python and Node alike. POSIX shells and PowerShell 7 write
UTF-8 without a mark and are fine. So ``epics-init --preset X > .mcp.json`` is correct on some
shells and silently broken on the default Windows one, which is not a difference to leave to the
caller.

Exit code:

* ``0``: the block was emitted, and either no check ran or it found nothing wrong. ⚠️ With ``--out``
  this does NOT promise a file. While placeholders remain the block is printed and the file is
  deliberately not written, and the exit stays ``0`` because nothing FAILED: the configuration is
  simply not finished yet. A script that needs the file has to test for the file, not for the code;
* ``1``: the check ran and a configured plane HARD-failed, or the check itself broke internally.
  The doctor gives those two the same code deliberately and tells them apart on stderr;
* ``2``: a usage error (unknown preset, malformed ``--set``, bad arguments), or a request that
  cannot be carried out (``--out`` onto an existing file without ``--force``, a missing parent
  directory, ``--absolute-command`` with nothing to resolve). Both are the same answer, "you asked
  for something impossible", so they share the code;
* ``3``: the check ran and a plane is reachable but its identity probe FAILED.

Codes 0/1/3 are the doctor's own, passed through unchanged, because "did my setup work?" is the
scriptable question and answering it twice with different numbers would help nobody. Code ``2``
means the same thing on both levels, "you asked for something impossible", so the overlap with the
doctor's usage code is an agreement rather than a collision.

Usage::

    epics-init --list
    epics-init --preset sandbox
    epics-init --preset sandbox --probe-pv TEST:Temperature
    epics-init --preset ioc-archiver --set EPICS_MCP_ARCHIVER_URL=http://arch:17665
    epics-init --preset sandbox --out .mcp.json
    epics-init --preset sandbox --out .mcp.json --absolute-command --force
"""

from __future__ import annotations

import argparse
import contextlib
import os
import pathlib
import shutil
import sys
from collections.abc import Mapping

from epics_mcp import cli_doctor
from epics_mcp.cli_common import add_version_argument, configure_stdout
from epics_mcp.presets import (
    PRESETS,
    SERVER_COMMAND,
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


def _resolve_absolute_command() -> str | None:
    """The absolute path of the installed server command, or ``None`` when it cannot be found.

    For ``--absolute-command``. A client launched from a desktop icon or a service manager does not
    inherit an interactive shell's ``PATH``, so a block naming the bare command can be byte-correct
    and still fail, with the client reporting nothing beyond "did not start".

    ``shutil.which`` rather than a sibling of ``sys.executable``: the two disagree in exactly the
    case this option exists for. A tool installation puts the console scripts in a directory of its
    own, and asking the PATH answers "where is the command YOU can run", which is what the client
    then has to be told, precisely because the client's own PATH will not answer it. Returning
    ``None`` instead of falling back to the bare name is deliberate: a silent fallback would emit
    the very block the caller asked not to have.

    ``abspath`` on the result, because ``shutil.which`` joins each PATH entry as written and a PATH
    may legitimately hold a relative one (and on Windows the current directory can join the search).
    A relative answer would satisfy the letter of the lookup and defeat the option, since the client
    resolves it from a working directory nobody chose. Making the promise structural is cheaper than
    documenting the exception.
    """
    found = shutil.which(SERVER_COMMAND)
    return os.path.abspath(found) if found else None


def _write_block(path: str, block: str, *, force: bool) -> str | None:
    """Write *block* to *path* as UTF-8 with LF endings. Return the path, or ``None`` on refusal.

    Both properties are explicit because the shell's own redirect gets them wrong: measured in
    Windows PowerShell 5.1, ``>`` writes UTF-16 with a byte-order mark and ``Set-Content -Encoding
    utf8`` writes UTF-8 with one, and a strict JSON parser rejects all of those. Writing the file
    here is the only way to promise the caller an encoding.

    ⚠️ Of the two, only ``newline`` is red-provable, and the difference is worth knowing before
    trusting a mutation sweep here. ``render_client_config`` goes through ``json.dumps``, which
    escapes non-ASCII as ``\\uXXXX``, so the block is pure ASCII and dropping ``encoding`` changes
    no byte today; ``tests/test_cli_init.py`` pins that premise rather than claiming a proof it
    cannot give. Dropping ``newline`` does change bytes, on Windows, and is caught.

    Refusals, each on stderr and each a reason the caller cannot fix by retrying:

    * the parent directory does not exist, which is the common typo and would otherwise surface as
      a bare ``FileNotFoundError``;
    * the target exists, unless *force* was given. A client configuration usually holds OTHER
      servers, so overwriting one by default would destroy work that has nothing to do with us;
    * the target is a symlink, which ``"x"`` alone does not stop on Windows, where the exclusive
      create follows a DANGLING link and creates its target instead.

    A write that FAILS midway leaves nothing behind either, and the two cases need different care.
    Creating exclusively and unlinking on error covers the new-file case without a
    check-then-create window. Replacing an existing file (``--force``) is done through a sibling
    temporary file and an atomic rename, so a full disk cannot leave the caller with a truncated
    configuration where a working one used to be.

    Deliberately NOT routed through ``epics_mcp.paths``: those helpers consult ``get_config()``,
    which builds the process-wide config singleton, and this write happens BEFORE ``_run_check``
    strips the caller's environment. A singleton built here would freeze the pre-strip environment
    and make the check report on a configuration nobody asked about. The cost is stated rather than
    hidden: ``--out`` is therefore outside any ``EPICS_MCP_ALLOWED_ROOTS`` boundary, unlike the
    server's own file-writing tool.
    """
    target = pathlib.Path(path)
    parent = target.parent
    if not parent.is_dir():
        sys.stderr.write(
            f"epics-init: cannot write {path}: the directory {parent} does not exist. "
            "Create it first, or name a path inside an existing one.\n"
        )
        return None
    if target.is_symlink():
        sys.stderr.write(
            f"epics-init: cannot write {path}: it is a symbolic link, and following one would "
            "write somewhere you did not name. Remove the link or choose another path.\n"
        )
        return None

    # Replacing writes through a sibling and renames; creating writes exclusively, which needs no
    # check-then-create window. Either way, a failure partway leaves the caller's disk as it was.
    scratch = target.with_name(target.name + ".epics-init-tmp") if force else target
    try:
        with open(scratch, "w" if force else "x", encoding="utf-8", newline="\n") as handle:
            handle.write(block + "\n")
        if force:
            os.replace(scratch, target)
    except FileExistsError:
        sys.stderr.write(
            f"epics-init: {path} already exists, and it may hold other MCP servers. "
            "Write to a new file and merge the block by hand, or pass --force to replace it.\n"
        )
        return None
    except OSError as exc:
        with contextlib.suppress(OSError):
            scratch.unlink()
        sys.stderr.write(f"epics-init: cannot write {path}: {exc.strerror or exc}\n")
        return None
    return str(target)


def _warn_about_write_gate(env: Mapping[str, str]) -> None:
    """Say so when the composed configuration arms a write gate, and say what it costs at START.

    ``epics-doctor`` PRINTS the effective write posture of both gates (its "Write gates" block), so
    this is no longer the only mention an armed gate gets. It stays for the two things that block
    deliberately does not cover: with ``--no-check`` no doctor runs at all, and the doctor reports
    what the gates are SET to without evaluating whether a server would start on them. The refusal
    is the likelier outcome of the two, and a client shows it as nothing more than "server not
    connected".

    ⚠️ The start conditions are NOT the same for the two gates, and stating them as one was wrong:
    a durable ``EPICS_MCP_AUDIT_LOG_FILE`` is required by BOTH, while the non-empty allowlist
    pattern and the loopback-only search reach are conditions of the PV gate ALONE. Measured: an
    Olog-write-enabled server starts with the subnet broadcast search on, because both checks hang
    off ``allow_pv_write`` (``safety.py``, ``server.main``).

    Only reachable through ``--set``, since no preset arms a gate: turning one on is a decision to
    make deliberately, not one to inherit from a flag.
    """
    armed = sorted(
        name
        for name in ("EPICS_MCP_ALLOW_PV_WRITE", "EPICS_MCP_ALLOW_OLOG_WRITE")
        if env.get(name, "").strip().lower() in {"1", "true", "yes", "on"}
    )
    if not armed:
        return
    pv_conditions = (
        " The PV gate additionally refuses an empty EPICS_MCP_PV_WRITE_PATTERN and a search reach "
        "beyond loopback; the logbook gate has neither of those conditions."
        if "EPICS_MCP_ALLOW_PV_WRITE" in armed
        else ""
    )
    sys.stderr.write(
        f"epics-init: this configuration ARMS a write gate ({', '.join(armed)}). Such a server "
        "refuses to start without a durable EPICS_MCP_AUDIT_LOG_FILE." + pv_conditions + " The "
        "check below prints the resulting posture in its 'Write gates' block, but it does not "
        "evaluate whether the server would start. See docs/safety.md before you use this block.\n"
    )


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
    parser.add_argument(
        "--out",
        default=None,
        metavar="PATH",
        help="also write the block to PATH as UTF-8 (a shell redirect cannot promise the encoding)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="with --out, replace an existing file instead of refusing",
    )
    parser.add_argument(
        "--absolute-command",
        action="store_true",
        help="name the installed server by its absolute path, for a client without your PATH",
    )
    args = parser.parse_args(argv)

    # Argument combinations that cannot mean anything, refused before any output exists. Both go
    # through parser.error so they exit 2 like every other usage error, rather than inventing a
    # second way to say the same thing.
    if args.list and args.out:
        parser.error("--out has nothing to write with --list: a listing is not a configuration")
    if args.force and not args.out:
        parser.error("--force only means something together with --out")

    if args.list:
        sys.stdout.write(format_listing(PRESETS.values()) + "\n")
        return 0

    command = SERVER_COMMAND
    if args.absolute_command:
        # Before the emit, deliberately: a block naming a command that could not be resolved is
        # worse than no block, because it fails later and somewhere else.
        resolved = _resolve_absolute_command()
        if resolved is None:
            sys.stderr.write(
                f"epics-init: --absolute-command cannot resolve {SERVER_COMMAND!r} on this PATH. "
                "Install the package so its console scripts are on PATH, or drop the option and "
                "put the path in by hand.\n"
            )
            return 2
        command = resolved

    env = with_overrides(PRESETS[args.preset].env, dict(args.overrides))
    block = render_client_config(env, command=command)
    sys.stdout.write(block + "\n")

    # Before any branch returns. An armed write gate is a property of the BLOCK, which has just been
    # emitted, so a reader can act on it whatever happens next; warning only on the path that also
    # runs the check would stay silent on exactly the preset most likely to carry a --set, the one
    # with placeholders still open.
    _warn_about_write_gate(env)

    pending = open_placeholders(env)
    if pending:
        # Refuse the check rather than run it: probing ``<archiver-host>`` yields a DNS failure
        # shaped exactly like a real finding, and the reader cannot tell "your archiver is down"
        # from "you have not filled this in yet". A named refusal is worth more than a report
        # that has to be discounted.
        _warn_about_placeholders(pending)
        if args.out:
            # And refuse the WRITE too, which is the difference between a helpful option and a
            # trap: writing an unfinished block would leave a file on disk that the obvious next
            # command, the same run with --set, then refuses to replace.
            sys.stderr.write(
                f"epics-init: nothing was written to {args.out}, because the block is not usable "
                "yet. Fill the values above in and run the same command again.\n"
            )
        return 0
    if args.out:
        if _write_block(args.out, block, force=args.force) is None:
            return 2
        sys.stderr.write(f"epics-init: wrote {args.out}\n")
    if args.no_check:
        return 0

    exit_code = _run_check(env, args.probe_pv)
    if not args.probe_pv and not configures_a_rest_plane(env):
        _warn_that_nothing_was_verified(args.preset)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
