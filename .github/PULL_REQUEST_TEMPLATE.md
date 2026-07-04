## What this changes

A short summary of the change and the motivation.

## Type

- [ ] Bug fix
- [ ] New tool / CLI / capability
- [ ] Refactor (no behaviour change)
- [ ] Docs / tests only

## Checklist

- [ ] `uv run pytest` is green (offline default; add `-m live` runs only if you have a sandbox).
- [ ] `uv run pre-commit run --all-files` is green (ruff, ruff-format, mypy --strict, PII guard).
- [ ] New/changed behaviour is covered by a test.
- [ ] Read-only posture preserved (no new default-on network egress; any write stays triple-gated).
- [ ] Public tool/CLI signatures and their descriptions are consistent with the change.
- [ ] Docs updated if a tool, resource, config var, or CLI was added or changed.

## Notes for reviewers

Anything worth calling out — a deferred follow-up, a known limitation, a design trade-off.
