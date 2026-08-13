"""Tests for epics_mcp.resources."""

import json
import os
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from epics_mcp import config as config_module
from epics_mcp.config import EpicsConfig
from epics_mcp.resources import get_epics_config, get_health


@pytest.fixture(autouse=True)
def _isolate_config_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Strip every ``EPICS_MCP_*`` variable so these assertions measure the code, not the machine.

    ``EpicsConfig`` is a settings model, so a field left unset in a test is read from the PROCESS
    ENVIRONMENT, and the autouse fixture in conftest strips only the EPICS SEARCH variables. This
    repository has paid for that twice already: ``_WRITE_GATE_DEFAULTS`` exists because an unpinned
    ``olog_url`` made the suite dial a real URL, and the guide-drift guard blanks the URL fields for
    the same reason. Blanking by PREFIX rather than by field list, so a field added to the model
    later is covered without anyone remembering to add it.

    ⚠️ Stripping alone is not enough for the tests that do NOT call ``_with_config``: they go through
    the cached ``get_config`` singleton, which a previous test may already have built from the
    unstripped environment. So the cache is dropped on both sides of the test, the older of the two
    patterns this suite uses.
    """
    for name in list(os.environ):
        if name.startswith("EPICS_MCP_"):
            monkeypatch.delenv(name, raising=False)
    config_module._config = None
    yield
    config_module._config = None


def _with_config(monkeypatch: pytest.MonkeyPatch, **kwargs: object) -> None:
    """Point both resources at one explicit configuration.

    A module-local rebind, the pattern the rest of the suite uses, rather than surgery on the
    ``get_config`` singleton: ``resources`` imports the name, so replacing it there is complete and
    leaves no state for the next test to inherit.
    """
    cfg = EpicsConfig(**kwargs)  # type: ignore[arg-type]
    monkeypatch.setattr("epics_mcp.resources.get_config", lambda: cfg)


#: Every top-level key the two resources put on the wire, declared rather than derived: an
#: expectation read off the payload is satisfied by whatever the payload happens to say, which is
#: the one thing these two cannot check. Both URIs are documented in ``docs/tools.md`` and are what
#: ``docs/deployment.md`` sends an approver to, so a key appearing or vanishing is a contract change
#: and has to show up in a diff rather than in a support question.
_HEALTH_KEYS = frozenset(
    {
        "server",
        "version",
        "status",
        "provider",
        "any_write_gate_armed",
        "write_enabled",
        "write_pattern",
        "write_pattern_is_a_known_allow_all_spelling",
        "write_rate_limit",
        "olog_write",
        "uptime_seconds",
        "python_version",
        "p4p_version",
        "channelfinder_enabled",
        "archiver_enabled",
        "archiver_retrieval_enabled",
        "alarm_enabled",
        "naming_enabled",
        "pv_search",
        "olog_enabled",
    }
)

#: The planes ``run_doctor`` probes, in the spelling its report uses. Declared here rather than
#: imported from the doctor on purpose: the point is that the two agree, and deriving the
#: expectation from the thing under test proves nothing. ``live`` is the one plane with no
#: ``*_enabled`` key and that is not an omission: it is always configured, so a field for it would
#: have exactly one reachable value. ``provider`` is its report instead.
_DOCTOR_PLANES = frozenset(
    {"live", "channelfinder", "archiver", "archiver_retrieval", "alarm", "naming", "olog"}
)

_CONFIG_KEYS = frozenset(
    {
        "provider",
        "default_timeout",
        "max_batch_size",
        "max_monitor_duration",
        "max_monitor_events",
        "allow_pv_write",
        "pv_write_pattern",
        "write_rate_limit",
        "channelfinder_url",
        "archiver_url",
        "alarm_url",
    }
)


def test_health_key_set_is_exact() -> None:
    """The key set was unpinned, so a key could be added or dropped with the whole suite green.

    ``test_health_shape`` below asserts a SUBSET deliberately, which is the right shape for "these
    are mandatory" and the wrong one for "this is the wire contract": it cannot see an addition at
    all, and that is how the payload came to describe four of the seven planes without anyone
    deciding to. This one is the second half, and it is meant to go red on every future edit.
    """
    assert set(get_health()) == set(_HEALTH_KEYS)


def test_health_names_every_plane_the_doctor_probes() -> None:
    """The payload described four of the seven planes, and nothing said which three were missing.

    naming had no key at all, archiver_retrieval had no key at all, and live is reported through
    ``provider``. An approver reading this resource to see what the running server reaches got a
    silent partial answer, which is worse than a short one: nothing distinguished "this plane is
    off" from "this payload does not know about that plane".
    """
    keys = set(get_health())
    named = {plane for plane in _DOCTOR_PLANES if f"{plane}_enabled" in keys}

    assert named == _DOCTOR_PLANES - {"live"}
    assert "provider" in keys, "the live plane is reported as provider, so it must be present"


def test_the_declared_plane_list_still_matches_the_shipped_guide() -> None:
    """The list above is declared, so something has to tie it to the planes that really exist.

    The guide's plane-inventory region is already drift-guarded against a real ``run_doctor`` run by
    tests/test_guide_matches_code.py, so tying this list to the REGION inherits that check and
    closes the chain without a second run. Without this test the list above would be a third
    statement of the plane names, free to rot on its own.
    """
    guide = (
        Path(__file__).resolve().parents[1] / "src" / "epics_mcp" / "operator_guide.md"
    ).read_text(encoding="utf-8")
    # The SAME region the guard uses, comment delimiters included: without them the capture also
    # holds the prose of the BEGIN comment, which the guide convention treats as outside the
    # markers. A backticked word in that comment would then redden this test and leave the guard
    # it claims to inherit from green, which is a weaker anchor rather than the inherited one.
    region = re.search(
        r"<!-- BEGIN:plane-inventory.*?-->(.*?)<!-- END:plane-inventory -->", guide, re.DOTALL
    )
    assert region is not None, "the guide plane-inventory markers moved, the drift anchor broke"

    assert set(re.findall(r"`([a-z_]+)`", region.group(1))) == _DOCTOR_PLANES


def test_the_gate_and_plane_booleans_are_wired_to_their_own_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every boolean this ticket added had a key guard and no VALUE guard, which is half a test.

    A key-presence check cannot see a field wired to the wrong source, and several of these are one
    character apart from a plausible mistake: any_write_gate_armed reading only the Olog half,
    naming_enabled reading the Olog URL, target_allowed reading the loopback bit. Each pair below is
    a mutant that survived the suite before this test existed.
    """
    _with_config(
        monkeypatch,
        allow_pv_write=True,
        pv_write_pattern="^SIM:.*$",
        audit_log_file="audit.log",
        naming_url="https://names.example.org/",
    )
    health = get_health()

    # The PV half of any_write_gate_armed, which the Olog test cannot reach.
    assert health["any_write_gate_armed"] is True
    assert health["write_enabled"] is True
    # A narrow pattern is not a known allow-all spelling, and the gate is armed, so this is the
    # branch where the flag means something.
    assert health["write_pattern_is_a_known_allow_all_spelling"] is False
    assert health["naming_enabled"] is True
    assert health["olog_enabled"] is False, "naming and olog must not be wired to the same URL"

    _with_config(monkeypatch, allow_pv_write=True, pv_write_pattern=".*", audit_log_file="a.log")
    assert get_health()["write_pattern_is_a_known_allow_all_spelling"] is True

    _with_config(monkeypatch)
    assert get_health()["any_write_gate_armed"] is False, "neither gate armed is the default"


