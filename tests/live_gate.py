"""The live-test skip gate: silent by default, LOUD on demand (S30).

Why this exists: every live module in this repo gated itself with a ``pytest.mark.skipif``
on its plane's env vars. CI calls bare ``uv run pytest`` with those vars unset, so the
live tests skip silently, and ``uv run pytest -m live`` in an empty environment reported
``51 skipped, exit 0`` when S30 was written: a DEMANDED live run was indistinguishable from a
fulfilled one. A silently skipped gate looks like a passed gate in every report.
The count is deliberately left at its historical value and dated rather than tracked: it grows
with every live module (75 as of 2026-08-27) and nothing watches it, so a number stated in the
present tense here would rot. What the sentence needs is the SHAPE of the report, not its size.

The mechanism is deliberately NOT a CI gate (CI has no live stack, and no CI guard can
prove a probe RAN, see CLAUDE.md, "Server-decided parameters"). Instead: whoever
explicitly DEMANDS a live run gets red instead of a skip when a prerequisite is missing.

Demanded via the env var ``EPICS_MCP_REQUIRE_LIVE=1``. The demand applies to the SELECTED
test set: scope a partial live run with ``-m live -k <plane>`` (a deselected test never
reaches its gate); every selected probe whose prerequisite is missing goes red. Data-
dependent skips INSIDE a running live probe (e.g. "fixture carries only one level") are a
separate class and stay skips.

The ``live`` marker (declared in ``pyproject.toml``) stays orthogonal: it SELECTS
(``-m live``), it does not demand.

The actual gate is ONE function, :func:`assert_live_available`, called from an autouse
fixture (module prerequisites) or as the first statement of a fixture/test (extra
prerequisites). It replaces the former ``pytest.mark.skipif`` decorators deliberately:
those were evaluated ONCE at collection time, while the prerequisite can change until
setup time.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

REQUIRE_LIVE_ENV = "EPICS_MCP_REQUIRE_LIVE"

# Read generously so a "true"/"yes" does not silently count as "not demanded", a
# false-negative gate would be exactly the defect this module removes.
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def live_demanded(env: Mapping[str, str]) -> bool:
    """Was a live run explicitly demanded?

    The environment is INJECTED instead of read globally, the decision stays
    deterministic and offline-testable (the only half of this gate CI can check at all).
    """
    return env.get(REQUIRE_LIVE_ENV, "").strip().lower() in _TRUTHY


def assert_live_available(available: bool, reason: str, *, demanded: bool) -> None:
    """The gate. Belongs BEFORE any connection attempt (fixture start / first test line).

    - prerequisite present   -> returns, the test runs
    - missing, not demanded  -> ``skip`` (default/CI: silent, green, as before)
    - missing but DEMANDED   -> ``fail`` with the reason in plain text

    ``pytrace=False`` because a stack trace explains nothing here: the message is "the
    prerequisite is missing", not "this code is broken".
    """
    if available:
        return
    if demanded:
        pytest.fail(f"live run demanded ({REQUIRE_LIVE_ENV}) but {reason}", pytrace=False)
    pytest.skip(reason)
