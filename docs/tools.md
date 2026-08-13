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
| `monitor_pv` | Subscribe to PV updates for a bounded duration; reports the channel `connection` so an empty result is readable |
| `discover_pvs` | Probe a concrete PV name (wildcards need ChannelFinder) |
| `set_pv_value` | Write a value to a PV, **gated off by default** (see Safety) |

**Connection diagnosis** (always available)

| Tool | Description |
|------|-------------|
| `diagnose_connection` | Why a PV is (dis)connected: live-authoritative state + likely cause (`healthy`/`ioc_down`/`name_typo`/`unregistered`/`indeterminate`) + per-plane evidence + next steps |

**Cross-plane services** (each enabled by its `*_URL`)

| Tool | Service | Enabled by |
|------|---------|-----------|
| `find_channels` | ChannelFinder: which IOC/host serves a PV, plus its tags/properties. Optional server-side property and tag filters, and `count_only` for an exact match count without pulling the matches. Property filtering is confined to the privacy-safe property allowlist. ⚠️ The tag-filter semantics are UNVERIFIED until a live probe | `EPICS_MCP_CHANNELFINDER_URL` |
| `list_channel_vocabulary` | ChannelFinder: the property keys and tag names `find_channels` can be filtered on, names only. An empty list is not the same as ChannelFinder being unconfigured | `EPICS_MCP_CHANNELFINDER_URL` |
| `is_archived` | Archiver Appliance: is a PV being archived, plus its connection state, last event and flapping counters, all harvested from the one `getPVStatus` call | `EPICS_MCP_ARCHIVER_URL` |
| `get_pv_history` | Archiver Appliance: archived samples over an ISO-8601 window, with the units/precision meta block. `status` is ok/empty/withheld, so an empty result is never mistaken for a failed read | `EPICS_MCP_ARCHIVER_URL` |
| `get_archive_info` | Archiver Appliance: how a PV is archived (sampling method and period, retention, DBRType, source host, the alarm/display/control limits, policy). `found:false` comes ONLY from the 404; an unreadable 2xx errors rather than minting a false answer | `EPICS_MCP_ARCHIVER_URL` |
| `get_appliance_info` | Archiver Appliance: the appliance's own topology, no PV needed. Confirms WHICH cluster you are on before you trust `list_archived_pvs` or `get_pv_history`, because the wrong cluster returns a complete-looking list of the wrong PVs | `EPICS_MCP_ARCHIVER_URL` |
| `list_archived_pvs` | Archiver Appliance: enumerate archived PV names, with an honest `capped`. An optional name glob works on the whole-appliance endpoint only, so combining it with `this_appliance` is refused rather than answered unfiltered | `EPICS_MCP_ARCHIVER_URL` |
| `is_alarm_configured` | Phoebus Alarm Logger: is a PV in the alarm tree? | `EPICS_MCP_ALARM_URL` |
| `get_alarm_history` | Phoebus Alarm Logger: alarm state history of a PV over a window (required start/end; newest-first; known AlarmLogMessage fields incl. user/host/command) | `EPICS_MCP_ALARM_URL` |
| `lookup_device_name` | ESS Naming Service: is a device name registered and ACTIVE? A definitive negative is given only after the identity beacon confirms the responder; an unverified or failing service is withheld, never a false negative | `EPICS_MCP_NAMING_URL` |
| `search_logbook` | Phoebus Olog: search log entries, returned whole. Paginate with `offset`/`sort`. Filter by `level`, and by `title`, which matches whole WORDS and is a separate axis from `text` (the body). ⚠️ An unknown level returns 0 hits rather than an error, so call `list_log_levels` first. ⚠️ Whole means BOTH body fields: `source` is the raw body, `description` the server's rendering of it, so a body you mean to write back comes from `source` | `EPICS_MCP_OLOG_URL` |
| `get_log_entry` | Phoebus Olog: one entry by id, whole (404 = definitive found:false, everything else (including an unreadable 2xx) errors; disabled → found:null). ⚠️ Whole means BOTH body fields; to edit this entry, read `source`, not `description` | `EPICS_MCP_OLOG_URL` |
| `list_logbooks` | Phoebus Olog: list the valid logbook names (name-only; owners dropped) | `EPICS_MCP_OLOG_URL` |
| `list_tags` | Phoebus Olog: list the valid tag names | `EPICS_MCP_OLOG_URL` |
| `list_log_levels` | Phoebus Olog: the valid log levels plus `default_level`. Site-configurable, so the server is the only source. Call it before filtering a search by level | `EPICS_MCP_OLOG_URL` |
| `create_log_entry` | Phoebus Olog: **post** a log entry (MUTATING, behind its own gate). Optional `attachments` (workspace file paths) and `embed_image_base64`. The author is the write service account and cannot be spoofed | `EPICS_MCP_OLOG_URL` + `EPICS_MCP_ALLOW_OLOG_WRITE` |
| `reply_to_log` | Phoebus Olog: **reply** to an entry (threads via the Log Entry Group; same gate/response/attachments as create_log_entry; bad id → 400) | `EPICS_MCP_OLOG_URL` + `EPICS_MCP_ALLOW_OLOG_WRITE` |
| `add_log_attachment` | Phoebus Olog: attach file(s) to an **existing** entry (MUTATING). Additive for content, every existing attachment and field survives. ⚠️ `owner` does not: the server re-stamps it to the write service account, because this endpoint IS its destructive update | `EPICS_MCP_OLOG_URL` + `EPICS_MCP_ALLOW_OLOG_WRITE` |
| `update_log_entry` | Phoebus Olog: edit an existing entry's `title` / body / `level` / `logbooks` / `tags` (MUTATING). An omitted argument means unchanged. The logbook allowlist is keyed on the UNION of the current and resulting logbooks, because a move writes to both. ⚠️ The new body REPLACES the whole body, so a read-modify-write takes the entry's `source`, never its rendered `description`; where that is detectable it comes back as a warning | `EPICS_MCP_OLOG_URL` + `EPICS_MCP_ALLOW_OLOG_WRITE` |
| `list_log_attachments` | Phoebus Olog: list one entry's attachments (id + filename + fileMetadataDescription; 404 → found:false) | `EPICS_MCP_OLOG_URL` |
| `download_log_attachment` | Phoebus Olog: download one attachment's raw **bytes** by (log_id + filename) or GridFS id, to a workspace `output_path` or `as_base64`, size-capped by `EPICS_MCP_OLOG_ATTACH_MAX_BYTES` | `EPICS_MCP_OLOG_URL` |

