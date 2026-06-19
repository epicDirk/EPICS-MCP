"""Tests for the self-contained .bob PV extractor."""

from pathlib import Path

from epics_pv_mcp.services.bob_pvs import extract_pvs, extract_pvs_from_dir

_BOB = """<display version="2.0.0">
  <widget type="textupdate"><pv_name>SYSX:concrete</pv_name></widget>
  <widget type="textentry"><pv_name>$(P)templated</pv_name></widget>
  <widget type="led"><pv_name>${pv_name}</pv_name></widget>
  <widget type="x"><pv_name>$(pv_name)</pv_name></widget>
  <widget type="x"><pv_name>=formula(1)</pv_name></widget>
  <widget type="xyplot"><x_pv>X:axis</x_pv><trace_0_y_pv>TR:y</trace_0_y_pv></widget>
  <widget type="x"><pv name="ATTR:pv"></pv></widget>
</display>
"""


def _write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def _mini(pv: str) -> str:
    return f'<display><widget type="x"><pv_name>{pv}</pv_name></widget></display>'


def test_extract_pvs_concrete_macro_trace_and_attr(tmp_path: Path) -> None:
    pvs = extract_pvs(_write(tmp_path / "d.bob", _BOB))
    assert "SYSX:concrete" in pvs  # concrete widget PV
    assert "$(P)templated" in pvs  # mixed macro kept
    assert "X:axis" in pvs  # x_pv
    assert "TR:y" in pvs  # trace_N_y_pv
    assert "ATTR:pv" in pvs  # name= attribute fallback
    assert "${pv_name}" not in pvs  # pure ${} skipped (the fix)
    assert "$(pv_name)" not in pvs  # pure $() skipped
    assert "=formula(1)" not in pvs  # formula skipped
    assert pvs == sorted(pvs)  # deterministic


def test_extract_pvs_malformed_returns_empty(tmp_path: Path) -> None:
    assert extract_pvs(_write(tmp_path / "bad.bob", "<not valid <<")) == []


def test_extract_pvs_missing_file_returns_empty(tmp_path: Path) -> None:
    assert extract_pvs(tmp_path / "nope.bob") == []


def test_extract_pvs_from_dir_recursive_posix_keys(tmp_path: Path) -> None:
    _write(tmp_path / "a.bob", _mini("A:1"))
    sub = tmp_path / "sub"
    sub.mkdir()
    _write(sub / "b.bob", _mini("B:1"))
    result = extract_pvs_from_dir(tmp_path)
    assert result["a.bob"] == ["A:1"]
    assert result["sub/b.bob"] == ["B:1"]  # POSIX-relative key
