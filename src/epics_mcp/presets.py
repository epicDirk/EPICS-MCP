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
#: which is worse than the line being absent. Whoever needs it adds it, and the guide says so.
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
