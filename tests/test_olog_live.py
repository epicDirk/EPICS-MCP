"""Live probes for the Olog search time window — the class of bug a mock CANNOT catch.

Opt-in: ``pytest -m live`` with ``EPICS_MCP_OLOG_URL`` pointing at a reachable Olog.

WHY THESE EXIST
---------------
The time window was broken from the day it shipped and no offline test could have known: the drop
happens on the SERVER. A mock only ever sees what the client SENT, never what the server HONOURED,
so a fully-mocked suite reports green while `search_logbook` answers "no entries" to a window
containing everything. Two reviewers read the Java and drew opposite (both wrong) conclusions; only
execution settled it.

The assertions are DIFFERENTIAL — the same window expressed several ways must give the same answer —
rather than exact counts, so they hold against any Olog with any data. The windows are fixed
constants, not derived from the clock, so a run is reproducible.
"""

from __future__ import annotations

import os
from collections import Counter
from datetime import UTC, datetime
from typing import Any

import pytest

from epics_pv_mcp.services._http import basic_auth_header
from epics_pv_mcp.services._time_window import TimeWindowFormatError
from epics_pv_mcp.services.olog_client import OlogClient
from epics_pv_mcp.services.olog_exceptions import OlogError, OlogFilterValueError
from epics_pv_mcp.services.redact import FREETEXT_WITHHELD

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.environ.get("EPICS_MCP_OLOG_URL"),
        reason="live Olog probe: set EPICS_MCP_OLOG_URL (e.g. the phoebus-olog compose)",
    ),
]

# A window wide enough to contain any sandbox entry, expressed two ways. Fixed, not clock-derived.
_WIDE_ISO = ("2020-01-01T00:00:00Z", "2030-01-01T00:00:00Z")
_WIDE_WALL = ("2020-01-01 00:00:00.000", "2030-01-01 00:00:00.000")
# A window that predates any sandbox entry — the negative control.
_PAST = ("2020-01-01T00:00:00Z", "2020-01-02T00:00:00Z")

#: The message for a failed positive control. It must NOT claim "your data is stale": an empty
#: reference also comes from auth, a wrong URL, a client regression or a changed payload — and a
#: ``total`` of ``None`` means the count could not be read at all, which is NOT "zero entries".
_NO_REFERENCE = (
    "positive control not met; the comparison is not evaluable. Check fixture, config, backend "
    "and client — an empty (or unreadable) reference cannot tell a honoured window from a "
    "dropped one."
)


@pytest.fixture
def client() -> OlogClient:
    url = os.environ["EPICS_MCP_OLOG_URL"]
    return OlogClient(url, timeout=10.0)


@pytest.fixture
def whole_client() -> OlogClient:
    """A client that may see free text — for probes that must READ a title to derive a probe word.

    Declaring test data is not enough on its own: the client un-redacts only when the URL is ALSO
    loopback, so pointing this suite at a real server keeps the free text withheld and the probes
    that need it skip rather than leak."""
    url = os.environ["EPICS_MCP_OLOG_URL"]
    return OlogClient(url, timeout=10.0, assume_test_data=True)


def _hits(client: OlogClient, start: str, end: str) -> int | None:
    return client.search_logbook(start=start, end=end, size=200)[2]


