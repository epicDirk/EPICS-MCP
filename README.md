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

It is **read-only by default**, and its network reach is decided by the **launcher's EPICS
search-path environment**: the address lists, `EPICS_PVA_NAME_SERVERS` (TCP unicast, not
subnet-bound), and the auto-address search — which EPICS defaults to **ON**, so even a null
environment broadcasts PV searches into the local subnets. The optional REST services stay
off until their URLs are set. Do not assume isolation from this document — run
`epics-doctor` to see what an instance actually reaches (see
[Safety & network posture](#safety--network-posture)).

## The planes it sees

The server joins several *planes* of an EPICS installation. Everything it reports is
framed in these terms:

| Plane | Source | Tools |
|-------|--------|-------|
| **Live** | p4p (PVAccess / Channel Access) | `get_pv_value`, `get_pvs`, `get_pv_info`, `monitor_pv`, `discover_pvs`, `set_pv_value` |
| **Registry** | ChannelFinder | `find_channels`, `list_channel_vocabulary`, `diagnose_connection`, `coverage_audit` |
| **History** | EPICS Archiver Appliance | `is_archived`, `get_pv_history`, `get_archive_info` |
| **Alarm** | Phoebus Alarm Logger | `is_alarm_configured`, `get_alarm_history` |
| **Naming** | ESS Naming Service | `lookup_device_name`, `diagnose_connection`, `crossplane_check` |
| **Logbook** | Phoebus Olog | `search_logbook`, `get_log_entry`, `list_logbooks`, `list_tags`, `list_log_levels`, `create_log_entry`, `reply_to_log`, `update_log_entry`, `add_log_attachment`, `list_log_attachments`, `download_log_attachment` |
| **Display** | `.bob` operator screens (CS-Studio / Phoebus) | `validate_pvs`, `crossplane_check`, `coverage_audit`, `find_device` |
| **IOC** | e3 `st.cmd` (+ optional `.db`) | `crossplane_check` |

The Live plane is the **only** authority for connected/disconnected; every other plane is
explanatory and is *withheld* (never a false negative) when its service is not configured.

**Strict response schemas (S11).** Every REST client validates a 2xx payload against the
measured schema of its endpoint. An unreadable payload **raises a loud error** — except in
`get_pv_history`, whose existing `status` channel reports it as `withheld` — and is **never**
minted into a definitive answer. (`is_alarm_configured`'s `null` stays the *readable-but-
tree-ambiguous* verdict; an unreadable payload there raises like everywhere else.) Definitive
negatives come only from each service's measured signal: Archiver `getPVTypeInfo` HTTP 404 and
Olog `get_log_entry` HTTP 404. **Naming HTTP 204/404 is definitive only once the responder proves
it is the Naming Service** via its `/rest/swagger.json` beacon (S13) — otherwise the "not
registered" answer is withheld, so a foreign/misconfigured URL's 404 no longer mints a false
`name_typo`. (Archiver and Olog 404 stay ungated — the same latent question, deferred as
lower-severity.) On every top-level listing and record, junk list items are never silently
dropped, missing fields never zero-filled, non-strings never stringified into names. The one
deliberate exception is metadata INSIDE an already-anchored record (a channel's
properties/tags, a log entry's logbook/tag names): there a malformed element is dropped rather
than sinking the whole record — documented at the two projection helpers — and even there
nothing is ever fabricated (a `null` never becomes the string `"None"`).

## Safety & network posture

This is a controls tool, so the trust questions come first:

- **Read-only by default.** The mutating tools are gated off. `set_pv_value` is **triple-gated**:
  `EPICS_MCP_ALLOW_PV_WRITE=true` **and** a regex allowlist (`EPICS_MCP_PV_WRITE_PATTERN` —
  **required** when writes are on: an empty pattern makes the server **refuse to start** rather
  than silently allow every PV; use `.*` to deliberately allow all) **and** a per-minute rate
  limit — every write is audit-logged: a rejected one is `DENY`d at the gate (nothing sent); an
  accepted one logs `ATTEMPT` before the I/O, then a terminal `ALLOW`/`FAILED` — or `UNKNOWN_PENDING`
  if it was cancelled mid-put (the value may still land at the IOC, so verify by read-back and never
  blindly retry). A write-enabled server **refuses to start** unless `EPICS_MCP_AUDIT_LOG_FILE` names
  a durable path — an ephemeral stderr audit would lose this trail on restart.
- **Every write is bounds-checked (always-on).** Before the put, the written value is checked
  against the record's own drive limits (`control` DRVL/DRVH); an out-of-range value is refused with
  `PVWriteBoundsError` (`PV_WRITE_OUT_OF_BOUNDS`) plus a `BOUNDS_DENY` audit event **before it
  reaches the IOC** — the name/rate gate only allowlists the PV name, never the value. A record with
  no declared limits (or a non-numeric value) is not bounds-checkable; the write proceeds and the
  result's `bounds_note` says so.
- **Every write is read back (always-on).** After a sanctioned put the server reads the value back
  and compares it to what was written: the result carries `verified` (true within tolerance / false
  mismatch / null not verifiable) plus `readback`/`tolerance`/`note`, and a `READBACK_OK` /
  `READBACK_MISMATCH` / `READBACK_UNVERIFIED` audit line follows the `ALLOW`. A mismatch is **not**
  an error (the put happened) — it is a loud `verified=false` plus the audit line, the direct
  countermeasure to a silent wrong-write. Tolerance is the record's `control.min_step` (> 0) or
  `EPICS_MCP_READBACK_TOLERANCE`.
- **Writes enabled require a loopback-only search reach.** Enabling writes with the EPICS client
  search env able to reach beyond loopback makes the server **refuse to start** (`SafetyConfigError`):
  the name-pattern above scopes *what* may be written, this gate scopes *where* a write can
  physically go, and both must hold. To write, the address lists (`EPICS_PVA/CA_ADDR_LIST`) and
  name servers (`EPICS_PVA/CA_NAME_SERVERS`) must be loopback hosts only, and the subnet broadcast
  must be off (`EPICS_PVA_AUTO_ADDR_LIST=NO` **and** `EPICS_CA_AUTO_ADDR_LIST=NO` — unset means ON).
  The check is parser-faithful (a spelling the real client rejects still counts as broadcasting) and
  resolution-free (a hostname is never trusted as loopback). This makes "read the facility, write
  the facility" a start-time impossibility, not a discipline.
- **Olog logbook write is a *separate* gate.** All four write tools — `create_log_entry`, `reply_to_log`, `add_log_attachment` and `update_log_entry` (the last two MUTATE an existing entry) — need
  `EPICS_MCP_ALLOW_OLOG_WRITE=true` **and** a **test-server URL boundary** (only a loopback Olog,
  or an exact **https** URL in `EPICS_MCP_OLOG_WRITE_URL_ALLOWLIST` with
  `EPICS_MCP_OLOG_WRITE_ALLOW_REMOTE=true` — a non-loopback/private host, and any plain-http remote,
  is refused by default, so a production write is a deliberate double action) **and** a logbook allowlist
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
- **Network reach is the launcher's decision, not this server's.** PV searches follow the
  standard EPICS env: the address lists (`EPICS_PVA_ADDR_LIST` / `EPICS_CA_ADDR_LIST`), the name
  servers (`EPICS_PVA_NAME_SERVERS` — TCP unicast, **not** subnet-bound), and the auto-address
  search (`*_AUTO_ADDR_LIST`), which EPICS defaults to **ON** when unset — even an empty
  environment broadcasts PV searches into the local subnets. A deployment may well point the env
  at a real facility — so do **not** assume isolation from this document; run `epics-doctor` to
  see what your instance actually reaches (it claims `localhost-isolated` only when every search
  list is unset and the auto search is explicitly disabled). The write gates above hold either way.
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
| `find_channels` | ChannelFinder — which IOC/host serves a PV + tags/properties; optional property/tag filters (`has_properties`/`lacks_properties` incl. `prop!=*` "lacks", `not_property_values`, `has_tags`/`lacks_tags`) and `count_only` for the exact match count via `/count`. The two modes return DISJOINT fields: the list is `{enabled, channels, total, capped}`, the count is `{enabled, match_count}` — plus a `note` on either when CF is unconfigured, and `enabled` is the only field on every path. Property filtering is gated to the DS-privacy safe-property allowlist (a redacted property like `accessGroup` is refused); tag-filter (`has_tags`/`lacks_tags`) semantics UNVERIFIED until a live probe | `EPICS_MCP_CHANNELFINDER_URL` |
| `list_channel_vocabulary` | ChannelFinder — the property keys + tag names `find_channels` can be filtered on, NAMES only (`{enabled, properties, tags}`). `properties` is the allowlisted subset present in this instance (never advertises a key `find_channels` would refuse); `tags` is the full server tag set. An empty list ≠ CF unconfigured (`enabled:false`); an unreadable listing raises | `EPICS_MCP_CHANNELFINDER_URL` |
| `is_archived` | Archiver Appliance — is a PV being archived? (+ connection_state / last_event / is_monitored, plus the connection-history cluster connection_loss_regain_count / connection_first_established / connection_last_restablished, from the same getPVStatus call) | `EPICS_MCP_ARCHIVER_URL` |
| `get_pv_history` | Archiver Appliance — archived samples over an ISO-8601 window (+ the getData.json `meta` block: EGU units, PREC precision; `status` = ok/empty/withheld so an empty result is never mistaken for "could not read"; one unreadable sample withholds the whole result) | `EPICS_MCP_ARCHIVER_URL` |
| `get_archive_info` | Archiver Appliance — how a PV is archived (sampling method/period, STS/MTS/LTS retention, DBRType, archived fields, source host, the alarm/display/control limits + units/precision — the nine numeric limits read `"0.0"` when unset, so `"0.0"` ≠ a literal zero limit — and controlling_pv/policy_name/modification_time); `found:false` ONLY on the 404 (never-archived) — an unreadable 2xx errors, never a false `found` | `EPICS_MCP_ARCHIVER_URL` |
| `get_appliance_info` | Archiver Appliance — the appliance's own topology (no PV): `identity`, the per-plane root URLs (mgmt/engine/etl/retrieval/data_retrieval), `cluster_inet_port` and a `version` string — the `getApplianceInfo` body of which the doctor plane-check keeps only `identity`. Confirms WHICH cluster you are on before trusting `list_archived_pvs`/`get_pv_history` (the wrong cluster silently returns a complete-looking list of the WRONG PVs) and reads a split/proxied deployment's plane layout. No `found` key; `version` omitted when absent (not "always present"); a 404 = wrong endpoint (retrieval serves `/retrieval/bpl`, not `/mgmt/bpl`), which errors rather than a false empty answer | `EPICS_MCP_ARCHIVER_URL` |
| `list_archived_pvs` | Archiver Appliance — enumerate archived PV names (getAllPVs / `this_appliance`=getPVsForThisAppliance, not getMatchingPVs); optional name-glob `pattern` (getAllPVs only — `getPVsForThisAppliance` has no name filter, so the combination is refused rather than answered unfiltered), honest `capped` | `EPICS_MCP_ARCHIVER_URL` |
| `is_alarm_configured` | Phoebus Alarm Logger — is a PV in the alarm tree? | `EPICS_MCP_ALARM_URL` |
| `get_alarm_history` | Phoebus Alarm Logger — alarm state history of a PV over a window (required start/end; newest-first; user/host stripped) | `EPICS_MCP_ALARM_URL` |
| `lookup_device_name` | ESS Naming Service — is a device name registered + ACTIVE? (HTTP 204 — the real service's measured "no such name" — and 404 = definitive not-registered **only after the `/rest/swagger.json` identity probe confirms the responder, S13**; a service error, unreadable record or unverified identity is withheld, never a false negative) | `EPICS_MCP_NAMING_URL` |
| `search_logbook` | Phoebus Olog — search log entries (DS-PRIVACY: author dropped, title/description free text withheld, attachments as a count — **unless a declared local test sandbox**, where entries come back whole; see "Olog output posture" below); paginate with `offset`/`sort`, `total_matches` = true total across all pages. Filter by `level` (triage axis, OR over `,`/`;`/`\|`) and `title` (whole WORDS, not substrings — wildcard with `*`; separate axis from `text`, which searches the body only); both case-insensitive and differentially probed. An **unknown level returns 0 hits rather than an error**, so an empty level-filtered result is annotated — see `list_log_levels` | `EPICS_MCP_OLOG_URL` |
| `get_log_entry` | Phoebus Olog — one entry by id (same redaction; 404 = definitive found:false, everything else — incl. an unreadable 2xx — errors; disabled → found:null) | `EPICS_MCP_OLOG_URL` |
| `list_logbooks` | Phoebus Olog — list the valid logbook names (name-only; owners dropped) | `EPICS_MCP_OLOG_URL` |
| `list_tags` | Phoebus Olog — list the valid tag names | `EPICS_MCP_OLOG_URL` |
| `list_log_levels` | Phoebus Olog — list the valid log levels (the triage axis; site-configurable, so the server is the only source). Returns the names plus `default_level` — the one a create uses when none is given, `null` + a note when the server does not state it unambiguously. Call it before filtering a search by level: an unknown level is **not rejected**, it returns 0 hits | `EPICS_MCP_OLOG_URL` |
| `create_log_entry` | Phoebus Olog — **post** a log entry (MUTATING; own gate + test-server URL boundary + logbook allowlist + upload-size cap + rate limit; author = the service account, not spoofable; response redacted). Optional `attachments` (workspace file paths, any type/size — multipart) + `embed_image_base64` (small inline image) | `EPICS_MCP_OLOG_URL` + `EPICS_MCP_ALLOW_OLOG_WRITE` |
| `reply_to_log` | Phoebus Olog — **reply** to an entry (threads via the Log Entry Group; same gate/redaction/attachments as create_log_entry; bad id → 400) | `EPICS_MCP_OLOG_URL` + `EPICS_MCP_ALLOW_OLOG_WRITE` |
| `add_log_attachment` | Phoebus Olog — attach file(s) to an **existing** entry (MUTATING; same gate as create, allowlist keyed on the TARGET entry's logbooks). **Whole-mode only** — the server's update is destructive (prunes/overwrites), so it round-trips the full entry; the attach is purely **additive** for CONTENT (existing attachments + content fields preserved) — but the server re-stamps `owner` to the write service account on every call, because this endpoint IS its destructive update | `EPICS_MCP_OLOG_URL` + `EPICS_MCP_ALLOW_OLOG_WRITE` (+ whole-mode) |
| `update_log_entry` | Phoebus Olog — edit an **existing** entry's `title` / body / `level` / `logbooks` / `tags` (MUTATING). **Whole-mode only**, same destructive endpoint: an omitted argument means *unchanged*, and attachments + properties + unedited fields are round-tripped verbatim. The logbook allowlist is keyed on the **UNION** of current + resulting logbooks (a move writes to both). A body edit goes to the raw `source` (the server regenerates the rendered text); an entry with duplicate/missing attachment filenames is **refused** (Olog matches attachments by filename) | `EPICS_MCP_OLOG_URL` + `EPICS_MCP_ALLOW_OLOG_WRITE` (+ whole-mode) |
| `list_log_attachments` | Phoebus Olog — list one entry's attachments (id + fileMetadataDescription; filename **whole-mode only** — author free text; 404 → found:false) | `EPICS_MCP_OLOG_URL` |
| `download_log_attachment` | Phoebus Olog — download one attachment's raw **bytes** by (log_id + filename) or GridFS id, to a workspace `output_path` or `as_base64`. **Withheld** unless whole-mode **AND** `EPICS_MCP_OLOG_ALLOW_ATTACHMENT_DOWNLOAD` (bytes bypass the entry redaction) | `EPICS_MCP_OLOG_URL` + `EPICS_MCP_OLOG_ALLOW_ATTACHMENT_DOWNLOAD` |

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
— it is a scriptable pass/fail, so it exits `0` when nothing failed and no identity probe failed,
`1` when a configured plane HARD-fails (unreachable / CA error / API error / probe-disconnect /
config_error — e.g. a retrieval URL with no archiver URL, which no tool would ever use — or
backend_down, a reachable+identified plane whose backend is down, e.g. the alarm logger's
Elasticsearch),
`2` on a usage error, and `3` (INCONCLUSIVE) when a plane is reachable but its identity probe
FAILED — a served non-2xx like a 401/404, a transport error, or a refused redirect on the identity
endpoint. Exit 3 is not a hard failure (the plane's tool endpoints may work) but not a silent
all-clear either. A service that ANSWERS with a *different* known service's name is reported
`unverified` (exit 0) with the found name in the detail — not a failure, because a path-based
reverse proxy can serve the real API behind a base URL that names another service (measured). Run
it first in a new facility to confirm your `.env` (see `docs/deployment.md`).

Each plane is also asked to **name itself**, because reachable is not identified: the transport probe
counts any HTTP response as reachable, so a URL aimed at the wrong host can look alive (measured: a
ChannelFinder URL pointing at a dead container read `✓ ok` because an unrelated service on that port
answered 401). A plane that ANSWERED (2xx) but cannot prove what it is reports `unverified` (`?`) —
honest, **not** healthy, exit `0`; a plane whose identity probe FAILED reports `identity_probe_failed`
(`!`) — reachable but suspect, exit `3`. ⚠️ Exit `0` therefore means "nothing failed", **not**
"everything confirmed": a script must read `verification_complete` / `unverified_planes` /
`inconclusive_identity_planes` from `--json` rather than the exit code alone (a failed probe lands in
`inconclusive_identity_planes`, **not** `unverified_planes`) — and for **positive** confirmation
assert `identified_planes` is non-empty, because `verification_complete` is vacuously true on an
empty config (nothing probed ≠ everything confirmed).

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
| `EPICS_MCP_MONITOR_MAX_CONCURRENCY` | `8` | Dedicated monitor-executor width; caps concurrent long monitors so they cannot starve the shared thread pool (≥ 1) |
| `EPICS_MCP_DIAGNOSE_TIMEOUT` | `5.0` | Live-probe timeout for `diagnose_connection` (> 0) |

**Write safety** (all writes blocked unless enabled)

| Variable | Default | Description |
|----------|---------|-------------|
| `EPICS_MCP_ALLOW_PV_WRITE` | `false` | Master switch for PV writes |
| `EPICS_MCP_PV_WRITE_PATTERN` | _(empty)_ | Regex allowlist of writable PV names (e.g. `^TEST:.*`); **required** once writes are enabled — empty + writes on refuses to start |
| `EPICS_MCP_WRITE_RATE_LIMIT` | `10` | Max writes per minute (≥ 1) |
| `EPICS_MCP_READ_RATE_LIMIT` | `0` | Max REST reads per 60 s (0 = disabled, opt-in); over the limit the shared GET chokepoint raises rather than blocks |
| `EPICS_MCP_AUDIT_LOG_FILE` | _(empty)_ | Audit log path; empty = stderr (ephemeral). **Required** when a write gate is on — a write-enabled server refuses to start without a durable path |
| `EPICS_MCP_READBACK_TOLERANCE` | `1e-06` | Fallback tolerance for the always-on post-write readback verification (feeds both `math.isclose` axes); used only when the record has no usable `control.min_step` (≥ 0) |

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
| `EPICS_MCP_ALLOW_OLOG_WRITE` | `false` | Master gate for `create_log_entry` / `reply_to_log` / `add_log_attachment` / `update_log_entry` (separate from `ALLOW_PV_WRITE`) |
| `EPICS_MCP_OLOG_WRITE_USER` | _(empty)_ | Basic-auth service account for Olog writes (a dedicated account, never a personal login — it becomes the record `owner`) |
| `EPICS_MCP_OLOG_WRITE_PASSWORD` | _(empty)_ | Basic-auth password for the write service account |
| `EPICS_MCP_OLOG_WRITE_LOGBOOKS` | _(empty)_ | Comma-separated logbook names a write may target; **empty + gate on = deny-all** |
| `EPICS_MCP_OLOG_WRITE_RATE_LIMIT` | `5` | Max Olog writes per 60 s window |
| `EPICS_MCP_OLOG_WRITE_URL_ALLOWLIST` | _(empty)_ | Comma-separated exact base URLs allowed as non-loopback write targets (only with `_ALLOW_REMOTE`) |
| `EPICS_MCP_OLOG_WRITE_ALLOW_REMOTE` | `false` | Permit writes to a non-loopback (allowlisted) Olog. Default false: only loopback is writable. A remote **must** be `https://` — a plain-http remote is refused (Basic creds are cleartext). The write session is env-independent (no proxy/`REQUESTS_CA_BUNDLE` env): give a remote's CA via `EPICS_MCP_CA_BUNDLE` |
| `EPICS_MCP_OLOG_ALLOW_ATTACHMENT_DOWNLOAD` | `false` | Opt-in for `download_log_attachment` to hand back raw **bytes** (+ list filenames): only in whole-mode (loopback + `ASSUME_TEST_DATA`) **and** with this set. Bytes bypass the entry redaction; the by-id endpoint has no server-side per-log auth — so byte egress is a deliberate choice |
| `EPICS_MCP_OLOG_ATTACH_MAX_BYTES` | `52428800` | Client-side cap on total upload bytes (checked before files are read; the server enforces its own 413) |

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

**Writes enabled for test PVs only** (triple-gated — the pattern is required, an empty one refuses to start):

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
