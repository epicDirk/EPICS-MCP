"""Guards for the reach field (GB-64): where an answer came from, on the answer itself.

The defect this field removes is a claim about reach made without a measurement, so these
guards are written to catch the field itself becoming such a claim. Three of them are about
that and nothing else: :func:`test_the_field_never_carries_an_address`,
:func:`test_a_null_environment_is_not_confined` and
:func:`test_nothing_here_opens_a_socket`.
"""

from __future__ import annotations

import json
import pathlib
import socket
from collections.abc import Mapping
from typing import Any, cast

import pytest

from epics_mcp.config import EpicsConfig
from epics_mcp.provenance import (
    REACH_KEY,
    _scope_of_live,
    _scope_of_url,
    reach_of,
    scope_of,
    with_reach,
)

#: The repository's own loopback lane (``presets._LOOPBACK_SEARCH``), spelled out rather than
#: imported: these tests are about what the classifier answers for a given environment, so the
#: environment is an input of the test, not a dependency of it.
LOOPBACK_ENV: Mapping[str, str] = {
    "EPICS_PVA_AUTO_ADDR_LIST": "NO",
    "EPICS_CA_AUTO_ADDR_LIST": "NO",
    "EPICS_PVA_ADDR_LIST": "127.0.0.1",
    "EPICS_CA_ADDR_LIST": "127.0.0.1",
    "EPICS_PVA_NAME_SERVERS": "127.0.0.1:5075",
}

#: The same lane with one entry pointed off this machine. One difference from LOOPBACK_ENV, so
#: a test comparing the two is comparing that difference and not two unrelated environments.
FACILITY_ENV: Mapping[str, str] = {
    **LOOPBACK_ENV,
    "EPICS_PVA_ADDR_LIST": "gateway.example.org",
}


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("", "not-configured"),
        ("http://localhost:8080/ChannelFinder", "loopback-only"),
        ("http://127.0.0.1:17665", "loopback-only"),
        ("http://[::1]:8080/Olog", "loopback-only"),
        ("https://archiver.example.org:17665", "beyond-loopback"),
        # A NAME is never resolved, the same fail-closed posture epics_address takes. This one
        # is the case that matters: DNS would map it to a loopback address on many machines,
        # and honouring that would claim a confinement decided by the resolver rather than by
        # the configuration.
        ("http://host.docker.internal:8080", "beyond-loopback"),
        # Nothing is proven about a value the parser cannot read, so nothing local is claimed.
        ("http://[not-an-ipv6/", "beyond-loopback"),
        ("::::", "beyond-loopback"),
    ],
)
def test_a_rest_url_is_classified_without_resolving_it(url: str, expected: str) -> None:
    """Provably red: make ``_scope_of_url`` fall back to ``loopback-only`` on an unreadable or
    unresolvable host, i.e. flip either of the two ``beyond-loopback`` returns in
    :func:`~epics_mcp.provenance._scope_of_url`."""
    assert _scope_of_url(url) == expected


def test_a_null_environment_is_not_confined() -> None:
    """The dangerous EPICS default, stated as a guard.

    An unset ``*_AUTO_ADDR_LIST`` means the subnet broadcast is ON, so a process with no EPICS
    variables at all still searches beyond this machine. A classifier that read "nothing
    configured" as "nothing reachable" would print the exact false all-clear this field exists
    to remove, and it would print it on the most common configuration of all.

    Provably red: make ``_scope_of_live`` return ``loopback-only`` when the environment is
    empty, or invert the ``loopback_only`` reading.
    """
    assert _scope_of_live({}) == "beyond-loopback"


def test_the_loopback_lane_and_the_facility_lane_are_told_apart() -> None:
    """The one distinction the whole field exists for: a local test rig against a real
    facility. Provably red: drop the ``EPICS_PVA_ADDR_LIST`` reading from the shared parser."""
    assert _scope_of_live(LOOPBACK_ENV) == "loopback-only"
    assert _scope_of_live(FACILITY_ENV) == "beyond-loopback"


