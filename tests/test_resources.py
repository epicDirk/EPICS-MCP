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
        "rest_tls",
        "rest_read_rate_limit",
        "allowed_roots_set",
        "channelfinder_redaction",
    }
)

#: Every dict BELOW the top level, by dotted path, with its key set. The set above pins one level,
#: which was the whole contract while every block belonged to one ticket; three of the four posture
#: fields added later are blocks, so a wire contract pinned at the top level only would let a nested
#: key appear, vanish or be renamed with the suite green. Declared for the same reason the set above
#: is declared rather than derived: an expectation read off the payload is satisfied by whatever the
#: payload happens to say. It goes as deep as the payload does, because two levels was not a
#: principle, it was the depth this payload happened to have: ``pv_search.auto_addr_broadcast`` is a
#: third level, and a key added there would otherwise be invisible.
_HEALTH_NESTED_KEYS: dict[str, frozenset[str]] = {
    "olog_write": frozenset(
        {"armed", "logbooks", "rate_limit_per_minute", "target_allowed", "target_is_loopback"}
    ),
    "pv_search": frozenset({"auto_addr_broadcast", "search_lists_set", "loopback_only"}),
    "pv_search.auto_addr_broadcast": frozenset({"pva", "ca"}),
    "rest_tls": frozenset(
        {"verification_enabled", "ca_bundle_configured", "https_plane_configured"}
    ),
    "rest_read_rate_limit": frozenset({"enabled", "per_minute"}),
    "channelfinder_redaction": frozenset(
        {
            "disclosed_owner_account_count",
            "disclosed_property_name_count",
            "owner_allowlist_site_configured",
            "property_allowlist_site_configured",
        }
    ),
}

#: The paths whose value may be TEXT. Every other leaf must be a boolean, a number or null, and
#: that is the shape of the disclosure rule rather than a style: a host, a path, an account name or
#: a raw environment value can only travel as text, so declaring where text is allowed at all bounds
#: the whole payload instead of one field at a time. Measured, a substring scan does not: a field
#: echoing a configured path as its BASENAME, with the separators normalised, or lower-cased, passes
#: every "the sentinel is absent" assertion while carrying the value.
_HEALTH_TEXT_PATHS = frozenset(
    {
        "server",
        "version",
        "status",
        "provider",
        "write_pattern",
        "python_version",
        "p4p_version",
        "olog_write.logbooks",
        "pv_search.search_lists_set",
    }
)


def _dicts_by_path(payload: object, prefix: str = "") -> dict[str, frozenset[str]]:
    """Every dict inside *payload* below the top level, keyed by its dotted path."""
    found: dict[str, frozenset[str]] = {}
    if isinstance(payload, dict):
        if prefix:
            found[prefix] = frozenset(payload)
        for key, value in payload.items():
            found.update(_dicts_by_path(value, f"{prefix}.{key}" if prefix else str(key)))
    return found


def _leaves_by_path(payload: object, prefix: str = "") -> list[tuple[str, object]]:
    """Every non-dict value inside *payload*, keyed by its dotted path."""
    if not isinstance(payload, dict):
        return [(prefix, payload)]
    return [
        leaf
        for key, value in payload.items()
        for leaf in _leaves_by_path(value, f"{prefix}.{key}" if prefix else str(key))
    ]


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

    It pins EVERY level, not only the top one. Pinning the top alone left every block's contents
    unwatched, which stayed invisible while the blocks were one ticket's and stopped being invisible
    when three more arrived; and pinning two levels would have stopped one level short of this
    payload's own depth. Comparing which paths hold dicts against the declaration also catches a
    block that quietly becomes a scalar, or a scalar that grows into a block.

    ⚠️ What it still does not see is the CONTENT of a list. ``pv_search.search_lists_set`` could
    lose half its entries with this green, because the key is there and its type is unchanged; that
    belongs to the test which knows what those entries mean.
    """
    health = get_health()

    assert set(health) == set(_HEALTH_KEYS)
    assert {path: set(keys) for path, keys in _dicts_by_path(health).items()} == {
        path: set(keys) for path, keys in _HEALTH_NESTED_KEYS.items()
    }


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


def test_a_ca_bundle_deployment_is_not_reported_as_an_unverified_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A field mirroring cfg.tls_verify would call a verifying server unverified.

    ``ca_bundle`` wins over ``tls_verify`` at the session chokepoint (``verify = cfg.ca_bundle or
    cfg.tls_verify``), so the two settings are not two independent switches: the third case below
    is a deployment that verifies against an internal root while its switch reads false. Reporting
    that as "unverified" is the kind of claim Decision WP forbids, and it is the false alarm this
    payload would have carried into an approver's transcript.

    Red-proof: spell verification_enabled as ``cfg.tls_verify`` and the third case fails; spell it
    as ``bool(cfg.ca_bundle)`` and the first fails.
    """
    bundle = "/etc/pki/internal-root.pem"
    off = {"https_plane_configured": False}

    _with_config(monkeypatch)
    assert get_health()["rest_tls"] == {
        "verification_enabled": True,
        "ca_bundle_configured": False,
        **off,
    }

    _with_config(monkeypatch, tls_verify=False)
    assert get_health()["rest_tls"] == {
        "verification_enabled": False,
        "ca_bundle_configured": False,
        **off,
    }

    _with_config(monkeypatch, tls_verify=False, ca_bundle=bundle)
    assert get_health()["rest_tls"] == {
        "verification_enabled": True,
        "ca_bundle_configured": True,
        **off,
    }

    # The fourth cell, and the one a three-case test cannot do without: verification ON with an
    # internal root, which is the ordinary secure production setting. Without it an expression
    # reading the two settings as EITHER-OR rather than either passes every case above.
    _with_config(monkeypatch, ca_bundle=bundle)
    assert get_health()["rest_tls"] == {
        "verification_enabled": True,
        "ca_bundle_configured": True,
        **off,
    }


