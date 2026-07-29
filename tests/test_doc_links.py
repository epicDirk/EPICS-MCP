"""QA-25: every relative file target in the documentation resolves to a tracked file.

A dead link is the one documentation defect a reader hits immediately and the author never does,
because the author knows where the page went. Nothing here checked them: the resource guard reads
URIs, the entry-point guard reads backticked commands, and the prose-number guard reads Python.

SCOPE, narrow on purpose. Three things are deliberately NOT checked, each because checking them
would make this guard less trustworthy rather than more:

* **Anchors** (``#section``). Slug rules differ between renderers: GitHub's slugger does not
  collapse consecutive separators (``## Resources & Prompts`` is ``#resources--prompts``), PyPI's
  ``readme_renderer`` and local previewers disagree with it and each other. A guard that pins one
  renderer's rule would fail the others and teach nobody anything. The three broken anchors this
  repository had were fixed by hand instead.
* **URLs.** Reaching the network from the ordinary gate would make it flaky and slow, and an
  external page that moves is not a defect in this tree.
* **Paths written in inline code.** Measured before excluding them: about 39 would be false
  positives. ``ARCHITECTURE.md`` names files relative to ``src/``, ``docs/safety.md`` names a REST
  endpoint, and ``docs/mcp-clients.md`` names a file on the READER's machine. A guard whose noise
  outnumbers its findings gets suppressed, and then it guards nothing.

RESOLVED AGAINST ``git ls-files``, NEVER AGAINST THE DISK. This is not a style preference, it is
the difference between passing here and failing in CI. Windows folds case in the filesystem, Linux
does not: measured on this checkout, ``readme.md``, ``Docs/tools.md`` and ``license`` all exist on
disk and none of them is a tracked path. A disk-based check is green on the author's machine and
red on the runner, which is the worst possible direction for a guard to be wrong in.
"""

from __future__ import annotations

import posixpath
import re
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]

# Matches the TARGET of any markdown link. Anchored on "](" rather than on the whole
# "[text](target)" shape, because the text half is where the naive pattern breaks: a badge is
# "[![alt](image-url)](target)", and a bracket class that forbids "[" inside the label skips the
# OUTER link entirely. README.md:5 is exactly that, and its target is the tracked file LICENSE.
_LINK_TARGET = re.compile(r"\]\(\s*([^)\s]+?)\s*\)")

# Targets this guard does not own. Anchors and URLs are argued in the module docstring.
_SKIPPED_PREFIXES = ("http://", "https://", "//", "#", "mailto:")


def _tracked_paths() -> frozenset[str]:
    """Every tracked path, exactly as git spells it, forward slashes and original case."""
    listing = subprocess.run(
        ["git", "-C", str(_REPO), "ls-files"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return frozenset(line.strip() for line in listing.splitlines() if line.strip())


def _relative_link_targets() -> list[tuple[str, int, str]]:
    """``(file, line number, target)`` for every relative link target in a tracked ``.md``."""
    tracked = _tracked_paths()
    found: list[tuple[str, int, str]] = []
    for path in sorted(p for p in tracked if p.endswith(".md")):
        text = (_REPO / path).read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for target in _LINK_TARGET.findall(line):
                if target.startswith(_SKIPPED_PREFIXES):
                    continue
                found.append((path, lineno, target))
    return found


def _resolves(source_file: str, target: str, tracked: frozenset[str]) -> bool:
    """Does *target*, read relative to *source_file*, name something git tracks?

    ``posixpath.normpath`` rather than ``PurePosixPath``: the latter does NOT collapse ``..``
    (deliberately, since ``..`` is symlink-dependent), so building the path with it leaves
    ``docs/../README.md``, which matches no tracked entry. Measured while writing this guard: that
    mistake reported 11 of the 45 real targets as dead.
    """
    # A trailing "#anchor" narrows a target inside a page; the page itself is what must exist.
    file_part = target.split("#", 1)[0]
    if not file_part:
        return True  # a pure "#anchor" target, out of scope (see the module docstring)

    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(source_file), file_part))
    if resolved in tracked:
        return True
    # A directory target ("examples/") is tracked only through its contents, never by name.
    return file_part.endswith("/") and any(p.startswith(resolved + "/") for p in tracked)


def test_every_relative_link_target_is_a_tracked_file() -> None:
    """No documentation link points at a path this repository does not contain."""
    tracked = _tracked_paths()
    dead = [
        f"{source}:{lineno} -> {target}"
        for source, lineno, target in _relative_link_targets()
        if not _resolves(source, target, tracked)
    ]
    assert not dead, "documentation links with no tracked target:\n  " + "\n  ".join(dead)


def test_the_link_population_is_not_empty() -> None:
    """The anchor against silent success.

    Every assertion above is over a comprehension, and a comprehension over nothing is empty, so a
    regex that stopped matching, a rename of the docs directory, or a ``git ls-files`` that returned
    nothing would all leave this module reporting a clean bill of health for a search it never ran.
    The floor is deliberately far below the current count (45 at the time of writing) so ordinary
    editing does not trip it; it catches collapse, not drift.
    """
    targets = _relative_link_targets()
    assert len(targets) >= 20, (
        f"only {len(targets)} relative link targets found across the tracked .md files; "
        "the extractor is probably broken rather than the documentation link-free"
    )


def test_the_extractor_sees_nested_and_directory_targets() -> None:
    """The two shapes a naive pattern misses, pinned to the real lines that carry them.

    Without this, someone simplifying ``_LINK_TARGET`` to the textbook ``\\[[^]]*\\]\\(([^)]+)\\)``
    would silently stop covering both, and every other test here would stay green.
    """
    targets = _relative_link_targets()
    readme = {(lineno, target) for source, lineno, target in targets if source == "README.md"}

    # The badge row: [![License: MIT](https://img.shields.io/...)](LICENSE)
    assert any(target == "LICENSE" for _, target in readme), (
        "the target of an image-inside-link was not extracted; README.md's badge row carries one"
    )
    # A directory target, which is tracked only through its contents.
    assert any(target == "examples/" for _, target in readme), (
        "a directory target was not extracted; README.md links to examples/"
    )


@pytest.mark.parametrize(
    ("target", "reason"),
    [
        ("docs/no-such-page.md", "a plain dead target"),
        ("readme.md", "a target that differs from the tracked path only in case"),
        ("docs/", "a directory that IS tracked through its contents, so this one must resolve"),
    ],
)
def test_resolution_rejects_what_it_should(target: str, reason: str) -> None:
    """The resolver's own behaviour, without touching a file.

    The case row is the one that matters most, and it is the one a disk-based check would get
    wrong: ``readme.md`` exists on a Windows filesystem and is not a tracked path.
    """
    tracked = _tracked_paths()
    resolved = _resolves("README.md", target, tracked)
    assert resolved is target.endswith("/"), f"{target}: {reason}"
