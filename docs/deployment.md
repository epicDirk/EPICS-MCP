# Deploying the EPICS MCP server in your facility

This server is **facility-agnostic by design**: every service URL and network setting is an
`EPICS_MCP_*` environment variable, never a hard-coded site. Deploying it in a new facility means
setting those variables for *your* services, with no code change. This guide walks through that, and
`epics-doctor` confirms it for you.

[Back to the README](../README.md)

**This guide starts after installation.** It assumes the package is installed and its commands
answer on your PATH; if `epics-doctor --version` does not print a version, install it first
(README, "Installation") and come back.

> **Config lives in the environment, not in a file this server reads.** Set the
> variables in whatever LAUNCHES the server: the `env` block of your MCP client's `.mcp.json`, a
> systemd unit's `Environment=` / `EnvironmentFile=`, a container spec, or your shell.
> `.env.example` is a commented REFERENCE of every recognised variable, not a file the server
> loads. There is no separate config-file format to learn.

Two consequences of that, worth having before you start. **Nothing on disk named `.env` is ever
read**: the server takes its configuration from the process environment, so a variable that does not
reach the process has no effect and says nothing. And **every service URL is optional**: an unset URL
disables that plane, with no network call and no complaint. The one exception is
`EPICS_MCP_ARCHIVER_RETRIEVAL_URL`, which falls back to the mgmt URL rather than switching
retrieval off, because a single-JVM appliance legitimately leaves it empty.

**What an install actually puts on your machine.** Two things belong to this package, and the layout
below is what a `pip install` into a fresh virtual environment produces, measured rather than
sketched.

⚠️ Two honest qualifications before you read it. An install also pulls the **dependency closure**,
which is not small: p4p brings the EPICS core libraries and numpy, the MCP framework brings its own
stack, and a fresh environment measured here held **82 installed distributions**, this package and
`pip` itself included. Count the same way before you call a difference drift: that figure is what
`pip list` reports, it is platform dependent, and `site-packages` holds roughly twice as many
ENTRIES, because each distribution leaves a `.dist-info` directory beside its package and a few
leave a `.pth` file, a loose module or a help file that is no package at all. And **where the
commands end up depends on the installer**: a virtual environment puts them in its own `bin`
(`Scripts` on Windows), reachable while it is active, whereas `uv tool install` and `pipx` expose
them from a separate tool directory that has to be on your PATH (`uv tool update-shell` arranges
that for uv).

```text
    <the environment you installed into>
         |
         + bin/  (Scripts\ on Windows)     the seven commands this package adds
         |    |
         |    - epics-mcp          the MCP server itself, started BY your client, not by you
         |    - epics-init         write a client configuration and check it, in one step
         |    - epics-testpv       serve two synthetic PVs, so a first try needs no IOC
         |    - epics-doctor       read-only self-check of every configured plane
         |    - epics-diagnose     why one PV is connected, or is not
         |    - epics-crossplane   display to IOC to Naming     (needs the display engine)
         |    - epics-coverage     coverage matrix              (needs the display engine)
         |
         + <site-packages>/epics_mcp/      the package itself
              |
              + services/           one client per service, and the doctor's probes
              + tools/              the MCP tools, grouped by plane
              - operator_guide.md   the operational cookbook. It SHIPS: the same file is served
              |                     to an assistant as the epics-pv://guide resource, so the
              |                     knowledge travels with the install rather than with the repo
              - py.typed            the marker that says the type hints are real
```

**What is NOT installed**, because it lives in the repository only: `.env.example`, `examples/`
and this `docs/` tree. So an instruction to open `.env.example` is performable from a checkout and
nowhere else; from an installed copy read [the configuration reference](configuration.md) instead,
which lists the same variables with their defaults.

**What you create, and no installer ever touches:** your client's configuration file (section 1
writes it for you), an audit log if you enable a write gate, and a CA bundle if one of your
services uses an internal root (section 3). The MCP framework also writes a small version-check
cache of its own under its home directory, which `FASTMCP_CHECK_FOR_UPDATES=off` prevents.

