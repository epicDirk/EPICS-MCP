"""Tests for services/checkers, the services-layer edge to the four REST planes (M8/M9/C8).

Covers the branches the tool/crossplane/coverage tests don't reach: the checker adapters'
error→RuntimeError translation (so the pure cores WITHHOLD a cell, never false-flag it), the
build_* config gates, and the query_* error→EpicsConnectionError translation.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from unittest.mock import Mock

import pytest
import requests

from epics_mcp.config import EpicsConfig
from epics_mcp.errors import EpicsConnectionError, EpicsError
from epics_mcp.services import checkers, checkers_olog
from epics_mcp.services.alarm_exceptions import AlarmConnectionError, AlarmResponseError
from epics_mcp.services.archiver_exceptions import ArchiverConnectionError
from epics_mcp.services.channelfinder_exceptions import (
    ChannelFinderConnectionError,
    ChannelFinderResponseError,
)
from epics_mcp.services.naming_exceptions import (
    NamingServiceConnectionError,
    NamingServiceResponseError,
)
from epics_mcp.services.olog_exceptions import OlogResponseError

# --- MA-1 split contract: the Olog surface lives in checkers_olog, re-exported by checkers ---


def test_checkers_reexports_olog_surface_from_checkers_olog() -> None:
    """The ten ``query_olog_*`` functions and the private ``_olog_error_code`` moved to
    :mod:`~.checkers_olog` (MA-1) and are re-exported by :mod:`~.checkers` as the SAME objects, so
    ``from ...checkers import query_olog_*`` keeps working. ``OlogClient`` no longer lives in the
    ``checkers`` namespace, patching it there would be a silent no-op."""
    names = (
        "query_olog_add_attachment",
        "query_olog_create",
        "query_olog_download",
        "query_olog_entry",
        "query_olog_levels",
        "query_olog_list_attachments",
        "query_olog_logbooks",
        "query_olog_search",
        "query_olog_tags",
        "query_olog_update",
        "_olog_error_code",
    )
    for name in names:
        assert getattr(checkers, name) is getattr(checkers_olog, name), name
    assert not hasattr(checkers, "OlogClient")
    assert hasattr(checkers_olog, "OlogClient")


# --- checker adapters: error → RuntimeError (the pure core withholds, never false-flags) ---


def test_archiver_checker_translates_error_to_runtimeerror(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FailClient:
        def __init__(self, *args: object, **kwargs: object) -> None: ...

        def is_archived(self, pv: str) -> tuple[bool, str]:
            raise ArchiverConnectionError("down")

    monkeypatch.setattr(checkers, "ArchiverClient", _FailClient)
    checker = checkers.ArchiverChecker("http://arch", None)
    with pytest.raises(RuntimeError, match="Archiver query failed"):
        checker.is_archived("X")


def test_archiver_checker_success_returns_bool(monkeypatch: pytest.MonkeyPatch) -> None:
    class _OkClient:
        def __init__(self, *args: object, **kwargs: object) -> None: ...

        def is_archived(self, pv: str) -> tuple[bool, str]:
            return True, "Being archived"

    monkeypatch.setattr(checkers, "ArchiverClient", _OkClient)
    assert checkers.ArchiverChecker("http://arch", None).is_archived("X") is True


def test_alarm_checker_translates_error_to_runtimeerror(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FailClient:
        def __init__(self, *args: object, **kwargs: object) -> None: ...

        def is_alarm_configured(
            self, pv: str, config_name: str = "Accelerator"
        ) -> tuple[bool, dict[str, object]]:
            raise AlarmConnectionError("down")

    monkeypatch.setattr(checkers, "AlarmClient", _FailClient)
    checker = checkers.AlarmConfigChecker("http://alarm", None, config_name="Accelerator")
    with pytest.raises(RuntimeError, match="Alarm query failed"):
        checker.is_alarm_configured("X")


def test_alarm_checker_success_returns_bool(monkeypatch: pytest.MonkeyPatch) -> None:
    class _OkClient:
        def __init__(self, *args: object, **kwargs: object) -> None: ...

        def is_alarm_configured(
            self, pv: str, config_name: str = "Accelerator"
        ) -> tuple[bool, dict[str, object]]:
            return True, {}

    monkeypatch.setattr(checkers, "AlarmClient", _OkClient)
    checker = checkers.AlarmConfigChecker("http://alarm", None, config_name="Accelerator")
    assert checker.is_alarm_configured("X") is True


# --- build_* factories: config gates (not requested / URL unset → None; both set → built) ---


def test_build_archiver_checker_gates_on_request_and_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(checkers, "get_config", lambda: EpicsConfig(archiver_url=""))
    assert checkers.build_archiver_checker(False) is None  # not requested
    assert checkers.build_archiver_checker(True) is None  # requested but URL unset
    monkeypatch.setattr(checkers, "get_config", lambda: EpicsConfig(archiver_url="http://arch"))
    assert checkers.build_archiver_checker(True) is not None


def test_build_alarm_checker_gates_on_request_and_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(checkers, "get_config", lambda: EpicsConfig(alarm_url=""))
    assert checkers.build_alarm_checker(False, "Accelerator") is None  # not requested
    assert checkers.build_alarm_checker(True, "Accelerator") is None  # requested but URL unset
    monkeypatch.setattr(checkers, "get_config", lambda: EpicsConfig(alarm_url="http://alarm"))
    assert checkers.build_alarm_checker(True, "Accelerator") is not None


def test_build_alarm_checker_requires_tree_when_plane_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MA-2b(d): opting into the alarm plane (URL set + requested) without naming a tree is a LOUD
    error, not a silent scan of a guessed 'Accelerator' tree that matches nothing. Mutant (a default
    tree restored) -> no raise -> this fails. When the plane is NOT active (not requested / URL
    unset) a missing tree is moot -> None, no raise."""
    monkeypatch.setattr(checkers, "get_config", lambda: EpicsConfig(alarm_url="http://alarm"))
    with pytest.raises(EpicsError):
        checkers.build_alarm_checker(True, None)
    # Plane inactive -> a missing tree is irrelevant, so no raise (returns None).
    assert checkers.build_alarm_checker(False, None) is None
    monkeypatch.setattr(checkers, "get_config", lambda: EpicsConfig(alarm_url=""))
    assert checkers.build_alarm_checker(True, None) is None


