"""Offline tests for the ChannelFinder client + tool (no network)."""

from unittest.mock import Mock

import pytest
import requests

from epics_pv_mcp.config import EpicsConfig
from epics_pv_mcp.errors import EpicsConnectionError, EpicsError
from epics_pv_mcp.services._http import is_ssl_error
from epics_pv_mcp.services.channelfinder_client import (
    _SAFE_OWNER_ACCOUNTS,
    _SAFE_PROPERTY_NAMES,
    ChannelFinderClient,
    ChannelInfo,
    _build_query_params,
    _resolve_allowlist,
    resolve_safe_owner_accounts,
    resolve_safe_property_names,
)
from epics_pv_mcp.services.channelfinder_exceptions import (
    ChannelFinderConnectionError,
    ChannelFinderResponseError,
)
from epics_pv_mcp.services.checkers import _CF_DISABLED_NOTE
from epics_pv_mcp.tools.channelfinder import _find_channels, _list_channel_vocabulary


def _resp(payload: object, *, ok: bool = True) -> Mock:
    """Build a fake requests response with the given JSON payload."""
    resp = Mock()
    resp.json.return_value = payload
    if ok:
        resp.raise_for_status.return_value = None
    else:
        resp.raise_for_status.side_effect = requests.exceptions.HTTPError("404")
    return resp


# --- client ---


def test_project_extracts_ioc_host_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ChannelFinderClient("http://cf:8080/ChannelFinder")
    payload = [
        {
            "name": "SYS:PV1",
            "owner": "cf",
            "properties": [
                {"name": "iocName", "value": "IOC1"},
                {"name": "hostName", "value": "host1"},
            ],
            "tags": [{"name": "archived"}, {"name": "alarm"}],
        }
    ]
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(payload)))
    out = client.find_channels("SYS:*")
    assert len(out) == 1
    assert out[0]["name"] == "SYS:PV1"
    assert out[0]["ioc_name"] == "IOC1"
    assert out[0]["host_name"] == "host1"
    assert out[0]["tags"] == ("alarm", "archived")  # sorted, deterministic


def test_project_never_fabricates_from_malformed_properties(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QA: a null property value was str()-minted into the literal string "None" — flowing
    through the allowlist into host_name and device_lookup's source_host; a non-str name
    was stringified into an invented key; junk items vanished without the documented
    rationale. The projection stays LENIENT (inside an anchored record — same rationale as
    olog_client._names) but never fabricates: malformed entries drop whole."""
    client = ChannelFinderClient("http://cf:8080/ChannelFinder")
    payload = [
        {
            "name": "SYS:PV1",
            "owner": "cf",
            "properties": [
                {"name": "hostName", "value": None},  # null value -> dropped, NOT "None"
                {"name": 7, "value": "x"},  # non-str name -> dropped, NOT key "7"
                "junk",  # non-dict entry -> dropped
                {"name": "iocName", "value": "IOC1"},
            ],
            "tags": [{"name": 3}, "junk", {"name": "archived"}],
        }
    ]
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(payload)))

    out = client.find_channels("SYS:*")

    assert out[0]["host_name"] is None  # pre-fix: the fabricated string "None"
    assert out[0]["ioc_name"] == "IOC1"
    assert out[0]["properties"] == {"iocName": "IOC1"}
    assert out[0]["tags"] == ("archived",)


def test_project_redacts_person_owner_and_recceiverid(monkeypatch: pytest.MonkeyPatch) -> None:
    """DS-PRIVACY: a service-account owner (recceiver) is kept, a person's username owner is
    redacted to "", and the opaque properties['recceiverID'] is dropped — technical provenance
    (iocName/hostName) is untouched."""
    client = ChannelFinderClient("http://cf")
    payload = [
        {
            "name": "SYS:PV1",
            "owner": "recceiver",
            "properties": [
                {"name": "iocName", "value": "IOC1"},
                {"name": "recceiverID", "value": "abc-123"},
            ],
        },
        {
            "name": "SYS:PV2",
            "owner": "jdoe",  # a person's ESS username (CF web-UI channel)
            "properties": [{"name": "iocName", "value": "IOC2"}],
        },
    ]
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(payload)))
    out = client.find_channels("SYS:*")
    assert out[0]["owner"] == "recceiver"  # service account kept
    assert "recceiverID" not in out[0]["properties"]  # opaque id dropped (not on allowlist)
    assert out[0]["ioc_name"] == "IOC1"  # technical provenance untouched
    assert out[1]["owner"] == ""  # person's username redacted


def test_project_allowlists_properties_drops_person_property(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DS-PRIVACY: the pre-live-smoke audit exhibited a person-name leak through a property VALUE —
    the reccaster ENGINEER/LOCATION env-var convention (devIocStats) or a cfstore custom field. The
    surfaced properties must be an ALLOWLIST of known-technical names, dropping any other property
    by default; the technical iocName/hostName survive (and still feed ioc_name/host_name)."""
    client = ChannelFinderClient("http://cf")
    payload = [
        {
            "name": "SYS:PV1",
            "owner": "recceiver",  # safe RecSync service account — owner gives no protection here
            "properties": [
                {"name": "iocName", "value": "IOC1", "owner": "recceiver"},
                {"name": "hostName", "value": "host1", "owner": "recceiver"},
                {"name": "ENGINEER", "value": "John Smith", "owner": "recceiver"},
                {"name": "LOCATION", "value": "Hall B, desk of A. Person", "owner": "recceiver"},
            ],
        }
    ]
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(payload)))
    out = client.find_channels("SYS:*")
    props = out[0]["properties"]
    assert props == {"iocName": "IOC1", "hostName": "host1"}  # only technical names survive
    assert "ENGINEER" not in props  # person name in the VALUE dropped by the allowlist
    assert "LOCATION" not in props
    assert out[0]["ioc_name"] == "IOC1"  # allowlisted property still feeds the dedicated field
    assert out[0]["host_name"] == "host1"


