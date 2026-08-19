# EPICS MCP

[![CI](https://github.com/epicDirk/EPICS-MCP/actions/workflows/ci.yml/badge.svg)](https://github.com/epicDirk/EPICS-MCP/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/epics-mcp.svg)](https://pypi.org/project/epics-mcp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/epicDirk/EPICS-MCP/blob/main/LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/)

Ask an AI assistant about your EPICS control system. What is this PV reading right now? Why is
it disconnected? Which operator screens show this device, and is it archived and
alarm-configured? The server exposes your EPICS layer as **Model Context Protocol (MCP)** tools
over [p4p](https://mdavidsaver.github.io/p4p/), so an assistant can answer those the way an
operator would, instead of you clicking through CS-Studio, the Archiver Appliance UI and
ChannelFinder by hand.

**Read-only by default.** `set_pv_value` is triple-gated: an env switch, a required regex
allowlist, and a rate limit. A value outside the record's own drive limits is refused after those
three have admitted the write, which is a further refusal and not a fourth gate. Logbook writes sit
behind their own separate gate, of six checks. The fullest statement of the posture, including what
leaves your machine, is
[Safety and network posture](https://github.com/epicDirk/EPICS-MCP/blob/main/docs/safety.md).

> **Project status.** Pre-1.0 and under active development. Tools and APIs may still change
> between minor versions, so pin one if you depend on it.

## Where to start

**I want to try it, and I have no control system.** Install it (below), then follow the
[quick start](https://github.com/epicDirk/EPICS-MCP/blob/main/docs/quick-start.md): three commands, and `epics-testpv` serves the PV
for you, so there is nothing else to obtain. No facility, no IOC, no EPICS Base, no ChannelFinder,
no archiver.

**I want to point it at my facility.** The [deployment guide](https://github.com/epicDirk/EPICS-MCP/blob/main/docs/deployment.md) is written for
that. It starts at `epics-init`, which prints a client-configuration block for one of four
deployment shapes and checks it in the same step, and it then walks the variables plane by plane,
the CA-bundle recipe for internal HTTPS, and the documented assumptions. Either way you end at
`epics-doctor`, which probes every configured plane read-only and tells you what your instance
actually reaches, and whether either write gate is armed and where it could write.

## What is this (for EPICS people)?

MCP is a small, standard protocol that lets an AI assistant (Claude, or any MCP client) call
*tools* you expose to it. This server is such a tool provider for EPICS.

Its READ reach is decided by the **launcher's EPICS search-path environment**, not by this
server: the address lists, `EPICS_PVA_NAME_SERVERS` (TCP unicast, not subnet-bound), and the
auto-address search, which EPICS defaults to **ON**. Even an empty environment broadcasts PV
searches into the local subnets. The optional REST services stay off until their URLs are set.
PV **write** is the one reach the launcher does not decide: enabling it forces a loopback-only
search reach, and the process refuses to start otherwise.
Do not assume isolation from this document. Run `epics-doctor` to see what an instance actually
reaches, and read [Safety and network posture](https://github.com/epicDirk/EPICS-MCP/blob/main/docs/safety.md).
An assistant does not have to ask: **every read answer carries a `reach` field** naming the plane
that served it and its scope, so no value can be quoted without both. The reach comes from the
configuration, not from a probe, and the field says so itself.

## The planes it sees

The server joins several *planes* of an EPICS installation, and everything it reports is framed
in these terms. Full descriptions are in [the tool reference](https://github.com/epicDirk/EPICS-MCP/blob/main/docs/tools.md).

| Plane | Source | Tools |
|-------|--------|-------|
| **Live** | p4p (PVAccess; p4p 4.x offers no Channel Access) | `get_pv_value`, `get_pvs`, `get_pv_info`, `monitor_pv`, `discover_pvs`, `set_pv_value` |
| **Registry** | ChannelFinder | `find_channels`, `list_channel_vocabulary`, `diagnose_connection`, `coverage_audit` |
| **History** | EPICS Archiver Appliance | `is_archived`, `get_pv_history`, `get_archive_info`, `get_appliance_info`, `list_archived_pvs` |
| **Alarm** | Phoebus Alarm Logger | `is_alarm_configured`, `get_alarm_history` |
| **Naming** | ESS Naming Service | `lookup_device_name`, `diagnose_connection`, `crossplane_check` |
| **Logbook** | Phoebus Olog | `search_logbook`, `get_log_entry`, `list_logbooks`, `list_tags`, `list_log_levels`, `create_log_entry`, `reply_to_log`, `update_log_entry`, `add_log_attachment`, `list_log_attachments`, `download_log_attachment` |
| **Display** | `.bob` operator screens and `.plt` Data Browser trends (CS-Studio / Phoebus) | `validate_pvs`, `crossplane_check`, `coverage_audit`, `find_device` |
| **IOC** | e3 `st.cmd` (+ optional `.db`) | `crossplane_check` |

The Live plane is the **only** authority for connected or disconnected. Every other plane is
explanatory, and is *withheld* rather than reported as a false negative when its service is not
configured.

## Requirements

- Python 3.12+
- [p4p](https://mdavidsaver.github.io/p4p/) ≥ 4.2, installed automatically. It bundles the
  EPICS libraries, so no separate EPICS Base build is needed for the client.
- A reachable EPICS PV. Your control system once you widen the address list, an IOC of your own, or
  the synthetic one `epics-testpv` serves, which needs nothing beyond this package.

## Installation

From [PyPI](https://pypi.org/project/epics-mcp/), with
[uv](https://docs.astral.sh/uv/) (recommended):

```bash
uv tool install epics-mcp
```

or with pip:

```bash
pip install epics-mcp
```

For the development version, install straight from the repository
(`uv tool install git+https://github.com/epicDirk/EPICS-MCP`); from a local checkout,
`uv tool install .` and `pip install .` do the same thing.

This installs the **core** server: live PV access, diagnosis, and the REST-service planes, plus seven
commands (`epics-mcp`, `epics-init`, `epics-testpv`, `epics-doctor`, `epics-diagnose`,
`epics-crossplane`, `epics-coverage`).

The last two are **display-aware** and need the `opi_navigation` engine, which is not part of the
package: they refuse with an explanation rather than running. Everything else works without it.
See [Related and roadmap](#related-and-roadmap).

**Check the commands are on your PATH before anything else**, because the failure otherwise arrives
much later and somewhere unhelpful:

```bash
epics-doctor --version    # or: which epics-doctor   /   where.exe epics-doctor
```

Every command answers `--help` and `--version` with its own name and the installed version, the one
a bug report asks for, on a core-only install as well. If nothing is found, add the tool directory
your installer used to your PATH (`uv tool update-shell` does that for uv). A current Linux
distribution refuses a system-wide `pip install`, so use a virtual environment or
`uv tool install`. If the install ends in compiler output, `p4p` had no prebuilt wheel for your
platform and fell back to building from source, see [Compatibility](#compatibility).

## Documentation

| Page | What it answers |
|------|-----------------|
| [Quick start](https://github.com/epicDirk/EPICS-MCP/blob/main/docs/quick-start.md) | Try it in three commands, with no control system: the test PV, the configuration block, and the client restart |
| [Tools, CLIs, resources and prompts](https://github.com/epicDirk/EPICS-MCP/blob/main/docs/tools.md) | What can it actually do? Every tool by plane, the standalone CLIs, the resources and prompts |
| [Configuration](https://github.com/epicDirk/EPICS-MCP/blob/main/docs/configuration.md) | Every `EPICS_MCP_*` variable, including TLS trust and the EPICS network block |
| [Safety and network posture](https://github.com/epicDirk/EPICS-MCP/blob/main/docs/safety.md) | What is gated, what is audited, what decides network reach |
| [MCP client integration](https://github.com/epicDirk/EPICS-MCP/blob/main/docs/mcp-clients.md) | Ready-to-paste `.mcp.json` blocks, where they go, and the restart that finishes the job |
| [Deployment guide](https://github.com/epicDirk/EPICS-MCP/blob/main/docs/deployment.md) | Bringing it up in **your** facility, plane by plane, with `epics-doctor` |
| [Troubleshooting](https://github.com/epicDirk/EPICS-MCP/blob/main/docs/deployment.md#6-troubleshooting) | It did not work: symptom first, from "the client says nothing" to a rejected config file, and the running-server questions after it |
| [Removing it again](https://github.com/epicDirk/EPICS-MCP/blob/main/docs/deployment.md#7-removing-it-again-and-going-back-a-version) | Uninstall and downgrade, and the four things an uninstall leaves behind |
| [Write-gate contract](https://github.com/epicDirk/EPICS-MCP/blob/main/docs/write-gate-contract.md) | What every in-server write gate must satisfy, and what a gate is deliberately not |
| [Operating guide](https://github.com/epicDirk/EPICS-MCP/blob/main/OPERATING.md) | The operational cookbook: service landscape, recipes, error signatures. An assistant reaches the same text through the `get_guide` tool, one named section at a time, and an application through the `epics-pv://guide` resource |
| [Architecture](https://github.com/epicDirk/EPICS-MCP/blob/main/ARCHITECTURE.md) | The `server → tools → services → clients` layering and the plane model |
| [Security policy](https://github.com/epicDirk/EPICS-MCP/blob/main/SECURITY.md) | Reporting a vulnerability, and an honest statement of what the write gates are **not** |
| [Contributing](https://github.com/epicDirk/EPICS-MCP/blob/main/CONTRIBUTING.md) | Dev setup, the gate chain, Definition of Done |
| [Known limits](https://github.com/epicDirk/EPICS-MCP/blob/main/docs/known-limits.md) | What is deliberately **not** guarded, dated and measured, including the tempting repairs that were probed and rejected |
| [Changelog](https://github.com/epicDirk/EPICS-MCP/blob/main/CHANGELOG.md) | Release history |

## Built at a facility, designed to be facility-agnostic

Written and validated against a real installation at ESS: an e3 IOC over PVAccess, an Archiver
Appliance, ChannelFinder, the Phoebus Alarm server and logger, and an Olog logbook. The quirks it
compensates for were measured against those services, not read off a specification.

No site is hard-coded. Every service URL and network setting is an `EPICS_MCP_*` environment
variable, so deploying elsewhere means setting those variables, not changing code. Two defaults
carry a site-specific value (the ChannelFinder privacy allowlists), both documented as overridable
in the [deployment guide](https://github.com/epicDirk/EPICS-MCP/blob/main/docs/deployment.md).

## Compatibility

**Platforms.** Installation is only as portable as `p4p`, which ships the EPICS libraries as
prebuilt wheels. Where a wheel exists, `pip install` just works; where it does not, pip falls back
to building from source, which needs a compiler and is not something this project tests.

| Platform | `p4p` wheel for Python 3.12+ | Install verified |
|----------|------------------------------|------------------|
| Linux x86_64 | yes | **yes**, in a clean container |
| Windows x86_64 | yes | **yes**, in a clean venv |
| macOS Apple Silicon | yes | not tested here |
| macOS Intel (x86_64) | yes, via the `universal2` wheel (needs macOS 11+) | not tested here |
| Linux aarch64 | **no**, source build | not tested here |

**Services.** Exercised against a local EPICS stack: an e3 test IOC (PVAccess), an EPICS Archiver
Appliance (single- or multi-instance), ChannelFinder, and the Phoebus Alarm server and logger.
Archiver topology note: in a single-JVM appliance the MGMT and retrieval webapps share a port, so
leave `EPICS_MCP_ARCHIVER_RETRIEVAL_URL` empty; in a split deployment MGMT (`:17665`) and retrieval
(`:17668`) are separate ports.

No version of those services is listed here, and that is deliberate. The deployment guide
explains [why, and what to run to answer the question for your own
installation](https://github.com/epicDirk/EPICS-MCP/blob/main/docs/deployment.md#will-it-work-against-my-version-of-these-services).

## Development

The gate chain is [uv](https://docs.astral.sh/uv/)-based: `uv sync --extra dev --locked`, then
`uv run pytest` and `uv run pre-commit run --all-files`. A full local install additionally needs
`--group displays` for the `opi_navigation` PV engine, and a fresh clone needs one further command
to wire the commit-message guard, which lives outside the tree and which nothing can install for
you.

Every one of those, what CI runs and what it deliberately cannot, the Definition of done and the
commit style: [CONTRIBUTING.md](https://github.com/epicDirk/EPICS-MCP/blob/main/CONTRIBUTING.md).

## Related and roadmap

Three things: what exists, what is deliberately missing, and what is only intended.

**What exists.** Four tools (`validate_pvs`, `crossplane_check`, `coverage_audit`, `find_device`)
and two commands (`epics-crossplane`, `epics-coverage`) join live PVs with the *display* plane:
the macro-expanded PV inventory of `.bob` operator screens and of the `.plt` Data Browser trends
reached from them, read through the `opi_navigation` PV engine. Everything else in this package,
live PVs, registry, history, alarm, naming and logbook, installs and runs without any of it.

**What is deliberately missing.** Those six are not available in a published install, and cannot
be made available by any flag. `opi_navigation` lives in a private repository, so it is wired
into a dependency group rather than advertised as an extra. A group stays out of the published
metadata, where an unreachable dependency would be a promise nobody could keep, and a direct git
reference is the one thing a package index refuses outright. The tools refuse with an explanation
instead of failing obscurely. This page names the gap rather than offering an install command
that cannot work.

**What is intended, with no date attached.** Two neighbouring pieces exist and are in daily use
here, both in private repositories: `opi-live`, a plugin that exposes a RUNNING CS-Studio, and
`CS-Studio-MCP`, an offline MCP server for the display files themselves. Opening the engine up is
tied to how those two hold up in practice. None of the three carries a release commitment, and
nothing on this page is a schedule. This page will say when one is published.

## License

MIT, see [LICENSE](https://github.com/epicDirk/EPICS-MCP/blob/main/LICENSE).

## Credits

Independently developed EPICS MCP server, originally seeded from
[Jacky1-Jiang/EPICS-MCP-Server](https://github.com/Jacky1-Jiang/EPICS-MCP-Server) (MIT) and since
rewritten on [FastMCP](https://github.com/jlowin/fastmcp) and the p4p library, with a
write-safety layer, batch operations, PV monitoring, cross-plane provenance, and OPI validation
by epicDirk.
