"""What counts as a display file, in one place.

Two surfaces need this answer and must not disagree: the ``validate_pvs`` tool, which REFUSES a
``file_path`` that is not a display, and the ``compare_machine_state`` prompt, which must not teach
a call that refusal is certain to reject.

It lives in its own module rather than in ``tools/validate.py`` for a hard reason: that module
imports ``opi_navigation`` at module level, the optional ``displays`` dependency group. A prompt
importing it would drag the optional engine into a core-only install, which is exactly the split
``display_tools.py`` exists to keep. Nothing here imports anything.
"""

from __future__ import annotations

#: The suffix a display file carries. This mirrors the collection rule of the display-PV engine:
#: ``opi_navigation``'s ``find_bob_files`` keeps a candidate only when ``suffix.lower()`` equals
#: this. Measured rather than read off the source: a ``.txt`` holding valid display XML AND
#: embedded by a ``.bob`` never surfaces as an ``origin_file``, while ``UPPER.BOB`` does, which is
#: why every comparison here folds case.
#:
#: Deliberately not an import of the engine's private ``_BOB_SUFFIX``;
#: ``tests/test_validate.py`` pins the coupling against the engine itself instead, so a drift is a
#: red test rather than a silent disagreement.
DISPLAY_SUFFIX = ".bob"


def is_display_file(name: str) -> bool:
    """True iff *name* has the display suffix, case-folded.

    Name-only by design, it answers "would the engine even look at this?" for a path that may not
    exist yet (a prompt renders text and must not touch the filesystem). The tool re-checks the
    RESOLVED path, which is the stricter question and the one that decides.
    """
    return name.lower().endswith(DISPLAY_SUFFIX)
