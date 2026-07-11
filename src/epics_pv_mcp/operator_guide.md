# EPICS PV MCP — Operator Guide (`epics-pv://guide`)

The operational cookbook for this MCP server: the service landscape, the tricks that are not
obvious from the tool signatures, and the error signatures that tell you which tool to reach for.
A fresh session should read this once instead of re-deriving it.

This file is served verbatim as the `epics-pv://guide` MCP resource and shipped inside the package,
so it travels with the server — no external skill or project folder required. It is **the** home for
operational knowledge (see the Knowledge Persistence Policy in `CLAUDE.md`).

> **Facility-agnostic by rule.** Every example here uses synthetic placeholder names
> (`SIM:PS-01:Cur-RB`, `sim://ramp`, `http://archiver:17665`, `http://channelfinder:8080/ChannelFinder`).
> Never paste a real site hostname, device/PV name, or personal name into this file — a committed test
> enforces it, and this file ships to a public fork.

## Posture (read this first)

- **Read-only by default.** The only mutating tool, `set_pv_value`, is gated OFF: it needs
  `EPICS_MCP_ALLOW_PV_WRITE=true` **plus** a regex allowlist, a rate limit and an audit log.
- **Localhost-isolated by default.** The server opens no non-local connection until its launcher
  widens the EPICS address list (`EPICS_PVA_ADDR_LIST` / `EPICS_CA_ADDR_LIST` and the matching
  `*_AUTO_ADDR_LIST`). Until then it does not reach any production network.
- **REST planes are opt-in.** Each REST plane stays disabled until its `*_URL` env var is set; an
  empty URL means no client and no network call.

## The planes

For the canonical plane→source→tools table and the safety/network posture, see the README sections
**"The planes it sees"** and **"Safety & network posture"** — the single source of truth. In short:

- **Live PV access** (p4p PVAccess/Channel Access) — the *only* authority for connected/disconnected.
- **ChannelFinder** — which IOC/host serves a PV, plus its tags/properties. `EPICS_MCP_CHANNELFINDER_URL`.
- **Archiver Appliance** — is a PV archived, how, and its history. `EPICS_MCP_ARCHIVER_URL`
  (MGMT root, `/mgmt/bpl`) + optionally `EPICS_MCP_ARCHIVER_RETRIEVAL_URL` (retrieval root, `/retrieval/data`).
- **Phoebus Alarm Logger** — is a PV alarm-configured, and its alarm history. `EPICS_MCP_ALARM_URL`.
- **Naming Service** — is a device name registered + ACTIVE. `EPICS_MCP_NAMING_URL`.
- **Phoebus Olog** — logbook search, author/free-text redacted. `EPICS_MCP_OLOG_URL`.

## Tool palette

The core tools always register. The four **display-aware** tools register only when the optional
`opi_navigation` engine is installed (`pip install epics-pv-mcp[displays]`); on a core-only install they
are simply absent — that is an unmet optional extra, not a bug.

<!-- BEGIN:tool-inventory (drift-guarded against @mcp.tool registrations — see tests/test_guide_matches_code.py) -->
**Core — live PV + REST planes:**
`get_pv_value` · `get_pvs` · `set_pv_value` · `get_pv_info` · `monitor_pv` · `discover_pvs` ·
`find_channels` · `lookup_device_name` · `is_archived` · `get_pv_history` · `get_archive_info` ·
`is_alarm_configured` · `get_alarm_history` · `diagnose_connection` · `search_logbook` · `get_log_entry`

**Optional `[displays]` — cross-plane with the operator-screen PV inventory:**
`validate_pvs` · `crossplane_check` · `coverage_audit` · `find_device`
<!-- END:tool-inventory -->

Composing the display tools: `find_device` (which screens show device X + live value + serving IOC),
`coverage_audit` (which delivered PV has no screen/archive/alarm — the blind spots).

## Operational recipes (the non-obvious parts)

### List archived PVs — there is no tool; use the MGMT API directly
No tool *enumerates* archived PVs — `is_archived`/`get_pv_history`/`get_archive_info` all require a PV
name. To LIST what an appliance archives, call its MGMT API: `getAllPVs` (whole appliance) or
`getPVsForThisAppliance` (this member). Do **not** use `getMatchingPVs` — it is not exposed on all
deployments and returns HTTP 404. Cluster shape: `getApplianceInfo` / `getAppliancesInCluster`.

