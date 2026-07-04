"""Static, Windows-safe parsing of ESS e3 IOC startup scripts and EPICS databases.

Pure-Python, read-only — no running IOC, no EPICS base, no SWIG. Two jobs:

1. :func:`parse_st_cmd` — read an e3 ``st.cmd`` (the IOC's startup script) into a
   :class:`StCmdInfo`: the ``require``d modules, ``epicsEnvSet`` variables, the
   ``dbLoadRecords``/``iocshLoad`` calls with their macro strings, and the dominant
   device prefix (the ``P=`` macro, e.g. ``DEV-TEST01:Ctrl-EVR-01:``).
2. :func:`ioc_db_pvs` — regex-extract record (PV) names from an EPICS ``.db`` text and
   substitute simple ``$(MACRO)`` references.

**Known limitation (documented, not a bug):** full ``.substitutions``/template
multi-instance expansion needs the EPICS ``msi`` tool (C++ / Linux/Docker) and is NOT
done here. Records whose names still contain ``$(...)`` after substitution are returned
as *unresolved* ("needs-msi") and must never be reported as "broken". The real ``.db``
of an e3 module also live in the module package (conda), not in the IOC repo — so an IOC
repo's ``st.cmd`` gives the prefix/macros/modules, while full PV enumeration needs the
module repos (deferred).
"""

from __future__ import annotations

import os
import re
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

# require <module>  (optional quotes, optional version after a comma)
_REQUIRE_RE = re.compile(r'^\s*require\s+["\']?([A-Za-z0-9_\-]+)', re.MULTILINE)

# epicsEnvSet("NAME", "value")  /  epicsEnvSet NAME value  /  epicsEnvSet "NAME" "value"
_ENV_RE = re.compile(
    r"""epicsEnvSet\s*\(?\s*["']?(?P<name>[A-Za-z0-9_]+)["']?\s*(?:,|\s)\s*"""
    r"""["']?(?P<val>[^"')\n]*)["']?""",
    re.MULTILINE,
)

# dbLoadRecords("file", "macros") / dbLoadTemplate("subs") / iocshLoad "file" "macros"
# (2nd arg optional). dbLoadTemplate is captured for DETECTION only (its records need msi); db_files
# still filters to dbLoadRecords, so the captured command set just lets the loader refuse to claim
# completeness when a mechanism it cannot statically follow is present.
_LOAD_RE = re.compile(
    r"""(?P<cmd>dbLoadRecords|dbLoadTemplate|iocshLoad)\s*\(?\s*["'](?P<file>[^"']+)["']"""
    r"""\s*(?:,\s*)?(?:["'](?P<macros>[^"']*)["'])?""",
    re.MULTILINE,
)

# record(type, "NAME")  — the record/PV name is the quoted 2nd argument.
_RECORD_RE = re.compile(r'record\s*\(\s*[A-Za-z0-9_]+\s*,\s*"([^"]+)"\s*\)')

# alias("record", "aliasName")  (standalone)  /  alias("aliasName")  (inside a record body).
# Either way the ALIAS name is a real PV the IOC serves: 2nd quoted arg if present, else the 1st.
# Leading non-word guard so identifiers ending in "alias" (e.g. setMyalias(...)) are NOT captured.
# (``_RECORD_RE`` deliberately keeps the substring match so ``grecord(...)`` is still recognised.)
_ALIAS_RE = re.compile(r'(?<![A-Za-z0-9_])alias\s*\(\s*"([^"]+)"\s*(?:,\s*"([^"]+)"\s*)?\)')

# A macro reference in either $(NAME) or ${NAME} form.
_MACRO_REF_RE = re.compile(r"\$\{([A-Za-z0-9_]+)\}|\$\(([A-Za-z0-9_]+)\)")


