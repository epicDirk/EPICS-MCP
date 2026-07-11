# Contributing

Thanks for your interest. This is a pre-1.0, work-in-progress project — issues and small
PRs are welcome.

## Development setup

The gate chain is [uv](https://docs.astral.sh/uv/)-based:

```bash
uv sync --extra all --frozen              # full local install (dev tools + display extra)
uv run pytest                             # run the test suite
uv run pytest --cov=src --cov-branch      # with coverage
uv run pre-commit run --all-files         # ruff check + ruff format + mypy --strict + guards
```

**Two extras:** `dev` is the core toolchain (pytest, ruff, mypy, pre-commit); `all` adds the
`[displays]` extra, which pulls the `opi_navigation` PV engine so the display-aware tools and
their tests run. Use `--extra all` locally for the full suite.

CI runs `uv sync --extra dev --frozen` (**not** `all`): `opi_navigation` lives in a private
repository the public-fork CI can't clone, so CI tests exactly the standalone core a public
user gets. The `opi_navigation`-coupled test modules skip themselves when the package is
absent, so the core suite stays green. A checkout with `--extra all` runs everything.

## Definition of done

- `uv run pytest` green and coverage not reduced.
- `uv run pre-commit run --all-files` green (ruff, ruff-format, **mypy --strict**, and the
  no-secrets guard).
- New behaviour has a test; new tools/config are documented in `README.md` (the resource
  URIs are drift-checked against the server by a test).
- New operational knowledge (a service/IOC recipe, an endpoint, an error signature) lands in the
  operator guide (`src/epics_pv_mcp/operator_guide.md`), not just a commit body — see the Knowledge
  Persistence Policy in `CLAUDE.md`. A new tool must appear in the guide's tool inventory and any new
  `EPICS_MCP_*` var must be mentioned there; both are drift-checked by `test_guide_matches_code.py`.

## Live / sandbox tests

Tests that need a running EPICS stack are opt-in and skip by default, so the standard run
and CI stay green without any infrastructure.

## Code style

Small, focused, deterministic functions; speaking names; comments explain *why*. The
layering contract is `server → tools → services → clients` (see `ARCHITECTURE.md`) — keep
dependencies pointing downward.

## Commits

Conventional-style prefixes (`fix:`, `feat:`, `refactor:`, `docs:`, `test:`), one logical
change per commit.
