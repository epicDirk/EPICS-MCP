"""Tests for the static e3 st.cmd / .db parser (synthetic fixtures, modelled on dln01)."""

from collections.abc import Iterator
from pathlib import Path

import pytest

import epics_pv_mcp.services.e3_db as e3_db
from epics_pv_mcp.services.e3_db import (
    StCmdInfo,
    _strip_line_comment,
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


def test_ioc_db_pvs_alias_word_boundary() -> None:
    # QA C6: an identifier ENDING in "alias" (setMyalias(...)) must NOT be parsed as an alias().
    db = 'record(bi, "$(P)rec")\nsetMyalias("$(P)ghost")\n'
    resolved, _unresolved = ioc_db_pvs(db, {"P": "SYS:"})
    assert resolved == {"SYS:rec"}
    assert "SYS:ghost" not in resolved


def test_ioc_db_pvs_inline_comment_stripped() -> None:
    # QA C4: a trailing inline #-comment carrying a templated alias()/record() must NOT pollute the
    # PV set (it would have made unresolved non-empty → withhold ALL broken verdicts for the IOC).
    db = 'record(bi, "$(P)real")\nrecord(ao, "$(P)x") # alias("$(P)$(R)ghost")\n'
    resolved, unresolved = ioc_db_pvs(db, {"P": "SYS:"})
    assert resolved == {"SYS:real", "SYS:x"}
    assert unresolved == set()


def test_ioc_db_pvs_keeps_hash_inside_quotes() -> None:
    # The inline-comment strip must NOT cut a # that lives inside a quoted name/value.
    resolved, _unresolved = ioc_db_pvs('record(stringin, "SYS:a#b") {}\n', {})
    assert resolved == {"SYS:a#b"}


def test_parse_prefix_ignores_dbloadtemplate_vote() -> None:
    # QA C3: dbLoadTemplate is captured for detection only and must not skew the IOC prefix.
    st = 'dbLoadTemplate("x.substitutions", "P=TPL:")\ndbLoadRecords("a.db", "P=REAL:")\n'
    assert parse_st_cmd(st).prefix == "REAL:"


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


def test_load_ioc_db_no_dbloadrecords_is_incomplete(tmp_path: Path) -> None:
    # QA C1: a st.cmd that enumerates ZERO concrete PVs must NOT report complete=True (else
    # crossplane would flag every linked PV as broken against the empty set).
    info = parse_st_cmd('epicsEnvSet("P", "SYS:")\n')
    result = load_ioc_db(info, tmp_path)
    assert result.resolved == frozenset()
    assert result.complete is False


def test_load_ioc_db_empty_db_is_incomplete(tmp_path: Path) -> None:
    # QA C1: a found-but-record-less .db enumerates nothing → not complete.
    info = parse_st_cmd('dbLoadRecords("empty.db", "P=SYS:")\n')
    _write(tmp_path / "empty.db", "# only a comment, no records\n")
    result = load_ioc_db(info, tmp_path)
    assert result.resolved == frozenset()
    assert result.complete is False


def test_load_ioc_db_unresolved_path_macro_not_basename_guessed(tmp_path: Path) -> None:
    # QA C2: an unresolved $(<module>_DIR) must NOT degrade to a basename search that loads a
    # same-named .db from the WRONG module → force missing, never load foreign PVs, complete=False.
    info = parse_st_cmd('require modx\ndbLoadRecords("$(othermod_DIR)/foo.db", "P=SYS:")\n')
    _write(tmp_path / "elsewhere" / "foo.db", 'record(bi, "$(P)wrong") {}\n')
    result = load_ioc_db(info, tmp_path)
    assert result.resolved == frozenset()  # the wrong-module foo.db was NOT loaded
    assert result.missing == ("$(othermod_DIR)/foo.db",)
    assert result.complete is False


def test_load_ioc_db_same_db_two_instances_merged(tmp_path: Path) -> None:
    # QA C7: two dbLoadRecords of the SAME .db with different macros must both be expanded (the
    # loader iterates st_info.loads, not the deduped db_files) → per-instance PV sets merge.
    info = parse_st_cmd('dbLoadRecords("foo.db", "P=A:")\ndbLoadRecords("foo.db", "P=B:")\n')
    _write(tmp_path / "foo.db", 'record(bi, "$(P)status") {}\n')
    result = load_ioc_db(info, tmp_path)
    assert result.resolved == frozenset({"A:status", "B:status"})


# --- Phase 4 L-Politur (S7-2 / S7-4 / S7-3) ---------------------------------------------------


def test_parse_st_cmd_unresolved_p_macro_does_not_vote_for_prefix() -> None:
    """S7-2: a P= that stays templated (points at a name not in epicsEnvSet) must not vote for the
    prefix; a concrete P= from a sibling load still wins."""
    info = parse_st_cmd('dbLoadRecords("a.db", "P=$(UNDEF):")\ndbLoadRecords("b.db", "P=REAL:")\n')
    assert info.prefix == "REAL:"


def test_parse_st_cmd_only_unresolved_p_yields_no_prefix() -> None:
    """S7-2: with only a templated P=, no concrete prefix is voted (None, not the raw macro)."""
    info = parse_st_cmd('dbLoadRecords("a.db", "P=$(UNDEF):")\n')
    assert info.prefix is None


def test_parse_st_cmd_empty_p_does_not_outvote_real_prefix() -> None:
    """S7-2 regression lock: an EMPTY ``P=`` carries no device info and must never win the
    majority vote over a real prefix. Two empty ``P=`` + one ``P=REAL:`` → ``REAL:`` (not ``""``).
    Against the pre-fix ``p_value is not None`` code the empties tallied ``{"": 2, "REAL:": 1}`` →
    ``""`` won and downstream (crossplane ``if prefix and …``) classified ZERO PVs as linked."""
    info = parse_st_cmd(
        'dbLoadRecords("a.db", "P=")\n'
        'dbLoadRecords("b.db", "P=")\n'
        'dbLoadRecords("c.db", "P=REAL:")\n'
    )
    assert info.prefix == "REAL:"


def test_parse_st_cmd_only_empty_p_yields_no_prefix() -> None:
    """S7-2: with only empty ``P=`` loads, no concrete prefix is voted (None, not ``""``)."""
    info = parse_st_cmd('dbLoadRecords("a.db", "P=")\ndbLoadRecords("b.db", "P=")\n')
    assert info.prefix is None


def test_strip_line_comment_ignores_backslash_escaped_quote() -> None:
    """S7-4: an escaped quote (``\\"``) inside a value must NOT flip the quote tracker, so a later
    ``#`` inside the same value is not mistaken for a comment and the line is not truncated."""
    line = 'field(DESC, "a \\" b # c")'  # value contains a literal quote then a '#'
    assert _strip_line_comment(line) == line  # not cut at the in-value '#'


def test_ioc_db_pvs_escaped_quote_does_not_hide_a_later_record() -> None:
    """S7-4 end-to-end: the escaped quote must not let an in-value '#' cut a record that follows on
    the same line."""
    db = 'record(stringin, "SYS:x") { field(DESC, "a \\" b # c") } record(stringin, "SYS:y") {}\n'
    resolved, _unresolved = ioc_db_pvs(db, {})
    assert resolved == {"SYS:x", "SYS:y"}


def test_strip_line_comment_escaped_backslash_before_quote_closes_the_string() -> None:
    """S7-4 robustness: a value ending in a LITERAL backslash (``\\\\`` in the .db text) closes the
    string at the following quote, so a trailing ``#`` IS a real comment and gets stripped. The old
    single-char lookback mis-read that closing quote as escaped (prev char is ``\\``) and left the
    comment in; counting the backslash-run parity fixes it."""
    line = r'record(bo, "c:\\") # record(bo, "GHOST")'  # value c:\  → quote closes → '#' comments
    assert _strip_line_comment(line) == r'record(bo, "c:\\") '


def test_ioc_db_pvs_escaped_backslash_strips_ghost_comment_record() -> None:
    """S7-4 end-to-end: with a literal-backslash value the quote closes, so a ``# record(...)``
    after it is a comment and its ghost record is NOT harvested."""
    db = 'record(bi, "SYS:real") { field(DESC, "c:\\\\") } # record(bi, "SYS:ghost")\n'
    resolved, _unresolved = ioc_db_pvs(db, {})
    assert resolved == {"SYS:real"}
    assert "SYS:ghost" not in resolved


def test_load_ioc_db_walks_module_root_once_for_many_basename_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S7-3: N basename-fallback loads walk the module root ONCE (index built per load_ioc_db),
    not once per load."""
    loads = "".join(f'dbLoadRecords("f{i}.db", "P=SYS:")\n' for i in range(5))
    info = parse_st_cmd(loads)
    # Files live in a subdir → each load misses the direct path and uses the basename index.
    for i in range(5):
        _write(tmp_path / "sub" / f"f{i}.db", 'record(bi, "$(P)x") {}\n')

    calls = 0
    real = e3_db._iter_files_bounded

    def counting(root: Path, *, max_depth: int = 8) -> Iterator[Path]:
        nonlocal calls
        calls += 1
        return real(root, max_depth=max_depth)

    monkeypatch.setattr(e3_db, "_iter_files_bounded", counting)
    result = load_ioc_db(info, tmp_path)
    assert calls == 1  # ONE walk for all 5 loads (was 5 before S7-3)
    assert result.resolved == frozenset({"SYS:x"})


def test_load_ioc_db_direct_resolves_never_walk_module_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S7-3 lazy: when every load resolves via the DIRECT path, the basename index is never built —
    ZERO filesystem walks (eager build walked once even on this happy path). The index is lazy now,
    built only on the first basename fallback."""
    loads = "".join(f'dbLoadRecords("f{i}.db", "P=SYS:")\n' for i in range(5))
    info = parse_st_cmd(loads)
    # Files at the root itself → each load hits the direct-path branch, no basename fallback.
    for i in range(5):
        _write(tmp_path / f"f{i}.db", 'record(bi, "$(P)x") {}\n')

    calls = 0
    real = e3_db._iter_files_bounded

    def counting(root: Path, *, max_depth: int = 8) -> Iterator[Path]:
        nonlocal calls
        calls += 1
        return real(root, max_depth=max_depth)

    monkeypatch.setattr(e3_db, "_iter_files_bounded", counting)
    result = load_ioc_db(info, tmp_path)
    assert calls == 0  # never walked — all loads resolved directly (lazy index)
    assert result.resolved == frozenset({"SYS:x"})
