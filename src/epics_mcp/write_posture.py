"""The two write gates' posture, as pure configuration and nothing else.

Split out of ``services/doctor.py`` because of a measured hazard rather than for tidiness. The
doctor's composition of this posture probes the audit sink first, and that probe opens a file
handle; by its own docstring it can stall without a timeout on an unreachable network share, and
the tempting repair was probed and rejected as cosmetic. A CLI can afford that, because a human
watches it and can interrupt. An MCP ``resources/read`` handler is synchronous and cannot, so the
resource that describes the running server needs the half of the posture that costs nothing.

This module is that half: no filesystem, no socket, no client, no asyncio. Everything here is a
pure function of :class:`~epics_mcp.config.EpicsConfig` plus ``os.environ``, which is what makes it
safe to call from a request handler. The audit sink stays in ``services/doctor.py`` with the probe
it needs.

Both reports resolve their answers through the SAME predicates the real gates apply, never through
a second reading of the configuration: ``write_reach_violations`` for the PV gate's reach,
``write_target_allowed`` and ``is_loopback_url`` for the Olog gate's boundary. A report that
re-derived any of them would be a second opinion, and the one thing worse than no posture report is
one that disagrees with the gate it describes.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict

from epics_mcp.config import EpicsConfig
from epics_mcp.epics_address import (
    CLIENT_REACH_PROVIDERS,
    auto_addr_search_disabled,
    write_reach_violations,
)
from epics_mcp.olog_safety import split_name_list, write_target_allowed
from epics_mcp.services._http import is_loopback_url, url_without_credentials


class _Model(BaseModel):
    """Frozen, closed value object (deterministic; unknown fields rejected).

    Deliberately a second declaration rather than an import from ``services/doctor.py``: taking it
    from there would restore exactly the dependency this module exists to avoid, and the two lines
    of configuration are cheaper than that edge.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


#: The pattern spellings that admit every PV name. A closed SET compared as a string, never an
#: interpretation of the expression: deciding whether an arbitrary regex matches every name is not
#: reliably doable, and a wrong guess would be a silent all-clear on the widest possible allowlist.
_ALLOW_EVERY_PV_NAME = frozenset(
    {
        ".*",
        ".*$",
        "^.*",
        "^.*$",
        ".*?",
        ".*?$",
        "^.*?",
        "^.*?$",
        "(.*)",
        "(.*)$",
        "^(.*)",
        "^(.*)$",
        "(?:.*)",
        "(?:.*)$",
        "^(?:.*)",
        "^(?:.*)$",
        ".{0,}",
        ".{0,}$",
        "^.{0,}",
        "^.{0,}$",
        "[\\s\\S]*",
        "[\\s\\S]*$",
        "^[\\s\\S]*",
        "^[\\s\\S]*$",
        "\\A.*",
        ".*\\Z",
        "\\A.*\\Z",
    }
)


