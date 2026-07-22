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
from tests.live_gate import assert_live_available, live_demanded

pytestmark = pytest.mark.live


@pytest.fixture(autouse=True)
def _require_live_stack() -> None:
    """Setup-time gate (S30): skip silently by default, fail loudly when a live run is
    demanded (EPICS_MCP_REQUIRE_LIVE=1) and the plane is not configured."""
    assert_live_available(
        bool(
            os.environ.get("EPICS_MCP_CHANNELFINDER_URL")
            and os.environ.get("EPICS_MCP_LIVE_CF_GLOB")
        ),
        "live ChannelFinder probe: set EPICS_MCP_CHANNELFINDER_URL and EPICS_MCP_LIVE_CF_GLOB "
        "(a mixed-case glob matching a few channels, e.g. 'SIM-DEV01*Temp*')",
        demanded=live_demanded(os.environ),
    )


@pytest.fixture
def client() -> ChannelFinderClient:
    return ChannelFinderClient(os.environ["EPICS_MCP_CHANNELFINDER_URL"], timeout=20.0)


@pytest.fixture
def glob() -> str:
    """A glob known to match a handful of channels — kept well under any cap, so a count
    comparison compares matches and not the cap."""
    return os.environ["EPICS_MCP_LIVE_CF_GLOB"]


#: The result cap for the probes. ``find_channels`` returns a plain list with NO ``capped`` flag
#: (unlike the alarm/archiver clients), so truncation is only visible as ``len(...)`` reaching this
#: value — which is why every set comparison below has to check the length against it itself.
_MAX_RESULTS = 300


def _names(client: ChannelFinderClient, pattern: str, max_results: int = _MAX_RESULTS) -> list[str]:
    found = client.find_channels(pattern, max_results=max_results)
    return sorted(c["name"] if isinstance(c, dict) else str(c) for c in found)


def test_glob_matches_something(client: ChannelFinderClient, glob: str) -> None:
    """The positive control — without hits, every probe below is vacuously green."""
    names = _names(client, glob)
    assert names, f"{glob!r} matched nothing: set EPICS_MCP_LIVE_CF_GLOB to a glob that hits"
    assert len(names) < _MAX_RESULTS, (
        "glob hits the cap — pick a narrower one, or counts compare the cap"
    )


def test_impossible_glob_matches_nothing(client: ChannelFinderClient) -> None:
    """The negative control: proves the server filters at all."""
    assert _names(client, "ZZZ-no-such-channel*") == []


def test_glob_is_case_insensitive(client: ChannelFinderClient, glob: str) -> None:
    """Differential: the same glob in three cases must return the IDENTICAL set.

    Both guards are inline on purpose. The positive control lives in a NEIGHBOURING test, and
    pytest runs that one independently — so a glob that matches nothing left this comparing
    ``[] == [] == []``: green, and proving nothing about case at all. The cap check has to be here
    too: three results truncated at the same cap are equal for a reason that has nothing to do
    with case (200-vs-200 is the cap, not evidence).
    """
    as_typed = _names(client, glob)
    assert as_typed, f"{glob!r} matched nothing — a case comparison of empty sets proves nothing"
    assert len(as_typed) < _MAX_RESULTS, (
        f"{glob!r} hits the cap ({_MAX_RESULTS}) — the three sets would compare the cap, not case"
    )
    assert _names(client, glob.lower()) == _names(client, glob.upper()) == as_typed


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


# --- S11 schema anchor: the strict client schema, pinned against the REAL payload ---


def test_live_channels_satisfy_the_strict_schema(client: ChannelFinderClient, glob: str) -> None:
    """S11 anchor: every channel record carries a non-empty string ``name`` (measured
    2026-07-16) — the client now RAISES on a nameless/non-dict record instead of minting
    ``ChannelInfo(name="")``, so this run passing pins the premise against the real registry."""
    channels = client.find_channels(glob, max_results=20)
    assert channels, (
        "positive control not met: the fixture glob matched nothing — the schema anchor cannot "
        "pin anything. Check EPICS_MCP_LIVE_CF_GLOB."
    )
    assert all(isinstance(channel["name"], str) and channel["name"] for channel in channels)


# --- MA-2 filter controls: the property/tag filters the tool description marks UNVERIFIED until a
# --- live probe. Differential (positive + negative controls), all on ``pvStatus`` — a SURFACED,
# --- always-allowlisted property, so these need no §8 allowlist override to run. Counts assert via
# --- the uncapped ``/count`` endpoint; any pulled list is checked against the cap itself (there is
# --- no ``capped`` flag on ``find_channels`` — truncation shows only as ``len == _MAX_RESULTS``.


