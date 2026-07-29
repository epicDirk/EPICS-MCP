"""The release gate, proven able to go red, on synthetic metadata rather than a build.

A guard that cannot fail is the defect it was meant to remove (CLAUDE.md, evidence discipline
point 5). This one guards a step that happens once per release and fails expensively: an index
never lets a version number be reused, so a wrong upload cannot be taken back, only superseded.

The probes are hand-written ``METADATA`` blocks, deliberately, for two reasons. A build takes
minutes and needs a toolchain, so a build-driven test would be skipped in exactly the environments
that most need the check. And a build can only ever produce the metadata this repository happens to
have TODAY, which cannot exercise the pre-release row or the empty-description row at all.

Honest limit, corrected here because the sentence that stood in its place claimed a test that does
not exist anywhere in this repository (grepped for it: no test module reads ``dist/``). Nothing
points the gate at the repository's OWN built artifacts, so a real ``METADATA`` shape the synthetic
blocks below fail to model is not seen by this module. That gap is deliberate rather than merely
open: such a test would have to ask whether ``dist/`` happens to exist on the machine running it,
which is green on a fresh checkout and red on a developer's, the "reads the environment instead of
faking it" shape this repository treats as a defect. The real artifacts are checked by the release
runbook and by the publish workflow, which build them first.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "scripts"))

from check_release_ready import (  # noqa: E402 (needs the sys.path splice above)
    check,
    check_one_build,
    is_prerelease,
    main,
    read_metadata,
)


def _metadata(
    *,
    name: str = "epics-mcp",
    version: str = "0.3.0",
    requires: tuple[str, ...] = ("fastmcp<4,>=3",),
    content_type: str | None = "text/markdown",
    description: str = "# EPICS PV MCP Server\n\nA real description.\n",
) -> str:
    """Build a METADATA block; every parameter is one thing the gate is supposed to notice."""
    lines = ["Metadata-Version: 2.4", f"Name: {name}", f"Version: {version}"]
    if content_type is not None:
        lines.append(f"Description-Content-Type: {content_type}")
    lines.extend(f"Requires-Dist: {requirement}" for requirement in requires)
    return "\n".join(lines) + "\n\n" + description


def test_a_publishable_artifact_passes() -> None:
    """The counter-direction. Without it, a gate that refused everything would pass every row
    below, which is the failure mode a red-only test cannot see."""
    assert check(_metadata(), allow_prerelease=False) == []


def test_a_direct_reference_is_refused() -> None:
    """RED-PROOF for the check that exists because of this repository's own history: the display
    extra shipped ``opi-navigation @ git+https://...`` in the metadata, which an index rejects."""
    metadata = _metadata(
        requires=("fastmcp<4,>=3", "opi-navigation @ git+https://example.invalid/x.git@abc123")
    )

    reasons = check(metadata, allow_prerelease=False)

    assert len(reasons) == 1, reasons
    assert "direct reference" in reasons[0]
    assert "opi-navigation" in reasons[0]


def test_a_direct_reference_inside_an_extra_is_refused_too() -> None:
    """The measured shape, not the tidy one: the real finding sat behind an environment marker,
    and it appeared TWICE because the ``all`` extra resolved ``displays`` transitively. A check
    that looked only at unconditional requirements would have passed the artifact that failed."""
    metadata = _metadata(
        requires=(
            "opi-navigation @ git+https://example.invalid/x.git@abc123 ; extra == 'displays'",
            "opi-navigation @ git+https://example.invalid/x.git@abc123 ; extra == 'all'",
        )
    )

    reasons = check(metadata, allow_prerelease=False)

    assert len(reasons) == 2, reasons
    assert all("direct reference" in reason for reason in reasons)


def test_an_at_sign_in_a_marker_alone_is_not_a_direct_reference() -> None:
    """The false-positive direction for the same check. An '@' can legitimately appear in marker
    text; splitting on ';' first is what keeps that from reading as a URL."""
    metadata = _metadata(requires=('requests>=2.25 ; extra == "mail@example.invalid"',))

    assert check(metadata, allow_prerelease=False) == []


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("0.3.0.dev0", True),
        ("0.3.0b1", True),
        ("1.0rc2", True),
        ("1.2.3a0", True),
        ("0.3.0", False),
        ("2026.1", False),
        ("1.0.0", False),
    ],
)
def test_prerelease_detection(version: str, expected: bool) -> None:
    """RED-PROOF for the version check, both directions in one table. The false rows matter as
    much as the true ones: a detector that called everything a pre-release would block every real
    release, and would look correct in a test that only asserted the true rows."""
    assert is_prerelease(version) is expected