def test_the_archiver_plane_is_classified_from_the_mgmt_url() -> None:
    """Not from ``archiver_retrieval_url``, and that is a decision rather than an omission.

    Every archiver tool gates on the mgmt URL and the retrieval URL falls back to it when
    unset, so classifying from the retrieval value would report a reach for a plane that is
    never used. ``epics-pv://health`` resolves the same pair the same way.

    Provably red: point the ``archiver`` row of ``scope_of`` at ``archiver_retrieval_url``.
    """
    cfg = EpicsConfig(archiver_url="", archiver_retrieval_url="https://retrieval.example.org:17668")
    assert scope_of("archiver", cfg, {}) == "not-configured"


def test_every_plane_answers_and_none_is_forgotten() -> None:
    """All six planes classify, so a plane added to the ``Plane`` alias without a row in
    ``scope_of`` is a KeyError here rather than a silently missing entry on the wire.

    Provably red: delete any row from the ``urls`` mapping in ``scope_of``.
    """
    cfg = EpicsConfig(
        channelfinder_url="http://localhost:8080/ChannelFinder",
        archiver_url="http://localhost:17665",
        alarm_url="http://localhost:8080",
        naming_url="http://localhost:8080",
        olog_url="http://localhost:8080/Olog",
    )
    field = reach_of(
        "live-pv",
        "channelfinder",
        "archiver",
        "alarm",
        "naming",
        "olog",
        cfg=cfg,
        environ=LOOPBACK_ENV,
    )
    assert set(field["planes"]) == {
        "live-pv",
        "channelfinder",
        "archiver",
        "alarm",
        "naming",
        "olog",
    }
    assert set(field["planes"].values()) == {"loopback-only"}


def test_the_field_never_carries_an_address() -> None:
    """The standing rule of this repository, as a guard rather than as prose.

    ``write_posture.PvWriteGateReport.search_reach_violations`` states it: the host-naming
    rendering is for an operator's own terminal, and anything shipping to a client projects
    the posture field by field and leaves that one out. This field ships to a client, so no
    host, no port and no environment VALUE may appear in it. Two planes make the rule
    load-bearing rather than tidy: the naming and logbook URLs are deliberately withheld even
    from ``epics-pv://config``, on the ground that they identify a facility.

    Every distinctive token of the configuration is searched for in the serialised field, so
    the guard does not depend on remembering which key might carry one.

    Provably red: put ``cfg.channelfinder_url`` (or the rendered search-path line from
    ``services.doctor._live_search_posture``) into the field.
    """
    cfg = EpicsConfig(
        channelfinder_url="http://channelfinder.example.org:8080/ChannelFinder",
        archiver_url="http://archiver.example.org:17665",
        alarm_url="http://alarm.example.org:8080",
        naming_url="http://naming.example.org",
        olog_url="http://olog.example.org/Olog",
    )
    serialised = json.dumps(
        reach_of(
            "live-pv",
            "channelfinder",
            "archiver",
            "alarm",
            "naming",
            "olog",
            cfg=cfg,
            environ=FACILITY_ENV,
        )
    )
    forbidden = [
        "channelfinder.example.org",
        "archiver.example.org",
        "alarm.example.org",
        "naming.example.org",
        "olog.example.org",
        "gateway.example.org",
        "17665",
        "8080",
        "5075",
        "127.0.0.1",
        # The environment VARIABLE names are not secret, but they are the shape the rejected
        # design shipped, so their absence is what distinguishes the two.
        "EPICS_PVA_ADDR_LIST",
        "EPICS_PVA_NAME_SERVERS",
    ]
    leaked = [token for token in forbidden if token in serialised]
    assert not leaked, f"the reach field leaked {leaked} onto the wire: {serialised}"


def test_the_field_says_it_was_not_probed() -> None:
    """``probed`` is the difference between this field and the defect it removes.

    Without it a caller reading ``beyond-loopback`` cannot tell a measured statement from a
    configured one, which is exactly the confusion GB-64 is about.

    Provably red: drop the key, or set it to True while nothing probes.
    """
    assert reach_of("live-pv", cfg=EpicsConfig(), environ=LOOPBACK_ENV)["probed"] is False


