"""Drift guard for the publish workflow's two safety properties.

Both can break silently, and both fail in the one direction that cannot be undone: an upload that
should not have happened. Nothing else in the suite reads this file, so without these tests a
refactor of the workflow is unreviewed by anything but a human reading YAML.

Checked as TEXT rather than through a YAML parser, deliberately: PyYAML is not a declared
dependency of this project, and a guard that only runs when a transitive dependency happens to be
installed is not a guard. The same reasoning keeps ``packaging`` out of
``scripts/check_release_ready.py``, and ``tests/test_lint_scope.py`` reads
``.pre-commit-config.yaml`` the same way.
"""

from __future__ import annotations

from pathlib import Path

_WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "publish.yml"


def _lines() -> list[str]:
    return _WORKFLOW.read_text(encoding="utf-8").splitlines()


def test_the_workflow_exists() -> None:
    """Without this, every assertion below would pass vacuously on a deleted file."""
    assert _WORKFLOW.is_file(), f"{_WORKFLOW} is missing"


def test_only_the_publish_job_may_request_the_publishing_token() -> None:
    """``id-token: write`` is the credential that can upload. Granting it workflow-wide, or to the
    build job, would hand it to every step including third-party actions, for no gain: the build
    job needs no credential at all.
    """
    granting = [
        line for line in _lines() if "id-token" in line and not line.strip().startswith("#")
    ]

    assert len(granting) == 1, f"expected exactly one id-token grant, found: {granting}"
    assert granting[0].strip().startswith("id-token: write")


def test_the_upload_is_gated_on_a_tag_and_on_not_being_a_dry_run() -> None:
    """The manual trigger exists so the workflow can be rehearsed end to end without an account.
    That rehearsal is only safe while BOTH halves of the condition survive: a tag ref, and an
    explicit non-dry-run. Losing either one turns a rehearsal into a release.
    """
    conditions = [
        line
        for line in _lines()
        if line.strip().startswith("if:") and "dry_run" in line  # the publish job's condition;
        # the two release-gate steps carry their own tag-vs-rehearsal conditions since QA-16
    ]

    assert len(conditions) == 1, f"expected exactly one upload condition, found: {conditions}"
    condition = conditions[0]
    assert "refs/tags/v" in condition, condition
    assert "dry_run" in condition, condition


def test_the_release_gate_runs_before_anything_is_uploaded() -> None:
    """The gate is worthless if the upload does not depend on it. It sits in the build job, so the
    dependency is expressed by ``needs``; without that the two jobs would run in parallel and the
    upload could win.
    """
    text = _WORKFLOW.read_text(encoding="utf-8")
    gate_at = text.index("check_release_ready.py")
    needs_at = text.index("needs: build")
    publish_at = text.index("pypa/gh-action-pypi-publish")

    assert gate_at < needs_at < publish_at, "the gate must precede the publish job that needs it"


def test_the_build_clears_dist_before_building() -> None:
    """``uv build`` does not clear ``dist/``. Measured on a developer checkout: a three-week-old
    0.2.0 wheel was still sitting there, carrying a direct reference the current build no longer
    has, and ``dist/*`` would have uploaded it alongside the new files.
    """
    text = _WORKFLOW.read_text(encoding="utf-8")
    clear_at = text.index("rm -rf dist")
    build_at = text.index("run: uv build")

    assert clear_at < build_at, "dist/ must be cleared BEFORE the build, not after"


def test_the_tests_run_in_the_same_workflow_that_publishes() -> None:
    """CI runs on the tag push too, but nothing makes this workflow wait for it. A release built
    from a red tree is exactly the artifact that must not exist, so the check lives here as well.
    """
    assert any("uv run pytest" in line for line in _lines()), (
        "the publish workflow must run the suite itself; a green CI run on the same commit is a "
        "separate workflow this one does not depend on"
    )
