"""What the display-PV inventory reads, in one place.

Two surfaces need this answer and must not disagree: the ``validate_pvs`` tool, which REFUSES a
``file_path`` the inventory cannot read, and the ``compare_machine_state`` prompt, which must not
teach a call that refusal is certain to reject.

It lives in its own module rather than in ``tools/validate.py`` for a hard reason: that module
imports ``opi_navigation`` at module level, the optional ``displays`` dependency group. A prompt
importing it would drag the optional engine into a core-only install, which is exactly the split
``display_tools.py`` exists to keep. Nothing here imports anything.
"""

from __future__ import annotations

#: A CS-Studio / Phoebus operator screen.
DISPLAY_SUFFIX = ".bob"

#: A Data Browser TREND file. Not a display, and the distinction is the engine's own: it reports
#: such a top level as ``node_kind="trend"`` and its PV occurrences as ``origin_kind="trend"``,
#: rather than pretending a trend is a screen. Kept as its own constant so a message can say which
#: of the two kinds it means instead of listing suffixes.
TREND_SUFFIX = ".plt"

#: The suffixes the display-PV engine collects, and therefore the ONLY ones ``validate_pvs`` can
#: answer about. This mirrors the collection rule of ``opi_navigation``: it keeps a candidate when
#: ``suffix.lower()`` equals one of these. Measured rather than read off the source: a ``.txt``
#: holding valid display XML AND embedded by a ``.bob`` never surfaces as an ``origin_file``, while
#: ``UPPER.BOB`` does, which is why every comparison here folds case.
#:
#: Deliberately not an import of the engine's private ``_BOB_SUFFIX`` / ``_PLT_SUFFIX``;
#: ``tests/test_validate.py`` pins the coupling against the engine itself instead, so a drift is a
#: red test rather than a silent disagreement.
#:
#: ⚠ THE SHAPE OF THAT GUARD IS THE LESSON HERE, and it was learned the expensive way. It used to
#: ask whether our ONE constant EQUALS the engine's one constant, and the engine widened by putting
#: a second suffix NEXT TO the first rather than replacing it, so equality survived a change that
#: made the refusal in ``tools/validate.py`` wrong: it rejected trend files the inventory reads.
#: The guard now compares SETS in both directions, so a third suffix, and a removed one, both go
#: red. A one-element constant could not express that question at all, which is why this is a tuple
#: even while it holds two entries.
INVENTORY_SUFFIXES: tuple[str, ...] = (DISPLAY_SUFFIX, TREND_SUFFIX)


#: How a rendered report NAMES a node kind that is not an operator screen, appended to the file
#: name in a Markdown line. One dict rather than one literal per renderer: ``find_device`` and
#: ``crossplane_check`` both mark a trend in their file lists, and two copies of a human-facing
#: phrase drift the way the cap wording drifted across four tools before GB-72 pinned it. A kind
#: with NO entry here renders nothing extra, which is the right default for ``"display"`` and the
#: honest one for a kind this server has not been taught: the renderers fall back to naming it.
KIND_MARKERS: dict[str, str] = {"trend": ", Data Browser trend (not a screen)"}


def is_inventory_file(name: str) -> bool:
    """True iff *name* carries a suffix the display-PV inventory reads, case-folded.

    Name-only by design, it answers "would the engine even look at this?" for a path that may not
    exist yet (a prompt renders text and must not touch the filesystem). The tool re-checks the
    RESOLVED path, which is the stricter question and the one that decides.

    ⚠ The question is "does the inventory read it", NOT "is it a display", and the name says so
    since the engine started collecting trend files too. A ``.plt`` answers True here and is still
    not a screen: what it IS comes back from the engine as ``node_kind``, never from its suffix.
    That distinction is not pedantic. The engine's own parser makes it: measured, the 17 ``.plt``
    files under a checkout of ``epics-base`` are Perl scripts, so the suffix is a candidate filter
    and the root element decides.
    """
    lowered = name.lower()
    return any(lowered.endswith(suffix) for suffix in INVENTORY_SUFFIXES)
