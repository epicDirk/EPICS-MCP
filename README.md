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

> **Project status: work in progress (pre-1.0).** Under active development;
> the tool surface and APIs may still change. Semantic-versioning pre-1.0 caveats apply, so
> pin a version if you depend on it. The released version is the one the PyPI badge above shows;
> it is deliberately not restated here, because a version spelled twice is a version that drifts.

**Maturity.** The full test suite passes on a standalone install with no EPICS infrastructure at
all, and that is what CI runs on every push, against Python 3.12 and 3.13; the live-stack tests are
opt-in and skip without a stack. `mypy --strict` covers `src`, `tests` and `scripts`, and the
package ships `py.typed`. The CI badge above reports the current state of both gates.

## Where to start

**I want to try it, and I have no control system.** Follow [Quick start](#quick-start) below. A
`softIocPVA` and the sample database in [`examples/`](https://github.com/epicDirk/EPICS-MCP/tree/main/examples/) give you a working PV in about
five minutes; no facility, no ChannelFinder, no archiver.

**I want to point it at my facility.** Start with `epics-init --list`, pick the shape that matches
what you run, and let `epics-init --preset <shape>` write the client-configuration block and check
it for you. When your facility does not match one of the four shapes, the
[deployment guide](https://github.com/epicDirk/EPICS-MCP/blob/main/docs/deployment.md) walks through the variables plane by plane, the CA-bundle
recipe for internal HTTPS, and the documented assumptions. Either way you end at `epics-doctor`,
which probes every configured plane read-only and tells you what your instance actually reaches.

## What is this (for EPICS people)?

MCP is a small, standard protocol that lets an AI assistant (Claude, or any MCP client) call
*tools* you expose to it. This server is such a tool provider for EPICS.

Its network reach is decided by the **launcher's EPICS search-path environment**, not by this
server: the address lists, `EPICS_PVA_NAME_SERVERS` (TCP unicast, not subnet-bound), and the
auto-address search, which EPICS defaults to **ON**. Even an empty environment broadcasts PV
searches into the local subnets. The optional REST services stay off until their URLs are set.
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
| **Display** | `.bob` operator screens (CS-Studio / Phoebus) | `validate_pvs`, `crossplane_check`, `coverage_audit`, `find_device` |
| **IOC** | e3 `st.cmd` (+ optional `.db`) | `crossplane_check` |

The Live plane is the **only** authority for connected or disconnected. Every other plane is
explanatory, and is *withheld* rather than reported as a false negative when its service is not
configured.

## Requirements

- **Python 3.12+**
- **[p4p](https://mdavidsaver.github.io/p4p/)** ≥ 4.2, installed automatically. It bundles the
  EPICS libraries, so no separate EPICS Base build is needed for the client.
- A reachable EPICS PV: an IOC, a `softIocPVA`, or your control system once you widen the
  address list.

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

This installs the **core** server: live PV access, diagnosis, and the REST-service planes, plus six
commands (`epics-mcp`, `epics-init`, `epics-doctor`, `epics-diagnose`, `epics-crossplane`,
`epics-coverage`).

The last two are **display-aware** and need the `opi_navigation` engine, which is not part of the
package: they refuse with an explanation rather than running. Everything else works without it.
See [Related and roadmap](#related-and-roadmap).

## Quick start

1. **Start a test PV.** No real IOC needed. Create `test.db`:

   ```
   record(ai, "TEST:Temperature") { field(VAL, "21.5") field(EGU, "C") }
   ```

   and serve it over PVAccess:

   ```bash
   softIocPVA -d test.db
   ```

   A ready-to-run version lives in [`examples/`](https://github.com/epicDirk/EPICS-MCP/tree/main/examples/). `softIocPVA` ships with EPICS Base
   and is **not** installed by this package.

2. **Run the server** over stdio:

   ```bash
   epics-mcp
   ```

   `epics-mcp --help` explains the invocation, and every console command answers
   `--help` and `--version` with its own name and the installed version (the one a bug report
   asks for), on a core-only install as well. `epics-crossplane` and `epics-coverage` still
   need the display engine to RUN, and say so when asked to. The server itself is configured
   through `EPICS_MCP_*` environment variables, not through options.

3. **Wire it into an MCP client**, see [MCP client integration](https://github.com/epicDirk/EPICS-MCP/blob/main/docs/mcp-clients.md). Then ask
   the assistant to read `TEST:Temperature`, or skip the assistant entirely:

   ```bash
   epics-diagnose TEST:Temperature
   ```

To reach a real control system, set the address list in the launcher's environment, for example
`EPICS_PVA_ADDR_LIST=<gateway-or-ioc-host>`, and then follow the
[deployment guide](https://github.com/epicDirk/EPICS-MCP/blob/main/docs/deployment.md).

## Documentation

| Page | What it answers |
|------|-----------------|
| [Tools, CLIs, resources and prompts](https://github.com/epicDirk/EPICS-MCP/blob/main/docs/tools.md) | What can it actually do? Every tool by plane, the standalone CLIs, the resources and prompts |
| [Configuration](https://github.com/epicDirk/EPICS-MCP/blob/main/docs/configuration.md) | Every `EPICS_MCP_*` variable, including TLS trust and the EPICS network block |
| [Safety and network posture](https://github.com/epicDirk/EPICS-MCP/blob/main/docs/safety.md) | What is gated, what is audited, what decides network reach |
| [MCP client integration](https://github.com/epicDirk/EPICS-MCP/blob/main/docs/mcp-clients.md) | Ready-to-paste `.mcp.json` blocks |
| [Deployment guide](https://github.com/epicDirk/EPICS-MCP/blob/main/docs/deployment.md) | Bringing it up in **your** facility, plane by plane, with `epics-doctor` |
| [Operating guide](https://github.com/epicDirk/EPICS-MCP/blob/main/OPERATING.md) | The operational cookbook: service landscape, recipes, error signatures. Also served to an assistant as `epics-pv://guide` |
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

## Development

The gate chain is [uv](https://docs.astral.sh/uv/)-based:

```bash
uv sync --extra dev --group displays --frozen  # full local install (toolchain + display engine)
uv run pytest                                  # test suite
uv run pytest --cov=src --cov-branch           # with coverage
uv run pre-commit run --all-files              # ruff + format + mypy --strict + guards
```

`dev` is the only extra, and it is the toolchain. The `opi_navigation` PV engine is a separate
**dependency group** (`--group displays`), because it lives in a private repository: a group
stays out of the published package, where an unreachable dependency would be a promise nobody
can keep. CI passes no `--group`, so it tests the standalone core a public user gets, and the
`opi_navigation`-coupled test modules self-skip when it is absent.

Live tests that need a running EPICS stack are opt-in and skip by default. See
[CONTRIBUTING.md](https://github.com/epicDirk/EPICS-MCP/blob/main/CONTRIBUTING.md).

## Related and roadmap

The display-aware tools join live PVs with the *display* plane, the macro-expanded PV inventory
of `.bob` operator screens, through the `opi_navigation` PV engine. The core PV server installs
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
