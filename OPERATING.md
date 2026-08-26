# Operating Guide

The operational cookbook for this server, covering the service landscape, the non-obvious recipes
and the error signatures that tell you which tool to reach for, lives in **one** source file:

**[`src/epics_mcp/operator_guide.md`](src/epics_mcp/operator_guide.md)**

That single source is shipped inside the package and served to an AI assistant verbatim: as the
`get_guide` tool, which is the channel a model fetches from and which can serve one named section
at a time, and as the `epics://guide` resource for an application reading it whole. The
knowledge therefore travels with the server: no external skill or project folder required. Read it
there; this file is only the human-facing signpost.

⚠️ **Deliberately a pointer, with no list of recipes in it.** An enumeration here would be a
second copy of the guide's own headings, and no test holds the two together, so it would go stale
without anyone noticing.

The deployment and site-adaptation guide (network configuration, CA bundles, per-facility
settings) is a separate document: **[docs/deployment.md](docs/deployment.md)**. Start there when
bringing the server up in a new facility, and run `epics-doctor` to confirm the result.
