"""Runtime redaction utilities for read-only tool output (DS-PRIVACY).

Person data must never leave a tool. Two complementary primitives generalise the hand-stripping
that was written inline in :mod:`epics_pv_mcp.services.alarm_client`
(``_project_alarm_event`` / ``_project_alarm_config``) so every name-capable REST surface (Olog,
ChannelFinder ``owner``, alarm ``detail``) routes its output through one shared barrier:

1. :func:`project_allowlist`, restrict a record to an ALLOWLIST of technical keys, dropping every
   other key (``author``/``owner``/``user`` and any unknown field). An allowlist, not a denylist:
   a NEW person-bearing field added upstream is dropped by default rather than leaked.

2. :func:`withhold_freetext`, a key-based allowlist is NOT enough for a field whose VALUE is
   author-written free text (a log title, body, comment, attachment name): a person's name can sit
   *inside* the text. These fields keep their key but have their value replaced by a withheld
   marker, so a caller learns a title existed without seeing who is named in it. The marker is
   deliberately not a truncated/"cleaned" excerpt, automated name-scrubbing of free text is not
   reliable, so the MVP withholds the content outright (upgrade path: a reviewed release).

:func:`redact_record` composes the two: project onto the allowlist, then withhold the free-text
fields within it.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping

#: Replacement value for a free-text field whose content may contain personal data.
FREETEXT_WITHHELD = "[withheld: free text may contain personal data]"


def project_allowlist(record: Mapping[str, object], allowed: Collection[str]) -> dict[str, object]:
    """Return *record* restricted to the keys in *allowed* (every other key is dropped).

    An allowlist (not a denylist): a person-bearing field that is not explicitly allowed, now or
    added upstream later, never survives.
    """
    return {key: value for key, value in record.items() if key in allowed}


def withhold_freetext(
    record: Mapping[str, object], freetext_keys: Collection[str]
) -> dict[str, object]:
    """Return *record* with each *freetext_keys* value replaced by :data:`FREETEXT_WITHHELD`.

    The key is kept (its PRESENCE is surfaced) but its author-written content is withheld, a name
    inside a title/body/comment cannot leak. Non-free-text keys are passed through unchanged.
    """
    return {
        key: (FREETEXT_WITHHELD if key in freetext_keys else value) for key, value in record.items()
    }


def redact_record(
    record: Mapping[str, object],
    *,
    allowed: Collection[str],
    freetext: Collection[str] = frozenset(),
) -> dict[str, object]:
    """Project *record* onto *allowed*, then withhold the *freetext* fields within it.

    *freetext* keys should be a subset of *allowed* (a free-text field kept for its presence but
    whose value is withheld). Keys in *freetext* but not *allowed* are simply dropped by the
    projection, which is safe (dropping is stricter than withholding).
    """
    return withhold_freetext(project_allowlist(record, allowed), freetext)