def _strip_line_comment(line: str) -> str:
    """Cut a ``#`` comment to end-of-line, but ONLY when the ``#`` is outside a quoted string.

    EPICS iocsh and ``.db`` both treat ``#`` as a comment. A leading-``#`` line becomes empty; a
    trailing ``# ...`` (e.g. ``alias("X") # note``) is cut — so a commented-out, still-templated
    ``record``/``alias`` no longer pollutes the PV set. A ``#`` INSIDE quotes (a record/field/value)
    is preserved, so real names are never corrupted.
    """
    in_quote = False
    for index, char in enumerate(line):
        # A backslash-escaped quote (``\"``, EPICS allows it inside a field value like
        # ``field(DESC, "a \"x\" # y")``) is NOT a quote boundary — counting it would flip
        # ``in_quote`` and mis-cut a trailing ``#`` inside the value (S7-4).
        if char == '"' and (index == 0 or line[index - 1] != "\\"):
            in_quote = not in_quote
        elif char == "#" and not in_quote:
            return line[:index]
    return line


def _strip_comment_lines(text: str) -> str:
    """Strip ``#`` comments (full-line AND inline-outside-quotes) line by line; structure kept."""
    return "\n".join(_strip_line_comment(line) for line in text.splitlines())


def substitute(text: str, macros: dict[str, str], *, max_depth: int = 10) -> str:
    """Expand ``$(NAME)``/``${NAME}`` in *text* from *macros* (bounded, undefined stay literal).

    Deterministic and pure: undefined macros are left untouched (so the caller can detect
    "still unresolved"); nested macros resolve over up to *max_depth* passes.
    """

    def _repl(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        return macros.get(name, match.group(0))

    for _ in range(max_depth):
        expanded = _MACRO_REF_RE.sub(_repl, text)
        if expanded == text:
            break
        text = expanded
    return text


def _parse_macro_string(macro_str: str) -> dict[str, str]:
    """Parse a Phoebus/e3 macro string ``"A=1,B=2"`` into a dict (comma-separated)."""
    out: dict[str, str] = {}
    for part in macro_str.split(","):
        if "=" in part:
            name, value = part.split("=", 1)
            out[name.strip()] = value.strip()
    return out


@dataclass
class Load:
    """One ``dbLoadRecords``/``iocshLoad`` call from an ``st.cmd``."""

    command: str  # "dbLoadRecords" | "iocshLoad"
    target: str  # file path (may contain $(MODULE_DIR))
    macros: dict[str, str] = field(default_factory=dict)


@dataclass
class StCmdInfo:
    """Structured view of an e3 ``st.cmd`` (read-only static parse)."""

    requires: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    loads: list[Load] = field(default_factory=list)
    prefix: str | None = None  # dominant P= value, e.g. "DEV-TEST01:Ctrl-EVR-01:"

    @property
    def device_name(self) -> str | None:
        """The ESS device name for the Naming Service (prefix without ONE trailing ':')."""
        if not self.prefix:
            return None
        return self.prefix[:-1] if self.prefix.endswith(":") else self.prefix

    @property
    def db_files(self) -> list[str]:
        """The ``.db`` files loaded directly via ``dbLoadRecords`` (deterministic order)."""
        return sorted(
            {
                load.target
                for load in self.loads
                if load.command == "dbLoadRecords" and load.target.endswith(".db")
            }
        )


def parse_st_cmd(text: str) -> StCmdInfo:
    """Parse an e3 ``st.cmd`` into a :class:`StCmdInfo` (pure, deterministic)."""
    text = _strip_comment_lines(text)
    info = StCmdInfo()
    info.requires = _REQUIRE_RE.findall(text)

    # Env vars in document order; each value sees the env defined so far.
    for match in _ENV_RE.finditer(text):
        name = match.group("name")
        info.env[name] = substitute(match.group("val"), info.env)

    # dbLoadRecords/iocshLoad calls; macro values expand against the env.
    prefixes: Counter[str] = Counter()
    for match in _LOAD_RE.finditer(text):
        raw_macros = match.group("macros") or ""
        macros = {
            name: substitute(value, info.env)
            for name, value in _parse_macro_string(raw_macros).items()
        }
        info.loads.append(
            Load(command=match.group("cmd"), target=match.group("file"), macros=macros)
        )
        p_value = macros.get("P")
        # Only record-instantiating loads vote for the IOC device prefix. dbLoadTemplate is
        # captured in _LOAD_RE for completeness DETECTION only (its records need msi); a stray
        # dbLoadTemplate P= must not skew the prefix (it drives bucketing + the Naming query). A P=
        # that stays templated after env substitution (points at a name NOT in epicsEnvSet, e.g. a
        # require/iocsh argument) is unresolved and must NOT vote for a concrete prefix either
        # (S7-2), consistent with the "never a still-templated name" discipline. Truthiness (not
        # ``is not None``) also excludes an EMPTY ``P=`` — an empty prefix carries no device
        # information and must never outvote a real prefix in a mixed st.cmd (S7-2).
        if (
            p_value
            and "$(" not in p_value
            and "${" not in p_value
            and match.group("cmd") != "dbLoadTemplate"
        ):
            prefixes[p_value] += 1

    if prefixes:
        # Most common P= value wins; ties resolve to the lexicographically first (deterministic).
        top = max(prefixes.items(), key=lambda kv: (kv[1], _neg_key(kv[0])))
        info.prefix = top[0]
    return info


def _neg_key(text: str) -> tuple[int, ...]:
    """Sort helper: makes ``max`` prefer the lexicographically smallest string on a tie."""
    return tuple(-ord(ch) for ch in text)


def ioc_db_pvs(db_text: str, macros: dict[str, str]) -> tuple[set[str], set[str]]:
    """Extract record AND alias (PV) names from an EPICS ``.db`` text, substituting *macros*.

    Returns ``(resolved, unresolved)``: *resolved* = names fully expanded; *unresolved* =
    names that still contain ``$(...)``/``${...}`` after substitution (e.g. substitution-
    file driven — "needs-msi"). Aliases are included because a display PV may legitimately
    reference an alias rather than the record name; omitting them would make a real PV look
    "broken". Never raises.
    """
    db_text = _strip_comment_lines(db_text)
    resolved: set[str] = set()
    unresolved: set[str] = set()
    raw_names = list(_RECORD_RE.findall(db_text))
    # The alias NAME is the 2nd quoted arg (standalone form) or the 1st (in-body form).
    raw_names += [(grp2 or grp1) for grp1, grp2 in _ALIAS_RE.findall(db_text)]
    for raw_name in raw_names:
        name = substitute(raw_name, macros)
        if "$(" in name or "${" in name:
            unresolved.add(name)
        else:
            resolved.add(name)
    return resolved, unresolved


@dataclass(frozen=True)
class IocDbResult:
    """The concrete IOC PV set loaded from a local module/db root (opt-in, read-only).

    ``complete`` is the load-bearing flag: it is True ONLY when the static load is provably
    complete — every referenced ``.db`` found unambiguously, every name fully resolved (no
    needs-msi), and NO record-loading mechanism we cannot statically follow (``dbLoadTemplate`` or
    ``iocshLoad``) present. It gates the cross-plane ``broken`` verdict; conservative by design
    (in doubt → False → the verdict is withheld, never a false alarm).
    """

    resolved: frozenset[str]
    unresolved: frozenset[str]
    complete: bool
    missing: tuple[str, ...]  # .db targets referenced but not found under the root
    ambiguous: tuple[str, ...]  # .db basenames matching >1 file (not loaded — wrong-module risk)
    unsupported_load: (
        bool  # dbLoadTemplate / iocshLoad present → records we cannot statically follow
    )


def _iter_files_bounded(root: Path, *, max_depth: int = 8) -> Iterator[Path]:
    """Yield files under *root* up to *max_depth* levels deep (no unbounded filesystem walk)."""
    root = root.resolve()
    root_depth = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root):
        if len(Path(dirpath).parts) - root_depth >= max_depth:
            dirnames[:] = []  # prune deeper traversal
        for filename in filenames:
            yield Path(dirpath) / filename


