"""Tests for the workspace path boundary helper (G3, resolve_user_path)."""

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

import epics_pv_mcp.config as config_module
from epics_pv_mcp.errors import EpicsError
from epics_pv_mcp.paths import resolve_user_path


@pytest.fixture(autouse=True)
def _reset_config() -> Iterator[None]:
    # resolve_user_path reads get_config() (a cached singleton). Reset it so each
    # test sees the EPICS_MCP_ALLOWED_ROOTS it sets (and never leaks to siblings).
    config_module._config = None
    yield
    config_module._config = None


def test_resolve_dir_ok(tmp_path: Path) -> None:
    assert resolve_user_path(str(tmp_path), kind="dir", label="d") == tmp_path.resolve()


def test_resolve_file_ok(tmp_path: Path) -> None:
    f = tmp_path / "x.bob"
    f.write_text("<display/>", encoding="utf-8")
    assert resolve_user_path(str(f), kind="file", label="f") == f.resolve()


def test_nonexistent_raises_invalid_input(tmp_path: Path) -> None:
    with pytest.raises(EpicsError) as exc:
        resolve_user_path(str(tmp_path / "nope"), kind="dir", label="d")
    assert exc.value.error_code == "INVALID_INPUT"


def test_wrong_kind_labels_the_field(tmp_path: Path) -> None:
    # A directory queried as a file → INVALID_INPUT, and the message names the arg.
    with pytest.raises(EpicsError) as exc:
        resolve_user_path(str(tmp_path), kind="file", label="myfield")
    assert exc.value.error_code == "INVALID_INPUT"
    assert "myfield" in str(exc.value)


def test_traversal_is_collapsed(tmp_path: Path) -> None:
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    weird = str(tmp_path / "a" / "b" / ".." / "b")  # ..-laden, resolves back to b
    assert resolve_user_path(weird, kind="dir", label="d") == sub.resolve()


def test_empty_allowed_roots_is_no_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Default/empty must mean NO boundary — guards the ""→[""]→CWD trap that would
    # otherwise silently restrict every path to the current working directory.
    monkeypatch.setenv("EPICS_MCP_ALLOWED_ROOTS", "")
    assert resolve_user_path(str(tmp_path), kind="dir", label="d") == tmp_path.resolve()


def test_inside_allowed_root_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "root"
    sub = root / "sub"
    sub.mkdir(parents=True)
    monkeypatch.setenv("EPICS_MCP_ALLOWED_ROOTS", str(root))
    assert resolve_user_path(str(sub), kind="dir", label="d") == sub.resolve()


def test_outside_allowed_root_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "root"
    other = tmp_path / "other"
    root.mkdir()
    other.mkdir()
    monkeypatch.setenv("EPICS_MCP_ALLOWED_ROOTS", str(root))
    with pytest.raises(EpicsError) as exc:
        resolve_user_path(str(other), kind="dir", label="d")
    assert exc.value.error_code == "PATH_OUTSIDE_WORKSPACE"


def test_multiple_allowed_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    monkeypatch.setenv("EPICS_MCP_ALLOWED_ROOTS", os.pathsep.join([str(a), str(b)]))
    # Both listed roots are honored.
    assert resolve_user_path(str(a), kind="dir", label="d") == a.resolve()
    assert resolve_user_path(str(b), kind="dir", label="d") == b.resolve()


@pytest.mark.skipif(sys.platform != "win32", reason="case-insensitive paths are Windows-only")
def test_mixed_case_root_matches_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sub = tmp_path / "Sub"
    sub.mkdir()
    # is_relative_to folds case on Windows → an upper-cased root still matches.
    monkeypatch.setenv("EPICS_MCP_ALLOWED_ROOTS", str(tmp_path).upper())
    assert resolve_user_path(str(sub), kind="dir", label="d") == sub.resolve()
