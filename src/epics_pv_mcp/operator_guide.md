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

- **Read-only by default.** The mutating tools are gated OFF. `set_pv_value` needs
  `EPICS_MCP_ALLOW_PV_WRITE=true` **plus** a regex allowlist, a rate limit and an audit log. The Olog
  logbook writers (`create_log_entry` / `reply_to_log`) sit behind a **separate** gate — see the Olog
  write posture below; `ALLOW_PV_WRITE` is untouched by it.
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
- **Phoebus Olog** — logbook search; author dropped, free text withheld unless a DECLARED local
  sandbox (see "Olog output posture"). `EPICS_MCP_OLOG_URL`.

## Tool palette

The core tools always register. The four **display-aware** tools register only when the optional
`opi_navigation` engine is installed (`pip install epics-pv-mcp[displays]`); on a core-only install they
are simply absent — that is an unmet optional extra, not a bug.

<!-- BEGIN:tool-inventory (drift-guarded against @mcp.tool registrations — see tests/test_guide_matches_code.py) -->
**Core — live PV + REST planes:**
`get_pv_value` · `get_pvs` · `set_pv_value` · `get_pv_info` · `monitor_pv` · `discover_pvs` ·
`find_channels` · `lookup_device_name` · `is_archived` · `get_pv_history` · `get_archive_info` ·
`list_archived_pvs` · `is_alarm_configured` · `get_alarm_history` · `diagnose_connection` ·
`search_logbook` · `get_log_entry` · `list_logbooks` · `list_tags` · `create_log_entry` · `reply_to_log`

**Optional `[displays]` — cross-plane with the operator-screen PV inventory:**
`validate_pvs` · `crossplane_check` · `coverage_audit` · `find_device`
<!-- END:tool-inventory -->

Composing the display tools: `find_device` (which screens show device X + live value + serving IOC),
`coverage_audit` (which delivered PV has no screen/archive/alarm — the blind spots).

### Olog output posture (what a read gives you back) — ESS-SPEC PENDING

Entries come back redacted — author dropped, `title`/`description` free text withheld, attachments
as a count, `logbooks`/`tags` as name-only lists — UNLESS **both** of these hold:

1. `EPICS_MCP_OLOG_URL` is **loopback** (`localhost` / `127.0.0.0/8` / `::1`), and
2. `EPICS_MCP_OLOG_ASSUME_TEST_DATA=true` **declares** the data synthetic.

Then the **whole** entry is returned, free text and owner included.

The shape does not change between the two: the full mode only ADDS fields, so `attachment_count` and
the name-only `logbooks`/`tags` are there either way.

**Why.** Withholding the free text costs a logbook its entire point — a search returns ids whose
content you cannot judge, and a write cannot verify what it just wrote (you would need a separate
client to check). The withholding rule was written against an *assumed* privacy policy; none was
ever specified for this server. So it is **deferred for local test data until a real specification
exists**, then re-applied — the allowlist and the projection stay intact meanwhile, guarding every
real server. Only the default moved.

**Why two conditions.** A loopback ADDRESS does not prove the DATA is synthetic: `ssh -L
8080:olog-prod:8080` or a port-forward serves a production logbook on localhost with the URL
unchanged (demonstrated live, QA 2026-07-15) — no URL inspection can see through a tunnel, so only
a person can assert what the data is. Conversely the declaration alone would not catch "pointed it
at the facility and forgot", which is what the loopback condition still catches. For the same
reason the client REFUSES redirects rather than follow them: a hop moves the data's true origin
without changing the URL the decision came from. And note the write gate's own URL check must NOT
be reused as the read predicate: it also passes an *allowlisted remote*, which would surface a
production logbook in the clear.

**Diagnosis:** `epics-doctor` prints the effective posture (`Olog free-text: withheld` vs `FULL
(declared local test data — ESS-spec pending)`). This is a *runtime output* policy and has nothing to do
with keeping person data out of committed files — that is a separate, unaffected guard.

### Olog write posture (`create_log_entry` / `reply_to_log`)

Posting to the logbook is the server's first mutating operation against a REST service, so it has its
own gate — distinct from `set_pv_value`, and it never touches `ALLOW_PV_WRITE`. Four things must line
up before a write proceeds, each fail-closed and audited as `DENY` before the raise:

- **Env gate.** `EPICS_MCP_ALLOW_OLOG_WRITE=true` (default false = every write denied).
- **Test-server URL boundary.** Unlike PV write (implicitly safe via the address-list localhost
  isolation), Olog speaks HTTP to an arbitrary URL. A write is refused unless the `OLOG_URL` host is
  **loopback** (the local Olog), or the exact base URL is in `EPICS_MCP_OLOG_WRITE_URL_ALLOWLIST`
  **and** `EPICS_MCP_OLOG_WRITE_ALLOW_REMOTE=true`. A private (non-loopback) host is refused by default
  — a production write is a deliberate, auditable double action. The host is read only from the URL's
  parsed hostname, so a `user@host` trick cannot smuggle a loopback prefix past a production host.
