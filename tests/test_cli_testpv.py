"""Tests for the test-PV server behind ``epics-testpv``.

Two halves. The pure builders are asserted directly, which is why the timestamp source is injected
rather than read inside them. Then one integration test actually serves the PVs and reads them back
through a real p4p client, because everything this command exists for lives on that path: a value
with a unit, a timestamp that is not 1970, and a switch that accepts a write.

The integration test binds through ``Server(isolate=True)``, which is the OPPOSITE of what the
command does in production and deliberately so: isolate picks a random port, which is right for a
test that must not collide with anything, and wrong for a server the ``sandbox`` preset has to
find at a known one. That asymmetry is the point of ``cli_testpv.server_conf``.
"""

from __future__ import annotations

import gc

import pytest
from p4p.client.thread import Context
from p4p.server import Server

from epics_mcp import cli_testpv
from epics_mcp.presets import PRESETS


class TestTheInitialValue:
    """What p4p does not supply on its own, and this module therefore sets."""

    def test_it_carries_a_unit(self) -> None:
        """``NTScalar("d", display=True)`` builds the display structure with an EMPTY units field,
        so a reader sees a bare number. Measured, and the reason this dict exists at all."""
        value = cli_testpv.initial_analogue_value(now=1_700_000_000.0)

        assert value["display"] == {
            "units": "C",
            "description": "Synthetic test reading",
            "format": "%.1f",
        }

    def test_it_carries_the_injected_timestamp(self) -> None:
        """``SharedPV(initial=<number>)`` leaves the timestamp at 0, which every reader renders as
        1970. The clock is a parameter so this is assertable at all."""
        value = cli_testpv.initial_analogue_value(now=1_700_000_000.5)

        assert value["timeStamp"] == {
            "secondsPastEpoch": 1_700_000_000,
            "nanoseconds": 500_000_000,
        }

    def test_it_names_no_precision_field(self) -> None:
        """A guard against a plausible addition. p4p's display structure holds limitLow, limitHigh,
        description, format and units; other NTScalar implementations offer ``precision``, and
        naming it here raises ``KeyError`` inside p4p at wrap time, which fails at SERVER STARTUP
        rather than in a test. Measured on p4p by doing exactly that."""
        display = cli_testpv.initial_analogue_value(now=0.0)["display"]

        assert "precision" not in display  # type: ignore[operator]


class TestTheServerConfiguration:
    """Where it listens, which is a safety property rather than a preference."""

    def test_the_default_is_loopback(self) -> None:
        """A PVA server binds every interface by default and the switch accepts writes, so an
        unbound test PV would be a writable service on the local subnets."""
        conf = cli_testpv.server_conf("127.0.0.1")

        assert conf["EPICS_PVAS_INTF_ADDR_LIST"] == "127.0.0.1"
        assert conf["EPICS_PVA_AUTO_ADDR_LIST"] == "0"

    def test_it_does_not_pin_a_random_port(self) -> None:
        """THE reason this is not ``Server(isolate=True)``, which reads like the right answer.

        isolate also sets ``EPICS_PVA_SERVER_PORT=0``, a random port, and the ``sandbox`` preset
        points its TCP-unicast route at ``127.0.0.1:5075``. A random port would therefore break the
        quick start this command exists to serve, while looking correct in every other respect.
        """
        conf = cli_testpv.server_conf("127.0.0.1")

        assert "EPICS_PVA_SERVER_PORT" not in conf
        assert "EPICS_PVAS_SERVER_PORT" not in conf


class TestTheReport:
    """What the command prints, which is the only place a reader learns its reach."""

    def test_it_reports_the_effective_port_not_the_requested_one(self) -> None:
        """Measured: when the default port is taken, p4p binds a free one and keeps the REQUESTED
        port in the interface entry. A report built from that entry names a port nothing listens on.
        """
        lines = cli_testpv.describe(
            "127.0.0.1",
            {"EPICS_PVAS_INTF_ADDR_LIST": "127.0.0.1:5075", "EPICS_PVAS_SERVER_PORT": "61301"},
        )

        assert "127.0.0.1:61301" in lines[0]
        assert "5075" not in lines[0]

    def test_a_port_fallback_is_named_with_its_consequence(self) -> None:
        """A fallback is survivable, because the preset also searches by UDP and that route is
        port-agnostic. It is not survivable for a reader who configured only the TCP route, so the
        note says which one stops working rather than just reporting a number."""
        lines = cli_testpv.describe("127.0.0.1", {"EPICS_PVAS_SERVER_PORT": "61301"})

        assert any("was taken" in line and "EPICS_PVA_NAME_SERVERS" in line for line in lines)

    def test_no_note_when_the_default_port_was_obtained(self) -> None:
        """The negative control: a note on every run is a note a reader learns to skip."""
        lines = cli_testpv.describe("127.0.0.1", {"EPICS_PVAS_SERVER_PORT": "5075"})

        assert not any("was taken" in line for line in lines)

    @pytest.mark.parametrize(
        ("interface", "expected"),
        [
            pytest.param("127.0.0.1", "this host only", id="loopback-says-so"),
            pytest.param("0.0.0.0", "WIDE", id="anything-else-says-wide"),
        ],
    )
    def test_the_reach_is_stated_either_way(self, interface: str, expected: str) -> None:
        """This repository states reach everywhere else, and a server it tells people to start is
        no exception."""
        lines = cli_testpv.describe(
            interface,
            {"EPICS_PVAS_SERVER_PORT": "5075", "EPICS_PVAS_INTF_ADDR_LIST": interface},
        )

        assert any(expected in line for line in lines)

    @pytest.mark.parametrize(
        ("bound", "expected"),
        [
            pytest.param("localhost:5075", "this host only", id="localhost-is-loopback"),
            pytest.param("[::1]:5075", "this host only", id="ipv6-loopback"),
            pytest.param("127.0.0.5:5075", "this host only", id="whole-127-range"),
            pytest.param("10.0.0.7:5075", "WIDE", id="a-real-address-is-wide"),
        ],
    )
    def test_the_reach_line_follows_the_binding_not_the_argument(
        self, bound: str, expected: str
    ) -> None:
        """The reach claim is this module's whole safety statement, so it is derived from what p4p
        reports rather than from what the caller typed.

        Reading the argument made the line a restatement of the request: ``--interface localhost``
        binds loopback and was reported as WIDE. The argument passed here is deliberately the
        OPPOSITE of the reported binding, so a version that reads the argument fails.
        """
        opposite = "0.0.0.0" if expected == "this host only" else "127.0.0.1"
        lines = cli_testpv.describe(
            opposite, {"EPICS_PVAS_SERVER_PORT": "5075", "EPICS_PVAS_INTF_ADDR_LIST": bound}
        )

        assert any(expected in line for line in lines)


