"""Ready-made ``EPICS_MCP_*`` environment blocks for the four common deployment shapes.

WHY this exists. Configuring this server means setting environment variables in whatever LAUNCHES
it, and there are enough of them that a newcomer's first question is not "which value" but "which
variables at all". ``.env.example`` answers that exhaustively, which is the right shape for a
reference and the wrong shape for a first run: it lists every recognised variable, so the reader
has to know the answer in order to find it. A preset inverts that. It names a SHAPE ("an IOC and
an archiver, nothing else") and emits exactly the variables that shape needs.

Deliberately DATA, not behaviour. This module holds no I/O and no argument parsing, so the four
shapes can be asserted against ``examples/mcp.json`` and against ``EpicsConfig`` without going
through a command line. ``cli_init`` is the only thing that renders or probes them.

Placeholders are written ``<like-this>`` because ``README.md`` already spells one that way
(``EPICS_PVA_ADDR_LIST=<gateway-or-ioc-host>``), so this introduces no second convention. The form
matters beyond style: it is what :func:`open_placeholders` detects, and an unreplaced placeholder
is the one state in which running the doctor would produce a guaranteed failure that reads like a
real finding.

Ports and paths follow ``.env.example`` (ChannelFinder 8080 with its context path, archiver mgmt
17665 and retrieval 17668, alarm 8081, Olog 8080 with its context path), so a reader who compares
the two sees the same numbers rather than a second set to reconcile.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

__all__ = [
    "AMBIENT_GROUPS",
    "PRESETS",
    "REST_PLANE_VARS",
    "SERVER_COMMAND",
    "SERVER_KEY",
    "AmbientGroup",
    "AmbientReport",
    "AmbientSurface",
    "Preset",
    "ambient_influences",
    "configures_a_rest_plane",
    "decides_tls_explicitly",
    "format_listing",
    "open_placeholders",
    "render_client_config",
    "stale_config_vars",
    "tls_decision",
    "with_overrides",
]

#: The key this server is conventionally registered under in an MCP client config, and the console
#: command that starts it. Both are held here rather than spelled inline, because
#: ``tests/test_presets_match_examples.py`` asserts them against ``examples/mcp.json`` and against
#: ``[project.scripts]``: three copies of a name drift, one copy plus two assertions does not.
SERVER_KEY = "epics-pv"
SERVER_COMMAND = "epics-mcp"

#: A placeholder a human still has to replace. Anchored on the angle brackets rather than on a word
#: list, so a new preset cannot introduce a placeholder this fails to see.
#:
#: The character class covers UPPER and mixed case and the underscore, which the first version did
#: not, and both halves of that were measured rather than imagined (QA-68). The presets themselves
#: spell their placeholders ``<lower-case-with-hyphens>``, so a lower-case-only pattern looked
#: sufficient; but the values this is applied to include everything a caller passes with ``--set``,
#: and ``--set EPICS_MCP_ARCHIVER_URL=http://<ARCHIVER-HOST>:17665`` went straight past it. The
#: check then RAN and reported ``Failed to resolve '%3carchiver-host%3e'``: a DNS failure shaped
#: exactly like a genuine finding, url-encoded into something even harder to recognise as an
#: unfilled blank. That is the report this refusal exists to prevent.
#:
#: The three lookbehinds carve out Python's named-group syntax, the one legitimate value in this
#: configuration that carries angle brackets: ``EPICS_MCP_PV_WRITE_PATTERN`` is a REGEX, and
#: ``^SIM:(?P<dev>PS-01):Cur-SP$`` was read as a placeholder, so a COMPLETE configuration had its
#: check silently refused. ``(?P<``, ``(?<`` and ``\k<`` are spelled out because they are the forms
#: a regex can carry; a general "is this value a regex" test does not exist and guessing one would
#: be a second, weaker copy of ``re``'s own parser.
#:
#: Honest limit, named rather than papered over: a placeholder with a SPACE in it
#: (``<archiver host>``) is still not seen. Admitting whitespace would let the pattern reach across
#: unrelated text, and no preset spells one that way; the two forms above are the ones a reader
#: actually types when copying the emitted block.
_PLACEHOLDER_RE = re.compile(r"(?<!\?)(?<!\?P)(?<!\\k)<[A-Za-z][A-Za-z0-9_-]*>")

#: The prefix ``EpicsConfig`` binds to (``model_config = {"env_prefix": "EPICS_MCP_"}``).
_CONFIG_PREFIX = "EPICS_MCP_"

#: The EPICS search-path variables the doctor reads DIRECTLY from ``os.environ`` rather than
#: through ``EpicsConfig``, because they belong to the EPICS client libraries and carry no
#: ``EPICS_MCP_`` prefix. Mirrors ``doctor._SEARCH_LIST_VARS`` plus the two auto-search switches.
#: Listed rather than matched on a prefix so that transport tuning a preset says nothing about
#: (ports, buffer sizes) is left alone: probing a preset should answer "is THIS configuration
#: sound", not "is this machine sound after I stripped it".
_SEARCH_VARS = frozenset(
    {
        "EPICS_PVA_ADDR_LIST",
        "EPICS_CA_ADDR_LIST",
        "EPICS_PVA_NAME_SERVERS",
        "EPICS_CA_NAME_SERVERS",
        "EPICS_PVA_AUTO_ADDR_LIST",
        "EPICS_CA_AUTO_ADDR_LIST",
    }
)

#: The variables whose presence ENABLES a REST plane, named explicitly rather than matched on a
#: ``_URL`` suffix. The suffix looks tempting and is wrong: ``EPICS_MCP_OLOG_WRITE_URL_ALLOWLIST``
#: carries it and enables nothing. Used to tell a preset that probing can confirm something from
#: one where the doctor would have nothing to do.
REST_PLANE_VARS = frozenset(
    {
        "EPICS_MCP_CHANNELFINDER_URL",
        "EPICS_MCP_ARCHIVER_URL",
        "EPICS_MCP_ARCHIVER_RETRIEVAL_URL",
        "EPICS_MCP_ALARM_URL",
        "EPICS_MCP_NAMING_URL",
        "EPICS_MCP_OLOG_URL",
    }
)

#: The two block settings that make an EXPLICIT TLS decision, which is what silences the ambient
#: HTTP environment. ``build_retrying_session`` resolves ``verify = ca_bundle or tls_verify`` and
#: pins ``trust_env=False`` whenever the result is anything other than plain ``True``; that switch
#: turns off the proxy environment and the ``*_CA_BUNDLE`` environment together
#: (``services/_http.py``, measured with a negative control: with ``trust_env=False`` requests
#: merges no proxies and keeps ``verify`` at ``True``).
_CA_BUNDLE_VAR = "EPICS_MCP_CA_BUNDLE"
_TLS_VERIFY_VAR = "EPICS_MCP_TLS_VERIFY"

#: The spellings ``EPICS_MCP_TLS_VERIFY`` accepts for FALSE. Held here rather than re-parsed,
#: because this module has no I/O and must not build the config singleton (see ``cli_init``: the
#: singleton would freeze the caller's PRE-strip environment). Deliberately not an import of
#: pydantic's parser; ``tests/test_cli_init.py`` pins these against ``EpicsConfig`` itself, so a
#: widened parser is a red test rather than a silent disagreement. Same shape as the coupling guard
#: on ``display_files.INVENTORY_SUFFIXES``.
_FALSE_SPELLINGS = frozenset({"0", "off", "false", "f", "n", "no"})

#: How to take a variable out for ONE run, spelled for both shells this project is used from. A
#: bare "unset it" is not an instruction: ``unset`` changes the shell rather than the run, and it
#: does not exist in PowerShell at all, which is the default console on the machine this is
#: developed on. A remedy a reader cannot type is the same as no remedy.
_UNSET_HOWTO = "POSIX: env -u NAME epics-init ...; PowerShell: Remove-Item Env:NAME, then re-run"


#: What a group acts on. ONE field rather than two flags that happen to agree today, because both
#: conditions follow from the same fact: a ``rest-http`` group can only bite when the block enables
#: a REST plane at all, and an explicit TLS decision pins ``trust_env=False`` and takes the whole
#: ambient HTTP environment out of play. An ``epics-search`` group is neither: it acts on the live
#: plane, which is always present, and no TLS setting reaches it.
AmbientSurface = Literal["rest-http", "epics-search"]


@dataclass(frozen=True)
class AmbientGroup:
    """One family of variable the caller's SHELL carries into a check, and the handle for it.

    *effect* says what a leftover member does to the report; *remedy* says what to do about it. Both
    are required, and the second is the point: a warning that names a cause without naming an action
    becomes noise, and noise gets dismissed. Four messages in this repository were measured to read
    as an invitation to switch the check OFF rather than repair the cause.

    ⚠ A REMEDY IS ALSO A THING SOMEBODY WILL DO, which is the harder half and was got wrong here
    once. The first version of the proxy remedy offered ``EPICS_MCP_CA_BUNDLE`` as a way to take the
    group out of play. That is TRUE and it is bad advice: the variable goes into the emitted block,
    so it reconfigures the SERVER the reader is about to run, replacing its whole trust store and
    switching off its proxy, and the warning disappearing looks like a repair. A remedy that
    silences a symptom by changing production belongs nowhere near a message like this.
    """

    variables: tuple[str, ...]
    effect: str
    remedy: str
    surface: AmbientSurface


#: What the caller's shell keeps contributing to a probe AFTER :func:`stale_config_vars` has run.
#:
#: WHY this exists, and why it is one list. ``epics-init`` strips what it OWNS (``EPICS_MCP_*`` plus
#: the six search-path variables) and applies a preset on top, so the report describes the block it
#: just printed. That claim is only true up to the variables it does NOT strip, and stripping those
#: is the wrong repair: it would break every site that reaches the network through a proxy and every
#: site with an internal CA. So the report NAMES them instead (decision UE, way (b)). A list like
#: this ages, which is why it lives at exactly one address and carries its own admission criterion:
#:
#:     a variable belongs here when it changes WHO ANSWERS or WHETHER an answer arrives.
#:     Transport tuning (buffer sizes, timeouts) does not, however loudly it is set.
#:
#: Measured on this installation rather than recalled, each family with a negative control:
#:
#: * The proxy and CA-bundle names are the ones ``requests`` really merges from the environment
#:   (probed through ``Session.merge_environment_settings``). ``SSL_CERT_FILE`` and ``SSL_CERT_DIR``
#:   are deliberately ABSENT: they were on the first draft of this list and the probe showed
#:   ``verify`` unchanged at ``True``, because requests names certifi's bundle explicitly instead of
#:   falling back to OpenSSL's default paths. ``FTP_PROXY`` is read as well and is also absent: no
#:   plane here speaks ftp, so it cannot reach a request.
#: * Both spellings of each proxy variable are listed because both are read, and the LOWER case one
#:   won on this platform when the two disagreed.
#: * ``EPICS_PVA_BROADCAST_PORT`` and ``EPICS_PVA_SERVER_PORT`` are the port variables the installed
#:   p4p/pvxs actually recognises on the client side (the ``EPICS_PVAS_*`` family is the SERVER
#:   half and cannot apply here). The first is the one measured to flip the live plane from ``ok``
#:   to ``disconnected`` while the report's ``search paths:`` line stayed word for word the same
#:   (QA-69, 2026-08-01).
#: * ⚠️ The two CA-protocol ports are NOT measured here, they are the same role in the other
#:   protocol and are listed on that reasoning alone. Said out loud rather than blended in with the
#:   measured ones.
AMBIENT_GROUPS: tuple[AmbientGroup, ...] = (
    AmbientGroup(
        variables=(
            "ALL_PROXY",
            "HTTPS_PROXY",
            "HTTP_PROXY",
            "NO_PROXY",
            "all_proxy",
            "http_proxy",
            "https_proxy",
            "no_proxy",
        ),
        effect=(
            "every REST plane is contacted THROUGH the proxy named here, so a healthy plane can be "
            "reported unreachable, and the failure names a host that appears nowhere in the block "
            "above. Your server will inherit whatever its launcher sets, which is usually not this "
            "shell, so check and runtime can disagree in either direction"
        ),
        remedy=(
            "carry them INTO the block, which makes check and server agree: --set "
            "HTTP_PROXY=<url> --set NO_PROXY=<hosts>. To take them out instead, remove ALL of them "
            "together for this run, never some: NO_PROXY is the EXEMPTION list, and dropping it "
            "alone sends internal hosts through the proxy that were bypassing it "
            f"({_UNSET_HOWTO}). Do NOT reach for {_CA_BUNDLE_VAR} to make this message go away: it "
            "would silence it by making the SERVER ignore your proxy and trust only that one "
            "bundle"
        ),
        surface="rest-http",
    ),
    AmbientGroup(
        variables=("CURL_CA_BUNDLE", "REQUESTS_CA_BUNDLE"),
        effect=(
            "the TLS trust store of the READ sessions is replaced, so an HTTPS plane can pass here "
            "and fail for the running server, which does not inherit your shell"
        ),
        remedy=(
            f"put the bundle in the block as --set {_CA_BUNDLE_VAR}=<path>, which is the setting "
            "the server itself reads. ⚠ That path becomes the WHOLE trust store, so combine your "
            "internal CA with the public roots into one PEM first (docs/deployment.md section 3), "
            "or a plane signed by a public CA starts failing"
        ),
        surface="rest-http",
    ),
    AmbientGroup(
        variables=(
            "EPICS_CA_REPEATER_PORT",
            "EPICS_CA_SERVER_PORT",
            "EPICS_PVA_BROADCAST_PORT",
            "EPICS_PVA_SERVER_PORT",
        ),
        effect=(
            "the PORT a PV search goes to is decided here, so this decides WHO answers; the "
            "check's 'search paths:' line reports the addresses only and says nothing about it. "
            "Your server inherits its launcher's environment, not this shell, so a port set only "
            "here means the check and the running server search different ones"
        ),
        remedy=(
            "state it in the block with --set NAME=VALUE, so the configuration you hand your "
            f"client is the one that was checked; or take it out for this run ({_UNSET_HOWTO})"
        ),
        surface="epics-search",
    ),
)


#: Loopback-only PV search: every route a PV search could take points at 127.0.0.1, and the two
#: subnet-broadcast switches are OFF. Held as a name because getting it WRONG is silent: EPICS
#: defaults its auto-address search to ON, so omitting the two ``*_AUTO_ADDR_LIST`` lines
#: broadcasts PV searches into the local subnets while looking like a narrower config than it is.
#:
#: ``EPICS_PVA_NAME_SERVERS`` is the TCP-unicast route, and it is here because the UDP one alone
#: does not reach a CONTAINERISED IOC: a container usually publishes its PVA TCP port and no UDP
#: search port, so the broadcast finds nothing while the IOC is perfectly healthy. Measured over
#: all four cells, both IOC shapes against both configurations: adding this line turns the
#: container from ``disconnected`` to ``connected`` and leaves a NATIVE server connected, because
#: the UDP search below still runs alongside it. That fourth cell is the one that decides between
#: repairing this and merely documenting it, so it is measured rather than assumed.
#:
#: ⚠️ Two claims that used to stand here were wrong and are gone: this is used by ONE preset
#: (``sandbox``), not three, and ``epics-doctor`` does NOT report it as ``localhost-isolated``,
#: which it claims only when every search list is UNSET. It prints the loopback search paths.
_LOOPBACK_SEARCH = {
    "EPICS_MCP_PROVIDER": "pva",
    "EPICS_PVA_AUTO_ADDR_LIST": "NO",
    "EPICS_CA_AUTO_ADDR_LIST": "NO",
    "EPICS_PVA_ADDR_LIST": "127.0.0.1",
    "EPICS_CA_ADDR_LIST": "127.0.0.1",
    "EPICS_PVA_NAME_SERVERS": "127.0.0.1:5075",
}

#: The same posture pointed at a real IOC or gateway instead of loopback. It repeats the five
#: keys above on purpose: a preset that reaches a facility must still disable the auto search
#: explicitly. It deliberately does NOT repeat the sixth, ``EPICS_PVA_NAME_SERVERS``: that one
#: needs a host AND a port, and this preset does not know the port. Emitting a guessed one would
#: put a wrong value in front of a reader who has no way to tell it apart from a measured one,
#: which is worse than the line being absent. Whoever needs it adds it, and ``docs/deployment.md``
#: section 5 ("An IOC that does not answer a broadcast") says so, with the trade-off spelled out.
#: ⚠️ That pointer used to say "the guide", meaning the shipped operator guide, and it was repointed
#: when the guide's copy of this advice was removed as a duplicate: the deployment page carries the
#: fuller version, including that this route needs the exact port where the broadcast does not.
_FACILITY_SEARCH = {
    "EPICS_MCP_PROVIDER": "pva",
    "EPICS_PVA_AUTO_ADDR_LIST": "NO",
    "EPICS_CA_AUTO_ADDR_LIST": "NO",
    "EPICS_PVA_ADDR_LIST": "<gateway-or-ioc-host>",
    "EPICS_CA_ADDR_LIST": "<gateway-or-ioc-host>",
}


@dataclass(frozen=True)
class Preset:
    """One deployment shape: a name, a one-line purpose, and the variables it sets.

    ``env`` is ordered, and the order is part of the output a reader sees: the PV search posture
    first, then the REST planes in the order ``docs/deployment.md`` introduces them. Frozen because
    a preset is a constant; :func:`with_overrides` returns a new mapping rather than mutating one.
    """

    name: str
    summary: str
    env: Mapping[str, str]


#: The four shapes, narrowest first. Order is the order ``--list`` prints, and it doubles as the
#: intended reading order: each preset is the previous one plus one more plane.
PRESETS: Mapping[str, Preset] = {
    "sandbox": Preset(
        name="sandbox",
        summary=(
            "Loopback only: a PV server on this machine, no REST planes. The shape to start with, "
            "and the one preset with no value left to fill in. Run 'epics-testpv' for something "
            "to talk to if you have no IOC yet. It searches both ways a PVA client can, UDP "
            "broadcast to 127.0.0.1 and TCP unicast to 127.0.0.1:5075, so it reaches a server "
            "running natively on this host AND one in a container, which usually publishes only "
            "its TCP port. Change the port if yours serves another."
        ),
        env=dict(_LOOPBACK_SEARCH),
    ),
    "ioc-only": Preset(
        name="ioc-only",
        summary=(
            "Live PVs from your own IOC or gateway, no REST planes. Replace the search address "
            "with the host your IOC or gateway answers on."
        ),
        env=dict(_FACILITY_SEARCH),
    ),
    "ioc-archiver": Preset(
        name="ioc-archiver",
        summary=(
            "Live PVs plus an Archiver Appliance, the most common partial deployment. If your "
            "retrieval webapp runs on its own port, add "
            "--set EPICS_MCP_ARCHIVER_RETRIEVAL_URL=http://<archiver-host>:17668 as well."
        ),
        env={
            **_FACILITY_SEARCH,
            "EPICS_MCP_ARCHIVER_URL": "http://<archiver-host>:17665",
        },
    ),
    "full": Preset(
        name="full",
        summary=(
            "Every plane this server speaks: live PVs, ChannelFinder, Archiver (mgmt and "
            "retrieval), Alarm Logger, Naming Service and Olog. Delete the lines you have no "
            "service for; an unset URL disables that plane with no network call. The one "
            "exception is EPICS_MCP_ARCHIVER_RETRIEVAL_URL, which falls back to the mgmt URL "
            "rather than switching retrieval off."
        ),
        env={
            **_FACILITY_SEARCH,
            "EPICS_MCP_CHANNELFINDER_URL": "http://<channelfinder-host>:8080/ChannelFinder",
            "EPICS_MCP_ARCHIVER_URL": "http://<archiver-host>:17665",
            "EPICS_MCP_ARCHIVER_RETRIEVAL_URL": "http://<archiver-host>:17668",
            "EPICS_MCP_ALARM_URL": "http://<alarm-host>:8081",
            "EPICS_MCP_NAMING_URL": "https://<naming-host>/",
            "EPICS_MCP_OLOG_URL": "http://<olog-host>:8080/Olog",
        },
    ),
}


def open_placeholders(env: Mapping[str, str]) -> list[str]:
    """Return the ``VAR=<placeholder>`` entries a human still has to replace, sorted.

    Used to decide whether probing the configuration can say anything. Running the doctor against
    ``<archiver-host>`` produces a DNS failure with the shape of a genuine finding, which is worse
    than not running it: the reader cannot tell "your archiver is down" from "you have not filled
    this in yet". Returning the entries rather than a boolean lets the caller name them.
    """
    return sorted(f"{key}={value}" for key, value in env.items() if _PLACEHOLDER_RE.search(value))


def with_overrides(env: Mapping[str, str], overrides: Mapping[str, str]) -> dict[str, str]:
    """Return *env* with *overrides* applied, preserving the preset's key order.

    A key not already in the preset is APPENDED rather than rejected: the presets cover the common
    shapes, not every variable ``EpicsConfig`` reads, so ``--set`` has to be able to add one
    (a CA bundle, a rate limit). Validity is ``EpicsConfig``'s job at start-up, not this
    function's; guessing here would mean a second, weaker copy of that schema.
    """
    merged = dict(env)
    merged.update(overrides)
    return merged


def configures_a_rest_plane(env: Mapping[str, str]) -> bool:
    """True when *env* enables at least one plane the doctor can identify.

    A preset that enables none (``sandbox``, ``ioc-only``) leaves the doctor with nothing to
    verify unless it is also given a PV to read: the live plane without ``--probe-pv`` reports its
    posture and makes no network call at all. Callers use this to say so out loud instead of
    printing a clean report that confirms nothing.
    """
    return any(name in REST_PLANE_VARS and value for name, value in env.items())


def stale_config_vars(environ: Iterable[str]) -> list[str]:
    """Return the names in *environ* that configure THIS server, sorted.

    WHY this exists, and it is the whole correctness of probing a preset. ``epics-doctor`` reads
    the process environment, never a file, so probing a preset means putting the preset INTO the
    environment first. Merely adding it is not enough: a variable the caller already exported and
    the preset does not mention would survive and be probed as though it were part of the preset.
    That is not hypothetical, it is the normal case for anyone who already runs this server, and
    the resulting report would describe a configuration that exists nowhere, mixing half the
    preset with half the shell.

    So the caller REMOVES these first, then applies the preset. The set is everything
    ``EpicsConfig`` binds plus the EPICS search-path variables the doctor reads directly. Transport
    tuning outside both (ports, buffer sizes) is deliberately left in place, because the question
    is whether this CONFIGURATION is sound, not whether the machine is.
    """
    return sorted(
        name for name in environ if name.startswith(_CONFIG_PREFIX) or name in _SEARCH_VARS
    )


def tls_decision(env: Mapping[str, str]) -> str | None:
    """The setting in *env* that makes the read sessions ignore the ambient HTTP environment.

    Returns the variable NAME responsible, or ``None``. The two are not symmetric and both have to
    be read: a non-empty ``CA_BUNDLE`` is a decision by its presence, while ``TLS_VERIFY`` is one
    only when it is FALSE (its default, true, is what leaves ``trust_env`` on in the first place).
    Either way the resulting ``verify`` is not plain ``True``, which is the condition
    ``build_retrying_session`` pins ``trust_env=False`` on.

    The NAME rather than a boolean, because the two have very different things to say to a reader:
    a bundle is a deliberate trust configuration, verification-off is a last resort, and a report
    that fell silent for the second reason without saying so would look like a clean environment.
    """
    if env.get(_CA_BUNDLE_VAR, "").strip():
        return _CA_BUNDLE_VAR
    if env.get(_TLS_VERIFY_VAR, "").strip().lower() in _FALSE_SPELLINGS:
        return _TLS_VERIFY_VAR
    return None


def decides_tls_explicitly(env: Mapping[str, str]) -> bool:
    """True when *env* itself decides TLS. The boolean face of :func:`tls_decision`."""
    return tls_decision(env) is not None


@dataclass(frozen=True)
class AmbientReport:
    """What the caller's shell contributes to a check, and what provably cannot contribute.

    *findings* are ``(group, names)`` pairs worth printing. *tls_silenced* names the HTTP variables
    that ARE set and were withheld because the block decides TLS: they are reported separately
    rather than dropped, because a reader whose environment stops being mentioned would otherwise
    read that as "my environment is clean" when the truth is "the sessions ignore it now".
    *tls_decided_by* is the setting responsible, or ``None``.
    """

    findings: tuple[tuple[AmbientGroup, tuple[str, ...]], ...]
    tls_silenced: tuple[str, ...]
    tls_decided_by: str | None


def ambient_influences(environ: Iterable[str], env: Mapping[str, str]) -> AmbientReport:
    """The shell variables that survive the strip AND can still change what the check reports.

    The whole question in ONE call, deliberately: which families exist, which of their members the
    caller has set, and which of them provably cannot act on THIS block. Splitting any of that out
    to the caller would put half the answer next to the printing code, which is the scattering the
    decision's own proviso forbids.

    *environ* is the caller's variable NAMES, the same shape :func:`stale_config_vars` takes, and
    for the same reason: no value is needed to name a variable, and a proxy URL can carry a
    password. *env* is the COMPOSED block.

    Three subtractions, each of which was missing once and each of which made the message WRONG
    rather than merely noisy:

    * **A name the block itself states is not ambient.** ``_run_check`` applies the block ON TOP of
      the environment, so the block's value is the one that acts. Without this, the remedy "state
      it in the block with --set" produced the identical warning on the next run, under a heading
      claiming these are not part of the block. Measured; a remedy that visibly does not work is
      the fastest way to teach a reader to skip the whole paragraph.
    * **An HTTP group cannot act on a block with no REST plane.** ``sandbox`` and ``ioc-only``
      enable none, so a proxy has nothing to route: the very first command
      ``docs/deployment.md`` teaches would have asserted an effect on "every REST plane" for a
      block that has none.
    * **An explicit TLS decision pins ``trust_env=False``**, which switches off the proxy and
      ``*_CA_BUNDLE`` environment together. Those names go to ``tls_silenced`` rather than
      vanishing.

    The same reasoning is applied in all three, and it is the one the coverage audit uses about its
    own figures: name a cause that can bite, never one that provably cannot.
    """
    present = set(environ)
    stated = set(env)
    decided_by = tls_decision(env)
    rest_plane = configures_a_rest_plane(env)
    findings: list[tuple[AmbientGroup, tuple[str, ...]]] = []
    silenced: list[str] = []
    for group in AMBIENT_GROUPS:
        names = tuple(n for n in group.variables if n in present and n not in stated)
        if not names:
            continue
        if group.surface == "rest-http":
            if decided_by is not None:
                silenced.extend(names)
                continue
            if not rest_plane:
                continue  # nothing to route: this block enables no REST plane at all
        findings.append((group, names))
    return AmbientReport(
        findings=tuple(findings), tls_silenced=tuple(silenced), tls_decided_by=decided_by
    )


def render_client_config(env: Mapping[str, str], *, command: str = SERVER_COMMAND) -> str:
    """Render *env* as the ``.mcp.json`` block an MCP client expects, without a trailing newline.

    Two spaces of indentation and no key sorting, so the output is diffable against
    ``examples/mcp.json`` and keeps the deliberate variable order of the preset. The caller decides
    the trailing newline, because this string is also embedded in prose.

    *command* is keyword-only and defaults to ``SERVER_COMMAND``, so the emitted block is unchanged
    unless a caller deliberately asks for something else. The one caller that does is
    ``epics-init --absolute-command``, for a client that cannot resolve a bare name on its own PATH.
    Resolving it is NOT done here: this module holds no I/O (see the module docstring), and a
    filesystem lookup is exactly that.
    """
    document = {"mcpServers": {SERVER_KEY: {"command": command, "env": dict(env)}}}
    return json.dumps(document, indent=2)


def format_listing(presets: Iterable[Preset]) -> str:
    """Render the ``--list`` output: one paragraph per preset, name then purpose then variables.

    The variable NAMES are listed but not their values, because the question ``--list`` answers is
    "which shape am I", not "what will it say"; the values are one ``--preset`` away.
    """
    blocks: list[str] = []
    for preset in presets:
        variables = ", ".join(preset.env)
        blocks.append(f"{preset.name}\n    {preset.summary}\n    sets: {variables}")
    return "\n\n".join(blocks)
