# Deploying the EPICS MCP server in your facility

This server is **facility-agnostic by design**: every service URL and network setting is an
`EPICS_MCP_*` environment variable, never a hard-coded site. Deploying it in a new facility means
setting those variables for *your* services, with no code change. This guide walks through that, and
`epics-doctor` confirms it for you.

> **Config lives in the environment (the 12-factor equivalent of a CS-Studio `.ini`).** Set the
> variables in whatever LAUNCHES the server: the `env` block of your MCP client's `.mcp.json`, a
> systemd unit's `Environment=` / `EnvironmentFile=`, a container spec, or your shell.
> `.env.example` is a commented REFERENCE of every recognised variable, not a file the server
> loads. There is no separate config-file format to learn.

## 1. Quick start

1. Open `.env.example` and copy the lines you need into your launcher, setting the URLs for the
   services you have (all are optional: an unset URL disables that plane, with no network call).
   Nothing on disk named `.env` is ever read: the server takes its configuration from the process
   environment, so a variable that does not reach the process has no effect and says nothing.
2. If any HTTPS service uses an internal CA, set up the CA bundle (section 3).
3. Run the self-check:

   ```bash
   epics-doctor            # probes every configured plane, read-only
   epics-doctor --json     # machine-readable, for CI
   epics-doctor --probe-pv SIM:PS-01:Cur-RB   # also pass/fail the live PVA plane
   ```

   Exit `0` = nothing failed and no identity probe failed; `1` = a configured plane HARD-failed
   (including `config_error`, where the variables contradict each other, e.g. a retrieval URL with no
   archiver URL, and `backend_down`, a reachable, identified plane whose backend is down, e.g. the
   alarm logger's Elasticsearch); `2` = usage error; `3` = INCONCLUSIVE, a plane is reachable but
   its identity
   probe FAILED (a served non-2xx like a 401/404, a transport error, or a refused redirect on the
   identity endpoint): not a hard failure, but not a silent all-clear either. A URL that ANSWERS
   with a *different* known service's name is reported `unverified` (exit `0`) with that name in the
   detail, not a failure (a path-based reverse proxy can serve the real API behind a base URL that
   names another service, measured), but the name is your first clue if the config IS wrong. Fix
   anything `epics-doctor` flags, then you are done; no need to ask us.

   Each line that reports a problem carries its own remedy: what was observed, then what to change
   about it. So the common cases need no lookup here, and the sections below are for when you want
   the reasoning behind one. `!` (`identity_probe_failed`) carries one too, being inconclusive
   rather than healthy. The exceptions are `?` (`unverified`) and `~` (`no_ingest`), honest states
   with nothing to set, and they say what they measured instead.

   ⚠️ Read the `?` (`unverified`, exit `0`), `!` (`identity_probe_failed`, exit `3`) and `~`
   (`no_ingest`, exit `0`) lines before
   calling it done. `?` = "answered 2xx, but could not prove what it is": honest, not healthy. `!` =
   "reachable, but the identity probe FAILED (401/404/redirect/...)": suspect, not a silent pass.
   `~` = "proved what it is, and is not doing its job": an Archiver appliance holding channels with
   none connected, or reporting one of its own webapps stopped. It is exit `0` on purpose, because
   a freshly commissioned appliance is legitimately in that state, but it is a finding and worth
   chasing before you call a deployment done.
   Every plane has its own identity beacon (see the operator guide). Scripting this? Read
   `verification_complete` / `unverified_planes` / `inconclusive_identity_planes` /
   `degraded_planes` from `--json` (a
   failed probe lands in `inconclusive_identity_planes`, not `unverified_planes`; a degraded one in
   `degraded_planes` and in neither of those); the exit code
   alone says "nothing failed", not "everything confirmed", and for positive confirmation assert
   `identified_planes` is non-empty (`verification_complete` is vacuously true on an empty config,
   and a degraded plane is listed there too, since its identity IS proven).

## 2. The variables, by plane

Only set the planes you use. See `.env.example` for the full commented list and defaults.

| Plane | Variable(s) | Notes |
|-------|-------------|-------|
| Live PV (PVA/CA) | `EPICS_MCP_PROVIDER`, plus the standard EPICS search env: `EPICS_PVA_ADDR_LIST` / `EPICS_CA_ADDR_LIST`, `EPICS_PVA_NAME_SERVERS`, `*_AUTO_ADDR_LIST` | No URL: reach follows the EPICS search env. ⚠️ The auto-addr search defaults to **ON** when unset (subnet broadcast); `epics-doctor` prints the effective posture. |
| ChannelFinder | `EPICS_MCP_CHANNELFINDER_URL` (+ `_AUTH`, `_MAX_RESULTS`) | Service root incl. context path, e.g. `http://channelfinder:8080/ChannelFinder`. |
| Archiver Appliance | `EPICS_MCP_ARCHIVER_URL` (mgmt `:17665`), `EPICS_MCP_ARCHIVER_RETRIEVAL_URL` (retrieval `:17668`), `_AUTH` | See the cluster/port assumptions in section 5. |
| Alarm Logger | `EPICS_MCP_ALARM_URL` (+ `_AUTH`) | Phoebus Alarm Logger REST root. |
| Naming Service | `EPICS_MCP_NAMING_URL` | No built-in host, so no egress unless set. |
| Olog logbook | `EPICS_MCP_OLOG_URL` (+ `_AUTH`, `_ASSUME_TEST_DATA`) | REST root incl. context path. Output is DS-PRIVACY-redacted (author dropped / free text withheld), **unless BOTH the URL is loopback AND `EPICS_MCP_OLOG_ASSUME_TEST_DATA=true`**, where entries come back whole (ESS-spec pending; `epics-doctor` prints the effective posture). |

Network posture: PV reach is decided by the launcher's EPICS search-path env: address lists, name
servers, and the auto-addr search, which defaults to **ON** (subnet broadcast) when unset; a
genuinely localhost-isolated instance needs every list unset **and** `*_AUTO_ADDR_LIST=NO`. The REST
planes stay off until their `*_URL` is set. Writes are gated off by default
(`EPICS_MCP_ALLOW_PV_WRITE=false`) and additionally need a regex allowlist, a rate limit, an audit
log and a loopback-only EPICS search reach. That last one is why the network posture above and the
write gate are not independent settings: writes on plus a reach beyond loopback is a start-time
refusal, not a warning.

## 3. TLS / CA bundle (the common first-deploy snag)

If a REST plane is HTTPS with a certificate signed by an **internal** root CA that is not in the
default trust store (certifi), verification fails with `self-signed certificate in chain`. Point the
server at a CA bundle:

```bash
EPICS_MCP_CA_BUNDLE=/etc/ssl/certs/combined-ca.pem
```

**When planes present different trust roots** (say ChannelFinder uses your internal CA while the
Naming service uses a *public* CA), a single-root bundle fails one of them. **Combine** your internal
CA PEM **with** the public roots (certifi's `cacert.pem`) into **one** PEM and point `CA_BUNDLE` at
that combined file. Use a path that carries no username (e.g. `/etc/ssl/certs/...`), not a home
directory. As a last resort on a trusted internal network only, `EPICS_MCP_TLS_VERIFY=false` disables
verification entirely.

Precedence: `EPICS_MCP_CA_BUNDLE` (a path) > `EPICS_MCP_TLS_VERIFY=false` > default (certifi).
`epics-doctor` reports `ca_error` on a plane whose TLS/CA verification fails, distinct from
`unreachable`.

## 4. Privacy: the ChannelFinder allowlists (site-configurable)

The ChannelFinder redaction surfaces an `owner` only for known **service** accounts and only the
listed **technical** property names; anything else (a person's username, a free-text property value)
is dropped. The defaults follow the ESS RecSync convention. If your registry uses different service
accounts or property names, override them:

```bash
# unset = the built-in defaults; a comma-list = override; an empty value = redact everything
EPICS_MCP_CHANNELFINDER_SAFE_OWNER_ACCOUNTS=recsync,ioc-svc
EPICS_MCP_CHANNELFINDER_SAFE_PROPERTY_NAMES=iocName,hostName,iocid,pvStatus,time
```

`epics-doctor` prints the effective allowlists so you can see exactly what will be surfaced.

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

## 6. Where to look next

- `.env.example`: every variable, commented.
- The `epics-pv://guide` MCP resource (and `OPERATING.md`) is the operational cookbook: service
  landscape, recipes, typical error signatures.
- `epics-doctor`: always the fastest way to answer "is my config right?".