## 1. Bring it up

1. **Start from a preset.** `epics-init` prints the client-configuration block for one of four
   deployment shapes (`sandbox`, `ioc-only`, `ioc-archiver`, `full`) and then runs the self-check of
   step 2 against exactly that block, so generating a configuration and checking it are one step:

   ```bash
   epics-init --list                                        # what each shape sets, and why
   epics-init --preset sandbox --probe-pv <a-connected-pv>   # loopback, nothing to fill in
   ```

   `sandbox` is the only shape that leaves no placeholder behind, so it is the cheapest way to watch
   the machinery run before any of your own hostnames are involved.

   ⚠️ Read what its check does and does not say, and note that the command says this itself: without
   `--probe-pv` no PV is contacted, so a clean report means nothing is MISCONFIGURED, not that
   anything WORKS. That is why the `--probe-pv` form is the one above.

   ⚠️ The shape is named after a deployment, not after the git-ignored `sandbox/` directory in this
   repository, which is a contributor-only test stack and is absent from every clone.

   For a facility, pick the shape that matches:

   ```bash
   epics-init --preset ioc-archiver     # prints the block, and names what is still unfilled
   ```

   It names every placeholder it still carries, three of them for `ioc-archiver`, and it SKIPS the
   check until none are left rather than reporting a failure you cannot act on. Replace them in the
   printed block, or pass each one as its own `--set NAME=VALUE`; the check runs as soon as none
   remain. `--no-check` skips it outright.

   ⚠️ If any of your HTTPS services uses an internal CA, add `--set EPICS_MCP_CA_BUNDLE=<path>` here
   as well (section 3 explains the bundle), or this first check reports `ca_error` for that plane and
   you have to run it again.

   The block goes to stdout and everything the command has to SAY goes to stderr. Where the block
   belongs is in [MCP client integration](mcp-clients.md), together with the restart that finishes
   the job.

   ⚠️ **Do not save it with a shell redirect.** `>` writes the encoding your shell prefers, and this
   is JSON a client has to parse; in Windows PowerShell 5.1 that means UTF-16 with a byte-order mark,
   which no strict parser accepts. Let the command write the file instead:

   ```bash
   epics-init --preset sandbox --out .mcp.json
   ```

   It writes UTF-8 with LF whatever the shell is, refuses an existing file unless you add `--force`
   (a client configuration usually holds other servers), and writes nothing at all while placeholders
   remain, so the fill-in-and-run-again loop above stays open. Add `--absolute-command` if your client
   cannot resolve a bare command name, which is the single most common reason a correct-looking block
   still does not start; see [MCP client integration](mcp-clients.md).