# --- query_* error branches: the per-service error → EpicsConnectionError translation ---


async def test_query_archived_translates_error_to_epics_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(checkers, "get_config", lambda: EpicsConfig(archiver_url="http://arch"))

    class _FailClient:
        def __init__(self, *args: object, **kwargs: object) -> None: ...

        def get_archive_status(self, pv: str) -> dict[str, object]:
            raise ArchiverConnectionError("down")

    monkeypatch.setattr(checkers, "ArchiverClient", _FailClient)
    with pytest.raises(EpicsConnectionError, match="Archiver"):
        await checkers.query_archived("X")


async def test_query_alarm_configured_translates_error_to_epics_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(checkers, "get_config", lambda: EpicsConfig(alarm_url="http://alarm"))

    class _FailClient:
        def __init__(self, *args: object, **kwargs: object) -> None: ...

        def is_alarm_configured(
            self, pv: str, config_name: str = "Accelerator"
        ) -> tuple[bool, dict[str, object]]:
            raise AlarmConnectionError("down")

    monkeypatch.setattr(checkers, "AlarmClient", _FailClient)
    with pytest.raises(EpicsConnectionError, match="Alarm Logger"):
        await checkers.query_alarm_configured("X", "Accelerator")


async def test_query_channels_translates_error_to_epics_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(checkers, "get_config", lambda: EpicsConfig(channelfinder_url="http://cf"))

    class _FailClient:
        def __init__(self, *args: object, **kwargs: object) -> None: ...

        def find_channels(self, name_pattern: str, max_results: int = 500) -> list[object]:
            raise ChannelFinderConnectionError("down")

    monkeypatch.setattr(checkers, "ChannelFinderClient", _FailClient)
    with pytest.raises(EpicsConnectionError, match="ChannelFinder"):
        await checkers.query_channels("X*")


