"""Serve two test PVs over PVAccess, so the quick start needs no control system (``epics-testpv``).

The first step of the quick start used to be ``softIocPVA -d test.db``, which needs EPICS Base. No
page said where to get it, and no route avoids a build, so the "five minutes, no facility"
promise was not keepable. This command replaces it: p4p is already a hard dependency of
this package, so whatever installed the server can serve a PV, and nothing else has to be obtained.

It serves exactly what the documentation needs and no more: an analogue reading with a unit, and a
writable switch, which is the only target the write-gate example in ``docs/mcp-clients.md`` has.

**Loopback by default, and that is structural rather than documented.** A PVA server is a network
service: p4p's own default binds every interface and the switch below accepts writes, so an unbound
test PV would be a writable service on the local subnets. ``--interface`` opens it deliberately, and
the command prints what it is listening on either way, because a reader should not have to take a
reach claim on trust.

Four things about p4p that this module gets right only because they were measured, each of which a
plausible first version gets wrong:

* ``Server(isolate=True)`` reads like exactly what "loopback only" wants, and its docstring says so,
  but it also sets ``EPICS_PVA_SERVER_PORT=0``: a RANDOM port. The ``sandbox`` preset points at
  ``127.0.0.1:5075``, so that would break the quick start it exists to serve. An explicit
  ``EPICS_PVAS_INTF_ADDR_LIST`` keeps the default port.
* The effective port is in ``conf()["EPICS_PVAS_SERVER_PORT"]``. ``EPICS_PVAS_INTF_ADDR_LIST`` keeps
  reporting the port that was ASKED for, so a report built from it lies after a fallback.
* When the default port is taken, p4p falls back to a free one and carries on. That is survivable,
  because the ``sandbox`` preset searches by UDP as well and the UDP route is port-agnostic
  (measured: with both routes set, a fallback server is found; with only the TCP route, it is not).
  It is still worth saying out loud, because a reader who configured only the TCP route sees nothing
  but a timeout.
* ``NTScalar("d", display=True)`` builds the display structure but leaves ``units`` empty, and
  ``SharedPV(initial=<number>)`` leaves the timestamp at 0, so every reader sees a 1970 sample with
  no unit. Both are set here explicitly. ``display`` has no ``precision`` member, whatever other
  NTScalar implementations offer; naming one raises ``KeyError`` at wrap time.

Exit code: ``0`` on Ctrl-C, which is how a foreground server is meant to end; ``2`` for a usage
error.

Usage::

    epics-testpv                        # loopback, the default port, until Ctrl-C
    epics-testpv --interface 0.0.0.0    # deliberately reachable from elsewhere
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable, Mapping

from p4p.nt import NTEnum, NTScalar
from p4p.server import Server, StaticProvider
from p4p.server.thread import SharedPV

from epics_mcp.cli_common import add_version_argument, configure_stdout

#: The analogue reading every example in the documentation names.
ANALOGUE_PV = "TEST:Temperature"
#: A writable switch, the only target the write-gate example has. An enum rather than a number, so a
#: readback shows a LABEL and the value/label distinction is visible in the example.
SWITCH_PV = "TEST:Heater"

_INITIAL_CELSIUS = 21.5
_SWITCH_CHOICES = ("Off", "On")
_LOOPBACK = "127.0.0.1"
#: p4p reports the port it actually bound under this key, which is not the one it was asked for.
_EFFECTIVE_PORT_KEY = "EPICS_PVAS_SERVER_PORT"
_DEFAULT_PVA_PORT = "5075"


def initial_analogue_value(*, now: float, celsius: float = _INITIAL_CELSIUS) -> dict[str, object]:
    """The initial value of the analogue PV, carrying a unit and a real timestamp.

    *now* is passed in rather than read here, so the structure this builds is a pure function of its
    arguments and a test can assert on it. Both fields are set because p4p supplies neither: a bare
    ``initial=21.5`` yields empty ``units`` and a timestamp of 0, which readers render as 1970.
    """
    seconds = int(now)
    return {
        "value": celsius,
        # display holds limitLow, limitHigh, description, format and units. There is no
        # 'precision' member; naming one raises KeyError when p4p wraps the value.
        "display": {"units": "C", "description": "Synthetic test reading", "format": "%.1f"},
        "timeStamp": {
            "secondsPastEpoch": seconds,
            "nanoseconds": int((now - seconds) * 1_000_000_000),
        },
    }


def build_provider(*, now: Callable[[], float] = time.time) -> StaticProvider:
    """A provider holding the two PVs, with the switch accepting writes.

    The switch is writable on purpose: ``docs/mcp-clients.md`` shows a write-enabled configuration
    whose allowlist is ``^TEST:.*``, and a documented write needs something to write to.
    """
    analogue = SharedPV(nt=NTScalar("d", display=True), initial=initial_analogue_value(now=now()))
    switch = SharedPV(nt=NTEnum(), initial={"index": 0, "choices": list(_SWITCH_CHOICES)})

    # p4p's put hook is an untyped property-based decorator, and there is no typed alternative:
    # SharedPV.put is a property rather than a callable, so it cannot be applied as a function.
    @switch.put  # type: ignore[untyped-decorator]
    def _accept_write(pv: SharedPV, op: object) -> None:
        """Accept and publish a write, so a readback shows what was sent."""
        pv.post(op.value())  # type: ignore[attr-defined]
        op.done()  # type: ignore[attr-defined]

    provider = StaticProvider("epics-testpv")
    provider.add(ANALOGUE_PV, analogue)
    provider.add(SWITCH_PV, switch)
    return provider


def server_conf(interface: str) -> dict[str, str]:
    """The p4p server configuration for binding *interface* on the DEFAULT PVA port.

    Deliberately not ``Server(isolate=True)``, which would also pick a random port and put the
    server where the ``sandbox`` preset cannot find it by its TCP route. The auto-address list is
    disabled so the server announces itself no further than it listens.
    """
    return {"EPICS_PVAS_INTF_ADDR_LIST": interface, "EPICS_PVA_AUTO_ADDR_LIST": "0"}


def describe(interface: str, effective: Mapping[str, str]) -> list[str]:
    """The lines printed once the server is up: where it listens, and what it serves.

    The port comes from the effective configuration rather than from what was requested, because p4p
    falls back to a free port when the default one is taken and keeps the requested one in the
    interface entry. A caller who trusted that entry would print a port nothing is listening on.
    """
    port = effective.get(_EFFECTIVE_PORT_KEY, "unknown")
    lines = [f"Serving on {interface}:{port} (PVAccess)"]
    if port != _DEFAULT_PVA_PORT:
        lines.append(
            f"  NOTE: port {_DEFAULT_PVA_PORT} was taken, so this fell back to {port}. A client "
            "searching by address list still finds it; one pointed only at "
            f"EPICS_PVA_NAME_SERVERS=...:{_DEFAULT_PVA_PORT} will not."
        )
    lines.append(f"  {ANALOGUE_PV}   analogue reading, degrees C")
    lines.append(f"  {SWITCH_PV}        writable switch, {'/'.join(_SWITCH_CHOICES)}")
    if interface == _LOOPBACK:
        lines.append("  Reach: this host only. Pass --interface to widen it deliberately.")
    else:
        lines.append(f"  Reach: WIDE. {interface} is not loopback, and the switch accepts writes.")
    lines.append("Press Ctrl-C to stop.")
    return lines


def main(argv: list[str] | None = None) -> int:
    """Serve the test PVs until interrupted."""
    configure_stdout()  # before the parser: --help and --version print inside parse_args
    parser = argparse.ArgumentParser(
        prog="epics-testpv",
        description=(
            "Serve two synthetic test PVs over PVAccess, so the quick start needs no control "
            "system. Loopback only unless --interface says otherwise. Runs until Ctrl-C."
        ),
    )
    add_version_argument(parser)
    parser.add_argument(
        "--interface",
        default=_LOOPBACK,
        metavar="ADDR",
        help=f"interface to bind (default {_LOOPBACK}, this host only)",
    )
    args = parser.parse_args(argv)

    with Server(
        providers=[build_provider()], useenv=False, conf=server_conf(args.interface)
    ) as server:
        for line in describe(args.interface, server.conf()):
            sys.stdout.write(line + "\n")
        sys.stdout.flush()
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            sys.stdout.write("Stopped.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
