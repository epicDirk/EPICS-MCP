# Configuration

Every setting is an environment variable with the `EPICS_MCP_` prefix. [`.env.example`](../.env.example) is the canonical, fully commented template; this page is the reference.

[Back to the README](../README.md)

**On this page:** [Core / p4p](#core--p4p) · [Write safety](#write-safety-all-writes-blocked-unless-enabled) · [Path boundary](#path-boundary) · [TLS trust for the HTTPS REST planes](#tls-trust-for-the-https-rest-planes) · [Optional REST services](#optional-rest-services-emptyunset-url--disabled-no-client-no-network-call) · [EPICS network](#epics-network-standard-epics-env-controls-what-the-server-can-reach)

## Core / p4p

| Variable | Default | Description |
|----------|---------|-------------|
| `EPICS_MCP_PROVIDER` | `pva` | Protocol: `pva` (PVAccess) or `ca` (Channel Access). Lowercase only |
| `EPICS_MCP_DEFAULT_TIMEOUT` | `5.0` | PV operation timeout in seconds (> 0) |
| `EPICS_MCP_MAX_BATCH_SIZE` | `100` | Max PVs per batch read (≥ 1) |
| `EPICS_MCP_MAX_MONITOR_DURATION` | `60.0` | Max monitor subscription duration in seconds (> 0) |
| `EPICS_MCP_MAX_MONITOR_EVENTS` | `1000` | Max events per monitor subscription (≥ 1) |
| `EPICS_MCP_MONITOR_MAX_CONCURRENCY` | `8` | Dedicated monitor-executor width; caps concurrent long monitors so they cannot starve the shared thread pool (≥ 1) |
| `EPICS_MCP_DIAGNOSE_TIMEOUT` | `5.0` | Live-probe timeout for `diagnose_connection` (> 0) |

## Write safety (all writes blocked unless enabled)

| Variable | Default | Description |
|----------|---------|-------------|
| `EPICS_MCP_ALLOW_PV_WRITE` | `false` | Master switch for PV writes |
| `EPICS_MCP_PV_WRITE_PATTERN` | _(empty)_ | Regex allowlist of writable PV names (e.g. `^TEST:.*`); **required** once writes are enabled; empty + writes on refuses to start |
| `EPICS_MCP_WRITE_RATE_LIMIT` | `10` | Max writes per minute (≥ 1) |
| `EPICS_MCP_READ_RATE_LIMIT` | `0` | Max REST reads per 60 s (0 = disabled, opt-in); over the limit the shared GET chokepoint raises rather than blocks |
| `EPICS_MCP_AUDIT_LOG_FILE` | _(empty)_ | Audit log path; empty = stderr (ephemeral). **Required** when a write gate is on: a write-enabled server refuses to start without a durable path |
| _(no variable of ours)_ | | **A loopback-only EPICS search reach is the third start condition for PV writes.** Enabling `EPICS_MCP_ALLOW_PV_WRITE` while the EPICS search path can reach beyond loopback refuses the start, so the network posture and the write gate are not independent settings. The variables are in the EPICS network table at the end of this page; the Olog write gate has its own conditions and is not affected |
| `EPICS_MCP_READBACK_TOLERANCE` | `1e-06` | Fallback tolerance for the always-on post-write readback verification (feeds both `math.isclose` axes); used only when the record has no usable `control.min_step` (≥ 0) |

## Path boundary

| Variable | Default | Description |
|----------|---------|-------------|
| `EPICS_MCP_ALLOWED_ROOTS` | _(empty)_ | `os.pathsep`-separated roots that file/dir args must resolve under. Empty = no boundary. Separator is `;` on Windows / `:` on Linux |

## TLS trust for the HTTPS REST planes

The most common first-deploy snag. A REST plane served over HTTPS with a certificate signed by an
internal root CA that certifi does not know fails with `self-signed certificate in chain`. The
combined-bundle recipe is in [the deployment guide](deployment.md#3-tls--ca-bundle-the-common-first-deploy-snag).

| Variable | Default | Description |
|----------|---------|-------------|
| `EPICS_MCP_CA_BUNDLE` | _(empty)_ | Path to a CA-bundle PEM. When the planes present **different** trust roots (one internal CA, another a public CA), combine your internal CA **with** the public roots (certifi's `cacert.pem`) into ONE PEM and point this at it. Use a path carrying no username. A remote Olog write target's CA must come from here, because the write session is deliberately env-independent |
| `EPICS_MCP_TLS_VERIFY` | `true` | `false` disables certificate verification entirely: a last resort on a trusted internal network only. `EPICS_MCP_CA_BUNDLE` takes **precedence** over it and keeps verification on. Precedence: `CA_BUNDLE` (a path) > `TLS_VERIFY=false` > default (certifi) |

`epics-doctor` reports `ca_error` on a plane whose TLS verification fails, distinct from
`unreachable`.

## Optional REST services (empty/unset URL = disabled, no client, no network call)

One variable in the table below is outside that heading, and the row says so: an empty
`EPICS_MCP_ARCHIVER_RETRIEVAL_URL` falls back to the mgmt URL and is still probed, because a
single-JVM appliance legitimately leaves it empty.

| Variable | Default | Enables |
|----------|---------|---------|
| `EPICS_MCP_CHANNELFINDER_URL` | _(empty)_ | `find_channels` / `list_channel_vocabulary` + the ChannelFinder plane |
| `EPICS_MCP_CHANNELFINDER_AUTH` | _(empty)_ | Optional `Authorization` header for a secured ChannelFinder |
| `EPICS_MCP_CHANNELFINDER_MAX_RESULTS` | `500` | Cap on channels per prefix query |
| `EPICS_MCP_CHANNELFINDER_SAFE_OWNER_ACCOUNTS` | _(ESS: `recceiver`)_ | DS-PRIVACY owner allowlist: service accounts whose `owner` is surfaced; unset = ESS default, comma-list = override, empty = redact all |
| `EPICS_MCP_CHANNELFINDER_SAFE_PROPERTY_NAMES` | _(ESS: `iocName,hostName,iocid,pvStatus,time`)_ | DS-PRIVACY property allowlist: technical property names surfaced; unset = ESS default, comma-list = override, empty = redact all |
| `EPICS_MCP_ARCHIVER_URL` | _(empty)_ | `is_archived` / `get_pv_history` / `get_archive_info` / `get_appliance_info` / `list_archived_pvs` (Archiver MGMT root, `:17665`) |
| `EPICS_MCP_ARCHIVER_RETRIEVAL_URL` | _(empty)_ | Archiver retrieval root (`:17668`); empty falls back to `ARCHIVER_URL` |
| `EPICS_MCP_ARCHIVER_AUTH` | _(empty)_ | Optional `Authorization` header for the Archiver |
| `EPICS_MCP_ALARM_URL` | _(empty)_ | `is_alarm_configured` / `get_alarm_history` (Phoebus Alarm Logger REST root) |
| `EPICS_MCP_ALARM_AUTH` | _(empty)_ | Optional `Authorization` header for the Alarm Logger |
| `EPICS_MCP_NAMING_URL` | _(empty)_ | ESS Naming plane for `lookup_device_name` / `diagnose_connection` / `crossplane_check`. **No built-in host, so no egress unless set** |
| `EPICS_MCP_OLOG_URL` | _(empty)_ | Phoebus Olog REST root (incl. context path, e.g. `.../Olog`). Enables the seven read tools (`search_logbook`, `get_log_entry`, `list_logbooks`, `list_tags`, `list_log_levels`, `list_log_attachments`, `download_log_attachment`) and, together with the gate below, the four write tools. **No built-in host, so no egress unless set** |
| `EPICS_MCP_OLOG_AUTH` | _(empty)_ | Optional `Authorization` header for a secured Olog (READ only) |
| `EPICS_MCP_ALLOW_OLOG_WRITE` | `false` | Master gate for `create_log_entry` / `reply_to_log` / `add_log_attachment` / `update_log_entry` (separate from `ALLOW_PV_WRITE`) |
| `EPICS_MCP_OLOG_WRITE_USER` | _(empty)_ | Basic-auth service account for Olog writes (a dedicated account, never a personal login, because it becomes the record `owner`) |
| `EPICS_MCP_OLOG_WRITE_PASSWORD` | _(empty)_ | Basic-auth password for the write service account |
| `EPICS_MCP_OLOG_WRITE_LOGBOOKS` | _(empty)_ | Comma-separated logbook names a write may target; **empty + gate on = deny-all** |
| `EPICS_MCP_OLOG_WRITE_RATE_LIMIT` | `5` | Max Olog writes per 60 s window |
| `EPICS_MCP_OLOG_WRITE_URL_ALLOWLIST` | _(empty)_ | Comma-separated exact base URLs allowed as non-loopback write targets (only with `_ALLOW_REMOTE`) |
| `EPICS_MCP_OLOG_WRITE_ALLOW_REMOTE` | `false` | Permit writes to a non-loopback (allowlisted) Olog. Default false: only loopback is writable. A remote **must** be `https://`; a plain-http remote is refused (Basic creds are cleartext). The write session is env-independent (no proxy/`REQUESTS_CA_BUNDLE` env): give a remote's CA via `EPICS_MCP_CA_BUNDLE` |
| `EPICS_MCP_OLOG_ATTACH_MAX_BYTES` | `52428800` | Client-side cap on total upload bytes (checked before files are read; the server enforces its own 413) |

## EPICS network (standard EPICS env; controls what the server can reach)

⚠️ **This server sets no default here.** Every variable below is unset unless the launcher sets
it, and an unset `*_AUTO_ADDR_LIST` means EPICS broadcasts PV searches into the local subnets.
A genuinely localhost-isolated instance needs every address list unset **and** both auto-address
searches explicitly disabled. Run `epics-doctor` to see what your instance actually reaches.
⚠️ Read the right line for the right question. The live plane's posture line judges the ACTIVE
provider's auto-address switch only, so it can say `localhost-isolated` while the other provider's
switch is still open. The `Write gates` block judges BOTH, because the PV write gate does, and its
reach line is the one that predicts whether a write-enabled server would start.

| Variable | Default | Description |
|----------|---------|-------------|
| `EPICS_PVA_ADDR_LIST` | _(unset)_ | PVAccess address list; set it to reach real IOCs |
| `EPICS_PVA_AUTO_ADDR_LIST` | `YES` (EPICS default when unset) | Subnet broadcast for PVAccess. Set `NO` to disable |
| `EPICS_PVA_NAME_SERVERS` | _(unset)_ | PVAccess name servers (TCP unicast, **not** subnet-bound) |
| `EPICS_CA_ADDR_LIST` | _(unset)_ | Channel Access address list |
| `EPICS_CA_AUTO_ADDR_LIST` | `YES` (EPICS default when unset) | Subnet broadcast for Channel Access. Set `NO` to disable |
| `EPICS_CA_NAME_SERVERS` | _(unset)_ | Channel Access name servers (TCP unicast, **not** subnet-bound) |