def test_nothing_here_opens_a_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    """The field must never make an answer slower or able to time out.

    Asserted by removing the ability to open one: any socket construction inside the call
    raises. A DNS lookup goes through ``socket`` as well, so this covers the resolution the
    classifier promises not to do, not merely a connect.

    ⚠️ Faked at the ``socket`` module rather than at a client class, the seam this repository's
    evidence discipline names: a class-level double would remove the very call it is meant to
    forbid, so the guard could pass while never executing anything.

    Provably red: make ``_scope_of_url`` call ``socket.gethostbyname`` before classifying, or
    build a p4p ``Context`` to read the search configuration back.
    """
    cfg = EpicsConfig(
        channelfinder_url="http://channelfinder.example.org:8080/ChannelFinder",
        olog_url="http://olog.example.org/Olog",
    )

    def _refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the reach field opened a socket")

    monkeypatch.setattr(socket, "socket", _refuse)
    monkeypatch.setattr(socket, "getaddrinfo", _refuse)
    monkeypatch.setattr(socket, "gethostbyname", _refuse)
    field = reach_of("live-pv", "channelfinder", "olog", cfg=cfg, environ=FACILITY_ENV)
    assert field["planes"]["channelfinder"] == "beyond-loopback"


@pytest.mark.asyncio
async def test_the_decorator_attaches_the_field_once_to_the_answer() -> None:
    """Once to the ANSWER, never once per value inside it.

    The batch envelope is the case that decides: attaching per entry would repeat one
    process-level fact up to ``max_batch_size`` times, and measured on a 100-PV read that
    doubles the payload.

    Provably red: move the attachment into ``services.epics_client._format_value``, or map it
    over ``result["results"]``.
    """

    @with_reach("live-pv")
    async def _batch() -> dict[str, object]:
        return {"results": [{"pv_name": "A"}, {"pv_name": "B"}], "errors": []}

    answer = await _batch()
    assert REACH_KEY in answer
    results = answer["results"]
    assert isinstance(results, list)
    assert all(REACH_KEY not in entry for entry in results)


@pytest.mark.asyncio
async def test_the_decorator_does_not_overwrite_a_tool_that_knows_better() -> None:
    """A tool computing a finer reach itself stays the authority on its own answer.

    ``discover_pvs`` is the real case: its concrete-name branch reads a live PV and its
    wildcard branch asks ChannelFinder, so a static plane would be wrong half the time.

    Provably red: drop the ``if REACH_KEY not in result`` condition in ``with_reach``.
    """

    own = {"planes": {"channelfinder": "loopback-only"}, "probed": False}

    @with_reach("live-pv")
    async def _knows_better() -> dict[str, object]:
        return {"total": 0, REACH_KEY: own}

    assert (await _knows_better())[REACH_KEY] is own


@pytest.mark.asyncio
async def test_the_decorator_preserves_the_wrapped_signature() -> None:
    """FastMCP builds the tool schema from the wrapped function, so ``functools.wraps`` is
    load-bearing rather than tidy: without it every decorated tool would advertise
    ``(*args, **kwargs)``.

    Provably red: drop the ``@functools.wraps(fn)`` line in ``with_reach``.
    """

    @with_reach("olog")
    async def _named(pv_name: str, timeout: float = 1.0) -> dict[str, object]:
        """A docstring that has to survive: it IS the wire description."""
        return {"pv_name": pv_name}

    assert _named.__name__ == "_named"
    assert _named.__doc__ is not None
    assert "IS the wire description" in _named.__doc__
    answer = await _named("VAC:PV")
    assert answer["pv_name"] == "VAC:PV"


# --- the field on the WIRE, driven through the registered tools -----------------------------
#
# The unit guards above prove the classifier and the decorator. They cannot prove the thing the
# whole item is about, which is whether the field actually REACHES a client. That question is
# only answerable through the registered tool, so these two drive it, with the one network seam
# faked and nothing else.


