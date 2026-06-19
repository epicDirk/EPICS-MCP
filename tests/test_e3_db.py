"""Tests for the static e3 st.cmd / .db parser (synthetic fixtures, modelled on dln01)."""

from epics_pv_mcp.services.e3_db import ioc_db_pvs, parse_st_cmd, substitute

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
    assert "DEV-TEST01:status" in resolved
    assert "LIT:fixed" in resolved
    assert any("$(R)" in name for name in unresolved)  # R undefined → needs-msi
