"""The cross-plane patterns (QA-96): what no single plane can see.

Every test here builds ``PlaneCheck`` objects and an ``EpicsConfig`` BY HAND. No client double, no
transport fake, no network, because the code under test has no client in its call path at all: it
is a pure function of ``(config, planes)``. That sidesteps the double-at-the-client-class hazard
this repository records (CLAUDE.md evidence rule 8) rather than navigating it, and it is worth
stating so nobody "improves" these tests into faking a seam that is not there.

⚠️ What these tests can NOT establish is that the patterns occur in reality. Two of the three are
reproduced end to end against a throwaway HTTP server in ``test_doctor_crosscut_e2e.py``, because
QA-96 says in as many words that a pattern only its own fixture produces does not count.
"""

from __future__ import annotations

import typing
from types import EllipsisType

import pytest

from epics_mcp.config import EpicsConfig
from epics_mcp.services.doctor import PlaneCheck, PlaneStatus
from epics_mcp.services.doctor_crosscut import (
    _NEVER_TRIGGERS,
    _TRIGGERS,
    installation_findings,
)


def _cfg(**urls: str) -> EpicsConfig:
    """A config with only the named plane URLs set."""
    return EpicsConfig(**urls)  # type: ignore[arg-type]


#: What ``reachable`` production sets alongside each status. A FIXTURE MIRROR, and it is here
#: because its absence was a defect: this helper used to leave ``reachable`` unset for every plane,
#: so every synthetic ``ok`` carried ``None`` while production carries ``True``. Nothing noticed
#: until ``_host_down`` started reading that field, and then three passing tests were passing on a
#: report shape the tool cannot produce.
#:
#: ⚠️ ``throttled`` maps to ``None``, the TRANSPORT arm, where nothing was sent. Its IDENTITY arm
#: carries ``reachable=True`` because the HEAD went out and answered, and a test that wants that
#: shape passes ``reachable=True`` explicitly. The two are genuinely different evidence and the
#: default is the one that cannot be guessed from the status name.
_REACHABLE_BY_STATUS: dict[str, bool | None] = {
    "ok": True,
    "unverified": True,
    "no_ingest": True,
    "backend_down": True,
    "identity_probe_failed": True,
    "api_error": True,
    "unreachable": False,
    "ca_error": False,
    "disconnected": False,
    "config_error": None,
    "disabled": None,
    "info": None,
    "throttled": None,
}


def _plane(
    name: str,
    status: PlaneStatus,
    *,
    ca_ok: bool | None = None,
    reachable: bool | None | EllipsisType = ...,
) -> PlaneCheck:
    resolved = _REACHABLE_BY_STATUS[status] if reachable is ... else reachable
    return PlaneCheck(plane=name, configured=True, status=status, ca_ok=ca_ok, reachable=resolved)


def _patterns(cfg: EpicsConfig, planes: list[PlaneCheck]) -> list[str]:
    return [f.pattern for f in installation_findings(cfg, planes).findings]


# --- the archiver pair ---

_SPLIT = {
    "archiver_url": "http://appliance.example.org:17665",
    "archiver_retrieval_url": "http://appliance.example.org:17668",
}


def test_a_swapped_archiver_pair_is_reported() -> None:
    """Both webapps erroring while pointing at different URLs is the signature of an exchange."""
    cfg = _cfg(**_SPLIT)
    planes = [_plane("archiver", "api_error"), _plane("archiver_retrieval", "api_error")]
    assert "archiver_url_pair" in _patterns(cfg, planes)


def test_a_single_failing_archiver_plane_is_not_a_pair_finding() -> None:
    """One broken webapp is one broken webapp. Widening this to ``or`` is the obvious mistake."""
    cfg = _cfg(**_SPLIT)
    planes = [_plane("archiver", "api_error"), _plane("archiver_retrieval", "ok")]
    assert "archiver_url_pair" not in _patterns(cfg, planes)