def test_a_prerelease_version_is_refused_but_can_be_allowed() -> None:
    """The working version between releases is ``0.3.0.dev0``; shipping it by accident cannot be
    undone. The escape hatch exists because a deliberate pre-release upload is a real workflow."""
    metadata = _metadata(version="0.3.0.dev0")

    refused = check(metadata, allow_prerelease=False)
    allowed = check(metadata, allow_prerelease=True)

    assert len(refused) == 1 and "pre-release" in refused[0]
    assert allowed == []


def test_a_version_mismatch_with_the_tag_is_refused() -> None:
    """QA-2: the workflow builds whatever ``pyproject.toml`` says, so a ``v0.3.0`` tag pushed
    against a tree still at ``0.4.0`` would upload the WRONG version under that tag, and an index
    never lets either number be reused. The expected version is exact-match; the reason has to
    name both numbers, because the reader's first question is which side is wrong."""
    reasons = check(_metadata(version="0.4.0"), allow_prerelease=False, expect_version="0.3.0")

    assert len(reasons) == 1, reasons
    assert "mismatch" in reasons[0]
    assert "'0.3.0'" in reasons[0] and "'0.4.0'" in reasons[0]


def test_a_version_matching_the_tag_passes() -> None:
    """The counter-direction: a gate that refused every expected version would block every real
    release and still pass the row above."""
    assert check(_metadata(version="0.3.0"), allow_prerelease=False, expect_version="0.3.0") == []


def test_the_version_expectation_composes_with_the_prerelease_check() -> None:
    """Both findings must surface together: ``--allow-prerelease`` must not smuggle a mismatched
    version past the tag pin, and the mismatch must not hide the pre-release reason either."""
    reasons = check(_metadata(version="0.4.0.dev0"), allow_prerelease=False, expect_version="0.3.0")
    assert len(reasons) == 2, reasons

    allowed = check(_metadata(version="0.4.0.dev0"), allow_prerelease=True, expect_version="0.3.0")
    assert len(allowed) == 1 and "mismatch" in allowed[0]


def test_the_expect_version_flag_reaches_the_check(tmp_path: Path) -> None:
    """Through ``main``, so the argparse plumbing is proven, not assumed: the same wheel passes
    bare and blocks under a mismatched ``--expect-version``."""
    wheel = tmp_path / "epics_mcp-0.3.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("epics_mcp-0.3.0.dist-info/METADATA", _metadata())

    assert main([str(wheel)]) == 0
    assert main(["--expect-version", "0.3.0", str(wheel)]) == 0
    assert main(["--expect-version", "9.9.9", str(wheel)]) == 1


@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [
        ({"content_type": None}, "Description-Content-Type"),
        ({"description": "   \n"}, "empty"),
    ],
)
def test_an_unrenderable_description_is_refused(kwargs: dict[str, object], fragment: str) -> None:
    """The long description IS the project page, and a published file gets no second chance."""
    reasons = check(_metadata(**kwargs), allow_prerelease=False)  # type: ignore[arg-type]

    assert len(reasons) == 1, reasons
    assert fragment in reasons[0]


def test_an_unreadable_artifact_is_a_finding_not_a_pass(tmp_path: Path) -> None:
    """A gate that treats "cannot read it" as "nothing wrong with it" is worse than no gate: it
    reports success on exactly the artifacts nobody inspected."""
    broken = tmp_path / "broken.whl"
    broken.write_bytes(b"this is not a zip file")

    assert main([str(broken)]) == 1


def test_a_non_artifact_argument_is_refused(tmp_path: Path) -> None:
    """Pointing the gate at the wrong file (a README, a stray path from a shell glob) must not
    silently report a clean run."""
    stray = tmp_path / "README.md"
    stray.write_text("not a distribution\n", encoding="utf-8")

    assert main([str(stray)]) == 1


def test_a_wheel_is_read_from_its_dist_info(tmp_path: Path) -> None:
    """:func:`read_metadata` must find METADATA inside a real zip, not just parse a string.

    Without this the whole module could pass while the gate never managed to open an artifact.
    """
    wheel = tmp_path / "epics_mcp-0.3.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("epics_mcp-0.3.0.dist-info/METADATA", _metadata())
        archive.writestr("epics_mcp/__init__.py", "")

    assert "Name: epics-mcp" in read_metadata(wheel)
    assert main([str(wheel)]) == 0