- **Logbook allowlist.** Every target logbook must be in `EPICS_MCP_OLOG_WRITE_LOGBOOKS`; an EMPTY
  allowlist with the gate on is **deny-all** (the inverse of the PV pattern).
- **Rate limit + audit.** `EPICS_MCP_OLOG_WRITE_RATE_LIMIT` (low; a logbook is human-paced). The audit
  line is metadata-only — logbook names, level, title LENGTH, entry id, service-account owner — and
  **never** the `title`/`description` free text.

The author (`owner`) is the write service account (`EPICS_MCP_OLOG_WRITE_USER`, a dedicated account —
never a personal login), set server-side from the auth Principal; a caller cannot spoof it. Use a
loopback Olog for testing; the target logbooks must already exist server-side (a non-existent logbook
is an HTTP 400).

## Operational recipes (the non-obvious parts)

### List archived PVs — `list_archived_pvs` (or the MGMT API directly)
`list_archived_pvs` enumerates the archived PV names — the sibling `is_archived`/`get_pv_history`/
`get_archive_info` tools each require a PV name. Under the hood it calls the MGMT `getAllPVs` (whole
appliance) or, with `this_appliance=true`, `getPVsForThisAppliance` (this member); `capped` is honest
(over-fetch by one). It deliberately does **not** use `getMatchingPVs` — a standard endpoint too, but
one that **may 404 on proxied/split deployments**. Cluster shape: `getApplianceInfo` /
`getAppliancesInCluster`.

**The name filter works on ONE of those two endpoints.** `getAllPVs` honours a `pv` glob.
`getPVsForThisAppliance` has **no name filter at all** — measured, it ignores `pv`, `regex`, `pattern`
and `name` alike and answers byte-identically to the unfiltered call. It does not fail; it hands back
a full, plausible list of the **wrong** PVs, and an honest `capped` makes that read like a fair
truncation. Going at the MGMT API directly, filter only via `getAllPVs`. `list_archived_pvs` refuses
the combination (`INVALID_ARGUMENT`) rather than answer it wrongly; client-side filtering is not a
substitute, because `limit` is applied server-side — filtering a capped page yields an arbitrary
subset behind a meaningless `capped`.

### Verify a deployment's config — `epics-doctor`
The `epics-doctor` CLI (core install) is a read-only self-check: it probes every CONFIGURED plane
(a transport probe, refined on success by an identity probe — up to two requests for a healthy
plane; retries on a 5xx add more) and
reports, per plane, whether it is reachable, whether the CA bundle works, whether the
service **identifies itself as the one that URL should point at**, and what the ChannelFinder
redaction is set to. A disabled plane (empty `*_URL`) is reported honestly, never a failure. Exit
`0` = no configured plane failed, `1` = a configured plane failed (`unreachable` / `ca_error` /
`api_error` / `wrong_service` / `config_error` / probe-disconnect), `2` = a usage error. Run it first in a new
facility to confirm the `.env`; add `--probe-pv NAME` to also pass/fail the live PVA plane against a
real PV. Full deployment/config guide: `docs/deployment.md`.

**Reachable is not identified — read the `?` lines.** The transport probe is a HEAD and counts *any*
HTTP response as reachable, so a URL pointing at the wrong host can look alive: measured, a
ChannelFinder URL aimed at a dead container reported `✓ ok` because an unrelated service on that
port answered `401` (blanket auth answers 401 for every path, so the status said nothing about
ChannelFinder). Each plane is therefore also asked to **name itself**:

| Mark | Status | Meaning |
|---|---|---|
| `✓` | `ok` | the service named itself correctly — confirmed |
| `?` | `unverified` | reachable, but it could not prove what it is (auth wall, no info endpoint, unreadable body). **Honest, not healthy** — exit stays `0` |
| `✗` | `wrong_service` | it named itself as a *different* known service — that URL points at the wrong one. Exit `1` |
| `✗` | `config_error` | the configuration is self-contradictory, no probe is even attempted (e.g. `EPICS_MCP_ARCHIVER_RETRIEVAL_URL` set while `EPICS_MCP_ARCHIVER_URL` is empty — every archiver tool gates on the latter, so that retrieval URL is never used). Exit `1` |

`unverified` is not a failure on purpose: that a healthy service answers its beacon anonymously is
measured at one site, and making that a hard failure everywhere would be an overclaim.

Each plane has its own beacon, because they do not share one:

