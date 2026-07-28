"""Static, Windows-safe parsing of ESS e3 IOC startup scripts and EPICS databases.

Pure-Python, read-only, no running IOC, no EPICS base, no SWIG. Two jobs:

1. :func:`parse_st_cmd`, read an e3 ``st.cmd`` (the IOC's startup script) into a
   :class:`StCmdInfo`: the ``require``d modules, ``epicsEnvSet`` variables, the
   ``dbLoadRecords``/``iocshLoad`` calls with their macro strings, and the dominant
   device prefix (the ``P=`` macro, e.g. ``DEV-TEST01:Ctrl-EVR-01:``).
2. :func:`ioc_db_pvs`, regex-extract record (PV) names from an EPICS ``.db`` text and
   substitute simple ``$(MACRO)`` references.

**Known limitation (documented, not a bug):** full ``.substitutions``/template
multi-instance expansion needs the EPICS ``msi`` tool (C++ / Linux/Docker) and is NOT
done here. Records whose names still contain ``$(...)`` after substitution are returned
as *unresolved* ("needs-msi") and must never be reported as "broken". The real ``.db``
of an e3 module also live in the module package (conda), not in the IOC repo, so an IOC
repo's ``st.cmd`` gives the prefix/macros/modules, while full PV enumeration needs the
module repos (deferred).
"""

from __future__ import annotations

import os
import re
from collections import Counter
from collections.abc import Callable, Iterator
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

# record(type, "NAME"), the record/PV name is the quoted 2nd argument.
_RECORD_RE = re.compile(r'record\s*\(\s*[A-Za-z0-9_]+\s*,\s*"([^"]+)"\s*\)')

# alias("record", "aliasName")  (standalone)  /  alias("aliasName")  (inside a record body).
# Either way the ALIAS name is a real PV the IOC serves: 2nd quoted arg if present, else the 1st.
# Leading non-word guard so identifiers ending in "alias" (e.g. setMyalias(...)) are NOT captured.
# (``_RECORD_RE`` deliberately keeps the substring match so ``grecord(...)`` is still recognised.)
_ALIAS_RE = re.compile(r'(?<![A-Za-z0-9_])alias\s*\(\s*"([^"]+)"\s*(?:,\s*"([^"]+)"\s*)?\)')

# Macro references ($(NAME), ${NAME}, $(NAME=default), $(NAME,scope=...)) are parsed by a
# depth-counting scanner (below), NOT a regex: the macLib grammar allows '=' and ',' inside
# a reference and NESTED references in both the name and the default ("$(P=$(Q))",
# "$($(SEL)_PV)"), which no single pattern can match (epics-base macCore.c scans too).
# The old regex here required the char class to touch the closing bracket, so ANY reference
# carrying a default did not match at all, even with the macro defined (BG2).


def _strip_line_comment(line: str) -> str:
    """Cut a ``#`` comment to end-of-line, but ONLY when the ``#`` is outside a quoted string.

    EPICS iocsh and ``.db`` both treat ``#`` as a comment. A leading-``#`` line becomes empty; a
    trailing ``# ...`` (e.g. ``alias("X") # note``) is cut, so a commented-out, still-templated
    ``record``/``alias`` no longer pollutes the PV set. A ``#`` INSIDE quotes (a record/field/value)
    is preserved, so real names are never corrupted.
    """
    in_quote = False
    for index, char in enumerate(line):
        if char == '"':
            # A quote is a real boundary only if the run of backslashes right before it is EVEN
            # (each ``\\`` is a literal backslash; an odd count means the last ``\`` escapes THIS
            # quote). A single-char lookback (S7-4) mishandles ``\\"``, a literal backslash then a
            # real closing quote, so count the parity of the whole preceding backslash run.
            backslashes = 0
            probe = index - 1
            while probe >= 0 and line[probe] == "\\":
                backslashes += 1
                probe -= 1
            if backslashes % 2 == 0:
                in_quote = not in_quote
        elif char == "#" and not in_quote:
            return line[:index]
    return line


def _strip_comment_lines(text: str) -> str:
    """Strip ``#`` comments (full-line AND inline-outside-quotes) line by line; structure kept."""
    return "\n".join(_strip_line_comment(line) for line in text.splitlines())


def _nested_ref_opens(text: str, index: int) -> str | None:
    """The expected CLOSER when ``text[index:]`` opens a nested reference, else ``None``.

    Only a ``$``-introduced bracket opens a reference (macLib scans raw characters, a
    bare bracket is an ordinary character), and each reference closes with ITS bracket
    type only (macCore.c:793: macEnd is ``"=,)"`` for ``$(`` and ``"=,}"`` for ``${``).
    """
    if text[index] == "$" and index + 1 < len(text) and text[index + 1] in "({":
        return ")" if text[index + 1] == "(" else "}"
    return None


