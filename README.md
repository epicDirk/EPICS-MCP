# EPICS PV MCP Server

[![CI](https://github.com/epicDirk/EPICS-MCP-Server/actions/workflows/ci.yml/badge.svg)](https://github.com/epicDirk/EPICS-MCP-Server/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/)

A read-only **Model Context Protocol (MCP)** server that lets an AI assistant read and
diagnose your EPICS control system — live PV values, connection diagnosis, and
cross-plane provenance (ChannelFinder · Archiver · Alarm) — over
[p4p](https://mdavidsaver.github.io/p4p/) (PVAccess **and** Channel Access).

> **Project status: work in progress (pre-1.0, `0.2.0`).** Under active development;
> the tool surface and APIs may still change. Semantic-versioning pre-1.0 caveats apply
> — pin a version if you depend on it.

## What is this (for EPICS people)?

MCP is a small, standard protocol that lets an AI assistant (Claude, or any MCP client)
call *tools* you expose to it. This server exposes your EPICS layer as such tools, so an
assistant can answer questions the way an operator would — *"what's the value of this
PV?"*, *"why is it disconnected?"*, *"which screens show this device?"*, *"is it
archived and alarm-configured?"* — instead of you clicking through CS-Studio, the
Archiver Appliance UI and ChannelFinder by hand.

It is **read-only by default** and **localhost-isolated by default**: out of the box it
reads live PVs on your machine and does **not** reach your production IOCs or any ESS
service until you explicitly widen the EPICS address list and set the optional service
URLs (see [Safety & network posture](#safety--network-posture)).

## The planes it sees

The server joins several *planes* of an EPICS installation. Everything it reports is
framed in these terms:

| Plane | Source | Tools |
|-------|--------|-------|
| **Live** | p4p (PVAccess / Channel Access) | `get_pv_value`, `get_pvs`, `get_pv_info`, `monitor_pv`, `discover_pvs`, `set_pv_value` |
| **Registry** | ChannelFinder | `find_channels`, `diagnose_connection`, `coverage_audit` |
| **History** | EPICS Archiver Appliance | `is_archived`, `get_pv_history`, `get_archive_info` |
| **Alarm** | Phoebus Alarm Logger | `is_alarm_configured`, `get_alarm_history` |
| **Naming** | ESS Naming Service | `lookup_device_name`, `diagnose_connection`, `crossplane_check` |
| **Logbook** | Phoebus Olog | `search_logbook`, `get_log_entry`, `list_logbooks`, `list_tags`, `create_log_entry`, `reply_to_log` |
| **Display** | `.bob` operator screens (CS-Studio / Phoebus) | `validate_pvs`, `crossplane_check`, `coverage_audit`, `find_device` |
| **IOC** | e3 `st.cmd` (+ optional `.db`) | `crossplane_check` |

The Live plane is the **only** authority for connected/disconnected; every other plane is
explanatory and is *withheld* (never a false negative) when its service is not configured.

## Safety & network posture

This is a controls tool, so the trust questions come first:

- **Read-only by default.** The mutating tools are gated off. `set_pv_value` is **triple-gated**:
  `EPICS_MCP_ALLOW_PV_WRITE=true` **and** an optional regex allowlist
  (`EPICS_MCP_PV_WRITE_PATTERN`) **and** a per-minute rate limit — every write *attempt*
  is audit-logged (`ALLOW`/`DENY`/`FAILED`).
- **Olog logbook write is a *separate* gate.** `create_log_entry` / `reply_to_log` need
  `EPICS_MCP_ALLOW_OLOG_WRITE=true` **and** a **test-server URL boundary** (only a loopback Olog,
  or an exact URL in `EPICS_MCP_OLOG_WRITE_URL_ALLOWLIST` with
  `EPICS_MCP_OLOG_WRITE_ALLOW_REMOTE=true` — a non-loopback/private host is refused by default, so
  a production write is a deliberate double action) **and** a logbook allowlist
  (`EPICS_MCP_OLOG_WRITE_LOGBOOKS`; empty = deny-all) **and** a rate limit. The author is the write
  service account (`EPICS_MCP_OLOG_WRITE_USER`), set server-side and not spoofable; the audit line
  is metadata-only (never the title/description free text). **`ALLOW_PV_WRITE` is untouched by it.**
- **Olog output posture — redacted unless declared test data** *(ESS-spec pending)*. Every entry
  leaves through a strict allowlist — author dropped, `title`/`description` free text withheld,
  attachments as a count — **unless BOTH** `EPICS_MCP_OLOG_URL` is loopback **and**
  `EPICS_MCP_OLOG_ASSUME_TEST_DATA=true`. Then entries come back **whole**, because withholding
  the free text costs a logbook its point (a search returns ids whose content you cannot judge,
  and a write cannot verify what it wrote). **Two conditions, because neither proves the case
  alone:** a loopback *address* does not prove the *data* is synthetic — a port-forward serves
  production on localhost with the URL unchanged — and a flag alone would not catch "pointed it at
  the facility and forgot". For the same reason the Olog client refuses redirects instead of
  following them. **Why "pending":** the withholding rule was written against an *assumed* privacy
  policy, never a specified one; it is deferred for declared test data until a real specification
  exists, then re-applied — the allowlist and projection stay intact meanwhile. `epics-doctor`
  reports the effective posture. Note this is a *runtime output* policy, unrelated to keeping
  person data out of committed files (see `CLAUDE.md`).
- **Network reach is the launcher's decision, not this server's.** Out of the box it opens no
  non-local connection: the EPICS address lists (`EPICS_PVA_ADDR_LIST` / `EPICS_CA_ADDR_LIST` and
  the matching `*_AUTO_ADDR_LIST`) and the `*_URL` vars are empty, so nothing is reached. A
  deployment may well point them at a real facility — so do **not** assume isolation from this
  document; run `epics-doctor` to see what your instance actually reaches. The write gates above
  hold either way.
- **Optional service planes are off until configured.** ChannelFinder, Archiver, Alarm and
  Naming stay disabled until their `*_URL` env vars are set; an empty/unset URL means *no
  client and no network call*. The ESS Naming plane has **no built-in host** — no egress
  unless you set `EPICS_MCP_NAMING_URL`.
- **Opt-in path boundary.** File/dir tool arguments are canonicalized and existence-checked;
  set `EPICS_MCP_ALLOWED_ROOTS` to confine them to specific roots (empty = no boundary).

## Requirements

- **Python ≥ 3.12**
- **[p4p](https://mdavidsaver.github.io/p4p/)** ≥ 4.2 (installed automatically; bundles the
  EPICS libraries — no separate EPICS base build needed for the client)
- A reachable EPICS PV to talk to (an IOC, a `softIocPVA`, or your control system once you
  widen the address list)

## Installation

Using [uv](https://docs.astral.sh/uv/) (recommended):

```bash
uv tool install epics-pv-mcp        # from a checkout: uv tool install .
```

or with pip:

```bash
pip install .
```

This installs the **core** server (live PV access, diagnosis, and the REST-service planes).
The display-aware tools (`validate_pvs`, `crossplane_check`, `coverage_audit`,
`find_device`) need the optional `[displays]` extra — see
[Related / roadmap](#related--roadmap).

## Quick start

1. **Start a test PV** (no real IOC needed). Create `test.db`:

   ```
   record(ai, "TEST:Temperature") { field(VAL, "21.5") field(EGU, "C") }
   ```

   and serve it over PVAccess:

   ```bash
   softIocPVA -d test.db
   ```

   A ready-to-run version lives in [`examples/`](examples/).

2. **Run the server** (stdio):

   ```bash
   epics-pv-mcp
   ```

3. **Wire it into an MCP client** — see [MCP client integration](#mcp-client-integration).
   Ask the assistant to read `TEST:Temperature`, or run a standalone CLI:

   ```bash
   epics-diagnose TEST:Temperature
   ```

To reach a real control system, set the address list in the launcher's environment
(e.g. `EPICS_PVA_ADDR_LIST=<gateway-or-ioc-host>`).

## Tools

**Core PV access** (always available)

| Tool | Description |
|------|-------------|
| `get_pv_value` | Read a single PV's current value (+ best-effort metadata) |
| `get_pvs` | Batch-read multiple PVs in one call |
| `get_pv_info` | Connection state, data type, alarm status, display/control limits |
| `monitor_pv` | Subscribe to PV updates for a bounded duration |
| `discover_pvs` | Probe a concrete PV name (wildcards need ChannelFinder) |
| `set_pv_value` | Write a value to a PV — **gated off by default** (see Safety) |

**Connection diagnosis** (always available)

| Tool | Description |
|------|-------------|
| `diagnose_connection` | Why a PV is (dis)connected: live-authoritative state + likely cause (`healthy`/`ioc_down`/`name_typo`/`unregistered`/`indeterminate`) + per-plane evidence + next steps |

**Cross-plane services** (each enabled by its `*_URL`)

| Tool | Service | Enabled by |
|------|---------|-----------|
| `find_channels` | ChannelFinder — which IOC/host serves a PV + tags/properties | `EPICS_MCP_CHANNELFINDER_URL` |
| `is_archived` | Archiver Appliance — is a PV being archived? (+ connection_state / last_event / is_monitored from the same getPVStatus call) | `EPICS_MCP_ARCHIVER_URL` |
| `get_pv_history` | Archiver Appliance — archived samples over an ISO-8601 window (+ the getData.json `meta` block: EGU units, PREC precision; `status` = ok/empty/withheld so an empty result is never mistaken for "could not read") | `EPICS_MCP_ARCHIVER_URL` |
| `get_archive_info` | Archiver Appliance — how a PV is archived (sampling method/period, STS/MTS/LTS retention, DBRType, archived fields, source host); `found:false` on a 404 (never-archived), errors propagate | `EPICS_MCP_ARCHIVER_URL` |
| `list_archived_pvs` | Archiver Appliance — enumerate archived PV names (getAllPVs / `this_appliance`=getPVsForThisAppliance, not getMatchingPVs); optional name-glob `pattern` (getAllPVs only — `getPVsForThisAppliance` has no name filter, so the combination is refused rather than answered unfiltered), honest `capped` | `EPICS_MCP_ARCHIVER_URL` |
| `is_alarm_configured` | Phoebus Alarm Logger — is a PV in the alarm tree? | `EPICS_MCP_ALARM_URL` |
| `get_alarm_history` | Phoebus Alarm Logger — alarm state history of a PV over a window (required start/end; newest-first; user/host stripped) | `EPICS_MCP_ALARM_URL` |
| `lookup_device_name` | ESS Naming Service — is a device name registered + ACTIVE? (404 = definitive not-registered; a service error is withheld, never a false negative) | `EPICS_MCP_NAMING_URL` |
| `search_logbook` | Phoebus Olog — search log entries (DS-PRIVACY: author dropped, title/description free text withheld, attachments as a count — **unless a declared local test sandbox**, where entries come back whole; see "Olog output posture" below); paginate with `offset`/`sort`, `total_matches` = true total across all pages | `EPICS_MCP_OLOG_URL` |
| `get_log_entry` | Phoebus Olog — one entry by id (same redaction; 404 = definitive found:false, other errors propagate) | `EPICS_MCP_OLOG_URL` |
| `list_logbooks` | Phoebus Olog — list the valid logbook names (name-only; owners dropped) | `EPICS_MCP_OLOG_URL` |
| `list_tags` | Phoebus Olog — list the valid tag names | `EPICS_MCP_OLOG_URL` |
| `create_log_entry` | Phoebus Olog — **post** a log entry (MUTATING; own gate + test-server URL boundary + logbook allowlist + rate limit; author = the service account, not spoofable; response redacted) | `EPICS_MCP_OLOG_URL` + `EPICS_MCP_ALLOW_OLOG_WRITE` |
| `reply_to_log` | Phoebus Olog — **reply** to an entry (threads via the Log Entry Group; same gate/redaction as create_log_entry; bad id → 400) | `EPICS_MCP_OLOG_URL` + `EPICS_MCP_ALLOW_OLOG_WRITE` |

**Display-aware** (require the optional `[displays]` extra)

| Tool | Description |
|------|-------------|
| `validate_pvs` | Extract the macro-resolved PVs of a `.bob` display and check their connectivity |
| `crossplane_check` | PV provenance: display PVs ↔ e3 IOC `st.cmd` (+ optional `.db`) ↔ Naming (read-only) |
| `coverage_audit` | Cross-plane coverage matrix: delivered PVs (ChannelFinder) ↔ displays ↔ archive ↔ alarm; blind spots + critical-uncovered |
| `find_device` | Which operator screens show a device, its live channel values (capped), and the serving IOC |

## Command-line tools (no AI needed)

The cross-plane analyses are also standalone CLIs — useful in a terminal or CI, with no
MCP client involved:

```bash
epics-doctor                                            # read-only config self-check (all planes)
epics-diagnose   TEST:Temperature                       # connection diagnosis
epics-crossplane --displays <project-root> <st.cmd>     # display ↔ IOC ↔ Naming (needs [displays])
epics-coverage   --displays <project-root> --scope DEV: # coverage matrix (needs [displays])
```

`epics-doctor` and `epics-diagnose` are part of the core install; `epics-crossplane` and
`epics-coverage` need the `[displays]` extra. All are read-only.

`epics-diagnose`/`epics-crossplane`/`epics-coverage` exit `0` even on a negative finding (a
disconnect / a broken link is a result, not a crash). **`epics-doctor` is the deliberate exception**
— it is a scriptable pass/fail, so it exits `0` when no configured plane failed, `1` when a
configured plane fails (unreachable / CA error / API error / wrong_service / probe-disconnect /
config_error — e.g. a retrieval URL with no archiver URL, which no tool would ever use), and
`2` on a usage error. Run it first in a new facility to confirm your `.env` (see
`docs/deployment.md`).

Each plane is also asked to **name itself**, because reachable is not identified: the transport probe
counts any HTTP response as reachable, so a URL aimed at the wrong host can look alive (measured: a
ChannelFinder URL pointing at a dead container read `✓ ok` because an unrelated service on that port
answered 401). A plane that cannot prove what it is reports `unverified` (`?`) — honest, **not**
healthy, and deliberately not a failure. ⚠️ Exit `0` therefore means "nothing failed", **not**
"everything confirmed": a script must read `verification_complete` / `unverified_planes` from
`--json` rather than the exit code alone — and for **positive** confirmation assert
`identified_planes` is non-empty, because `verification_complete` is vacuously true on an empty
config (nothing probed ≠ everything confirmed).

## Resources & Prompts

| Resource URI | Description |
|--------------|-------------|
| `epics-pv://health` | Server health: version, uptime, provider, p4p version |
| `epics-pv://config` | Non-secret configuration values |
| `epics-pv://guide` | Operational cookbook: service planes, recipes, error signatures |

The guide's source is `src/epics_pv_mcp/operator_guide.md` — one file, shipped in the wheel and served
as the resource (and mirrored for human readers by [`OPERATING.md`](OPERATING.md)).

| Prompt | Description |
|--------|-------------|
| `diagnose_pv` | Step-by-step PV diagnosis workflow (info, read, monitor) |
| `compare_machine_state` | Compare current PV values against an expected state |

## Configuration

All settings are read from environment variables with the `EPICS_MCP_` prefix.
[`.env.example`](.env.example) is the canonical, fully-commented template.

**Core / p4p**

| Variable | Default | Description |
|----------|---------|-------------|
| `EPICS_MCP_PROVIDER` | `pva` | Protocol: `pva` (PVAccess) or `ca` (Channel Access). Lowercase only |
| `EPICS_MCP_DEFAULT_TIMEOUT` | `5.0` | PV operation timeout in seconds (> 0) |
| `EPICS_MCP_MAX_BATCH_SIZE` | `100` | Max PVs per batch read (≥ 1) |
| `EPICS_MCP_MAX_MONITOR_DURATION` | `60.0` | Max monitor subscription duration in seconds (> 0) |
| `EPICS_MCP_MAX_MONITOR_EVENTS` | `1000` | Max events per monitor subscription (≥ 1) |
| `EPICS_MCP_DIAGNOSE_TIMEOUT` | `5.0` | Live-probe timeout for `diagnose_connection` (> 0) |

**Write safety** (all writes blocked unless enabled)

| Variable | Default | Description |
|----------|---------|-------------|
| `EPICS_MCP_ALLOW_PV_WRITE` | `false` | Master switch for PV writes |
| `EPICS_MCP_PV_WRITE_PATTERN` | _(empty)_ | Regex allowlist of writable PV names (e.g. `^TEST:.*`) |
| `EPICS_MCP_WRITE_RATE_LIMIT` | `10` | Max writes per minute (≥ 1) |
| `EPICS_MCP_AUDIT_LOG_FILE` | _(empty)_ | Audit log path (empty = stderr) |

**Path boundary**

| Variable | Default | Description |
|----------|---------|-------------|
| `EPICS_MCP_ALLOWED_ROOTS` | _(empty)_ | `os.pathsep`-separated roots that file/dir args must resolve under. Empty = no boundary. Separator is `;` on Windows / `:` on Linux |

**Optional REST services** (empty/unset URL = disabled, no client, no network call)

| Variable | Default | Enables |
|----------|---------|---------|
| `EPICS_MCP_CHANNELFINDER_URL` | _(empty)_ | `find_channels` + the ChannelFinder plane |
| `EPICS_MCP_CHANNELFINDER_AUTH` | _(empty)_ | Optional `Authorization` header for a secured ChannelFinder |
| `EPICS_MCP_CHANNELFINDER_MAX_RESULTS` | `500` | Cap on channels per prefix query |
| `EPICS_MCP_CHANNELFINDER_SAFE_OWNER_ACCOUNTS` | _(ESS: `recceiver`)_ | DS-PRIVACY owner allowlist — service accounts whose `owner` is surfaced; unset = ESS default, comma-list = override, empty = redact all |
| `EPICS_MCP_CHANNELFINDER_SAFE_PROPERTY_NAMES` | _(ESS: `iocName,hostName,iocid,pvStatus,time`)_ | DS-PRIVACY property allowlist — technical property names surfaced; unset = ESS default, comma-list = override, empty = redact all |
| `EPICS_MCP_ARCHIVER_URL` | _(empty)_ | `is_archived` / `get_pv_history` / `get_archive_info` / `list_archived_pvs` (Archiver MGMT root, `:17665`) |
| `EPICS_MCP_ARCHIVER_RETRIEVAL_URL` | _(empty)_ | Archiver retrieval root (`:17668`); empty falls back to `ARCHIVER_URL` |
| `EPICS_MCP_ARCHIVER_AUTH` | _(empty)_ | Optional `Authorization` header for the Archiver |
| `EPICS_MCP_ALARM_URL` | _(empty)_ | `is_alarm_configured` / `get_alarm_history` (Phoebus Alarm Logger REST root) |
| `EPICS_MCP_ALARM_AUTH` | _(empty)_ | Optional `Authorization` header for the Alarm Logger |
| `EPICS_MCP_NAMING_URL` | _(empty)_ | ESS Naming plane for `lookup_device_name` / `diagnose_connection` / `crossplane_check`. **No built-in host — no egress unless set** |
| `EPICS_MCP_OLOG_URL` | _(empty)_ | Phoebus Olog REST root (incl. context path, e.g. `.../Olog`) for `search_logbook` / `get_log_entry`. **No built-in host — no egress unless set** |
| `EPICS_MCP_OLOG_AUTH` | _(empty)_ | Optional `Authorization` header for a secured Olog (READ only) |
| `EPICS_MCP_ALLOW_OLOG_WRITE` | `false` | Master gate for `create_log_entry` / `reply_to_log` (separate from `ALLOW_PV_WRITE`) |
| `EPICS_MCP_OLOG_WRITE_USER` | _(empty)_ | Basic-auth service account for Olog writes (a dedicated account, never a personal login — it becomes the record `owner`) |
| `EPICS_MCP_OLOG_WRITE_PASSWORD` | _(empty)_ | Basic-auth password for the write service account |
| `EPICS_MCP_OLOG_WRITE_LOGBOOKS` | _(empty)_ | Comma-separated logbook names a write may target; **empty + gate on = deny-all** |
| `EPICS_MCP_OLOG_WRITE_RATE_LIMIT` | `5` | Max Olog writes per 60 s window |
| `EPICS_MCP_OLOG_WRITE_URL_ALLOWLIST` | _(empty)_ | Comma-separated exact base URLs allowed as non-loopback write targets (only with `_ALLOW_REMOTE`) |
| `EPICS_MCP_OLOG_WRITE_ALLOW_REMOTE` | `false` | Permit writes to a non-loopback (allowlisted) Olog. Default false: only loopback is writable. Prefer `https://` for a remote (Basic creds are cleartext otherwise) |

**EPICS network** (standard EPICS env; controls what the server can reach)

| Variable | Default | Description |
|----------|---------|-------------|
| `EPICS_PVA_ADDR_LIST` | `127.0.0.1` | PVAccess address list — widen to reach real IOCs |
| `EPICS_CA_ADDR_LIST` | `127.0.0.1` | Channel Access address list |
| `EPICS_CA_AUTO_ADDR_LIST` | `YES` | Auto-augment the CA address list |

## MCP client integration

Add to your `.mcp.json` or `claude_desktop_config.json`. **Read-only, localhost:**

```json
{
  "mcpServers": {
    "epics-pv": {
      "command": "epics-pv-mcp",
      "env": { "EPICS_MCP_PROVIDER": "pva" }
    }
  }
}
```

**With the REST planes enabled:**

```json
{
  "mcpServers": {
    "epics-pv": {
      "command": "epics-pv-mcp",
      "env": {
        "EPICS_MCP_CHANNELFINDER_URL": "http://localhost:8080/ChannelFinder",
        "EPICS_MCP_ARCHIVER_URL": "http://localhost:17665",
        "EPICS_MCP_ALARM_URL": "http://localhost:8081"
      }
    }
  }
}
```

**Writes enabled for test PVs only** (triple-gated):

```json
{
  "mcpServers": {
    "epics-pv": {
      "command": "epics-pv-mcp",
      "env": {
        "EPICS_MCP_ALLOW_PV_WRITE": "true",
        "EPICS_MCP_PV_WRITE_PATTERN": "^TEST:.*"
      }
    }
  }
}
```

## Architecture

A layered `server → tools → services → clients` design with pure, deterministic analysis
cores and injected protocol checkers. See [ARCHITECTURE.md](ARCHITECTURE.md) for the layer
diagram and the plane model.

## Compatibility

Exercised against a local EPICS stack: an **e3** test IOC (PVAccess), an **EPICS Archiver
Appliance** (single- or multi-instance), **ChannelFinder**, and the **Phoebus Alarm** server +
logger. Archiver topology note: in a single-JVM appliance the MGMT and retrieval webapps share a
port (leave `ARCHIVER_RETRIEVAL_URL` empty); in a split/clustered deployment MGMT (`:17665`) and
retrieval (`:17668`) are separate ports.

## Development

The gate chain is [uv](https://docs.astral.sh/uv/)-based:

```bash
uv sync --extra all --frozen              # full local install (dev tools + display extra)
uv run pytest                             # test suite
uv run pytest --cov=src --cov-branch      # with coverage
uv run pre-commit run --all-files         # ruff check + ruff format + mypy --strict + guards
```

The `dev` extra is the core toolchain; `all` adds the `[displays]` extra (the `opi_navigation`
PV engine) so the display-aware tools and their tests run. CI installs `--extra dev` only —
`opi_navigation` is privately hosted, so CI tests the standalone core a public user gets, and
the `opi_navigation`-coupled test modules self-skip when it's absent.

Live tests that need a running EPICS stack are opt-in and skip by default. See
[CONTRIBUTING.md](CONTRIBUTING.md). Release history is in [CHANGELOG.md](CHANGELOG.md).

## Related / roadmap

The display-aware tools join live PVs with the *display* plane — the macro-expanded PV
inventory of `.bob` operator screens — via the optional `[displays]` extra
(`pip install epics-pv-mcp[displays]`), which pulls the `opi_navigation` PV engine. The
core PV server installs and runs fully **without** it.

A dedicated **CS-Studio / Phoebus MCP** that complements these tools is in the works and
will be released separately.

## License

MIT — see [LICENSE](LICENSE).

## Credits

Independently developed EPICS PV MCP server — originally seeded from
[Jacky1-Jiang/EPICS-MCP-Server](https://github.com/Jacky1-Jiang/EPICS-MCP-Server) (MIT)
and since rewritten on [FastMCP](https://github.com/jlowin/fastmcp) and the p4p library,
with a write-safety layer, batch operations, PV monitoring, cross-plane provenance, and
OPI validation by epicDirk.
