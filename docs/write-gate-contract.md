# Write-gate contract

What any in-server write gate must provide. This is a **specification**, consulted when a gate is
built or reviewed, not a rule that applies to every session. It lives here rather than in
`CLAUDE.md` so the standing conventions stay short, and it is linked from
[CLAUDE.md](../CLAUDE.md), [CONTRIBUTING.md](../CONTRIBUTING.md) and [SECURITY.md](../SECURITY.md).

This server exposes tools that **mutate** state on a downstream service: a PV setpoint, a logbook entry, an
archive/config record. Every such tool sits behind an **in-server write gate**. Today there are two (the PV
gate and the logbook gate); a third (an archiver, an alarm-acknowledge, or a channel-registry mutator)
repeats every question below. So the requirements are written once, here, as the standing contract. A new
gate that does not meet all six points is not done.

Read the scope statement (point 6) **first**: it fixes what a gate is *for*, and the other five points only
make sense under it.

⚠️ **Three numbers circulate about the gates, they are different questions, and confusing them is the
one documented way this page gets misread.** They are written out once, here, so a text elsewhere can
name its own:

| Number | What it counts |
|---|---|
| **six** | the REQUIREMENTS below. A specification every gate must meet, not a count of anything a gate does at runtime. |
| **three** | the PV gate's per-write CHECKS: env gate, name allowlist, rate limit. |
| **six** | the logbook gate's per-write CHECKS: named target logbooks, env gate, URL boundary, logbook allowlist, attachment size cap, rate limit. |

The two sixes are unrelated and land on the same value by accident: the requirements map onto the
logbook gate's checks neither one-to-one nor in order, and two of that gate's checks (the empty-target
guard and the size cap) answer no requirement at all, while requirement 4 (audit) is no check.

A gate's **start conditions** are a fourth category again and belong to neither count. The PV gate has
**five start conditions** of its own, every one of them a refuse-to-start, and they are given here as a
list rather than as a figure somewhere else, which is this page's own rule three paragraphs down:

1. the PV-name allowlist pattern must COMPILE;
2. writes enabled with an EMPTY pattern is refused, never read as an allow-all;
3. writes enabled while the EPICS client search reach extends beyond loopback is refused;
4. `write_rate_limit` must be a usable window size;
5. a configured `EPICS_MCP_AUDIT_LOG_FILE` must be openable for writing.

⚠️ **Four of them, and only four, can be reached from a configuration**, which is why a text about what
an operator has to get right says four where this page says five. Number 4 is the exception: `EpicsConfig`
constrains that field to `ge=1`, so no environment produces a bad value and only a caller that bypassed
validation with `model_construct` ever meets it. This was measured for [GQ-130] after two texts said four
and this one said five; they were not counting the same population, and neither said which it counted.
The order above is the order `SafetyLayer` applies them TODAY, which is what makes "the third start
condition" elsewhere refer to the reach; nothing compares that ordinal to the code.

The logbook gate is built lazily, so its equivalents (a bad rate-limit value, an unwritable audit
sink) surface at the FIRST WRITE rather than at boot, and the only condition that stops the process
itself is the durable audit path both gates share.

So: a text that says "triple-gated" about `set_pv_value` is correct, and one that lists five things
after it is not a contradiction but a different category. Say which of the four you mean, or give the
list rather than a number.

⚠️ **And say it in ONE spelling.** The PLURAL `<word> start conditions` always names the TOTAL; a
subset is written "N of the five start conditions"; the SINGULAR says which one. `test_write_gate_contract.py`
compares every counted plural claim in the tracked tree to the code. It does **not** compare the list
above to the code: remove an item from it and leave the figure standing, and nothing goes red.

**1. An environment on/off gate, default OFF.** Writes for the surface are disabled unless an explicit
`EPICS_MCP_ALLOW_<SURFACE>_WRITE=true` is set. Off is the shipping default and the safe state; a fresh
checkout, a missing env file, or a typo in the var name all resolve to *no writes*. Each surface's env gate
is **independent**: enabling one never enables another.

**2. An allowlist with explicitly-defined empty-semantics: empty is fail-closed, and *which* fail-closed
shape is a deliberate, documented per-surface choice.** The env gate only says *whether* the surface may be
written; an allowlist says *what* target is permitted: a regex over target names (`^SIM:PS-01:.*-SP$`), an
exact set of logbook names, a URL allowlist.