**Display-aware** (require the `opi_navigation` engine, which is **not** installable from
PyPI: it lives in a private repository and is wired in as the local `displays` dependency
group. These four tools are therefore unavailable in a published install today.)

| Tool | Description |
|------|-------------|
| `validate_pvs` | Extract the macro-resolved PVs of a `.bob` display or a `.plt` Data Browser trend and check their connectivity. Two views: `view="file"` (default) is what the file itself declares, `view="display"` what it resolves to when opened, embedded fragments included. A screen that only composes fragments answers `total: 0` under the default. On a TREND the route decides which view finds it: embedded through a `databrowser` widget its traces are attributed to the embedding screen, so only the file view finds them here; opened by an `open_file` button the trend is a top level of its own. Every file-mode answer carries `file_path`, `shown_by_display` and `shown_by_display_capped`, plus a `notes` entry per incompleteness source (per-display context cap, glob cap). ⚠️ Passing a NON-EMPTY `pv_names` wins, the file path is then not looked at, and those three fields are absent; an empty list does not win |
| `crossplane_check` | PV provenance: display PVs ↔ e3 IOC `st.cmd` (+ optional `.db`) ↔ Naming (read-only) |
| `coverage_audit` | Cross-plane coverage matrix: delivered PVs (ChannelFinder) ↔ displays ↔ archive ↔ alarm; blind spots + critical-uncovered |
| `find_device` | Which operator screens show a device, its live channel values (capped), and the serving IOC. The live cap does not shorten the screen list; the inventory walk's own two caps can, and each fires a `notes` entry naming the count |

## Command-line tools (no AI needed)

The cross-plane analyses are also standalone CLIs, useful in a terminal or CI, with no
MCP client involved:

```bash
epics-testpv                                            # serve two test PVs, loopback, until Ctrl-C
epics-init       --list                                 # emit a client configuration, then check it
epics-doctor                                            # read-only self-check: planes + write gates
epics-diagnose   TEST:Temperature                       # connection diagnosis
epics-crossplane --displays <project-root> --st-cmd <st.cmd>     # display ↔ IOC ↔ Naming (needs opi_navigation)
epics-coverage   --displays <project-root> --scope DEV: # coverage matrix (needs opi_navigation)
```

