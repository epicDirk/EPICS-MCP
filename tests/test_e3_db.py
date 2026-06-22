"""Tests for the static e3 st.cmd / .db parser (synthetic fixtures, modelled on dln01)."""

from pathlib import Path

from epics_pv_mcp.services.e3_db import (
    StCmdInfo,
    ioc_db_pvs,
    load_ioc_db,
    parse_st_cmd,
    substitute,
)

# Modelled on iocs/factory/e3-ioc-evr-fbis-dln01-ctrl-01/st.cmd (read-only spike).
ST_CMD = """require essioc
require mrfioc2ess
epicsEnvSet("ASGPROTECTED", "")

iocshLoad "$(mrfioc2ess_DIR)/evrEss.iocsh"  "P=DEV-TEST01:Ctrl-EVR-01:"
dbLoadRecords("mrfioc2-compatible.db", "P=DEV-TEST01:Ctrl-EVR-01:")
dbLoadRecords "initialValueWave.db"  "P=DEV-TEST01:Ctrl-EVR-01:, S=Label-I"
iocshLoad("$(essioc_DIR)/common_config.iocsh")
"""


def test_parse_requires() -> None:
    info = parse_st_cmd(ST_CMD)
    assert info.requires == ["essioc", "mrfioc2ess"]


def test_parse_prefix_and_device_name() -> None:
    info = parse_st_cmd(ST_CMD)
    assert info.prefix == "DEV-TEST01:Ctrl-EVR-01:"
    assert info.device_name == "DEV-TEST01:Ctrl-EVR-01"


def test_db_files_only_db_loads() -> None:
    info = parse_st_cmd(ST_CMD)
    assert info.db_files == ["initialValueWave.db", "mrfioc2-compatible.db"]


def test_env_captured() -> None:
    info = parse_st_cmd(ST_CMD)
    assert info.env["ASGPROTECTED"] == ""


def test_substitute_basic_undefined_and_nested() -> None:
    assert substitute("$(P)Foo", {"P": "X:"}) == "X:Foo"
    assert substitute("$(UNDEF):x", {}) == "$(UNDEF):x"  # undefined stays literal
    assert substitute("${A}", {"A": "$(B)", "B": "z"}) == "z"  # nested resolves


def test_ioc_db_pvs_resolved_and_needs_msi() -> None:
    db = (
        'record(bi, "$(P)status") {}\n'
        'record(ao, "$(P)$(R)setpoint") {}\n'
        'record(calc, "LIT:fixed") {}\n'
    )
    resolved, unresolved = ioc_db_pvs(db, {"P": "DEV-TEST01:"})
    assert resolved == {"DEV-TEST01:status", "LIT:fixed"}
    assert unresolved == {"DEV-TEST01:$(R)setpoint"}  # R undefined → needs-msi (exact)


def test_parse_st_cmd_no_prefix() -> None:
    info = parse_st_cmd('dbLoadRecords("x.db")\n')
    assert info.prefix is None
    assert info.device_name is None


def test_parse_prefix_tie_breaks_lexicographically() -> None:
    # Two distinct P values, equal counts → lexicographically smallest wins (deterministic).
    st = 'dbLoadRecords("a.db", "P=Z:")\ndbLoadRecords("b.db", "P=A:")\n'
    assert parse_st_cmd(st).prefix == "A:"


def test_substitute_cyclic_terminates() -> None:
    # A -> B -> A: must terminate (bounded) and leave a macro literal, never loop.
    result = substitute("$(A)", {"A": "$(B)", "B": "$(A)"})
    assert "$(" in result


def test_commented_st_cmd_lines_ignored() -> None:
    # A commented-out dbLoadRecords must NOT inject a ghost prefix / db file.
    st = '# dbLoadRecords("ghost.db", "P=GHOST:")\ndbLoadRecords("real.db", "P=REAL:")\n'
    info = parse_st_cmd(st)
    assert info.prefix == "REAL:"
    assert info.db_files == ["real.db"]


def test_commented_db_records_ignored() -> None:
    db = '# record(bi, "GHOST:x")\nrecord(ao, "$(P)real")\n'
    resolved, _unresolved = ioc_db_pvs(db, {"P": "SYS:"})
    assert "GHOST:x" not in resolved
    assert "SYS:real" in resolved


def test_device_name_strips_single_trailing_colon() -> None:
    assert StCmdInfo(prefix="X:Y:").device_name == "X:Y"
    assert StCmdInfo(prefix="SYS::").device_name == "SYS:"  # only ONE colon stripped
    assert StCmdInfo(prefix=None).device_name is None