def _build_basename_index(root: Path) -> dict[str, list[Path]]:
    """Map each basename under *root* to its resolved paths via ONE bounded walk (S7-3).

    Built once per :func:`load_ioc_db` so N ``dbLoadRecords`` loads that fall back to a basename
    search no longer each trigger a full ``os.walk``. A same-named ``.db`` in several modules yields
    multiple paths (the caller treats >1 as ambiguous).
    """
    index: dict[str, list[Path]] = {}
    for f in _iter_files_bounded(root):
        index.setdefault(f.name, []).append(f.resolve())
    return index


def _locate_db(target: str, root: Path, basename_index: dict[str, list[Path]]) -> list[Path]:
    """Resolve a (macro-substituted) ``.db`` *target* to file(s) under *root* (deterministic).

    Primary: the target as a direct path (absolute, or relative to *root* — this resolves the
    synthesised ``$(<module>_DIR)/...`` form). Secondary: a lookup in *basename_index* (prebuilt
    once per :func:`load_ioc_db` — S7-3). Returns ALL matches sorted; the caller treats 0 = missing
    and >1 = ambiguous (a same-named ``.db`` in several modules must not silently pick a wrong set).
    """
    path = Path(target)
    direct = path if path.is_absolute() else (root / path)
    if direct.is_file():
        return [direct.resolve()]
    return sorted(set(basename_index.get(path.name, [])))


