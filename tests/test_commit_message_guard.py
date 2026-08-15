"""Guard for the commit-message guard (GB-33).

WHY THESE ASSERTIONS EXIST AT ALL. ``pre-commit run --all-files`` drives the pre-commit stage
only, so the ``commit-msg`` hook never executes in CI, and a hook nothing exercises is a hook
nobody notices breaking. Its decision function is pure, so its behaviour is pinned here instead,
without a git repository and without the hook being installed.

⚠️ WHAT THIS MODULE CANNOT DO, said plainly because the gap is the interesting part. It holds the
WIRING (the stage entry and ``default_install_hook_types`` in ``.pre-commit-config.yaml``) and the
DECISION (which messages are refused). It cannot hold the INSTALLATION: ``.git/hooks/commit-msg``
is per-clone state outside the tree, and no test can assert a file into existence there. On a
fresh clone this guard is inert until somebody runs ``pre-commit install --hook-type commit-msg``.
That limit is repeated at the script and in CONTRIBUTING.md rather than left to be discovered.

THE PV-SHAPED FIXTURES ARE IMPORTED, NOT TYPED OUT, and that is the same rule as everything else
here: ONE declaration, several readers. ``test_guide.py`` scans every tracked file with this very
detector and subtracts only the shapes IT declares, so a literal device name written into this
module would be reported by that scan, correctly. Writing one anyway and widening the exemption to
a second file would turn a documented, self-granted allowance for one file into a habit. Importing
the declared tuple keeps the allowance where it is, and every shape stays invented (no real
facility segment), which is what makes stating it possible at all.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

from tests.test_guide import _PV_SHAPES_DECLARED_HERE

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "scripts"))

from check_commit_message import findings, message_lines  # noqa: E402 (sys.path splice above)

_CONFIG = _ROOT / ".pre-commit-config.yaml"

#: pre-commit's real default: a hook that names no stage runs at EVERY stage. Only the two that
#: matter here are listed, because the assertion asks a membership question and a fuller list
#: would suggest this is a mirror of pre-commit's table, which it is not.
_ALL_STAGES = ["pre-commit", "commit-msg"]

#: Three of the declared shapes, addressed by index so this module states none of them itself.
#: The assertion below keeps the indices honest: it fails if the declared tuple ever stops
#: supplying a shape the detector actually flags, rather than silently testing an empty string.
_LEAK, _SECOND_LEAK, _THIRD_LEAK = _PV_SHAPES_DECLARED_HERE[0:3]


def test_the_imported_fixtures_are_really_flagged_by_the_detector() -> None:
    """The premise every red case below rests on. Without it, a declared tuple that had drifted to
    non-PV strings would make each of them pass for the wrong reason.

    The LABEL is asserted, not merely truthiness: a shape that drifted into tripping the secret or
    site arm instead would satisfy a truthiness check while proving nothing about the device/PV
    detector these fixtures stand for. Pointed out by the review, which measured that this test is
    load-bearing for only one of the three shapes and weak for all three.
    """
    for shape in (_LEAK, _SECOND_LEAK, _THIRD_LEAK):
        reported = findings(f"fix: x\n\nSeen on {shape}.\n")
        assert reported == ["line 3: device/PV name (not a declared synthetic placeholder)"], (
            f"{shape!r} is no longer detected as a device name (got {reported}), so the cases "
            "below prove nothing"
        )


def test_a_clean_english_message_passes() -> None:
    """The counter-direction, first: a guard that refused everything would satisfy every red case
    below, and that failure mode is invisible to red cases alone."""
    message = (
        "fix(archiver): a cluster member answering 404 is not an empty history\n"
        "\n"
        "Measured against a two-member retrieval cluster: the second member holds the\n"
        "data and the first reports nothing. Placeholder used in the docs: SIM:PS-01:Cur-RB.\n"
    )

    assert findings(message) == []


def test_a_real_device_name_in_the_body_is_refused() -> None:
    """The measured leak class: commit BODIES carried real device names, and no file guard sees a
    commit message (GB-33)."""
    reported = findings(f"fix(pv): timeout\n\nSeen on {_LEAK} during the ramp.\n")

    # Membership, not a count. The pattern set is 5 built-ins on CI and larger on a machine that
    # has the git-ignored scripts/.pii_patterns.local, so an exact total would be a guard about
    # the environment rather than about the message.
    assert "line 3: device/PV name (not a declared synthetic placeholder)" in reported


def test_a_synthetic_placeholder_in_the_body_passes() -> None:
    """The other half of the same rule. The detector passes DECLARED synthetic markers, so the
    documented placeholders can be quoted in a message without an exemption."""
    assert findings("docs: name the example\n\nUse SIM:PS-01:Cur-RB and DEV-TEST01:Spu01.\n") == []


def test_a_secret_in_the_message_is_refused() -> None:
    """The built-in secret patterns reach the message too, not only tracked files.

    The fixture is ASSEMBLED rather than written out, and the reason is worth stating because it
    looks like evasion and is the opposite. ``scripts/check_no_ess_internal.py`` scans this very
    file at every commit, and its AWS arm is purely structural, so a literal invented key here
    would block every commit of the test that proves the guard works. That guard exempts exactly
    one file, its own source; widening the exemption to a test file would weaken the secret scan
    over the tree to make a test convenient. Splitting the shape leaves the file scan intact and
    still hands the guard the exact string a real message would carry. The value is invented: it
    is an AWS access-key-id SHAPE, never a credential.
    """
    invented_key = "AKIA" + "QRSTUVWXYZ012345"

    reported = findings(f"chore: rotate\n\nOld key {invented_key} revoked.\n")

    # Membership rather than a total, for the reason stated in the device-name case above.
    assert "line 3: secret: AWS access key" in reported


def test_the_matched_text_is_never_echoed() -> None:
    """Output discipline, inherited from the sibling file guard: a guard that prints what it found
    leaks it a second time, into the terminal and any CI log that captures it."""
    reported = findings(f"fix: something\n\nObserved on {_SECOND_LEAK}.\n")

    assert reported and all(_SECOND_LEAK not in line for line in reported)


def test_git_comment_lines_are_not_scanned() -> None:
    """Git strips these before storing the message, so scanning them would block a commit over text
    that never becomes part of any commit. It is not a nicety: the template git writes lists the
    staged paths, and a device-named file reaches that list without anyone typing it.

    ⚠️ **THIS TEST WAS A SHAM AND THE REVIEW PROVED IT.** Its fixture read
    ``#\\tmodified:   docs/<name>.md``, and the ``/`` in ``docs/`` blocks ``PV_RE``'s run-boundary
    lookbehind, so that line produced no finding WITH or WITHOUT the comment filter: deleting
    ``_COMMENT_LINE`` from the guard entirely left this green. The path prefix is gone for exactly
    that reason, and the assertion below now fails if the filter does.
    """
    message = (
        "docs: update the deployment page\n"
        "\n"
        "# Please enter the commit message for your changes.\n"
        "# Changes to be committed:\n"
        f"#\tmodified:   {_LEAK}.md\n"
    )

    assert findings(f"docs: x\n\n{_LEAK}.md\n"), (
        "the fixture below no longer trips the detector at all, so it would pass with the comment "
        "filter removed; that is how this guard was a sham before"
    )
    assert findings(message) == []


def test_an_indented_hash_is_scanned_because_git_keeps_it() -> None:
    """Git's comment rule is column zero, and reading it as "any leading whitespace" made this
    guard BLINDER than git rather than stricter.

    Measured with ``git stripspace --strip-comments`` on git 2.53: an indented ``   # note`` is
    KEPT and reaches the stored message. The earlier ``^\\s*#`` skipped it, so a device name after
    an indented hash was published unreported. Found by review, not by any test here.
    """
    reported = findings(f"fix: x\n\n   # looks like a comment, git keeps it: {_LEAK}\n")

    assert reported == ["line 3: device/PV name (not a declared synthetic placeholder)"]


def test_a_hand_typed_divider_does_not_switch_the_scan_off() -> None:
    """The scissors line is matched as git's exact literal, not as a pattern.

    A loose ``#\\s*-+\\s*>8\\s*-+`` also accepted ``#--->8---``, so one hand-typed divider anywhere
    in a body silently disabled the scan for everything below it, with no trace for the author.
    The exact literal was measured, not read off the source: a ``git commit -v`` whose editor dumps
    ``.git/COMMIT_EDITMSG`` writes it byte for byte, with and without ``--cleanup=scissors``.
    """
    assert findings(f"fix: x\n\n#--->8---\n{_LEAK}\n"), (
        "a loose divider switched the scan off; only git's own cut line may do that"
    )


def test_everything_below_the_scissors_line_is_ignored() -> None:
    """`git commit --verbose` puts the whole staged diff below the scissors, and git removes it.
    Scanning it would judge the diff a second time, on a surface where the file guards already ran,
    and would report their content as a message defect."""
    message = (
        "docs: quote a device name in a fixture\n"
        "\n"
        "# ------------------------ >8 ------------------------\n"
        f"+    pv = '{_THIRD_LEAK}'\n"
    )

    assert findings(message) == []


def test_reported_line_numbers_are_those_of_the_edited_file() -> None:
    """The number must locate the line in the editor, so it counts the file's lines rather than
    the filtered ones. Numbering the survivors would point one line off for every comment above."""
    message = f"fix: x\n\n# a comment git will strip\nSeen on {_LEAK}.\n"

    assert [number for number, _ in message_lines(message)] == [1, 2, 4]
    assert findings(message) == [
        "line 4: device/PV name (not a declared synthetic placeholder)",
    ]


def test_the_hook_is_wired_at_the_commit_msg_stage() -> None:
    """The wiring half, which a test CAN hold: the hook entry and the install-type list.

    Removing either leaves the script in the tree while nothing runs it, which is exactly the
    shape this repository keeps finding (a promise with no mechanism behind it). The installation
    half is out of reach on purpose, see the module docstring.
    """
    config = yaml.safe_load(_CONFIG.read_text(encoding="utf-8"))

    assert "commit-msg" in config.get("default_install_hook_types", []), (
        "default_install_hook_types no longer names commit-msg, so `pre-commit install` stops "
        "wiring the message guard and a new clone silently loses it"
    )

    hooks = [hook for repo in config["repos"] for hook in repo["hooks"]]
    staged = [hook for hook in hooks if "commit-msg" in hook.get("stages", [])]
    assert [hook["id"] for hook in staged] == ["commit-message"], (
        f"expected exactly one commit-msg hook, found {[hook['id'] for hook in staged]}"
    )
    assert staged[0]["entry"].endswith("scripts/check_commit_message.py")
    assert staged[0]["always_run"] is True, (
        "the file pre-commit hands a commit-msg hook is .git/COMMIT_EDITMSG, which has no "
        "extension; without always_run a types filter would skip it and the hook would pass "
        "without reading anything"
    )
    assert staged[0]["pass_filenames"] is True, (
        "without pass_filenames the script is handed no message path at all. That is a one-word "
        "change that leaves every test here green while the hook becomes permanently inert, so it "
        "is pinned; the script refuses an empty argument list for the same reason."
    )


def test_the_file_hooks_stay_off_the_commit_message() -> None:
    """The unintended widening this configuration caused once, caught by the first live attempt.

    A hook that names no stage runs at EVERY stage, so installing the commit-msg shim silently
    made the six FILE hooks run again over ``.git/COMMIT_EDITMSG``. Two of them do not belong
    there: ``typography`` and ``language`` police the tracked tree, and an em dash or a German
    word in a commit subject is a style question, not a leak. Blocking a commit over one would be
    a behaviour change nobody argued for, and it would arrive by omission rather than by decision,
    which is why the default is pinned rather than left implicit.

    ⚠️ **THE ORDER OF THE TWO ASSERTIONS IS DELIBERATE, and it was wrong first time round.** The
    effective-stage question is asked FIRST and computed the way pre-commit computes it, falling
    back to pre-commit's real default rather than to the pin below. With the pin read first and
    used as the fallback, this test could only ever fail on "the pin changed": the scenario in the
    paragraph above reddened it without ever naming ``typography`` or ``language``, so the offender
    list its failure message promises was unreachable. Asked in this order, deleting
    ``default_stages`` reddens it WITH the offenders named, which is the finding a reader needs.
    """
    config = yaml.safe_load(_CONFIG.read_text(encoding="utf-8"))
    hooks = [hook for repo in config["repos"] for hook in repo["hooks"]]

    every_stage = config.get("default_stages", _ALL_STAGES)
    on_the_message = [
        hook["id"] for hook in hooks if "commit-msg" in hook.get("stages", every_stage)
    ]
    assert on_the_message == ["commit-message"], (
        f"these hooks reach the commit message: {on_the_message}. Only the message guard should; "
        "the others were written for files and would judge text git strips or never stored."
    )

    # The pin itself, second: the check above would also pass on a stage list that merely happens
    # not to include commit-msg, and the reason this file names one at all is the omission case.
    assert config.get("default_stages") == ["pre-commit"], (
        "default_stages is no longer pinned to the pre-commit stage, so every hook that names no "
        "stage now also runs over the commit message"
    )
