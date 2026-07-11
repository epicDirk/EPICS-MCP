# Operating Guide

The operational cookbook for this server — the service landscape, the non-obvious recipes
(archiver PV enumeration, retrieval-cluster-aware appliances, the combined CA-bundle recipe, the
`.FIELD` read path, the Naming `/rest` gotcha) and the error signatures that tell you which tool to
reach for — lives in **one** source file:

**[`src/epics_pv_mcp/operator_guide.md`](src/epics_pv_mcp/operator_guide.md)**

That single source is shipped inside the package and served to an AI assistant verbatim as the
`epics-pv://guide` MCP resource, so the knowledge travels with the server — no external skill or
project folder required. Read it there; this file is only the human-facing signpost (kept a pointer,
not a copy, so the two can never drift).

A deployment / site-adaptation guide (network configuration, CA bundles, per-facility settings) will
join it here as the server matures.
