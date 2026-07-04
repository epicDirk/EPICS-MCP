# Contributing

Thanks for your interest. This is a pre-1.0, work-in-progress project — issues and small
PRs are welcome.

## Development setup

The gate chain is [uv](https://docs.astral.sh/uv/)-based; CI runs exactly these steps:

```bash
uv sync --extra dev --frozen              # install exact, locked dependencies
uv run pytest                             # run the test suite
uv run pytest --cov=src --cov-branch      # with coverage
uv run pre-commit run --all-files         # ruff check + ruff format + mypy --strict + guards
```

`uv sync --extra dev` also pulls the `[displays]` extra, so the full suite (including the
display-aware tools) runs. A checkout without `[displays]` still runs the core suite —
the opi_navigation-coupled test modules skip themselves.

## Definition of done

- `uv run pytest` green and coverage not reduced.
- `uv run pre-commit run --all-files` green (ruff, ruff-format, **mypy --strict**, and the
  no-secrets guard).
- New behaviour has a test; new tools/config are documented in `README.md` (the resource
  URIs are drift-checked against the server by a test).

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