# --- site-configurable DS-PRIVACY allowlists (E2) ---


def test_safe_allowlists_default_to_ess_when_unset() -> None:
    """UNSET config resolves to the built-in ESS RecSync defaults (no behaviour change)."""
    cfg = EpicsConfig()  # both SAFE vars unset -> None
    assert resolve_safe_owner_accounts(cfg) == _SAFE_OWNER_ACCOUNTS
    assert resolve_safe_property_names(cfg) == _SAFE_PROPERTY_NAMES


def test_resolve_allowlist_three_way() -> None:
    """None -> default; ""/whitespace -> empty; "a, b ,," -> {a, b} (trimmed, empties dropped)."""
    default = frozenset({"x"})
    assert _resolve_allowlist(None, default) == default
    assert _resolve_allowlist("", default) == frozenset()
    assert _resolve_allowlist("   ", default) == frozenset()
    assert _resolve_allowlist(" a , b ,,", default) == frozenset({"a", "b"})


def test_safe_owner_accounts_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """A facility override swaps the kept owner: the site account is kept, ESS default redacted."""
    cfg = EpicsConfig(channelfinder_safe_owner_accounts="ops_svc")
    monkeypatch.setattr("epics_pv_mcp.services.channelfinder_client.get_config", lambda: cfg)
    client = ChannelFinderClient("http://cf")
    payload = [
        {"name": "SYS:PV1", "owner": "ops_svc", "properties": []},
        {"name": "SYS:PV2", "owner": "recceiver", "properties": []},
    ]
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(payload)))
    out = client.find_channels("SYS:*")
    assert out[0]["owner"] == "ops_svc"  # site service account kept
    assert out[1]["owner"] == ""  # the ESS default is not on THIS site's allowlist


def test_safe_property_names_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """A facility override swaps the surfaced technical properties; the ESS default is dropped."""
    cfg = EpicsConfig(channelfinder_safe_property_names="siteProp")
    monkeypatch.setattr("epics_pv_mcp.services.channelfinder_client.get_config", lambda: cfg)
    client = ChannelFinderClient("http://cf")
    payload = [
        {
            "name": "SYS:PV1",
            "owner": "recceiver",
            "properties": [
                {"name": "siteProp", "value": "keepme"},
                {"name": "iocName", "value": "IOC1"},
            ],
        }
    ]
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(payload)))
    out = client.find_channels("SYS:*")
    assert out[0]["properties"] == {"siteProp": "keepme"}  # only the site allowlist survives
    assert "iocName" not in out[0]["properties"]  # ESS default no longer allowed here


