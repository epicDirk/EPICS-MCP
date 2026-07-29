# EPICS MCP: Operator Guide (`epics-pv://guide`)

The operational cookbook for this MCP server: the service landscape, the tricks that are not
obvious from the tool signatures, and the error signatures that tell you which tool to reach for.
A fresh session should read this once instead of re-deriving it.

This file is served verbatim as the `epics-pv://guide` MCP resource and shipped inside the package,
so it travels with the server, no external skill or project folder required. It is **the** home for
operational knowledge (see the Knowledge Persistence Policy in `CLAUDE.md`).

> **Facility-agnostic by rule.** Every example here uses synthetic placeholder names
> (`SIM:PS-01:Cur-RB`, `sim://ramp`, `http://archiver:17665`, `http://channelfinder:8080/ChannelFinder`).
> Never paste a real site hostname, device/PV name, or personal name into this file, a committed test
> enforces it, and this file ships to a public fork.

## Posture (read this first)

- **Read-only by default.** The mutating tools are gated OFF. `set_pv_value` needs
  `EPICS_MCP_ALLOW_PV_WRITE=true` **plus** a regex allowlist, a rate limit and an audit log. The Olog
  logbook writers (`create_log_entry`, `reply_to_log`, `add_log_attachment` and `update_log_entry`) sit behind a **separate** gate, see the Olog
  write posture below; `ALLOW_PV_WRITE` is untouched by it.
- **Writes enabled ⇒ loopback-only search reach, enforced at boot.** With `ALLOW_PV_WRITE=true` the
  server refuses to start (`SafetyConfigError`) unless the EPICS client search env is loopback-only:
  every `EPICS_PVA/CA_ADDR_LIST` / `_NAME_SERVERS` token a loopback host AND both `*_AUTO_ADDR_LIST`
  parser-faithfully disabled (`NO`; unset means ON). If a write-enabled process won't boot, the
  message names the offending var, the fix is to loopback-scope the reach or disable writes, NEVER
  to weaken the assert (a mis-scoped allowlist over a facility-reaching env is the exact footgun this
  removes). The name-pattern scopes *what*, this scopes *where*.
- **Network reach is the launcher's decision, do NOT assume isolation.** PV searches follow the
  standard EPICS env: the address lists (`EPICS_PVA_ADDR_LIST` / `EPICS_CA_ADDR_LIST`), the name
  servers (`EPICS_PVA_NAME_SERVERS`; TCP unicast, **not** subnet-bound), and the auto-address
  search (`*_AUTO_ADDR_LIST`), which EPICS defaults to **ON**: even a null environment broadcasts
  PV searches into the local subnets. Run `epics-doctor` for the effective posture; it claims
  `localhost-isolated` only when every search list is unset AND the auto search is explicitly
  disabled.
- **REST planes are opt-in.** Each REST plane stays disabled until its `*_URL` env var is set; an
  empty URL means no client and no network call.
- **Long monitors run in a dedicated pool.** `monitor_pv` holds a worker thread for up to
  `EPICS_MCP_MAX_MONITOR_DURATION` (60 s). Those blocking subscriptions run on a DEDICATED executor
  sized by `EPICS_MCP_MONITOR_MAX_CONCURRENCY` (default 8), NOT the shared pool behind every other
  read/write, so a burst of monitors cannot starve the rest of the server into an apparent hang.
  Raise it only if operators legitimately need more simultaneous long monitors; the ceiling bounds
  monitors alone.
- **Reads can be throttled (opt-in).** `EPICS_MCP_READ_RATE_LIMIT` (default 0 = off) caps REST reads
  per 60 s at the shared GET chokepoint; over the limit a read is DENIED
  (`ReadRateLimitError` → `READ_RATE_LIMIT_EXCEEDED`, its own code, never the write gates'
  `RATE_LIMIT_EXCEEDED`, because this refusal writes no audit line and is not a gate verdict), never
  blocked, a blocking wait there would hold a worker thread and reintroduce the monitor starvation
  above. Turn it on to protect a production facility from an unthrottled read burst. It bounds only
  the REST planes (ChannelFinder/Archiver/Alarm/Naming/Olog), not live p4p PV reads. Two caveats:
  the ChannelFinder/Alarm/Olog/Naming reachability self-checks are HTTP HEADs and bypass the
  throttle entirely, but the ARCHIVER's is not, it is a real GET of `/mgmt/bpl/getApplianceInfo`
  (it needs a 2xx to tell "reachable" from "wrong endpoint"), so it spends a token like any other
  read and `epics-doctor` can be denied on a tight limit; and a multi-GET tool like
  `coverage_audit` spends ~2 tokens per PV, so set the limit ABOVE that fan-out or the tool aborts
  mid-run with a loud `READ_RATE_LIMIT_EXCEEDED` (never a silent partial result).

## The planes

For the canonical plane→source→tools table and the safety/network posture, see the README sections
**"The planes it sees"** and **"Safety & network posture"**, the single source of truth. In short:

- **Live PV access** (p4p PVAccess/Channel Access), the *only* authority for connected/disconnected.
- **ChannelFinder**: which IOC/host serves a PV, plus its tags/properties. `EPICS_MCP_CHANNELFINDER_URL`.
- **Archiver Appliance**: is a PV archived, how, and its history. `EPICS_MCP_ARCHIVER_URL`
  (MGMT root, `/mgmt/bpl`) + optionally `EPICS_MCP_ARCHIVER_RETRIEVAL_URL` (retrieval root, `/retrieval/data`).
- **Phoebus Alarm Logger**: is a PV alarm-configured, and its alarm history. `EPICS_MCP_ALARM_URL`.
- **Naming Service**: is a device name registered + ACTIVE. `EPICS_MCP_NAMING_URL`.
- **Phoebus Olog**: logbook search; author dropped, free text withheld unless a DECLARED local
  sandbox (see "Olog output posture"). `EPICS_MCP_OLOG_URL`.

## Tool palette

The core tools always register. The four **display-aware** tools register only when the optional
`opi_navigation` engine is installed (the `displays` dependency group, a local-checkout
surface that never reaches the published package); on a core-only install they
are simply absent, that is an unmet optional dependency group, not a bug.