def test_a_single_jvm_appliance_can_never_look_like_a_swapped_pair() -> None:
    """The retrieval variable is empty there and both planes resolve to the mgmt URL.

    Structural rather than filtered: with one variable unset there is no pair to exchange. This is
    the false positive that would hit the most common deployment, so it is closed by the trigger
    itself and not by a caveat in the text.
    """
    cfg = _cfg(archiver_url="http://appliance.example.org:17665")
    planes = [_plane("archiver", "api_error"), _plane("archiver_retrieval", "api_error")]
    assert "archiver_url_pair" not in _patterns(cfg, planes)


def test_two_identical_archiver_urls_are_not_a_swapped_pair() -> None:
    """Both variables set to the same value: exchanging them changes nothing."""
    one = "http://appliance.example.org:17665"
    cfg = _cfg(archiver_url=one, archiver_retrieval_url=one)
    planes = [_plane("archiver", "api_error"), _plane("archiver_retrieval", "api_error")]
    assert "archiver_url_pair" not in _patterns(cfg, planes)


def test_the_pair_finding_names_a_check_and_never_an_exchange() -> None:
    """It is a SIGNATURE, and the invited action has to match that.

    A gateway erroring for both webapps produces the same evidence. An operator told to swap two
    values on this evidence would turn a correct configuration into a wrong one, and quietly: the
    retrieval fallback keeps answering. So the text may say "check" and must not say "swap them".
    """
    cfg = _cfg(**_SPLIT)
    planes = [_plane("archiver", "api_error"), _plane("archiver_retrieval", "api_error")]
    finding = installation_findings(cfg, planes).findings[0]
    assert finding.evidence == "signature"
    assert "verify a route before you change a value" in finding.detail
    assert "gateway" in finding.detail


# --- one host down ---

_TWO_ON_ONE_HOST = {
    "channelfinder_url": "http://services.example.org:8080/ChannelFinder",
    "alarm_url": "http://services.example.org:8081",
}


def test_two_dead_services_on_one_host_are_reported_as_the_host() -> None:
    cfg = _cfg(**_TWO_ON_ONE_HOST, naming_url="http://elsewhere.example.org:8099")
    planes = [
        _plane("channelfinder", "unreachable"),
        _plane("alarm", "unreachable"),
        _plane("naming", "ok"),
    ]
    findings = installation_findings(cfg, planes).findings
    assert [f.pattern for f in findings] == ["host_down"]
    assert findings[0].host == "services.example.org"
    # The other host is NAMED, so the reader learns this is one host rather than everything.
    assert "elsewhere.example.org" in findings[0].detail


def test_one_dead_plane_on_a_shared_host_is_not_a_dead_host() -> None:
    """Needs at least two authorities; one failure among several on a host is a service fault."""
    cfg = _cfg(**_TWO_ON_ONE_HOST)
    planes = [_plane("channelfinder", "unreachable"), _plane("alarm", "ok")]
    assert _patterns(cfg, planes) == []


def test_two_planes_on_ONE_authority_are_one_service_not_two() -> None:
    """A single-JVM appliance, and a path-based reverse proxy, put several planes on one address.

    Counting PLANES instead of authorities would call that "2 services down" on a host where
    exactly one thing is broken. Conservative on purpose: this block removes false all-clears, it
    must not add false alarms.
    """
    one = "http://appliance.example.org:17665"
    cfg = _cfg(archiver_url=one, olog_url="http://elsewhere.example.org:8080/Olog")
    planes = [
        _plane("archiver", "unreachable"),
        _plane("archiver_retrieval", "unreachable"),
        _plane("olog", "ok"),
    ]
    assert _patterns(cfg, planes) == []