| Plane | Beacon | Identified by |
|---|---|---|
| ChannelFinder / Olog / Alarm | `GET <base-url>` | exact `name` ("ChannelFinder Service", …) |
| Archiver (MGMT) | `GET /mgmt/bpl/getApplianceInfo` | the `identity` field |
| Archiver (retrieval) | `GET /retrieval/bpl/getVersion` | the product name in `version` |
| Naming | `GET /rest/swagger.json` | `info.title` |

⚠️ Mind the base PATH on the archiver: retrieval serves `/retrieval/bpl`, **not** `/mgmt/bpl`.
Probing the latter on the retrieval port 404s and tells you nothing — that mistake is exactly how an
earlier pass concluded retrieval had no beacon at all.

Scripting against this? Exit `0` means "nothing failed", **not** "everything confirmed" — read
`verification_complete` / `unverified_planes` from `--json`, never the exit code alone. And for
**positive** confirmation assert `identified_planes` is non-empty: `verification_complete` is
vacuously true on an empty config, so it alone cannot tell "all confirmed" from "nothing ran".

### Retrieval-cluster-aware appliances
An Archiver Appliance may run as an **N-member failover cluster**. Such a cluster is retrieval-aware: a
MGMT/retrieval query to **one** member transparently returns data physically owned by **another** member
— the response's `appliance` field names the true owner. So one `EPICS_MCP_ARCHIVER_URL` covers the whole
cluster and you do **not** route per member.

This is **orthogonal** to the MGMT/retrieval **port split**. When the mgmt webapp (`:17665`, `/mgmt/bpl`)
and the retrieval webapp (`:17668`, `/retrieval/data`) run on different ports/hosts — common on a split
deployment, **clustered or not** — you **must** set `EPICS_MCP_ARCHIVER_RETRIEVAL_URL` to the retrieval
one, or `get_pv_history` falls back to the mgmt URL and 404s. Clustering removes per-member routing; it
does **not** remove the port split. Only genuinely separate, non-clustered appliances (or several
disjoint clusters) need per-appliance handling beyond that.

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

### Archiver history time window: ISO-with-zone only — and a 500 does not mean unreachable
Three planes, three different time grammars — this is the narrowest, and the only one that is
strict. Measured: `2026-07-08T00:00:00.000Z` works; a naive `2026-07-08T00:00:00`, the
space-separated wall clock Olog *requires*, a bare date, and the `7 days` amount the alarm/logbook
searches take are each an **HTTP 500**; an inverted window is a 400.

That strictness is a virtue — nothing here is silently wrong. The trap is the diagnosis: a served
5xx is not an outage, and a caller carrying `7 days` over from `get_alarm_history` gets one. Going
at the retrieval API directly, always send zone-explicit ISO. `get_pv_history` normalizes every
absolute notation for you and refuses a relative amount by name.

### Alarm history time window: ISO without a zone is read as "now" (and will not tell you)
The Alarm Logger uses the REAL Phoebus parser (no vendored copy), so it reads ISO **with** a zone —
`2026-07-08T12:45:58Z` and `+02:00` both work, and a bare date works too. But a zone-LESS
`2026-07-08T12:45:58` matches no parser and falls into the same silent zero-offset path as Olog's:
the window becomes `[now, now]` and the answer is **HTTP 200 with an empty list**. Measured live:
the same 7-day window returned 20+ events with the `Z` and 0 without it. A window that wide cannot
be emptied by a mere zone shift — it is the collapse.

This matters more than it looks: `datetime.now().isoformat()` emits exactly the zone-less form, so
it is the most likely wrong value a caller will ever produce. `500 millis` is worse still — it
RETURNS data, for a 500-**minute** window. And the server's own range check is an empty `if` branch
carrying a `// TODO check that the start is before the end`: an inverted window is logged
server-side and queried anyway.

`get_alarm_history` normalizes for you (absolute → `yyyy-MM-ddTHH:mm:ss.SSSZ`; amounts pass
through; anything unclassifiable is refused before the request). Going at the REST API directly,
always send the zone.

### Olog search time window: the server cannot read ISO-8601 (and will not tell you)
Olog parses `start`/`end` with a **vendored, stripped** copy of Phoebus' `TimestampFormats` that has
no ISO support — upstream's own copy has it; the fork deleted it. Only a **space-separated** wall
clock (`yyyy-MM-dd HH:mm:ss.SSS` and shorter) or a **single relative amount** (`7 days`, `90 min`,
`now`) parses.

The trap is the failure mode: an unreadable value is **not** an error. Olog falls back to its
relative-amount parser, which scans for a unit token, finds none in an ISO string, and returns a
**zero** offset — which it subtracts from *now*. The window collapses to `[now, now+µs]` and matches
nothing, so the answer is **HTTP 200 with an empty list**: indistinguishable from "nothing matched".
Same window, same data, different format → 0 results vs. all of them.

