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

# README.md is the PyPI long_description, where a relative target has no repository around it and
# resolves against pypi.org (QA-30). Those links are absolute, and this is the shape they take.
# They are URLs, so the skip list above would drop them and the most-read page in the project would
# become the one page this guard does not check. Instead they are recognised, reduced to their
# repository path and resolved from the repository ROOT, so a rename still goes red.
_CANONICAL_PREFIXES = (
    "https://github.com/epicDirk/EPICS-MCP/blob/main/",
    "https://github.com/epicDirk/EPICS-MCP/tree/main/",
)


def _repo_relative(target: str) -> str | None:
    """The repository path a canonical GitHub URL points at, or ``None`` if it is not one."""
    for prefix in _CANONICAL_PREFIXES:
        if target.startswith(prefix):
            return target[len(prefix) :]
    return None


def _tracked_paths() -> frozenset[str]:
    """Every tracked path, exactly as git spells it, forward slashes and original case."""
    listing = subprocess.run(
        ["git", "-C", str(_REPO), "ls-files"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return frozenset(line.strip() for line in listing.splitlines() if line.strip())


def _relative_link_targets() -> list[tuple[str, int, str, str]]:
    """``(file, line number, target, base)`` for every checkable link target in a tracked ``.md``.

    *base* is what the target resolves against, and it is a SEPARATE slot from *file* on purpose.
    A canonical GitHub URL is repository-root-relative, so its base is ``""`` while its file stays
    the document that carries it: collapsing the two would resolve correctly and then report the
    finding against no file at all.
    """
    tracked = _tracked_paths()
    found: list[tuple[str, int, str, str]] = []
    for path in sorted(p for p in tracked if p.endswith(".md")):
        text = (_REPO / path).read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for target in _LINK_TARGET.findall(line):
                if _repo_relative(target) is not None:
                    found.append((path, lineno, target, ""))
                elif not target.startswith(_SKIPPED_PREFIXES):
                    found.append((path, lineno, target, path))
    return found


def _resolves(base: str, target: str, tracked: frozenset[str]) -> bool:
    """Does *target*, read relative to *base*, name something git tracks?

    ``posixpath.normpath`` rather than ``PurePosixPath``: the latter does NOT collapse ``..``
    (deliberately, since ``..`` is symlink-dependent), so building the path with it leaves
    ``docs/../README.md``, which matches no tracked entry. Measured while writing this guard: that
    mistake reported 11 of the 45 real targets as dead.
    """
    # A canonical GitHub URL carries the repository path directly; everything else is as written.
    target = _repo_relative(target) or target
    # A trailing "#anchor" narrows a target inside a page; the page itself is what must exist.
    file_part = target.split("#", 1)[0]
    if not file_part:
        return True  # a pure "#anchor" target, out of scope (see the module docstring)

    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(base), file_part))
    if resolved in tracked:
        return True
    # A directory target ("examples/") is tracked only through its contents, never by name.
    return file_part.endswith("/") and any(p.startswith(resolved + "/") for p in tracked)


def test_every_relative_link_target_is_a_tracked_file() -> None:
    """No documentation link points at a path this repository does not contain."""
    tracked = _tracked_paths()
    dead = [
        f"{source}:{lineno} -> {target}"
        for source, lineno, target, base in _relative_link_targets()
        if not _resolves(base, target, tracked)
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


def test_the_extractor_sees_the_nested_image_link() -> None:
    """The one shape a naive pattern misses, pinned to the LINE that carries it.

    Without this, someone simplifying ``_LINK_TARGET`` to the textbook ``\\[[^]]*\\]\\(([^)]+)\\)``
    would silently stop seeing it, and every other test here would stay green.

    The line number is load-bearing and the earlier value-only form was not. Measured: under the
    textbook pattern the badge row yields only the image URL (which this guard skips as a URL),
    while the real pattern also yields ``LICENSE``. But README.md carries a second, ordinary
    ``[LICENSE](LICENSE)`` further down that BOTH patterns see, so asserting merely that some
    ``LICENSE`` target exists passes under either one. That is a guard which cannot go red.

    The directory shape is not asserted here. It is not a shape the textbook pattern misses (both
    extract ``examples/`` identically), it is a RESOLVER case, and it is covered as one in
    ``test_resolution_rejects_what_it_should``.
    """
    targets = _relative_link_targets()
    readme = {(lineno, target) for source, lineno, target, _ in targets if source == "README.md"}

    # The badge row: [![License: MIT](https://img.shields.io/...)](<canonical URL for LICENSE>)
    assert (5, _CANONICAL_PREFIXES[0] + "LICENSE") in readme, (
        "the target of an image-inside-link was not extracted; README.md's badge row carries one"
    )


def test_the_readme_carries_no_bare_relative_link() -> None:
    """QA-30: on the PyPI project page there is no repository around the README, so a relative
    target resolves against pypi.org and lands nowhere. Measured on the rendered page before the
    fix: every relative target was dead there, while the absolute sidebar links caught the reader.

    This pins the INVARIANT rather than a count. A count ("at least twenty absolute targets") stays
    green when someone turns exactly one link back, which is the way this regresses.

    The operator guide is checked too, and for the same reason rather than for symmetry: it ships
    inside the wheel and is served as the ``epics-pv://guide`` resource, so it is the second surface
    with no repository around it. It carried no link at all until its two ``docs/`` pointers became
    absolute, which is what made this arm of the guard stop being vacuous; the point is that a
    future one cannot slip in relative, which the sibling guard would happily accept.

    Honest limit: this checks the SPELLING of a link, not whether the page renders it. Whether
    PyPI's renderer follows these URLs is not measurable from this tree.
    """
    surfaces = {"README.md", "src/epics_mcp/operator_guide.md"}
    bare = [
        f"{source}:{lineno} -> {target}"
        for source, lineno, target, base in _relative_link_targets()
        if source in surfaces and base != ""
    ]
    assert not bare, (
        "a target on a surface that ships without its repository must be an absolute "
        "GitHub URL, an anchor or a foreign URL:\n  " + "\n  ".join(bare)
    )


@pytest.mark.parametrize(
    ("target", "expected", "reason"),
    [
        ("docs/no-such-page.md", False, "a plain dead target"),
        ("readme.md", False, "a target that differs from the tracked path only in case"),
        ("docs/", True, "a directory that IS tracked through its contents, so this one resolves"),
        (
            "https://github.com/epicDirk/EPICS-MCP/blob/main/docs/tools.md",
            True,
            "a canonical URL is resolved through to its repository path, not skipped as a URL",
        ),
        (
            "https://github.com/epicDirk/EPICS-MCP/blob/main/docs/no-such-page.md",
            False,
            "a canonical URL whose repository path is dead must still be caught",
        ),
        (
            "https://github.com/epicDirk/EPICS-MCP/blob/main/Docs/tools.md",
            False,
            "case folding does not save a canonical URL either",
        ),
    ],
)
def test_resolution_rejects_what_it_should(target: str, expected: bool, reason: str) -> None:
    """The resolver's own behaviour, without touching a file.

    The case rows are the ones that matter most, and they are what a disk-based check would get
    wrong: ``readme.md`` exists on a Windows filesystem and is not a tracked path.

    ``expected`` is explicit rather than derived from the target's shape. It used to be
    ``target.endswith("/")``, which happened to describe the first three rows and cannot describe a
    canonical URL that must resolve.
    """
    tracked = _tracked_paths()
    base = "" if _repo_relative(target) is not None else "README.md"
    assert _resolves(base, target, tracked) is expected, f"{target}: {reason}"
