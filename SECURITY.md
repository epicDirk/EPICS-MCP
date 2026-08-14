# Security Policy

## Reporting a vulnerability

Report privately through GitHub's
[private vulnerability reporting](https://github.com/epicDirk/EPICS-MCP/security/advisories/new)
on this repository. Please do not open a public issue for a suspected vulnerability.

Include what you can: the tool or CLI involved, a configuration that reproduces it (with any
site-specific host or PV name replaced by a placeholder), and what an attacker would gain. This is
a pre-1.0 project with a single maintainer, so expect an acknowledgement rather than a
service-level guarantee.

## Supported versions

Only `main` is supported. There is no backport branch, and pre-1.0 minor versions may carry
breaking changes.

## Security posture

The server is built to be safe to point at a control system without a separate review of every
setting.

- **Read-only by default.** Every mutating tool is off unless explicitly enabled. `set_pv_value`
  needs `EPICS_MCP_ALLOW_PV_WRITE=true` **and** a non-empty regex allowlist of writable PV names
  **and** a per-minute rate limit. Writes enabled with an empty pattern makes the server refuse to
  start, rather than silently permitting every PV.
- **Two independent write gates.** The Olog logbook gate (`EPICS_MCP_ALLOW_OLOG_WRITE`) is separate
  from the PV gate, with its own allowlist, its own rate limit and its own URL boundary. Enabling
  one never enables the other.
- **PV writes require a loopback-only search reach.** A server with `EPICS_MCP_ALLOW_PV_WRITE=true`
  whose EPICS client search environment can reach beyond loopback refuses to start. "Read the
  facility and write the facility" is a start-time impossibility here, not a matter of discipline.
  The check reads the reach with the same parser the real client uses and never trusts a hostname as
  loopback. ⚠️ **PV, not both gates**, and the distinction is measured rather than assumed: this
  refusal and the empty-allowlist one are conditions of the PV gate alone, while the durable audit
  path is required by both. An Olog-write-enabled server starts with the subnet broadcast search on;
  its own boundary is on the write TARGET (loopback, or an exactly allowlisted https URL), not on
  the search reach.
- **Every sanctioned write is bounds-checked and read back.** The value is checked against the
  record's own drive limits before the put; an out-of-range value is refused before it reaches the
  IOC. After the put, the server reads the value back and reports whether it landed, so a silent
  wrong write surfaces as `verified=false` plus an audit line.
- **Mandatory, metadata-only audit.** A write-enabled server refuses to start unless a durable audit
  path is configured, because an audit nobody can read after the process exits is a promise rather
  than a record. The audit carries identifiers, the writing principal and bounded scalars. It never
  carries free text (a title/description body, a filename).
- **No network reach without configuration.** Each optional REST plane (ChannelFinder, Archiver,
  Alarm, Naming, Olog) stays disabled until its `*_URL` is set: unset means no client and no network
  call. PV READ reach follows the standard EPICS search environment, which the launcher controls and
  this server does not. PV WRITE is the exception: enabling it forces a loopback-only reach and the
  process refuses to start otherwise, so that reach is not the launcher's to widen (see above). Run
  `epics-doctor` to see what an instance actually reaches, and to see the effective write posture
  of both gates: its `Write gates` block gives a gate that is OFF, the default for both, one line
  saying so, and an ARMED one what it allows and where a write could go. `--json` carries the same
  under `write_safety`, every field present either way. It reads the environment of the command you
  run, which need not be the one a running server was started with.
- **The server log is deliberately unredacted, and it is a different channel from the answer.** An
  unexpected internal error tells the caller only the exception's class name and puts the full
  message and traceback in the server log, so the bug stays debuggable. That detail can carry a
  service URL as configured, credentials included. **The one measure that removes them from this
  channel is putting credentials in `EPICS_MCP_*_AUTH` rather than in a `*_URL`**; a log level will
  not, because the line that carries them is an `ERROR`. Where that log goes is not this server's
  decision either: it speaks stdio unless `FASTMCP_TRANSPORT` says otherwise (see below), so its
  stderr belongs to whatever launched it. `docs/safety.md` states the exposure with the three
  qualifications that bound it.
  ⚠️ **That measure is unavailable on one plane.** Four planes have an `EPICS_MCP_*_AUTH` variable
  (ChannelFinder, Archiver, Alarm, Olog). The **Naming** plane has none, so a credential it needs
  can only live in its `*_URL`, and for that plane the sentence above has no remedy to offer.
- **The ANSWER is a second channel, and it is closed.** Everything above is about the log. The
  other route out of this process is what a tool RETURNS, and it used to carry the same value: a
  REST failure message named the whole request URL, and two tools put such a message into a `note`
  of a payload they returned SUCCESSFULLY, which a client keeps (`diagnose_connection` and
  `lookup_device_name`). Measured 2026-08-13 across every REST-backed tool: all of them carried a
  configured credential, in one of the two ways. Measured 2026-08-14 after the fix, over the same
  set and both failure kinds: none does. An address is now printed without its userinfo and without
  its query, or as `(unparseable)` where that cannot be proven, and a served status is reported in
  this client's own words rather than the responding server's. **The two channels therefore differ
  on purpose**: the answer is redacted, the log is not, and an operator who treats them alike will
  be wrong about one of them. **Two things happened to `epics-doctor` on 2026-08-14 and they are not
  the same event.** Its write-gate block printed a fragment of a configured Olog password for one
  spelling of `EPICS_MCP_OLOG_URL`, on every run with an armed gate rather than on a failure,
  because it rebuilt the address from the parse; that was a real disclosure and it is FIXED, by
  deleting the userinfo and withholding what cannot be proven. Separately, this document tracked
  the command's own pattern-based redaction as a residual exposure; no unredacted exception was
  found that could reach it, so that entry is withdrawn and the redaction removed rather than
  repaired.
- **Output redaction (ChannelFinder only).** ChannelFinder owners and property values pass a
  site-configurable allowlist. Olog entries come back WHOLE (title, text, author, attachments):
  the former Olog read redaction was removed 2026-08-01 as a deliberate prototype decision, see
  `docs/safety.md` for the stated consequences.

## What this is not

The write gates are a **guardrail on the sanctioned path, not a security boundary.** This is stated
plainly because the word "gate" invites a category error:

- A gate guards writes **through this server**. Anyone with a shell, or with the same EPICS client
  library this server depends on in order to run, can reach the same target without passing the
  gate. That path is outside the gate's reach by construction. It is the gate's shape, not a hole to
  be patched inside the server.
- The real boundary, where one is wanted, lives outside this process: network reach, account
  privileges, an external reconciliation watchdog. This server does not claim to be one, and no
  reader should mistake it for one.
- **One of those boundaries is not hypothetical, and it applies to the sanctioned path too.** Even
  a write this server permits still has to satisfy the IOC's own access security, which decides
  whether the value lands. The gate here is policy and audit over whether the server ATTEMPTS the
  write; nothing in it reads or models what an IOC allows, so an allowlisted PV name is a statement
  about our configuration, never a claim about the record. The `epics-pv://guide` resource states
  what is and is not measured about how such a refusal arrives.
- The audit's promise is therefore *every gate verdict, and every write through this server that
  reaches the I/O*. It is not *every write*.

If you are deciding whether to deploy this in a facility, that distinction is the one to carry into
the review. The full contract every in-server write gate must satisfy, including the deny paths and
their evidence, is in [docs/write-gate-contract.md](docs/write-gate-contract.md).

## Dependencies

The EPICS client is [p4p](https://mdavidsaver.github.io/p4p/), distributed as prebuilt wheels that
bundle the EPICS Base libraries, so no separate EPICS Base build takes part. REST access uses
`requests`. Secret scanning and push protection are enabled on this repository; dependency updates
are watched by Dependabot.

## Release path

A release is the one action here that cannot be undone: a package index never lets a version number
be reused. Four repository settings stand in front of it, measured on 2026-08-14 rather than
intended:

- **No API token exists.** The upload uses Trusted Publishing (OIDC), bound to this repository, the
  `publish.yml` workflow and the `pypi` environment, so there is no credential to steal from here.
- **Only an administrator can create a `v*` tag** (ruleset `release-tags`, rules `creation`,
  `update`, `deletion`, `non_fast_forward`).
- **The upload waits for an approval** on the `pypi` environment, whose deployment policy admits tag
  refs matching `v*` only. A job referencing an environment does not start until its rules pass, so
  no OIDC token is minted before that approval.
- **Third-party actions are allowlisted.** Only GitHub-owned actions plus `astral-sh/setup-uv` and
  `pypa/gh-action-pypi-publish` may run.

Two limits, because the list above reads stronger than it is. `prevent_self_review` is false, so the
account that pushes the tag approves its own deployment: this is a stop against the accident, not a
second independent factor. And repository administrators may bypass the approval entirely, which is
GitHub's default and is not settable through its REST API.

`main` carries a ruleset that blocks force pushes and deletion. It deliberately does **not** require
pull requests or status checks, so a red commit can still reach `main`; the release workflow
therefore runs the full lint and test chain itself rather than trusting a separate CI run.