def test_an_allowlisted_remote_logbook_is_not_reported_as_a_local_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """target_allowed and target_is_loopback were only ever measured where BOTH are true.

    That is the one configuration in which the two cannot be told apart, so a payload wiring either
    to the other survived. The distinction is the whole reason they are separate fields: an
    allowlisted remote https target is allowed AND not loopback, and reading the pair as one is how
    a sandbox posture gets claimed for a production one.
    """
    _with_config(
        monkeypatch,
        olog_url="https://journal.example.org/Olog",
        allow_olog_write=True,
        olog_write_allow_remote=True,
        olog_write_url_allowlist="https://journal.example.org/Olog",
        olog_write_logbooks="Operations",
        audit_log_file="audit.log",
    )
    olog = get_health()["olog_write"]
    assert isinstance(olog, dict)

    assert olog["target_allowed"] is True
    assert olog["target_is_loopback"] is False


def test_the_audit_path_never_reaches_the_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    """Three things are declared withheld; this was the one with no value guard behind the claim.

    An operator's filesystem layout carries a username often enough that the payload states it as a
    deliberate omission, in docs/safety.md and the changelog. Only the key list stood behind that,
    and a key list is satisfied by any spelling of the same disclosure.
    """
    sentinel = "/var/log/only-here-audit.log"
    _with_config(
        monkeypatch,
        allow_olog_write=True,
        olog_url="http://localhost:8080/Olog",
        olog_write_logbooks="Operations",
        audit_log_file=sentinel,
    )
    health = json.dumps(get_health())

    # Positive control first: the configuration really carries the sentinel, so the absence below
    # is a finding rather than an empty fixture.
    assert get_health()["any_write_gate_armed"] is True
    assert sentinel not in health
    assert "only-here-audit" not in health