### Retrieval-cluster-aware appliances
An Archiver Appliance may run as an **N-member failover cluster**. Such a cluster is
retrieval-aware: a MGMT/retrieval query to **one** member transparently returns data physically owned by
**another** member — the response's `appliance` field names the true owner. So a single
`EPICS_MCP_ARCHIVER_URL` covers the whole cluster; you do **not** need to route per member. Only
**separate, non-clustered** appliances (or several disjoint clusters) need a distinct
`EPICS_MCP_ARCHIVER_RETRIEVAL_URL` or per-appliance handling.

### CA-bundle: combine the trust anchors
When the REST planes present **different** trust roots — e.g. one internal service signed by a private
facility CA and another signed by a **public** CA — a single-root bundle fails one of them. Fix:
concatenate the internal CA PEM **with** the public roots (certifi's `cacert.pem`) into **one** combined
PEM and point `EPICS_MCP_CA_BUNDLE` at it. The session pins `trust_env=False` whenever a CA path is set,
so a stray `REQUESTS_CA_BUNDLE` cannot silently override it. (`EPICS_MCP_TLS_VERIFY=false` disables
verification entirely — internal-network escape hatch only, never the default.)

### Read record fields directly (`.FIELD`)
Any channel name works, so append a field suffix to read record metadata without a dedicated tool:
`get_pv_info("SIM:PS-01:Cur-RB.RTYP")`, `.SCAN`, `.HIHI`, `.HOPR`. This is the cheap path to alarm
thresholds when an NT `valueAlarm` comes back NaN over a PVA gateway. A cold first connect can be slow —
pass `timeout` ≥ 8 for the first probe.

### Naming URL: no trailing `/rest`
Set `EPICS_MCP_NAMING_URL` to the service root **without** `/rest` and without a trailing slash. The
client appends `/rest/parts/…` and `/rest/deviceNames/…` itself; a trailing slash is normalized, but
appending `/rest` yourself yields `/rest/rest/…` → 404.

## Error signatures → which tool answers

Illustrate signatures by the exception **class and shape**, never a copied runtime string (error
messages embed the full request URL — an internal host would leak into this file).

- **PV times out / disconnected.** On a PVA name-server a typo **and** a dead IOC both surface as
  `PV_TIMEOUT` (never `PV_NOT_FOUND`) — the cause cannot be read off the transport error code. Use
  `diagnose_connection`: the live probe is the sole authority for connected/disconnected; ChannelFinder
  and Naming only *explain* it (they never flip the verdict; `withheld ≠ no`).
- **"not found" vs "not registered".** `lookup_device_name`: a real HTTP 404 on the device endpoint is a
  **definitive** `registered:false`; a service/transport/TLS failure is **withheld** (`registered:null`),
  never a false negative.
- **Archived? how? history?** `is_archived` / `get_archive_info` / `get_pv_history`. History `status` is
  `ok` / `empty` / `withheld` — a bare `[]` means only a truly empty history, not "could not read".
- **Alarm configured / history?** `is_alarm_configured` (a miss is a true negative only if the logger ran
  at config-import time, else withheld) / `get_alarm_history` (`start` + `end` required).
- **Which IOC/host serves a PV?** `find_channels`.
- **A REST 404 = `found:false`** only where documented (`getPVTypeInfo`, Olog `get_log_entry`); any other
  error propagates — could-not-read is never silently "not there".

## Acceptance — the questions this guide must answer

A fresh session with only this guide should now handle the four things a live smoke had to re-derive:

1. **Enumerate archived PVs** → `getAllPVs` / `getPVsForThisAppliance`, not `getMatchingPVs` (404).
2. **A clustered archiver** → one `EPICS_MCP_ARCHIVER_URL` covers all members (retrieval-aware; the
   `appliance` field names the owner; MGMT `:17665` / retrieval `:17668` are separate ports/webapps).
3. **TLS fails on one plane but not another** → combine internal CA + public roots (certifi) into one
   `EPICS_MCP_CA_BUNDLE` PEM.
4. **A service presents a public certificate** → the combined CA bundle already covers it; no separate config.

---

*Found a new service/operational/IOC fact? Add it here — this file is the operational-knowledge tier of
the Knowledge Persistence Policy (`CLAUDE.md`), not a session transcript. Keep it facility-agnostic.*