- **An empty allowlist is NEVER a silent allow-all.** On every surface it fails closed. An operator who
  genuinely wants every target writable must say so **explicitly** (a regex allowlist: `.*`; an exact-set
  allowlist has no "all", so an empty set can only mean deny). A silent allow-all on empty is a footgun (a
  forgotten env var reading as "permit everything") and is a defect, not a valid configuration.
- **The *shape* of the fail-closed is a per-surface decision, made deliberately and documented at the config
  field itself.** Two sanctioned shapes exist:
  - **Refuse-to-start**: writes enabled with an empty allowlist raises a config error at gate construction,
    so the process will not run write-enabled-but-unscoped. Use this where an empty allowlist can *only* be a
    misconfiguration (a forgotten pattern) **and** there is a way to spell "all" explicitly.
  - **Deny-all at runtime**: the gate constructs, logs a loud warning that writes are enabled but nothing is
    allowlisted, and denies every write. Use this where deny-all-with-a-visible-error is the better operator
    experience (a wrong or missing target surfaces as a denied write, not a dead process).
- **Never copy a sibling gate's empty-semantics blindly, and never describe one gate in terms of another's.**
  The choice is load-bearing and must be re-decided for each surface, with its rationale written **at that
  surface's config field**, not inferred from whichever sibling the author read first. In particular: do
  **not** propagate any cross-gate comment of the form *"the inverse of the other gate, where an empty
  allowlist allows all."* Such a description is **inaccurate** (a name-pattern gate that refuses to start on
  an empty pattern does **not** allow all), and copying it forward is exactly how a third gate inherits a
  *fail-open* reading of "empty". Both existing gates are fail-closed on empty; they differ only in shape, and
  each states its own shape and reason at its own field.

**3. A rate limit.** At most N writes per fixed window, enforced in-server. This bounds the blast radius of a
runaway loop: the failure mode where the caller is wrong and issues the same mistaken write repeatedly.
Acquisition of a rate token (purge-expired → count → append) must be **atomic** under a lock if the gate can
run on more than one thread, and a write **denied by the gate** must never consume a token (audit-then-raise
happens before the token is appended). A refusal that happens *after* the gate has admitted the write (a
value-bounds check that runs on data fetched post-admission, say) legitimately *has* consumed one; that is a
different event, and it says so where it is implemented.

**4. A mandatory, metadata-only audit.** Every gate verdict, and every write that reaches the I/O, leaves a
**durable** record, and the record carries **metadata only, never free text**.

- **Mandatory core (every gate):** exactly one **terminal** line per **gate verdict**: **ALLOW** (the write
  completed), **DENY** (a gate precondition rejected it, so record the reason code, emitted before the raise),
  or **FAILED** (it passed the gate and failed downstream). A surface may define **further terminal events**
  where its semantics genuinely differ (the PV gate's post-admission bounds refusal is its own event, not a
  DENY, precisely because it already consumed a token); enumerate those where the gate is described instead
  of filing them under one of the three.
- **Scope this claim honestly.** The promise is *one terminal line per **gate verdict***, **not** "no write
  request is ever un-recorded". A refusal raised **before** the gate is consulted (a precondition in the
  tool or service layer above it) writes **no audit line at all**, and such refusals exist in this server
  today. Which forces the next rule:
- **A refusal raised outside the gate must not carry the gate's error code.** If a caller cannot distinguish
  an un-audited pre-gate refusal from a real, audited gate DENY, the audit's coverage claim becomes
  unfalsifiable from the outside: the exact silent gap the audit exists to close. Give pre-gate refusals
  their own code.
- **Stronger shape, REQUIRED where the mutation can be interrupted mid-flight**, where the code that issues
  the I/O can be abandoned by its caller while the I/O itself proceeds (an async put handed to a worker that
  a cancellation does not stop, so the value may still land). Then: an **ATTEMPT** line emitted **before** the
  I/O carrying a **correlation id**, plus an **UNKNOWN** terminal outcome (cancelled or timed out; may or may
  not have landed: verify by read-back, never blindly retry). Where the gate, the I/O and its terminal audit
  all run to completion inside one unit that a caller's cancellation cannot split, ATTEMPT/UNKNOWN are
  **optional**. **Do not hard-code one surface's audit shape as universal**: decide it per gate, like point
  2, and write down the reason. *(Known limit: "a caller cannot split it" is not "it cannot be interrupted".
  A transport-level timeout inside such a unit can still leave an applied write reported as FAILED. Where
  that is so, say it at the gate.)*
