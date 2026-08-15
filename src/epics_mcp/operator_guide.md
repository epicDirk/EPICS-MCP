# EPICS MCP: Operator Guide (`epics-pv://guide`)

The operational cookbook for this MCP server: the service landscape, the tricks that are not
obvious from the tool signatures, and the error signatures that tell you which tool to reach for.
A fresh session should read this once instead of re-deriving it.

It is served verbatim as the `epics-pv://guide` MCP resource and shipped inside the package, so it
travels with the server: no external skill or project folder required.

**Every example uses a synthetic placeholder name, never a real one, and a new example draws from
this declared set rather than inventing a name:** `SIM:PS-01:Cur-RB` (a PV), `sim://ramp` (a
non-channel reference), `http://archiver:17665` and `http://channelfinder:8080/ChannelFinder`
(service roots). This is a RULE for whoever adds an example, not a description of the text below:
measured 2026-08-15, only the first of the four appears anywhere else in this document.

## Posture (read this first)

- **Read-only by default.** The mutating tools are gated OFF. `set_pv_value` passes **three** gate
  checks (`EPICS_MCP_ALLOW_PV_WRITE=true`, a regex allowlist, a rate limit) and needs an audit log,
  which records the verdict rather than reaching one; a value outside the record's drive limits is
  refused after the gate has already admitted the write, so it is a fourth refusal, not a fourth
  gate. The Olog
  logbook writers (`create_log_entry`, `reply_to_log`, `add_log_attachment` and `update_log_entry`) sit behind a **separate** gate, see the Olog
  write posture below; `ALLOW_PV_WRITE` is untouched by it. The fullest statement of this posture,
  including what leaves your machine on every plane, is
  [docs/safety.md](https://github.com/epicDirk/EPICS-MCP/blob/main/docs/safety.md); what follows
  here is the operating half of it.
- **Writes enabled ⇒ loopback-only search reach, enforced at boot.** With `ALLOW_PV_WRITE=true` the
  server refuses to start (`SafetyConfigError`) unless the EPICS client search env is loopback-only:
  every `EPICS_PVA/CA_ADDR_LIST` / `_NAME_SERVERS` token a loopback host AND both `*_AUTO_ADDR_LIST`
  parser-faithfully disabled, which is NOT the same test on both providers: PVA (pvxs) accepts
  exactly `NO` in any case or exactly `0`, CA (libca) disables on any value CONTAINING `no`/`NO`,
  so `false` and `0` keep CA broadcasting. Unset means ON for both. If a write-enabled process won't boot, the
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
  empty URL means no client and no network call. One exception:
  `EPICS_MCP_ARCHIVER_RETRIEVAL_URL` falls back to the mgmt URL and is still probed, because a
  single-JVM appliance legitimately leaves it empty.
- **Long monitors run in a dedicated pool.** `monitor_pv` holds a worker thread for up to
  `EPICS_MCP_MAX_MONITOR_DURATION` (60 s). Those blocking subscriptions run on a DEDICATED executor
  sized by `EPICS_MCP_MONITOR_MAX_CONCURRENCY` (default 8), NOT the shared pool behind every other
  read/write, so a burst of monitors cannot starve the rest of the server into an apparent hang.
  Raise it only if operators legitimately need more simultaneous long monitors; the ceiling bounds
  monitors alone.
- **An empty monitor result says WHY it is empty.** `monitor_pv` returns `connection`
  (`connected` / `disconnected` / `unknown`) alongside the events, so zero events no longer reads
  the same for a quiet PV and for one that was never reachable, which is what made this tool
  contradict `get_pv_value` (it raises `PV_TIMEOUT` on the second case). `connected` is claimed
  only where a delivered value proves it; a stream the server ended, or one that reported an
  error, is `unknown` rather than a guess, and `connection_detail` then carries one sentence
  saying what was seen. The state is subscribed for, not probed separately, so it describes the
  same run as the events.
- **Reads can be throttled (opt-in).** `EPICS_MCP_READ_RATE_LIMIT` (default 0 = off) caps REST reads
  per 60 s at the shared GET chokepoint; over the limit a read is DENIED
  (`ReadRateLimitError` → `READ_RATE_LIMIT_EXCEEDED`, its own code, never the write gates'
  `RATE_LIMIT_EXCEEDED`, because this refusal writes no audit line and is not a gate verdict), never
  blocked, a blocking wait there would hold a worker thread and reintroduce the monitor starvation
  above. Turn it on to protect a production facility from an unthrottled read burst. It bounds only
  the REST planes (ChannelFinder/Archiver/Alarm/Naming/Olog), not live p4p PV reads. Whether it
is on in the process you are talking to is `rest_read_rate_limit` in `epics-pv://health`, whose
name carries that REST-only reach, so the field cannot be read as an all-clear for PV reads.
Two caveats:
  the ChannelFinder/Alarm/Olog/Naming reachability self-checks are HTTP HEADs and bypass the
  throttle entirely, but the ARCHIVER's is not, it is a real GET of `/mgmt/bpl/getApplianceInfo`
  (it needs a 2xx to tell "reachable" from "wrong endpoint"), so it spends a token like any other
  read and `epics-doctor` can be denied on a tight limit. That archiver plane now spends **three**
  tokens per run, not two: `check_connectivity`, the identity beacon, and the ingest probe
  (`getApplianceMetrics`) that the identity beacon leads to. Sizing a limit for a WHOLE
  `epics-doctor` run rather than for one plane, measured 2026-08-03: every configured REST plane
  spends one token for its identity beacon, and the archiver spends FIVE all told, because its
  reachability probe is a GET as well and its retrieval half is probed as a plane of its own even
  when it falls back to the management URL. All six planes configured spend NINE; the archiver
  alone already spends five. The two multi-GET tools whose cost SCALES with the input are measured
  (2026-07-31) rather than estimated, and they do NOT behave alike, so size each from its own
  figure or the tool aborts mid-run with a loud `READ_RATE_LIMIT_EXCEEDED` (never a silent partial
  result):
  - With both per-PV planes requested, `coverage_audit` spends 1 + 2N tokens for N audited PVs
    (one ChannelFinder query for the whole set, then Archiver and Alarm once each per PV), rising
    to 1 + 3N when none of them is alarm-configured: a MISSED alarm lookup buys one extra GET,
    because the tree is re-asked to tell "not configured" apart from a misspelled tree name. The
    cost per PV is therefore between 2 and 3 and depends on your data, not on a constant. Request
    fewer planes and the per-PV term shrinks with them; the leading 1 is the ChannelFinder query.
    The audited set N is ChannelFinder UNION the display PVs, which is not the display PV count.
  - `crossplane_check` spends 2 tokens in total and is NOT per PV: Naming once for the IOC device
    name, ChannelFinder once for the prefix, the same for one display PV or a thousand. It costs
    3 when the device name is not registered, which is the finding it exists to report, because a
    definitive negative is only given after the Naming service's identity is probed.
  - A THIRD tool can spend several tokens, and only its defaults keep it off that list:
    `diagnose_connection` runs one gatherer per explanatory plane, and only ChannelFinder is on by
    default (one GET). Switch the other three on and it costs five requests, because Naming spends
    two (a reachability probe plus the name lookup). Its cost does not scale with anything, so size
    it as a constant, not per PV.

## The planes

The canonical plane→source→tools table is the README section **"The planes it sees"**; the safety
and network posture is
[docs/safety.md](https://github.com/epicDirk/EPICS-MCP/blob/main/docs/safety.md). Those two are the
single source of truth. In short:

- **Live PV access** (p4p PVAccess/Channel Access), the *only* authority for connected/disconnected.
- **ChannelFinder**: which IOC/host serves a PV, plus its tags/properties. `EPICS_MCP_CHANNELFINDER_URL`.
- **Archiver Appliance**: is a PV archived, how, and its history. `EPICS_MCP_ARCHIVER_URL`
  (MGMT root, `/mgmt/bpl`) + optionally `EPICS_MCP_ARCHIVER_RETRIEVAL_URL`, the retrieval root,
  which serves **two** context paths and is the root of both rather than of either: `/retrieval/data`
  (the samples) and `/retrieval/bpl` (its own version beacon). Setting the URL one level too deep is
  the ordinary cause of a 404 here.
- **Phoebus Alarm Logger**: is a PV alarm-configured, and its alarm history. `EPICS_MCP_ALARM_URL`.
- **Naming Service**: is a device name registered + ACTIVE. `EPICS_MCP_NAMING_URL`.
- **Phoebus Olog**: logbook search and reading, whole entries (title, text, author, attachments).
  `EPICS_MCP_OLOG_URL`.

The bullets above are grouped by SERVICE, which is how you configure them. The `epics-doctor` report
is grouped by CHECK, and prints one line per plane under the name below. These are the strings to
search for when a report line is unclear, and each one is also the `plane` field of an entry in the
`planes` list of the `--json` output.

<!-- BEGIN:plane-inventory (names only, drift-guarded against the planes run_doctor reports; keep prose OUTSIDE these markers, see tests/test_guide_matches_code.py) -->
`live` · `channelfinder` · `archiver` · `archiver_retrieval` · `alarm` · `naming` · `olog`
<!-- END:plane-inventory -->

The Archiver Appliance is the one service holding two of them, which is why the bullets above list
one archiver while the report prints two lines. They are probed as separate planes: a correct
management URL beside an unreachable retrieval one shows up as one healthy line and one failing
line rather than as a single ambiguous verdict. They are not fully independent, though, and the one
coupling is deliberate: a retrieval URL set while the management URL is empty is reported on the
retrieval line as `config_error` without any probe at all, because every archiver tool gates on the
management URL, so that retrieval URL would never be used.

## Tool palette

The core tools always register. The four **display-aware** tools register only when the optional
`opi_navigation` engine is installed (the `displays` dependency group, a local-checkout
surface that never reaches the published package); on a core-only install they
are simply absent, that is an unmet optional dependency group, not a bug.

<!-- BEGIN:tool-inventory (drift-guarded against @mcp.tool registrations, see tests/test_guide_matches_code.py) -->
**This document, served as a tool:**
`get_guide`

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

### Olog output shape (what a read gives you back)

Entries come back **whole**: `title`, `description`, `owner` (the author), `source`, `properties`
and the raw `attachments` array, plus two derived conveniences, the synthesised
`attachment_count` and name-only `logbooks`/`tags` lists.

⚠️ **A body arrives in two shapes that are not interchangeable, and nothing in the payload marks
which is which.** `source` is the raw body its author wrote; `description` is the plain text Olog
RENDERED from it. So when you edit an entry you just read, **read `source`, not `description`**:
writing the rendering back replaces the whole body with it, whatever the rendering dropped is gone,
and the archived version is not reachable from here (see the edit recipe below). The two do diverge
in practice, measured read-only on a local sandbox Olog (`services/olog_client._expand_log_entry`
carries the figures and their scope); how much a given rendering drops is not decidable from here,
it may be markup, an inline image, or only a blank line.

An entry written by an old client can carry **no `source` at all**. ⚠️ Editing THAT entry's body is
the one shape nothing warns about: the legacy warning fires only when you leave the body alone, and
the read-modify-write warning needs a `source` to compare against.

The former DS-PRIVACY read redaction (author dropped, free text withheld outside a declared local
sandbox) was removed 2026-08-01: it was written against an *assumed* privacy policy that was never
specified for this server, and it cost the logbook its point (a search returned ids whose content
you could not judge, and a write could not verify what it just wrote). If a real facility privacy
specification ever arrives, it will be rebuilt against that specification; the removed mechanism
is in the git history. The privacy consequences of whole reads are stated in `docs/safety.md`.

The client still REFUSES redirects rather than follow them: Olog's REST API has no legitimate
redirect, so a hop is a misconfiguration surfaced loudly. Keeping person data out of COMMITTED
files is a separate, unaffected guard.

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
| separators only (`","`, `";"`, `"\|"`; for `title` also `"+"`) | **dropped** → unfiltered result | **dropped** → unfiltered result |
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
3. **`title` matches whole WORDS, not substrings**: a word fragment finds nothing unless wildcarded
   (`att*`). `find_channels` fails the same way for a different reason: its glob is **anchored**,
   so a bare substring matches nothing there either (wrap it in `*`). Same trap, different level,
   words versus the whole name.
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

### PV write posture (`set_pv_value`): who actually decides

**Two layers decide a write, and this server is only the first of them.** The gate here (the env
switch, the regex allowlist, the rate limit, the audit trail below) decides whether this server
ATTEMPTS the put. Whether the value LANDS is the IOC's decision, through its own access security,
which this server neither reads nor models: nothing in it knows what an IOC permits, and an
allowlisted PV name is a statement about our policy, never a promise about the record. So do not
report a sanctioned write as done because the gate let it through, and do not read a refusal at the
IOC as a fault in this server.

**A refused write can reach you in two shapes, and only one of them carries a verdict.** If the put
RETURNS, the value is read back and compared (see the readback verdicts below), so a value the IOC
accepted and then did not apply reads as `verified=false` with the readback that was actually seen.
If the put RAISES, there is no readback and no `verified` at all: the tool call fails, the audit
line is `FAILED`, and the caller gets an error rather than a result. Do not go looking for
`verified` in that case, and do not read its absence as success.

⚠️ **Which of those two shapes an IOC refusal takes is NOT measured here.** No live test in this
project has an IOC decline a write; the access-security material that exists is static, over the
database file. What is known is where such a refusal would arrive if it raises: the ordinary
put-failure path classifies by message text and has no category of its own for "the IOC said no",
so it would read as a connection-class error rather than an authorisation one. Treat that as the
shape to expect, not as a documented promise.

**So when a write fails and you cannot tell why:** read the PV back yourself with `get_pv_value`
and say what you found. Report both possible causes, a transport problem or a refusal at the IOC,
because this project cannot yet distinguish them, and name the operator rather than choosing one.
**Do not retry.** A `FAILED` audit line does not prove nothing was written, and a retry can
double-apply to hardware; that reasoning is spelled out for `UNKNOWN_PENDING` below and it applies
here for the same reason.

### The audit trail

A `set_pv_value` write leaves a `PV_WRITE` audit line at each stage. A write-enabled server
**refuses to start** unless `EPICS_MCP_AUDIT_LOG_FILE` names a durable path, an ephemeral stderr
audit would lose this trail on restart. The `op=<id>` token that correlates the lines is issued when
a write is dispatched, so the two refusals below carry **no** `op=`: they happen before any
operation exists, and each is a single self-contained line. The stages:

- `event=DENY`: the gate refused it (writes off / not in the allowlist / rate limit); nothing was sent.
- `event=BOUNDS_DENY`: the value gate (O2) refused it: the PV is in the allowlist but the written
  value is outside the record's own drive limits (`control` DRVL/DRVH). Refused **before** the put,
  so nothing reached the IOC; carries the value + limits. **Three cases are not bounds-checked at
  all, and this line then does not appear:** a record with no declared limits, a degenerate range
  (`limit_low >= limit_high`, which includes the two-equal-limits case), and a non-numeric written
  value such as an enum label. The opposite case is `nan`/`inf`: it IS float-coercible, lies outside
  every finite range, and is refused fail-closed.
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
  An **enum** record (a switch, a command, a reset) is compared differently, and this is the lane
  where the verdict carries the most weight, because such a record declares no drive limits and so is
  not bounds-checked either. Its value is the numeric index and its labels ride in a separate `enum`
  block, so a written **label** is resolved against the record's own `choices` exactly as the write
  path resolves it (case-sensitive, first match wins) and then compared by index, exact, with no
  tolerance; `readback` stays the index. Writing the **index** instead stays on the numeric compare
  described above, tolerance included. **Operator consequence:** a command record that clears itself
  after the pulse may already have cleared by the time the readback arrives, in which case it reads
  back its idle state and a command that executed correctly is reported as `READBACK_MISMATCH`
  (whether it does depends on the pulse length against the round trip, so both verdicts occur). That
  line says what the readback saw, not that the command failed: judge the effect of a self-clearing
  command from the record's own status, not from its readback.

Every terminal line **of a dispatched write** shares the `op=<id>` of its `ATTEMPT` (the
`READBACK_*` line too), so an interrupted or misverified write is never a silent gap in the trail.
The two pre-dispatch refusals (`DENY`, `BOUNDS_DENY`) are outside that correlation, as noted above.
(A direct, non-tool call to the audit helpers logs `op=-`.)

### Olog write posture (all four write tools)

Posting to the logbook is the server's first mutating operation against a REST service, so it has its
own gate, distinct from `set_pv_value`, and it never touches `ALLOW_PV_WRITE`. SIX **gate** checks
must line up before a write proceeds, each fail-closed and audited as `DENY` before the raise, and
all six are below, in the order the gate applies them. The first and the fifth are the ones that go
missing: an earlier version of this list said "four" and named four, then said "six" and still
named four, so the sentence announcing the correction was itself the incomplete list.

- **Non-empty target logbooks.** A write that names NO logbook is refused before the allowlist is
  consulted, because `set() <= frozenset()` is true and an empty request would otherwise pass the
  allowlist check unexamined (SEC-3).
- **Env gate.** `EPICS_MCP_ALLOW_OLOG_WRITE=true` (default false = every write denied).
- **Test-server URL boundary.** PV write bounds its reach IN-SERVER: enabling it forces a
  loopback-only EPICS search reach, checked at boot, and the process refuses to start otherwise
  (see the posture section above). Olog cannot work that way, because it speaks HTTP to an
  arbitrary URL, so it gets an explicit boundary instead. A write is refused unless the `OLOG_URL` host is
  **loopback** (the local Olog), or the exact **https** base URL is in
  `EPICS_MCP_OLOG_WRITE_URL_ALLOWLIST` **and** `EPICS_MCP_OLOG_WRITE_ALLOW_REMOTE=true`. A private
  (non-loopback) host, and any plain-http remote (Basic creds are cleartext), is refused by default;
  a production write is a deliberate, auditable double action, and the write session is env-independent
  (a remote's CA comes from `EPICS_MCP_CA_BUNDLE`, not the env). The host is read only from the URL's
  parsed hostname, so a `user@host` trick cannot smuggle a loopback prefix past a production host.
  ⚠️ **Error signature:** this refusal names the VARIABLE (`EPICS_MCP_OLOG_URL`) and never its
  value, so do not expect the address back from it and do not treat its absence as the tool having
  no target. The reason is that the value is an unvalidated string operators do spell as
  `https://user:password@host/Olog`, and the message reaches the caller verbatim. The gate's own
  verdict IS available in band, as `olog_write.target_allowed` in `epics-pv://health`, out of the
  process that answered. The address itself is operator-side only: `epics-doctor`'s `Write gates`
  block deletes the userinfo and drops the query and fragment, and prints it **only when that
  command's own environment arms the gate**, which a shell beside an MCP-launched server usually
  does not; where it cannot PROVE the shown value names the same address it prints `(unparseable)`
  rather than a best effort. ⚠️ That is not the same as the parser refusing the URL, and the
  difference matters when you go looking: `https://svc:p@ss/w0rd@host/Olog` parses fine (host `ss`)
  and is withheld anyway, because a rebuild of it would print part of the password in the path.
- **Logbook allowlist.** Every target logbook must be in `EPICS_MCP_OLOG_WRITE_LOGBOOKS`; an EMPTY
  allowlist with the gate on is **deny-all** (fail-closed). (The PV write pattern is fail-closed too,
  but differently: an empty pattern with writes on is refused at startup, not treated as allow-all.)
- **Attachment size cap.** `EPICS_MCP_OLOG_ATTACH_MAX_BYTES` (default 50 MiB) bounds the TOTAL size
  of an upload, checked from the files' `stat` before any bytes are read and re-checked while
  reading, so a file that grows between the two is still refused. It runs BEFORE the rate limit, so
  an over-limit upload consumes no token.
- **Rate limit + audit.** `EPICS_MCP_OLOG_WRITE_RATE_LIMIT` (low; a logbook is human-paced). The limit
  is enforced ATOMICALLY (the purge/check/record step holds a per-gate lock), so concurrent writes,
  the Olog gate runs under `asyncio.to_thread`, i.e. real worker threads, can never both slip past it
  and exceed the window. The audit line is metadata-only, logbook names, level, title LENGTH, entry
  id, service-account owner, and **never** the `title`/`description` free text.

On the two round-tripping tools (`add_log_attachment` / `update_log_entry`) the gate's checks are
split around the pre-write read: the env gate and the test-server URL boundary run **before** the
target entry is read (a write target the gate refuses is never even read, so a denied caller gets
no entry-existence oracle), and the logbook allowlist runs after it, because it is keyed on the
target entry's own logbooks. Every one of those denials is the gate's own audited `DENY` with
`OLOG_WRITE_DENIED`.

The author (`owner`) is the write service account (`EPICS_MCP_OLOG_WRITE_USER`, a dedicated account,
never a personal login), set server-side from the auth Principal; a caller cannot spoof it. Use a
loopback Olog for testing; the target logbooks must already exist server-side (a non-existent logbook
is an HTTP 400).

### Olog attachment posture (upload + download)

Attachments are the one Olog surface where raw bytes cross the boundary, so the posture has two tiers.

**Upload** (`create_log_entry` / `reply_to_log` with `attachments` = workspace file paths, or
`embed_image_base64`): the entry is sent as `multipart/form-data` to `PUT /logs/multipart` (mirroring
CS-Studio's own client) instead of the plain JSON `PUT /logs`, the no-attachment path is unchanged. It
rides the SAME write gate as a plain post (non-empty logbooks + env + URL boundary + logbook
allowlist + rate limit), plus a
**total-size cap** (`EPICS_MCP_OLOG_ATTACH_MAX_BYTES`, checked from file `stat` BEFORE any bytes are
read and RE-CHECKED while reading, a file that grew past the cap between stat and read is refused,
with at most one byte over budget ever read). Any file type is accepted (only HEIC is refused
server-side, by content-sniff); an over-limit request is the server's HTTP 413. The audit line carries
attachment COUNT + total BYTES, never a filename (a filename is author free text). Each attachment gets
a client-minted UUID; a FILE keeps its own name behind that id (`<uuid>_<name>`), while an inline
image has no name to keep and is stored as `<uuid>.png`, with an image markup pointing at
`attachment/<uuid>` (the CS-Studio inline convention) handed back to append to the description.
Either way a by-name download can never hit the server's duplicate-filename 404. That prefix carries a second, heavier job: because
retention below is filename-keyed and case-insensitive, a FRESH uuid per upload is what keeps a new
file from taking the place of an existing attachment with the same name.

**Attach to an existing entry** (`add_log_attachment` → `POST /logs/multipart`) is a third write tool,
gated identically, with the logbook allowlist keyed on the TARGET entry's OWN logbooks (read first).
It takes `embed_image_base64` as well, not only file paths: the same argument, the same `<uuid>.png`
naming, so an inline image can be added to an entry that already exists.
Olog's update endpoint is destructive, it `retainAll`-prunes any attachment not
resubmitted and overwrites title/body/logbooks/tags/level/properties, so a safe attach
round-trips the target entry's FULL content. The write is purely ADDITIVE for CONTENT:
existing attachments and every content field survive, and only the new file(s) are added.

⚠️ **One field does NOT survive: `owner`.** This endpoint IS the destructive update, so the server
re-stamps the entry's owner with the write service account on every attach; the original author
then exists only in the server-side archived version. Attaching a file to someone else's entry
therefore rewrites its authorship. Same behaviour as `update_log_entry` below.

⚠️ **Attachment retention is FILENAME-keyed, not id-keyed** (measured in the server source, and the
single most surprising fact about this endpoint). `retainAll` tests membership against the SUBMITTED
collection, which deserializes into a `TreeSet`, so the match runs through `Attachment.compareTo`,
which compares `filename` **case-insensitively**. `equals`/`hashCode` do use the id, but a `TreeSet`
never consults them. Two consequences: re-listing an attachment by id alone (filename null) prunes it,
and two attachments whose filenames collide case-insensitively collapse into one, silently, and not
fixable client-side. That is why every round-trip re-lists a non-null filename per attachment, and why
**both** round-tripping tools (`update_log_entry` and `add_log_attachment`) REFUSE an entry whose
attachments cannot survive the match, duplicate, missing, or otherwise unreadable, instead of
writing to it. `add_log_attachment` checks the list it will really SUBMIT, which is the entry's
attachments plus the new upload(s): a new file whose name collides case-insensitively with one
already there is refused too, locally, before the destructive round-trip starts. The re-submitted list and that refusal are derived in the SAME pass, on purpose: when
they were computed separately they disagreed, and an attachment the payload quietly omitted passed the
guard as safe (found by adversarial review, probe-measured, the guard existed and the file was lost
anyway).

**Edit an existing entry** (`update_log_entry` → `POST /logs/multipart` with the `logEntry` part and NO
file parts) changes `title` / body / `level` / `logbooks` / `tags`. Same destructive endpoint, same
gate (env + URL boundary before the read, allowlist after it), with
one difference that matters: the logbook allowlist is keyed on
the **UNION** of the entry's current and resulting logbooks, because moving an entry INTO a logbook and
pulling it OUT of one are both writes to that logbook; gating on either side alone leaves a hole. An
omitted argument means "unchanged", the tool round-trips the whole entry and overlays only what was
passed, so attachments and properties survive. Server behaviours it has to compensate for:
a body edit must carry the new text in **`source`**, which is the field of truth (under
`markup=commonmark` the server regenerates `description` FROM `source`, so a new description beside
a stale source is silently overwritten and the edit vanishes); the client sets **both** fields to
the new text, so the two cannot disagree even if the markup step were skipped; the update does
**not** validate logbook/tag existence the way
create does, so unknown names are checked client-side or they become phantom references; and an
empty title is **not** rejected server-side. Note also that the server re-sets `owner` to the write
service account on EVERY update, the original author survives only in the archived version, and that
editing a legacy entry with no raw `source` makes the server re-render its visible body (reported back
as a warning).

⚠️ **The body you hand it comes from the entry's `source`, never from its `description`** (the read
side of this is under "Olog output shape" above). A read-modify-write that takes the rendering
replaces the whole body with it, and whatever the rendering dropped is gone. Where that is
detectable the tool says so in a `warnings` entry, and the wording names what it CHECKED rather
than what it suspects: the new body starts with the entry's own rendering and NOT with its raw
`source`. That covers the read-it-straight-back and the append shapes. A body rewritten in the
MIDDLE, or prepended to, is the same mistake and passes unseen, and how much a rendering dropped is
not decidable here either, so the warning is a safety net, not a gate and not a damage report.

⚠️ **The archived version is not reachable from this server.** It is cited here and in the code as
the reason a re-stamped owner or an overwritten field is recoverable, but no MCP tool can read or
restore it, recovery is manual, by someone with direct access to the Olog service (its history
API or its datastore). Treat every edit as effectively irreversible from here, and take the
`owner` re-stamp as permanent for any purpose this server can serve.

**Download** (`download_log_attachment`, by `log_id`+`filename`, or by GridFS `attachment_id`) hands
the raw bytes back; `list_log_attachments` lists each attachment's id, filename and
fileMetadataDescription. Worth knowing: the by-id endpoint has NO server-side per-log authorization
(any valid GridFS id grants content). Bytes cross the MCP
boundary as a workspace file `output_path` (a NEW file, `EPICS_MCP_ALLOWED_ROOTS`-checked, and
whether that boundary exists at all is `allowed_roots_set` in `epics-pv://health`, true only for a
value holding a non-blank root and never a claim that the roots are NARROW; it refuses
to overwrite an existing file or follow a symlink, so a download never loses data or escapes the
boundary) or, for a small inline image, base64 in the result. The download body is size-capped by
`EPICS_MCP_OLOG_ATTACH_MAX_BYTES` (a base64 result is capped smaller still, it rides back as response
tokens), so a huge attachment cannot OOM the process.

## Operational recipes (the non-obvious parts)

⚠️ **Bringing a deployment UP is not on this surface, and that is deliberate.** Generating a client
configuration (`epics-init`), reading a report glyph by glyph and scripting its exit code are things
a HUMAN does at a terminal, and they are written for that reader in
[docs/deployment.md](https://github.com/epicDirk/EPICS-MCP/blob/main/docs/deployment.md) section 1
and [docs/tools.md](https://github.com/epicDirk/EPICS-MCP/blob/main/docs/tools.md). Conversationally,
the same walkthrough is the `setup_epics_mcp` prompt. What follows is the other half: what the
services do that no tool description can tell you.

### List archived PVs: `list_archived_pvs` (or the MGMT API directly)
`list_archived_pvs` enumerates the archived PV names, the sibling `is_archived`/`get_pv_history`/
`get_archive_info` tools each require a PV name. Under the hood it calls the MGMT `getAllPVs` (whole
appliance) or, with `this_appliance=true`, `getPVsForThisAppliance` (this member); `capped` is honest
(over-fetch by one). It deliberately does **not** use `getMatchingPVs`, a standard endpoint too, but
one that **may 404 on proxied/split deployments**. Cluster shape: ask `get_appliance_info`, the tool
answers exactly this question without a PV and hands back the MGMT body whole; going at the API
directly it is `getApplianceInfo` / `getAppliancesInCluster`.

**The name filter works on ONE of those two endpoints.** `getAllPVs` honours a `pv` glob.
`getPVsForThisAppliance` has **no name filter at all**, measured, it ignores `pv`, `regex`, `pattern`
and `name` alike and answers byte-identically to the unfiltered call. It does not fail; it hands back
a full, plausible list of the **wrong** PVs, and an honest `capped` makes that read like a fair
truncation. Going at the MGMT API directly, filter only via `getAllPVs`. `list_archived_pvs` refuses
the combination (`INVALID_ARGUMENT`) rather than answer it wrongly; client-side filtering is not a
substitute, because `limit` is applied server-side, filtering a capped page yields an arbitrary
subset behind a meaningless `capped`.

**Finding PVs by archiving RATE (a moderately-sampled test fixture, say):** enumeration cannot see
rates, but `GET /mgmt/bpl/getEventRateReport` answers a rate-sorted `{pvName, eventRate}` list.
Three traps, all measured on a live cluster: `limit` is NOT a row cap (it behaves as if applied per
cluster member, so omit it and filter client-side); the report is near-complete but not the whole
archived set, so a PV's absence from it proves nothing; and the rate is the appliance's own
recent-window figure rather than your target window's, so a band hit is a hypothesis that a real
history fetch in the window you care about has to confirm. Sampling `getAllPVs` blindly finds
nothing, archives being bimodal. `python -m epics_mcp.find_moderate_pv` (read-only, core install)
automates the whole walk.

Also know your SITE'S cluster layout before
enumerating: a facility may run **several archiver clusters split by purpose** (e.g. scalars vs.
large waveforms vs. per-network instances, each with its own MGMT root), the `identity` that
`get_appliance_info` returns tells you which appliance a URL actually is, and enumerating the wrong
cluster silently yields a complete-looking list of the wrong population.

### Verify a deployment's config: `epics-doctor`
<!-- BEGIN:status-prose (the plane statuses named outside the legend below are drift-guarded against PlaneStatus, see tests/test_guide_matches_code.py) -->
The `epics-doctor` CLI (core install) is a read-only self-check: it probes every CONFIGURED plane
(a transport probe, refined on success by an identity probe, up to two requests for a healthy
plane, THREE on the archiver, whose identified appliance is also asked whether it is actually
ingesting; retries on a 5xx add more) and
reports, per plane, whether it is reachable, whether the CA bundle works, and whether the
service **identifies itself as the one that URL should point at**. The ChannelFinder redaction is
printed too, in a `Privacy` block of its own: it belongs to no plane and is not a plane field, so
look for it below the per-plane lines rather than on one of them.
A disabled plane (empty `*_URL`) is reported honestly, never a failure. Exit
`0` = nothing failed and no identity probe failed, `1` = a configured plane HARD-failed
(`unreachable` / `ca_error` / `api_error` / `config_error` / `backend_down` / `disconnected`),
`2` = a usage error,
`3` = INCONCLUSIVE, a plane is reachable but its identity probe FAILED (a served non-2xx like a
401/404, a transport error, or a refused redirect): not a hard failure, but not a silent all-clear
either. Run it first in a new facility to confirm the environment the launcher handed this process
(there is no `.env` file: configuration is read from `os.environ`); add `--probe-pv NAME` to also
pass/fail the live PVA plane against a real PV. Full deployment/config guide:
[docs/deployment.md](https://github.com/epicDirk/EPICS-MCP/blob/main/docs/deployment.md).
<!-- END:status-prose -->

**Reachable is not identified, and identified is not working: read the `?`, `!` and `~` lines.**
The transport probe is a HEAD for ChannelFinder, Alarm, Olog and Naming (the archiver's is a real
GET, see above), and it
counts *any* HTTP response as reachable, so a URL pointing at the wrong host can look alive:
measured, a ChannelFinder URL aimed at a dead container reported `✓ ok` because an unrelated service
on that port answered `401` (blanket auth answers 401 for every path, so the status said nothing
about ChannelFinder). That case surfaces as `!` `identity_probe_failed` (exit `3`), because the
identity probe hits the 401. Each plane is therefore also asked to **name itself**:

<!-- BEGIN:status-glyphs (drift-guarded against cli_doctor._STATUS_MARK, see tests/test_guide_matches_code.py) -->
| Mark | Status | Meaning |
|---|---|---|
| `✓` | `ok` | the service named itself correctly, confirmed |
| `·` | `disabled` | the plane's `*_URL` is unset, so it is honestly OFF: no client is built and no request leaves this process. Reported rather than hidden, and never a failure. Exit stays `0` |
| `i` | `info` | the live/PVA plane has no URL to probe, so without `--probe-pv` there is nothing to connect to. The line reports the POSTURE it would use (provider plus the EPICS search path) instead of a verdict: nothing was probed, so nothing is claimed. Exit stays `0` |
| `?` | `unverified` | reachable and ANSWERED 2xx, but could not prove what it is (a body with no usable `name`, an unreadable/HTML body, or a beacon naming a *different* service, with that name in the detail). **Honest, not healthy**, exit stays `0` |
| `!` | `identity_probe_failed` | reachable, but the identity probe FAILED (a served non-2xx like a `401` auth wall or `404`, a transport error, or a refused redirect on the identity endpoint). Reachable but suspect, **not** a hard failure (the plane's tool endpoints may work), **not** a silent all-clear either: exit `3` (INCONCLUSIVE) |
| `✗` | `config_error` | the configuration is self-contradictory, no probe is even attempted (e.g. `EPICS_MCP_ARCHIVER_RETRIEVAL_URL` set while `EPICS_MCP_ARCHIVER_URL` is empty, every archiver tool gates on the latter, so that retrieval URL is never used). Exit `1` |
| `✗` | `ca_error` | TLS/CA verification failed against this plane's host, so the transport never completed and nothing about the service behind it is known. The remedy is the CA bundle, not the URL. Exit `1` |
| `✗` | `api_error` | the host ANSWERED and the answer was unusable: a served non-2xx carrying its HTTP code, or a 5xx that survived the retries. Reachable AND erroring, which is a different fault from not being reached at all, and the URL may well be pointing at the right host but the wrong webapp. Exit `1` |
| `✗` | `unreachable` | the transport probe failed with no HTTP response chained to it at all: wrong host, wrong port, or nothing listening there. Exit `1` |
| `✗` | `disconnected` | with `--probe-pv NAME`, that PV did not connect within the timeout. The live plane is the only one probed over EPICS rather than over HTTP, and an internal failure of the probe itself is reported here as well rather than being hidden behind a healthy-looking line. Exit `1` |
| `✗` | `backend_down` | reachable AND the service named itself, but a backend it depends on is measurably down, the alarm logger reports its Elasticsearch as not `Connected`, so its search/history tools will fail. Unlike `unverified`, identity IS proven here and the service reports its OWN backend broken. Exit `1` |
| `~` | `no_ingest` | reachable, identity PROVEN, and the service is measurably not doing its job: an Archiver appliance holding channels with **none** connected, or reporting one of its own webapps stopped. The wiring fault this tool exists to catch. Exit stays `0` on purpose (a freshly commissioned or fully paused appliance is legitimately in this state, and a hard failure would cry wolf in every CI job), so it is surfaced instead: this glyph, its own verdict line, and `degraded_planes` in `--json`. Not `?`: that means "we could not tell what this is", and here we can |
<!-- END:status-glyphs -->

Each plane has its own beacon, because they do not share one:

| Plane | Beacon | Identified by |
|---|---|---|
| ChannelFinder / Olog / Alarm | `GET <base-url>` | exact `name` ("ChannelFinder Service", ...) |
| Archiver (MGMT) | `GET /mgmt/bpl/getApplianceInfo`, then `GET /mgmt/bpl/getApplianceMetrics` | the `identity` field, then whether that appliance is INGESTING |
| Archiver (retrieval) | `GET /retrieval/bpl/getVersion` | the product name in `version` |
| Naming | `GET /rest/swagger.json` | `info.title` |

⚠️ Mind the base PATH on the archiver: retrieval serves `/retrieval/bpl`, **not** `/mgmt/bpl`.
Probing the latter on the retrieval port 404s and tells you nothing, that mistake is exactly how an
earlier pass concluded retrieval had no beacon at all.

The archiver is the one plane with a SECOND question, because naming itself proves too little there:
an appliance whose engine has never spoken to its IOCs answers `getApplianceInfo` exactly like a
healthy one. `getApplianceMetrics` is parameterless and appliance-wide, and the row whose `instance`
matches the reported `identity` carries `pvCount` / `connectedPVCount` / `eventRate` directly. Two
things make it a finding (`~` `no_ingest`): channels held with **none** connected, or the appliance's
own `status` reporting a stopped webapp, which is the worse fault and is invisible in the counts
(when the engine webapp does not reply, mgmt cannot merge its numbers, so they vanish from the row
while `pvCount` survives, and the payload is still served as HTTP `200`). Any failure to reach, read
or match the metrics leaves the plane `ok` with the reason in the detail, never a claimed fault.
⚠️ The figures are for **that member only**. A cluster is retrieval-aware (see below), so a query to
one member returns data owned by another, but ingest is per member.

The Naming `/rest/swagger.json` beacon is single-sourced (title + path in `services/naming_identity`)
and now backs **two** surfaces: epics-doctor's naming plane AND `lookup_device_name`'s S13
definitive-negative gate (a 204/404 is trusted only after this beacon confirms the responder, see
"not found vs not registered" below).

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
What is in force in a RUNNING server is `rest_tls` in `epics-pv://health`:
`verification_enabled` resolves the precedence, so a bundle beats a disabled switch and such a
server is not reported as unverified; `ca_bundle_configured` says whether a bundle is set at all
(false does NOT mean certifi, since at the plain default `trust_env` stays on for the read
sessions and an environment `REQUESTS_CA_BUNDLE` can still replace the trust store); and
`https_plane_configured` says whether any plane speaks TLS at all, because verification being on
means nothing where there is no certificate. The bundle PATH is in none of them, and no surface
prints it: that answer is the environment the server was started with.

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

### Discover the alarm config-tree names

Two tools spend a config-tree name and neither can fall back on a guess, the trees being
site-specific: `is_alarm_configured` REQUIRES one outright (no default at all), and `coverage_audit`
refuses loudly as soon as its alarm plane is active without one. No tool enumerates them. The planes
that do have a listing sibling are ChannelFinder (`list_channel_vocabulary`), Olog (`list_logbooks`,
`list_tags`, `list_log_levels`) and the archiver (`list_archived_pvs`); alarm and naming have none.
Derive the names from the history stream instead.

Call `get_alarm_history` **without** `root`, which leaves the search across all trees, and read the
`config` field of each returned event. **Its first path segment is the tree name: split on `/` and
take index 1.** Split on the slash and never on the colon, because a PV name carries colons of its
own, so `"state:/MyTree/Sub/SIM:PS-01:Cur-RB"` splits into
`["state:", "MyTree", "Sub", "SIM:PS-01:Cur-RB"]`.

Three document kinds share that stream, and all three carry the tree in the same position: a state
change (`state:`), a configuration change (`config:`), and an operator acknowledgement (`command:`,
which also carries `user` and `host`). Measured live 2026-08-06. Only the first two were on record
here before, so do not read a two-way state-versus-config split elsewhere in this repository as a
closed set.

**Round-trip the name before trusting it**, positive and negative control in one pass (measured live
2026-08-06): the derived spelling answered `configured: true` for a PV taken out of the same event,
while that same name lower-cased answered `configured: null`. Case matters here and fails quietly.

Four limits, because this returns a SAMPLE and not an enumeration:

- `pv_name` is required and matches as a wildcard substring of the config path, so you see only the
  trees in which that fragment occurs. A short fragment widens the sample deliberately; a tree with
  no activity inside the window stays invisible whatever you pass.
- The window is required as well, and events come newest first under `max_events` (default 100,
  ceiling 999). One busy tree can fill the page and crowd the others out, which makes a single call a
  skewed sample. ⚠ SHORTENING the window does not cure that, and reaching for it first is the natural
  mistake: the sort is by time descending, so with `end` at now a later `start` returns the very same
  newest page. Raise `max_events` first, which is monotone and has a factor of ten of headroom, then
  MOVE the window backwards rather than trimming it.
- ⚠ **Where the name came from decides whether it round-trips.** Without `root` the history search
  sets no index restriction at all and therefore reads every alarm index, while
  `is_alarm_configured` is pinned to the config index alone. So a name lifted out of a `config:`
  event is proven to live there and round-trips; a name lifted out of a `state:` or `command:` event
  is not, because that tree may hold nothing in the config index. A CORRECTLY spelled name then
  answers `configured: null` all the same. The note lists "or an empty tree" as its third cause, so
  read it to the end instead of re-typing the name.
- Feeding the derived name back into `root` as a cross-check is itself server-decided and UNVERIFIED
  (see the alarm-history filters below): a logger that does not support the parameter ignores it and
  BROADENS the result instead of failing, so that check counts only differentially, with one value
  that must match and one that must not. ⚠ It is also blind to one of the three kinds: the server ORs
  `root` over `state:/<tree>*` and `config:/<tree>*` only, so a `command:` document never matches it,
  and a name derived from one can look wrong under `root` while being perfectly right.

## Error signatures → which tool answers

Each signature here is an exception **class and shape**, never a copied runtime string, so match on
the class and the field rather than on wording your server may phrase differently.

⚠️ **This part is the lead only. The signatures are grouped below and each group is a topic key of
its own**, so ask `get_guide` for the group rather than for `errors`: `err-transport` (an address
that reads short, a withheld cause, the CA bundle) · `err-pv` (a PV that will not connect, a device
that may not be registered) · `err-archiver` · `err-alarm` · `err-olog` (reads; a refused WRITE is
`err-gates`) · `err-channelfinder` · `err-rest` (what a 404 may mean) · `err-displays` (the display
and trend tools, and `total: 0`) · `err-arguments` · `err-gates` (both write gates, the read
throttle, and a server that will not start) · `err-guide`.

### A plane fails and the message reads short: redaction, a withheld cause, a bundle that never opened

- **A REST plane fails and the message names a shorter address than you configured.** That is the
  redaction, not a different target: an error names the address WITHOUT its userinfo and without
  its query string, and prints `(unparseable)` where it cannot prove the shown address is the
  configured one minus its credentials. The request itself used the value as configured. The cause
  half is not abridged for a transport failure, which is the only place refused, not-resolved,
  timed-out and TLS-broken are told apart; a served status reads `HTTP <code> <phrase>` in this
  client's own words. A `note` in a SUCCESSFUL payload follows the same rule, and so does an
  `epics-doctor` plane verdict, which had a redaction of its own until 2026-08-14. To read a full
  URL back, look at the server log, not at the answer.
- **A cause reads `<ExceptionClass> (message withheld: it would echo a credential)`.** The rule
  above is enforced on the OUTPUT, not on the configured secret, because the transport rewrites a
  userinfo in flight and a search for the configured value would find nothing. What cannot be
  rewritten is the separator, so any text still carrying an `@` is withheld whole rather than
  patched. **Two causes, and the first is by far the likelier: a credential is spelled into that
  plane's `*_URL` and the parser did not read all of it as userinfo.** Measured,
  `https://svc:p@ss/w0rd@host/Olog` parses with host `ss` and path `/w0rd@host/Olog`, so the
  transport's own text carries an `@` outside the userinfo and the barrier cannot clear it. Move
  the credential into that plane's `EPICS_MCP_*_AUTH` variable and the cause text comes back
  whole; four planes have one (ChannelFinder, Archiver, Alarm, Olog). Otherwise a site built a
  message that skipped the barrier: that is a server defect, the same event is logged at WARNING
  naming the exception class, and it should be reported.
  ⚠️ **An `@` that was never a credential is the trap this rule cannot see by itself**, because it
  is a shape test. One such case is recognised by name and reports `ca_error` instead, see the next
  entry; any other one still reads as a withheld cause.
- **Every configured https plane fails at once and the finding says `ca_error`, naming
  `EPICS_MCP_CA_BUNDLE`.** No service was contacted: the bundle itself could not be read, which
  fails before any handshake. The verdict names the variable and NOT the file path, deliberately,
  because such a path routinely carries an account name; read the value back from the environment
  of the process that reported it. ⚠️ Do not read this as a certificate being rejected, which is a
  different finding with the same status: that one happens after a handshake was attempted, and it
  fails only the planes whose trust root is missing rather than all of them.

### A PV will not connect, and whether the device is registered at all

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

### Archiver: history, which appliance answered, and refusals that do not mean "down"

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
- **An Archiver call answers `ARCHIVER_HTTP_5xx` / `INVALID_TIME_WINDOW` / `INVALID_ARGUMENT`.**
  None of these means the appliance is down: it answered, or the call never left. A 5xx on
  `get_pv_history` is nearly always the time format (this plane reads zone-explicit ISO and nothing
  else); `INVALID_TIME_WINDOW` is a window the plane cannot express (e.g. a relative amount);
  `INVALID_ARGUMENT` is an argument combination that cannot be answered at all. Only
  `EPICS_CONNECTION_FAILED` means unreachable.

### Alarm: configuration, history filters, and why a correct tree name still withholds

- **Alarm configured / history?** `is_alarm_configured` (`configured` is `true`/`false`/`null`;
  `null` = withheld; the recipe "Discover the alarm config-tree names" above says where a tree name
  comes from, and why a correct one can still be withheld) / `get_alarm_history` (`start` + `end`
  required; `pv_name` is matched as a wildcard SUBSTRING of the config path, `Value` matches both
  `...:Temp1Value` and `...:12VValue`). A `false` is a true negative only if the Alarm Logger was
  running at config-import time, otherwise treat it as unreliable; the tool cannot flag this.
- **Narrow an alarm-history query.** `get_alarm_history` has optional SERVER-SIDE filters:
  `root` (config tree name(s), comma-separated), `command` (`Enabled`/`Disabled`, the config
  change that turned an alarm on/off; maps to the `enabled` field and is restricted to config-change
  docs so state events do not swamp it), and `severity`/`current_severity` (the 9 EPICS severities:
  `OK`/`MINOR`/`MAJOR`/`INVALID`/`UNDEFINED`, plus an `_ACK` variant of each of those **except**
  `OK`, so five and four rather than five doubled; there is no `OK_ACK`). ⚠ **UNVERIFIED, and the
  failure mode is silent:** the Alarm Logger IGNORES a query parameter it does not support (its
  parser's default branch is a bare `break;`), so an older logger BROADENS the result instead of
  erroring, a returned set can be wider than the filter implies. Confirm a filter with a
  differential probe (a value that must match vs. one that must not) before trusting it.
- **`is_alarm_configured` answers `configured:null`.** The alarm tree itself returned nothing, so
  "this PV is not configured" cannot be told apart from "that is not the tree name", the answer is
  withheld, and a `note` says so. The tree name is **REQUIRED** (no default, the trees are
  site-specific, so there is no correct universal one; a guessed default silently matches nothing)
  and **case-sensitive** in the query even though the server lower-cases it to pick the index, so a
  mis-cased name (`mytree` vs `MyTree`) selects the right index yet matches nothing. Re-run with the
  tree spelled exactly as the logger stores it. The `config` field in the response echoes your input;
  it is not the server confirming the tree. ⚠ **Spelling is not the only way to land here, so do not
  stop at re-typing the name**, and the note says as much with its third cause, "or an empty tree".
  This tool is pinned to the config index, while the discovery recipe reads every alarm index, so a
  correctly spelled tree that simply holds nothing in the config index answers `null` too. Which of
  the two you have follows from where the name came from: see "Discover the alarm config-tree names"
  above.

### Olog reads: a 401 that is not about credentials, and an empty list that is not empty

- **An Olog search answers 401.** On an anonymous read that is almost never a credentials problem:
  Olog's error dispatch requires authentication, so it returns **401 in place of its own 400**:
  every server-side rejection looks like "unauthorized". Check the query (the time window first);
  a deployment configured with read credentials sees the real 400 and its message.
- **An Olog search answers 200 with an empty list.** Not necessarily "nothing matched", an
  unreadable `start`/`end` degrades to *now* server-side and matches nothing (see the recipe "Olog
  search time window", topic `olog-window`). Re-run with a relative amount (`7 days`) to tell a real
  empty from a dead window.

### ChannelFinder: filters, the anchored glob, and a silent zero that is not a refusal

- **Which IOC/host serves a PV?** `find_channels`.
- **Filter or count channels by property/tag.** `find_channels` takes optional server-side filters
  (`has_properties`/`lacks_properties`/`not_property_values`/`has_tags`/`lacks_tags`) and `count_only`
  for an exact match count via `/count` without pulling the matches. Property filtering is gated to
  the DS-privacy safe-property allowlist (a redacted property like `accessGroup` is refused with
  `INVALID_INPUT`). `EPICS_MCP_CHANNELFINDER_SAFE_PROPERTY_NAMES` **REPLACES** that list, it does
  not extend it: a comma-list becomes the whole allowlist, so naming one extra property silently
  drops the built-in ones, and the same allowlist also decides which properties the RESULTS carry.
  To keep the defaults, list them alongside the new name. How many names the effective allowlists
disclose is `channelfinder_redaction` in `epics-pv://health`, counted through these same
resolvers; the counters say DISCLOSED, so zero is every owner and property redacted rather than
a broken configuration, and the entries themselves stay with `epics-doctor`. Two honesty rules:
  the TAG filter semantics (`has_tags`/`lacks_tags`) are **UNVERIFIED** until a differential live
  probe (the property filters were live-verified 2026-07-22); and a silent 0 is not the same thing
  as a refusal. A property name that is NOT on the allowlist, which every misspelling is, is
  refused **client-side** with `INVALID_INPUT` before the request ever leaves. An **allowlisted**
  name this instance does not carry is **not a server error**, it narrows the result to 0,
  indistinguishable from a genuinely empty match. TAG names are never allowlisted at all, so a tag
  that is merely misspelled is that same silent 0; only a MALFORMED one is refused (blank, leading
  `~`, trailing `!`, or the same tag in both `has_tags` and `lacks_tags`), and that is a syntax
  check, not the allowlist. `list_channel_vocabulary` is what tells them apart:
  it names this instance's
  filterable property keys (the allowlisted subset `find_channels` accepts) and its full tag set,
  so a 0 can be checked against the vocabulary instead of being read as a fact about the registry.
- **Enumerate PVs by pattern?** `discover_pvs` with a wildcard (`*`/`?`) routes to ChannelFinder,
  the same registry `find_channels` queries, so hits are `registered` (registry membership, not a
  live connect; use `get_pvs`/`get_pv_value` for liveness). A concrete name instead does a live p4p
  probe. Wildcard enumeration needs `EPICS_MCP_CHANNELFINDER_URL`; unset, it returns an honest
  'requires ChannelFinder' note, never a bare empty that reads as 'no such PV'.
- **A ChannelFinder glob finds nothing / finds odd casing.** The glob is **anchored**: a bare
  substring (`Ctrl-EVR-01`) matches 0, wrap it in stars (`*Ctrl-EVR-01*`). And it is
  **case-insensitive**: `*Temp*` legitimately matches `...MorTemPrd`, so a hit may differ in case
  from what you asked.

### What a REST 404 is allowed to mean, and what an unreadable 2xx does instead

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

### The display tools: which files they read, and the three ways to get `total: 0`

- **`validate_pvs` reads TWO kinds of file: a `.bob` display and a `.plt` Data Browser trend.**
  A trend is not a screen and the inventory does not pretend it is, it reports the kind on its own
  field; but its trace PVs are real channels and the tool checks them like any other. How a trend
  is REACHED decides which view finds it, and this is the one thing worth knowing before calling:
  embedded in a screen through a `databrowser` widget its traces are attributed to that SCREEN and
  only the file view of the trend finds them here, while a trend opened by an `open_file` button is
  a top level of its own and answers under either view. So on a trend, when in doubt, ask for both.
- **`validate_pvs` refuses a `file_path` with `INVALID_INPUT`.** Two inputs are refused before any
  work: a path whose suffix is neither of the two above, and one that is not under `displays_dir`.
  Both would run the
  full display-inventory walk (tens of seconds on a large dataset) to arrive at a result that was
  already settled: the inventory collects those two suffixes and resolves embed targets against
  that same collected set, so a file of any other kind cannot contribute a PV. The suffix
  comparison folds case,
  `UPPER.BOB` is a display. ⚠ Do NOT read this as "an empty answer means a bad path": a genuine
  display or trend that declares no real `ca`/`pva` channels of its own still answers `total: 0`,
  and that is
  a statement about the file rather than a refusal (though not always a complete one, see the two
  caps below). To check a plain list of PVs with no display involved, pass
  `pv_names` instead; supplying a NON-EMPTY one makes the list win and the file path is not looked
  at. An empty list does not win, the file is read as usual.
- **`total: 0` is not the same as "this screen has no PVs", and the answer says which one it is.**
  There are THREE ways to get it, and they need different follow-ups. **One:** a `.bob` that only
  composes embedded fragments declares nothing itself, so the default file view is empty while the
  display resolves plenty. Every file-mode result therefore carries `shown_by_display` (plus
  `shown_by_display_capped` and `file_path`), and when the file view omits something a note says
  how many channels it omits. Pass `view="display"` to check that set instead.
  Those three fields are the FILE-MODE fields: they travel together on both file-mode answers, the
  empty one included, and they are absent when a NON-EMPTY `pv_names` list wins, because then no
  file was opened to report about. An EMPTY list does not win, so the file is read and the fields
  come back. `file_path` comes back exactly as passed, so an answer can be
  matched to its call; it is not a statement about the file on disk (with a symlink the numbers
  describe the target while the field names the link). Neither view is the right one:
  `view="file"` asks what this file contributes wherever it is used, which is what you want for a
  fragment; `view="display"` asks what an operator opening this screen would see. A fragment
  answers 0 under `view="display"`, because its macros are unbound when it stands alone, and that
  is correct rather than a failure. **Two:** the file DOES declare PVs, but not one of them
  resolved under the per-display context cap. Then a `notes` entry names the cap, and the 0 is a lower
  bound rather than a fact about the file. ⚠ `validate_pvs` has **no** `context_cap` argument, so
  the cap cannot be raised from here; `crossplane_check`, `coverage_audit` and `find_device` do
  take one, and they walk the same inventory, so ask one of those when the fuller picture matters.
  **Three:** a glob cap left the embedded screens out of the expansion, so the file was never
  reached through the parent that binds its macros. That one has its own `notes` entry (see "There
  is a THIRD incompleteness source" below), and ⚠ raising the `context_cap` does NOT help against
  it: no tool exposes a glob cap.
- **The two `capped` signals answer different questions, and the display one is deliberately
  pessimistic.** `shown_by_display_capped` is always the DISPLAY view's verdict. The `notes` cap
  entry belongs to whichever view you asked for: under `view="file"` it is the file verdict, under
  `view="display"` it is the display verdict, so the same sentence means two different things. They
  disagree routinely and neither is a copy of the other. The **file** verdict only fires when the
  file declares a **macro-templated** PV occurrence of its own, because an occurrence with no macro
  resolves to the same channel whatever the budget, so its answer is exact at every cap. The
  **display** verdict carries no such test: measured on a 257-display dataset, 42 displays answer
  an empty display view and still report `shown_by_display_capped: true`, and quadrupling the cap
  grows exactly one of them. So on the display side read a `true` as **"cannot be ruled out"**
  rather than as "known to be incomplete", and read a `false` as the stronger statement of the two.
- **There is a THIRD incompleteness source, and it is why no absence of notes means "complete".**
  Beside the per-display context cap the walk has a **glob cap**: a `<file>` reference that still
  carries a macro is resolved by globbing the known displays, and past the cap the surplus matches
  are left out. That drops whole embedded SCREENS rather than instances, so it can shrink either
  view while both `capped` verdicts above stay `false`. All **four** display tools report it as
  its own `notes` entry, and none of them turns it into a per-file verdict. ⚠ `find_device` was
  the exception until GB-65: it ran the same walk and read neither cap, so a screen dropped by the
  glob cap was simply absent from its answer with nothing saying so. **That restraint is measured,
  not caution.** The engine records the SOURCE
  display of a capped glob, and testing membership in that set looks like the obvious per-file
  flag while missing most of the damage: a cap lift from 50 to 200 on a 2878-display dataset grows
  **7** display views and only **3** of them are named as a source, the largest miss gaining 651
  channels, because a capped glob inside a fragment shrinks every top that embeds it while the
  diagnostic names the fragment. The count itself is 16 distinct (source, target) pairs across 4
  source displays there; on four datasets between 13 and 485 displays the cap never fires at all.
  ⚠ Do not read the entry as "this file is affected" and do not expect a cap argument to help:
  `validate_pvs` exposes neither cap, and the other three display tools take a `context_cap` only.
- **`coverage_audit` refuses "alarm plane, no tree named" with `INVALID_INPUT`.** Same shape as
  above: the verdict follows from the arguments, so it is given before the display-PV walk rather
  than after it. Name the tree (`alarm_config`); there is no correct default, they are site-specific.

### A refusal that happens before any request exists (the tool-specific ones sit with their tool)

- **Every `timeout` is refused at zero or below, on every tool that takes one.** A validation error
  naming the argument, before any request exists. A `timeout=0` did not fail honestly: measured, it
  made `find_device` answer "No operator-facing screen references this device", `validate_pvs`
  report the PV as disconnected, `diagnose_connection` name a cause, and `discover_pvs`/`get_pvs`
  return empty. So repeat any EARLIER `timeout=0` call before believing its "nothing found".

### The write gates, the read throttle, and a server that declines to start

- **`set_pv_value` is refused, and WHICH code it carries is the whole diagnosis.**
  `PV_WRITE_DENIED` means the gate said no: either `EPICS_MCP_ALLOW_PV_WRITE` is not `true` (the
  shipping default) or the name is outside `EPICS_MCP_PV_WRITE_PATTERN`. Nothing was sent, an audit
  DENY line was written, and no rate token was spent, so a retry after fixing the configuration is
  free. `PV_WRITE_OUT_OF_BOUNDS` is a **different event that looks similar**: the gate had already
  ADMITTED the write and the value failed the record's own drive limits (`control` DRVL/DRVH), so
  nothing reached the IOC but a rate token IS gone and the audit line is `BOUNDS_DENY`. Fix the
  configuration for the first, the value for the second. It subclasses the denial, so an
  `except PVWriteDeniedError` catches both and only the code tells them apart.
- **A rate-limit refusal, and the two codes are NOT the same event.** `RATE_LIMIT_EXCEEDED` comes
  from a WRITE gate (PV or Olog), is audited, and means the per-window budget for mutations is
  spent. `READ_RATE_LIMIT_EXCEEDED` comes from the shared REST read throttle in front of every
  GET, is **not** audited at all, and can fire on the reads a WRITE tool performs before its gate
  is even consulted. Reading the second as the first would report a throttled read as a refused
  write that never happened. Both back off in time; neither is a permission problem.
- **An Olog write is refused with `OLOG_WRITE_DENIED`.** One code for all six checks of that gate
  (a named target logbook, the env gate, the URL boundary, the logbook allowlist, the attachment
  size cap, the rate limit), because they are one gate; the message says which fired. It is
  independent of the PV gate: `EPICS_MCP_ALLOW_PV_WRITE` neither enables nor explains it. ⚠️ The
  URL-boundary refusal names the VARIABLE and not its value, deliberately, because that value can
  carry a credential; read the gate's own verdict from `epics-pv://health` (`olog_write`) instead
  of trying to get the address out of the error.
- **The server refuses to START with `SAFETY_CONFIG_INVALID`.** Not a tool error: the process
  declined to come up, which is fail-closed by design rather than a fault. The conditions are a
  malformed `EPICS_MCP_PV_WRITE_PATTERN`, PV writes enabled with an empty pattern, PV writes
  enabled while the EPICS search reach extends beyond loopback, and either gate armed without a
  durable `EPICS_MCP_AUDIT_LOG_FILE`. A server that starts with the gates off never meets it.

### The guide tool itself: your key, or the shipped document

- **`get_guide` refuses a key with `UNKNOWN_TOPIC`, or the guide itself with `GUIDE_DRIFT`.** The
  first is yours to fix: the key does not exist and the message lists every one that does, so no
  second call is needed to find out. Nothing is guessed and nothing falls back to the whole
  document, because a plausible wrong section costs more than a refusal. The second is **not**
  yours: the shipped guide's headings and the topic table disagree, so a key would answer with the
  section next to the one it names. Report it; there is no argument that works around it.
