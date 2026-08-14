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

    QA-1: the first version only COUNTED the grants, which stayed green when the one grant was
    moved into the build job, the exact failure the test is named after. The grant's line is now
    bound to its job by position: after ``publish:``, never between ``build:`` and ``publish:``.
    """
    lines = _lines()
    build_at = next(i for i, line in enumerate(lines) if line.rstrip() == "  build:")
    publish_at = next(i for i, line in enumerate(lines) if line.rstrip() == "  publish:")
    granting = [
        i for i, line in enumerate(lines) if "id-token" in line and not line.strip().startswith("#")
    ]

    assert len(granting) == 1, f"expected exactly one id-token grant, found lines: {granting}"
    assert lines[granting[0]].strip().startswith("id-token: write")
    assert build_at < publish_at < granting[0], (
        "the id-token grant must sit inside the publish job, "
        f"found it at line {granting[0] + 1} (build: {build_at + 1}, publish: {publish_at + 1})"
    )


def test_the_upload_is_gated_on_a_tag_and_on_not_being_a_dry_run() -> None:
    """The manual trigger exists so the workflow can be rehearsed end to end without an account.
    That rehearsal is only safe while BOTH halves of the condition survive: a tag ref, and an
    explicit non-dry-run. Losing either one turns a rehearsal into a release.

    QA-1: the first version asserted two substrings, which stayed green under an ``&&`` to ``||``
    swap (a branch dispatch with dry_run=false would have attempted an upload) and under ``!=``
    to ``==`` (a rehearsal dispatched on a tag ref would have uploaded). The condition is pinned
    as an exact string, so any edit to it is a conscious edit of this test too.
    """
    conditions = [
        line.strip()
        for line in _lines()
        if line.strip().startswith("if:") and "dry_run" in line  # the publish job's condition;
        # the two release-gate steps carry their own tag-vs-rehearsal conditions since QA-16
    ]

    assert conditions == ["if: startsWith(github.ref, 'refs/tags/v') && inputs.dry_run != true"], (
        f"the publish condition changed, decide consciously: {conditions}"
    )


def test_the_release_gate_runs_before_anything_is_uploaded() -> None:
    """The gate is worthless if the upload does not depend on it. It sits in the build job, so the
    dependency is expressed by ``needs``; without that the two jobs would run in parallel and the
    upload could win.

    QA-1: the first version located the gate with ``text.index`` over the whole file, which a
    commented-out gate step still satisfied (the filename survives in the comment). Only lines
    that are actual ``run:`` steps count now, and both gate shapes are pinned with their flags,
    so swapping ``--allow-prerelease`` onto the tag path goes red as well.
    """
    lines = _lines()
    gate_runs = [
        (i, line.strip())
        for i, line in enumerate(lines)
        if "check_release_ready.py" in line and line.strip().startswith("run:")
    ]
    needs_at = next(i for i, line in enumerate(lines) if line.strip() == "needs: build")
    upload_at = next(i for i, line in enumerate(lines) if "pypa/gh-action-pypi-publish" in line)

    assert [text for _i, text in gate_runs] == [
        "run: uv run python scripts/check_release_ready.py --allow-prerelease dist/*",
        'run: uv run python scripts/check_release_ready.py --expect-version "${GITHUB_REF_NAME#v}"'
        " dist/*",
    ], f"the gate steps changed, decide consciously: {gate_runs}"
    assert max(i for i, _text in gate_runs) < needs_at < upload_at, (
        "both gate shapes must precede the publish job that needs them"
    )


def test_each_gate_shape_binds_to_its_ref() -> None:
    """The rehearsal gate must fire on non-tag refs and the strict gate on tag refs, ADJACENTLY:
    a condition is YAML that binds to whatever step it happens to sit in, so the pairing itself
    is the safety property. Pinned as the exact ordered sequence of every ``if:`` line and every
    live gate line, which also reddens a commented-out condition or a swapped pair."""
    relevant = [
        line.strip()
        for line in _lines()
        if line.strip().startswith("if:")
        or ("check_release_ready.py" in line and line.strip().startswith("run:"))
    ]

    assert relevant == [
        "if: \"!startsWith(github.ref, 'refs/tags/v')\"",
        "run: uv run python scripts/check_release_ready.py --allow-prerelease dist/*",
        "if: startsWith(github.ref, 'refs/tags/v')",
        'run: uv run python scripts/check_release_ready.py --expect-version "${GITHUB_REF_NAME#v}"'
        " dist/*",
        "if: startsWith(github.ref, 'refs/tags/v') && inputs.dry_run != true",
    ], f"the gate/upload condition sequence changed, decide consciously: {relevant}"


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

    Bound to the BUILD job by position rather than found anywhere in the file, the same idiom the
    token test above uses and for the same reason: a ``uv run pytest`` line that drifted into
    another job, or into a comment, satisfies a whole-file scan while the artifact is built
    ungated.
    """
    lines = _lines()
    build_at = next(i for i, line in enumerate(lines) if line.rstrip() == "  build:")
    publish_at = next(i for i, line in enumerate(lines) if line.rstrip() == "  publish:")
    running = [
        i
        for i, line in enumerate(lines)
        if "uv run pytest" in line and not line.strip().startswith("#")
    ]

    assert running, (
        "the publish workflow must run the suite itself; a green CI run on the same commit is a "
        "separate workflow this one does not depend on"
    )
    assert all(build_at < at < publish_at for at in running), (
        f"the suite must run in the build job (lines {build_at}..{publish_at}), found at {running}"
    )