- **Metadata only.** The record may carry identifiers (target name, entry/operation id), the **principal** the
  server records as the writing account, any correlation id, and bounded scalars appropriate to the surface (a
  numeric setpoint's old/new value, a title *length*, an attachment *count* and *byte total*). It must
  **never** carry free text (a title/description body, a filename): the audit is a durable, appended
  record, and authored content does not belong in it.
- **The audit sink is validated at gate construction and fails closed**: a broken or unwritable sink is a
  config error, never a raw exception at the first write. *When* that validation runs follows the gate's own
  construction: at startup for an eagerly-built gate, at first write for a lazily-built one. An audit
  emission must never turn a denial or failure into a crash.
- **Know where the audit goes, and it is not optional.** A default sink (e.g. stderr) is ephemeral and
  un-persisted; an audit nobody can read after the process exits is a promise, not a record. So, separately
  from the per-gate validation above and unconditionally at startup: a write-enabled process **refuses to
  start** unless a durable sink is configured. Document the effective sink.

**5. Where the surface has network reach: a reach / URL boundary, enforced fail-closed.** A gate whose target
can be an arbitrary host must confine *where* a write can physically go, in addition to *what* may be written
(points 1-2):

- **When the target is fixed at construction** (a client search reach): confine it at construction: writes
  enabled while the search reach extends beyond loopback is a config error; refuse to start a write-enabled
  process that could reach a real facility network. The check must be **parser-faithful** (it reads the reach
  exactly as the underlying client will) and **resolution-free** (a hostname is never trusted as loopback).
- **When the target is per-write** (an HTTP URL like `http://logbook:8080`): confine it at each write: permit
  a loopback host, or an **exactly-allowlisted** URL over a confidential scheme with remote writes explicitly
  enabled, and take the host from **the same parser the client will actually connect with**, never a second
  parser and never a substring of the authority (`http://127.0.0.1@logbook-remote/...` has host
  `logbook-remote` and is refused). **Two URL parsers do not agree on hostile input:** a URL that one library
  reads as a loopback host, another reads as a remote one. A boundary validated with a *different* parser
  than the one that opens the connection is a bypass waiting to be found, so parse with the connecting
  library's own parser, not with whatever the standard library offers.
- **Fail-closed on the boundary itself:** an unparseable or unresolvable target is **denied first**, before
  any allowlist check: a bad target is a clean, audited DENY, and the allowlist can never override a target
  that fails to parse.
- **The refusal must not hand the caller a target that can carry a secret.** A per-write URL is an
  unvalidated configuration string, and `https://user:password@host/path` is a spelling operators use, so a
  refusal that echoes it back discloses a credential on the gate's most ordinary path: "not allowlisted" is
  the boundary's normal state, and no service has to be down for the message to be produced. Name the
  VARIABLE, point at the diagnostic command, and let the address be read from the surface built for it.
  ⚠️ **Stated per data class, not as a ban on naming targets**, because the two are different questions: a
  PV name or a logbook name cannot carry a credential, and both gates deliberately DO name those (the PV
  allowlist miss names its PV beside an escalation sentence; the logbook allowlist names the refused
  logbooks). Which of its refusals may name their target is a per-surface decision each gate makes and
  writes down, exactly as point 2 requires for empty-allowlist semantics. Do not redact the value instead
  of omitting it unless the redaction is proven for the shape being printed, and "proven" means measured
  over spellings rather than argued: of this server's three redactions, one leaves a query-string token
  in place, one REBUILDS the address and was measured to print a fragment of a password in the path for
  `https://svc:p@ss/w0rd@host/x` (the parser reads the host as `ss`), and only the third both deletes and
  asks the parser to confirm the result names the same address, withholding where it cannot. The first
  two each read as safe until the spelling that breaks them was tried, which is the point of this
  sentence. Deciding to omit rather than to
  redact was, in this server's case, settled by a posture it already held, that the logbook target
  address is disclosed to a caller on no surface at all.
  ⚠️ **Scope, because the sentence above is about what the GATE produces and a caller can be handed the
  target on the way to a gate verdict.** A gate whose verdict needs a pre-write READ (the logbook
  allowlist is keyed on the target entry's own logbooks) performs that read first, and a transport
  failure there is reported by a different layer with its own contract, not this one's. That layer
  now names the address without its userinfo (measured 2026-08-14), so the pre-write read no longer
  hands a caller what this point withholds; state the boundary where the gate is described anyway,
  rather than letting a reader infer that passing this point is what makes the tool
  credential-free.
  Guarded by `tests/test_write_gate_contract.py`: every registered deny row must produce a refusal with
  no address-shaped fragment and no configured service host in it, message and `details` alike, with no
  per-row opt-out (a field that could be set to "not applicable" was tried and a mutant switched it off
  while a credential sat on the wire).

**6. An honest scope statement: the gate is a guardrail on the sanctioned path, not a security boundary.**
State this plainly where the gate is described, because the word "gate" invites a category error:

- The gate guards writes **through this server**. A caller with a shell, or with the write library the server
  itself depends on to run, can reach the same target **without passing the gate**. That path is **out of the
  gate's reach by construction**: it is the gate's *shape*, not a hole to be patched inside the server. The
  real boundary, if one is wanted, lives outside this process (network reach, account privileges, an external
  reconciliation watchdog); the gate does not claim to be it, and no future reader should mistake it for one.
- Therefore the audit's promise is **"every gate verdict, and every write through the server that reaches the
  I/O,"** not "every write". Scope every completeness claim that way; a claim of total coverage would be the
  very silent gap the audit exists to close.
- **The downstream service authorises too, and a gate owes the caller a way to SEE its verdict.** Passing the
  gate means this server ATTEMPTS the mutation; whether it takes effect is decided by the target (an IOC's
  access security, a logbook service's account permissions), which no gate here reads or models. So an
  allowlisted target is a statement about our configuration, never a claim about the target's own rules.
  Two duties follow, and the second is the load-bearing one:
  - **Say it where the gate is described**, in the same terms. Measured on the PV gate: its tool description
    had drifted into claiming to be "what actually gates the write" in two separate sentences, which is the
    same category error as the one this point opens with, one layer further in. Enumerate the surfaces the
    way point 4 does rather than recalling them; for the PV gate they were three (the tool description, the
    operator guide, `SECURITY.md`).
  - **Make the RESULT carry whether the mutation took effect**, because a caller cannot otherwise tell a
    permitted-and-applied write from a permitted-and-refused one. The PV gate does this with the always-on
    readback (`verified` / `readback` / `tolerance`), the logbook gate with the server's echoed entry. A new
    surface names its own equivalent, or states here that none exists, what a caller should do instead, and
    why the target offers nothing to read back. ⚠️ State the SHAPE too: on the PV gate the verdict only
    exists when the write itself returned, so a mutation that raised carries no verdict at all, and a
    description that promises the field unconditionally is wrong on exactly the path that matters.
- **Every deny-path must be verifiable, and a guard that cannot be driven red is the defect.** Each denial the
  gate can raise (env off, allowlist miss, boundary reject, rate limit) must be exercised by a test that can
  be shown to **fail on the un-gated code or on a mutant**: the evidence-discipline rule applied to the
  gate. Note that the mutant must actually target the assertion: for a rate-limit denial, deleting the deny
  branch does not exercise "a denial consumes no token"; appending the token anyway does. Prefer a *live*
  deny-path test (one that goes red against a real target when the env points at a service) over an
  in-memory assertion alone: an in-memory-only deny test proves the branch, not that the gate fires against
  the wire. A gate whose refusals are only asserted in prose, never observed going red, has a documented
  promise, not a verified one.

**Honest limit:** points 1-5 are enforceable in code and are drift-guarded per gate by
`tests/test_write_gate_contract.py`; point 6 and the empty-semantics *rationale* of point 2 are **prose**,
the category that rots. No CI check proves a future gate author re-decided the empty-shape deliberately or
scoped the audit claim honestly; the guard there is the review plus the red-provable deny tests of point 6.

Point 6's own *live* preference, stated per surface rather than as one number:

- **PV gate: not applicable, and that is a reason, not an excuse.** Every one of its deny paths raises
  *before* the first network I/O. A live deny test would execute byte-identical code beside an unused
  socket; the in-memory rows are the honest coverage there, not a lesser substitute.
- **Olog gate: one path proven against the wire, the rest reasoned away.** `olog_allowlist_miss` on
  `add_log_attachment` / `update_log_entry` is live-covered by `tests/test_write_gate_live.py`. It is the
  only deny path whose *decision input* comes from the server (the target entry's logbooks, read back over
  HTTP), i.e. the only one a mock cannot falsify, because the mock supplies the very payload shape the code
  assumes. The other Olog paths are argued out one by one in that module's docstring (env-off and the URL
  boundary deny before any client exists, pinned in-memory by
  `test_round_trip_write_is_gate_denied_before_any_read`; empty-logbooks is unreachable
  there; the size cap has no server-side input; a rate-limit probe would need N real mutating writes).
- **Not claimed:** that every gate refusal has been observed against a real service. It has not, and the
  list above says which ones and why, recorded rather than papered over.