def test_two_dead_planes_on_two_hosts_are_not_one_dead_host() -> None:
    cfg = _cfg(
        channelfinder_url="http://cf.example.org:8080/ChannelFinder",
        alarm_url="http://alarm.example.org:8081",
        naming_url="http://naming.example.org:8099",
    )
    planes = [
        _plane("channelfinder", "unreachable"),
        _plane("alarm", "unreachable"),
        _plane("naming", "ok"),
    ]
    assert _patterns(cfg, planes) == []


def test_everything_dead_everywhere_is_not_reported_as_a_dead_host() -> None:
    """The measured false positive, twice over, and both times the cause was on THIS machine.

    An unreadable CA bundle turned all six HTTPS planes ``unreachable`` without a request leaving,
    and an ``HTTP_PROXY`` did the same. Firing here would name several innocent hosts, and print
    them INSTEAD of the only true statement: nothing left this machine at all.
    """
    cfg = _cfg(**_TWO_ON_ONE_HOST, naming_url="http://elsewhere.example.org:8099")
    planes = [
        _plane("channelfinder", "unreachable"),
        _plane("alarm", "unreachable"),
        _plane("naming", "unreachable"),
    ]
    assert "host_down" not in _patterns(cfg, planes)


def test_a_lone_host_is_named_as_the_whole_deployment() -> None:
    """With no other host configured, "one host of several" would be the wrong sentence."""
    cfg = _cfg(**_TWO_ON_ONE_HOST, naming_url="http://services.example.org:8099")
    planes = [
        _plane("channelfinder", "unreachable"),
        _plane("alarm", "unreachable"),
        _plane("naming", "ok"),
    ]
    findings = installation_findings(cfg, planes).findings
    # naming is healthy on the same host, so the host has an answer: nothing is claimed.
    assert findings == []


def test_a_healthy_plane_on_the_host_withdraws_the_finding() -> None:
    """ "None on it answered" is part of the claim, so one answer refutes it."""
    cfg = _cfg(
        **_TWO_ON_ONE_HOST,
        olog_url="http://services.example.org:8082/Olog",
    )
    planes = [
        _plane("channelfinder", "unreachable"),
        _plane("alarm", "unreachable"),
        _plane("olog", "ok"),
    ]
    assert _patterns(cfg, planes) == []


def test_a_plane_that_answered_is_never_evidence_that_its_host_is_silent() -> None:
    """The S4 shape, and the finding it used to produce contradicted the report's own lines.

    A front proxy answering 401 for every path on two ports of one host gives both planes
    ``identity_probe_failed``, whose per-plane line reads "transport reachable". The block printed
    "2 services on H failed and none on it answered, so check the host" directly underneath, and
    told the operator to check a host that had answered every single request. Found by the
    post-build review; the trigger for this pattern is now ``unreachable`` alone.
    """
    cfg = _cfg(
        channelfinder_url="http://proxy.example.org:8080/ChannelFinder",
        naming_url="http://proxy.example.org:8099",
        alarm_url="http://elsewhere.example.org:8081",
    )
    planes = [
        _plane("channelfinder", "identity_probe_failed"),
        _plane("naming", "identity_probe_failed"),
        _plane("alarm", "ok"),
    ]
    assert "host_down" not in _patterns(cfg, planes)


def test_a_tls_failure_that_never_opened_a_socket_is_not_evidence_about_a_host() -> None:
    """An unreadable CA bundle fails a plane BEFORE any connection exists.

    Measured by the post-build review on an ordinary mixed deployment (two internal HTTPS planes,
    one HTTP appliance elsewhere): the block named the HTTPS host as dead, told the operator to
    "check the host before the services", and added the reassurance that the rest of the
    deployment was fine. Nothing had ever been sent to that host. The whole-deployment suppression
    could not catch it, because the healthy HTTP plane defeated the all-or-nothing count.
    """
    cfg = _cfg(
        channelfinder_url="https://internal.example.org:8080/ChannelFinder",
        alarm_url="https://internal.example.org:8081",
        archiver_url="http://appliance.example.org:17665",
    )
    planes = [
        _plane("channelfinder", "ca_error", ca_ok=False),
        _plane("alarm", "ca_error", ca_ok=False),
        _plane("archiver", "ok", ca_ok=True),
    ]
    patterns = _patterns(cfg, planes)
    assert "host_down" not in patterns
    # The TLS half is still reported, and it is the one that names the real cause.
    assert "ca_bundle" in patterns