def test_ioc_db_pvs_captures_aliases() -> None:
    # A display PV may reference an ALIAS, not the record name — both must count as served.
    db = 'record(bi, "$(P)rec") { alias("$(P)recAlias") }\nalias("$(P)rec", "$(P)other")\n'
    resolved, unresolved = ioc_db_pvs(db, {"P": "SYS:"})
    assert resolved == {"SYS:rec", "SYS:recAlias", "SYS:other"}
    assert unresolved == set()


# --- load_ioc_db (opt-in IOC .db enumeration) -------------------------------------------------


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_load_ioc_db_resolves_module_dir_and_load_macro(tmp_path: Path) -> None:
    # The two QA-critical fixes together: $(<module>_DIR) resolves under the root, and P comes from
    # the per-load macro (NOT st_info.env) → $(P)status becomes a concrete resolved PV.
    st = 'require modx\ndbLoadRecords("$(modx_DIR)/db/foo.db", "P=SYS:")\n'
    info = parse_st_cmd(st)
    _write(tmp_path / "modx" / "db" / "foo.db", 'record(bi, "$(P)status") {}\n')
    result = load_ioc_db(info, tmp_path)
    assert result.resolved == frozenset({"SYS:status"})
    assert result.unresolved == frozenset()
    assert result.missing == ()
    assert result.ambiguous == ()
    assert result.unsupported_load is False
    assert result.complete is True


def test_load_ioc_db_missing_file_is_incomplete(tmp_path: Path) -> None:
    info = parse_st_cmd('dbLoadRecords("nope.db", "P=SYS:")\n')
    result = load_ioc_db(info, tmp_path)
    assert result.missing == ("nope.db",)
    assert result.complete is False


def test_load_ioc_db_ambiguous_basename_not_loaded(tmp_path: Path) -> None:
    # Same basename in two modules → must NOT guess a PV set (wrong-module risk) → ambiguous.
    info = parse_st_cmd('dbLoadRecords("shared.db", "P=SYS:")\n')
    _write(tmp_path / "a" / "shared.db", 'record(bi, "$(P)a") {}\n')
    _write(tmp_path / "b" / "shared.db", 'record(bi, "$(P)b") {}\n')
    result = load_ioc_db(info, tmp_path)
    assert result.ambiguous == ("shared.db",)
    assert result.resolved == frozenset()
    assert result.complete is False


def test_load_ioc_db_iocsh_load_forces_incomplete(tmp_path: Path) -> None:
    # iocshLoad loads records we cannot statically follow → completeness cannot be claimed even
    # though the dbLoadRecords .db itself resolves (the dln01-EVR reality).
    st = 'iocshLoad("$(modx_DIR)/evrEss.iocsh", "P=SYS:")\ndbLoadRecords("foo.db", "P=SYS:")\n'
    info = parse_st_cmd(st)
    _write(tmp_path / "foo.db", 'record(bi, "$(P)status") {}\n')
    result = load_ioc_db(info, tmp_path)
    assert result.resolved == frozenset({"SYS:status"})
    assert result.unsupported_load is True
    assert result.complete is False


def test_load_ioc_db_dbloadtemplate_forces_incomplete(tmp_path: Path) -> None:
    st = 'dbLoadTemplate("x.substitutions")\ndbLoadRecords("foo.db", "P=SYS:")\n'
    info = parse_st_cmd(st)
    _write(tmp_path / "foo.db", 'record(bi, "$(P)status") {}\n')
    result = load_ioc_db(info, tmp_path)
    assert result.unsupported_load is True
    assert result.complete is False


def test_load_ioc_db_needs_msi_residue_is_incomplete(tmp_path: Path) -> None:
    # A record still macro-templated after substitution (R undefined) → unresolved → not complete.
    info = parse_st_cmd('dbLoadRecords("foo.db", "P=SYS:")\n')
    _write(tmp_path / "foo.db", 'record(ao, "$(P)$(R)sp") {}\n')
    result = load_ioc_db(info, tmp_path)
    assert result.unresolved == frozenset({"SYS:$(R)sp"})
    assert result.complete is False


def test_load_ioc_db_decode_error_is_graceful_missing(tmp_path: Path) -> None:
    info = parse_st_cmd('dbLoadRecords("bad.db", "P=SYS:")\n')
    (tmp_path / "bad.db").write_bytes(b"\xff\xfe\x00bad bytes")
    result = load_ioc_db(info, tmp_path)
    assert result.missing == ("bad.db",)
    assert result.complete is False
