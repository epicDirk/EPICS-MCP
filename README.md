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
allowlist, and a rate limit. Logbook writes sit behind their own separate gate.

> **Project status: pre-1.0 and under active development.** Tools and APIs may still change
> between minor versions, so pin one if you depend on it.

**Maturity.** CI runs the standalone core suite on every push against Python 3.12, 3.13 and 3.14,
which is every version `requires-python` permits, on an install with no EPICS infrastructure at
all; the live-stack tests are opt-in and skip without a stack, and the seven display-coupled test
modules are not collected there at all, because the engine they need is not installable from a
public checkout. That last part is a deliberate choice rather than a gap in the report: CI tests
exactly the standalone core a public user gets, and a run that drops those modules says so in its
own header. `mypy --strict` covers `src`, `tests` and `scripts`, and the package ships `py.typed`.

## Where to start

**I want to try it, and I have no control system.** Install it (below), then follow
[Quick start](#quick-start). `epics-testpv` serves the PV for you, so there is nothing else to
obtain: no facility, no IOC, no EPICS Base, no ChannelFinder, no archiver.

**I want to point it at my facility.** Start with `epics-init --list`, pick the shape that matches
what you run, and pass its open values with `--set NAME=VALUE`; then
`epics-init --preset <shape> --set ... --out .mcp.json` writes the client-configuration block and
checks it for you (`--out` rather than a shell redirect, which cannot promise an encoding the
client can read). Every facility shape ships placeholders, and while one is still standing the
block is only PRINTED: no file is written and no check runs, and the command says which value it is
waiting for. Only `sandbox` has nothing left to fill in, which is why the quick start above needs
no `--set` at all. When your facility does not match one of the four shapes, the
[deployment guide](https://github.com/epicDirk/EPICS-MCP/blob/main/docs/deployment.md) walks through the variables plane by plane, the CA-bundle
recipe for internal HTTPS, and the documented assumptions. Either way you end at `epics-doctor`,
which probes every configured plane read-only and tells you what your instance actually reaches,
and whether either write gate is armed and where it could write.

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

## The planes it sees

The server joins several *planes* of an EPICS installation, and everything it reports is framed
in these terms. Full descriptions are in [the tool reference](https://github.com/epicDirk/EPICS-MCP/blob/main/docs/tools.md).

| Plane | Source | Tools |
|-------|--------|-------|
| **Live** | p4p (PVAccess / Channel Access) | `get_pv_value`, `get_pvs`, `get_pv_info`, `monitor_pv`, `discover_pvs`, `set_pv_value` |
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

- **Python 3.12+**
- **[p4p](https://mdavidsaver.github.io/p4p/)** ≥ 4.2, installed automatically. It bundles the
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
your installer used to your PATH (`uv tool update-shell` does that for uv). Two related notes: a
current Linux distribution refuses a system-wide `pip install`, so use a virtual environment or
`uv tool install`; and if the install ends in compiler output instead, `p4p` had no prebuilt wheel
for your platform and fell back to building from source, see [Compatibility](#compatibility).

## Quick start

Three commands, and nothing to obtain beyond the install above.

1. **Serve a test PV**, in a terminal of its own. It runs until Ctrl-C and binds loopback only:

   ```bash
   epics-testpv
   ```

2. **Write the client configuration, and check it in the same step.** `--out` writes the file
   itself, which a shell redirect cannot do reliably: in Windows PowerShell 5.1 it produces bytes
   a strict JSON parser rejects.

   ```bash
   epics-init --preset sandbox --out .mcp.json --probe-pv TEST:Temperature
   ```

   The block goes to stdout, the check to stderr, and a `live ok` line means the server reached the
   PV from step 1. Without `--probe-pv` no PV is contacted, so a clean report would say only that
   nothing is misconfigured.

   ⚠️ **Already have a `.mcp.json` with other servers in it?** Then this refuses rather than
   overwrite it, which is the point. Write to a new file (`--out epics-pv.json`) and paste the one
   entry into your existing `mcpServers` object, or pass `--force` if the file is yours to replace.

3. **Point your MCP client at that file and restart it**, see
   [MCP client integration](https://github.com/epicDirk/EPICS-MCP/blob/main/docs/mcp-clients.md): a client reads its configuration at startup, so
   until it is restarted the tools are simply absent. Then ask the assistant to read
   `TEST:Temperature`. Or skip the assistant entirely:

   ```bash
   epics-diagnose TEST:Temperature
   ```

   which prints a short report: the PV, its state and the likely cause, then one line for the live
   result (`connected, value=21.5`, plus the alarm severity where the PV reports one) and one for
   each service plane that was consulted, and last the next steps, any notes, and any plane that
   was asked for but is unavailable.

   ⚠️ **That command reads YOUR shell, not the file step 2 just wrote.** The block in `.mcp.json`
   configures the server your MCP CLIENT launches; a command you run yourself sees the environment
   of your terminal, and a fresh terminal points at no PV at all. Measured: run exactly as shown in
   a shell with no `EPICS_*` variables, it answers `disconnected (PV_TIMEOUT)` while the test PV is
   serving perfectly well. Two ways round it, neither of which edits anything. Set in that terminal
   the search path the `sandbox` preset sets, which for the test PV is
   `EPICS_PVA_AUTO_ADDR_LIST=NO` plus `EPICS_PVA_ADDR_LIST=127.0.0.1`; or let step 2's own check
   answer, since it applies the preset itself. Without `--out` it writes no file, printing the
   block on stdout and the check on stderr:

   ```bash
   epics-init --preset sandbox --probe-pv TEST:Temperature
   ```

   ⚠️ **If the client reports only that the server did not start**, the likeliest cause is that
   `"command": "epics-mcp"` is a bare name and a client launched from a desktop icon does not
   inherit your shell's `PATH`. Rerun step 2 with `--absolute-command`, which writes the resolved
   path into the block instead and refuses rather than guessing when it cannot find one. Since
   step 2 already wrote that file, add `--force` to replace it, or write a new one and copy the
   entry across.

⚠️ Note what step 1 is: a PVAccess server, and its second PV accepts writes. It binds loopback
unless you pass `--interface`, and it says which port it got, which is not the default one when that
is already taken.

To reach a real control system instead, pick the matching preset and follow the
[deployment guide](https://github.com/epicDirk/EPICS-MCP/blob/main/docs/deployment.md), which walks the variables plane by plane and ends at the
same self-check. What every facility shape needs is the PV search path, and a preset leaves one
placeholder per protocol for it, so a `--set` looks like this in practice:

```bash
epics-init --preset ioc-only --set EPICS_PVA_ADDR_LIST=<host> --set EPICS_CA_ADDR_LIST=<host> --out <my config>
```

Fill in only one of the two and the command writes nothing rather than a half-configured file: it
names the placeholder still standing, says the file was not written, and leaves the exit code at
`0`, because an unfinished configuration is a step you have not taken yet rather than an error.

A containerised IOC usually needs `EPICS_PVA_NAME_SERVERS=<ioc-host>:5075` **alongside** that
rather than instead of it, since it publishes a TCP port and answers no broadcast. The deployment
guide explains why, and what to do when neither finds anything.

## Documentation

| Page | What it answers |
|------|-----------------|
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

**Services.** Exercised against a local EPICS stack: an **e3** test IOC (PVAccess), an **EPICS
Archiver Appliance** (single- or multi-instance), **ChannelFinder**, and the **Phoebus Alarm**
server and logger. Archiver topology note: in a single-JVM appliance the MGMT and retrieval webapps
share a port, so leave `EPICS_MCP_ARCHIVER_RETRIEVAL_URL` empty; in a split deployment MGMT
(`:17665`) and retrieval (`:17668`) are separate ports.

No version of any of those is listed here, and that is deliberate rather than an omission: the
deployment guide explains [why, and what to run to answer the question for your own
installation](https://github.com/epicDirk/EPICS-MCP/blob/main/docs/deployment.md#will-it-work-against-my-version-of-these-services).

## Development

The gate chain is [uv](https://docs.astral.sh/uv/)-based:

```bash
uv sync --extra dev --group displays --locked  # full local install (toolchain + display engine)
uv run pytest                                  # test suite
uv run pytest --cov=src --cov-branch           # with coverage
uv run pre-commit run --all-files              # ruff + format + mypy --strict + guards
```

`dev` is the only extra, and it is the toolchain. The `opi_navigation` PV engine is a separate
**dependency group** (`--group displays`), because it lives in a private repository: a group
stays out of the published package, where an unreachable dependency would be a promise nobody
can keep. CI passes no `--group`, so it tests the standalone core a public user gets, and the
`opi_navigation`-coupled test modules are dropped when it is absent. A run that drops them says so
in its report header, and `EPICS_MCP_REQUIRE_DISPLAYS=1` turns that skip into a refusal, so a
half-installed checkout cannot report green over tests it never ran.

Live tests that need a running EPICS stack are opt-in and skip by default. See
[CONTRIBUTING.md](https://github.com/epicDirk/EPICS-MCP/blob/main/CONTRIBUTING.md).

## Related and roadmap

The display-aware tools join live PVs with the *display* plane, the macro-expanded PV inventory
of `.bob` operator screens and the `.plt` Data Browser trends reached from them, through the
`opi_navigation` PV engine. The core PV server installs
and runs fully **without** them.

⚠️ **Those four tools are not available in a published install today.** `opi_navigation` lives
in a private repository, so it is reachable only from a checkout that has access, and it is
wired in as a local dependency group rather than advertised as an extra. Opening it up is
planned once the Java live plugin and the CS-Studio MCP have been tested in practice; until
then this says so plainly rather than offering an install command that cannot work.

A dedicated **CS-Studio / Phoebus MCP** that complements these tools is in the works and will be
released separately.

## License

MIT, see [LICENSE](https://github.com/epicDirk/EPICS-MCP/blob/main/LICENSE).

## Credits

Independently developed EPICS MCP server, originally seeded from
[Jacky1-Jiang/EPICS-MCP-Server](https://github.com/Jacky1-Jiang/EPICS-MCP-Server) (MIT) and since
rewritten on [FastMCP](https://github.com/jlowin/fastmcp) and the p4p library, with a
write-safety layer, batch operations, PV monitoring, cross-plane provenance, and OPI validation
by epicDirk.

The Naming-Service client was written for this repository and follows the API shape of the client
in pvValidator (GPL-3.0-only); it carries none of that code, and pvValidator is not a dependency.
The measurement behind that statement is recorded in
[known limits](https://github.com/epicDirk/EPICS-MCP/blob/main/docs/known-limits.md), entry 11.