def test_a_service_error_beside_a_silent_one_does_not_prop_up_the_finding() -> None:
    """A plane that answered with an error is evidence about a SERVICE, not about the host.

    So it neither counts towards "none answered" nor rescues the host as a healthy neighbour. With
    one genuinely silent plane and one erroring plane on the same host there is exactly one silent
    authority, which is below the threshold, and nothing is claimed.
    """
    cfg = _cfg(**_TWO_ON_ONE_HOST, naming_url="http://elsewhere.example.org:8099")
    planes = [
        _plane("channelfinder", "unreachable"),
        _plane("alarm", "api_error"),
        _plane("naming", "ok"),
    ]
    assert "host_down" not in _patterns(cfg, planes)


# --- the TLS comparison ---

_THREE_HTTPS = {
    "channelfinder_url": "https://cf.example.org:8080/ChannelFinder",
    "alarm_url": "https://alarm.example.org:8081",
    "naming_url": "https://naming.example.org:8099",
}


def test_one_failing_https_plane_beside_a_verified_one_points_at_that_host() -> None:
    cfg = _cfg(**_THREE_HTTPS)
    planes = [
        _plane("channelfinder", "ca_error", ca_ok=False),
        _plane("alarm", "ok", ca_ok=True),
        _plane("naming", "ok", ca_ok=True),
    ]
    findings = installation_findings(cfg, planes).findings
    assert [f.pattern for f in findings] == ["trust_root"]
    # It must NOT name a cause: ca_error covers an expired cert and a hostname mismatch too.
    assert "foreign" not in findings[0].detail
    # And it must repeat the whole-trust-store warning, or the invited fix breaks what works.
    assert "combine your internal roots WITH the public ones" in findings[0].detail


def test_a_proper_subset_larger_than_one_still_points_at_those_hosts() -> None:
    """Not "exactly one": the shipped guide already says "the planes whose trust root is missing".

    A threshold of one would make the code contradict prose that ships with the server, and no
    guard compares the two.
    """
    cfg = _cfg(**_THREE_HTTPS)
    planes = [
        _plane("channelfinder", "ca_error", ca_ok=False),
        _plane("alarm", "ca_error", ca_ok=False),
        _plane("naming", "ok", ca_ok=True),
    ]
    assert "trust_root" in _patterns(cfg, planes)


def test_every_https_plane_failing_points_at_the_bundle_instead() -> None:
    """The inverse finding, and it names a different fix.

    Swapping the two branches is the mutant this pair of tests exists to catch.
    """
    cfg = _cfg(**_THREE_HTTPS)
    planes = [
        _plane("channelfinder", "ca_error", ca_ok=False),
        _plane("alarm", "ca_error", ca_ok=False),
        _plane("naming", "ca_error", ca_ok=False),
    ]
    findings = installation_findings(cfg, planes).findings
    assert [f.pattern for f in findings] == ["ca_bundle"]
    assert "trust material this process uses" in findings[0].detail


def test_a_single_https_plane_cannot_discriminate_and_says_nothing() -> None:
    """With n=1 the host and the bundle are indistinguishable, so neither finding is earned."""
    cfg = _cfg(channelfinder_url="https://cf.example.org:8080/ChannelFinder")
    planes = [_plane("channelfinder", "ca_error", ca_ok=False)]
    assert _patterns(cfg, planes) == []