def _find_closing_bracket(text: str, start: int) -> int:
    """Index of the bracket closing the reference opened at ``start+1``, or ``-1``.

    Bracket-TYPE-faithful (see :func:`_nested_ref_opens`): for a ``$(`` reference a
    ``}`` is a NAME character, never a terminator, a cross-matching scanner would
    RESOLVE the typo ``$(P}`` and mint a PV name the IOC never serves. Nested
    references (``$(P=$(Q))``, ``${FOO=${BAZ}}``) close at their own bracket.
    """
    expected = [")" if text[start + 1] == "(" else "}"]
    index = start + 2
    while index < len(text):
        closer = _nested_ref_opens(text, index)
        if closer is not None:
            expected.append(closer)
            index += 2
            continue
        if text[index] == expected[-1]:
            expected.pop()
            if not expected:
                return index
        index += 1
    return -1


def _split_body(body: str) -> tuple[str, str]:
    """Split a reference body into ``(name, raw_rest)`` at the first TOP-LEVEL ``=``/``,``.

    Per macLib the name ends there (macCore.c:794); ``raw_rest`` keeps its leading ``=``
    (a default follows) or ``,`` (scoped-macro arguments follow), or is ``""``. Top-level
    means outside any NESTED reference (bare brackets do not nest, raw-character scan).
    """
    expected: list[str] = []
    index = 0
    while index < len(body):
        closer = _nested_ref_opens(body, index)
        if closer is not None:
            expected.append(closer)
            index += 2
            continue
        char = body[index]
        if expected and char == expected[-1]:
            expected.pop()
        elif not expected and char in "=,":
            return body[:index], body[index:]
        index += 1
    return body, ""


def _default_from_rest(rest: str) -> str | None:
    """The default value carried by *rest*, or ``None`` when there is none.

    Only a rest starting with ``=`` carries a default. It ends at a top-level ``,``
    (scoped-macro arguments); further ``=`` inside are legal (macCore.c:812), and an
    empty default is a real ``""`` (macLibTest.c:94). Nesting rule as in
    :func:`_split_body`, only ``$``-introduced references nest.
    """
    if not rest.startswith("="):
        return None
    collected: list[str] = []
    expected: list[str] = []
    index = 1
    while index < len(rest):
        closer = _nested_ref_opens(rest, index)
        if closer is not None:
            expected.append(closer)
            collected.append(rest[index])
            collected.append(rest[index + 1])
            index += 2
            continue
        char = rest[index]
        if expected and char == expected[-1]:
            expected.pop()
        elif not expected and char == ",":
            break
        collected.append(char)
        index += 1
    return "".join(collected)


def _has_macro_ref(text: str) -> bool:
    """True when *text* still carries a reference, a bare ``$`` is an ordinary char."""
    return "$(" in text or "${" in text


#: Bound for the name-expansion recursion. macLib bounds its own recursion too; without a
#: bound, hostile nesting depth (measured: 2000) blows the Python stack, and both
#: ``ioc_db_pvs`` and ``load_ioc_db`` promise "never raises". Real e3 names nest 1-2 deep.
_MAX_NAME_EXPANSION_DEPTH = 32


def _expand_once(text: str, macros: dict[str, str], *, name_depth: int = 0) -> tuple[str, bool]:
    """One left-to-right expansion pass over *text*; returns ``(expanded, changed)``.

    Semantics anchored to epics-base macLib (modules/libcom/src/macLib/macCore.c):
    a DEFINED macro beats its default (:860-880) · an undefined macro WITH a default
    expands to the default · the NAME may itself contain references (:798), is resolved
    first and looked up VERBATIM, a ``=``/``,`` arriving from a macro VALUE never
    becomes a separator (macLib copies the expansion into the lookup buffer; re-parsing
    it would fabricate resolutions) · scoped arguments after a top-level ``,`` are
    recognised, not evaluated. An undefined macro WITHOUT a default stays literal:
    the project's needs-msi convention (callers detect "still unresolved"), deliberately
    narrower than macLib's warning path; a name whose INNER references stay unresolved
    keeps the whole reference literal rather than emitting a half-expanded hybrid.
    """
    out: list[str] = []
    index = 0
    changed = False
    while index < len(text):
        char = text[index]
        if char != "$" or index + 1 >= len(text) or text[index + 1] not in "({":
            out.append(char)
            index += 1
            continue
        end = _find_closing_bracket(text, index)
        if end == -1:  # unbalanced reference -> literal
            out.append(char)
            index += 1
            continue
        body = text[index + 2 : end]
        name, rest = _split_body(body)
        if _has_macro_ref(name):
            if name_depth >= _MAX_NAME_EXPANSION_DEPTH:
                out.append(text[index : end + 1])  # hostile depth -> literal, never raise
                index = end + 1
                continue
            expanded_name = name
            for _ in range(_MAX_NAME_EXPANSION_DEPTH):
                expanded_name, inner_changed = _expand_once(
                    expanded_name, macros, name_depth=name_depth + 1
                )
                if not inner_changed:
                    break
            if _has_macro_ref(expanded_name):
                out.append(text[index : end + 1])  # inner refs unresolved -> whole literal
                index = end + 1
                continue
            name = expanded_name  # verbatim lookup; separators from values stay inert
        default = _default_from_rest(rest)
        if name in macros:
            out.append(macros[name])
            changed = True
        elif default is not None:
            out.append(default)
            changed = True
        else:
            out.append(text[index : end + 1])
        index = end + 1
    return "".join(out), changed