# --- query_* error branches (S11 §8): a RESPONSE error must NOT be relabelled "unreachable" ---
#
# The clients now RAISE their plane's ResponseError on an unreadable 2xx (S11). These query
# functions used to collapse EVERY plane error into EpicsConnectionError, "cannot reach the
# service" about a server that ANSWERED. That is the neighbouring falsehood of the class S11
# closes; query_olog_search and query_archived already live the honest three-way split.


async def test_query_alarm_configured_response_error_is_not_a_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(checkers, "get_config", lambda: EpicsConfig(alarm_url="http://alarm"))

    class _FailClient:
        def __init__(self, *args: object, **kwargs: object) -> None: ...

        def is_alarm_configured(
            self, pv: str, config_name: str = "Accelerator"
        ) -> tuple[bool, dict[str, object]]:
            raise AlarmResponseError("unreadable payload")

    monkeypatch.setattr(checkers, "AlarmClient", _FailClient)
    with pytest.raises(EpicsError, match="Alarm Logger") as excinfo:
        await checkers.query_alarm_configured("X", "Accelerator")
    assert not isinstance(excinfo.value, EpicsConnectionError)  # the server ANSWERED


async def test_query_alarm_history_response_error_is_not_a_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(checkers, "get_config", lambda: EpicsConfig(alarm_url="http://alarm"))

    class _FailClient:
        def __init__(self, *args: object, **kwargs: object) -> None: ...

        def get_alarm_history(
            self, pv: str, start: str, end: str, max_events: int = 100, **kwargs: object
        ) -> tuple[list[dict[str, object]], bool]:
            raise AlarmResponseError("unreadable payload")

    monkeypatch.setattr(checkers, "AlarmClient", _FailClient)
    with pytest.raises(EpicsError, match="Alarm Logger") as excinfo:
        await checkers.query_alarm_history("X", "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z")
    assert not isinstance(excinfo.value, EpicsConnectionError)


async def test_query_channels_response_error_is_not_a_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(checkers, "get_config", lambda: EpicsConfig(channelfinder_url="http://cf"))

    class _FailClient:
        def __init__(self, *args: object, **kwargs: object) -> None: ...

        def find_channels(self, name_pattern: str, max_results: int = 500) -> list[object]:
            raise ChannelFinderResponseError("unreadable payload")

    monkeypatch.setattr(checkers, "ChannelFinderClient", _FailClient)
    with pytest.raises(EpicsError, match="ChannelFinder") as excinfo:
        await checkers.query_channels("X*")
    assert not isinstance(excinfo.value, EpicsConnectionError)


