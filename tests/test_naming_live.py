"""Live probes for an ESS-style Naming Service — the S11 schema anchors a mock cannot carry.

Opt-in: ``pytest -m live`` with ``EPICS_MCP_NAMING_URL`` pointing at a reachable Naming Service
and ``EPICS_MCP_LIVE_NAMING_DEVICE`` naming a REGISTERED device. No facility value is committed —
both come from the environment; the negative-control name is synthetic.

WHY THESE EXIST
---------------
The strict response schema (S11) was DERIVED from live measurements (2026-07-16): the record
behind ``/rest/deviceNames/{name}`` always carries a string ``status``; a nonexistent name is
answered with HTTP **204** (not the 404 the old contract assumed — S16a); and the service serves
XML unless the client asks for JSON (which the shared session builder does). These tests pin
those premises against the real wire, so a service change turns them red — the schema is then
re-measured, never loosened blindly.

S13 adds an identity premise: a 204/404 is trusted as a definitive "not registered" only when the
service names itself via ``/rest/swagger.json`` (info.title). ``test_swagger_beacon_identifies_the
_service`` pins that server fact so a title reword or a dropped swagger endpoint turns red — then
re-measure ``NAMING_SWAGGER_TITLE`` in ``naming_identity``, never loosen the gate.
"""

from __future__ import annotations

import os

import pytest

from epics_pv_mcp.services.naming_client import NamingServiceClient
from epics_pv_mcp.services.naming_identity import probe_naming_identity
from tests.live_gate import assert_live_available, live_demanded

pytestmark = pytest.mark.live


@pytest.fixture(autouse=True)
def _require_live_stack() -> None:
    """Setup-time gate (S30): skip silently by default, fail loudly when a live run is
    demanded (EPICS_MCP_REQUIRE_LIVE=1) and the plane is not configured."""
    assert_live_available(
        bool(
            os.environ.get("EPICS_MCP_NAMING_URL")
            and os.environ.get("EPICS_MCP_LIVE_NAMING_DEVICE")
        ),
        "live Naming probe: set EPICS_MCP_NAMING_URL and EPICS_MCP_LIVE_NAMING_DEVICE "
        "(a registered device name on that service)",
        demanded=live_demanded(os.environ),
    )


@pytest.fixture
def client() -> NamingServiceClient:
    return NamingServiceClient(os.environ["EPICS_MCP_NAMING_URL"], timeout=15.0)


@pytest.fixture
def device() -> str:
    return os.environ["EPICS_MCP_LIVE_NAMING_DEVICE"]


def test_registered_device_record_satisfies_the_strict_schema(
    client: NamingServiceClient, device: str
) -> None:
    """S11 anchor: the live record carries a readable string ``status`` (measured: a JSON dict
    with status/name/uuid/… when asked with ``Accept: application/json``). The client now RAISES
    on a record without it — this run passing pins the premise against the real service.
    ``registered`` may be True or False (an OBSOLETE fixture is fine); the anchor is that the
    answer is READABLE, never fabricated."""
    result = client.validate_name(device)
    assert isinstance(result["status"], str) and result["status"]
    assert isinstance(result["registered"], bool)


def test_nonexistent_name_is_definitively_not_registered(client: NamingServiceClient) -> None:
    """S16(a) premise pin, measured 2026-07-16: the real service answers HTTP **204** (No
    Content) for a nonexistent name — not 404. The client maps that measured signal (and a
    genuine 404) to the definitive ``registered: False``; anything else withholds. Goes red if
    the service changes its no-such-name signal — then re-measure before touching the mapping.
    Since S13 this also implicitly requires the swagger-identity gate to VERIFY (a real service
    does), so a broken swagger would withhold here too — pinned explicitly in the next test."""
    result = client.validate_name("ZZZ-FAKE99:Ctrl-X-99")
    assert result["registered"] is False
    assert "not registered" in result["message"]


def test_swagger_beacon_identifies_the_service(client: NamingServiceClient) -> None:
    """S13 premise pin: the definitive-negative identity gate trusts a 204/404 only when
    ``/rest/swagger.json`` names the service (info.title == NAMING_SWAGGER_TITLE). This pins that
    server fact against the real wire, so it goes RED if ESS rewords the swagger title or drops the
    endpoint — then re-measure the constant in ``naming_identity``, never loosen the gate blindly.
    (The 204→not-registered mapping is pinned by the test above, which now depends on this.)"""
    assert probe_naming_identity(client.base_url, timeout=client.timeout) == "verified"
