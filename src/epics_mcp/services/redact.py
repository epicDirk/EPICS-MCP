"""Runtime allowlist projection for read-only tool output.

One primitive: :func:`project_allowlist` restricts a record to an ALLOWLIST of known keys,
dropping every other key. An allowlist, not a denylist: a NEW field added upstream is dropped by
default instead of silently widening the advertised shape. Consumers: the ChannelFinder property
projection (site-configurable DS-PRIVACY allowlist) and the two alarm projections (structural,
known-field shape).

The former free-text withholding (``withhold_freetext`` / ``redact_record`` and the
``FREETEXT_WITHHELD`` marker) was removed together with the Olog read redaction it existed for
(decision PI, 2026-08-01); the mechanism is in the git history up to that date.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping


def project_allowlist(record: Mapping[str, object], allowed: Collection[str]) -> dict[str, object]:
    """Return *record* restricted to the keys in *allowed* (every other key is dropped).

    An allowlist (not a denylist): a field that is not explicitly allowed, now or added upstream
    later, never survives.
    """
    return {key: value for key, value in record.items() if key in allowed}