def substitute(text: str, macros: dict[str, str], *, max_depth: int = 10) -> str:
    """Expand ``$(NAME)``/``${NAME}``/``$(NAME=default)`` in *text* from *macros*.

    Deterministic and pure, grammar per epics-base macLib (see :func:`_expand_once`):
    a defined macro beats its default; an undefined macro WITH a default expands to the
    default (an empty default is a real ``""``); an undefined macro WITHOUT a default
    stays literal, so the caller can detect "still unresolved" (needs-msi). Nested and
    recursive values resolve over up to *max_depth* passes; cycles terminate bounded.
    """
    for _ in range(max_depth):
        text, changed = _expand_once(text, macros)
        if not changed:
            break
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
        # ``is not None``) also excludes an EMPTY ``P=``, an empty prefix carries no device
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
    file driven, "needs-msi"). Aliases are included because a display PV may legitimately
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
    complete, every referenced ``.db`` found unambiguously, every name fully resolved (no
    needs-msi), and NO record-loading mechanism we cannot statically follow (``dbLoadTemplate`` or
    ``iocshLoad``) present. It gates the cross-plane ``broken`` verdict; conservative by design
    (in doubt → False → the verdict is withheld, never a false alarm).
    """

    resolved: frozenset[str]
    unresolved: frozenset[str]
    complete: bool
    missing: tuple[str, ...]  # .db targets referenced but not found under the root
    ambiguous: tuple[str, ...]  # .db basenames matching >1 file (not loaded, wrong-module risk)
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

    A same-named ``.db`` in several modules yields multiple paths (the caller treats >1 as
    ambiguous).
    """
    index: dict[str, list[Path]] = {}
    for f in _iter_files_bounded(root):
        index.setdefault(f.name, []).append(f.resolve())
    return index


def _lazy_basename_index(root: Path) -> Callable[[str], list[Path]]:
    """Return a ``basename -> paths`` lookup that builds its index on the FIRST query (S7-3).

    Loads that all resolve via the direct ``$(<module>_DIR)/...`` path never query this, so the
    common case does ZERO filesystem walks; the (bounded) walk happens once, lazily, only if some
    load actually falls back to a basename search, and is cached for the remaining loads.
    """
    cache: dict[str, list[Path]] | None = None

    def lookup(name: str) -> list[Path]:
        nonlocal cache
        if cache is None:
            cache = _build_basename_index(root)
        return cache.get(name, [])

    return lookup


def _locate_db(target: str, root: Path, basename_lookup: Callable[[str], list[Path]]) -> list[Path]:
    """Resolve a (macro-substituted) ``.db`` *target* to file(s) under *root* (deterministic).

    Primary: the target as a direct path (absolute, or relative to *root*, this resolves the
    synthesised ``$(<module>_DIR)/...`` form). Secondary: *basename_lookup* (a lazy per-
    :func:`load_ioc_db` index, S7-3). Returns ALL matches sorted; the caller treats 0 = missing and
    >1 = ambiguous (a same-named ``.db`` in several modules must not silently pick a wrong set).
    """
    path = Path(target)
    direct = path if path.is_absolute() else (root / path)
    if direct.is_file():
        return [direct.resolve()]
    return sorted(set(basename_lookup(path.name)))


def load_ioc_db(st_info: StCmdInfo, module_db_root: Path) -> IocDbResult:
    """Load the IOC's concrete ``.db`` PV set from a local module/db *root* (opt-in, read-only).

    Iterates ``st_info.loads`` (NOT ``db_files``, the per-load ``P=`` macro lives on the ``Load``
    and is what makes ``$(P)Foo`` concrete). For each ``dbLoadRecords`` ``.db``: synthesise
    ``<module>_DIR`` from the ``require``d modules + *root*, resolve the path, read it, and extract
    record/alias PVs substituting ``st_info.env`` + the synthesised dirs + the per-load macros.
    Returns an :class:`IocDbResult` whose ``complete`` flag gates the ``broken`` verdict. Pure +
    deterministic + graceful (a missing/unreadable file is recorded, never raised).
    """
    dir_env = {f"{module}_DIR": str(module_db_root / module) for module in st_info.requires}
    base_env = {**st_info.env, **dir_env}
    # Lazy: walk the module/db root at most ONCE, and only if some load actually falls back to a
    # basename search, loads that all resolve via the direct $(<module>_DIR)/... path walk 0×
    # (S7-3).
    basename_lookup = _lazy_basename_index(module_db_root)
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
            # back to a basename search, it could load a same-named .db from the WRONG module and
            # report it as the IOC's authoritative PV set. Force missing → complete=False.
            missing.append(load.target)
            continue
        matches = _locate_db(target, module_db_root, basename_lookup)
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
    # record-less .db) enumerates ZERO PVs, without this term it would report complete=True over an
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