@pytest.mark.asyncio
async def test_a_single_read_carries_its_reach_to_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``get_pv_value`` is the tool the original incident went through.

    Faked at ``epics_mcp.tools.read.pv_get``, the module-namespace seam this repository's other
    tests for the same tool already use, so the only thing removed is the socket.

    Provably red: drop ``@with_reach("live-pv")`` from ``get_pv_value``.
    """
    from epics_mcp.server import mcp

    async def _fake_pv_get(pv_name: str, timeout: float | None = None) -> dict[str, object]:
        return {"pv_name": pv_name, "value": 42}

    monkeypatch.setattr("epics_mcp.tools.read.pv_get", _fake_pv_get)
    structured = cast(
        dict[str, Any],
        (await mcp.call_tool("get_pv_value", {"pv_name": "SIM:PS-01:Cur-RB"})).structured_content,
    )
    assert structured["value"] == 42
    reach = structured[REACH_KEY]
    assert set(reach["planes"]) == {"live-pv"}
    assert reach["planes"]["live-pv"] in {"loopback-only", "beyond-loopback"}
    assert reach["probed"] is False


@pytest.mark.asyncio
async def test_a_batch_read_carries_its_reach_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The envelope carries it; the entries do not.

    This is the cost decision as a guard rather than as a note in a commit message: measured,
    attaching per entry doubles a 100-PV answer, and the fact is identical for every entry
    anyway because it is a property of the process.

    Provably red: attach the field inside ``services.epics_client._format_value``, or map it
    over the ``results`` list.
    """
    from epics_mcp.server import mcp

    async def _fake_pv_get_batch(
        names: list[str], timeout: float | None = None
    ) -> dict[str, object]:
        return {"results": [{"pv_name": n, "value": 1} for n in names], "errors": []}

    monkeypatch.setattr("epics_mcp.tools.read.pv_get_batch", _fake_pv_get_batch)
    structured = cast(
        dict[str, Any],
        (
            await mcp.call_tool("get_pvs", {"pv_names": ["SIM:PS-01:Cur-RB", "SIM:PS-02:Cur-RB"]})
        ).structured_content,
    )
    assert REACH_KEY in structured
    assert len(structured["results"]) == 2
    assert all(REACH_KEY not in entry for entry in structured["results"])


def test_the_server_header_names_the_field_and_not_the_command() -> None:
    """GB-64 (e). The header is the ONE text that arrives ungefragt on every connection, and it
    used to end this clause with "run epics-doctor", a console script the reader of that text
    cannot run. The incident behind GB-64 had that very sentence in context.

    Both halves are asserted. Naming the field is the easy one; the absence of the command is the
    load-bearing one, because re-adding it reads like a helpful extra and silently restores an
    unexecutable instruction on the one channel that cannot afford one.

    The position binding ("before you quote a value") is asserted too: the imperative was already
    there before this item, what was missing was WHEN.

    Provably red: restore the old clause in ``build_instructions``.
    """
    from epics_mcp.server import build_instructions

    for display_tools in (True, False):
        header = build_instructions(display_tools)
        assert "reach field" in header, f"display_tools={display_tools}: header lost the field"
        assert "before you quote a value" in header, (
            f"display_tools={display_tools}: header lost the position binding"
        )
        assert "epics-doctor" not in header, (
            f"display_tools={display_tools}: the header tells a client to run a console script"
        )