def test_filter_positive_is_a_strict_subset(client: ChannelFinderClient, glob: str) -> None:
    """Positive control: a value filter returns a NON-EMPTY subset and every member actually
    carries that value. Without the member check, ``has_properties`` being silently dropped would
    still pass — it would return the whole glob, which is also non-empty. ``pvStatus`` is surfaced,
    so the value is verifiable on each record."""
    total = client.count_channels(glob)
    matches = client.find_channels(
        glob, max_results=_MAX_RESULTS, has_properties={"pvStatus": "Inactive"}
    )
    assert 0 < len(matches) < _MAX_RESULTS, (
        f"{glob!r} filtered on pvStatus=Inactive returned {len(matches)} channels — need a "
        "non-empty, un-capped subset (pick a glob that straddles Active and Inactive)"
    )
    assert len(matches) <= total, "a filtered subset cannot exceed the unfiltered count"
    assert all(c["properties"].get("pvStatus") == "Inactive" for c in matches), (
        "has_properties was ignored: a returned channel does not carry the value it filtered on"
    )


def test_filter_absence_partitions_the_glob(client: ChannelFinderClient, glob: str) -> None:
    """Absence control: ``lacks_properties=[p]`` and ``has_properties={p: '*'}`` are exact
    complements, so their counts must sum to the unfiltered total. Uses the uncapped ``/count``
    endpoint (no cap guard needed) and proves ``lacks_properties`` does not silently broaden."""
    total = client.count_channels(glob)
    lacking = client.count_channels(glob, lacks_properties=["pvStatus"])
    having = client.count_channels(glob, has_properties={"pvStatus": "*"})
    assert lacking + having == total, (
        f"lacks({lacking}) + has-present({having}) != total({total}) — the absence filter does "
        "not partition the glob"
    )
    # No member of the 'lacks' set carries the property. Vacuously true when ``lacking == 0``,
    # but non-vacuous the moment a glob includes property-less channels.
    without = client.find_channels(glob, max_results=_MAX_RESULTS, lacks_properties=["pvStatus"])
    assert len(without) < _MAX_RESULTS
    assert all("pvStatus" not in c["properties"] for c in without)


def test_filter_negation_excludes_that_value_only(client: ChannelFinderClient, glob: str) -> None:
    """Negation control: ``not_property_values={p: v}`` drops the channels whose ``p == v`` while
    keeping the other-valued ones. Pull a concrete ``Active`` channel, then assert it is ABSENT
    from the negated result AND that an ``Inactive`` channel SURVIVES — the two-sided proof a
    single 'it returned fewer' count could not give."""
    active = client.find_channels(
        glob, max_results=_MAX_RESULTS, has_properties={"pvStatus": "Active"}
    )
    assert active and len(active) < _MAX_RESULTS, (
        f"{glob!r} has no un-capped Active channel to negate against — pick a glob that has one"
    )
    an_active_name = active[0]["name"]

    negated = client.find_channels(
        glob, max_results=_MAX_RESULTS, not_property_values={"pvStatus": "Active"}
    )
    assert len(negated) < _MAX_RESULTS
    negated_names = {c["name"] for c in negated}
    assert an_active_name not in negated_names, (
        "not_property_values did not exclude the channel whose value it negated"
    )
    assert any(c["properties"].get("pvStatus") == "Inactive" for c in negated), (
        "the negation dropped the non-Active channels too — it is not a value negation"
    )


def test_count_only_agrees_with_the_list(client: ChannelFinderClient, glob: str) -> None:
    """count_only cross-check: the ``/count`` endpoint and the pulled list must agree on the SAME
    filtered query. Kept under the cap so the list is complete — otherwise this would measure the
    cap, not the count."""
    listed = client.find_channels(
        glob, max_results=_MAX_RESULTS, has_properties={"pvStatus": "Active"}
    )
    assert len(listed) < _MAX_RESULTS, "list hit the cap — count_only would legitimately disagree"
    counted = client.count_channels(glob, has_properties={"pvStatus": "Active"})
    assert counted == len(listed), (
        f"count_only({counted}) != len(find_channels)({len(listed)}) for the same filter"
    )


def test_impossible_value_collapses_to_zero(client: ChannelFinderClient, glob: str) -> None:
    """Negative control on the VALUE axis: an impossible ``pvStatus`` value must collapse to 0, not
    broaden. This is exactly why '0 matches != unknown property' holds — an unknown VALUE and an
    unknown property NAME both narrow to nothing, indistinguishably."""
    impossible = {"pvStatus": "ZZZ-no-such-status-XYZ"}
    assert client.find_channels(glob, max_results=_MAX_RESULTS, has_properties=impossible) == []
    assert client.count_channels(glob, has_properties=impossible) == 0