class PvWriteGateReport(_Model):
    """The PV write gate's effective posture, as the launcher's environment has it."""

    #: True iff ``EPICS_MCP_ALLOW_PV_WRITE`` is on. It says the gate is ARMED, never that a write
    #: would succeed: the name allowlist, the rate limit and the start conditions all still apply.
    armed: bool
    #: The regex allowlist verbatim. Empty while armed makes the server REFUSE TO START
    #: (``safety.py``), which is the opposite of "every PV is writable".
    name_pattern: str
    #: True iff the pattern is the sanctioned allow-everything spelling. Compared as a STRING
    #: against a closed set, never by interpreting the expression: deciding whether an arbitrary
    #: regex matches every name is not reliably doable, and a wrong guess would be a silent
    #: all-clear on the widest possible allowlist.
    pattern_allows_every_name: bool
    #: True iff the pattern COMPILES. False is a FOURTH start condition of this gate, and the one
    #: the report used to hide: ``SafetyLayer`` compiles the pattern at construction and raises
    #: ``SafetyConfigError`` when it cannot, so a typo in the allowlist is a refuse-to-start. Until
    #: this field existed the block printed the broken pattern exactly like a working narrow one,
    #: under a line saying the whole name has to match it. Compiling is safe here where EXECUTING
    #: is not: ``re.compile`` does not run the expression against any input, so a catastrophically
    #: backtracking pattern costs nothing (measured: the compile of a nested-quantifier pattern is
    #: immediate; only a match against a long subject is not). Vacuously True for the empty
    #: pattern, which has its own line and its own start condition.
    pattern_is_valid_regex: bool
    #: Writes admitted per 60 s window.
    rate_limit_per_minute: int
    #: Every way the EPICS client search reach extends beyond loopback, from
    #: :func:`~epics_mcp.epics_address.write_reach_violations`, the SAME function the gate calls at
    #: construction. Non-empty while ``armed`` means the process refuses to start.
    #: ⚠️ This can disagree with the live plane's own search-posture line, and the disagreement is
    #: real rather than a defect: that line judges the ACTIVE provider's auto-address switch only,
    #: this one covers both providers, because the write gate does. (The address lists themselves
    #: are read for both providers on either side; it is the auto-address switch that differs.)
    #: ⚠️ The strings name RAW hosts and RAW environment values, so this field is for an operator's
    #: own terminal. Anything shipping to a client projects the posture field by field and leaves
    #: this one out.
    search_reach_violations: list[str]


class OlogWriteGateReport(_Model):
    """The Olog logbook write gate's effective posture.

    A SEPARATE gate: nothing here is implied by the PV one, and the two share only the audit sink.
    """

    armed: bool
    #: The exact, case-sensitive logbook names a write may target, sorted. EMPTY while armed is
    #: DENY-ALL rather than unrestricted (``olog_safety``), which is why the render says so in
    #: words: the naive reading of an empty allowlist is the exact opposite of what it means.
    logbooks: list[str]
    rate_limit_per_minute: int
    #: The configured Olog base URL, credentials redacted. Empty when the plane is off.
    target_url: str
    #: True iff a write to that URL would pass the gate's test-server boundary, decided by
    #: :func:`~epics_mcp.olog_safety.write_target_allowed`, the SAME predicate the gate applies.
    target_allowed: bool
    #: True iff the target is a loopback host, i.e. a local test server. Deliberately separate from
    #: ``target_allowed``, which is ALSO True for an allowlisted REMOTE https target: that one
    #: reaches a real logbook, and reading the two as one is how a sandbox posture gets claimed for
    #: a production one.
    target_is_loopback: bool


class PvSearchPosture(_Model):
    """How far a PV search from THIS process can travel, said without naming a single address.

    The addresses themselves are what ``epics-doctor`` prints into an operator's own terminal. This
    is the shape a client may keep: it answers the question ``docs/deployment.md`` actually poses,
    which is not "which subnet" but "does this broadcast without anyone asking it to", and it
    answers it without putting a facility host into a transcript.
    """

    #: Per PROVIDER, because the two parsers disagree about what disables the broadcast: pvxs takes
    #: only ``NO`` or ``0``, libca takes any value CONTAINING ``no``. One combined flag would have
    #: to pick a reading and would be wrong for the other client. Unset means ON for both, which is
    #: the default that is easy to miss.
    auto_addr_broadcast: dict[str, bool]
    #: The NAMES of the search-list variables that carry a value, never the values. Their vocabulary
    #: is closed and generated from the provider list, so this cannot leak an address.
    search_lists_set: list[str]
    #: True iff the reach is provably loopback-only, from the SAME function the PV write gate calls
    #: at construction, so this bit and that gate's start condition cannot disagree.
    #: ⚠️ It judges BOTH providers, while ``auto_addr_broadcast`` reports them separately. A process
    #: with the active provider's broadcast disabled and the idle one's still on is loopback_only
    #: False with one True entry above, and that is the truth rather than a contradiction: the write
    #: gate refuses to start on the idle provider too, because the variables are process-global.
    loopback_only: bool


