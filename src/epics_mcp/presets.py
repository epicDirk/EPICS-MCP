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
_PLACEHOLDER_RE = re.compile(r"<[a-z][a-z0-9-]*>")

#: Loopback-only PV search, the posture ``epics-doctor`` reports as ``localhost-isolated``. Held
#: once because three of the four presets open with it, and because getting it WRONG is silent:
#: EPICS defaults its auto-address search to ON, so omitting the two ``*_AUTO_ADDR_LIST`` lines
#: broadcasts PV searches into the local subnets while looking like a narrower config than it is.
_LOOPBACK_SEARCH = {
    "EPICS_MCP_PROVIDER": "pva",
    "EPICS_PVA_AUTO_ADDR_LIST": "NO",
    "EPICS_CA_AUTO_ADDR_LIST": "NO",
    "EPICS_PVA_ADDR_LIST": "127.0.0.1",
    "EPICS_CA_ADDR_LIST": "127.0.0.1",
}

#: The same posture pointed at a real IOC or gateway instead of loopback. Same five keys on
#: purpose: a preset that reaches a facility must still disable the auto search explicitly.
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
            "Loopback only: a local soft IOC, no REST planes. The workshop and summer-school "
            "shape, and the one preset that needs no editing."
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
            "service for; an unset URL disables that plane with no network call."
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


def render_client_config(env: Mapping[str, str]) -> str:
    """Render *env* as the ``.mcp.json`` block an MCP client expects, without a trailing newline.

    Two spaces of indentation and no key sorting, so the output is diffable against
    ``examples/mcp.json`` and keeps the deliberate variable order of the preset. The caller decides
    the trailing newline, because this string is also embedded in prose.
    """
    document = {"mcpServers": {SERVER_KEY: {"command": SERVER_COMMAND, "env": dict(env)}}}
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