def test_safe_allowlists_empty_redacts_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicitly EMPTY allowlist redacts ALL owners and ALL properties (maximal privacy)."""
    cfg = EpicsConfig(channelfinder_safe_owner_accounts="", channelfinder_safe_property_names="")
    monkeypatch.setattr("epics_pv_mcp.services.channelfinder_client.get_config", lambda: cfg)
    client = ChannelFinderClient("http://cf")
    payload = [
        {
            "name": "SYS:PV1",
            "owner": "recceiver",
            "properties": [{"name": "iocName", "value": "IOC1"}],
        }
    ]
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(payload)))
    out = client.find_channels("SYS:*")
    assert out[0]["owner"] == ""  # even the service account is redacted
    assert out[0]["properties"] == {}  # everything dropped
    assert out[0]["ioc_name"] is None  # iocName no longer surfaces -> dedicated field is None


def test_empty_result(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ChannelFinderClient("http://cf")
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp([])))
    assert client.find_channels("NOPE:*") == []


def test_connection_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ChannelFinderClient("http://cf")
    monkeypatch.setattr(
        client.session, "get", Mock(side_effect=requests.exceptions.ConnectionError())
    )
    with pytest.raises(ChannelFinderConnectionError):
        client.find_channels("X")


def test_non_list_payload_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ChannelFinderClient("http://cf")
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp({"oops": 1})))
    with pytest.raises(ChannelFinderResponseError):
        client.find_channels("X")


# --- client: strict response schema (S11) — unreadable 2xx is NEVER a definitive answer ---


@pytest.mark.parametrize(
    "payload",
    [[123], ["SYS:PS-01"], [{"name": "SYS:PS-01"}, None]],
    ids=["number-item", "string-item", "null-after-valid"],
)
def test_find_channels_non_dict_item_raises(
    payload: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S11: a non-dict element in the channel list must RAISE — it used to be silently dropped
    (a fabricated smaller registry; two different malformed payloads both looked 'successful')."""
    client = ChannelFinderClient("http://cf")
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(payload)))
    with pytest.raises(ChannelFinderResponseError):
        client.find_channels("X")


