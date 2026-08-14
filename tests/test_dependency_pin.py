"""Drift guard for the engine pin: one revision, named the same in pyproject.toml and uv.lock.

WHY THIS EXISTS. The comment above `[tool.uv.sources]` promises that a bump moves the rev and
`uv lock` together, and USED TO NAME `uv sync --frozen` in CI as the thing that enforces it.
Measured against `uv help sync`, that flag enforces the opposite: "Instead of checking if the
lockfile is up-to-date, uses the versions in the lockfile as the source of truth. [...] If the
pyproject.toml includes changes to dependencies that have not been included in the lockfile yet,
they will not be present in the environment." A half-done bump therefore resolves the OLD engine in
silence, and nothing here noticed. (`--locked` would notice. Switching CI to it was judged a wider
change than this guard, so the promise got a test first.)

⚠️ **CI has since been switched to `--locked`, and that does NOT retire this guard.** The two
overlap rather than substitute: `--locked` refuses a lockfile whose dependency set disagrees with
``pyproject.toml``, while this module additionally reads the RESOLVED entry for the pinned revision,
which ``--locked`` never looks at. The half-done bump this was written for is now caught twice; the
resolved-entry half is caught only here.

WHAT IT CANNOT DO, said plainly because the sibling guard in the display MCP says it too: it
compares the two places that live in THIS repository. It cannot tell whether the pinned revision is
the RIGHT one, which is a decision rather than an invariant. That question is measured in the
producing repository, whose ratchet workflow fails when the distance grows, and reported locally by
`parallel_lint --context`. Nor does it stand in for an install check: this repository's CI never
installs the engine at all, because it syncs without `--group displays`.

AN ABSENT PIN IS A FINDING, NOT SILENCE, AND SO IS A LOST STRUCTURE. The comparison alone would
pass over an empty set, so what is expected is asserted before any value is compared. Six ways to
lose the subject were measured while designing this: a deleted `[tool.uv.sources]` table, a
`branch =` or `tag =` in place of `rev =`, a renamed package, a dropped dependency group, a move to
a registry source, and a URL swap that keeps the revision. Each makes a naive difference-of-sets
guard green while it checks nothing.

WHAT IS CHECKED IS THE STRUCTURE, NOT A TOTAL, and that distinction was earned the hard way: the
first version counted all commit ids and expected three. Three dependency groups reach that total
on their own, so a lockfile that had LOST its resolved entry read as clean, while a resolution fork
or a second group, both of which uv writes legitimately, read as broken.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

#: tests/ -> repository root. One level, because tests/ is flat here.
_ROOT = Path(__file__).resolve().parents[1]

#: The package name as uv normalises it (hyphen, not underscore) in both files.
_PINNED_PACKAGE = "opi-navigation"

#: A 40 hex commit in the query part of a git URL: the REQUESTED revision.
_REV_IN_QUERY = re.compile(r"[?&]rev=([0-9a-f]{40})\b")

#: The same commit as a URL fragment: the RESOLVED one. Today the two are byte equal, because the
#: request is itself a commit id. They would legitimately differ if the pin ever became a branch
#: name, which is why the count below is asserted rather than the equality being forced: a pin that
#: is not an immutable revision is reported as such instead of failing as a mismatch.
_REV_IN_FRAGMENT = re.compile(r"#([0-9a-f]{40})$")

#: Exactly one declaration is expected in pyproject.toml.
_EXPECTED_DECLARATIONS = 1

#: The commit ids in uv.lock sit in TWO KINDS of string field:
#:   package[name="epics-mcp"].metadata.requires-dev.<group>[].git  -> "...?rev=<sha>"
#:   package[name="opi-navigation"].source.git                      -> "...?rev=<sha>#<sha>"
#: The resolved package entry is the one that decides what gets installed, so its ABSENCE is a
#: finding on its own. Counting alone cannot say that, and that was a real defect: three
#: dependency groups produce three ids by themselves, and a lockfile whose resolved entry is
#: simply gone then passed as clean. Conversely a plain total went red on two shapes uv may
#: legitimately write, a resolution fork (the package twice) and a second group carrying the same
#: requirement. Structure is therefore checked per kind, not as one sum.
_EXPECTED_RESOLVED_ENTRIES = 1


def _commits_in(url: str) -> list[str]:
    """Every 40 hex commit id carried by one git URL, request first, resolution second."""
    return _REV_IN_QUERY.findall(url) + _REV_IN_FRAGMENT.findall(url)


def _repository_of(url: str) -> str:
    """A git URL reduced to the repository it names, without the query and fragment uv appends."""
    return url.split("?", 1)[0].split("#", 1)[0]


def _declared_urls(pyproject_text: str) -> list[str]:
    """The git URLs pinned for the engine in `[tool.uv.sources]`."""
    data = tomllib.loads(pyproject_text)
    source = data.get("tool", {}).get("uv", {}).get("sources", {}).get(_PINNED_PACKAGE)
    url = source.get("git") if isinstance(source, dict) else None
    return [url] if isinstance(url, str) else []


def _declared_revisions(pyproject_text: str) -> list[str]:
    """The `rev` values pinned for the engine in `[tool.uv.sources]`.

    A source that pins something other than a revision (a branch or a tag) contributes nothing, so
    the count check below reports it as a missing declaration. That is the honest outcome: such a
    source is not a pin at all.
    """
    data = tomllib.loads(pyproject_text)
    source = data.get("tool", {}).get("uv", {}).get("sources", {}).get(_PINNED_PACKAGE)
    if not isinstance(source, dict):
        return []
    rev = source.get("rev")
    return [rev] if isinstance(rev, str) and re.fullmatch(r"[0-9a-f]{40}", rev) else []


def _locked_urls(lock_text: str) -> tuple[list[str], list[str]]:
    """The engine's git URLs in uv.lock, split by kind: (resolved entries, requirement entries).

    Packages are looked up BY NAME and never by position: `uv.lock` records five resolution forks
    in its header, so a future lockfile may well carry the same package more than once, and an
    index that happens to be right today would silently read a neighbour tomorrow.

    Every container is type-checked before it is walked. uv does not write these shapes today, but
    a guard whose failure mode is `AttributeError` reports nothing useful about the file it was
    handed, and this one is read by a human looking at a red CI.
    """
    data = tomllib.loads(lock_text)
    resolved: list[str] = []
    required: list[str] = []
    packages = data.get("package")
    for package in packages if isinstance(packages, list) else []:
        if not isinstance(package, dict):
            continue
        source = package.get("source")
        if (
            package.get("name") == _PINNED_PACKAGE
            and isinstance(source, dict)
            and isinstance(source.get("git"), str)
        ):
            resolved.append(source["git"])
        metadata = package.get("metadata")
        groups = metadata.get("requires-dev") if isinstance(metadata, dict) else None
        for requirements in (groups or {}).values() if isinstance(groups, dict) else []:
            required.extend(
                requirement["git"]
                for requirement in (requirements if isinstance(requirements, list) else [])
                if isinstance(requirement, dict)
                and requirement.get("name") == _PINNED_PACKAGE
                and isinstance(requirement.get("git"), str)
            )
    return resolved, required


def pin_mismatches(pyproject_text: str, lock_text: str) -> list[str]:
    """Everything that makes these two files disagree about the engine revision.

    Pure on purpose: the red direction and the counter-direction are both provable from literals,
    which is how `tests/test_release_ready.py` earns its confidence too. Returning the reasons
    rather than asserting inside keeps the caller free to show all of them at once.
    """
    declared = _declared_revisions(pyproject_text)
    resolved_urls, required_urls = _locked_urls(lock_text)

    if len(declared) != _EXPECTED_DECLARATIONS:
        return [
            f"pyproject.toml declares {len(declared)} pinned revision(s) for {_PINNED_PACKAGE!r}, "
            f"expected {_EXPECTED_DECLARATIONS}. Either the dependency is gone (then this guard "
            f"belongs with it), or it no longer pins an immutable revision, in which case nothing "
            f"is being pinned at all."
        ]
    # The RESOLVED entry decides what gets installed, so its absence is its own finding. Counting
    # all ids together hid exactly this: several dependency groups reach the expected total on
    # their own, and a lockfile that had lost its resolved entry then read as clean.
    if len(resolved_urls) != _EXPECTED_RESOLVED_ENTRIES:
        return [
            f"uv.lock holds {len(resolved_urls)} resolved git entry for {_PINNED_PACKAGE!r}, "
            f"expected {_EXPECTED_RESOLVED_ENTRIES}. That entry is what --frozen installs: "
            f"without it the engine comes from somewhere else, and with several the file no "
            f"longer says which."
        ]
    if not required_urls:
        return [
            f"uv.lock names no dependency-group requirement for {_PINNED_PACKAGE!r}. The group "
            f"is how this repository asks for the engine at all, so its absence means the pin "
            f"below is not reached by any install."
        ]

    expected_repo = _repository_of(_declared_urls(pyproject_text)[0])
    # An URL swap is the OTHER half of the same half-done bump: same revision, different origin.
    # Comparing commit ids alone would call that consistent, which it is not.
    foreign = sorted(
        {url for url in resolved_urls + required_urls if _repository_of(url) != expected_repo}
    )
    if foreign:
        return [
            f"pyproject.toml points {_PINNED_PACKAGE!r} at {expected_repo!r}, uv.lock at "
            f"{[_repository_of(url) for url in foreign]}. Same revision, different origin: the "
            f"lockfile was not regenerated after the source moved."
        ]

    locked = [sha for url in resolved_urls + required_urls for sha in _commits_in(url)]
    stale = sorted(set(locked) - set(declared))
    if stale:
        return [
            f"pyproject.toml pins {declared[0]}, uv.lock names {stale}. The bump was only half "
            f"made: after changing the rev, `uv lock` has to run, or --frozen keeps installing "
            f"the old engine without saying so."
        ]
    return []


def test_the_pinned_revision_is_the_same_in_pyproject_and_uv_lock() -> None:
    """The real files. Everything else in this module proves the function; this proves the tree."""
    pyproject = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    lock = (_ROOT / "uv.lock").read_text(encoding="utf-8")

    # Positive control, and it stands BEFORE the comparison: without it a tree that lost the pin
    # entirely would satisfy the assertion below by having nothing to disagree about. Both KINDS
    # of lock entry are asserted, not their sum, because one kind can stand in for the other.
    resolved, required = _locked_urls(lock)
    assert len(_declared_revisions(pyproject)) == _EXPECTED_DECLARATIONS
    assert len(resolved) == _EXPECTED_RESOLVED_ENTRIES
    assert required

    assert pin_mismatches(pyproject, lock) == []


def test_a_consistent_pair_is_accepted() -> None:
    """The counter-direction. A guard that rejected everything would pass every red test below,
    and that failure mode is invisible to red tests alone."""
    assert pin_mismatches(_pyproject(_SHA_A), _lock(_SHA_A, _SHA_A, _SHA_A)) == []


def test_a_lockfile_left_behind_is_refused() -> None:
    """The measured failure: the rev moves, `uv lock` does not, and --frozen installs the old one.
    This is the shape the promise in pyproject.toml claimed CI would catch."""
    reasons = pin_mismatches(_pyproject(_SHA_B), _lock(_SHA_A, _SHA_A, _SHA_A))

    assert len(reasons) == 1, reasons
    assert _SHA_A in reasons[0]
    assert _SHA_B in reasons[0]


def test_a_resolution_that_disagrees_with_its_request_is_refused() -> None:
    """The fragment, which the sibling guard leaves out. A lockfile whose resolved commit differs
    from the requested one installs neither of the two revisions anybody wrote down."""
    reasons = pin_mismatches(_pyproject(_SHA_A), _lock(_SHA_A, _SHA_A, _SHA_B))

    assert len(reasons) == 1, reasons
    assert _SHA_B in reasons[0]


def test_a_vanished_declaration_is_refused_rather_than_passing_over_an_empty_set() -> None:
    """Removing `[tool.uv.sources]` is the shape a difference-of-sets guard reports as clean."""
    reasons = pin_mismatches("[project]\nname = 'epics-mcp'\n", _lock(_SHA_A, _SHA_A, _SHA_A))

    assert len(reasons) == 1, reasons
    assert "expected 1" in reasons[0]


def test_a_branch_pin_is_reported_as_no_pin_at_all() -> None:
    """`branch = "main"` resolves to whatever exists at install time. Reading it as a pin would be
    worse than saying nothing, because the file still LOOKS pinned."""
    pyproject = f'[tool.uv.sources]\n{_PINNED_PACKAGE} = {{ git = "{_URL}", branch = "main" }}\n'

    reasons = pin_mismatches(pyproject, _lock(_SHA_A, _SHA_A, _SHA_A))

    assert len(reasons) == 1, reasons
    assert "immutable revision" in reasons[0]


def test_a_registry_source_is_refused_rather_than_read_as_agreement() -> None:
    """If the engine ever came from an index, both sides would find nothing and a set difference
    would be empty. That is the one shape where silence and agreement look identical."""
    lock = (
        "[[package]]\n"
        f'name = "{_PINNED_PACKAGE}"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
    )

    reasons = pin_mismatches(_pyproject(_SHA_A), lock)

    assert len(reasons) == 1, reasons
    assert "resolved git entry" in reasons[0]


def test_a_lost_resolved_entry_is_refused_even_when_the_groups_still_add_up() -> None:
    """The defect a plain total could not see, found by the post-build QA.

    Three dependency groups carry three commit ids by themselves. With only a sum to go on, a
    lockfile whose resolved entry had vanished passed as clean, while --frozen would install the
    engine from wherever it liked."""
    lock = '[[package]]\nname = "epics-mcp"\n[package.metadata.requires-dev]\n' + "".join(
        f'{group} = [{{ name = "{_PINNED_PACKAGE}", git = "{_URL}?rev={_SHA_A}" }}]\n'
        for group in ("displays", "dev", "ci")
    )

    reasons = pin_mismatches(_pyproject(_SHA_A), lock)

    assert len(reasons) == 1, reasons
    assert "resolved git entry" in reasons[0]


def test_a_resolution_fork_is_not_mistaken_for_a_defect() -> None:
    """The counter-direction of the same fix, and the reason it is per-kind rather than a total.

    uv records resolution forks in this file's header, and a second dependency group is ordinary.
    Both raise the number of commit ids without anything being wrong; a sum-based guard went red
    on a lockfile uv had just written correctly."""
    lock = (
        "[[package]]\n"
        'name = "epics-mcp"\n'
        "[package.metadata.requires-dev]\n"
        f'displays = [{{ name = "{_PINNED_PACKAGE}", git = "{_URL}?rev={_SHA_A}" }}]\n'
        f'dev = [{{ name = "{_PINNED_PACKAGE}", git = "{_URL}?rev={_SHA_A}" }}]\n'
        "\n"
        "[[package]]\n"
        f'name = "{_PINNED_PACKAGE}"\n'
        f'source = {{ git = "{_URL}?rev={_SHA_A}#{_SHA_A}" }}\n'
    )

    assert pin_mismatches(_pyproject(_SHA_A), lock) == []


def test_a_lockfile_pointing_at_another_repository_is_refused() -> None:
    """The other half of a half-done bump: same revision, different origin.

    Comparing commit ids alone called this consistent. It is not: --frozen would then install the
    engine from a repository nobody declared."""
    lock = (
        "[[package]]\n"
        'name = "epics-mcp"\n'
        "[package.metadata.requires-dev]\n"
        f'displays = [{{ name = "{_PINNED_PACKAGE}", git = "{_URL}?rev={_SHA_A}" }}]\n'
        "\n"
        "[[package]]\n"
        f'name = "{_PINNED_PACKAGE}"\n'
        f'source = {{ git = "https://example.invalid/fork.git?rev={_SHA_A}#{_SHA_A}" }}\n'
    )

    reasons = pin_mismatches(_pyproject(_SHA_A), lock)

    assert len(reasons) == 1, reasons
    assert "different origin" in reasons[0]


def test_a_malformed_lockfile_is_reported_rather_than_crashing() -> None:
    """A guard whose failure mode is AttributeError says nothing about the file it was handed."""
    lock = '[[package]]\nname = "epics-mcp"\nmetadata = "not a table"\n'

    assert pin_mismatches(_pyproject(_SHA_A), lock)


_URL = "https://github.com/epicDirk/opi-foundry.git"
_SHA_A = "a" * 40
_SHA_B = "b" * 40


def _pyproject(rev: str) -> str:
    """A minimal pyproject carrying one engine pin."""
    return f'[tool.uv.sources]\n{_PINNED_PACKAGE} = {{ git = "{_URL}", rev = "{rev}" }}\n'


def _lock(group_rev: str, source_rev: str, resolved: str) -> str:
    """A minimal lockfile carrying the three commit ids uv writes, each settable on its own."""
    return (
        "[[package]]\n"
        'name = "epics-mcp"\n'
        "[package.metadata.requires-dev]\n"
        f'displays = [{{ name = "{_PINNED_PACKAGE}", git = "{_URL}?rev={group_rev}" }}]\n'
        "\n"
        "[[package]]\n"
        f'name = "{_PINNED_PACKAGE}"\n'
        f'source = {{ git = "{_URL}?rev={source_rev}#{resolved}" }}\n'
    )