def test_the_retrieval_plane_follows_the_mgmt_url_in_both_directions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """bool(archiver_retrieval_url) would have been a new lie of the exact kind this ticket removes.

    Two configurations, and the naive spelling gets both wrong. A single-JVM appliance serves both
    webapps on one port and is configured with the mgmt URL alone, so the naive field reports false
    while history retrieval works. And a retrieval URL without a mgmt URL is a config_error the
    doctor reports, because every archiver tool gates on the mgmt one, so the naive field reports
    true for a plane that is never used.

    Red-proof: spell the field bool(cfg.archiver_retrieval_url) and BOTH assertions below fail.
    """
    _with_config(monkeypatch, archiver_url="http://archiver:17665")
    assert get_health()["archiver_retrieval_enabled"] is True, (
        "an empty retrieval URL falls back to the mgmt one, the plane works"
    )

    _with_config(monkeypatch, archiver_retrieval_url="http://archiver:17668")
    assert get_health()["archiver_retrieval_enabled"] is False, (
        "a retrieval URL without a mgmt URL is never used, the doctor calls it a config_error"
    )


def test_the_pattern_field_never_claims_a_state_the_server_refuses_to_start_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ "(none)" described a configuration that cannot exist, and could not be told from a pattern.

    An armed PV gate with an empty allowlist raises SafetyConfigError at construction, so a RUNNING
    server is never write_enabled with no pattern. The placeholder therefore stood only where the
    gate is off, where the pattern is not "none" but irrelevant, and nothing distinguished it from
    an allowlist whose text happens to be that word. null says absent once, unambiguously.

    Red-proof: restore ``or "(none)"`` in either payload and the matching assertion fails.
    """
    _with_config(monkeypatch)
    assert get_health()["write_pattern"] is None
    assert get_epics_config()["pv_write_pattern"] is None

    _with_config(monkeypatch, pv_write_pattern="^SIM:.*$")
    assert get_health()["write_pattern"] == "^SIM:.*$"
    assert get_epics_config()["pv_write_pattern"] == "^SIM:.*$"


def test_the_search_posture_names_variables_and_never_their_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whether this process broadcasts must be answerable without naming a single address.

    ``docs/deployment.md`` sends an approver here to find out whether the server reads the wrong
    facility, and the trap it names is the DEFAULT: an unset ``*_AUTO_ADDR_LIST`` means the
    broadcast is on, so an environment that names nothing still reaches into the local subnets. That
    is answerable from booleans. Which subnet is not, and it stays with ``epics-doctor``.

    The positive control matters here: the assertion that a host is absent proves nothing unless the
    host really was configured, which is how a fixture typo makes a leak test vacuously green.
    """
    sentinel = "10.0.0.7"
    monkeypatch.setenv("EPICS_PVA_ADDR_LIST", sentinel)
    monkeypatch.setenv("EPICS_PVA_AUTO_ADDR_LIST", "NO")
    monkeypatch.delenv("EPICS_CA_AUTO_ADDR_LIST", raising=False)

    search = get_health()["pv_search"]
    assert isinstance(search, dict)

    # Positive control: the sentinel really is in the environment this payload was built from.
    assert "EPICS_PVA_ADDR_LIST" in search["search_lists_set"]
    assert sentinel not in json.dumps(search), (
        "the payload quoted an address, not just its variable"
    )

    # Per provider, because the two parsers disagree about what disables the broadcast. Here pva is
    # explicitly off and ca is unset, and unset means ON.
    assert search["auto_addr_broadcast"] == {"pva": False, "ca": True}
    assert search["loopback_only"] is False, "a non-loopback entry is a reach beyond loopback"