<!-- BEGIN:tool-inventory (drift-guarded against @mcp.tool registrations, see tests/test_guide_matches_code.py) -->
**Core, live PV + REST planes:**
`get_pv_value` · `get_pvs` · `set_pv_value` · `get_pv_info` · `monitor_pv` · `discover_pvs` ·
`find_channels` · `list_channel_vocabulary` · `lookup_device_name` · `is_archived` · `get_pv_history` · `get_archive_info` ·
`get_appliance_info` · `list_archived_pvs` · `is_alarm_configured` · `get_alarm_history` · `diagnose_connection` ·
`search_logbook` · `get_log_entry` · `list_logbooks` · `list_tags` · `list_log_levels` ·
`create_log_entry` · `reply_to_log` · `update_log_entry` · `add_log_attachment` ·
`list_log_attachments` · `download_log_attachment`

**Optional `displays` dependency group, cross-plane with the operator-screen PV inventory:**
`validate_pvs` · `crossplane_check` · `coverage_audit` · `find_device`
<!-- END:tool-inventory -->

Composing the display tools: `find_device` (which screens show device X + live value + serving IOC),
`coverage_audit` (which delivered PV has no screen/archive/alarm, the blind spots).

### Olog output posture (what a read gives you back), ESS-SPEC PENDING

Entries come back redacted: author dropped, `title`/`description` free text withheld, attachments
as a count, `logbooks`/`tags` as name-only lists, UNLESS **both** of these hold:

1. `EPICS_MCP_OLOG_URL` is **loopback** (`localhost` / `127.0.0.0/8` / `::1`), and
2. `EPICS_MCP_OLOG_ASSUME_TEST_DATA=true` **declares** the data synthetic.

Then the **whole** entry is returned, free text and owner included.

The shape does not change between the two: the full mode only ADDS fields, so `attachment_count` and
the name-only `logbooks`/`tags` are there either way.

**Why.** Withholding the free text costs a logbook its entire point, a search returns ids whose
content you cannot judge, and a write cannot verify what it just wrote (you would need a separate
client to check). The withholding rule was written against an *assumed* privacy policy; none was
ever specified for this server. So it is **deferred for local test data until a real specification
exists**, then re-applied, the allowlist and the projection stay intact meanwhile, guarding every
real server. Only the default moved.

**Why two conditions.** A loopback ADDRESS does not prove the DATA is synthetic: `ssh -L
8080:olog-prod:8080` or a port-forward serves a production logbook on localhost with the URL
unchanged (demonstrated live, QA 2026-07-15), no URL inspection can see through a tunnel, so only
a person can assert what the data is. Conversely the declaration alone would not catch "pointed it
at the facility and forgot", which is what the loopback condition still catches. For the same
reason the client REFUSES redirects rather than follow them: a hop moves the data's true origin
without changing the URL the decision came from. And note the write gate's own URL check must NOT
be reused as the read predicate: it also passes an *allowlisted remote*, which would surface a
production logbook in the clear.

**Diagnosis:** `epics-doctor` prints the effective posture (`Olog free-text: withheld` vs `FULL
(declared local test data, ESS-spec pending)`). This is a *runtime output* policy and has nothing to do
with keeping person data out of committed files, that is a separate, unaffected guard.

### Olog search filters: what the server does with a value it does not like

**The signature to recognise: Olog does not reject a bad search parameter, it ignores it.** Its
parameter switch ends in `default: // Unsupported search parameters are ignored`, so a misspelled
parameter NAME is dropped and the query comes back with the **unfiltered** set behind a 200. "The
search returned results" is therefore never evidence that a filter was applied, which is why every
filter promise here rests on a differential probe carrying a positive control, a negative control,
and an ignored-parameter control (`tests/test_olog_live.py`).

Measured behaviour of the filters, 2026-07-19:

| Value | `level` | `title` |
|---|---|---|
| known / matching | filters as named, case-insensitive | filters as named, case-insensitive |
| **unknown** | **200 + 0 hits, no error** | **200 + 0 hits, no error** |
| empty string `""` | matches **nothing** (0 hits) | **dropped** → unfiltered result |
| separators only (`","`, `"+"`) | **dropped** → unfiltered result | **dropped** → unfiltered result |
| several values | OR over `,` `;` `\|` | AND over whitespace-separated words |
| quoted `"a b"` | phrase (also for values with a space) | phrase, in order |
| wildcard `*` | **honoured** (`Inf*` matches `Info`) | **honoured** |

⚠️ The two "blank" rows are NOT the same mechanism, and both are refused client-side for that
reason. `""` survives Java's `split` as a single empty term and becomes a wildcard matching nothing;
a separators-only value leaves NO term, so the filter is dropped and the **unfiltered** set comes
back looking filtered, the worse of the two. And the separator classes DIFFER: `title` also splits
on whitespace **and on a literal `+`** (the Java class is `[\|,;\s+]`, where the trailing `+` is a
class member, not a quantifier), so `"+"` is a blank title but a perfectly ordinary level.

⚠️ `level` values are trimmed with Java's `trim()`, which removes only code points ≤ U+0020, NOT
NBSP or other Unicode spaces. `level=" Info"` therefore returns 0 hits while `level="Info"`
returns the entries. Anything comparing a level value against the name list must trim the same way,
or it will normalise an unmatchable value into a configured one and stay silent about it.

Three consequences worth carrying:

1. **An unknown level reads exactly like an empty logbook.** `level=Warnign` returns 0 hits, and
   nothing distinguishes that from "no entries are Warnings". Call **`list_log_levels`** for the
   valid values, levels are site-configurable, not a fixed enum. `search_logbook` annotates an
   empty level-filtered result when the value is not a configured level, so the 0 is not reported
   as a fact about the logbook.
2. **Blank is not "no filter", and the two fields disagree about what it means.** Both are refused
   client-side before the request (`INVALID_INPUT`), neither server answer is usable.
3. **`title` matches whole WORDS, not substrings**: the opposite of `find_channels`, whose bare
   value is an anchored substring glob. A word fragment finds nothing unless wildcarded (`att*`).
   `title` is also a separate axis from `text`/`desc`, which searches the **body** only and never
   the title.

Do **not** validate a level against `list_log_levels` before **searching**: a level can be deleted
while existing entries keep the string, so a pre-flight check would refuse a legitimate search of
history. The cross-check belongs where it is, on an empty result.

**Writing is the opposite case, and the rule does not carry over.** `create_log_entry`,
`reply_to_log` and `update_log_entry` DO validate a passed `level` against `list_log_levels`, and
refuse an unknown one with `INVALID_INPUT`. The asymmetry is deliberate: searching for a deleted
level is legitimate (the string still sits on old entries), but *writing* one is a typo that Olog
answers with HTTP 200 and that no filter ever finds again. A blank level is refused separately, the
server accepts it and silently clears the entry's triage level. The match is exact: no casefolding,
no trimming, no `,`/`;`/`|` splitting. Those are search semantics; a level being written is a scalar.