def test_the_served_pvs_answer_a_real_client() -> None:
    """The integration test: both PVs, read back over PVAccess, plus a write to the switch.

    Everything the command exists for is on this path and invisible in the builders: that
    p4p accepts the initial structures at all (a wrong display member raises at startup, not here),
    that the unit and the timestamp survive the round trip, and that the write handler is wired.

    ``isolate=True`` is used HERE and nowhere else, for a random loopback port that cannot collide
    with a developer's own IOC or with a parallel test run. Production does the opposite.

    The provider is BOUND to a name, exactly as ``main`` binds it, and that is the property this
    test now pins rather than a detail of its own setup: p4p's ``Server`` registers only the C++
    source and keeps no Python reference to a ``StaticProvider``, so an anonymous temporary loses
    its ``SharedPV`` wrappers to the first cyclic collection and the put handler with them. The
    switch then refuses every write for the life of the process (pvxs 1.5.1 reports it as the
    copy-pasted "RPC not implemented by this PV"), while the gets keep passing from the C++ cache.
    This test went red that way four times in CI, every time on 3.12, which is a scheduling
    accident rather than a version difference: a collection merely has to fall between the
    construction and the put. The forced collection below makes it fall there on every run, so the
    guard states the property instead of sampling it.
    """
    provider = cli_testpv.build_provider()
    with (
        Server(providers=[provider], isolate=True) as server,
        Context("pva", conf=server.conf(), useenv=False) as ctx,
    ):
        analogue = ctx.get(cli_testpv.ANALOGUE_PV, timeout=10).raw
        switch = ctx.get(cli_testpv.SWITCH_PV, timeout=10)

        assert analogue["value"] == pytest.approx(21.5)
        assert analogue["display.units"] == "C"
        assert analogue["timeStamp.secondsPastEpoch"] > 0  # not 1970
        assert str(switch) == "Off"

        # Red proof for the paragraph above: with the provider passed anonymously, the put below
        # raises RemoteError("RPC not implemented by this PV") after these two lines.
        gc.collect()
        gc.collect()

        ctx.put(cli_testpv.SWITCH_PV, "On", timeout=10)

        assert str(ctx.get(cli_testpv.SWITCH_PV, timeout=10)) == "On"

        # The report is built from these two keys. p4p renaming either would make describe() fall
        # back to "unknown" and to the argument, quietly, and nothing else looks at a real conf().
        assert cli_testpv._EFFECTIVE_PORT_KEY in server.conf()
        assert cli_testpv._EFFECTIVE_INTERFACE_KEY in server.conf()


def test_the_default_port_matches_the_preset_that_has_to_find_this_server() -> None:
    """Two copies of one number, and the fallback note is built on ours.

    ``describe()`` says "port 5075 was taken" by comparing against its own constant. If the sandbox
    preset ever pointed its TCP route elsewhere, that message would print on every ordinary run
    while the real mismatch went unmentioned. No I/O, so it costs nothing to keep the two tied.
    """
    assert cli_testpv._DEFAULT_PVA_PORT in PRESETS["sandbox"].env["EPICS_PVA_NAME_SERVERS"]


def test_an_unbindable_interface_is_a_usage_error_not_a_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The module documents "2 for a usage error", and naming an address this host does not have is
    how a caller reaches it. p4p raises RuntimeError, which without a handler leaves the reader with
    a traceback and the documented contract broken."""
    exit_code = cli_testpv.main(["--interface", "203.0.113.255"])

    assert exit_code == 2
    assert "cannot bind" in capsys.readouterr().err
