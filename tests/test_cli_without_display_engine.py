"""The two display-aware CLIs must refuse politely where the engine is absent, not traceback.

Found by installing the built wheel into a clean environment and running all five console scripts:
three worked, and ``epics-crossplane`` and ``epics-coverage`` died on their module-level import
chain with a bare ``ModuleNotFoundError``. That is the first thing an outside user sees after
``pip install``, and it reads as a broken package rather than as a missing optional capability.

``server.py`` already had the right posture for the MCP surface: register the display tools only
when the engine imports, keep the core server up. These tests pin the same decision for the CLI
surface, which had never been given it.

The engine is normally PRESENT in this checkout, so every test here fakes its absence rather than
relying on the environment. That is the only way this can run in CI, where the engine is absent,
AND locally, where it is not.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from epics_mcp.cli_common import (
    _ENGINE_ENTRY_POINT,
    _report_engine_absent,
    require_display_engine,
)
from tests.engine_gate import engine_available

_REPO = Path(__file__).resolve().parents[1]
_COMMANDS = (
    ("epics_mcp.cli_coverage", "epics-coverage"),
    ("epics_mcp.cli_crossplane", "epics-crossplane"),
)

#: All four console entry points, for properties every CLI must hold regardless of the engine.
_ALL_CLI_MODULES = (
    "epics_mcp.cli_doctor",
    "epics_mcp.cli_diagnose",
    "epics_mcp.cli_coverage",
    "epics_mcp.cli_crossplane",
)


@pytest.fixture
def engine_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``importlib.util.find_spec('opi_navigation')`` answer None, as a clean install does."""
    real_find_spec = importlib.util.find_spec

    def _find_spec(name: str, package: str | None = None) -> object | None:
        if name == "opi_navigation" or name.startswith("opi_navigation."):
            return None
        return real_find_spec(name, package)

    monkeypatch.setattr(importlib.util, "find_spec", _find_spec)


def test_the_engine_is_reported_present_in_this_checkout() -> None:
    """A control for the fixture below: without it, a broken fake would look like a passing test.

    Skips rather than fails where the engine really is absent (CI), because that is a legitimate
    environment, not a defect.

    The question goes through ``tests.engine_gate`` (QA-48), which reaches ``find_spec`` as an
    attribute of ``importlib.util`` for the reason that matters here: the ``engine_absent`` fixture
    below fakes an engine-less environment by monkeypatching exactly that attribute, and this
    control has to see the same world the fixture builds.
    """
    if not engine_available():
        pytest.skip("opi_navigation absent in this environment, nothing to control against")

    assert require_display_engine("epics-coverage") is None