def test_the_tls_field_agrees_with_the_session_this_process_really_builds() -> None:
    """The precedence is measured against the sessions, not against a second copy of the rule.

    The assertions above compare the payload with a rule spelled out in the test, which cannot see
    the two drifting apart: the field would keep passing if ``build_retrying_session`` stopped
    honouring ``ca_bundle``. This one asks the REST session factory itself, with the same
    configuration, and pins that "verification_enabled" means what requests will actually do.

    The real config singleton is set here rather than the module rebind ``_with_config`` uses,
    because ``_http`` reads ``epics_mcp.config.get_config`` and a rebind in ``resources`` would
    leave the two halves looking at different configurations, which is exactly the drift this test
    exists to catch. The autouse fixture drops the singleton on both sides.
    """
    from epics_mcp.services._http import build_retrying_session

    bundle = "/etc/pki/internal-root.pem"
    config_module._config = EpicsConfig(tls_verify=False, ca_bundle=bundle)
    session = build_retrying_session()
    assert session.verify == bundle, "the bundle path is what requests will verify against"
    with_bundle = get_health()["rest_tls"]
    assert isinstance(with_bundle, dict)
    assert with_bundle["verification_enabled"] is True

    config_module._config = EpicsConfig(tls_verify=False)
    session = build_retrying_session()
    assert session.verify is False, "positive control: without a bundle the switch really is off"
    without_bundle = get_health()["rest_tls"]
    assert isinstance(without_bundle, dict)
    assert without_bundle["verification_enabled"] is False


def test_verification_on_says_nothing_about_a_plane_that_speaks_no_tls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """verification_enabled is TRUE by default, including where there is no certificate at all.

    Read alone on a plain-http deployment it is an all-clear for traffic that is in the clear, and
    the payload carries no prose to qualify it with. The configured URLs answer that question
    without naming one, so the answer sits beside it: two settings, two fields, each true to
    itself, rather than one field folding a second question in and being wrong about both.

    Red-proof: drop https_plane_configured and the first case cannot be told from the second.
    """
    _with_config(monkeypatch, channelfinder_url="http://cf:8080/ChannelFinder")
    plain = get_health()["rest_tls"]
    assert isinstance(plain, dict)
    assert plain == {
        "verification_enabled": True,
        "ca_bundle_configured": False,
        "https_plane_configured": False,
    }, "nothing here speaks TLS, so verification being on is not an all-clear"

    # Every plane that can carry one, so the field is not wired to a single URL. archiver_retrieval
    # is included because it is a URL of its own even though its ENABLED bit follows the mgmt one.
    for plane in (
        "channelfinder_url",
        "archiver_url",
        "archiver_retrieval_url",
        "alarm_url",
        "naming_url",
        "olog_url",
    ):
        _with_config(monkeypatch, **{plane: "https://secure.example.org/svc"})
        secure = get_health()["rest_tls"]
        assert isinstance(secure, dict)
        assert secure["https_plane_configured"] is True, plane