def test_the_lint_chain_runs_in_the_same_workflow_that_publishes() -> None:
    """The other half of the argument above, and it was missing until this guard existed.

    ``ci.yml`` has two jobs, ``test`` and ``lint``; this workflow replicated the first and left the
    second behind, so a release was built and uploaded without ruff, without ruff-format, without
    mypy --strict and without the two prose guards. The consequence is not symmetric with a failing
    test, which is why this is not merely tidiness: ``pre-commit`` is the only place
    ``scripts/check_no_ess_internal.py`` runs, the only thing in this repository that scans the
    tree for a credential at all (its built-in formats; the site-specific patterns live in a
    git-ignored file that no runner checkout materialises, which ``CLAUDE.md`` records as well),
    ``src/`` ships in the wheel and ``docs/`` in the sdist, and a version on a package index can be
    superseded but never withdrawn. Nothing upstream compensates: ``main`` carries no branch
    protection and no ruleset, so no required status check stands between a red commit and a tag.

    Red proof: delete the ``pre-commit`` step from the build job and this fails on the first
    assertion; move it into another job and it fails on the second.
    """
    lines = _lines()
    build_at = next(i for i, line in enumerate(lines) if line.rstrip() == "  build:")
    publish_at = next(i for i, line in enumerate(lines) if line.rstrip() == "  publish:")
    linting = [
        i
        for i, line in enumerate(lines)
        if "pre-commit run" in line and not line.strip().startswith("#")
    ]

    assert linting, (
        "the publish workflow must run the pre-commit chain itself, the same one ci.yml's lint "
        "job runs; a green CI run on the same commit is a separate workflow this one does not "
        "depend on and cannot be blocked by"
    )
    assert all(build_at < at < publish_at for at in linting), (
        f"the lint chain must run in the build job (lines {build_at}..{publish_at}), found at "
        f"{linting}"
    )