@pytest.mark.asyncio
async def test_every_registered_read_tool_carries_the_field() -> None:
    """The claim "every read answer carries a reach field" is now a MEASUREMENT, not a sentence.

    The post-build review found the first version making that claim in README.md and docs/tools.md
    while five registered read tools did not carry it, four of them the very cross-plane tools the
    field's mapping shape is justified by. A missing field is exactly as ambiguous as the missing
    reach was, so an incomplete surface does not half-solve the item, it re-creates it in a
    quieter place.

    Derived from the REGISTERED set rather than from a list here, so a read tool added later is
    red until it answers the question too. The write tools are excluded by name and the exclusion
    is stated: they mutate, and their gates report their own posture.

    Provably red: drop @with_reach from any read tool, or delete one of the setdefault calls in
    display_tools.py.
    """
    import importlib
    import inspect

    from epics_mcp import display_tools
    from epics_mcp.server import mcp

    # Only the tools whose plane is decided at CALL time live here; everything else answers
    # through the decorator on the wrapper. Kept explicit rather than searched, so adding a
    # second such tool is a deliberate line rather than a silent widening of this guard.
    _IMPL_MODULE = {"discover_pvs": "discover"}

    # Tools that WRITE, plus the one tool that reads nothing but its own packaged document.
    NOT_A_READ = {
        "set_pv_value",
        "create_log_entry",
        "reply_to_log",
        "add_log_attachment",
        "update_log_entry",
        "get_guide",
    }
    server_module = __import__("epics_mcp.server", fromlist=["server"])
    missing: list[str] = []
    for tool in await mcp.list_tools():
        if tool.name in NOT_A_READ:
            continue
        fn = getattr(server_module, tool.name, None) or getattr(display_tools, tool.name, None)
        assert fn is not None, f"{tool.name}: registered but not importable, cannot be checked"
        source = inspect.getsource(fn)
        # One level down as well: a tool whose plane depends on its argument sets the field in its
        # implementation rather than through the decorator (discover_pvs is the case), so a check
        # that read only the wrapper would report it missing when it is not.
        impl = getattr(
            importlib.import_module("epics_mcp.tools." + _IMPL_MODULE.get(tool.name, "read"))
            if tool.name in _IMPL_MODULE
            else None,
            "_" + tool.name,
            None,
        )
        if impl is not None:
            source += inspect.getsource(impl)
        if "with_reach" not in source and "reach_of" not in source:
            missing.append(tool.name)
    assert not missing, (
        f"these registered read tools answer without a reach field: {sorted(missing)}. An absent "
        "field is as ambiguous as the absent reach this item removed."
    )


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # ⛔ The case the post-build review found, and the reason this module no longer parses
        # URLs itself. urlparse splits at the LAST '@' and answers 127.0.0.1; urllib3, the parser
        # requests actually connects through, answers evil.example.org. The first version reported
        # loopback-only for a configuration whose socket leaves the machine, i.e. it produced the
        # false all-clear this whole module removes.
        (r"http://evil.example.org:8080\@127.0.0.1/Olog", "beyond-loopback"),
        # Userinfo alone, the same trap without a backslash: both parsers agree here, and the
        # answer is still the remote host.
        ("http://127.0.0.1@evil.example.org/Olog", "beyond-loopback"),
        # The other direction, where the shared parser normalises and the stdlib one did not: a
        # trailing FQDN dot IS localhost. Harmless when wrong, but it made the field disagree
        # with the Olog read redaction about the same URL, so a reader could not use one to
        # predict the other.
        ("http://localhost./Olog", "loopback-only"),
        (r"http://127.0.0.1\@evil.example.org/Olog", "loopback-only"),
    ],
)
def test_the_classification_follows_the_parser_the_socket_follows(url: str, expected: str) -> None:
    """Provably red: put ``urllib.parse.urlsplit`` back into ``_scope_of_url``."""
    assert _scope_of_url(url) == expected


def test_the_classification_agrees_with_the_gate_primitive_on_every_case_here() -> None:
    """Stronger than the table above, and the reason it exists beside it: the table pins four
    answers, this pins the RELATIONSHIP. The field and the Olog write gate / read redaction must
    make the same loopback judgement about the same URL, or a reader cannot use the field to
    predict how the gate will behave.

    Provably red: any second parser in ``_scope_of_url``.
    """
    from epics_mcp.services._http import is_loopback_url

    for url in (
        r"http://evil.example.org:8080\@127.0.0.1/Olog",
        "http://127.0.0.1@evil.example.org/Olog",
        "http://localhost./Olog",
        r"http://127.0.0.1\@evil.example.org/Olog",
        "http://localhost:8080/ChannelFinder",
        "https://archiver.example.org:17665",
        "http://[not-an-ipv6/",
    ):
        wanted = "loopback-only" if is_loopback_url(url) else "beyond-loopback"
        assert _scope_of_url(url) == wanted, f"{url}: the field and the gate primitive disagree"


def test_the_archiver_retrieval_plane_is_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    """The post-build review's other high finding: get_pv_history connects to
    ``archiver_retrieval_url``, every other archiver tool to the mgmt URL, and a split deployment
    can put those on different HOSTS. One collapsed plane named the wrong world for exactly the
    tool that fetches the data.

    Provably red: fold ``archiver-retrieval`` back onto ``cfg.archiver_url``.
    """
    cfg = EpicsConfig(
        archiver_url="http://localhost:17665",
        archiver_retrieval_url="https://archiver.example.org:17668",
    )
    assert scope_of("archiver", cfg, {}) == "loopback-only"
    assert scope_of("archiver-retrieval", cfg, {}) == "beyond-loopback"

    # The single-JVM case, which is why the row falls back rather than reporting not-configured.
    single = EpicsConfig(archiver_url="http://localhost:17665", archiver_retrieval_url="")
    assert scope_of("archiver-retrieval", single, {}) == "loopback-only"


