# Tools, CLIs, resources and prompts

The full surface: every MCP tool grouped by plane, the standalone command-line tools that need no MCP client, and the resources and prompts the server exposes.

[Back to the README](../README.md)

**On this page:** [Tools](#tools) · [Command-line tools (no AI needed)](#command-line-tools-no-ai-needed) · [Resources & Prompts](#resources--prompts)

## Tools

**Core PV access** (always available)

| Tool | Description |
|------|-------------|
| `get_pv_value` | Read a single PV's current value (+ best-effort metadata) |
| `get_pvs` | Batch-read multiple PVs in one call |
| `get_pv_info` | Connection state, data type, alarm status, display/control limits |
| `monitor_pv` | Subscribe to PV updates for a bounded duration |
| `discover_pvs` | Probe a concrete PV name (wildcards need ChannelFinder) |
| `set_pv_value` | Write a value to a PV, **gated off by default** (see Safety) |

**Connection diagnosis** (always available)

| Tool | Description |
|------|-------------|
| `diagnose_connection` | Why a PV is (dis)connected: live-authoritative state + likely cause (`healthy`/`ioc_down`/`name_typo`/`unregistered`/`indeterminate`) + per-plane evidence + next steps |

**Cross-plane services** (each enabled by its `*_URL`)

| Tool | Service | Enabled by |
|------|---------|-----------|
| `find_channels` | ChannelFinder: which IOC/host serves a PV + tags/properties; optional property/tag filters (`has_properties`/`lacks_properties` incl. `prop!=*` "lacks", `not_property_values`, `has_tags`/`lacks_tags`) and `count_only` for the exact match count via `/count`. The two modes return DISJOINT fields, and the configuration splits them again: configured list = `{enabled, channels, total, capped}`, configured count = `{enabled, match_count}`; unconfigured they are `{enabled, channels, total, note}` and `{enabled, match_count, note}`, so `note` is added and `capped` is NOT emitted. `enabled` is the only field on every path. Property filtering is gated to the DS-privacy safe-property allowlist (a redacted property like `accessGroup` is refused); tag-filter (`has_tags`/`lacks_tags`) semantics UNVERIFIED until a live probe | `EPICS_MCP_CHANNELFINDER_URL` |
| `list_channel_vocabulary` | ChannelFinder: the property keys + tag names `find_channels` can be filtered on, NAMES only (`{enabled, properties, tags}`). `properties` is the allowlisted subset present in this instance (never advertises a key `find_channels` would refuse); `tags` is the full server tag set. An empty list ≠ CF unconfigured (`enabled:false`); an unreadable listing raises | `EPICS_MCP_CHANNELFINDER_URL` |
| `is_archived` | Archiver Appliance: is a PV being archived? (+ connection_state / last_event / is_monitored, plus the connection-history cluster connection_loss_regain_count / connection_first_established / connection_last_restablished, from the same getPVStatus call) | `EPICS_MCP_ARCHIVER_URL` |
| `get_pv_history` | Archiver Appliance: archived samples over an ISO-8601 window (+ the getData.json `meta` block: EGU units, PREC precision; `status` = ok/empty/withheld so an empty result is never mistaken for "could not read"; one unreadable sample withholds the whole result) | `EPICS_MCP_ARCHIVER_URL` |
| `get_archive_info` | Archiver Appliance: how a PV is archived (sampling method/period, STS/MTS/LTS retention, DBRType, archived fields, source host, the alarm/display/control limits + units/precision (the nine numeric limits read `"0.0"` when unset, so `"0.0"` ≠ a literal zero limit) and controlling_pv/policy_name/modification_time); `found:false` ONLY on the 404 (never-archived); an unreadable 2xx errors, never a false `found` | `EPICS_MCP_ARCHIVER_URL` |
| `get_appliance_info` | Archiver Appliance: the appliance's own topology (no PV): `identity`, the per-plane root URLs (mgmt/engine/etl/retrieval/data_retrieval), `cluster_inet_port` and a `version` string, the `getApplianceInfo` body of which the doctor plane-check keeps only `identity`. Confirms WHICH cluster you are on before trusting `list_archived_pvs`/`get_pv_history` (the wrong cluster silently returns a complete-looking list of the WRONG PVs) and reads a split/proxied deployment's plane layout. No `found` key; `version` omitted when absent (not "always present"); a 404 = wrong endpoint (retrieval serves `/retrieval/bpl`, not `/mgmt/bpl`), which errors rather than a false empty answer | `EPICS_MCP_ARCHIVER_URL` |
| `list_archived_pvs` | Archiver Appliance: enumerate archived PV names (getAllPVs / `this_appliance`=getPVsForThisAppliance, not getMatchingPVs); optional name-glob `pattern` (getAllPVs only, because `getPVsForThisAppliance` has no name filter, so the combination is refused rather than answered unfiltered), honest `capped` | `EPICS_MCP_ARCHIVER_URL` |
| `is_alarm_configured` | Phoebus Alarm Logger: is a PV in the alarm tree? | `EPICS_MCP_ALARM_URL` |
| `get_alarm_history` | Phoebus Alarm Logger: alarm state history of a PV over a window (required start/end; newest-first; user/host stripped) | `EPICS_MCP_ALARM_URL` |
| `lookup_device_name` | ESS Naming Service: is a device name registered + ACTIVE? (HTTP 204, the real service's measured "no such name", and 404 = definitive not-registered **only after the `/rest/swagger.json` identity probe confirms the responder, S13**; a service error, unreadable record or unverified identity is withheld, never a false negative) | `EPICS_MCP_NAMING_URL` |
| `search_logbook` | Phoebus Olog: search log entries (DS-PRIVACY: author dropped, title/description free text withheld, attachments as a count, **unless a declared local test sandbox**, where entries come back whole; see "Olog output posture" below); paginate with `offset`/`sort`, `total_matches` = true total across all pages. Filter by `level` (triage axis, OR over `,`/`;`/`\|`) and `title` (whole WORDS, not substrings; wildcard with `*`; separate axis from `text`, which searches the body only); both case-insensitive and differentially probed. An **unknown level returns 0 hits rather than an error**, so an empty level-filtered result is annotated; see `list_log_levels` | `EPICS_MCP_OLOG_URL` |
| `get_log_entry` | Phoebus Olog: one entry by id (same redaction; 404 = definitive found:false, everything else (including an unreadable 2xx) errors; disabled → found:null) | `EPICS_MCP_OLOG_URL` |
| `list_logbooks` | Phoebus Olog: list the valid logbook names (name-only; owners dropped) | `EPICS_MCP_OLOG_URL` |
| `list_tags` | Phoebus Olog: list the valid tag names | `EPICS_MCP_OLOG_URL` |
| `list_log_levels` | Phoebus Olog: list the valid log levels (the triage axis; site-configurable, so the server is the only source). Returns the names plus `default_level`, the one a create uses when none is given, `null` + a note when the server does not state it unambiguously. Call it before filtering a search by level: an unknown level is **not rejected**, it returns 0 hits | `EPICS_MCP_OLOG_URL` |
| `create_log_entry` | Phoebus Olog: **post** a log entry (MUTATING; own gate + test-server URL boundary + logbook allowlist + upload-size cap + rate limit; author = the service account, not spoofable; response redacted). Optional `attachments` (workspace file paths, any type/size, multipart) + `embed_image_base64` (small inline image) | `EPICS_MCP_OLOG_URL` + `EPICS_MCP_ALLOW_OLOG_WRITE` |
| `reply_to_log` | Phoebus Olog: **reply** to an entry (threads via the Log Entry Group; same gate/redaction/attachments as create_log_entry; bad id → 400) | `EPICS_MCP_OLOG_URL` + `EPICS_MCP_ALLOW_OLOG_WRITE` |
| `add_log_attachment` | Phoebus Olog: attach file(s) to an **existing** entry (MUTATING; same gate as create, allowlist keyed on the TARGET entry's logbooks). **Whole-mode only**, because the server's update is destructive (prunes/overwrites), so it round-trips the full entry; the attach is purely **additive** for CONTENT (existing attachments + content fields preserved), but the server re-stamps `owner` to the write service account on every call, because this endpoint IS its destructive update | `EPICS_MCP_OLOG_URL` + `EPICS_MCP_ALLOW_OLOG_WRITE` (+ whole-mode) |
| `update_log_entry` | Phoebus Olog: edit an **existing** entry's `title` / body / `level` / `logbooks` / `tags` (MUTATING). **Whole-mode only**, same destructive endpoint: an omitted argument means *unchanged*, and attachments + properties + unedited fields are round-tripped verbatim. The logbook allowlist is keyed on the **UNION** of current + resulting logbooks (a move writes to both). A body edit goes to the raw `source` (the server regenerates the rendered text); an entry with duplicate/missing attachment filenames is **refused** (Olog matches attachments by filename) | `EPICS_MCP_OLOG_URL` + `EPICS_MCP_ALLOW_OLOG_WRITE` (+ whole-mode) |
| `list_log_attachments` | Phoebus Olog: list one entry's attachments (id + fileMetadataDescription; filename **whole-mode only**, since it is author free text; 404 → found:false) | `EPICS_MCP_OLOG_URL` |
| `download_log_attachment` | Phoebus Olog: download one attachment's raw **bytes** by (log_id + filename) or GridFS id, to a workspace `output_path` or `as_base64`. **Withheld** unless whole-mode **AND** `EPICS_MCP_OLOG_ALLOW_ATTACHMENT_DOWNLOAD` (bytes bypass the entry redaction) | `EPICS_MCP_OLOG_URL` + `EPICS_MCP_OLOG_ALLOW_ATTACHMENT_DOWNLOAD` |

**Display-aware** (require the `opi_navigation` engine, which is **not** installable from
PyPI: it lives in a private repository and is wired in as the local `displays` dependency
group. These four tools are therefore unavailable in a published install today.)

| Tool | Description |
|------|-------------|
| `validate_pvs` | Extract the macro-resolved PVs of a `.bob` display and check their connectivity |
| `crossplane_check` | PV provenance: display PVs ↔ e3 IOC `st.cmd` (+ optional `.db`) ↔ Naming (read-only) |
| `coverage_audit` | Cross-plane coverage matrix: delivered PVs (ChannelFinder) ↔ displays ↔ archive ↔ alarm; blind spots + critical-uncovered |
| `find_device` | Which operator screens show a device, its live channel values (capped), and the serving IOC |

## Command-line tools (no AI needed)

The cross-plane analyses are also standalone CLIs, useful in a terminal or CI, with no
MCP client involved:

```bash
epics-doctor                                            # read-only config self-check (all planes)
epics-diagnose   TEST:Temperature                       # connection diagnosis
epics-crossplane --displays <project-root> --st-cmd <st.cmd>     # display ↔ IOC ↔ Naming (needs opi_navigation)
epics-coverage   --displays <project-root> --scope DEV: # coverage matrix (needs opi_navigation)
```

`epics-doctor` and `epics-diagnose` are part of the core install; `epics-crossplane` and
`epics-coverage` need the `opi_navigation` engine, which a published install cannot obtain
(see above). All are read-only.

All five commands answer `--help` and `--version` with their own name and the package version, on a
core-only install as well: they parse their arguments before asking for the display engine, so the
one command that explains the usage is never the one you cannot run.

`epics-crossplane` and `epics-coverage` still need the engine to do their work, and they say so when
asked to do it. So does a usage error on those two, deliberately: `epics-coverage --nope` on a
core-only install answers with the missing engine rather than with `the following arguments are
required`, because supplying the argument would not make the command run either. The exit code is the
engine's, not argparse's: `2` where the engine is absent (which agrees with the usage-error code
anyway) and `1` where it is installed and fails to load, the same code the command returns on that
install with correct arguments. Install the engine from a checkout with
`uv sync --extra dev --group displays`.

Every `epics-doctor` line that reports a PROBLEM also says what to change: the observation (the HTTP
code, the variable that could not be reached, the appliance figures) and then the remedy for that
status. The honest-but-not-healthy states are deliberately left without one, because there is nothing
here to set: `unverified` already carries the specific clue it measured, and `no_ingest` is a fault
inside the appliance, not in this configuration.

`epics-diagnose`/`epics-crossplane`/`epics-coverage` exit `0` even on a negative finding (a
disconnect / a broken link is a result, not a crash). **`epics-doctor` is the deliberate exception**
It is a scriptable pass/fail, so it exits `0` when nothing failed and no identity probe failed,
`1` when a configured plane HARD-fails (unreachable / CA error / API error / probe-disconnect /
config_error (for instance a retrieval URL with no archiver URL, which no tool would ever use), or
backend_down, a reachable+identified plane whose backend is down, e.g. the alarm logger's
Elasticsearch),
`2` on a usage error, and `3` (INCONCLUSIVE) when a plane is reachable but its identity probe
FAILED: a served non-2xx like a 401/404, a transport error, or a refused redirect on the identity
endpoint. Exit 3 is not a hard failure (the plane's tool endpoints may work) but not a silent
all-clear either. A service that ANSWERS with a *different* known service's name is reported
`unverified` (exit 0) with the found name in the detail, not a failure, because a path-based
reverse proxy can serve the real API behind a base URL that names another service (measured). Run
it first in a new facility to confirm the environment your launcher hands the server (see
`docs/deployment.md`).

Each plane is also asked to **name itself**, because reachable is not identified: the transport probe
counts any HTTP response as reachable, so a URL aimed at the wrong host can look alive (measured: a
ChannelFinder URL pointing at a dead container read `✓ ok` because an unrelated service on that port
answered 401). A plane that ANSWERED (2xx) but cannot prove what it is reports `unverified` (`?`),
honest, **not** healthy, exit `0`; a plane whose identity probe FAILED reports `identity_probe_failed`
(`!`): reachable but suspect, exit `3`. And identified is not working: a plane that proved its
identity and is measurably not doing its job reports `no_ingest` (`~`), exit `0` (an Archiver
appliance holding channels with none connected, or reporting one of its own webapps stopped).
⚠️ Exit `0` therefore means "nothing failed", **not**
"everything confirmed": a script must read `verification_complete` / `unverified_planes` /
`inconclusive_identity_planes` / `degraded_planes` from `--json` rather than the exit code alone (a
failed probe lands in `inconclusive_identity_planes`, **not** `unverified_planes`; a degraded one in
`degraded_planes` and in NEITHER of those), and for **positive** confirmation
assert `identified_planes` is non-empty, because `verification_complete` is vacuously true on an
empty config (nothing probed ≠ everything confirmed). ⚠️ `identified_planes` also lists a degraded
plane, since its identity IS proven, so it alone never means healthy.

## Resources & Prompts

| Resource URI | Description |
|--------------|-------------|
| `epics-pv://health` | Server health: version, uptime, provider, p4p version |
| `epics-pv://config` | Non-secret configuration values |
| `epics-pv://guide` | Operational cookbook: service planes, recipes, error signatures |

The guide's source is `src/epics_mcp/operator_guide.md`: one file, shipped in the wheel and served
as the resource (and mirrored for human readers by [`OPERATING.md`](../OPERATING.md)).

| Prompt | Description |
|--------|-------------|
| `diagnose_pv` | Step-by-step PV diagnosis workflow (info, read, monitor) |
| `compare_machine_state` | Compare current PV values against an expected state |