def test_every_action_reference_is_pinned_in_order() -> None:
    """QA-23: nothing here read the ``uses:`` lines at all, and they are the release's supply chain.

    Pinned as the exact ordered list rather than checked one by one, so an action ADDED to this
    workflow is a conscious edit of this test too. The set matters beyond version drift: this
    workflow's own comment records that ``astral-sh/setup-uv`` publishes v8.x and v9.x but no
    floating major tag for either, so a routine bump to ``@v9`` would not resolve and would break
    the release outright.

    ⚠️ "Pinned" here means "pinned in THIS list", which is a weaker property than the name suggests
    and is worth saying plainly: five of the six references are major TAGS, which the owner can
    move without anyone editing this file, so this test notices a bump and not a hijack. Exactly
    one reference is a COMMIT, and that asymmetry is deliberate rather than half-finished work: the
    publishing action is the only one that runs in a job holding ``id-token: write``, and it used
    to resolve through ``release/v1``, a mutable BRANCH. Its version comment is inside the pinned
    string on purpose, so a SHA and a comment that no longer agree are a red test rather than a
    convincing-looking lie.
    """
    refs = [
        line.strip().split("uses:", 1)[1].strip()
        for line in _lines()
        if "uses:" in line and not line.strip().startswith("#")
    ]

    assert refs == [
        "actions/checkout@v5",
        "astral-sh/setup-uv@v7",
        "actions/upload-artifact@v7",
        "actions/download-artifact@v8",
        "pypa/gh-action-pypi-publish@dc37677b2e1c63e2034f94d8a5b11f265b73ba33 # v1.14.2",
        # The github-release job's own checkout, added consciously: it needs CHANGELOG.md and the
        # slicer script. Everything else that job does is `gh`, which is preinstalled on the runner,
        # so no THIRD-PARTY action joined the release's supply chain here.
        "actions/checkout@v5",
    ], f"the action set or its versions changed, decide consciously: {refs}"


def test_the_github_release_follows_the_upload_and_cannot_publish() -> None:
    """The release job must sit AFTER the upload and hold no publishing credential.

    Two properties, and they fail in opposite directions. Announcing a version whose upload failed
    is the same disagreement between the two public faces of this project that the job exists to
    remove, only pointing the other way; and a `contents: write` job is the one place a careless
    edit could quietly grant itself more than it needs.

    It deliberately carries NO `if:` of its own: a job whose `needs` was skipped is skipped too, so
    the rehearsal path stops here exactly as it stops at the upload. That is also why the two
    condition tests above still see a single dry-run condition. Adding an `if:` here would make
    those tests red, which is the intended prompt to think about it.

    Red-proof (mutation): change `needs: publish` to `needs: build` and the ordering assertion
    fails; add `id-token: write` here and
    test_only_the_publish_job_may_request_the_publishing_token fails.
    """
    lines = _lines()
    publish_at = next(i for i, line in enumerate(lines) if line.rstrip() == "  publish:")
    release_at = next(i for i, line in enumerate(lines) if line.rstrip() == "  github-release:")
    needs = [i for i, line in enumerate(lines) if line.strip() == "needs: publish"]

    assert publish_at < release_at, "the release job must be declared after the upload job"
    assert len(needs) == 1 and release_at < needs[0], (
        f"the release job must declare `needs: publish`, found the line at {needs}"
    )
    grants = [
        line.split("#", 1)[0].strip()  # the trailing comment is documentation, not a grant
        for line in lines[release_at:]
        if line.strip().startswith(("id-token", "contents"))
    ]
    assert grants == ["contents: write"], (
        f"the release job may create a release and nothing more, found {grants}"
    )


def test_the_release_body_comes_from_the_changelog() -> None:
    """`--generate-notes` would list commit subjects and ignore the changelog this project keeps.

    Pinned on both halves: the slicer is invoked, and its output is what `gh` is handed. Asserting
    only the first would stay green if the notes file were built and then not used.

    Read over the LIVE lines only, never the whole file. This test's first version asserted
    ``"--generate-notes" not in text`` and went red on the workflow comment explaining why that
    flag is not used, which is the same comment-blindness the gate tests above record twice.
    """
    live = "\n".join(line for line in _lines() if not line.strip().startswith("#"))
    assert "scripts/changelog_section.py" in live, "the release body must come from the changelog"
    assert "--notes-file release-notes.md" in live
    assert "--generate-notes" not in live, "commit subjects would replace the maintained changelog"
    assert "--verify-tag" in live, "a release must not be created for a tag that does not exist"