@pytest.mark.parametrize(
    "record",
    [{"owner": "recceiver"}, {"name": ""}, {"name": 7}],
    ids=["no-name", "empty-name", "non-str-name"],
)
def test_find_channels_record_without_name_raises(
    record: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S11: a channel record without a usable ``name`` must RAISE — it used to become
    ``ChannelInfo(name="")``, an identity-less phantom channel that entered downstream sets
    (crossplane/coverage) as the empty string. Measured (ESS CF): ``name`` is always present."""
    client = ChannelFinderClient("http://cf")
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp([record])))
    with pytest.raises(ChannelFinderResponseError):
        client.find_channels("X")


def test_check_connectivity_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any HTTP response to a HEAD on the root = reachable (transport + CA proven)."""
    client = ChannelFinderClient("http://cf")
    monkeypatch.setattr(client.session, "head", Mock(return_value=Mock()))
    assert client.check_connectivity() is True


def test_check_connectivity_raises_on_transport_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ChannelFinderClient("http://cf")
    monkeypatch.setattr(
        client.session, "head", Mock(side_effect=requests.exceptions.ConnectionError())
    )
    with pytest.raises(ChannelFinderConnectionError):
        client.check_connectivity()


def test_check_connectivity_ssl_error_chains_cause(monkeypatch: pytest.MonkeyPatch) -> None:
    """A TLS/CA failure on the HEAD probe chains the SSLError (via `from exc`) so the doctor
    classifier buckets it ca_error, not unreachable — a dropped `from exc` would misclassify it."""
    client = ChannelFinderClient("http://cf")
    monkeypatch.setattr(
        client.session, "head", Mock(side_effect=requests.exceptions.SSLError("self-signed"))
    )
    with pytest.raises(ChannelFinderConnectionError) as excinfo:
        client.check_connectivity()
    assert is_ssl_error(excinfo.value) is True


# --- tool ---


@pytest.mark.asyncio
async def test_tool_disabled_makes_no_network_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no URL configured, the tool returns enabled=false and never constructs a client.

    Gating + client construction moved to services/checkers.query_channels (M9); _find_channels is
    a thin delegator, so config/client are patched there.
    """
    monkeypatch.setattr(
        "epics_pv_mcp.services.checkers.get_config",
        lambda: EpicsConfig(channelfinder_url=""),
    )

    def _boom(*args: object, **kwargs: object) -> ChannelFinderClient:
        raise AssertionError("client must not be constructed when disabled")

    monkeypatch.setattr("epics_pv_mcp.services.checkers.ChannelFinderClient", _boom)
    result = await _find_channels("X")
    assert result["enabled"] is False
    assert result["channels"] == []
    assert result["total"] == 0


@pytest.mark.asyncio
async def test_tool_enabled_returns_channels(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "epics_pv_mcp.services.checkers.get_config",
        lambda: EpicsConfig(channelfinder_url="http://cf"),
    )

    class _Fake:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def find_channels(self, pattern: str, max_results: int = 500) -> list[ChannelInfo]:
            return [
                ChannelInfo(
                    name="P",
                    owner="o",
                    ioc_name="I",
                    host_name="h",
                    properties={"iocName": "I"},
                    tags=("t",),
                )
            ]

    monkeypatch.setattr("epics_pv_mcp.services.checkers.ChannelFinderClient", _Fake)
    result = await _find_channels("P*")
    assert result["enabled"] is True
    assert result["total"] == 1
    channels = result["channels"]
    assert isinstance(channels, list)
    assert channels[0]["name"] == "P"


def _ch(name: str) -> ChannelInfo:
    return ChannelInfo(name=name, owner="o", ioc_name=None, host_name=None, properties={}, tags=())


class _BoundedCFClient:
    """Emulates CF's ~size truncation: never returns more than the requested max_results."""

    available = 3

    def __init__(self, *args: object, **kwargs: object) -> None: ...

    def find_channels(self, pattern: str, max_results: int = 500) -> list[ChannelInfo]:
        return [_ch(f"c{i}") for i in range(min(_BoundedCFClient.available, max_results))]


async def test_find_channels_capped_is_honest_at_the_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S8-6: exactly max_results real channels → capped False; max_results+1 → capped True. The tool
    fetches max_results+1 and truncates, so 'capped' is an honest '> max_results', not '>='."""
    monkeypatch.setattr(
        "epics_pv_mcp.services.checkers.get_config",
        lambda: EpicsConfig(channelfinder_url="http://cf"),
    )
    monkeypatch.setattr("epics_pv_mcp.services.checkers.ChannelFinderClient", _BoundedCFClient)

    _BoundedCFClient.available = 3  # exactly the cap → NOT capped (the off-by-one case)
    result = await _find_channels("X*", max_results=3)
    assert result["capped"] is False
    assert result["total"] == 3

    _BoundedCFClient.available = 4  # one over the cap → capped, truncated to the cap
    result = await _find_channels("X*", max_results=3)
    assert result["capped"] is True
    assert result["total"] == 3


# --- CF query surface: _build_query_params (pure, deterministic) ---
#
# Exact-equality pins BOTH what we emit AND what we never emit (the AR-D discipline). The vendor
# grammar (ChannelRepository.getBuiltQuery) is the ground truth: negation is a trailing '!' on the
# KEY; prop!=* (value literally '*') means "lacks property"; tag lists are OR; ~name negation is
# silently ignored server-side, so a '!' is never emitted on ~name / reserved keys.

_ALLOW = frozenset({"pvStatus", "hostName", "iocName"})


def test_build_params_name_only_is_todays_call() -> None:
    """No filters ⇒ byte-identical to the pre-MA-2 params (the No-Filter regression invariant)."""
    assert _build_query_params("SYS:*", 500, allowed_properties=_ALLOW) == {
        "~name": "SYS:*",
        "~size": "500",
    }


def test_build_params_has_properties_value_and_present() -> None:
    params = _build_query_params(
        "*", 10, has_properties={"pvStatus": "Inactive", "hostName": "*"}, allowed_properties=_ALLOW
    )
    assert params == {"~name": "*", "~size": "10", "pvStatus": "Inactive", "hostName": "*"}


def test_build_params_lacks_property_is_bang_star() -> None:
    """prop!=* ⇒ key 'prop!', value '*' (mustNot(nested(name==prop)); value MUST be '*')."""
    params = _build_query_params("*", 10, lacks_properties=["pvStatus"], allowed_properties=_ALLOW)
    assert params == {"~name": "*", "~size": "10", "pvStatus!": "*"}


def test_build_params_not_property_value_is_bang_value() -> None:
    params = _build_query_params(
        "*", 10, not_property_values={"pvStatus": "Active"}, allowed_properties=_ALLOW
    )
    assert params == {"~name": "*", "~size": "10", "pvStatus!": "Active"}


def test_build_params_tags_or_and_negation() -> None:
    params = _build_query_params(
        "*", 10, has_tags=["a", "b"], lacks_tags=["c"], allowed_properties=_ALLOW
    )
    assert params == {"~name": "*", "~size": "10", "~tag": "a|b", "~tag!": "c"}


def test_build_params_no_size_for_count() -> None:
    params = _build_query_params(
        "*", 0, has_properties={"pvStatus": "Active"}, allowed_properties=_ALLOW, include_size=False
    )
    assert params == {"~name": "*", "pvStatus": "Active"}


def test_build_params_empty_collections_emit_nothing() -> None:
    """Empty/None collections ⇒ no filter param (never an accidental broaden/collapse)."""
    assert _build_query_params(
        "*", 10, has_properties={}, lacks_properties=[], has_tags=[], allowed_properties=_ALLOW
    ) == {"~name": "*", "~size": "10"}


# --- guards (each raises ValueError; the client maps it to EpicsError INVALID_INPUT) ---


def test_build_params_gate_rejects_non_allowlisted_property() -> None:
    """DS-privacy: a filter on a redacted property (e.g. accessGroup) is a redaction bypass —
    it reconstructs the name→value partition the projection hides. Fail closed."""
    with pytest.raises(ValueError, match="allowlist"):
        _build_query_params("*", 10, has_properties={"accessGroup": "x"}, allowed_properties=_ALLOW)


def test_build_params_empty_allowlist_disables_property_filter_but_not_tags() -> None:
    with pytest.raises(ValueError, match="allowlist"):
        _build_query_params(
            "*", 10, has_properties={"pvStatus": "x"}, allowed_properties=frozenset()
        )
    # tags are not redacted → not gated
    assert _build_query_params("*", 10, has_tags=["a"], allowed_properties=frozenset()) == {
        "~name": "*",
        "~size": "10",
        "~tag": "a",
    }


@pytest.mark.parametrize("bad_name", ["~name", "~size", "pvStatus!"])
def test_build_params_rejects_reserved_or_bang_names_across_all_surfaces(bad_name: str) -> None:
    """No path may synthesise a '~name!' / reserved-collision / trailing-'!' key. The guard runs on
    has_properties keys, lacks_properties items and not_property_values keys, before the '!'."""
    allow = _ALLOW | {bad_name.rstrip("!").lstrip("~")}  # even if somehow allowlisted, still reject
    with pytest.raises(ValueError):
        _build_query_params("*", 10, has_properties={bad_name: "x"}, allowed_properties=allow)
    with pytest.raises(ValueError):
        _build_query_params("*", 10, lacks_properties=[bad_name], allowed_properties=allow)
    with pytest.raises(ValueError):
        _build_query_params("*", 10, not_property_values={bad_name: "x"}, allowed_properties=allow)


def test_build_params_never_emits_a_name_bang_key() -> None:
    """Explicit: lacks_properties=['~name'] must not synthesise the forbidden {'~name!': '*'}."""
    with pytest.raises(ValueError):
        _build_query_params(
            "*", 10, lacks_properties=["~name"], allowed_properties=_ALLOW | {"~name"}
        )


def test_build_params_rejects_contradictory_property_across_args() -> None:
    """Same property in >1 of the three property args (subsumes the 'prop!' key collision between
    lacks_properties and not_property_values)."""
    with pytest.raises(ValueError, match="contradict"):
        _build_query_params(
            "*",
            10,
            lacks_properties=["pvStatus"],
            not_property_values={"pvStatus": "x"},
            allowed_properties=_ALLOW,
        )


def test_build_params_rejects_contradictory_tag() -> None:
    with pytest.raises(ValueError, match="contradict"):
        _build_query_params("*", 10, has_tags=["a"], lacks_tags=["a"], allowed_properties=_ALLOW)


def test_build_params_rejects_separator_in_negated_value() -> None:
    """A |,; in a not_property_values value flips the single negation into an OR-of-negations
    tautology (matches anything having the property) — reject. has_properties values keep OR."""
    with pytest.raises(ValueError, match="separator"):
        _build_query_params(
            "*", 10, not_property_values={"pvStatus": "a|b"}, allowed_properties=_ALLOW
        )
    # has_properties value with a separator is the intended OR — allowed
    assert _build_query_params(
        "*", 10, has_properties={"pvStatus": "a|b"}, allowed_properties=_ALLOW
    ) == {"~name": "*", "~size": "10", "pvStatus": "a|b"}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"has_properties": {"pvStatus": "  "}},
        {"has_properties": {"  ": "x"}},
        {"not_property_values": {"pvStatus": ""}},
        {"has_tags": ["  "]},
    ],
)
def test_build_params_rejects_blank(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _build_query_params("*", 10, allowed_properties=_ALLOW, **kwargs)  # type: ignore[arg-type]


# --- count_channels (client) ---


def test_count_channels_parses_bare_long(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ChannelFinderClient("http://cf")
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(1234)))
    assert client.count_channels("*") == 1234


def test_count_channels_parses_string_number(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ChannelFinderClient("http://cf")
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp("1234")))
    assert client.count_channels("*") == 1234


def test_count_channels_targets_the_count_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ChannelFinderClient("http://cf:8080/ChannelFinder")
    getter = Mock(return_value=_resp(0))
    monkeypatch.setattr(client.session, "get", getter)
    client.count_channels("*", has_properties={"pvStatus": "Inactive"})
    assert getter.call_args.args[0] == "http://cf:8080/ChannelFinder/resources/channels/count"
    # count carries the filter but NOT ~size, and stays gated to the default allowlist
    assert getter.call_args.kwargs["params"] == {"~name": "*", "pvStatus": "Inactive"}


# --- route identity: every method must request its OWN endpoint ---


def test_each_route_targets_its_own_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """S31: the requested URL is the only observable difference between the sibling routes.

    ``list_properties`` and ``list_tags`` return the SAME shape and both go through the same
    ``_named_list`` helper, so no type or shape guard can ever fire on a swap between them —
    measured: pointing ``list_tags`` at the properties URL left the whole suite green, and
    against a real ChannelFinder it would have returned property names as tags. The one
    asymmetric step, the allowlist filter in ``list_properties``, works the wrong way round for
    detection: it makes the properties answer SMALLER, so a swapped ``list_tags`` would not stand
    out by being too large. ``find_channels`` carries the same gap and worse — against the
    properties URL, ``_project`` turns ``{name, owner}`` dicts into plausible invented channels.

    Together with test_count_channels_targets_the_count_endpoint above, this pins all four URL
    properties the client declares (channels_url, count_url, properties_url, tags_url).

    The assertion follows each call immediately because ``call_args`` holds only the LAST call.
    ``call_count`` pins something narrower than it looks: exactly one request per route. ⚠️ It is
    NOT a floor against a route that stops requesting — measured, that case is already caught by
    the URL assertion, since the three routes expect three DIFFERENT urls and a predecessor's
    stale ``call_args`` can never satisfy the next one. An earlier version of this docstring
    claimed otherwise; what the line actually catches is a route issuing a SECOND request.

    Red-proof: point ``list_tags`` at ``self.properties_url`` (or ``find_channels`` at it) in
    services/channelfinder_client.py. mypy stays green — both are ``str``."""
    base = "http://cf:8080/ChannelFinder"
    client = ChannelFinderClient(base)
    getter = Mock(return_value=_resp([{"name": "SIM:PV1", "owner": "someone"}]))
    monkeypatch.setattr(client.session, "get", getter)

    client.find_channels("SIM:*")
    assert getter.call_args.args[0] == f"{base}/resources/channels"
    client.list_properties()
    assert getter.call_args.args[0] == f"{base}/resources/properties"
    client.list_tags()
    assert getter.call_args.args[0] == f"{base}/resources/tags"
    assert getter.call_count == 3


@pytest.mark.parametrize("payload", [True, "notanumber", {"count": 1}])
def test_count_channels_rejects_non_numeric(
    payload: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """bool ⊂ int → a JSON boolean must NOT parse as a count; non-numeric bodies raise."""
    client = ChannelFinderClient("http://cf")
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(payload)))
    with pytest.raises(ChannelFinderResponseError):
        client.count_channels("*")


# --- tool layer: conditional forwarding + count_only + gate → EpicsError ---


@pytest.mark.asyncio
async def test_tool_no_filter_passes_no_extra_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    """The No-Filter path must call find_channels with NO filter kwargs — this is why the 4 fixed-
    signature doubles stay green. A double WITHOUT **kwargs proves it (a TypeError would fail)."""
    monkeypatch.setattr(
        "epics_pv_mcp.services.checkers.get_config",
        lambda: EpicsConfig(channelfinder_url="http://cf"),
    )

    class _StrictNoKwargs:
        def __init__(self, *args: object, **kwargs: object) -> None: ...

        def find_channels(self, name_pattern: str, max_results: int = 500) -> list[ChannelInfo]:
            return []

    monkeypatch.setattr("epics_pv_mcp.services.checkers.ChannelFinderClient", _StrictNoKwargs)
    result = await _find_channels("X*")  # must not raise TypeError
    assert result["enabled"] is True


@pytest.mark.asyncio
async def test_tool_forwards_set_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "epics_pv_mcp.services.checkers.get_config",
        lambda: EpicsConfig(channelfinder_url="http://cf"),
    )
    captured: dict[str, object] = {}

    class _Capture:
        def __init__(self, *args: object, **kwargs: object) -> None: ...

        def find_channels(
            self, name_pattern: str, max_results: int = 500, **filters: object
        ) -> list[ChannelInfo]:
            captured.update(filters)
            return []

    monkeypatch.setattr("epics_pv_mcp.services.checkers.ChannelFinderClient", _Capture)
    await _find_channels("X*", has_properties={"pvStatus": "Active"}, lacks_tags=["x"])
    assert captured == {"has_properties": {"pvStatus": "Active"}, "lacks_tags": ["x"]}


@pytest.mark.asyncio
async def test_tool_count_only_returns_match_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """count_only returns a DISTINCT {enabled, match_count} — never overloads total, no capped."""
    monkeypatch.setattr(
        "epics_pv_mcp.services.checkers.get_config",
        lambda: EpicsConfig(channelfinder_url="http://cf"),
    )

    class _CountFake:
        def __init__(self, *args: object, **kwargs: object) -> None: ...

        def count_channels(self, name_pattern: str, **filters: object) -> int:
            return 42

    monkeypatch.setattr("epics_pv_mcp.services.checkers.ChannelFinderClient", _CountFake)
    result = await _find_channels("X*", count_only=True)
    assert result == {"enabled": True, "match_count": 42}


@pytest.mark.asyncio
async def test_tool_maps_bad_filter_to_invalid_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """A gate/guard ValueError surfaces as EpicsError(INVALID_INPUT), before any network call."""
    cfg = EpicsConfig(channelfinder_url="http://cf")  # default allowlist → accessGroup rejected
    monkeypatch.setattr("epics_pv_mcp.services.checkers.get_config", lambda: cfg)
    monkeypatch.setattr("epics_pv_mcp.services.channelfinder_client.get_config", lambda: cfg)
    with pytest.raises(EpicsError) as excinfo:
        await _find_channels("X*", has_properties={"accessGroup": "x"})
    assert excinfo.value.error_code == "INVALID_INPUT"
    assert not isinstance(excinfo.value, EpicsConnectionError)


@pytest.mark.asyncio
async def test_tool_count_disabled_makes_no_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """The count mode's disabled path: no client is constructed, and the SHAPE is the count shape.

    Both halves were missing and the name promised the first of them — found by the adversarial
    review of the S29 typing commit. Without the ``_boom`` double this test asserted nothing about
    "no call" at all (its list-mode twin above always had one); without the exact key set it also
    could not tell the count literal from the list literal, which is the disjointness the typed
    ``ChannelQueryResult`` advertises. Red-proofs: drop the ``count_only`` branch in
    services/checkers.query_channels and the key-set assertion goes red; make the disabled gate fall
    through to the client and ``_boom`` fires.
    """
    monkeypatch.setattr(
        "epics_pv_mcp.services.checkers.get_config",
        lambda: EpicsConfig(channelfinder_url=""),
    )

    def _boom(*args: object, **kwargs: object) -> ChannelFinderClient:
        raise AssertionError("client must not be constructed when disabled")

    monkeypatch.setattr("epics_pv_mcp.services.checkers.ChannelFinderClient", _boom)
    result = await _find_channels("X*", count_only=True)
    assert result["enabled"] is False
    assert result["match_count"] == 0
    assert set(result) == {"enabled", "match_count", "note"}


# --- CF query surface: list_channel_vocabulary (property + tag NAME discovery) ---


def test_list_properties_intersects_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    """list_properties surfaces ONLY allowlisted property names present on the server, sorted.

    A non-allowlisted name (ENGINEER — a person-bearing cfstore property) is excluded, exactly as
    ``_project`` reduces per-channel properties to ``_safe_property_names``. Otherwise the tool
    would advertise filter keys ``find_channels`` refuses with INVALID_INPUT. The list route
    carries ``owner``/``value`` — both dropped (name-only).
    """
    client = ChannelFinderClient("http://cf:8080/ChannelFinder")
    payload = [
        {"name": "pvStatus", "owner": "cf", "value": "", "channels": []},
        {"name": "iocName", "owner": "cf", "value": "", "channels": []},
        {"name": "ENGINEER", "owner": "alice", "value": "Alice A", "channels": []},
    ]
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(payload)))
    assert client.list_properties() == ["iocName", "pvStatus"]  # sorted, allowlisted only


def test_list_tags_returns_all_names_sorted_owner_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tags are ungated (as in find_channels' _project): every tag name is returned, sorted, with
    the per-tag ``owner`` dropped (name-only)."""
    client = ChannelFinderClient("http://cf")
    payload = [
        {"name": "sim", "owner": "alice", "channels": []},
        {"name": "archived", "owner": "bob", "channels": []},
    ]
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp(payload)))
    assert client.list_tags() == ["archived", "sim"]  # sorted, ungated, owner never surfaced


