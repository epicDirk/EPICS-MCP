"""Live premise behind the WRITE-side level guard (OQ1), pinned so it goes red if Olog changes.

Opt-in: ``pytest tests/test_olog_write_live.py -m live`` against a WRITABLE Olog.

This is the live half of ``tests/test_olog_write.py``, which holds the same guard's offline half.
That pairing is the reason this file exists as its own module rather than as one more test
somewhere else: nine of the live modules already sit opposite an offline module of the same stem,
and the OQ1 write guard was one of the two Olog surfaces missing that partner (the other is
``tests/test_olog_update.py``, which still has none and is not this module's business).

WHY IT IS NOT IN ``test_olog_live.py`` ANY MORE
-----------------------------------------------
It used to live there, and that module's gate asks for ``EPICS_MCP_OLOG_URL`` and nothing else. So
a module advertised as read-only carried a test that creates two real entries and updates one, in a
service with no delete: point a read run at a logbook, happen to have write credentials in the
environment, and the entries are there for good. The second cost was visible on every demanded read
run, because ``EPICS_MCP_REQUIRE_LIVE=1`` turns this test's own missing write prerequisite into a
LOUD failure that says nothing about the read plane. The 2026-09-02 acceptance run worked around it
with ``--deselect``, which is a plaster on a module boundary that was simply in the wrong place.

Now the module gate IS the write precondition: without it the file skips as a whole, and a demanded
run fails on the gate with a reason instead of somewhere inside a test.

WHAT THIS STILL DOES NOT GUARD, so nobody reads it as more than it is: the client below is built
directly, so ``OlogWriteGate`` and its loopback boundary are not on this path. The environment is
what decides where these entries land. That gap is tracked separately; it is a property of every
Olog write module here, not of this move.

MUTATION, NAMED: a full green run leaves TWO entries on the server, one carrying an unknown level
and one whose level is then cleared. Both titles mark them as probes, because Olog has no delete
and an artifact can be labelled but never removed.
"""

from __future__ import annotations

import os

import pytest

from epics_mcp.services._http import basic_auth_header
from epics_mcp.services.olog_client import OlogClient
from tests.live_gate import assert_live_available, live_demanded

#: The write preconditions, read once at import so the gate and the body share one snapshot, the
#: same MECHANISM as the sibling write modules but deliberately not the same LIST: they demand
#: five keys including the password, this gate demands four, because this test needs no password
#: to be correct. ``EPICS_MCP_OLOG_URL`` belongs in here and not only in
#: the body: the body reads it with ``os.environ[...]``, so a missing value would raise KeyError at
#: setup instead of skipping. ``EPICS_MCP_OLOG_WRITE_PASSWORD`` deliberately does NOT belong in it,
#: because the body reads that one with a ``""`` default and a wrong password fails loudly as 401.
_URL = os.environ.get("EPICS_MCP_OLOG_URL")
_WRITE = os.environ.get("EPICS_MCP_ALLOW_OLOG_WRITE", "").lower() == "true"
_LOGBOOKS = os.environ.get("EPICS_MCP_OLOG_WRITE_LOGBOOKS")
_WRITE_USER = os.environ.get("EPICS_MCP_OLOG_WRITE_USER")

pytestmark = pytest.mark.live


@pytest.fixture(autouse=True)
def _require_write_stack() -> None:
    """Setup-time gate (S30): skip silently by default, fail loudly when a live run is demanded
    (EPICS_MCP_REQUIRE_LIVE=1) and the write plane is not configured."""
    assert_live_available(
        bool(_URL and _WRITE and _LOGBOOKS and _WRITE_USER),
        "pins the server behaviour that justifies the write-side level refusal: needs a WRITABLE "
        "Olog (EPICS_MCP_OLOG_URL + EPICS_MCP_ALLOW_OLOG_WRITE + _WRITE_LOGBOOKS + _WRITE_USER; "
        "the password is read with a '' default, a wrong one fails loudly as 401)",
        demanded=live_demanded(os.environ),
    )


def test_server_does_not_validate_a_written_level() -> None:
    """The PREMISE of the OQ1 guard, measured instead of read off the Java source.

    ``create_log_entry`` / ``update_log_entry`` refuse an unknown or blank ``level``. That refusal
    is only correct while the server itself does NOT validate, otherwise the client would be
    turning a server-side 400 into a made-up story, or worse, refusing something the server would
    have accepted meaningfully.

    CLAUDE.md is explicit that reading ``LevelsResource``/``LogResource`` is NOT a measurement here
    ("wrong five times in one week"), and that a refusal's premise must be pinned live "so it goes
    red when the server improves". This is that pin. If a future Olog starts rejecting an unknown
    level, this test fails FIRST and the three tool descriptions + the operator guide get corrected
    instead of quietly lying.

    Deliberately bypasses the service layer and drives the client directly, the service is exactly
    what refuses, so going through it could never observe the server.
    """
    logbook = str(os.environ["EPICS_MCP_OLOG_WRITE_LOGBOOKS"]).split(",")[0].strip()
    url = os.environ["EPICS_MCP_OLOG_URL"]
    client = OlogClient(
        url,
        timeout=15.0,
        auth_header=basic_auth_header(
            os.environ["EPICS_MCP_OLOG_WRITE_USER"],
            os.environ.get("EPICS_MCP_OLOG_WRITE_PASSWORD", ""),
        ),
    )
    known, _default, _note = client.list_log_levels()
    bogus = "Urgnet"  # a typo of a real level, so it cannot collide with a site's vocabulary
    assert bogus not in known, "pick a value the server really does not know"

    # (1) an UNKNOWN level is accepted and stored verbatim, no 400, no coercion to the default
    created = client.create_log_entry(
        title="live pin: unknown level is not validated",
        logbooks=[logbook],
        description="synthetic probe entry",
        level=bogus,
    )
    entry_id = str(created["id"])
    stored = client.get_raw_entry(entry_id)
    assert stored is not None
    assert stored["level"] == bogus, (
        "the server VALIDATES a written level now, the write-side refusal's premise is gone; "
        "update the level descriptions in server.py and the operator guide"
    )

    # ...and the entry is therefore invisible to every valid level filter (the actual damage).
    # Absence is asserted over ONE page, which is sound here and not the single-page assumption
    # repaired in the level probes next door: the entry was created seconds ago, the search is
    # newest-first, so if the filter did match it, it would be at the front of the first page.
    hits, _capped, _total = client.search_logbook(level=",".join(known), size=200)
    assert all(str(h.get("id")) != entry_id for h in hits)

    # (2) a BLANK level is accepted too, and CLEARS the field rather than leaving it alone
    with_level = client.create_log_entry(
        title="live pin: blank level clears the field",
        logbooks=[logbook],
        description="synthetic probe entry",
        level=known[0],
    )
    blank_id = str(with_level["id"])
    raw = client.get_raw_entry(blank_id)
    assert raw is not None and raw["level"] == known[0]
    client.update_log_entry(raw, level="")
    after = client.get_raw_entry(blank_id)
    assert after is not None
    assert after["level"] in ("", None), (
        "a blank level no longer clears the field, the blank-level refusal needs a rethink"
    )
