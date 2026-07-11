# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) (pre-1.0: minor versions may
carry breaking changes).

## [Unreleased]

### Added

- `epics-pv://guide` resource + `OPERATING.md`: an operational cookbook (service planes,
  archiver-enumeration/retrieval-cluster/CA-bundle recipes, error signatures) shipped inside
  the package, so the server carries its own operational knowledge. Backed by one source file
  (`src/epics_pv_mcp/operator_guide.md`), drift-guarded against the tool/env surface.
- Optional `[displays]` extra: the display-aware tools (`validate_pvs`,
  `crossplane_check`, `coverage_audit`, `find_device`) now live behind an optional
  dependency on the `opi_navigation` PV engine. The **core** server (live PV access,
  diagnosis, REST-service planes) installs and starts standalone without it.
- Packaging metadata (`license`, `readme`, `authors`, `keywords`, project URLs);
  `ARCHITECTURE.md`, `CONTRIBUTING.md`, and this changelog.

### Changed

- `EPICS_MCP_DEFAULT_TIMEOUT` is now honoured on the whole read/write path. Tool
  timeouts default to the configured server timeout instead of a hardcoded `5.0`.
- The 15 tool wrappers now share a single `translate_epics_errors` decorator instead of
  a copied `try/except` block.
- The live probe in `diagnose_connection` runs concurrently with the explanatory planes
  (worst-case latency ~1×timeout instead of ~2×timeout).

### Fixed

- `NamingServiceClient` normalises `base_url` (trailing-slash strip) like the other REST
  clients, so a URL configured without a trailing slash no longer 404s.
- README ↔ code drift: resource URIs, the full configuration table (incl. the optional
  REST-service and EPICS-network variables), and the uv-based development gate chain.

## [0.2.0]

Baseline: an independently developed EPICS PV MCP server on FastMCP + p4p with a
write-safety layer, batch operations, PV monitoring, cross-plane provenance
(ChannelFinder · Archiver · Alarm · Naming), device lookup, and OPI file validation.
Originally seeded from [Jacky1-Jiang/EPICS-MCP-Server](https://github.com/Jacky1-Jiang/EPICS-MCP-Server).