def test_an_http_plane_is_never_counted_as_a_healthy_https_one() -> None:
    """``ca_ok`` is True on every successful path INCLUDING plain http.

    It means "no TLS failure happened", not "TLS was verified". Reading it as the latter would let
    an http plane act as the "other planes are fine" half of a comparison that never ran, and the
    population would then be decided by a field instead of by the scheme.
    """
    cfg = _cfg(
        channelfinder_url="https://cf.example.org:8080/ChannelFinder",
        alarm_url="http://alarm.example.org:8081",
    )
    planes = [
        _plane("channelfinder", "ca_error", ca_ok=False),
        _plane("alarm", "ok", ca_ok=True),
    ]
    assert _patterns(cfg, planes) == []


def test_a_failure_with_no_completed_handshake_anywhere_is_withheld() -> None:
    """ "The others are fine" would be a claim about handshakes that never ran."""
    cfg = _cfg(**_THREE_HTTPS)
    planes = [
        _plane("channelfinder", "ca_error", ca_ok=False),
        _plane("alarm", "unreachable"),
        _plane("naming", "unreachable"),
    ]
    assert "trust_root" not in _patterns(cfg, planes)


# --- properties that hold across every pattern ---


def test_a_disabled_plane_never_enters_a_pattern() -> None:
    """A plane with no URL carries no host and is not part of any comparison."""
    cfg = _cfg(channelfinder_url="http://services.example.org:8080/ChannelFinder")
    planes = [_plane("channelfinder", "unreachable"), _plane("alarm", "disabled")]
    assert _patterns(cfg, planes) == []


def test_an_unreadable_url_is_excluded_rather_than_grouped() -> None:
    """Fail closed: a URL the connecting parser cannot read is no evidence of a shared host."""
    cfg = _cfg(
        channelfinder_url="http://services.example.org:8080/ChannelFinder",
        alarm_url="not a url at all",
    )
    planes = [_plane("channelfinder", "unreachable"), _plane("alarm", "unreachable")]
    assert "host_down" not in _patterns(cfg, planes)


def test_a_throttled_run_produces_no_cross_plane_finding() -> None:
    """BG-DTHR: a plane nobody contacted is evidence about this command's budget, not about a host.

    The exact shape a throttled run can build, and the one that made this block contradict the
    report above it. Measured on the pre-fix code, where a refused probe was classified
    ``unreachable``: both archiver webapps on ONE host, two distinct authorities, with a healthy
    plane elsewhere so the caller-side suppression does not fire either. That produced TWO findings
    at once. ``host_down`` said "2 services on appliance.example.org failed and none on it
    answered", about a host nothing had asked. ``archiver_url_pair`` said "Both archiver webapps
    answered with an error" and sent the operator to check two URL variables for a swap that never
    happened, on a deployment that was correct.

    Both are silenced by membership alone, without a line in either pattern: ``throttled`` is in
    ``_NEVER_TRIGGERS``, so ``failing`` never contains such a plane. That is the whole reason the
    two sets are declared rather than derived.

    Red-proof: move ``throttled`` from ``_NEVER_TRIGGERS`` into ``_TRIGGERS`` and both findings
    come back, which ``test_the_crosscut_sorts_every_plane_status`` cannot see, since the sets
    still tile ``PlaneStatus``.
    """
    cfg = _cfg(**_SPLIT, channelfinder_url="http://elsewhere.example.org:8080/ChannelFinder")
    planes = [
        _plane("archiver", "throttled"),
        _plane("archiver_retrieval", "throttled"),
        _plane("channelfinder", "ok"),
    ]
    assert _patterns(cfg, planes) == []


def test_a_throttled_plane_does_not_complete_a_pair_with_a_real_failure() -> None:
    """The mixed run, which is the one a partial throttle actually produces.

    One archiver half genuinely erroring and the other merely unasked is NOT the swapped-pair
    signature, because that signature rests on BOTH webapps having answered. Without this the
    fix would still leave a false finding on every run where the throttle bit one half and a real
    fault the other.

    Red-proof: same mutation as above; the pair finding returns from a single real failure.
    """
    cfg = _cfg(**_SPLIT)
    planes = [_plane("archiver", "api_error"), _plane("archiver_retrieval", "throttled")]
    assert "archiver_url_pair" not in _patterns(cfg, planes)