def test_no_payload_field_carries_a_host_outside_the_three_declared_url_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Which fields may name a host is a decision, so it is written down rather than left to habit.

    channelfinder_url, archiver_url and alarm_url disclosed one before this ticket and still do;
    naming and olog deliberately do not, and neither does the search posture. Without this list the
    next field derived from a URL joins them silently, which is how the payload would drift back.

    ⚠️ ALL THREE permitted fields are configured here, and each of them carries a credential. Both
    halves of that were missing and both were load-bearing: with only channelfinder_url set, the
    other two keys read "(disabled)" and the test could not tell a payload that redacts one field
    from one that redacts three; without credentials, a redaction applied to one field only would
    still satisfy every assertion. Now the expected values below are the REDACTED spellings, so
    that mutant fails on two rows.
    """
    # Host names that share no substring with a plane name, so an assertion about a HOST cannot be
    # satisfied or broken by a KEY: "channelfinder" as a host would collide with
    # channelfinder_enabled and make the test say something it does not mean.
    _with_config(
        monkeypatch,
        naming_url="https://names.example.org/",
        olog_url="http://journal.example.org:8080/Olog",
        channelfinder_url="http://cf-svc:cf-pw@directory.example.org:8080/ChannelFinder",
        archiver_url="http://ar-svc:ar-pw@storage.example.org:17665",
        alarm_url="http://al-svc:al-pw@bells.example.org:8081",
    )

    health = json.dumps(get_health())
    config = get_epics_config()

    for withheld in ("names.example.org", "journal.example.org"):
        assert withheld not in health, f"health disclosed {withheld}"
        assert withheld not in json.dumps(config), f"config disclosed {withheld}"

    # Positive control, so the assertions above cannot pass on an empty configuration: the three
    # hosts that ARE disclosed are disclosed, each through its declared key and nowhere else, and
    # each without the userinfo it was configured with.
    assert config["channelfinder_url"] == "http://directory.example.org:8080/ChannelFinder"
    assert config["archiver_url"] == "http://storage.example.org:17665"
    assert config["alarm_url"] == "http://bells.example.org:8081"
    for disclosed in ("directory.example.org", "storage.example.org", "bells.example.org"):
        assert disclosed not in health, (
            "health names planes as booleans, so no plane host belongs in it"
        )


def test_a_credential_in_a_service_url_never_reaches_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A password written into one of the three service URLs would land in a chat transcript.

    The three fields are nothing but the configured strings, the config model does not validate
    them, and ``https://user:password@host/path`` is an ordinary spelling. A resource payload is
    kept by the client, so this is the one class of disclosure a later fix cannot repair.

    The password CONTAINS an ``@`` on purpose. urllib3, the parser requests connects through,
    splits the authority at the last one while a regex stops at the first, and under the first-``@``
    reading the tail ``ter2`` stays in the clear. An assertion that only searched for the whole
    secret would pass on that, which is why the expected strings are exact and the tail is asserted
    on its own.

    Red-proof: the pre-fix payload was ``cfg.<x>_url or "(disabled)"``, so all three rows fail.
    """
    _with_config(
        monkeypatch,
        channelfinder_url="https://svc:hun@ter2@directory.example.org:8080/ChannelFinder",
        archiver_url="https://svc:hun@ter2@storage.example.org:17665",
        alarm_url="https://svc:hun@ter2@bells.example.org:8081",
    )
    config = get_epics_config()

    assert config["channelfinder_url"] == "https://directory.example.org:8080/ChannelFinder"
    assert config["archiver_url"] == "https://storage.example.org:17665"
    assert config["alarm_url"] == "https://bells.example.org:8081"
    assert "ter2" not in json.dumps(config), "a first-@ split would leave the password's tail here"
    assert "svc" not in json.dumps(config)


