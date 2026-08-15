"""The two cross-plane patterns that had NO field evidence, reproduced end to end.

QA-96 says it in as many words: *a pattern only the test fixture produces does not count*. Of the
three patterns, exactly one arrived with a real observation behind it (a facility where one HTTPS
plane presented a self-signed certificate while two neighbours were healthy). The other two,
"the archiver pair is exchanged" and "one host is gone", had no recorded occurrence anywhere in
this project, only neighbouring failure modes.

So they are reproduced here the way this repository reproduced the retrieval-fallback finding
before: against a REAL HTTP server on loopback and a REAL closed port, with no client double
anywhere in the path. The whole stack runs, from ``run_doctor`` through the actual REST clients
and sockets, and the pattern has to fall out of it.

⚠️ Loopback only, ephemeral ports, and every server is shut down by its fixture. Nothing here
reaches a facility, and nothing here needs the sandbox: these are throwaway servers whose only job
is to answer one route and refuse another.
"""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from epics_mcp.config import EpicsConfig
from epics_mcp.services.doctor import run_doctor

_TIMEOUT = 1.0


def _serve(routes: dict[str, object]) -> Iterator[str]:
    """A throwaway HTTP server answering *routes* with JSON and 404 for everything else."""

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            payload = routes.get(self.path)
            body = json.dumps(payload if payload is not None else {"error": "no such route"})
            self.send_response(200 if payload is not None else 404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body.encode())

        def do_HEAD(self) -> None:
            self.send_response(200 if self.path in routes else 404)
            self.end_headers()

        def log_message(self, *args: object) -> None:
            """Silent: the probes here are expected to fail and the log is noise."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def mgmt_only() -> Iterator[str]:
    """An appliance serving the MGMT webapp and nothing else."""
    yield from _serve({"/mgmt/bpl/getApplianceInfo": {"identity": "appliance0"}})


@pytest.fixture
def retrieval_only() -> Iterator[str]:
    """An appliance serving the RETRIEVAL webapp and nothing else."""
    yield from _serve({"/retrieval/bpl/getVersion": {"version": "Archiver Appliance 2.2.1"}})


def _closed_port() -> int:
    """A port nothing is listening on: bound to learn the number, then released."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


async def _findings(monkeypatch: pytest.MonkeyPatch, **urls: str) -> list[str]:
    cfg = EpicsConfig(**urls)  # type: ignore[arg-type]
    monkeypatch.setattr("epics_mcp.services.doctor.get_config", lambda: cfg)
    report = await run_doctor(timeout=_TIMEOUT)
    return [finding.pattern for finding in report.installation.findings]


async def test_an_exchanged_archiver_pair_is_reproduced_against_real_servers(
    monkeypatch: pytest.MonkeyPatch, mgmt_only: str, retrieval_only: str
) -> None:
    """The pattern, produced by the mistake itself rather than by a hand-built PlaneCheck.

    Two real appliances, each serving exactly one webapp, with the two variables pointing at the
    wrong one. Both identity probes then hit a route their server does not serve, both planes come
    back erroring, and the cross-cut has to notice that the pair is the thing to look at.
    """
    patterns = await _findings(
        monkeypatch,
        archiver_url=retrieval_only,  # the MGMT variable, pointed at the retrieval webapp
        archiver_retrieval_url=mgmt_only,  # and the other way round
    )
    assert "archiver_url_pair" in patterns


async def test_the_same_two_servers_wired_correctly_produce_no_finding(
    monkeypatch: pytest.MonkeyPatch, mgmt_only: str, retrieval_only: str
) -> None:
    """The control, and it is the half that makes the test above mean anything.

    Same servers, same fixture, correct wiring. A finding here would prove the pattern fires on
    the SETUP rather than on the mistake.
    """
    patterns = await _findings(
        monkeypatch, archiver_url=mgmt_only, archiver_retrieval_url=retrieval_only
    )
    assert "archiver_url_pair" not in patterns


async def test_one_dead_host_is_reproduced_against_real_closed_ports(
    monkeypatch: pytest.MonkeyPatch, mgmt_only: str
) -> None:
    """Two services on one host that nothing answers, beside a host that does answer.

    The dead pair is two genuinely closed ports, so the failures are real transport failures from
    the real clients, not a raised exception from a double. The healthy neighbour is the live mgmt
    server reached under a DIFFERENT host spelling, which is what stops the finding from being the
    trivial "everything is dead" case the pattern deliberately stays silent about.
    """
    dead_one, dead_two = _closed_port(), _closed_port()
    patterns = await _findings(
        monkeypatch,
        channelfinder_url=f"http://localhost:{dead_one}/ChannelFinder",
        alarm_url=f"http://localhost:{dead_two}",
        archiver_url=mgmt_only,  # 127.0.0.1, a different host string, and it answers
    )
    assert "host_down" in patterns


async def test_a_single_dead_service_on_that_host_produces_no_finding(
    monkeypatch: pytest.MonkeyPatch, mgmt_only: str
) -> None:
    """The control. One closed port on the host is a broken service, not a gone host."""
    patterns = await _findings(
        monkeypatch,
        channelfinder_url=f"http://localhost:{_closed_port()}/ChannelFinder",
        archiver_url=mgmt_only,
    )
    assert "host_down" not in patterns


async def test_everything_closed_is_reproduced_as_silence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The suppression, against real closed ports rather than against constructed statuses.

    Every configured plane dead is the shape a proxy or a broken resolver produces on THIS machine,
    measured twice in this project, and naming a host then would be pointing at the wrong end.
    """
    patterns = await _findings(
        monkeypatch,
        channelfinder_url=f"http://localhost:{_closed_port()}/ChannelFinder",
        alarm_url=f"http://localhost:{_closed_port()}",
        naming_url=f"http://localhost:{_closed_port()}",
    )
    assert "host_down" not in patterns