def test_every_artifact_is_checked_not_just_the_first(tmp_path: Path) -> None:
    """A release uploads whatever is in ``dist/``, so the gate has to judge all of it.

    Not hypothetical: this checkout's ``dist/`` held 0.2.0 artifacts from an earlier build, three
    weeks stale, still carrying the direct reference. ``uv build`` does not clear the directory, so
    a run that built and then uploaded ``dist/*`` would have shipped them alongside the new ones.
    A gate that stopped at the first clean file would have waved that through.
    """
    good = tmp_path / "good-1.0-py3-none-any.whl"
    stale = tmp_path / "stale-0.2.0-py3-none-any.whl"
    for path, metadata in (
        (good, _metadata(version="1.0")),
        (stale, _metadata(version="0.2.0", requires=("x @ git+https://example.invalid/x.git",))),
    ):
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(f"{path.stem.split('-py3')[0]}.dist-info/METADATA", metadata)

    assert main([str(good)]) == 0, "the clean artifact alone must pass"
    assert main([str(good), str(stale)]) == 1, "a stale artifact behind a clean one must block"


def _wheel(path: Path, metadata: str) -> Path:
    """A minimal but real wheel carrying *metadata*, so the gate opens an archive, not a string."""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{path.stem.split('-py3')[0]}.dist-info/METADATA", metadata)
    return path


def test_one_build_passes_however_many_artifacts_it_has() -> None:
    """The counter-direction, and it has to come first.

    A set check that refused every multi-artifact run would satisfy the red proof below while
    blocking every real release: a build produces a wheel AND an sdist, and they are handed to the
    gate together by the ``dist/*`` glob the runbook prescribes.
    """
    one_build = [("epics_mcp-0.4.0-py3-none-any.whl", _metadata(version="0.4.0"))]
    assert check_one_build(one_build) == [], "a single artifact cannot disagree with itself"
    one_build.append(("epics_mcp-0.4.0.tar.gz", _metadata(version="0.4.0")))
    assert check_one_build(one_build) == [], "a wheel and its sdist are one build"


def test_a_name_spelled_two_ways_is_still_one_build() -> None:
    """``epics_mcp`` and ``epics-mcp`` are the same distribution (PEP 503), and a backend may emit
    either into ``Name``. Comparing raw strings would report a build against itself, which is the
    false positive that would get this check disabled the first time it fired."""
    assert (
        check_one_build(
            [
                ("a.whl", _metadata(name="epics_mcp", version="0.4.0")),
                ("b.tar.gz", _metadata(name="epics-mcp", version="0.4.0")),
            ]
        )
        == []
    )


def test_a_leftover_build_beside_a_fresh_one_is_refused(tmp_path: Path) -> None:
    """QA-21, and the case the existing set test does NOT cover.

    ``test_every_artifact_is_checked_not_just_the_first`` uses a stale artifact that carries a
    direct reference, so it blocks on its own merits. The real leftover in this checkout did not:
    ``epics-pv-mcp 0.3.0.dev0``, a pre-rename name on a pre-release version, individually clean of
    everything the three per-artifact checks look at. On the TAG path ``--expect-version`` already
    catches it. The REHEARSAL path passes no expected version and allows pre-releases, and that is
    where it went through. Both artifacts below are individually clean, so nothing but the set
    check can refuse them.
    """
    fresh = _wheel(tmp_path / "epics_mcp-0.4.0-py3-none-any.whl", _metadata(version="0.4.0"))
    leftover = _wheel(
        tmp_path / "epics_pv_mcp-0.3.0.dev0-py3-none-any.whl",
        _metadata(name="epics-pv-mcp", version="0.3.0.dev0"),
    )
    rehearsal = ["--allow-prerelease"]

    assert main([*rehearsal, str(fresh)]) == 0, "the fresh artifact alone must pass"
    assert main([*rehearsal, str(leftover)]) == 0, (
        "and so must the leftover ALONE, or this test proves the wrong thing: it is clean on every "
        "per-artifact check, which is exactly why only a set check can see it"
    )
    assert main([*rehearsal, str(fresh), str(leftover)]) == 1

    reasons = check_one_build(
        [(fresh.name, read_metadata(fresh)), (leftover.name, read_metadata(leftover))]
    )
    assert len(reasons) == 1, reasons
    # Both builds named with the spelling a reader will see on disk, plus the way out.
    assert "epics-mcp 0.4.0" in reasons[0]
    assert "epics-pv-mcp 0.3.0.dev0" in reasons[0]
    assert leftover.name in reasons[0], "the file to delete has to be named"
    assert "rm -rf dist" in reasons[0]