def test_a_missing_engine_is_a_usage_error_not_a_crash(
    engine_absent: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit code 2, the usage-error code: nothing went wrong, the command cannot apply here."""
    code = require_display_engine("epics-coverage")

    assert code == 2
    message = capsys.readouterr().err
    assert message.startswith("epics-coverage: needs the opi_navigation display engine")


def test_the_message_says_what_still_works_and_how_to_get_the_rest(
    engine_absent: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """A refusal that only says "no" sends the reader to the issue tracker. This one has to name
    the three commands that DO work and the one command that installs the engine, because the
    reader's next question is always one of those two."""
    require_display_engine("epics-crossplane")
    message = capsys.readouterr().err

    for expected in ("epics-doctor", "epics-diagnose", "epics-mcp", "--group displays"):
        assert expected in message, f"{expected!r} missing from the refusal"


@pytest.mark.parametrize(("module_name", "command"), _COMMANDS)
def test_main_refuses_cleanly_with_the_engine_faked_away(
    module_name: str, command: str, engine_absent: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """End to end through the entry point, so the check is proven to sit BEFORE argument parsing.

    Deliberately called with no arguments at all: both commands have a required ``--displays``, so
    a check placed after parsing would exit 2 for the wrong reason and this test would pass on a
    broken implementation. The stderr assertion is what tells the two apart.
    """
    module = importlib.import_module(module_name)

    code = module.main([])

    assert code == 2
    assert command in capsys.readouterr().err


@pytest.mark.parametrize(("module_name", "command"), _COMMANDS)
def test_version_is_engine_gated_on_the_display_clis(
    module_name: str, command: str, engine_absent: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """QA-46 met QA-42 here, and this pins the seam between them.

    ``--version`` was added to all five console commands, but on these two the availability check
    runs BEFORE the parser is built, and the parser cannot be built without the engine anyway: its
    help text and a default read ``DEFAULT_PV_CONTEXT_CAP`` from ``services/inventory_adapter``,
    the sole importer of the engine. So on a core-only install (which is what CI is) the flag is
    unreachable here, and ``tests/test_cli_version.py`` asserts exactly this outcome for that
    environment.

    Faked rather than environment-dependent, so the limit is provable in a checkout that HAS the
    engine too. ⚠️ Should QA-42 ever move argument handling ahead of the check, this test goes red
    on purpose: that is the signal to update it and the note in ``test_cli_version.py`` together,
    instead of leaving a stale claim about what a published install can do.
    """
    module = importlib.import_module(module_name)

    code = module.main(["--version"])

    assert code == 2, f"{command} --version should refuse with a usage error, not answer"
    assert command in capsys.readouterr().err


@pytest.mark.parametrize(("module_name", "command"), _COMMANDS)
def test_the_module_imports_at_all_without_the_engine(module_name: str, command: str) -> None:
    """The actual regression, and the one a fake cannot show: the module-level import chain.

    Faking ``find_spec`` inside this process proves nothing about it, because the module is already
    imported. So this runs a SUBPROCESS whose import system cannot produce ``opi_navigation`` at
    all, which is what a package-index install looks like. Red before the fix: ModuleNotFoundError
    on import, long before any of this module's other tests get a say.

    The blocker RAISES rather than answering None, which is stricter than a plain missing package
    and deliberately so: ``find_spec`` propagates whatever a meta-path finder raises, so this also
    covers a restricted or broken import system. It found a real weakness on first run, an
    availability probe that crashed instead of answering "unavailable".
    """
    blocker = (
        "import sys\n"
        "class _Blocker:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] == 'opi_navigation':\n"
        '            raise ModuleNotFoundError(f"No module named {name!r}")\n'
        "        return None\n"
        "sys.meta_path.insert(0, _Blocker())\n"
        f"import {module_name} as m\n"
        "sys.exit(m.main([]))\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", blocker],
        capture_output=True,
        text=True,
        cwd=_REPO,
        timeout=120,
        check=False,
    )

    assert "ModuleNotFoundError" not in result.stderr, (
        f"{command} still dies on its import chain without the engine:\n{result.stderr[-800:]}"
    )
    assert result.returncode == 2, result.stderr[-800:]
    assert command in result.stderr


@pytest.fixture
def engine_present_but_broken(monkeypatch: pytest.MonkeyPatch) -> None:
    """A half-installed engine: ``find_spec`` finds it, the import then fails.

    BOTH halves are faked, deliberately. Faking only the import made these tests pass here (engine
    present) and fail in CI (engine absent, so the absent branch answered first and the import
    probe was never reached), which is the environment dependence this module's header warns about,
    reintroduced. The everyday cause of this state is a missing transitive dependency.

    The import half is keyed on ``_ENGINE_ENTRY_POINT`` rather than on a literal module name, so
    moving the probe deeper into the engine cannot silently stop this fixture from biting.
    """
    real_find_spec = importlib.util.find_spec
    real_import_module = importlib.import_module

    def _find_spec(name: str, package: str | None = None) -> object | None:
        if name == "opi_navigation":
            return object()  # only tested for "is not None"
        return real_find_spec(name, package)

    def _import_module(name: str, package: str | None = None) -> object:
        if name == _ENGINE_ENTRY_POINT:
            raise ImportError("No module named 'numpy'")
        return real_import_module(name, package)

    monkeypatch.setattr(importlib.util, "find_spec", _find_spec)
    monkeypatch.setattr(importlib, "import_module", _import_module)


@pytest.fixture
def engine_top_package_fine_entry_point_broken(monkeypatch: pytest.MonkeyPatch) -> None:
    """The state a top-package probe cannot see: the package imports, its entry point does not.

    This is not a hypothetical. Measured on the real engine: ``opi_navigation/__init__.py`` is a
    docstring with no imports in it, so ``import opi_navigation`` executes nothing that a missing
    transitive dependency could break. An engine in exactly this state answered "usable" to the
    old probe, and the caller then met the bare ModuleNotFoundError.

    BOTH halves faked, for the reason the fixture above states: the engine is present in this
    checkout and absent in CI, and a test that asks the environment passes in one and fails in the
    other. Here the top-package import is faked to SUCCEED, which is the whole point.
    """
    real_find_spec = importlib.util.find_spec
    real_import_module = importlib.import_module

    def _find_spec(name: str, package: str | None = None) -> object | None:
        if name == "opi_navigation":
            return object()  # only tested for "is not None"
        return real_find_spec(name, package)

    def _import_module(name: str, package: str | None = None) -> object:
        if name == "opi_navigation":
            return object()  # the docstring-only top package: nothing to fail
        if name == _ENGINE_ENTRY_POINT:
            raise ModuleNotFoundError("No module named 'pydantic'")
        return real_import_module(name, package)

    monkeypatch.setattr(importlib.util, "find_spec", _find_spec)
    monkeypatch.setattr(importlib, "import_module", _import_module)


def test_a_broken_entry_point_behind_a_healthy_top_package_is_reported(
    engine_top_package_fine_entry_point_broken: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """Probing the TOP package proves a directory exists, not that the engine works (QA-14b).

    The top package of this engine is a docstring, so it cannot fail, so a probe that stopped there
    could only ever answer "usable". Falsified by shadowing the installed engine with a
    docstring-only package whose submodule imported a missing module: the helper returned None and
    the caller raised ModuleNotFoundError, which is the outcome the helper exists to prevent.

    Red on the pre-fix probe, which imported ``opi_navigation`` alone: it returned None and wrote
    nothing, so both assertions below fail.
    """
    code = require_display_engine("epics-coverage")
    message = capsys.readouterr().err

    assert code == 1, "an engine whose entry point will not import is broken, not usable"
    assert "No module named 'pydantic'" in message  # the real cause, named


def test_the_probe_targets_the_module_the_adapter_actually_imports() -> None:
    """The entry point is only meaningful while it is the module the engine is really used through.

    ``services/inventory_adapter.py`` states that it is the sole importer of the engine, and the
    probe's affordability argument rests on that being true: one name, not a list of every
    submodule some CLI happens to need. If the adapter moves to a different module, this goes red
    instead of the probe quietly guarding the wrong door.
    """
    adapter = (_REPO / "src" / "epics_mcp" / "services" / "inventory_adapter.py").read_text(
        encoding="utf-8"
    )

    assert f"from {_ENGINE_ENTRY_POINT} import" in adapter, (
        f"the adapter no longer imports {_ENGINE_ENTRY_POINT}, so the availability probe is "
        "guarding a module the engine is not reached through any more"
    )


def test_an_installed_but_broken_engine_is_not_reported_as_missing(
    engine_present_but_broken: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """An engine that is PRESENT and does not import is a failure, not a missing capability (QA-14).

    The probe used to be ``find_spec`` alone. A half-installed engine passed that check, and the
    command then died in its own import line with a bare traceback: exactly the outcome this helper
    exists to prevent, one step further along. ``server.py`` had the right posture for the MCP
    surface already, logging the failure loud and keeping the core up; the CLI surface had never
    been given it.

    Red on the pre-fix code twice over: it returned ``None`` (the caller proceeded straight into the
    traceback), so both the exit code and the message assertion fail.
    """
    code = require_display_engine("epics-coverage")
    message = capsys.readouterr().err

    assert code == 1, "an installed engine that will not load is a command failure, not usage"
    assert "installed but failed to load" in message
    assert "No module named 'numpy'" in message  # the actual cause, not a guess about it
    assert "which is not installed" not in message  # NOT the absent message


def test_the_absent_and_broken_messages_do_not_share_their_advice(
    engine_present_but_broken: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """The two refusals must give DIFFERENT advice, which is the whole point of telling them apart.

    Without this, the fix could satisfy the test above by printing a second message that still ends
    in "install the engine", advice that cannot help a reader whose engine is already installed.
    The absent case keeps the install instruction and the list of commands that still work; the
    broken case must say that installing will not help.

    The absent message is taken from ``_report_engine_absent`` directly rather than by faking the
    environment a second way inside one test: both reporters are pure writers to stderr, so calling
    one is the same evidence with none of the fixture juggling.
    """
    require_display_engine("epics-coverage")
    # Whitespace-normalized: these messages are hand-wrapped for a terminal, so a phrase that
    # happens to straddle a line break is not a missing phrase.
    broken = " ".join(capsys.readouterr().err.split())

    _report_engine_absent("epics-coverage")
    absent = " ".join(capsys.readouterr().err.split())

    assert "--group displays" in absent
    assert "epics-doctor" in absent  # what still works
    assert "installing the engine will not help" in broken
    assert "which is not installed" not in broken


@pytest.mark.parametrize("module_name", _ALL_CLI_MODULES)
def test_help_survives_a_legacy_encoded_console(module_name: str) -> None:
    """``--help`` must never die with a bare traceback on a cp1252 console (QA-8).

    Measured on Windows before the fix: ``epics-coverage --help`` and ``epics-crossplane --help``
    raised ``UnicodeEncodeError`` on the U+2194 arrow in their parser description whenever stdout
    was not UTF-8 (any redirected or legacy-encoded console), because ``configure_stdout()`` ran
    AFTER ``parse_args`` and argparse prints the help inside it. The doctor and diagnose help
    texts are ASCII today, so they pass either way; they are parametrized in anyway so a future
    non-ASCII character in their help cannot re-open the hole.

    ``PYTHONIOENCODING=cp1252:strict`` forces the legacy stream on every platform, so this runs
    red-provably on Linux CI too, not only on a Windows console. Exit 0 is the help path; exit 2
    is the engine refusal that legitimately precedes parsing on a core-only install. Both are
    fine, a traceback is not.
    """
    env = {**os.environ, "PYTHONIOENCODING": "cp1252:strict"}

    result = subprocess.run(
        [sys.executable, "-m", module_name, "--help"],
        capture_output=True,
        text=True,
        cwd=_REPO,
        timeout=120,
        env=env,
        check=False,
    )

    assert "UnicodeEncodeError" not in result.stderr, result.stderr[-800:]
    assert "Traceback" not in result.stderr, result.stderr[-800:]
    assert result.returncode in (0, 2), result.stderr[-800:]