`epics-init` prints the MCP client-configuration block for one of four deployment shapes
(`sandbox`, `ioc-only`, `ioc-archiver`, `full`; `--list` describes them) and then runs the
`epics-doctor` checks against exactly that block, so generating a configuration and checking it are
one step. It writes nothing unless asked to: `--out PATH` writes the block as UTF-8 with LF, which a
shell redirect cannot promise (in Windows PowerShell 5.1 both `>` and `Set-Content -Encoding utf8`
produce bytes a strict JSON parser rejects). `--out` refuses an existing file unless `--force` is
given, and writes nothing at all while placeholders remain. `--absolute-command` names the installed
server by its resolved path instead of a bare name, for a client that does not inherit your `PATH`.

`epics-testpv` serves two synthetic PVs so the quick start needs no control system at all:
`TEST:Temperature`, an analogue reading with a unit, and `TEST:Heater`, a writable switch, which is
what the write-enabled example needs a target for. Nothing extra has to be installed, since p4p is
already a dependency of this package. It binds LOOPBACK by default, deliberately: a PVA server is a
network service and that switch accepts writes. `--interface` widens the reach, and the command
prints what it is listening on, including the port it actually got, which is not always the one it
asked for.

`epics-mcp`, `epics-init`, `epics-testpv`, `epics-doctor` and `epics-diagnose` are part of the
core install; `epics-crossplane` and `epics-coverage` need the `opi_navigation` engine, which a
published install cannot obtain (see above). None of them writes to a service of yours: the only
things written anywhere are the file `epics-init --out` names and the PVs `epics-testpv` serves
itself.

Every console command answers `--help` and `--version` with its own name and the package version, on a
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

`epics-diagnose` / `epics-crossplane` / `epics-coverage` exit `0` even on a negative finding: a
disconnect or a broken link is a result, not a crash. **`epics-doctor` is the deliberate
exception**, a scriptable pass/fail with four exit codes: `0` clean, `1` a configured plane
hard-failed, `2` a usage error, `3` inconclusive. The full contract, the six failing statuses, the
remedies and the `--json` keys a script must read are in
[the deployment guide](deployment.md#1-bring-it-up). Run it first in a new facility.

Each plane is also asked to **name itself**, because reachable is not identified: the transport probe
counts any HTTP response as reachable, so a URL aimed at the wrong host can look alive (measured: a
ChannelFinder URL pointing at a dead container read `✓ ok` because an unrelated service on that port
answered 401). A plane that ANSWERED (2xx) but cannot prove what it is reports `unverified` (`?`),
honest but **not** healthy, exit `0`. A plane whose identity probe FAILED reports
`identity_probe_failed` (`!`), reachable but suspect, exit `3`. A plane that proved its identity and
is measurably not doing its job reports `no_ingest` (`~`), exit `0`.

⚠️ So exit `0` means "nothing failed", **not** "everything confirmed". A script reads
`verification_complete` / `unverified_planes` / `inconclusive_identity_planes` / `degraded_planes`
from `--json` rather than the exit code alone, and asserts `identified_planes` is non-empty for
positive confirmation, because `verification_complete` is vacuously true on an empty config.

The same report carries `write_safety`, which answers a different question: what each of the two
write gates would allow, and where a write could go. It is informative and never moves the verdict
or the exit code, so a pass-or-fail script can ignore it, while a review of a deployment starts
there. The human report prints it as a `Write gates` block. Both describe the environment of the
command you ran, which need not be the one a running server was started with.

## Resources & Prompts

| Resource URI | Description |
|--------------|-------------|
| `epics-pv://health` | What this process IS and what it may WRITE: version, uptime, provider, p4p version, one boolean per service plane, both write gates, the PV search posture, and a posture group: REST TLS verification (`ca_bundle` beats a disabled `tls_verify`, and the field says the resolved answer), the REST GET read throttle, whether `EPICS_MCP_ALLOWED_ROOTS` holds a root, and how many ChannelFinder owner accounts and property names the redaction DISCLOSES. Counts and booleans only: no path, no root list and no account name |
| `epics-pv://config` | Non-secret configuration values it was started with. A userinfo (`user:password@`) is removed from each service URL and every other character is kept, so the value stays comparable with a client's configuration; an address whose userinfo could not be removed provably is `null` |
| `epics-pv://guide` | Operational cookbook: service planes, recipes, error signatures |

The guide's source is `src/epics_mcp/operator_guide.md`: one file, shipped in the wheel and served
as the resource (and mirrored for human readers by [`OPERATING.md`](../OPERATING.md)).

| Prompt | Description |
|--------|-------------|
| `diagnose_pv` | Step-by-step PV diagnosis workflow (info, read, monitor) |
| `compare_machine_state` | Compare current PV values against an expected state |
| `setup_epics_mcp` | Walk through configuring this server for a facility, one question at a time |