An unbalanced double quote in `title` makes the server throw, which an anonymous read sees as **401**
(Olog's error dispatch requires auth and so masks its own 400). On this read path a 401 almost always
means the QUERY was rejected, not that credentials are wrong.

**Honest limit of the free-text withholding (stated, not solved).** Against a redacted server the
`title`/`description` values are withheld, but a *search* still answers with a hit COUNT, so asking
`title=<word>` reveals whether that word occurs in some title even though no title is readable. This
is inherent to any search interface over withheld text and is **not new** with the `title` facet:
`text`/`desc` has offered the same oracle over the body since the first read tool. It is recorded
here because the redaction section otherwise reads as a stronger guarantee than it is. Closing it
would mean withholding counts too, which costs search its purpose, the withholding policy is
ESS-spec-pending anyway (see above), so this is a known, bounded gap, not an accepted design.

### PV write posture (`set_pv_value`): the audit trail

A `set_pv_value` write leaves a `PV_WRITE` audit line at each stage, correlated by an `op=<id>` token.
A write-enabled server **refuses to start** unless `EPICS_MCP_AUDIT_LOG_FILE` names a durable path, an
ephemeral stderr audit would lose this trail on restart. The stages, each `op`-correlated:

- `event=DENY`: the gate refused it (writes off / not in the allowlist / rate limit); nothing was sent.
- `event=BOUNDS_DENY`: the value gate (O2) refused it: the PV is in the allowlist but the written
  value is outside the record's own drive limits (`control` DRVL/DRVH). Refused **before** the put,
  so nothing reached the IOC; carries the value + limits. (A record with no declared limits is not
  bounds-checked and this line does not appear.)
- `event=ATTEMPT`: emitted **before** the I/O; a durable record that this write was dispatched.
- `event=ALLOW`: the put returned successfully.
- `event=FAILED error_code=...`: the put raised (timeout, connection, or a bug tagged `INTERNAL`).
- `event=UNKNOWN_PENDING`: **the important one.** The write coroutine was cancelled mid-put (client
  disconnect, `wait_for` timeout, task cancel). The blocking p4p put runs in a worker thread that a
  cancellation does **not** stop, so the value **may still reach the IOC** after the caller sees the
  cancel. **Operator action:** treat the value as *possibly written*, read the PV back to establish
  the real state; do **not** blindly retry (a retry can double-apply to hardware). This is why a cancel
  is never logged as `FAILED` (which would wrongly imply nothing was written).
- `event=READBACK_OK` / `event=READBACK_MISMATCH` / `event=READBACK_UNVERIFIED`, the readback verdict,
  emitted **after** `ALLOW`. The write is **always** read back and compared against what was written:
  `READBACK_OK` = within tolerance; `READBACK_MISMATCH` = a genuine mismatch, **the loud signal for a
  wrong or not-landed write; the value is NOT reverted** (a wrong value may now sit at the IOC);
  `READBACK_UNVERIFIED` = the readback could not be obtained (timeout / value withheld), an absence of
  evidence, never a mismatch. The tolerance is the record's own `control.min_step` when it carries one
  (> 0, the IOC's drive resolution), else `EPICS_MCP_READBACK_TOLERANCE` fed through a magnitude-safe
  `math.isclose`. The same verdict rides back in the tool result (`verified` true/false/null plus
  `readback`/`tolerance`/`note`), so a silent wrong-write cannot hide.

Every terminal line shares the `op=<id>` of its `ATTEMPT` (the `READBACK_*` line too), so an interrupted
or misverified write is never a silent gap in the trail. (A direct, non-tool call to the audit helpers
logs `op=-`.)

### Olog write posture (all four write tools)

Posting to the logbook is the server's first mutating operation against a REST service, so it has its
own gate, distinct from `set_pv_value`, and it never touches `ALLOW_PV_WRITE`. Four **gate** checks
must line up before a write proceeds, each fail-closed and audited as `DENY` before the raise:

- **Env gate.** `EPICS_MCP_ALLOW_OLOG_WRITE=true` (default false = every write denied).
- **Test-server URL boundary.** Unlike PV write (bounded by its own gate + regex allowlist, with
  reach decided by the launcher's EPICS search env), Olog speaks HTTP to an arbitrary URL. A write is refused unless the `OLOG_URL` host is
  **loopback** (the local Olog), or the exact **https** base URL is in
  `EPICS_MCP_OLOG_WRITE_URL_ALLOWLIST` **and** `EPICS_MCP_OLOG_WRITE_ALLOW_REMOTE=true`. A private
  (non-loopback) host, and any plain-http remote (Basic creds are cleartext), is refused by default;
  a production write is a deliberate, auditable double action, and the write session is env-independent
  (a remote's CA comes from `EPICS_MCP_CA_BUNDLE`, not the env). The host is read only from the URL's
  parsed hostname, so a `user@host` trick cannot smuggle a loopback prefix past a production host.
- **Logbook allowlist.** Every target logbook must be in `EPICS_MCP_OLOG_WRITE_LOGBOOKS`; an EMPTY
  allowlist with the gate on is **deny-all** (fail-closed). (The PV write pattern is fail-closed too,
  but differently: an empty pattern with writes on is refused at startup, not treated as allow-all.)
- **Rate limit + audit.** `EPICS_MCP_OLOG_WRITE_RATE_LIMIT` (low; a logbook is human-paced). The limit
  is enforced ATOMICALLY (the purge/check/record step holds a per-gate lock), so concurrent writes,
  the Olog gate runs under `asyncio.to_thread`, i.e. real worker threads, can never both slip past it
  and exceed the window. The audit line is metadata-only, logbook names, level, title LENGTH, entry
  id, service-account owner, and **never** the `title`/`description` free text.

And, on the two round-tripping tools only (`add_log_attachment` / `update_log_entry`), one further
precondition that is **NOT** part of the gate:

- **Whole-mode (pre-gate, un-audited, own error code).** Both tools must round-trip the target
  entry's FULL content, so they refuse a redacted/remote server *before* the gate is consulted,
  at the top of the call, before any read and before any rate token exists. Because it is not a
  gate verdict it writes **no `DENY` audit line at all**, and precisely for that reason it carries
  its **own** code, `OLOG_WHOLE_MODE_REQUIRED`, never the gate's `OLOG_WRITE_DENIED`: a caller must
  be able to tell an un-audited pre-gate refusal from an audited gate DENY (write-gate contract
  point 4). The exception is still a subclass of the gate's, so `except OlogWriteDeniedError`
  catches both.

The author (`owner`) is the write service account (`EPICS_MCP_OLOG_WRITE_USER`, a dedicated account,
never a personal login), set server-side from the auth Principal; a caller cannot spoof it. Use a
loopback Olog for testing; the target logbooks must already exist server-side (a non-existent logbook
is an HTTP 400).

### Olog attachment posture (upload + download)

Attachments are the one Olog surface where raw bytes cross the boundary, so the posture has two tiers.

**Upload** (`create_log_entry` / `reply_to_log` with `attachments` = workspace file paths, or
`embed_image_base64`): the entry is sent as `multipart/form-data` to `PUT /logs/multipart` (mirroring
CS-Studio's own client) instead of the plain JSON `PUT /logs`, the no-attachment path is unchanged. It
rides the SAME write gate as a plain post (env + URL boundary + logbook allowlist + rate limit), plus a
**total-size cap** (`EPICS_MCP_OLOG_ATTACH_MAX_BYTES`, checked from file `stat` BEFORE any bytes are
read and RE-CHECKED while reading, a file that grew past the cap between stat and read is refused,
with at most one byte over budget ever read). Any file type is accepted (only HEIC is refused
server-side, by content-sniff); an over-limit request is the server's HTTP 413. The audit line carries
attachment COUNT + total BYTES, never a filename (a filename is author free text). Each attachment gets
a client-minted UUID and an id-prefixed unique filename (`<uuid>_<name>`), so a by-name download can
never hit the server's duplicate-filename 404.

**Attach to an existing entry** (`add_log_attachment` → `POST /logs/multipart`) is a third write tool,
gated identically, with the logbook allowlist keyed on the TARGET entry's OWN logbooks (read first). It
is **whole-mode only**: Olog's update endpoint is destructive, it `retainAll`-prunes any attachment not
resubmitted and overwrites title/body/logbooks/tags/level/properties, so a safe attach
must round-trip the target entry's FULL content, readable only whole (loopback + `ASSUME_TEST_DATA`).
Against a redacted/remote server it is refused, by the **pre-gate** check above, un-audited and
reported as `OLOG_WHOLE_MODE_REQUIRED`, not by the gate. The write is purely ADDITIVE for CONTENT:
existing attachments and every content field survive, and only the new file(s) are added.

⚠️ **One field does NOT survive: `owner`.** Because this endpoint IS the destructive update,
the server re-stamps the entry's owner with the write service account on every attach, the
original author then exists only in the server-side archived version. Attaching a file to
someone else's entry therefore rewrites its authorship. This is the same behaviour documented
for `update_log_entry` below; it was previously described here as "every field survives",
which was the opposite of what the server does.

⚠️ **Attachment retention is FILENAME-keyed, not id-keyed** (measured in the server source, and the
single most surprising fact about this endpoint). `retainAll` tests membership against the SUBMITTED
collection, which deserializes into a `TreeSet`, so the match runs through `Attachment.compareTo`,
which compares `filename` **case-insensitively**. `equals`/`hashCode` do use the id, but a `TreeSet`
never consults them. Two consequences: re-listing an attachment by id alone (filename null) prunes it,
and two attachments whose filenames collide case-insensitively collapse into one, silently, and not
fixable client-side. That is why every round-trip re-lists a non-null filename per attachment, and why
**both** round-tripping tools (`update_log_entry` and `add_log_attachment`) REFUSE an entry whose
attachments cannot survive the match, duplicate, missing, or otherwise unreadable, instead of
writing to it. The re-submitted list and that refusal are derived in the SAME pass, on purpose: when
they were computed separately they disagreed, and an attachment the payload quietly omitted passed the
guard as safe (found by adversarial review, probe-measured, the guard existed and the file was lost
anyway).

**Edit an existing entry** (`update_log_entry` → `POST /logs/multipart` with the `logEntry` part and NO
file parts) changes `title` / body / `level` / `logbooks` / `tags`. Same destructive endpoint, same
**whole-mode only** pre-gate rule (same `OLOG_WHOLE_MODE_REQUIRED`, still un-audited), same gate, with
one difference that matters: the logbook allowlist is keyed on
the **UNION** of the entry's current and resulting logbooks, because moving an entry INTO a logbook and
pulling it OUT of one are both writes to that logbook; gating on either side alone leaves a hole. An
omitted argument means "unchanged", the tool round-trips the whole entry and overlays only what was
passed, so attachments and properties survive. Three server behaviours it has to compensate for:
a body edit is written to **`source`**, never `description` (under `markup=commonmark` the server
regenerates `description` FROM `source`, so a new description beside a stale source is silently
overwritten and the edit vanishes); the update does **not** validate logbook/tag existence the way
create does, so unknown names are checked client-side or they become phantom references; and an
empty title is **not** rejected server-side. Note also that the server re-sets `owner` to the write
service account on EVERY update, the original author survives only in the archived version, and that
editing a legacy entry with no raw `source` makes the server re-render its visible body (reported back
as a warning).

⚠️ **The archived version is not reachable from this server.** It is cited here and in the code as
the reason a re-stamped owner or an overwritten field is recoverable, but no MCP tool can read or
restore it, recovery is manual, by someone with direct access to the Olog service (its history
API or its datastore). Treat every edit as effectively irreversible from here, and take the
`owner` re-stamp as permanent for any purpose this server can serve.

**Download** (`download_log_attachment`, by `log_id`+`filename`, or by GridFS `attachment_id`) and the
filenames in `list_log_attachments`: raw bytes and filenames are author free text and BYPASS the entry
redaction, so they leave only in **whole-mode** (loopback URL + `ASSUME_TEST_DATA`), and the raw BYTES
additionally require the explicit **`EPICS_MCP_OLOG_ALLOW_ATTACHMENT_DOWNLOAD`** opt-in (default false =
withheld). Two reasons for the second gate: bytes sidestep the dict-based redaction entirely, and the
by-id endpoint has NO server-side per-log authorization (any valid GridFS id grants content), so
un-redacted byte egress is a deliberate, auditable choice, not implied by the URL. Bytes cross the MCP
boundary as a workspace file `output_path` (a NEW file, `EPICS_MCP_ALLOWED_ROOTS`-checked, it refuses
to overwrite an existing file or follow a symlink, so a download never loses data or escapes the
boundary) or, for a small inline image, base64 in the result. The download body is size-capped by
`EPICS_MCP_OLOG_ATTACH_MAX_BYTES` (a base64 result is capped smaller still, it rides back as response
tokens), so a huge attachment cannot OOM the process. `epics-doctor`'s Olog posture line already tells
you whether whole-mode is in effect.

## Operational recipes (the non-obvious parts)

### List archived PVs: `list_archived_pvs` (or the MGMT API directly)
`list_archived_pvs` enumerates the archived PV names, the sibling `is_archived`/`get_pv_history`/
`get_archive_info` tools each require a PV name. Under the hood it calls the MGMT `getAllPVs` (whole
appliance) or, with `this_appliance=true`, `getPVsForThisAppliance` (this member); `capped` is honest
(over-fetch by one). It deliberately does **not** use `getMatchingPVs`, a standard endpoint too, but
one that **may 404 on proxied/split deployments**. Cluster shape: `getApplianceInfo` /
`getAppliancesInCluster`.

**The name filter works on ONE of those two endpoints.** `getAllPVs` honours a `pv` glob.
`getPVsForThisAppliance` has **no name filter at all**, measured, it ignores `pv`, `regex`, `pattern`
and `name` alike and answers byte-identically to the unfiltered call. It does not fail; it hands back
a full, plausible list of the **wrong** PVs, and an honest `capped` makes that read like a fair
truncation. Going at the MGMT API directly, filter only via `getAllPVs`. `list_archived_pvs` refuses
the combination (`INVALID_ARGUMENT`) rather than answer it wrongly; client-side filtering is not a
substitute, because `limit` is applied server-side, filtering a capped page yields an arbitrary
subset behind a meaningless `capped`.

**Finding PVs by archiving RATE (e.g. a moderately-sampled test fixture):** enumeration cannot see
rates, but the MGMT report endpoints can, `GET /mgmt/bpl/getEventRateReport` answers a rate-sorted
list of `{pvName, eventRate}` objects (the shape: measured live on two appliances, 2026-07-16;
the descending sort order, string-serialized rates and the `limit` semantics below: measured on
one 16-member cluster, 2026-07-17). Two measured semantics matter:

- **`limit` is NOT a row cap.** It behaves as if applied per cluster member with the members'
  slices merged, row counts = limit × member count, measured twice (`limit=3` → 48 rows,
  `limit=100` → 1600 on 16 members; the mechanism is inferred from the counts, not observed).
  Omit `limit` entirely and filter client-side. The no-limit report is near-complete but NOT
  the whole archived set (measured: ~1.5M report rows vs ~1.7M `getAllPVs` names on the same
  cluster; the gap is unverified, plausibly paused or rate-less PVs), so a PV's absence from
  the report proves nothing about its archive status.
- **The rate is the appliance's own recent-window figure, not your target window's.** A band hit
  is a hypothesis, never a fixture: counter-verify every candidate with a real history fetch in
  the window you actually care about (a PV can sit in the band and still hold nothing there).

Sampling `getAllPVs` blindly is the wrong tool for that job: archives are often bimodal (fast PVs
plus carried-only ones), so a blind stride finds no middle, measured: five blind strategies over
153 candidates found none, while the report-walk verified its first candidates in one pass.
`python -m epics_mcp.find_moderate_pv` (read-only, in the core install) automates exactly this
walk: full report → rate band → per-candidate counter-verification against the target window.
Also know your SITE'S cluster layout before
enumerating: a facility may run **several archiver clusters split by purpose** (e.g. scalars vs.
large waveforms vs. per-network instances, each with its own MGMT root), `getApplianceInfo`'s
`identity` tells you which appliance a URL actually is, and enumerating the wrong cluster silently
yields a complete-looking list of the wrong population.

### Verify a deployment's config: `epics-doctor`
The `epics-doctor` CLI (core install) is a read-only self-check: it probes every CONFIGURED plane
(a transport probe, refined on success by an identity probe, up to two requests for a healthy
plane; retries on a 5xx add more) and
reports, per plane, whether it is reachable, whether the CA bundle works, whether the
service **identifies itself as the one that URL should point at**, and what the ChannelFinder
redaction is set to. A disabled plane (empty `*_URL`) is reported honestly, never a failure. Exit
`0` = nothing failed and no identity probe failed, `1` = a configured plane HARD-failed
(`unreachable` / `ca_error` / `api_error` / `config_error` / `backend_down` / probe-disconnect),
`2` = a usage error,
`3` = INCONCLUSIVE, a plane is reachable but its identity probe FAILED (a served non-2xx like a
401/404, a transport error, or a refused redirect): not a hard failure, but not a silent all-clear
either. Run it first in a new facility to confirm the environment the launcher handed this process
(there is no `.env` file: configuration is read from `os.environ`); add `--probe-pv NAME` to also
pass/fail the live PVA plane against a real PV. Full deployment/config guide: `docs/deployment.md`.

**Reachable is not identified: read the `?` and `!` lines.** The transport probe is a HEAD and
counts *any* HTTP response as reachable, so a URL pointing at the wrong host can look alive:
measured, a ChannelFinder URL aimed at a dead container reported `✓ ok` because an unrelated service
on that port answered `401` (blanket auth answers 401 for every path, so the status said nothing
about ChannelFinder). That exact case now surfaces as `!` `identity_probe_failed` (exit `3`): the
identity probe hits the 401 and no longer collapses to a silent exit `0`. Each plane is therefore
also asked to **name itself**:

| Mark | Status | Meaning |
|---|---|---|
| `✓` | `ok` | the service named itself correctly, confirmed |
| `?` | `unverified` | reachable and ANSWERED 2xx, but could not prove what it is (a body with no usable `name`, an unreadable/HTML body, or a beacon naming a *different* service, with that name in the detail). **Honest, not healthy**, exit stays `0` |
| `!` | `identity_probe_failed` | reachable, but the identity probe FAILED (a served non-2xx like a `401` auth wall or `404`, a transport error, or a refused redirect on the identity endpoint). Reachable but suspect, **not** a hard failure (the plane's tool endpoints may work), **not** a silent all-clear either: exit `3` (INCONCLUSIVE) |
| `✗` | `config_error` | the configuration is self-contradictory, no probe is even attempted (e.g. `EPICS_MCP_ARCHIVER_RETRIEVAL_URL` set while `EPICS_MCP_ARCHIVER_URL` is empty, every archiver tool gates on the latter, so that retrieval URL is never used). Exit `1` |
| `✗` | `backend_down` | reachable AND the service named itself, but a backend it depends on is measurably down, the alarm logger reports its Elasticsearch as not `Connected`, so its search/history tools will fail. The blind HEAD used to hide this as `ok`; unlike `unverified`, identity IS proven here and the service reports its OWN backend broken. Exit `1` |

`unverified` is not a failure on purpose: that a healthy service answers its beacon anonymously is
measured at one site, and making that a hard failure everywhere would be an overclaim. The same
holds when the beacon names a *different* known service (an earlier release failed that case as
`wrong_service`, exit `1`): measured, a path-based reverse proxy served the real ChannelFinder API
while the base GET answered as Olog, the foreign name cannot prove a misconfiguration, so the
doctor reports it honestly and keeps the found name in the detail as the first clue for when the
config IS wrong.

Each plane has its own beacon, because they do not share one:

| Plane | Beacon | Identified by |
|---|---|---|
| ChannelFinder / Olog / Alarm | `GET <base-url>` | exact `name` ("ChannelFinder Service", ...) |
| Archiver (MGMT) | `GET /mgmt/bpl/getApplianceInfo` | the `identity` field |
| Archiver (retrieval) | `GET /retrieval/bpl/getVersion` | the product name in `version` |
| Naming | `GET /rest/swagger.json` | `info.title` |

⚠️ Mind the base PATH on the archiver: retrieval serves `/retrieval/bpl`, **not** `/mgmt/bpl`.
Probing the latter on the retrieval port 404s and tells you nothing, that mistake is exactly how an
earlier pass concluded retrieval had no beacon at all.

The Naming `/rest/swagger.json` beacon is single-sourced (title + path in `services/naming_identity`)
and now backs **two** surfaces: epics-doctor's naming plane AND `lookup_device_name`'s S13
definitive-negative gate (a 204/404 is trusted only after this beacon confirms the responder, see
"not found vs not registered" below).

Scripting against this? Exit `0` means "nothing failed", **not** "everything confirmed", read
`verification_complete` / `unverified_planes` / `inconclusive_identity_planes` from `--json`, never
the exit code alone. A FAILED identity probe lands in `inconclusive_identity_planes` (exit `3`), NOT
in `unverified_planes` (exit `0`), a script hunting identity problems must read both. And for
**positive** confirmation assert `identified_planes` is non-empty: `verification_complete` is
vacuously true on an empty config, so it alone cannot tell "all confirmed" from "nothing ran".

### Retrieval-cluster-aware appliances
An Archiver Appliance may run as an **N-member failover cluster**. Such a cluster is retrieval-aware: a
MGMT/retrieval query to **one** member transparently returns data physically owned by **another** member;
the response's `appliance` field names the true owner. So one `EPICS_MCP_ARCHIVER_URL` covers the whole
cluster and you do **not** route per member.

This is **orthogonal** to the MGMT/retrieval **port split**. When the mgmt webapp (`:17665`, `/mgmt/bpl`)
and the retrieval webapp (`:17668`, `/retrieval/data`) run on different ports/hosts, common on a split
deployment, **clustered or not**, you **must** set `EPICS_MCP_ARCHIVER_RETRIEVAL_URL` to the retrieval
one, or `get_pv_history` falls back to the mgmt URL and 404s. Clustering removes per-member routing; it
does **not** remove the port split. Only genuinely separate, non-clustered appliances (or several
disjoint clusters) need per-appliance handling beyond that.

### CA-bundle: combine the trust anchors
When the REST planes present **different** trust roots, e.g. one internal service signed by a private
facility CA and another signed by a **public** CA, a single-root bundle fails one of them. Fix:
concatenate the internal CA PEM **with** the public roots (certifi's `cacert.pem`) into **one** combined
PEM and point `EPICS_MCP_CA_BUNDLE` at it. The session pins `trust_env=False` whenever a CA path is set,
so a stray `REQUESTS_CA_BUNDLE` cannot silently override it. (`EPICS_MCP_TLS_VERIFY=false` disables
verification entirely, internal-network escape hatch only, never the default.)

### Read record fields directly (`.FIELD`)
Any channel name works, so append a field suffix to read record metadata without a dedicated tool:
`get_pv_info("SIM:PS-01:Cur-RB.RTYP")`, `.SCAN`, `.HIHI`, `.HOPR`. This is the cheap path to alarm
thresholds when an NT `valueAlarm` comes back NaN over a PVA gateway. A cold first connect can be slow:
pass `timeout` ≥ 8 for the first probe.

### Naming URL: no trailing `/rest`
Set `EPICS_MCP_NAMING_URL` to the service root **without** `/rest` and without a trailing slash. The
client appends `/rest/deviceNames/...` itself; a trailing slash is normalized, but
appending `/rest` yourself yields `/rest/rest/...` → 404.

### Archiver history time window: ISO-with-zone only, and a 500 does not mean unreachable
Three planes, three different time grammars, this is the narrowest, and the only one that is
strict. Measured: `2026-07-08T00:00:00.000Z` works; a naive `2026-07-08T00:00:00`, the
space-separated wall clock Olog *requires*, a bare date, and the `7 days` amount the alarm/logbook
searches take are each an **HTTP 500**; an inverted window is a 400.

That strictness is a virtue: nothing here is silently wrong. The trap is the diagnosis: a served
5xx is not an outage, and a caller carrying `7 days` over from `get_alarm_history` gets one. Going
at the retrieval API directly, always send zone-explicit ISO. `get_pv_history` normalizes every
absolute notation for you and refuses a relative amount by name.

### Alarm history time window: ISO without a zone is read as "now" (and will not tell you)
The Alarm Logger uses the REAL Phoebus parser (no vendored copy), so it reads ISO **with** a zone:
`2026-07-08T12:45:58Z` and `+02:00` both work, and a bare date works too. But a zone-LESS
`2026-07-08T12:45:58` matches no parser and falls into the same silent zero-offset path as Olog's:
the window becomes `[now, now]` and the answer is **HTTP 200 with an empty list**. Measured live:
the same 7-day window returned 20+ events with the `Z` and 0 without it. A window that wide cannot
be emptied by a mere zone shift, it is the collapse.

This matters more than it looks: `datetime.now().isoformat()` emits exactly the zone-less form, so
it is the most likely wrong value a caller will ever produce. `500 millis` is worse still, it
RETURNS data, for a 500-**minute** window. And the server's own range check is an empty `if` branch
carrying a `// TODO check that the start is before the end`: an inverted window is logged
server-side and queried anyway.

`get_alarm_history` normalizes for you (absolute → `yyyy-MM-ddTHH:mm:ss.SSSZ`; amounts pass
through; anything unclassifiable is refused before the request). Going at the REST API directly,
always send the zone.

### Olog search time window: the server cannot read ISO-8601 (and will not tell you)
Olog parses `start`/`end` with a **vendored, stripped** copy of Phoebus' `TimestampFormats` that has
no ISO support, upstream's own copy has it; the fork deleted it. Only a **space-separated** wall
clock (`yyyy-MM-dd HH:mm:ss.SSS` and shorter) or a **single relative amount** (`7 days`, `90 min`,
`now`) parses.

The trap is the failure mode: an unreadable value is **not** an error. Olog falls back to its
relative-amount parser, which scans for a unit token, finds none in an ISO string, and returns a
**zero** offset, which it subtracts from *now*. The window collapses to `[now, now+µs]` and matches
nothing, so the answer is **HTTP 200 with an empty list**: indistinguishable from "nothing matched".
Same window, same data, different format → 0 results vs. all of them.

`search_logbook` normalizes for you (absolute → UTC wall clock plus `tz=UTC`; amounts pass through;
anything unclassifiable is refused **before** the request). Going at the REST API directly, do it
yourself. Also note: **months/years are unusable** (the amount is subtracted from a point in time,
which cannot carry them → rejected), and `millis` means **minutes** to Olog's unit dispatch (use
`ms`). Always send `tz` with a wall clock, without it Olog reads the string in the **server's**
default zone, so a window is silently offset against any deployment not on UTC.

**Generalisable:** a fully-mocked test suite cannot detect a server-side silent drop, a mock only
sees what the client *sent*, never what the server *honoured*. Only a differential live probe
(same window, several formats, plus a negative control) can.

The converse hides just as well: a mock cannot see a **loud** server-side rejection either. The
Archiver 500s on four notations a caller may reasonably send, and the offline suite was green
throughout, it passed `"a"`/`"b"` as the window, values no layer ever looked at. A parameter that
nothing validates and nothing probes live is *untested*, however many tests name it. Three planes
were checked this way; **three** carried a defect no mock could reach.

## Error signatures → which tool answers

Illustrate signatures by the exception **class and shape**, never a copied runtime string (error
messages embed the full request URL, an internal host would leak into this file).

- **PV times out / disconnected.** On a PVA name-server a typo **and** a dead IOC both surface as
  `PV_TIMEOUT` (never `PV_NOT_FOUND`), the cause cannot be read off the transport error code. Use
  `diagnose_connection`: the live probe is the sole authority for connected/disconnected; ChannelFinder
  and Naming only *explain* it (they never flip the verdict; `withheld ≠ no`).
- **"not found" vs "not registered".** `lookup_device_name`: the service's measured "no such name",
  HTTP **204** (what an ESS-style Naming Service actually answers), and a real HTTP 404 are the
  **definitive** `registered:false`, but **only once the responder proves it is the Naming Service via
  its `/rest/swagger.json` beacon (S13)**; a service/transport/TLS failure, an unreadable record, OR an
  unverified/foreign identity is **withheld** (`registered:null`), never a false negative. (A
  registered/ACTIVE answer skips the identity probe, the measured hazard is a foreign 404, not a
  foreign ACTIVE record.) The extra swagger round-trip fires only on the not-registered path and is
  cached per client instance; when it withholds, a server-side `logger.debug` records the probe verdict.
- **Archived? how? history?** `is_archived` / `get_archive_info` / `get_pv_history`. History `status` is
  `ok` / `empty` / `withheld`, a bare `[]` means only a truly empty history, not "could not read";
  a single unreadable sample withholds the whole result rather than being silently skipped.
  Both status tools harvest fields the appliance already returns in the same call (no extra request):
  `is_archived` adds the getPVStatus connection-history cluster `connection_loss_regain_count` (the
  flapping counter), `connection_first_established` and `connection_last_restablished` (`"Never"` if
  never dropped, note the appliance's upstream typo "Restablished", preserved verbatim);
  `get_archive_info` adds the getPVTypeInfo alarm/display/control limits + `units`/`precision`
  (`upper_alarm_limit`=HIHI ... `*_ctrl_limit`=DRVH/DRVL) and `controlling_pv`/`policy_name`/
  `modification_time`. ⚠️ The nine numeric limits are ALWAYS present and read `"0.0"` when the PV
  had no ctrl info, `"0.0"` may mean "no limit configured", not a literal zero.
- **Which appliance am I on? cluster topology?** `get_appliance_info` (no PV) surfaces the whole
  MGMT `getApplianceInfo` body, the appliance `identity`, the per-plane root URLs
  (`mgmt_url`/`engine_url`/`etl_url`/`retrieval_url`/`data_retrieval_url`), `cluster_inet_port` and a
  `version` string, of which the doctor plane-check keeps only `identity`. Use it to confirm you are
  pointed at the intended cluster BEFORE trusting `list_archived_pvs`/`get_pv_history` (enumerating
  the wrong cluster silently returns a complete-looking list of the WRONG PVs), and to read a
  split/proxied deployment's plane layout. `version` is omitted when the appliance lacks it (not
  "always present"). A served **404** here means the wrong endpoint, the retrieval webapp serves
  `/retrieval/bpl`, not `/mgmt/bpl`, and propagates as an error, never a false empty answer.
- **Alarm configured / history?** `is_alarm_configured` (`configured` is `true`/`false`/`null`;
  `null` = withheld, see the config-tree recipe below) / `get_alarm_history` (`start` + `end`
  required; `pv_name` is matched as a wildcard SUBSTRING of the config path, `Value` matches both
  `...:Temp1Value` and `...:12VValue`). A `false` is a true negative only if the Alarm Logger was
  running at config-import time, otherwise treat it as unreliable; the tool cannot flag this.
- **Narrow an alarm-history query.** `get_alarm_history` has optional SERVER-SIDE filters:
  `root` (config tree name(s), comma-separated), `command` (`Enabled`/`Disabled`, the config
  change that turned an alarm on/off; maps to the `enabled` field and is restricted to config-change
  docs so state events do not swamp it), and `severity`/`current_severity` (the 9 EPICS severities
  `OK`/`MINOR`/`MAJOR`/`INVALID`/`UNDEFINED` and their `_ACK` variants). ⚠ **UNVERIFIED, and the
  failure mode is silent:** the Alarm Logger IGNORES a query parameter it does not support (its
  parser's default branch is a bare `break;`), so an older logger BROADENS the result instead of
  erroring, a returned set can be wider than the filter implies. Confirm a filter with a
  differential probe (a value that must match vs. one that must not) before trusting it.
- **An Olog search answers 401.** On an anonymous read that is almost never a credentials problem:
  Olog's error dispatch requires authentication, so it returns **401 in place of its own 400**:
  every server-side rejection looks like "unauthorized". Check the query (the time window first);
  a deployment configured with read credentials sees the real 400 and its message.
- **An Olog search answers 200 with an empty list.** Not necessarily "nothing matched", an
  unreadable `start`/`end` degrades to *now* server-side and matches nothing (see the recipe
  above). Re-run with a relative amount (`7 days`) to tell a real empty from a dead window.
- **An Archiver call answers `ARCHIVER_HTTP_5xx` / `INVALID_TIME_WINDOW` / `INVALID_ARGUMENT`.**
  None of these means the appliance is down: it answered, or the call never left. A 5xx on
  `get_pv_history` is nearly always the time format (this plane reads zone-explicit ISO and nothing
  else); `INVALID_TIME_WINDOW` is a window the plane cannot express (e.g. a relative amount);
  `INVALID_ARGUMENT` is an argument combination that cannot be answered at all. Only
  `EPICS_CONNECTION_FAILED` means unreachable.
- **Which IOC/host serves a PV?** `find_channels`.
- **Filter or count channels by property/tag.** `find_channels` takes optional server-side filters
  (`has_properties`/`lacks_properties`/`not_property_values`/`has_tags`/`lacks_tags`) and `count_only`
  for an exact match count via `/count` without pulling the matches. Property filtering is gated to
  the DS-privacy safe-property allowlist (a redacted property like `accessGroup` is refused with
  `INVALID_INPUT`); widen it with `EPICS_MCP_CHANNELFINDER_SAFE_PROPERTY_NAMES`. Two honesty rules:
  the TAG filter semantics (`has_tags`/`lacks_tags`) are **UNVERIFIED** until a differential live
  probe (the property filters were live-verified 2026-07-22); and an unknown/misspelled property
  name is **not a server error**, it narrows the result to 0, indistinguishable from a genuinely
  empty match (there is no vocabulary-discovery endpoint yet).
- **Enumerate PVs by pattern?** `discover_pvs` with a wildcard (`*`/`?`) routes to ChannelFinder,
  the same registry `find_channels` queries, so hits are `registered` (registry membership, not a
  live connect; use `get_pvs`/`get_pv_value` for liveness). A concrete name instead does a live p4p
  probe. Wildcard enumeration needs `EPICS_MCP_CHANNELFINDER_URL`; unset, it returns an honest
  'requires ChannelFinder' note, never a bare empty that reads as 'no such PV'.
- **`is_alarm_configured` answers `configured:null`.** The alarm tree itself returned nothing, so
  "this PV is not configured" cannot be told apart from "that is not the tree name", the answer is
  withheld, and a `note` says so. The tree name is **REQUIRED** (no default, the trees are
  site-specific, so there is no correct universal one; a guessed default silently matches nothing)
  and **case-sensitive** in the query even though the server lower-cases it to pick the index, so a
  mis-cased name (`mytree` vs `MyTree`) selects the right index yet matches nothing. Re-run with the
  tree spelled exactly as the logger stores it. The `config` field in the response echoes your input;
  it is not the server confirming the tree.
- **A ChannelFinder glob finds nothing / finds odd casing.** The glob is **anchored**: a bare
  substring (`Ctrl-EVR-01`) matches 0, wrap it in stars (`*Ctrl-EVR-01*`). And it is
  **case-insensitive**: `*Temp*` legitimately matches `...MorTemPrd`, so a hit may differ in case
  from what you asked.
- **A REST 404 = `found:false`** only where documented (`getPVTypeInfo`, Olog `get_log_entry`); any other
  error propagates, could-not-read is never silently "not there". An **unreadable 2xx** is part of
  that rule: every REST client validates the payload against its measured schema and raises (or
  withholds where a status channel exists) instead of minting a definitive answer. Two wire
  realities worth knowing: a real Olog answers **401** (not 404) for an unknown id on the anonymous
  read path, that surfaces as an error, never as `found:false`; and an ESS-style Naming Service
  answers **204** for a nonexistent name, mapped to the definitive `registered:false` **only after
  its `/rest/swagger.json` identity beacon confirms the responder (S13); an unverified/foreign 404 is
  withheld, not minted into a false `name_typo`**. The Archiver/Olog `404 = found:false` stay
  **ungated**: the same latent "is this the right service?" question, deferred as lower-severity (a
  foreign archiver/olog 404 is corroboration, not a device-identity verdict). With the Olog plane
  *disabled*, `get_log_entry` answers `found:null` (not checked), mirroring the
  `archived`/`configured`/`registered` siblings.

## Acceptance: the questions this guide must answer

A fresh session with only this guide should now handle the four things a live smoke had to re-derive:

1. **Enumerate archived PVs** → `list_archived_pvs` (uses `getAllPVs` / `getPVsForThisAppliance`, not
   `getMatchingPVs`, which may 404 on split/proxied deployments).
2. **A clustered archiver** → one `EPICS_MCP_ARCHIVER_URL` covers all members (retrieval-aware; the
   `appliance` field names the owner). The mgmt/retrieval port split is orthogonal: if retrieval runs on
   `:17668`, still set `EPICS_MCP_ARCHIVER_RETRIEVAL_URL`, clustering does not remove the port split.
3. **TLS fails on one plane but not another** → combine internal CA + public roots (certifi) into one
   `EPICS_MCP_CA_BUNDLE` PEM.
4. **A service presents a public certificate** → the combined CA bundle already covers it; no separate config.

---

*Found a new service/operational/IOC fact? Add it here, this file is the operational-knowledge tier of
the Knowledge Persistence Policy (`CLAUDE.md`), not a session transcript. Keep it facility-agnostic.*