def test_list_vocabulary_strict_on_bad_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    """S11: a non-list payload or an item without a string 'name' RAISES, never collapses to [] —
    the listing IS the answer, so 'unreadable' must not read as 'there are none'.

    S31: the diagnosis must also name the endpoint that was ACTUALLY requested. The label used to
    be a hand-written literal, decoupled from ``self.tags_url``/``self.properties_url``, so a
    swapped route would have produced a correctly-worded error about the wrong address."""
    client = ChannelFinderClient("http://cf")
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp({"not": "a list"})))
    with pytest.raises(ChannelFinderResponseError) as excinfo:
        client.list_tags()
    assert client.tags_url in str(excinfo.value)
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp([{"owner": "x"}])))
    with pytest.raises(ChannelFinderResponseError) as excinfo:
        client.list_properties()
    # Both halves, not just one: the properties label fell back to its literal without any test
    # noticing until this line existed, so half the change was itself an unobserved guard.
    assert client.properties_url in str(excinfo.value)


def test_list_vocabulary_empty_is_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty server list yields [] (valid — genuinely no tags/properties), NOT a raise."""
    client = ChannelFinderClient("http://cf")
    monkeypatch.setattr(client.session, "get", Mock(return_value=_resp([])))
    assert client.list_tags() == []
    assert client.list_properties() == []


@pytest.mark.asyncio
async def test_vocabulary_disabled_makes_no_network_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no URL configured, the tool returns enabled=false with empty vocab + a note, and never
    constructs a client (gate lives in services/checkers.query_channel_vocabulary)."""
    monkeypatch.setattr(
        "epics_pv_mcp.services.checkers.get_config",
        lambda: EpicsConfig(channelfinder_url=""),
    )

    def _boom(*args: object, **kwargs: object) -> ChannelFinderClient:
        raise AssertionError("client must not be constructed when disabled")

    monkeypatch.setattr("epics_pv_mcp.services.checkers.ChannelFinderClient", _boom)
    result = await _list_channel_vocabulary()
    # Exact shape: enabled=false + empty vocab + the disabled note, and NO unexpected keys.
    assert result == {"enabled": False, "properties": [], "tags": [], "note": _CF_DISABLED_NOTE}


