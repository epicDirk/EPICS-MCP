# Operating Guide

The operational cookbook for this server, covering the service landscape, the non-obvious recipes
and the error signatures that tell you which tool to reach for, lives in **one** source file:

**[`src/epics_mcp/operator_guide.md`](src/epics_mcp/operator_guide.md)**

That single source is shipped inside the package and served to an AI assistant verbatim: as the
`get_guide` tool, which is the channel a model fetches from and which can serve one named section
at a time, and as the `epics-pv://guide` resource for an application reading it whole. The
knowledge therefore travels with the server: no external skill or project folder required. Read it
there; this file is only the human-facing signpost.

⚠️ **A pointer, deliberately, and it used to be less of one.** This file named five of the guide's
recipes in a parenthesis. The guide has ten recipe headings, no test held the two together, and
that gap was the whole reason the list was worth removing rather than correcting: a pointer with no
enumeration in it cannot drift, and an enumeration nobody checks is a second copy waiting to go
stale.

The deployment and site-adaptation guide (network configuration, CA bundles, per-facility
settings) is a separate document: **[docs/deployment.md](docs/deployment.md)**. Start there when
bringing the server up in a new facility, and run `epics-doctor` to confirm the result.