def load_ioc_db(st_info: StCmdInfo, module_db_root: Path) -> IocDbResult:
    """Load the IOC's concrete ``.db`` PV set from a local module/db *root* (opt-in, read-only).

    Iterates ``st_info.loads`` (NOT ``db_files`` — the per-load ``P=`` macro lives on the ``Load``
    and is what makes ``$(P)Foo`` concrete). For each ``dbLoadRecords`` ``.db``: synthesise
    ``<module>_DIR`` from the ``require``d modules + *root*, resolve the path, read it, and extract
    record/alias PVs substituting ``st_info.env`` + the synthesised dirs + the per-load macros.
    Returns an :class:`IocDbResult` whose ``complete`` flag gates the ``broken`` verdict. Pure +
    deterministic + graceful (a missing/unreadable file is recorded, never raised).
    """
    dir_env = {f"{module}_DIR": str(module_db_root / module) for module in st_info.requires}
    base_env = {**st_info.env, **dir_env}
    # Walk the module/db root ONCE; every basename fallback below reads this index (S7-3).
    basename_index = _build_basename_index(module_db_root)
    resolved: set[str] = set()
    unresolved: set[str] = set()
    missing: list[str] = []
    ambiguous: list[str] = []

    for load in st_info.loads:
        if load.command != "dbLoadRecords" or not load.target.endswith(".db"):
            continue
        target = substitute(load.target, base_env)
        if "$(" in target or "${" in target:
            # Path macro stayed unresolved (e.g. an unsynthesised/versioned module dir). Do NOT fall
            # back to a basename search — it could load a same-named .db from the WRONG module and
            # report it as the IOC's authoritative PV set. Force missing → complete=False.
            missing.append(load.target)
            continue
        matches = _locate_db(target, module_db_root, basename_index)
        if not matches:
            missing.append(load.target)
            continue
        if len(matches) > 1:
            ambiguous.append(load.target)  # same basename in several modules → don't guess
            continue
        try:
            text = matches[0].read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            missing.append(load.target)
            continue
        file_resolved, file_unresolved = ioc_db_pvs(text, {**base_env, **load.macros})
        resolved |= file_resolved
        unresolved |= file_unresolved

    # Any iocshLoad/dbLoadTemplate loads records we cannot statically follow → we cannot claim the
    # IOC's PV set is complete (the bulk of an e3 EVR's records come in via iocshLoad'ed .iocsh).
    unsupported = any(load.command in {"iocshLoad", "dbLoadTemplate"} for load in st_info.loads)
    # ``bool(resolved)`` is load-bearing: a degenerate st.cmd (no dbLoadRecords, or a comment-/
    # record-less .db) enumerates ZERO PVs — without this term it would report complete=True over an
    # EMPTY set and crossplane would flag EVERY linked PV as broken (the exact trap we close).
    complete = (
        bool(resolved) and not missing and not ambiguous and not unresolved and not unsupported
    )
    return IocDbResult(
        resolved=frozenset(resolved),
        unresolved=frozenset(unresolved),
        complete=complete,
        missing=tuple(sorted(missing)),
        ambiguous=tuple(sorted(ambiguous)),
        unsupported_load=unsupported,
    )