def pv_search_posture(environ: Mapping[str, str]) -> PvSearchPosture:
    """The search posture of *environ*, address-free.

    Takes the environment rather than reading ``os.environ`` itself, so the answer is a function of
    its argument and a test does not have to mutate process state to ask a question.
    """
    return PvSearchPosture(
        auto_addr_broadcast={
            provider: not auto_addr_search_disabled(
                provider, environ.get(f"EPICS_{provider.upper()}_AUTO_ADDR_LIST", "")
            )
            for provider in CLIENT_REACH_PROVIDERS
        },
        # Same construction as write_reach_violations walks, so a variable that would produce a
        # violation is a variable named here; only the VALUE is dropped.
        search_lists_set=[
            var
            for provider in CLIENT_REACH_PROVIDERS
            for suffix in ("ADDR_LIST", "NAME_SERVERS")
            if (var := f"EPICS_{provider.upper()}_{suffix}") and environ.get(var, "").strip()
        ],
        loopback_only=not write_reach_violations(environ),
    )


def compiles_as_regex(pattern: str) -> bool:
    """Does *pattern* compile? The question ``SafetyLayer`` asks at construction, asked here safely.

    ``re.compile`` only PARSES, so this is not the pattern-execution the neighbouring flag refuses
    to do: nothing is matched against any subject and a catastrophically backtracking expression
    costs no more than its parse. ``re.error`` is the documented failure; ``TypeError`` and
    ``ValueError`` are caught alongside it for the same reason ``safety.py`` widened its own audit
    clause, a config that bypassed validation can hold a non-string.
    """
    if not pattern:
        return True  # the empty case is a start condition of its own, with its own line
    try:
        re.compile(pattern)
    except (re.error, TypeError, ValueError):
        return False
    return True


def pv_write_gate_report(cfg: EpicsConfig) -> PvWriteGateReport:
    """The PV write gate's posture. Builds NO gate, and that is a requirement, not an optimisation.

    Constructing ``SafetyLayer`` raises on exactly the configurations this report exists to
    describe: writes armed with an empty allowlist, an allowlist that does not compile, or a search
    reach beyond loopback. Nor is that theoretical, ``epics-init`` puts the block it has just
    composed into ``os.environ`` and reports on it, so a report that constructed a gate would die on
    a configuration the onboarding command had just handed the user.
    """
    return PvWriteGateReport(
        armed=cfg.allow_pv_write,
        name_pattern=cfg.pv_write_pattern,
        pattern_allows_every_name=cfg.pv_write_pattern in _ALLOW_EVERY_PV_NAME,
        pattern_is_valid_regex=compiles_as_regex(cfg.pv_write_pattern),
        rate_limit_per_minute=cfg.write_rate_limit,
        search_reach_violations=write_reach_violations(os.environ),
    )


def olog_write_gate_report(cfg: EpicsConfig) -> OlogWriteGateReport:
    """The Olog write gate's posture. Builds no gate either, for a second reason.

    ``OlogWriteGate`` constructs a file audit logger on the way, which is filesystem work this
    module promises not to do.
    """
    return OlogWriteGateReport(
        armed=cfg.allow_olog_write,
        logbooks=sorted(split_name_list(cfg.olog_write_logbooks)),
        rate_limit_per_minute=cfg.olog_write_rate_limit,
        # REBUILT without its userinfo, not pattern-redacted. The regex redactor matches up to the
        # FIRST ``@`` while urllib3, the parser that decides the boundary two lines down, splits the
        # authority at the LAST one, so a password containing ``@`` keeps its tail in the clear
        # under it (measured: ``https://svc:hun@ter2@host/Olog`` came out as
        # ``https://***@ter2@host/Olog``, and a bare ``svc@host`` username was untouched). This is
        # printed on EVERY run, where the old error path printed it only on a failure, so a partial
        # redaction here would be a new and routine exposure.
        target_url=url_without_credentials(cfg.olog_url) if cfg.olog_url else "",
        target_allowed=write_target_allowed(cfg),
        target_is_loopback=is_loopback_url(cfg.olog_url),
    )