2. **Run the self-check again whenever you change the configuration by hand.** ⚠️ `epics-doctor`
   reads the environment of the process YOU start, not the `env` block of a client-configuration
   file. Run it with the same variables set in your own shell, or it will report a healthy nothing
   and mean it. Step 1 avoids that trap by design: `epics-init` puts the block it just printed into
   the environment and checks THAT.

   ```bash
   epics-doctor --probe-pv <a-connected-pv>   # the REST planes, plus one live PV: pass or fail
   epics-doctor                               # the same, leaving the live plane uncontacted
   epics-doctor --json                        # machine-readable, for CI
   ```

   Prefer the `--probe-pv` form: without it the live plane is only REPORTED, never contacted, so a
   clean run says nothing about whether you can read a PV. To see one PV in detail, including the
   value it holds, use `epics-diagnose <a-connected-pv>`.

   Reading the report, glyph by glyph: `✓` is a plane that passed, `·` one you have not configured,
   `i` a line that informs rather than judges (the live plane, when you did not ask it to probe),
   and `✗` a failure. Three further glyphs are honest rather than healthy, and they are worth
   knowing before you call a deployment done; they are further down this step.

   The exit code is the scriptable contract:

   | Exit | Meaning |
   |------|---------|
   | `0` | nothing failed, and no identity probe failed |
   | `1` | a configured plane HARD-failed: `unreachable`, `ca_error`, `api_error`, `config_error` (the variables contradict each other, e.g. a retrieval URL with no archiver URL), `backend_down` (identified, but a backend it needs is down), or `disconnected` (only with `--probe-pv`). ALSO an internal error in the check itself, which shares this code deliberately and is the only case that writes a `doctor:` line to stderr and produces no report |
   | `2` | usage error (bad arguments) |
   | `3` | INCONCLUSIVE: a plane is reachable but its identity probe FAILED, a served non-2xx like a 401/404, a transport error, or a refused redirect |

   Every line that reports a problem also carries its remedy: what was observed, then what to
   change. So the common cases need no lookup here, and the sections below are for when you want
   the reasoning behind one. Fix what `epics-doctor` flags and your service configuration is done.

   The report also carries a `Write gates` block, which is the fastest answer to "can this server
   write anywhere, and where". A gate that is OFF, which is what you have here unless you went out
   of your way, gets one line saying so and that is its whole answer; an ARMED one adds what it
   allows by name, its rate limit, and the reach or target a write would use. The audit log and
   whether it can be appended to are named either way. `--json` carries it as `write_safety`.

   ⚠️ Note what that does NOT cover, because a clean report is easy to read as "everything works":
   the doctor probes the seven service planes, never the LAUNCH. Whether your client can start this
   server at all, and whether `epics-mcp` resolves in the environment the client launches from, are
   separate questions with separate symptoms, in [Troubleshooting](#6-troubleshooting). So is
   whether a write-enabled block will boot: the write block above reports what the gates are SET
   to, it does not evaluate the start conditions, and its audit line answers only one of them.

   ⚠️ Three states are honest rather than healthy, and none of them fails. Read them before
   calling a deployment done. `?` (`unverified`) answered 2xx but could not prove what it is, exit
   `0`. `!` (`identity_probe_failed`) is reachable but suspect, exit `3`, and it carries a remedy.
   `~` (`no_ingest`) proved what it is and is measurably not doing its job, an Archiver appliance
   holding channels with none connected or reporting one of its own webapps stopped; exit `0` on
   purpose, because a freshly commissioned appliance is legitimately in that state. A URL that
   answers with a *different* known service's name is `unverified` too rather than a failure, since
   a path-based reverse proxy can serve the real API behind a base URL naming another service
   (measured). The found name rides in the detail and is your first clue when the config IS wrong.

   Every plane has its own identity beacon (see the operator guide). Scripting this? Read
   `verification_complete` / `unverified_planes` / `inconclusive_identity_planes` /
   `degraded_planes` from `--json`, and `write_safety` for the write posture (that one never moves
   the verdict, so a script that only wants pass or fail can ignore it). A failed probe lands in
   `inconclusive_identity_planes`, not in
   `unverified_planes`; a degraded one in `degraded_planes` and in neither of those. The exit code
   alone says "nothing failed", not "everything confirmed", so for positive confirmation assert
   `identified_planes` is non-empty: `verification_complete` is vacuously true on an empty config,
   and a degraded plane is listed there too, since its identity IS proven.

**No preset matches your facility?** Then set the variables by hand, which is what the presets are
made of anyway: section 2 lists them plane by plane. Copy the lines you need from `.env.example` (in a
checkout) or from [the configuration reference](configuration.md) (from an installed copy) into
whatever launches the server, then run step 2 above against them.

## 2. The variables, by plane

Only set the planes you use. See `.env.example` for the full commented list and defaults.

| Plane | Variable(s) | Notes |
|-------|-------------|-------|
| Live PV (PVA/CA) | `EPICS_MCP_PROVIDER`, plus the standard EPICS search env: `EPICS_PVA_ADDR_LIST` / `EPICS_CA_ADDR_LIST`, `EPICS_PVA_NAME_SERVERS`, `*_AUTO_ADDR_LIST` | No URL: reach follows the EPICS search env. ⚠️ The auto-addr search defaults to **ON** when unset (subnet broadcast); `epics-doctor` prints the effective posture. |
| ChannelFinder | `EPICS_MCP_CHANNELFINDER_URL` (+ `_AUTH`, `_MAX_RESULTS`) | Service root incl. context path, e.g. `http://channelfinder:8080/ChannelFinder`. |
| Archiver Appliance | `EPICS_MCP_ARCHIVER_URL` (mgmt `:17665`), `EPICS_MCP_ARCHIVER_RETRIEVAL_URL` (retrieval `:17668`), `_AUTH` | See the cluster/port assumptions in section 5. |
| Alarm Logger | `EPICS_MCP_ALARM_URL` (+ `_AUTH`) | Phoebus Alarm Logger REST root. |
| Naming Service | `EPICS_MCP_NAMING_URL` | No built-in host, so no egress unless set. |
| Olog logbook | `EPICS_MCP_OLOG_URL` (+ `_AUTH`) | REST root incl. context path. Reads return the whole entry (title, text, author, attachments); the former read redaction was removed 2026-08-01, see `docs/safety.md`. |

The table groups by SERVICE, which is how you configure them. The report and the `plane` field of
`--json` group by CHECK and print seven names: `live`, `channelfinder`, `archiver`,
`archiver_retrieval`, `alarm`, `naming`, `olog`. The Archiver is the one service holding two of
them, probed separately so a correct management URL beside an unreachable retrieval one shows up as
one healthy line and one failing line rather than as a single ambiguous verdict.

Network posture: PV reach is decided by the launcher's EPICS search-path env: address lists, name
servers, and the auto-addr search, which defaults to **ON** (subnet broadcast) when unset; a
genuinely localhost-isolated instance needs every list unset **and** `*_AUTO_ADDR_LIST=NO`.
⚠️ `epics-doctor` claims `localhost-isolated` on the live plane against the ACTIVE provider's
auto-addr switch only, so it can print that line while the other provider's switch is still open.
The write gate is the stricter of the two and demands both, which is why the `Write gates` block
computes its own reach line rather than pointing at this one. The REST
planes stay off until their `*_URL` is set. Writes are gated off by default
(`EPICS_MCP_ALLOW_PV_WRITE=false`) and additionally need a regex allowlist, a rate limit, an audit
log and a loopback-only EPICS search reach. That last one is why the network posture above and the
write gate are not independent settings: writes on plus a reach beyond loopback is a start-time
refusal, not a warning.

## 3. TLS / CA bundle (the common first-deploy snag)

If a REST plane is HTTPS with a certificate signed by an **internal** root CA that is not in the
default trust store (certifi), verification fails with `self-signed certificate in chain`. Point the
server at a CA bundle:

```ini
EPICS_MCP_CA_BUNDLE=/etc/ssl/certs/combined-ca.pem
```

**When planes present different trust roots** (say ChannelFinder uses your internal CA while the
Naming service uses a *public* CA), a single-root bundle fails one of them. **Combine** your internal
CA PEM **with** the public roots (certifi's `cacert.pem`) into **one** PEM and point `CA_BUNDLE` at
that combined file. Building it is a plain concatenation of your internal CA PEM and certifi's
`cacert.pem` into one file, in either order, with whatever your shell uses to join two text files.
Locate certifi's copy first, because it lives inside the installed package rather than in a system
directory: `python -m certifi` prints its full path.

Use a path that carries no username, and not a home directory. The reason is whose process reads it:
the server runs as whatever launched it, often a service account rather than you, and a bundle under
your profile is then unreadable or absent. A machine-wide location is the safe choice, `/etc/ssl/`
or similar on Linux, a directory under ProgramData on Windows. ⚠️ On Windows, write the path out in
full: this value is an environment variable read by Python, and neither the client nor this server
expands a `%VARIABLE%` reference, so a `%ProgramData%\...` string is taken literally and the plane
fails as if the bundle were missing. As a last resort on a trusted internal network only,
`EPICS_MCP_TLS_VERIFY=false` disables verification entirely.

Precedence: `EPICS_MCP_CA_BUNDLE` (a path) > `EPICS_MCP_TLS_VERIFY=false` > default (certifi).
`epics-doctor` reports `ca_error` on a plane whose TLS/CA verification fails, distinct from
`unreachable`.

## 4. Privacy: the ChannelFinder allowlists (site-configurable)

The ChannelFinder redaction surfaces an `owner` only for known **service** accounts and only the
listed **technical** property names; anything else (a person's username, a free-text property value)
is dropped. The defaults follow the ESS RecSync convention. If your registry uses different service
accounts or property names, override them:

```ini
# unset = the built-in defaults; a comma-list REPLACES them; an empty value = redact everything
EPICS_MCP_CHANNELFINDER_SAFE_OWNER_ACCOUNTS=recceiver,my-ioc-service
EPICS_MCP_CHANNELFINDER_SAFE_PROPERTY_NAMES=iocName,hostName,iocid,pvStatus,time
```

A value REPLACES the default rather than adding to it, which is why the example above repeats
`recceiver`, the built-in owner account, next to the one being added. Drop it and you have switched
the default off, silently and successfully.

`epics-doctor` prints the effective ChannelFinder allowlists so you can see exactly what will be
surfaced. (Its `Write gates` block prints the write allowlists, which are a different thing.)

## 5. Documented assumptions (adapt if your site differs)

- **Archiver clustering.** The server assumes a **retrieval-cluster-aware** appliance: one
  `EPICS_MCP_ARCHIVER_URL` transparently answers for every cluster member (the `appliance` field
  names the owning member). A facility with **separate, non-clustered** appliances (or several
  independent clusters) would need per-endpoint routing, not built yet, but the result contract
  already carries the `appliance` / `withheld_reason` fields for it. Raise it with us if you need it.
- **Mgmt vs. retrieval port split (orthogonal to clustering).** If retrieval runs on a separate
  Tomcat (`:17668`) from mgmt (`:17665`), set `EPICS_MCP_ARCHIVER_RETRIEVAL_URL` as well; clustering
  does not remove the port split. In a single-JVM appliance leave it empty (it falls back to the mgmt
  URL). If `epics-doctor` reports the archiver `api_error`, a likely cause is `ARCHIVER_URL` pointing
  at the retrieval webapp instead of mgmt.
- **Enumerating archived PVs.** Use `list_archived_pvs` (it calls `getAllPVs` /
  `getPVsForThisAppliance`, not `getMatchingPVs`, which may 404 on split/proxied deployments).
- **API shapes.** The Naming-service request form and the Olog parameter/field names follow the
  Phoebus / ESS conventions; a different implementation of those services may need adjustment.
- **An IOC that does not answer a broadcast.** The UDP search alone does not reach a CONTAINERISED
  IOC: a container usually publishes its PVA TCP port and no UDP search port, so the broadcast finds
  nothing while the IOC is perfectly healthy. Add the TCP-unicast route,
  `EPICS_PVA_NAME_SERVERS=<ioc-host>:5075`, **alongside** the address list rather than instead of it:
  the two searches run in parallel, so a native server stays reachable while a containerised one
  becomes reachable. The trade-off is that this route needs the exact port where the UDP one does
  not, so a server that fell back to another port is found by the broadcast and missed by this
  (measured on a loopback server whose default port was taken). Only the `sandbox` preset sets both,
  because loopback fixes the port; the three facility shapes deliberately set the address list alone,
  since none of them can know your port and a guessed one is worse than none.

## 6. Troubleshooting

Symptom first, because the symptom is what you have. Each entry points at the material that explains
it rather than repeating it.

**The client reports the server as failed or not connected, and shows no error.** Most causes land
here, because a client hides the server's stderr, and stderr is where every refusal is written.
Reproduce it in the open: run `epics-mcp` yourself in a shell with the same variables set. A healthy
start prints a third-party banner to stderr and then waits silently for a client to speak to it. That
silence is correct and there is nothing further to see. A refusal instead ends in a message that names
the variable to change. Your client may also keep an MCP log of its own, which is worth finding once.
⚠️ A slow start before the banner is usually the framework's update check, which fetches its own
latest version from `pypi.org`. Where that is unreachable it costs about two seconds and then
continues, because it uses a short timeout and swallows the error, so it is not the cause of a
real hang: `FASTMCP_CHECK_FOR_UPDATES=off` removes the delay (see [safety](safety.md)).

**The tools do not appear, and nothing looks wrong.** A client reads its configuration when it starts
and will not notice an edit, so restart or reconnect it, see
[MCP client integration](mcp-clients.md). Then confirm you edited the file that client actually
reads, which is not necessarily the one in your working directory.

**The configuration is right and the client still cannot start the server.** The `command` in the
block is a bare name, so something has to resolve it, and a client launched from a desktop icon does
not inherit the PATH of your shell. See [MCP client integration](mcp-clients.md).

**The file I saved is rejected, or the client reads nothing out of it.** A shell redirect writes the
encoding its shell prefers, not the one JSON needs. Measured in Windows PowerShell 5.1: `>` writes
UTF-16 with a byte-order mark, and both `Set-Content -Encoding utf8` and `Out-File -Encoding utf8`
write UTF-8 with one. All three are rejected by a strict parser, Python and Node alike. PowerShell 7
writes UTF-8 without a mark, as do POSIX shells, and those are fine. So check what you actually
wrote: the file has to begin with `{` and carry no invisible bytes in front of it.

**A write-enabled block will not start.** Those variables are start conditions rather than hardening,
and each one refuses instead of warning: a durable audit path for either gate, plus a loopback-only
search reach and a non-empty allowlist pattern for the PV gate specifically. The reasoning is in
section 2 and the block itself is in [MCP client integration](mcp-clients.md). One thing neither
states: the audit path's parent directory has to exist already.

Run `epics-doctor` with the same environment and read its `Write gates` block, which answers three
of those four directly: it prints the pattern, the reach findings, and whether the audit log can be
appended to (a missing parent directory is reported as such rather than guessed at). It does not
evaluate the start itself, so it narrows the search rather than ending it.

**A PV probe says it did not connect.** The finding names the search path it used, so compare that
with where the IOC actually is. Two causes are invisible in the PV name: an IOC that answers on a
port but not to a broadcast needs `EPICS_PVA_NAME_SERVERS` (section 5), and a firewall between you and
the IOC drops the search without saying so. `epics-diagnose <a-connected-pv>` shows one PV in full
detail, including the value, where the doctor reports only reachability.

**The install itself ended in an error.** If the output mentions a compiler, `p4p` had no prebuilt
wheel for your platform and fell back to building from source, see Compatibility in the README. If it
mentions an externally managed environment, your Python is refusing a system-wide `pip install`: use
a virtual environment, or `uv tool install`.

## 7. Where to look next

- [MCP client integration](mcp-clients.md): where the block has to end up, the paste-ready variants
  including the write-enabled one, and the restart step that finishes the job.
- [Configuration](configuration.md): every variable with its default. This is the page to use instead
  of `.env.example` when you installed from a package rather than a checkout; in a checkout,
  `.env.example` is the same set with commentary.
- The `epics-pv://guide` MCP resource (and `OPERATING.md`) is the operational cookbook: service
  landscape, recipes, typical error signatures.
- The `setup_epics_mcp` prompt, available once the server is connected, covers the same ground as
  this page one question at a time, for whoever would rather be asked than read.
- `epics-doctor`: always the fastest way to answer "is my config right?".
