"""Guards for the reach field (GB-64): where an answer came from, on the answer itself.

The defect this field removes is a claim about reach made without a measurement, so these
guards are written to catch the field itself becoming such a claim. Three of them are about
that and nothing else: :func:`test_the_field_never_carries_an_address`,
:func:`test_a_null_environment_is_not_confined` and
:func:`test_nothing_here_opens_a_socket`.
"""

from __future__ import annotations

import json
import socket
from collections.abc import Mapping
from typing import Any

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