def test_the_comment_that_names_how_often_the_shape_ships_is_still_right() -> None:
    """A number written into a comment is the thing that rots, so this one is derived.

    The post-build review found it wrong: the comment above ``Reach`` claimed the docstring
    ships TWELVE times in tools/list while the measured count was eighteen, and the arithmetic
    in the same comment (14 904 / 18 = 828, the length of the docstring it replaced) already
    said so. The count is not a list anywhere, so nothing could have kept it in step.

    Provably red: change the word in the comment, or type another read tool without updating it.
    """
    import asyncio
    import re

    from epics_mcp import provenance
    from epics_mcp.server import mcp

    words = {
        "TWELVE": 12,
        "THIRTEEN": 13,
        "FOURTEEN": 14,
        "FIFTEEN": 15,
        "SIXTEEN": 16,
        "SEVENTEEN": 17,
        "EIGHTEEN": 18,
        "NINETEEN": 19,
        "TWENTY": 20,
    }
    source = pathlib.Path(provenance.__file__).read_text(encoding="utf-8")
    claimed = [n for word, n in words.items() if re.search(rf"ships {word} times", source)]
    assert len(claimed) == 1, f"the comment names {len(claimed)} counts, expected exactly one"

    tools = asyncio.run(mcp.list_tools())
    carrying = [
        t
        for t in (tool.to_mcp_tool() for tool in tools)
        if "reach" in ((t.outputSchema or {}).get("properties") or {})
    ]
    assert claimed[0] == len(carrying), (
        f"the comment says the shape ships {claimed[0]} times, measured {len(carrying)}: "
        f"{sorted(t.name for t in carrying)}"
    )


@pytest.mark.asyncio
async def test_a_read_that_RAISES_still_says_which_world_failed_to_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failing read is the one that needs the reach most, and the first version left it out.

    A PV that does not connect is where a diagnosis STARTS, and it is also the case where "does
    not exist", "is down" and "is not reachable from here" look identical. The first version
    attached the field only to a successful answer, so that path arrived with no statement about
    which world had failed to answer, while the shipped prompt told the reader to read the reach
    of the first answer. On that path there was no first answer: the advice was unfollowable in
    exactly the situation it was written for.

    The error code must survive, because callers branch on it.

    Provably red: remove the ``except EpicsError`` arm from ``with_reach``.
    """
    from epics_mcp.errors import PVTimeoutError
    from epics_mcp.server import mcp

    async def _timeout(pv_name: str, timeout: float | None = None) -> dict[str, object]:
        raise PVTimeoutError(f"PV {pv_name} timed out")

    monkeypatch.setattr("epics_mcp.tools.read.pv_get", _timeout)
    with pytest.raises(Exception) as caught:
        await mcp.call_tool("get_pv_value", {"pv_name": "SIM:PS-01:Cur-RB"})
    message = str(caught.value)
    assert "[PV_TIMEOUT]" in message, f"the error code did not survive: {message}"
    assert "reach:" in message, f"the failing read says nothing about its world: {message}"
    assert "live-pv=" in message
    assert "not probed" in message


def test_the_inline_form_says_the_same_thing_as_the_field() -> None:
    """Two renderings, one source. The flat clause on an error must never be able to disagree
    with the structured field on an answer, so it is derived from the same mapping rather than
    built a second time.

    Provably red: give ``_inline`` its own classification call.
    """
    from epics_mcp.provenance import _inline

    field = reach_of("live-pv", "olog", cfg=EpicsConfig(), environ=LOOPBACK_ENV)
    flat = _inline(field)
    for name, scope in field["planes"].items():
        assert f"{name}={scope}" in flat, f"{name} is missing from the inline form: {flat}"
    assert "not probed" in flat