_DEAD_PAIR = {
    "archiver_url": "http://dead.example.org:17665",
    "archiver_retrieval_url": "http://dead.example.org:17668",
}


def _host_down_detail(cfg: EpicsConfig, planes: list[PlaneCheck]) -> str | None:
    finding = next(
        (f for f in installation_findings(cfg, planes).findings if f.pattern == "host_down"), None
    )
    return finding.detail if finding else None


def test_a_plane_nobody_asked_is_not_a_host_that_answered() -> None:
    """BG-DTHR post-build review: the first fix put ``throttled`` outside every TRIGGER and left it
    inside the healthy-NEIGHBOUR bucket, which is a different question and was answered wrong.

    ``_host_down`` sorted every non-failing plane into ``healthy_hosts``, so a plane this command
    never contacted was counted as a host that ANSWERED. Measured on the intermediate state, all
    three consequences, which is why three assertions stand here rather than one:

    * the finding names a host nothing had asked as proof that the deployment is otherwise fine;
    * an unasked plane on the DEAD host puts that host into ``healthy_hosts`` and SUPPRESSES the
      finding, silencing a real outage;
    * an unasked plane inflates the denominator of the caller-side suppression, turning a correctly
      suppressed "nothing you asked survived" into a printed one-host claim.

    Red-proof: drop the ``unmeasured`` filter from ``_host_down`` and each assertion fails on its
    own shape.
    """
    # 1. it must not be named as a host that answered
    cfg = _cfg(
        **_DEAD_PAIR,
        channelfinder_url="http://cf.example.org:8080/ChannelFinder",
        olog_url="http://olog.example.org:8080/Olog",
    )
    detail = _host_down_detail(
        cfg,
        [
            _plane("archiver", "unreachable"),
            _plane("archiver_retrieval", "unreachable"),
            _plane("channelfinder", "throttled"),
            _plane("olog", "ok"),
        ],
    )
    assert detail is not None
    assert "olog.example.org" in detail
    assert "cf.example.org" not in detail, "a host nothing contacted cannot prove the rest is fine"

    # 2. and it must not SUPPRESS the finding by sitting on the dead host itself
    cfg_same_host = _cfg(
        **_DEAD_PAIR,
        alarm_url="http://dead.example.org:8081",
        olog_url="http://olog.example.org:8080/Olog",
    )
    assert (
        _host_down_detail(
            cfg_same_host,
            [
                _plane("archiver", "unreachable"),
                _plane("archiver_retrieval", "unreachable"),
                _plane("alarm", "throttled"),
                _plane("olog", "ok"),
            ],
        )
        is not None
    ), "a real outage must not be silenced by a plane nobody asked on the same host"

    # 3. and with it removed, everything that WAS asked failed, which is the caller-side shape
    cfg_only = _cfg(**_DEAD_PAIR, channelfinder_url="http://cf.example.org:8080/ChannelFinder")
    assert (
        _host_down_detail(
            cfg_only,
            [
                _plane("archiver", "unreachable"),
                _plane("archiver_retrieval", "unreachable"),
                _plane("channelfinder", "throttled"),
            ],
        )
        is None
    )


def test_a_plane_that_did_answer_is_still_a_healthy_neighbour() -> None:
    """The control, and it is what keeps the fix above from over-reaching.

    Four statuses are outside every trigger AND genuinely answered: ``ok`` named itself,
    ``unverified`` answered 2xx, ``no_ingest`` and ``backend_down`` answered and identified. For
    them "this host answered" is a measurement, so they must go on refuting the one-host finding.

    Red-proof: widen ``_UNMEASURED`` to any of these four and this goes red while the test above
    stays green.
    """
    cfg = _cfg(**_DEAD_PAIR, olog_url="http://olog.example.org:8080/Olog")
    for status in ("ok", "unverified", "no_ingest", "backend_down"):
        detail = _host_down_detail(
            cfg,
            [
                _plane("archiver", "unreachable"),
                _plane("archiver_retrieval", "unreachable"),
                _plane("olog", status),
            ],
        )
        assert detail is not None and "olog.example.org" in detail, status


