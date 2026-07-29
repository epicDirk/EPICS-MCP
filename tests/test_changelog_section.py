"""Tests for the changelog slicer that supplies the GitHub release body.

It runs in the release workflow, in the job that follows a successful upload, so a wrong slice
would describe an already-published package. That is the one shape of failure worth guarding: not
"it crashed", but "it silently returned the wrong section, or nothing".
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_CHANGELOG = _REPO / "CHANGELOG.md"

# The house way of reaching scripts/ (tests/test_release_ready.py does the same): scripts/ is not a
# package, and importing it as `scripts.x` would make mypy see the file under two module names.
sys.path.insert(0, str(_REPO / "scripts"))

from changelog_section import extract, main  # noqa: E402 (needs the sys.path splice above)

_SAMPLE = """# Changelog

Preamble that belongs to no version.

## [Unreleased]

### Added

- something not yet released

## [0.4.0] - 2026-07-29

### Changed

- the fourth thing

## [0.3.0] - 2026-07-28

- the third thing

## [0.2.0]

- the second thing
"""


def test_a_middle_section_stops_at_the_next_version() -> None:
    """The slice must end at the next heading, not run to the end of the file. A release body that
    trailed off into every older version would be worse than none."""
    assert extract(_SAMPLE, "0.4.0") == "### Changed\n\n- the fourth thing"


def test_the_last_section_runs_to_the_end_of_the_file() -> None:
    """The oldest version has no following heading to stop at, so it needs the other branch."""
    assert extract(_SAMPLE, "0.2.0") == "- the second thing"


def test_unreleased_is_addressable_by_name() -> None:
    """Not a version number, but it is a heading like any other, and asking for it is legitimate
    (a rehearsal, or checking what is staged)."""
    assert extract(_SAMPLE, "Unreleased") == "### Added\n\n- something not yet released"


@pytest.mark.parametrize("asked", ["0.3.0", "v0.3.0"])
def test_a_tag_name_works_as_well_as_a_bare_version(asked: str) -> None:
    """The workflow passes GITHUB_REF_NAME, which carries the leading v. Stripping it at the call
    site would mean every caller has to remember to."""
    assert extract(_SAMPLE, asked) == "- the third thing"


def test_a_missing_version_is_an_error_not_an_empty_body() -> None:
    """The failure that matters. An empty string here would publish a release that says nothing
    about a version somebody is about to install, and the workflow step would still be green.

    Red-proof (mutation): return "" instead of raising and this test fails, while every test above
    keeps passing.
    """
    with pytest.raises(SystemExit) as excinfo:
        extract(_SAMPLE, "9.9.9")
    message = str(excinfo.value)
    assert "9.9.9" in message
    assert "0.4.0" in message, "the error must list what IS available, or it cannot be acted on"


def test_the_real_changelog_yields_a_body_for_the_current_version() -> None:
    """Crossed against the file that will actually be sliced, not only the sample above: the sample
    is a copy, and a copy stays unguarded until it is checked against the real thing. Pinned to the
    version pyproject.toml declares, so a version bump without a changelog section goes red HERE
    rather than in the release job.
    """
    pyproject = (_CHANGELOG.parent / "pyproject.toml").read_text(encoding="utf-8")
    version = next(
        line.split("=", 1)[1].strip().strip('"')
        for line in pyproject.splitlines()
        if line.startswith("version")
    )
    body = extract(_CHANGELOG.read_text(encoding="utf-8"), version)
    assert body, f"CHANGELOG.md has no section for the declared version {version}"
    assert not body.startswith("## ["), "the heading itself must not be part of the body"


def test_the_cli_prints_the_section(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    """The entry point the workflow calls, including its argument shape."""
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(_SAMPLE, encoding="utf-8")
    assert main(["v0.3.0", "--changelog", str(changelog)]) == 0
    assert capsys.readouterr().out.strip() == "- the third thing"