def test_a_service_url_without_a_credential_is_byte_identical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The redaction must not normalise, because the documented use of this payload is comparison.

    ``docs/deployment.md`` sends an operator here to hold the running server's configuration
    against the block in their client's configuration file. The obvious function to reuse
    (``url_without_credentials``) rebuilds the address from the parse: measured, it lower-cases the
    host, drops a query and a fragment and percent-encodes a space, so four of five realistic
    spellings print differently from what the operator typed and the comparison goes
    false-negative. Every character therefore has to survive.

    Red-proof: point the three fields at ``url_without_credentials`` and rows one and two fail.
    """
    spellings = (
        "http://CF.Example.ORG:8080/Channel Finder",  # case and space
        "http://cf.example.org:8080/ChannelFinder?x=1#f",  # query and fragment
        "http://cf.example.org:8080/ChannelFinder/",  # trailing slash
    )
    for spelling in spellings:
        _with_config(monkeypatch, channelfinder_url=spelling)
        assert get_epics_config()["channelfinder_url"] == spelling

    # And the one non-URL value this field has always been able to carry is untouched by all of it.
    _with_config(monkeypatch)
    assert get_epics_config()["channelfinder_url"] == "(disabled)"


def test_a_service_url_that_cannot_be_redacted_provably_is_withheld(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The third state of these fields, and nothing exercised it until the review asked.

    A password containing a delimiter makes the parser refuse the whole URL, so there is no
    boundary to cut at and the address is withheld as null. That state is documented for adopters
    (``docs/known-limits.md``), and it is the state a support question arrives about, which makes
    the tempting repair ``url_without_userinfo(configured) or configured`` and that repair prints
    the password. Measured: with it, the whole module stayed green.

    null and "(disabled)" are DIFFERENT answers, so the distinction is asserted rather than
    assumed: health still reports the plane as configured while config declines to name it.

    Red-proof: the pre-fix payload, or that ``or configured`` fallback, and the first assertion
    fails.
    """
    _with_config(monkeypatch, channelfinder_url="https://svc:s3cr3t/x@directory.example.org/CF")
    config = get_epics_config()

    assert config["channelfinder_url"] is None
    assert config["channelfinder_url"] != "(disabled)", "withheld is not the same as unconfigured"
    assert "s3cr3t" not in json.dumps(config)
    assert get_health()["channelfinder_enabled"] is True, (
        "the plane IS configured, which is exactly what null says and (disabled) would deny"
    )


def test_an_armed_olog_gate_is_visible_in_health(monkeypatch: pytest.MonkeyPatch) -> None:
    """A server that may create logbook entries reported that it does not write, and nothing else.

    That is the whole ticket. The PV gate was the only writing this payload could describe, so the
    field an approver reads first said "false" about a server whose Olog gate is armed, with a
    service account, an allowlist and a durable audit trail behind it. The CONTRAST is asserted
    rather than only the new field, so the test states the misreading it exists to prevent: a reader
    who stops at write_enabled is still wrong, and now something else in the payload says so.
    """
    _with_config(
        monkeypatch,
        olog_url="http://localhost:8080/Olog",
        allow_olog_write=True,
        olog_write_logbooks="Commissioning, Operations",
        audit_log_file="audit.log",
    )
    health = get_health()

    assert health["write_enabled"] is False, "the PV gate is off, and that much was always right"
    assert health["any_write_gate_armed"] is True, "but this server writes, which was invisible"
    assert health["olog_write"] == {
        "armed": True,
        "logbooks": ["Commissioning", "Operations"],
        "rate_limit_per_minute": 5,
        "target_allowed": True,
        "target_is_loopback": True,
    }