def test_the_two_throttled_shapes_are_told_apart_by_what_they_measured() -> None:
    """The correction an adversarial review of the first repair paid for.

    ``throttled`` is not one state. The TRANSPORT arm reports ``reachable=None``: nothing was sent,
    so that host proves nothing. The IDENTITY arm reports ``reachable=True``: the HEAD went out and
    the host answered, and only the beacon was refused. The first repair keyed on the STATUS and
    removed both, so a host that really had answered stopped counting as a healthy neighbour and a
    correct one-host finding was withdrawn.

    Red-proof: key the rule on the status again, or on ``reachable is not False``, and the second
    half goes red.
    """
    cfg = _cfg(**_DEAD_PAIR, olog_url="http://olog.example.org:8080/Olog")
    dead = [_plane("archiver", "unreachable"), _plane("archiver_retrieval", "unreachable")]

    # the transport arm proves nothing about its host, so it neither refutes nor supports
    nothing_sent = _host_down_detail(cfg, [*dead, _plane("olog", "throttled")])
    assert nothing_sent is None, (
        "with the only other plane unasked, nothing that WAS asked survived"
    )

    # the identity arm DID get an answer from its host, so it refutes exactly like an ok plane
    answered = _host_down_detail(
        cfg, [*dead, _plane("olog", "throttled", reachable=True, ca_ok=True)]
    )
    assert answered is not None and "olog.example.org" in answered


def test_the_crosscut_sorts_every_plane_status() -> None:
    """The two declared sets tile ``PlaneStatus`` exactly.

    So a status added to the report is RED here until somebody decides whether a pattern may key
    on it, instead of silently meaning nothing to this module. The annotation is the ``Literal``
    type, so mypy catches a typo in either set as well.
    """
    every = set(typing.get_args(PlaneStatus))
    assert every == _TRIGGERS | _NEVER_TRIGGERS, "a PlaneStatus is in neither set"
    assert not _TRIGGERS & _NEVER_TRIGGERS, "a PlaneStatus is in both sets"


@pytest.mark.parametrize(
    ("cfg", "planes"),
    [
        (
            _cfg(**_SPLIT),
            [_plane("archiver", "api_error"), _plane("archiver_retrieval", "api_error")],
        ),
        (
            _cfg(**_TWO_ON_ONE_HOST, naming_url="http://elsewhere.example.org:8099"),
            [
                _plane("channelfinder", "unreachable"),
                _plane("alarm", "unreachable"),
                _plane("naming", "ok"),
            ],
        ),
        (
            _cfg(**_THREE_HTTPS),
            [
                _plane("channelfinder", "ca_error", ca_ok=False),
                _plane("alarm", "ok", ca_ok=True),
                _plane("naming", "ok", ca_ok=True),
            ],
        ),
    ],
    ids=["archiver_url_pair", "host_down", "trust_root"],
)
def test_every_finding_names_a_variable_and_something_to_do(
    cfg: EpicsConfig, planes: list[PlaneCheck]
) -> None:
    """A plane name is not actionable; a variable is, and the detail has to say what to do.

    Asserted on non-empty CONTENT rather than on presence: a table of empty strings satisfies
    every containment check while telling the reader nothing, which this repository has met before.
    """
    for finding in installation_findings(cfg, planes).findings:
        assert finding.variables and all(v.startswith("EPICS_MCP_") for v in finding.variables)
        assert len(finding.detail) > 80
        assert "check" in finding.detail.lower()
        assert "above" not in finding.detail and "below" not in finding.detail