def test_the_file_boundary_field_is_not_satisfied_by_a_value_holding_no_root(
    tmp_path: Path,
) -> None:
    """``bool(cfg.allowed_roots)`` reports a boundary that no file argument is held to.

    A separator-only or blank value is dropped to nothing by the resolver, so the naive spelling
    would answer "the path boundary is configured" for a server on which every file argument is
    unbounded. That direction is the dangerous one, and it is the same false all-clear the two
    preceding tickets each built into their own new field.

    Measured against ``paths._allowed_roots`` rather than against a second copy of the rule, for
    the reason the TLS pair states: a payload agreeing with a rule the test spells out cannot see
    the two drifting apart. The real config singleton is set for that reason too.

    Red-proof: spell the field ``bool(cfg.allowed_roots)`` and the separator cases fail.
    """
    from epics_mcp import paths

    empty = ("", "   ", os.pathsep, f" {os.pathsep} ", os.pathsep * 2)
    # Positive controls, and the shapes matter as much as their presence. A LEADING separator is
    # the case that tells a predicate reading every entry from one reading only the first, and that
    # mutant would switch the boundary off for a value which really does hold a root. Several roots
    # is the second: with one root, "any" and "exactly one" are the same expression.
    holding = (
        str(tmp_path),
        f"{os.pathsep}{tmp_path}",
        f"{tmp_path}{os.pathsep}",
        f"{tmp_path}{os.pathsep}{tmp_path / 'second'}",
    )

    for raw in empty + holding:
        config_module._config = EpicsConfig(allowed_roots=raw)
        reported = get_health()["allowed_roots_set"]
        assert reported is (raw in holding), raw
        # Computed rather than spelled out a second time: the payload and the boundary must agree
        # about THIS value, which is what sharing one predicate is for.
        assert reported == (paths._allowed_roots() != []), f"the resolver disagrees about {raw!r}"