def test_an_armed_olog_gate_with_no_logbooks_is_not_read_as_unrestricted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An EMPTY allowlist on an armed gate is deny-all, and the naive reading is its exact opposite.

    Worth its own test because the two gates are fail-closed in DIFFERENT shapes: an empty PV
    pattern refuses the start, an empty logbook list starts and denies every write. A payload that
    showed ``armed: true`` beside an empty list without anything else distinguishing the two would
    invite the reading that nothing constrains this gate.
    """
    _with_config(
        monkeypatch,
        olog_url="http://localhost:8080/Olog",
        allow_olog_write=True,
        audit_log_file="audit.log",
    )
    olog = get_health()["olog_write"]
    assert isinstance(olog, dict)

    assert olog["armed"] is True
    assert olog["logbooks"] == []


def test_config_key_set_is_exact() -> None:
    """The same pin for the configuration payload, and it is the more exposed of the two.

    Its keys carry service URLs, so an addition here is not only a contract change but a disclosure
    decision, and ``test_config_no_secrets`` cannot make it: that one reads key NAMES, never values.
    """
    assert set(get_epics_config()) == set(_CONFIG_KEYS)


def test_health_shape() -> None:
    result = get_health()
    expected_keys = {
        "server",
        "version",
        "status",
        "provider",
        "write_enabled",
        "uptime_seconds",
        "python_version",
        "p4p_version",
    }
    assert expected_keys.issubset(result.keys())


def test_health_values() -> None:
    result = get_health()
    assert result["server"] == "epics-mcp"
    assert result["status"] == "ok"
    assert result["write_enabled"] is False
    # REST services are disabled by default (localhost isolation preserved).
    assert result["channelfinder_enabled"] is False
    assert result["archiver_enabled"] is False
    assert result["alarm_enabled"] is False


def test_health_version_matches_package_version() -> None:
    """S1-2: the health resource must report the real package ``__version__`` (locks the wiring:
    the old shape test only checked the key was present, not its value)."""
    from epics_mcp import __version__

    assert get_health()["version"] == __version__


def test_low_level_server_version_attribute_exists() -> None:
    """S1-2 early-warning guard: since the standalone-fastmcp migration (6bd12c6), server.py sets
    the handshake version through the PUBLIC constructor:
    ``FastMCP("epics-mcp", version=__version__)``: which standalone FastMCP mirrors onto the
    PRIVATE low-level attribute ``mcp._mcp_server.version`` (the value that reaches
    ``serverInfo.version`` on the wire, independently confirmed via an ``initialize`` handshake).
    This asserts that private mirror still exists and carries ``__version__``, so a FastMCP upgrade
    that stops mirroring the constructor version turns a silently-unset handshake version into a
    loud test failure. (The old code reached into ``_mcp_server`` directly to SET the version; that
    private-write path is gone, the constructor arg replaced it.)"""
    from epics_mcp import __version__
    from epics_mcp.server import mcp

    low_level = getattr(mcp, "_mcp_server", None)
    assert low_level is not None, (
        "mcp._mcp_server vanished (FastMCP upgrade?), version-set is a no-op"
    )
    assert low_level.version == __version__


#: Words that make a key name look like a credential. Matched as SUBSTRINGS, see the test.
_SECRET_WORDS = ("secret", "password", "token", "credential", "auth", "key")


def _every_key(payload: object, prefix: str = "") -> list[str]:
    """Every key in *payload* at ANY depth, as dotted paths, descending through lists as well.

    The list half is not decoration: both nested blocks already hold lists (``olog_write.logbooks``,
    ``pv_search.search_lists_set``), so a list OF DICTS is one edit away, and a dict-only walk would
    have skipped it while the caller's docstring promised any depth.
    """
    if isinstance(payload, list):
        found: list[str] = []
        for index, item in enumerate(payload):
            found.extend(_every_key(item, f"{prefix}{index}."))
        return found
    if not isinstance(payload, dict):
        return []
    found = []
    for key, value in payload.items():
        path = f"{prefix}{key}"
        found.append(path)
        found.extend(_every_key(value, f"{path}."))
    return found


def test_neither_resource_exposes_a_secret_like_key() -> None:
    """The scan compared WHOLE key names and ran on one of the two payloads. Both were too narrow.

    Measured: ``olog_write_password`` is a real field name in ``EpicsConfig`` today, and an exact
    comparison against {"secret", "password", "key", "token"} does not match it, so the one guard
    standing between a credential and the wire would have passed it. Substring matching does.

    It now walks NESTED keys, because the write-gate blocks this ticket added are nested and a
    top-level loop would not see ``olog_write.password``, and it runs on health as well as config,
    because health is where the gate blocks landed and a credential-shaped field is likelier there.

    Red-proof: add a key containing any of the words to either payload, at any depth.
    """
    for resource, payload in (("health", get_health()), ("config", get_epics_config())):
        for key in _every_key(payload):
            lowered = key.lower()
            assert not any(word in lowered for word in _SECRET_WORDS), (
                f"{resource} exposes a secret-like key: {key}"
            )

    # Positive control: the walk really descends, so the assertions above are not vacuous on the
    # nested blocks. Without this a broken _every_key would make the whole test pass silently.
    assert "olog_write.armed" in _every_key(get_health())