`search_logbook` normalizes for you (absolute → UTC wall clock plus `tz=UTC`; amounts pass through;
anything unclassifiable is refused **before** the request). Going at the REST API directly, do it
yourself. Also note: **months/years are unusable** (the amount is subtracted from a point in time,
which cannot carry them → rejected), and `millis` means **minutes** to Olog's unit dispatch (use
`ms`). Always send `tz` with a wall clock — without it Olog reads the string in the **server's**
default zone, so a window is silently offset against any deployment not on UTC.

**Generalisable:** a fully-mocked test suite cannot detect a server-side silent drop — a mock only
sees what the client *sent*, never what the server *honoured*. Only a differential live probe
(same window, several formats, plus a negative control) can.

The converse hides just as well: a mock cannot see a **loud** server-side rejection either. The
Archiver 500s on four notations a caller may reasonably send, and the offline suite was green
throughout — it passed `"a"`/`"b"` as the window, values no layer ever looked at. A parameter that
nothing validates and nothing probes live is *untested*, however many tests name it. Three planes
were checked this way; **three** carried a defect no mock could reach.

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
- **Alarm configured / history?** `is_alarm_configured` (`configured` is `true`/`false`/`null`;
  `null` = withheld, see the config-tree recipe below) / `get_alarm_history` (`start` + `end`
  required; `pv` is matched as a wildcard SUBSTRING of the config path — `Value` matches both
  `...:Temp1Value` and `...:12VValue`). A `false` is a true negative only if the Alarm Logger was
  running at config-import time — otherwise treat it as unreliable; the tool cannot flag this.
- **An Olog search answers 401.** On an anonymous read that is almost never a credentials problem:
  Olog's error dispatch requires authentication, so it returns **401 in place of its own 400** —
  every server-side rejection looks like "unauthorized". Check the query (the time window first);
  a deployment configured with read credentials sees the real 400 and its message.
- **An Olog search answers 200 with an empty list.** Not necessarily "nothing matched" — an
  unreadable `start`/`end` degrades to *now* server-side and matches nothing (see the recipe
  above). Re-run with a relative amount (`7 days`) to tell a real empty from a dead window.
- **An Archiver call answers `ARCHIVER_HTTP_5xx` / `INVALID_TIME_WINDOW` / `INVALID_ARGUMENT`.**
  None of these means the appliance is down — it answered, or the call never left. A 5xx on
  `get_pv_history` is nearly always the time format (this plane reads zone-explicit ISO and nothing
  else); `INVALID_TIME_WINDOW` is a window the plane cannot express (e.g. a relative amount);
  `INVALID_ARGUMENT` is an argument combination that cannot be answered at all. Only
  `EPICS_CONNECTION_FAILED` means unreachable.
- **Which IOC/host serves a PV?** `find_channels`.
- **`is_alarm_configured` answers `configured:null`.** The alarm tree itself returned nothing, so
  "this PV is not configured" cannot be told apart from "that is not the tree name" — the answer is
  withheld, and a `note` says so. The tree name is **case-sensitive** in the query even though the
  server lower-cases it to pick the index, so `accelerator` selects the right index and matches
  nothing. Re-run with the tree spelled as the logger stores it (`Accelerator`). The `config` field
  in the response echoes your input — it is not the server confirming the tree.
- **A ChannelFinder glob finds nothing / finds odd casing.** The glob is **anchored**: a bare
  substring (`Ctrl-EVR-01`) matches 0 — wrap it in stars (`*Ctrl-EVR-01*`). And it is
  **case-insensitive**: `*Temp*` legitimately matches `...MorTemPrd`, so a hit may differ in case
  from what you asked.
- **A REST 404 = `found:false`** only where documented (`getPVTypeInfo`, Olog `get_log_entry`); any other
  error propagates — could-not-read is never silently "not there".

## Acceptance — the questions this guide must answer

A fresh session with only this guide should now handle the four things a live smoke had to re-derive:

1. **Enumerate archived PVs** → `list_archived_pvs` (uses `getAllPVs` / `getPVsForThisAppliance`, not
   `getMatchingPVs`, which may 404 on split/proxied deployments).
2. **A clustered archiver** → one `EPICS_MCP_ARCHIVER_URL` covers all members (retrieval-aware; the
   `appliance` field names the owner). The mgmt/retrieval port split is orthogonal: if retrieval runs on
   `:17668`, still set `EPICS_MCP_ARCHIVER_RETRIEVAL_URL` — clustering does not remove the port split.
3. **TLS fails on one plane but not another** → combine internal CA + public roots (certifi) into one
   `EPICS_MCP_CA_BUNDLE` PEM.
4. **A service presents a public certificate** → the combined CA bundle already covers it; no separate config.

---

*Found a new service/operational/IOC fact? Add it here — this file is the operational-knowledge tier of
the Knowledge Persistence Policy (`CLAUDE.md`), not a session transcript. Keep it facility-agnostic.*