def test_the_read_throttle_and_the_redaction_counters_are_wired_to_their_own_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Counts are the half a key-presence check cannot see, and each has a plausible wrong source.

    The sentinel allowlists deliberately have a cardinality the built-in defaults do NOT have
    (three owners, two properties against one and five): with matching sizes an implementation that
    ignores the configuration and counts the module constants would pass every assertion here. The
    owner sentinel also carries a duplicate and a blank entry, so a count taken from the raw
    environment string rather than from the resolved allowlist is a different number.

    The three-way of the allowlists is the point of the site_configured pair: unset means the
    built-in default, an explicitly EMPTY value means redact everything, and a count of zero is
    therefore the most private posture rather than a broken one. Nothing else in the payload can
    tell those two apart.

    Red-proof: count the module constants instead of the resolved allowlists, or wire either
    site_configured flag to the other field, and one of the blocks below fails.
    """
    _with_config(
        monkeypatch,
        read_rate_limit=60,
        # A duplicate and a blank entry, because a count taken from the raw string instead of the
        # resolved allowlist agrees with this one on any list that has neither.
        channelfinder_safe_owner_accounts="svc-alpha, svc-beta, svc-alpha, ,svc-gamma",
        channelfinder_safe_property_names="propOne,propTwo",
    )
    health = get_health()
    assert health["rest_read_rate_limit"] == {"enabled": True, "per_minute": 60}

    assert health["channelfinder_redaction"] == {
        "disclosed_owner_account_count": 3,
        "disclosed_property_name_count": 2,
        "owner_allowlist_site_configured": True,
        "property_allowlist_site_configured": True,
    }

    # A third rate, off the two the rest of this test uses: 0 and 60 are fixed points of several
    # plausible corruptions (a cap at 60, a modulo), and a two-point test cannot see any of them.
    _with_config(monkeypatch, read_rate_limit=600)
    assert get_health()["rest_read_rate_limit"] == {"enabled": True, "per_minute": 600}

    # The default posture. The two figures are the built-in ESS allowlists, and a change to either
    # is a change to what a ChannelFinder answer discloses, so it is meant to surface here.
    _with_config(monkeypatch)
    health = get_health()
    assert health["rest_read_rate_limit"] == {"enabled": False, "per_minute": 0}
    assert health["channelfinder_redaction"] == {
        "disclosed_owner_account_count": 1,
        "disclosed_property_name_count": 5,
        "owner_allowlist_site_configured": False,
        "property_allowlist_site_configured": False,
    }

    # Explicitly empty: configured AND disclosing nothing, which is not the same as unset.
    _with_config(
        monkeypatch,
        channelfinder_safe_owner_accounts="",
        channelfinder_safe_property_names="",
    )
    assert get_health()["channelfinder_redaction"] == {
        "disclosed_owner_account_count": 0,
        "disclosed_property_name_count": 0,
        "owner_allowlist_site_configured": True,
        "property_allowlist_site_configured": True,
    }

    # Exactly one of the two overridden. Every case above sets both or neither, which is the one
    # shape in which a field wired to its sibling cannot be told from a correct one: the flags
    # agree, and the counts differ only where the sentinel cardinalities differ.
    _with_config(monkeypatch, channelfinder_safe_owner_accounts="svc-alpha, svc-beta, svc-gamma")
    assert get_health()["channelfinder_redaction"] == {
        "disclosed_owner_account_count": 3,
        "disclosed_property_name_count": 5,
        "owner_allowlist_site_configured": True,
        "property_allowlist_site_configured": False,
    }


def test_no_payload_field_carries_a_host_a_path_or_an_account_outside_the_url_keys(
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

    A host is no longer the only shape that must not travel, which is why the name grew: the
    posture fields are derived from a CA-bundle path, a root list and two allowlists of service
    accounts, and each of those is a filesystem path or a raw environment value. They are set here
    with sentinels of their own, and each is checked in both spellings, because ``json.dumps``
    escapes a backslash: for the one sentinel that carries one, the raw form is absent from the
    rendered payload and the escaped form is what would be present, so checking only the obvious
    spelling would pass with the value right there. For the other sentinels the two spellings are
    the same string, and the second check costs nothing rather than adding cover.

    ⚠️ This scan compares bytes it was told to look for, so it sees an ECHO and not a leak in
    general. ``test_only_the_declared_paths_of_the_health_payload_may_carry_text`` is the half that
    covers a transformed value.

    ⚠️ The bundle sentinel carries a backslash but NO drive letter, deliberately: the repo-wide
    facility-agnostic scan in test_guide.py reads ``X:\\dir`` as a local drive path and rejects it
    in any tracked file, this one included. A relative Windows path exercises the escaping just as
    well and stays inside that rule.
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
        ca_bundle=r"certs\internal-root-ca.pem",
        allowed_roots=os.pathsep.join(("/srv/displays", "/srv/trends")),
        channelfinder_safe_owner_accounts="svc-recceiver,ops-user-one,ops-user-two",
        channelfinder_safe_property_names="siteEngineer,locationHall",
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

    # The path and account sentinels, each in both spellings: as configured, and as json.dumps
    # would render it. The escaped form is the one a Windows path actually takes in the payload.
    for value in (
        r"certs\internal-root-ca.pem",
        "/srv/displays",
        "/srv/trends",
        "svc-recceiver",
        "ops-user-one",
        "siteEngineer",
        "locationHall",
    ):
        for spelling in (value, json.dumps(value)[1:-1]):
            assert spelling not in health, f"health disclosed {value}"
            assert spelling not in json.dumps(config), f"config disclosed {value}"

    # Positive control for those four settings, so the loop above cannot be green on a payload
    # that simply forgot them: each one really did reach the process, counted rather than named.
    posture = get_health()
    rest_tls, redaction = posture["rest_tls"], posture["channelfinder_redaction"]
    assert isinstance(rest_tls, dict) and isinstance(redaction, dict)
    assert rest_tls["ca_bundle_configured"] is True
    assert posture["allowed_roots_set"] is True
    assert redaction["disclosed_owner_account_count"] == 3
    assert redaction["disclosed_property_name_count"] == 2


def test_only_the_declared_paths_of_the_health_payload_may_carry_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A substring scan sees an echo, not a leak, and the difference is one transformation wide.

    Measured on this payload: a field that returns a configured path's BASENAME, or the same path
    with its separators normalised, or an allowlist entry lower-cased, satisfies every "the
    sentinel is absent" assertion in the test above while carrying the value to the client. The
    scan can only ever compare bytes it was told to look for.

    So the rule is stated as a SHAPE instead: a host, a path, an account name and a raw environment
    value are all TEXT, and text is allowed only where this file says it is. Everything else must
    be a boolean, a number or null, which no transformation of a configured string can become.

    Red-proof: return any configured string, transformed however you like, under a path outside the
    declaration, and this fails on the path rather than on the spelling.
    """
    _with_config(
        monkeypatch,
        ca_bundle=r"certs\internal-root-ca.pem",
        allowed_roots=os.pathsep.join(("/srv/displays", "/srv/trends")),
        channelfinder_safe_owner_accounts="svc-recceiver,ops-user-one",
        channelfinder_safe_property_names="siteEngineer,locationHall",
        olog_url="http://journal.example.org:8080/Olog",
        pv_write_pattern="^SIM:.*$",
    )

    for path, value in _leaves_by_path(get_health()):
        if path in _HEALTH_TEXT_PATHS:
            assert isinstance(value, str) or (
                isinstance(value, list) and all(isinstance(item, str) for item in value)
            ), f"{path} is declared as text and is not"
            continue
        assert isinstance(value, bool | int | float) or value is None, (
            f"{path} carries {type(value).__name__}, and only the declared paths may carry text"
        )

    # Positive control: the declaration is not empty, so the loop cannot be vacuously satisfied by
    # a payload that stopped producing text at all.
    assert {path for path, _ in _leaves_by_path(get_health())} & _HEALTH_TEXT_PATHS


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