@pytest.mark.asyncio
async def test_vocabulary_enabled_returns_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """The enabled path returns the property + tag NAME lists the client produced."""
    monkeypatch.setattr(
        "epics_pv_mcp.services.checkers.get_config",
        lambda: EpicsConfig(channelfinder_url="http://cf"),
    )

    class _Fake:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def list_properties(self) -> list[str]:
            return ["iocName", "pvStatus"]

        def list_tags(self) -> list[str]:
            return ["archived", "sim"]

    monkeypatch.setattr("epics_pv_mcp.services.checkers.ChannelFinderClient", _Fake)
    result = await _list_channel_vocabulary()
    assert result["enabled"] is True
    assert result["properties"] == ["iocName", "pvStatus"]
    assert result["tags"] == ["archived", "sim"]


@pytest.mark.asyncio
async def test_vocabulary_enabled_empty_is_not_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """A CONFIGURED ChannelFinder with no vocabulary yields enabled=true + empty lists — DISTINCT
    from the disabled envelope (enabled=false + note). Locks the documented invariant that an empty
    vocabulary must never collapse into a 'disabled' appearance (e.g. a regression to
    ``enabled = bool(properties or tags)``). Empty is genuinely reachable: a configured CF whose
    allowlisted-property intersection is empty and which has no tags."""
    monkeypatch.setattr(
        "epics_pv_mcp.services.checkers.get_config",
        lambda: EpicsConfig(channelfinder_url="http://cf"),
    )

    class _Empty:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def list_properties(self) -> list[str]:
            return []

        def list_tags(self) -> list[str]:
            return []

    monkeypatch.setattr("epics_pv_mcp.services.checkers.ChannelFinderClient", _Empty)
    result = await _list_channel_vocabulary()
    assert result == {"enabled": True, "properties": [], "tags": []}  # no note, enabled distinct