def _artifact_step_name(lines: list[str], action: str) -> str:
    """The ``name:`` the step using *action* passes, bound to that step by position.

    Deliberately NOT "collect every name: and hope two of them agree". This file carries a dozen
    ``name:`` lines, and the publish job's ``environment: name: pypi`` sits BETWEEN the two
    artifact steps, so a whole-file reader would compare the wrong pair.

    The step ends at the first line indented no deeper than its own dash. Delimiting on the next
    step's dash instead is not enough, and this test caught that on its first run: the upload is
    the LAST step of the build job, so a dash-delimited scan ran past the job boundary and reached
    exactly the ``environment: name: pypi`` this docstring warns about.

    Exactly one ``name:`` is required inside the step. The two artifact steps are the only ones
    here without a step-level name, so adding one after ``uses:`` (the ordinary future edit) goes
    red HERE rather than quietly supplying the wrong value.
    """
    uses = [i for i, line in enumerate(lines) if f"uses: {action}@" in line]
    assert len(uses) == 1, f"expected exactly one step using {action}, found lines {uses}"
    start = uses[0]
    dash_indent = len(lines[start]) - len(lines[start].lstrip())
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if not lines[i].strip():
            continue
        if len(lines[i]) - len(lines[i].lstrip()) <= dash_indent:
            end = i
            break
    names = [line.strip() for line in lines[start:end] if line.strip().startswith("name:")]
    assert len(names) == 1, (
        f"expected exactly one name: inside the {action} step, found {names}. A step-level name "
        "written after uses: has to be moved above it, or this binding reads the wrong line"
    )
    return names[0].removeprefix("name:").strip()


def test_the_artifact_is_downloaded_under_the_name_it_was_uploaded_with() -> None:
    """The two jobs are joined by a STRING, and nothing compared the two ends of it.

    ``upload-artifact`` names the artifact; ``download-artifact`` asks for that name. A typo in
    either one leaves the publish job with an empty ``dist/`` in the single job that runs once, at
    release time. Nothing else notices: the upload succeeds, the download of a missing name is the
    failure, and the rehearsal path never reaches the download step at all.
    """
    lines = _lines()
    uploaded = _artifact_step_name(lines, "actions/upload-artifact")
    downloaded = _artifact_step_name(lines, "actions/download-artifact")

    assert uploaded == downloaded, (
        f"the publish job asks for {downloaded!r} but the build job uploads {uploaded!r}; the "
        "download would find nothing and the release would ship an empty dist/"
    )
    assert uploaded == "distributions", (
        f"the artifact name changed to {uploaded!r}; harmless in itself, but this test is the only "
        "thing tying the two jobs together, so the change is made consciously here"
    )


def test_the_download_states_its_digest_behaviour_instead_of_inheriting_it() -> None:
    """A safety default in the job that cannot be re-run is written down, not inherited.

    Measured against ``actions/download-artifact``'s own ``action.yml`` at v8: ``digest-mismatch``
    accepts ignore/info/warn/error and defaults to ``error``. Stating it changes nothing today and
    keeps a later v8.x from changing it silently, in the one step whose failure mode is a
    corrupted artifact reaching a package index that never lets a version be reused.
    """
    lines = _lines()
    start = next(i for i, line in enumerate(lines) if "uses: actions/download-artifact@" in line)
    dash_indent = len(lines[start]) - len(lines[start].lstrip())
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if not lines[i].strip():
            continue
        if len(lines[i]) - len(lines[i].lstrip()) <= dash_indent:
            end = i
            break
    stated = [
        line.strip() for line in lines[start:end] if line.strip().startswith("digest-mismatch")
    ]

    assert stated == ["digest-mismatch: error"], (
        f"the download step must state digest-mismatch, found {stated}. Anything but 'error' lets "
        "a corrupted artifact through to the upload"
    )
