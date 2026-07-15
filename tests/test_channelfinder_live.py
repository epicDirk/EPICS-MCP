"""Live probes for the ChannelFinder name glob — semantics only the server knows.

Opt-in: ``pytest -m live`` with ``EPICS_MCP_CHANNELFINDER_URL`` and ``EPICS_MCP_LIVE_CF_GLOB`` set.

WHY THESE EXIST
---------------
The client passes ``name_pattern`` through verbatim as ``~name`` — no escaping, no rewriting, no
anchoring — so every property of the match is the server's, and the tool description used to
promise only "glob (ChannelFinder syntax: * and ?)". Two properties it stayed silent about both
mislead quietly rather than loudly:

* ANCHORED — a bare substring matches NOTHING. That reads as "no such channel", not as "you
  needed stars", which is the same failure mode as an unreadable time window: a well-formed
  answer to a question the caller did not ask.
* CASE-INSENSITIVE — ``*Temp*`` matches ``...MorTemPrd``. Harmless when known, confusing when not.

The assertions are DIFFERENTIAL (the same target expressed several ways) plus a positive control
(the glob must actually hit) and a negative control (an impossible glob must return nothing —
otherwise "it returns channels" would also pass if the filter were ignored entirely).
"""

from __future__ import annotations

import os

import pytest

from epics_pv_mcp.services.channelfinder_client import ChannelFinderClient

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not (
            os.environ.get("EPICS_MCP_CHANNELFINDER_URL")
            and os.environ.get("EPICS_MCP_LIVE_CF_GLOB")
        ),
        reason=(
            "live ChannelFinder probe: set EPICS_MCP_CHANNELFINDER_URL and EPICS_MCP_LIVE_CF_GLOB "
            "(a mixed-case glob matching a few channels, e.g. 'SIM-DEV01*Temp*')"
        ),
    ),
]


@pytest.fixture
def client() -> ChannelFinderClient:
    return ChannelFinderClient(os.environ["EPICS_MCP_CHANNELFINDER_URL"], timeout=20.0)


@pytest.fixture
def glob() -> str:
    """A glob known to match a handful of channels — kept well under any cap, so a count
    comparison compares matches and not the cap."""
    return os.environ["EPICS_MCP_LIVE_CF_GLOB"]


def _names(client: ChannelFinderClient, pattern: str, max_results: int = 300) -> list[str]:
    found = client.find_channels(pattern, max_results=max_results)
    return sorted(c["name"] if isinstance(c, dict) else str(c) for c in found)


def test_glob_matches_something(client: ChannelFinderClient, glob: str) -> None:
    """The positive control — without hits, every probe below is vacuously green."""
    names = _names(client, glob)
    assert names, f"{glob!r} matched nothing: set EPICS_MCP_LIVE_CF_GLOB to a glob that hits"
    assert len(names) < 300, "glob hits the cap — pick a narrower one, or counts compare the cap"


def test_impossible_glob_matches_nothing(client: ChannelFinderClient) -> None:
    """The negative control: proves the server filters at all."""
    assert _names(client, "ZZZ-no-such-channel*") == []


def test_glob_is_case_insensitive(client: ChannelFinderClient, glob: str) -> None:
    """Differential: the same glob in three cases must return the IDENTICAL set."""
    assert _names(client, glob.lower()) == _names(client, glob.upper()) == _names(client, glob)


def test_glob_is_anchored(client: ChannelFinderClient, glob: str) -> None:
    """A full name matches exactly; a bare inner substring of it matches nothing until wrapped
    in stars. This is the property that reads as 'channel does not exist'.

    The wrapped form is only asserted to be NON-EMPTY, deliberately: '*<inner>*' can match a
    whole site, and a capped result would then omit `exact` for reasons that have nothing to do
    with anchoring. Comparing against a cap measures the cap.
    """
    exact = _names(client, glob)[0]
    assert _names(client, exact) == [exact]

    inner = exact[len(exact) // 3 : len(exact) // 3 + 8]
    assert _names(client, inner) == [], f"{inner!r} matched unanchored — the glob is not anchored"
    assert _names(client, f"*{inner}*"), f"'*{inner}*' matched nothing — inner is not a substring"