def _iso_from_ms(epoch_ms: int) -> str:
    """Epoch milliseconds → the ISO-Z form the search takes. Derived from data, never the clock."""
    return datetime.fromtimestamp(epoch_ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_iso_window_is_honoured(client: OlogClient) -> None:
    """THE regression. Sent raw, this exact window returned 0 of 9 entries — a plausible empty
    answer, not an error. It must now return the logbook's actual content."""
    total = _hits(client, *_WIDE_ISO)
    assert total, "an ISO window spanning 2020-2030 returned nothing — the window is being dropped"


def test_iso_and_wall_clock_windows_agree(client: OlogClient) -> None:
    """The decisive differential: the same window in two notations must give the same answer.

    Stronger than 'ISO returns something' — it pins that ISO is honoured FAITHFULLY, not merely
    that the request survives.

    The guard is load-bearing: ``_hits`` returns ``int | None``, so without it this was green both
    on an empty logbook (``0 == 0``) and on an unreadable count (``None == None``) — two very
    different facts, neither of them "the notations agree".
    """
    iso, wall = _hits(client, *_WIDE_ISO), _hits(client, *_WIDE_WALL)
    assert iso, _NO_REFERENCE
    assert wall is not None, _NO_REFERENCE
    assert iso == wall


def test_relative_window_agrees_with_absolute(client: OlogClient) -> None:
    """A wide relative amount must find what a wide absolute window finds.

    HONEST LIMIT — read before trusting this: the two windows are NOT the same instant ('3650
    days' is clock-relative, the absolute one is fixed) and on a young logbook both simply contain
    EVERY entry, which is why they agree. So this pins that a relative amount PARSES and is not
    rejected — it cannot see a relative window that is dropped entirely, because searching
    everything returns the same total. The boundary itself is pinned by
    test_narrow_window_discriminates; that is where a dropped window goes red.
    """
    relative = client.search_logbook(start="3650 days", size=200)[2]
    absolute = _hits(client, *_WIDE_ISO)
    assert absolute, _NO_REFERENCE
    assert relative is not None, _NO_REFERENCE
    assert relative == absolute


def test_past_window_returns_nothing(client: OlogClient) -> None:
    """The negative control, and it is not optional: every test above would ALSO pass if we
    silently dropped the window entirely and searched everything. This is what proves the window
    is applied rather than ignored."""
    assert _hits(client, *_PAST) == 0


def test_narrow_window_discriminates(client: OlogClient) -> None:
    """A window starting at the NEWEST entry must exclude the older ones — the filter is
    time-accurate, not merely present. This is the test that goes red on a dropped window.

    Two things here are deliberate:

    * ``<`` not ``<=``. The old ``narrow <= wide`` was satisfied by a window that was ignored
      ENTIRELY (both sides return the whole logbook, and ``9 <= 9`` is green) and by an empty
      logbook (``0 <= 0``). It asserted nothing a dropped filter would violate.
    * The boundary is DERIVED FROM THE DATA, not guessed. A hardcoded date passes or fails by luck
      of the fixture, and the old ``"1 hour"`` conflated two causes: a window that is ignored and a
      logbook whose entries are all younger than an hour look identical.
    """
    entries, _, wide = client.search_logbook(start=_WIDE_ISO[0], end=_WIDE_ISO[1], size=200)
    assert wide, _NO_REFERENCE

    stamps = [int(str(e["createdDate"])) for e in entries if e.get("createdDate") is not None]
    assert stamps, "entries carry no createdDate — cannot derive a boundary from the data"
    narrow = _hits(client, _iso_from_ms(max(stamps)), _WIDE_ISO[1])
    assert narrow is not None, _NO_REFERENCE
    assert narrow < wide, (
        f"a window starting at the newest entry returned {narrow} of {wide} — either the window is "
        "ignored, or the newest timestamp is shared by every entry the window would have excluded "
        "(a fixture with no time spread cannot discriminate)"
    )


def test_year_amount_rejected_before_any_request(client: OlogClient) -> None:
    """Olog cannot subtract years from a point in time. Left to the server this is a 400 that an
    anonymous read only ever sees as 401 ('unauthorized') — so it is refused here instead."""
    with pytest.raises(TimeWindowFormatError, match="days or weeks"):
        client.search_logbook(start="1 year")


def _order(client: OlogClient, sort: str) -> list[object]:
    return [e.get("id") for e in client.search_logbook(size=5, sort=sort)[0]]


def test_sort_orders_the_page(client: OlogClient) -> None:
    """The positive control: without it, the probe below could not tell 'sort was applied' from
    'sort does nothing at all'."""
    down, up = _order(client, "down"), _order(client, "up")
    assert down, "no entries — the sort probes below would prove nothing"
    assert down != up


def test_unreadable_sort_silently_reverses_on_the_server(client: OlogClient) -> None:
    """The reason the tool constrains sort to a Literal, measured rather than assumed.

    Olog does not reject an order it cannot read: anything that is not 'down'/'desc' becomes
    ASC — the REVERSE of our documented default — behind a 200 and a well-formed page. 'newest'
    is the sharpest case: the word an operator would reach for to mean newest-first returns
    oldest-first.

    This measures the SERVER, not us: the tool layer rejects these values before they are sent
    (Literal['down','up']), so this asks the client directly. Should a future Olog reject them
    itself, this test goes red and says the constraint may be relaxed — a refusal is only correct
    while its premise holds.

    The inline positive control is not decoration. This test is cited in CLAUDE.md as the model of
    "pin the premise so it goes red when the server improves" — and it could not go red at all: on
    an empty logbook every ``_order`` is ``[]`` and ``[] == []`` passes. The control lived in the
    NEIGHBOURING test, which pytest runs independently, so a red neighbour never stopped this one
    from reporting green. A guard that cannot fail is not a guard.
    """
    down, up = _order(client, "down"), _order(client, "up")
    assert down, "no entries — this probe cannot distinguish any sort order"
    assert down != up, "sort has no effect at all — an unreadable value cannot be shown to collapse"
    for unreadable in ("garbage", "newest", "asc", ""):
        assert _order(client, unreadable) == up, f"{unreadable!r} no longer collapses to ASC"


# --- S11 schema anchors: the strict client schema, pinned against the REAL payload ---


def test_live_payloads_satisfy_the_strict_schema(client: OlogClient) -> None:
    """S11 anchor: the strict response schema (search wrapper, entries carry ``id``, listings
    carry ``name``) was DERIVED from this live payload (measured 2026-07-16, Olog 6.x). This pins
    the premise so it goes red if a server stops matching — the schema is then re-MEASURED, never
    loosened blindly. A mock cannot carry this burden: it only ever knows what we assumed."""
    entries, _capped, total = client.search_logbook(start=_WIDE_ISO[0], end=_WIDE_ISO[1], size=5)
    assert entries, _NO_REFERENCE
    assert all("id" in entry for entry in entries)  # the measured anchor field
    assert total is None or isinstance(total, int)  # wrapper hitCount readable (or absent)
    fetched = client.get_log_entry(str(entries[0]["id"]))
    assert fetched is not None and "id" in fetched
    logbooks = client.list_logbooks()
    assert logbooks and all(isinstance(name, str) and name for name in logbooks)
    tags = client.list_tags()
    assert all(isinstance(name, str) and name for name in tags)


# --- OA2/OA5: the level + title facets, with the control that makes a probe MEAN something ---
#
# Olog's parameter switch ends in `default: // Unsupported search parameters are ignored`, so a
# filter it does not understand is DROPPED, not rejected — and the answer is a well-formed 200 with
# the unfiltered set. "The filter returned results" therefore proves nothing on its own. Every probe
# below carries three things: a positive control (must match), a negative control (must not), and
# the IGNORED-PARAMETER control that shows what a dropped filter actually looks like.


def _raw_hit_count(client: OlogClient, params: dict[str, str]) -> int | None:
    """``hitCount`` for an arbitrary query — deliberately bypassing ``search_logbook``.

    The client only ever sends parameters it knows, so it cannot express the one query this file
    needs most: a filter under a name the server does NOT know. Reaching past the public method is
    the point here, not a shortcut."""
    data = client._get(f"{client.base_url}/logs/search", params)
    return data.get("hitCount") if isinstance(data, dict) else None


def _levels_in_fixture(client: OlogClient) -> Counter[str]:
    """How many entries carry each level, read from the unfiltered set. ``level`` is a technical
    field and survives redaction, so this works in either output posture."""
    entries, _capped, _total = client.search_logbook(size=200)
    return Counter(str(entry["level"]) for entry in entries if entry.get("level"))


def test_level_filter_is_honoured_by_the_server(client: OlogClient) -> None:
    """The differential probe behind the documented level promise (CLAUDE.md's hard rule).

    Positive: filtering by a level present in the fixture returns exactly the entries carrying it.
    Negative: the entries of a DIFFERENT level are absent — without this, a dropped filter would
    look identical. Control: the same value under an unknown parameter name comes back UNFILTERED,
    which is what "silently ignored" looks like, so the positive result cannot be explained that
    way."""
    counts = _levels_in_fixture(client)
    if len(counts) < 2:
        pytest.skip(f"fixture carries {len(counts)} distinct level(s); need 2 to discriminate")
    (level, count), (other, other_count) = counts.most_common(2)

    entries, _capped, _total = client.search_logbook(level=level, size=200)
    assert entries, _NO_REFERENCE
    assert {str(entry["level"]) for entry in entries} == {level}  # negative: nothing else got in
    assert len(entries) == count  # positive: everything carrying it got out

    unfiltered = _raw_hit_count(client, {"size": "200"})
    # PRECONDITION, not an assertion about the filter: the per-level counts are read from the same
    # single page as `unfiltered`, so they only add up while the fixture fits in one page. Stated
    # explicitly (rather than left as a bare equality that would start failing for an unrelated
    # reason once the sandbox outgrows `size`) — and skipped, not failed, because a fixture too
    # large to page in one go says nothing about whether the level filter works.
    if unfiltered is None or unfiltered != sum(counts.values()):
        pytest.skip(
            f"fixture no longer fits one page ({unfiltered} total vs {sum(counts.values())} "
            "counted) — the arithmetic control needs a single-page fixture"
        )
    assert unfiltered == count + other_count + sum(
        n for lvl, n in counts.items() if lvl not in (level, other)
    )
    ignored = _raw_hit_count(client, {"size": "200", "notaparameter": level})
    assert ignored == unfiltered, (
        "the ignored-parameter control did not come back unfiltered — this server may now reject "
        "unknown parameters, which would make the control meaningless (re-measure before trusting)"
    )
    assert len(entries) != unfiltered, (
        "the level filter narrowed nothing — indistinguishable from having been dropped"
    )


def test_level_filter_is_case_insensitive(client: OlogClient) -> None:
    """The index analyzer lowercases, so the filter is case-insensitive. Documented in the tool
    description, therefore pinned here rather than assumed from reading the mapping."""
    counts = _levels_in_fixture(client)
    if not counts:
        pytest.skip("fixture carries no levelled entries")
    level, count = counts.most_common(1)[0]
    for spelling in (level.lower(), level.upper()):
        entries, _capped, _total = client.search_logbook(level=spelling, size=200)
        assert len(entries) == count, f"{spelling!r} did not match as {level!r}"


def test_unknown_level_is_silently_zero_not_an_error(client: OlogClient) -> None:
    """S8, measured: an unrecognised level is NOT rejected — the server answers 200 with 0 hits.

    This is why ``list_log_levels`` exists and why an empty level-filtered result is annotated: at
    the wire there is no difference between "this level does not exist" and "no entries have this
    level", so a typo reads as a fact about the logbook. Goes red if a future Olog starts validating
    the value — the annotation could then be simplified.

    The non-emptiness precondition is load-bearing, not decoration: on an empty logbook ``entries ==
    []`` holds no matter what the server does, so without it this test would be green against an
    Olog that rejects the value loudly — the exact ``[] == []`` class already documented for
    ``test_unreadable_sort_silently_reverses_on_the_server``."""
    reference, _capped, _total = client.search_logbook(size=200)
    assert reference, _NO_REFERENCE
    entries, _capped, total = client.search_logbook(level="NotAConfiguredLevel", size=200)
    assert entries == []
    assert total in (0, None)


def test_title_filter_is_honoured_and_matches_whole_words(whole_client: OlogClient) -> None:
    """``title`` filters as named, case-insensitively, and matches whole WORDS — not substrings.

    The fragment is DERIVED, not hard-coded: a strict prefix of a real title word that is itself not
    a word anywhere in the fixture. That makes the substring claim decidable rather than likely —
    bare, it must find nothing; wildcarded, it must find the word it was taken from. (Nothing from
    the fixture is committed; titles are read at runtime.)

    Takes the WHOLE-mode client on purpose: the probe word has to be read out of a real title, and
    the default posture WITHHOLDS title free text. Deriving from a redacted title silently probes
    the withheld-placeholder text instead — which matches nothing and fails the positive control
    (measured while writing this test).

    Worth stating because it is the OPPOSITE of ``find_channels``, whose bare value is an anchored
    substring glob — copying that wording over would have been a false documented promise."""
    entries, _capped, _total = whole_client.search_logbook(size=200)
    assert entries, _NO_REFERENCE
    titles = [
        str(entry["title"])
        for entry in entries
        if isinstance(entry.get("title"), str) and entry["title"] != FREETEXT_WITHHELD
    ]
    if not titles:
        pytest.skip("titles are withheld here (not a declared loopback sandbox) — no probe word")
    words = {word for title in titles for word in title.lower().split() if word.isalnum()}

    # Sorted by (-length, word) so a length tie breaks on the WORD, not on set-iteration order —
    # which depends on PYTHONHASHSEED and would make the chosen probe differ between runs of the
    # same fixture, contradicting this file's own reproducibility promise.
    probe = next((w for w in sorted(words, key=lambda w: (-len(w), w)) if len(w) >= 4), None)
    if probe is None:
        pytest.skip("no title word long enough to derive a fragment from")
    fragment = probe[:-1]
    if fragment in words:
        pytest.skip("the derived fragment is itself a title word; cannot decide substring-ness")

    def hits(value: str) -> int:
        return len(whole_client.search_logbook(title=value, size=200)[0])

    assert hits(probe), "positive control: the word must match"
    assert hits(probe.upper()) == hits(probe), "must be case-insensitive"
    assert hits("zzznosuchtitleword") == 0  # negative control
    assert hits(fragment) == 0, (
        "a bare word FRAGMENT matched — title is not the whole-word matcher the tool description "
        "promises (and is not the anchored substring glob of find_channels either); re-measure"
    )
    assert hits(f"{fragment}*") >= hits(probe), "the wildcard must find at least the word itself"


def test_documented_combination_semantics_hold(
    client: OlogClient, whole_client: OlogClient
) -> None:
    """Pins the three COMBINATION promises the tool description makes, each of which was documented
    from one probe and nothing else — an unpinned promise is one server upgrade from being a lie.

    * ``level`` ORs over ``,`` ``;`` ``|`` — all three separators, not just the comma that got
      measured first: two levels joined must return the union of their individual counts.
    * several ``title`` words are AND-ed — two words from the SAME title must return that title's
      count, two words from DIFFERENT titles must return nothing.
    * a quoted ``title`` matches the phrase IN ORDER — reversing the words must return nothing.
    """
    counts = _levels_in_fixture(client)
    nonzero = [name for name, count in counts.items() if count]
    if len(nonzero) < 2:
        pytest.skip("need two levels with entries to prove the OR")
    first, second = nonzero[0], nonzero[1]
    union = counts[first] + counts[second]
    for separator in (",", ";", "|"):
        joined = client.search_logbook(level=f"{first}{separator}{second}", size=200)[0]
        assert len(joined) == union, f"{separator!r} did not OR the two levels"

    # The title half needs the WHOLE-mode client: the default posture withholds title free text,
    # and a probe word derived from the withheld placeholder matches nothing (measured twice
    # while writing this file — it presents as a skip or a failed positive control, never as a
    # real result).
    entries, _capped, _total = whole_client.search_logbook(size=200)
    titles = [
        str(entry["title"])
        for entry in entries
        if isinstance(entry.get("title"), str) and entry["title"] != FREETEXT_WITHHELD
    ]
    pair = next((t.lower().split() for t in sorted(titles) if len(t.split()) >= 2), None)
    if pair is None:
        pytest.skip("no multi-word title readable in this posture — cannot probe AND/phrase")

    def hits(value: str) -> int:
        return len(whole_client.search_logbook(title=value, size=200)[0])

    assert hits(f"{pair[0]} {pair[1]}") == hits(f'"{pair[0]} {pair[1]}"'), (
        "AND-ing two adjacent words and quoting them as a phrase disagree on a title that "
        "contains them in that order — one of the two documented rules is wrong"
    )
    assert hits(f"{pair[0]} zzznosuchtitleword") == 0, "several title words are not AND-ed"
    assert hits('"zzznosuch wordpairzzz"') == 0  # negative control for the phrase form


def test_blank_filters_are_refused_before_any_request(client: OlogClient) -> None:
    """Pins BOTH halves of the asymmetry the guard exists for — read straight off the server, so it
    goes red if Olog ever starts treating the two fields alike.

    A blank ``level`` matches nothing (0 hits, reading exactly like "no such entries"); a blank
    ``title`` is dropped entirely (the UNFILTERED count, presented as a filtered result). Neither is
    "no filter", and because they disagree, neither behaviour can be inferred from the other — hence
    the client refuses both before sending."""
    unfiltered = _raw_hit_count(client, {"size": "200"})
    assert unfiltered, _NO_REFERENCE
    assert _raw_hit_count(client, {"size": "200", "level": ""}) == 0
    assert _raw_hit_count(client, {"size": "200", "title": ""}) == unfiltered

    for field in ("level", "title"):
        filters: dict[str, Any] = {field: "  ", "size": 200}
        with pytest.raises(OlogFilterValueError):
            client.search_logbook(**filters)


def test_levels_listing_satisfies_the_strict_schema(client: OlogClient) -> None:
    """OA2 anchor: ``GET /levels`` returns ``{name, defaultLevel}`` records (measured 2026-07-19).
    Pins the premise ``_level_list`` was written against, so a server that changes shape goes red
    instead of quietly losing the default."""
    names, default, _note = client.list_log_levels()
    assert names and all(isinstance(name, str) and name for name in names)

    # Differential, not a constant: read the flag straight off the wire and require the extracted
    # default to agree with it. `default is None or default in names` would be satisfied by None
    # alone, so it stays green exactly when the shape change it exists to catch happens (a renamed
    # or dropped `defaultLevel` yields None and the anchor never notices). Deriving the expectation
    # from the payload keeps this true for any Olog with any seed data — including the two-defaults
    # case the seed file ships, where withholding is the CORRECT answer.
    raw = client._get(f"{client.base_url}/levels", {})
    assert isinstance(raw, list)
    flagged = [item["name"] for item in raw if item.get("defaultLevel") is True]
    assert [str(item["name"]) for item in raw] == names
    assert default == (flagged[0] if len(flagged) == 1 else None), (
        f"extracted default {default!r} disagrees with the wire, which flags {flagged!r} — "
        "the 'defaultLevel' premise this anchor pins no longer holds"
    )

    # NOT "every entry's level is listed" — that assertion used to stand here and was over-strict:
    # its own message already explained why it cannot hold ("a level can be deleted while entries
    # keep the string"), and that is precisely the premise the READ side is built on. Measured
    # 2026-07-20: an entry written with an unlisted level ("Urgnet") is stored verbatim, so the
    # unlisted-level state is not a sandbox defect but the normal case the code is designed around —
    # and the WRITE-side guard exists exactly because the server allows it (see
    # test_server_does_not_validate_a_written_level, which deliberately creates such an entry).
    #
    # What is still worth pinning: the listing must describe the levels entries ACTUALLY use, i.e.
    # the fixture must exercise at least one listed level. A listing that shares nothing with the
    # corpus means the anchor is measuring the wrong server or an empty fixture.
    counts = _levels_in_fixture(client)
    assert counts, _NO_REFERENCE
    assert set(counts) & set(names), (
        f"no entry carries any listed level: entries use {sorted(counts)}, /levels lists "
        f"{sorted(names)} — the listing and the corpus do not belong to the same server"
    )


def test_unknown_id_error_is_loud_not_a_not_found(client: OlogClient) -> None:
    """S16(b) premise pin, measured 2026-07-16: a real Olog answers **401** (not the documented
    404) for an unknown id on this anonymous read path — its error dispatch requires auth. The
    loud error is the correct surface (``found:false`` stays reserved for a genuine 404, which
    this server never emits here). Goes red if a future Olog starts answering 404 — then the
    documented contract finally matches the wire and this pin gets updated."""
    with pytest.raises(OlogError):
        client.get_log_entry("99999999")


# ======================================================================================
# The premise behind the WRITE-side level guard (OQ1) — pinned so it goes red if Olog changes
# ======================================================================================


@pytest.mark.skipif(
    not (
        os.environ.get("EPICS_MCP_ALLOW_OLOG_WRITE", "").lower() == "true"
        and os.environ.get("EPICS_MCP_OLOG_WRITE_LOGBOOKS")
        and os.environ.get("EPICS_MCP_OLOG_WRITE_USER")
    ),
    reason=(
        "pins the server behaviour that justifies the write-side level refusal: needs a WRITABLE "
        "loopback Olog (EPICS_MCP_ALLOW_OLOG_WRITE + _WRITE_LOGBOOKS + write creds)"
    ),
)
def test_server_does_not_validate_a_written_level() -> None:
    """The PREMISE of the OQ1 guard, measured instead of read off the Java source.

    ``create_log_entry`` / ``update_log_entry`` refuse an unknown or blank ``level``. That refusal
    is only correct while the server itself does NOT validate — otherwise the client would be
    turning a server-side 400 into a made-up story, or worse, refusing something the server would
    have accepted meaningfully.

    CLAUDE.md is explicit that reading ``LevelsResource``/``LogResource`` is NOT a measurement here
    ("wrong five times in one week"), and that a refusal's premise must be pinned live "so it goes
    red when the server improves". This is that pin. If a future Olog starts rejecting an unknown
    level, this test fails FIRST and the three tool descriptions + the operator guide get corrected
    instead of quietly lying.

    Deliberately bypasses the service layer and drives the client directly — the service is exactly
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
        assume_test_data=True,
    )
    known, _default, _note = client.list_log_levels()
    bogus = "Urgnet"  # a typo of a real level, so it cannot collide with a site's vocabulary
    assert bogus not in known, "pick a value the server really does not know"

    # (1) an UNKNOWN level is accepted and stored verbatim — no 400, no coercion to the default
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
        "the server VALIDATES a written level now — the write-side refusal's premise is gone; "
        "update the level descriptions in server.py and the operator guide"
    )

    # ...and the entry is therefore invisible to every valid level filter (the actual damage)
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
        "a blank level no longer clears the field — the blank-level refusal needs a rethink"
    )
