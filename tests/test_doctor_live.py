"""Live probes for the doctor's archiver INGEST check (QA-35), against a real appliance.

Opt-in: ``pytest -m live`` with ``EPICS_MCP_ARCHIVER_URL`` and ``EPICS_MCP_LIVE_DOCTOR_INGEST=1``
set. The second one is test-only and deliberately absent from ``EpicsConfig`` and the operator
guide (``test_guide_matches_code`` checks every ``EPICS_MCP_*`` token in the guide against the
config and would go red). It exists so this probe fires only on an environment somebody DECLARED
for it: the plain ``EPICS_MCP_ARCHIVER_URL`` also names production archivers, and a read against
one of those should be asked for, not inherited from a shell that happened to have it set.

WHY THESE EXIST
---------------
The offline suite fakes ``rest_get_json``, so every fixture it uses is a copy of a payload
somebody once observed. A copy is unguarded until it is crossed against the real service: the
sibling planes shipped a silent time-window bug that no mocked test could see, for exactly this
reason.

What is pinned here is only what held across BOTH measured appliances (a single-member sandbox and
a 16-member production cluster): the body is a list of dicts, every value is a string, the four
field names exist, and ``instance`` carries the identity that ``getApplianceInfo`` reports. What is
deliberately NOT pinned: the row count (1 versus 16) and the numeric readability of ``eventRate``
(plain "0" on an idle appliance, locale formatted "15,431.54" on a busy one). Pinning either would
make a healthy production cluster fail a test that proves nothing about configuration, the same
overclaim ``_is_archiver_version_string`` avoids one module over.

The verdict assertion is DIFFERENTIAL rather than fixed, so the same test is the negative control
on a starved appliance and the positive control on a working one, instead of encoding whichever
one the author happened to point at.
"""

from __future__ import annotations

import os

import pytest
import requests

from epics_mcp.services.doctor import _identify_archiver
from tests.live_gate import assert_live_available, live_demanded

pytestmark = pytest.mark.live

_COUNT_FIELDS = ("pvCount", "connectedPVCount", "disconnectedPVCount")


@pytest.fixture(autouse=True)
def _require_live_archiver() -> None:
    """Setup-time gate (S30): skip silently by default, fail loudly when a live run is demanded."""
    assert_live_available(
        bool(
            os.environ.get("EPICS_MCP_ARCHIVER_URL")
            and os.environ.get("EPICS_MCP_LIVE_DOCTOR_INGEST")
        ),
        "live doctor ingest probe: set EPICS_MCP_ARCHIVER_URL and EPICS_MCP_LIVE_DOCTOR_INGEST=1 "
        "(the second one declares that this appliance may be probed for its ingest figures)",
        demanded=live_demanded(os.environ),
    )


def test_the_ingest_verdict_matches_what_the_appliance_reports() -> None:
    """The whole chain against a real appliance, asserted DIFFERENTIALLY.

    Reads the appliance's own metrics first, then requires the doctor verdict to agree with them:
    a member with connected channels must be ``ok``, one holding channels with none connected must
    be ``no_ingest``. Either outcome PASSES; a disagreement between the numbers and the verdict is
    the failure. That is what makes this test a positive control on a healthy appliance and a
    negative control on the starved sandbox, without hard-coding which one is on the other end.
    """
    base = os.environ["EPICS_MCP_ARCHIVER_URL"].rstrip("/")
    info = requests.get(f"{base}/mgmt/bpl/getApplianceInfo", timeout=30).json()
    rows = requests.get(f"{base}/mgmt/bpl/getApplianceMetrics", timeout=60).json()

    identity = info["identity"]
    assert isinstance(identity, str) and identity.strip()

    # The shape, pinned only where both measured appliances agree.
    assert isinstance(rows, list) and rows, "getApplianceMetrics returns a non-empty list of rows"
    assert all(isinstance(row, dict) for row in rows)
    mine = [row for row in rows if row.get("instance") == identity]
    assert len(mine) == 1, "getApplianceInfo.identity names exactly one getApplianceMetrics row"
    row = mine[0]
    for field in (*_COUNT_FIELDS, "eventRate", "status"):
        assert field in row, f"{field} is part of the row shape"
    assert all(isinstance(row[field], str) for field in (*_COUNT_FIELDS, "eventRate"))
    # Counts are plain digit runs on both appliances. eventRate is NOT asserted numeric: it is
    # locale formatted once the appliance is busy, which is a display concern, not a fault.
    counts = {field: int(row[field]) for field in _COUNT_FIELDS}

    check = _identify_archiver(base, None, 30.0)
    assert check.identified is True
    assert identity in (check.detail or "")

    if counts["pvCount"] > 0 and counts["connectedPVCount"] == 0:
        assert check.status == "no_ingest", "holding PVs with none connected must be a finding"
        assert "none connected" in (check.detail or "")
    elif row["status"] != "Working":
        assert check.status == "no_ingest", "the appliance reporting a stopped webapp is a finding"
    else:
        assert check.status == "ok", "a connected appliance must not be flagged"
        assert "ingesting" in (check.detail or "")