@pytest.mark.parametrize("method", ["get_log_entry", "list_logbooks", "list_tags"])
async def test_query_olog_response_error_is_not_a_connection_error(
    method: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each of the three remaining olog query functions has its own except block, each must
    stop relabelling a ResponseError as 'Olog unreachable' (search already splits)."""
    monkeypatch.setattr(checkers_olog, "get_config", lambda: EpicsConfig(olog_url="http://olog"))

    class _FailClient:
        def __init__(self, *args: object, **kwargs: object) -> None: ...

        def get_log_entry(self, log_id: str) -> dict[str, object] | None:
            raise OlogResponseError("unreadable payload")

        def list_logbooks(self) -> list[str]:
            raise OlogResponseError("unreadable payload")

        def list_tags(self) -> list[str]:
            raise OlogResponseError("unreadable payload")

    monkeypatch.setattr(checkers_olog, "OlogClient", _FailClient)
    calls: dict[str, Callable[[], Awaitable[object]]] = {
        "get_log_entry": lambda: checkers.query_olog_entry("1"),
        "list_logbooks": lambda: checkers.query_olog_logbooks(),
        "list_tags": lambda: checkers.query_olog_tags(),
    }
    with pytest.raises(EpicsError, match="Olog") as excinfo:
        await calls[method]()
    assert not isinstance(excinfo.value, EpicsConnectionError)


# --- build_naming_client: timeout forwarding (DS-2 tool wiring; was dropped before) ---


def test_build_naming_client_forwards_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """build_naming_client must pass the tool timeout THROUGH to the client, before the DS-2
    lookup tool it built the client without a timeout, so a tool timeout would have been silently
    lost. The default stays 5.0 so existing positional callers (orchestration, diagnose tests) are
    unchanged."""
    monkeypatch.setattr(checkers, "get_config", lambda: EpicsConfig(naming_url="http://naming"))
    forwarded = checkers.build_naming_client(True, timeout=9.0)
    assert forwarded is not None
    assert forwarded.timeout == 9.0
    default = checkers.build_naming_client(True)
    assert default is not None
    assert default.timeout == 5.0  # default preserved for positional callers


# --- query_naming_lookup (DS-2 standalone tool): gate · definitive answer · withheld · timeout ---


def _naming_client_mock(
    *,
    connectivity_error: Exception | None = None,
    validate_result: dict[str, object] | None = None,
    validate_error: Exception | None = None,
) -> Mock:
    """A fake NamingServiceClient with scripted check_connectivity/validate_name behaviour."""
    client = Mock()
    if connectivity_error is not None:
        client.check_connectivity.side_effect = connectivity_error
    else:
        client.check_connectivity.return_value = True
    if validate_error is not None:
        client.validate_name.side_effect = validate_error
    else:
        client.validate_name.return_value = validate_result
    return client


async def test_query_naming_lookup_disabled_when_url_unset_makes_no_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """URL unset → enabled:false, registered:null, and NO client is constructed (no ESS egress)."""
    monkeypatch.setattr(checkers, "get_config", lambda: EpicsConfig(naming_url=""))
    factory = Mock()
    monkeypatch.setattr(checkers, "NamingServiceClient", factory)
    result = await checkers.query_naming_lookup("DEV-TEST01:Ctrl-EVR-01")
    assert result["enabled"] is False
    assert result["registered"] is None
    factory.assert_not_called()  # gate short-circuits before any client is built (no network)


async def test_query_naming_lookup_active_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(checkers, "get_config", lambda: EpicsConfig(naming_url="http://naming"))
    client = _naming_client_mock(
        validate_result={
            "registered": True,
            "status": "ACTIVE",
            "message": 'The name "DEV-TEST01:Ctrl-EVR-01" is registered (ACTIVE)',
        }
    )
    monkeypatch.setattr(checkers, "NamingServiceClient", Mock(return_value=client))
    result = await checkers.query_naming_lookup("DEV-TEST01:Ctrl-EVR-01")
    assert result["enabled"] is True
    assert result["registered"] is True
    assert result["status"] == "ACTIVE"
    assert result.get("withheld") is not True
    client.check_connectivity.assert_called_once()  # reachability probed before the lookup


async def test_query_naming_lookup_404_is_definitive_not_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reachable service answering 'not registered' (validate_name → registered=False on a 404) is
    a DEFINITIVE answer, NOT withheld, the split that DS-2 protects."""
    monkeypatch.setattr(checkers, "get_config", lambda: EpicsConfig(naming_url="http://naming"))
    client = _naming_client_mock(
        validate_result={"registered": False, "status": "", "message": "not registered"}
    )
    monkeypatch.setattr(checkers, "NamingServiceClient", Mock(return_value=client))
    result = await checkers.query_naming_lookup("NOPE:nope")
    assert result["enabled"] is True
    assert result["registered"] is False
    assert result.get("withheld") is not True


async def test_query_naming_lookup_obsolete_preserves_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DS-2 point 2: a non-ACTIVE registered name (OBSOLETE/DELETED) surfaces registered:false WITH
    the status string preserved verbatim, a DEFINITIVE answer, not withheld. Pins the unconditional
    status pass-through against a future refactor that only surfaced status on the registered=true
    branch (the ACTIVE and 404 tests would both still pass, silently regressing this guarantee)."""
    monkeypatch.setattr(checkers, "get_config", lambda: EpicsConfig(naming_url="http://naming"))
    client = _naming_client_mock(
        validate_result={
            "registered": False,
            "status": "OBSOLETE",
            "message": 'The name "DEV-TEST01:Ctrl-EVR-99" is OBSOLETE',
        }
    )
    monkeypatch.setattr(checkers, "NamingServiceClient", Mock(return_value=client))
    result = await checkers.query_naming_lookup("DEV-TEST01:Ctrl-EVR-99")
    assert result["enabled"] is True
    assert result["registered"] is False
    assert result["status"] == "OBSOLETE"  # non-ACTIVE status preserved verbatim (DS-2)
    assert result.get("withheld") is not True


async def test_query_naming_lookup_service_error_is_withheld_not_false_negative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A NON-404 service/URL failure (here a 500 out of validate_name) is WITHHELD (registered:null
    + withheld:true), never collapsed into a false 'not registered' (DS-2 / audit S5)."""
    monkeypatch.setattr(checkers, "get_config", lambda: EpicsConfig(naming_url="http://naming"))
    client = _naming_client_mock(validate_error=NamingServiceResponseError("HTTP 500"))
    monkeypatch.setattr(checkers, "NamingServiceClient", Mock(return_value=client))
    result = await checkers.query_naming_lookup("DEV-TEST01:Ctrl-EVR-01")
    assert result["enabled"] is True
    assert result["registered"] is None
    assert result["withheld"] is True


async def test_query_naming_lookup_unreachable_is_withheld(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreachable service (check_connectivity raises) is WITHHELD and never reaches the
    lookup, no false verdict from a down/timing-out service."""
    monkeypatch.setattr(checkers, "get_config", lambda: EpicsConfig(naming_url="http://naming"))
    client = _naming_client_mock(connectivity_error=NamingServiceConnectionError("refused"))
    monkeypatch.setattr(checkers, "NamingServiceClient", Mock(return_value=client))
    result = await checkers.query_naming_lookup("DEV-TEST01:Ctrl-EVR-01")
    assert result["registered"] is None
    assert result["withheld"] is True
    client.validate_name.assert_not_called()


async def test_query_naming_lookup_timeout_reaches_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plan DS-2 timeout wiring: the tool timeout must land in the NamingServiceClient
    (build_naming_client must forward it, it did NOT before this change)."""
    monkeypatch.setattr(checkers, "get_config", lambda: EpicsConfig(naming_url="http://naming"))
    client = _naming_client_mock(
        validate_result={"registered": True, "status": "ACTIVE", "message": "ok"}
    )
    factory = Mock(return_value=client)
    monkeypatch.setattr(checkers, "NamingServiceClient", factory)
    await checkers.query_naming_lookup("DEV-TEST01:Ctrl-EVR-01", timeout=9.0)
    factory.assert_called_once_with(base_url="http://naming", timeout=9.0)


def _session_that_serves(status: int) -> Mock:
    """A session double at the TRANSPORT seam: HEAD succeeds, GET is answered with *status*.

    Deliberately NOT a client-class double. This module fakes clients that way elsewhere and it is
    right for what those tests ask, but it cannot answer THIS question: the message under test is
    built inside the real client and the real ``_http``, from the url requests really prepared, so
    a fake client would produce the assertion's own input (CLAUDE.md, evidence point 8).
    """
    failing = Mock()
    failing.raise_for_status.side_effect = requests.exceptions.HTTPError(
        f"{status} Server Error: x for url: http://svc:s3cr3t@naming.example.org/rest/deviceNames/D",
        response=Mock(status_code=status),
    )
    session = Mock()
    session.head.return_value = Mock(status_code=200)
    session.get.return_value = failing
    return session


@pytest.mark.asyncio
async def test_the_naming_withheld_note_carries_no_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``lookup_device_name`` answers SUCCESSFULLY with ``withheld: true`` and a note explaining
    why, and that note is a payload the client keeps. Measured before BG-DERR-A: it carried the
    whole prepared url, credential included.

    Red-proof by mutant, since the fix is upstream of this line: reverting either half of
    ``_http``'s message, or ``naming_client``'s own HTTPError arm, puts ``s3cr3t`` back in the note
    and fails the second assertion.
    """
    monkeypatch.setattr(
        checkers,
        "get_config",
        lambda: EpicsConfig(naming_url="http://svc:s3cr3t@naming.example.org"),
    )
    monkeypatch.setattr(
        "epics_mcp.services.naming_client.get_shared_session",
        lambda **_kwargs: _session_that_serves(500),
    )

    result = await checkers.query_naming_lookup("DEV-TEST01")

    note = str(result.get("note", ""))
    assert result["withheld"] is True
    assert "HTTP 500" in note
    assert "s3cr3t" not in note


def _session_whose_head_fails() -> Mock:
    """A transport-seam double whose HEAD dies, so ``check_connectivity`` is the failing call."""
    session = Mock()
    session.head.side_effect = requests.exceptions.ConnectionError(
        "HTTPConnectionPool: failed to establish a new connection to "
        "http://svc:s3cr3t@naming.example.org"
    )
    return session


@pytest.mark.parametrize(
    ("case", "expected_fragment"),
    [
        ("connectivity", "Failed to connect to Naming Service"),
        ("identity", "swagger beacon"),
    ],
)
async def test_the_other_two_naming_withheld_notes_carry_no_credential_either(
    monkeypatch: pytest.MonkeyPatch, case: str, expected_fragment: str
) -> None:
    """The same promise on the two withheld routes the test above cannot reach.

    WHY this is not a duplicate, and the distinction is the whole point. That test drives ONE route,
    a 5xx inside ``_get_device_name``, whose message is built by ``shown_cause``. Two further routes
    reach a caller of ``lookup_device_name`` with a note, and each is composed by a DIFFERENT
    function of the substrate:

    * ``check_connectivity`` fails at the transport and composes ``shown_failure`` (address AND
      cause, so a leak could enter through either half);
    * the S13 identity gate refuses a would-be definitive "not registered" and composes
      ``shown_url`` (address only).

    Neither function is exercised by the 5xx route, so "there is a test for that" was a claim about
    a route rather than about the promise. Measured 2026-08-15 across all four withheld routes of
    this function, including the S11 unreadable-payload one: every one is clean today. This guard
    holds the two that nothing else held.

    Red-provable, both cases measured on a mutant and the source restored byte for byte:
    replacing ``shown_failure(self.base_url, exc)`` with an f-string of the two raw halves in
    ``naming_client.check_connectivity`` fails ``connectivity``; replacing ``shown_url(...)`` with
    ``self.base_url`` in ``naming_client._require_verified_identity`` fails ``identity``.

    Positive control in both cases: an empty answer would satisfy the absence of a credential on its
    own, so the note must exist AND name its own route. The two fragments are deliberately chosen
    from the part of each message the mutants leave STANDING. Picked from the redacted part instead,
    the control fires first and the mutant is recorded as caught by the wrong assertion, which says
    nothing about whether the credential one bites.
    """
    monkeypatch.setattr(
        checkers,
        "get_config",
        lambda: EpicsConfig(naming_url="http://svc:s3cr3t@naming.example.org"),
    )
    # 404 on deviceNames is the service's "not registered", which the S13 gate then refuses to
    # trust until the responder proves it is the Naming Service, so this is the identity route.
    session = _session_whose_head_fails() if case == "connectivity" else _session_that_serves(404)
    monkeypatch.setattr(
        "epics_mcp.services.naming_client.get_shared_session", lambda **_kwargs: session
    )
    # The identity probe builds its OWN session (build_retrying_session), so it needs its own
    # double; without it this case would reach for a real socket.
    monkeypatch.setattr(
        "epics_mcp.services.naming_identity.build_retrying_session", lambda **_kwargs: session
    )

    result = await checkers.query_naming_lookup("DEV-TEST01")

    note = str(result.get("note", ""))
    assert result["withheld"] is True, result
    assert expected_fragment in note, note
    assert "s3cr3t" not in note
