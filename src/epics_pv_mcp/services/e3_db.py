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

import re
from collections import Counter
from dataclasses import dataclass, field

# require <module>  (optional quotes, optional version after a comma)
_REQUIRE_RE = re.compile(r'^\s*require\s+["\']?([A-Za-z0-9_\-]+)', re.MULTILINE)

# epicsEnvSet("NAME", "value")  /  epicsEnvSet NAME value  /  epicsEnvSet "NAME" "value"
_ENV_RE = re.compile(
    r"""epicsEnvSet\s*\(?\s*["']?(?P<name>[A-Za-z0-9_]+)["']?\s*(?:,|\s)\s*"""
    r"""["']?(?P<val>[^"')\n]*)["']?""",
    re.MULTILINE,
)

# dbLoadRecords("file", "macros")  /  iocshLoad "file" "macros"  (2nd arg optional)
_LOAD_RE = re.compile(
    r"""(?P<cmd>dbLoadRecords|iocshLoad)\s*\(?\s*["'](?P<file>[^"']+)["']"""
    r"""\s*(?:,\s*)?(?:["'](?P<macros>[^"']*)["'])?""",
    re.MULTILINE,
)

# record(type, "NAME")  — the record/PV name is the quoted 2nd argument.
_RECORD_RE = re.compile(r'record\s*\(\s*[A-Za-z0-9_]+\s*,\s*"([^"]+)"\s*\)')

# A macro reference in either $(NAME) or ${NAME} form.
_MACRO_REF_RE = re.compile(r"\$\{([A-Za-z0-9_]+)\}|\$\(([A-Za-z0-9_]+)\)")


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
        """The ESS device name for the Naming Service (prefix without trailing ':')."""
        return self.prefix.rstrip(":") if self.prefix else None

    @property
    def db_files(self) -> list[str]:
        """The ``.db`` files loaded directly via ``dbLoadRecords`` (deterministic order)."""
        return sorted(
            {load.target for load in self.loads if load.command == "dbLoadRecords"
             and load.target.endswith(".db")}
        )


def parse_st_cmd(text: str) -> StCmdInfo:
    """Parse an e3 ``st.cmd`` into a :class:`StCmdInfo` (pure, deterministic)."""
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
        info.loads.append(Load(command=match.group("cmd"), target=match.group("file"),
                               macros=macros))
        p_value = macros.get("P")
        if p_value:
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
    """Extract record (PV) names from an EPICS ``.db`` text, substituting *macros*.

    Returns ``(resolved, unresolved)``: *resolved* = names fully expanded; *unresolved* =
    names that still contain ``$(...)``/``${...}`` after substitution (e.g. substitution-
    file driven — "needs-msi"). Never raises.
    """
    resolved: set[str] = set()
    unresolved: set[str] = set()
    for raw_name in _RECORD_RE.findall(db_text):
        name = substitute(raw_name, macros)
        if "$(" in name or "${" in name:
            unresolved.add(name)
        else:
            resolved.add(name)
    return resolved, unresolved
