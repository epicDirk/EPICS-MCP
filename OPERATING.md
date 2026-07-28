# Operating Guide

The operational cookbook for this server, covering the service landscape, the non-obvious recipes
(archiver PV enumeration, retrieval-cluster-aware appliances, the combined CA-bundle recipe, the
`.FIELD` read path, the Naming `/rest` gotcha) and the error signatures that tell you which tool to
reach for, lives in **one** source file:

**[`src/epics_mcp/operator_guide.md`](src/epics_mcp/operator_guide.md)**

That single source is shipped inside the package and served to an AI assistant verbatim as the
`epics-pv://guide` MCP resource, so the knowledge travels with the server: no external skill or
project folder required. Read it there; this file is only the human-facing signpost (kept a pointer,
not a copy, so the two can never drift).

The deployment and site-adaptation guide (network configuration, CA bundles, per-facility
settings) is a separate document: **[docs/deployment.md](docs/deployment.md)**. Start there when
bringing the server up in a new facility, and run `epics-doctor` to confirm the result.
