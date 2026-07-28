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

from epics_pv_mcp.cli_common import require_display_engine

_REPO = Path(__file__).resolve().parents[1]
_COMMANDS = (
    ("epics_pv_mcp.cli_coverage", "epics-coverage"),
    ("epics_pv_mcp.cli_crossplane", "epics-crossplane"),
)

#: All four console entry points, for properties every CLI must hold regardless of the engine.
_ALL_CLI_MODULES = (
    "epics_pv_mcp.cli_doctor",
    "epics_pv_mcp.cli_diagnose",
    "epics_pv_mcp.cli_coverage",
    "epics_pv_mcp.cli_crossplane",
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
    """
    if importlib.util.find_spec("opi_navigation") is None:
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
