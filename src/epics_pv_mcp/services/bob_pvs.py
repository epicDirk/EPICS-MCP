"""Self-contained PV extraction from Phoebus ``.bob`` display files.

Vendored (pure stdlib) equivalent of ``phoebus_mcp_core.bob_parser.extract_pvs`` so the
cross-plane check stays standalone (no dependency on the MCP repo). Same element set and
filters as the canonical walker (``MCP/src/phoebus_mcp_core/bob_parser.py:79-109``).

The returned PV strings are **raw**: macros like ``$(P):Value`` are kept verbatim (full
per-instance macro expansion lives in the parked ``opi_navigation`` PV-inventory work, not
here). Pure macro-only references (``$(pv_name)``) and ``=``-formulas are skipped.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

# Direct PV-bearing element names: standard widget PV, rule/script PV, XY-plot axis PVs.
_PV_ELEMENTS = frozenset({"pv_name", "pv", "x_pv", "y_pv"})
# XY-plot trace PVs: trace_N_x_pv / trace_N_y_pv / trace_N_err_pv.
_TRACE_PV_RE = re.compile(r"^trace_\d+_(?:x|y|err)_pv$")
# A pure macro-only reference such as "$(pv_name)" or "${pv_name}" — skipped (no concrete
# PV). Both Phoebus macro syntaxes are covered (a .bob may use either form).
_PURE_MACRO_RE = re.compile(r"\$\([^)]+\)|\$\{[^}]+\}")


def _pv_text(elem: ET.Element) -> str | None:
    """Return the cleaned PV string of a PV-bearing *elem*, or None if it carries none."""
    text = (elem.text or "").strip()
    if not text:
        text = (elem.get("name") or "").strip()
    if not text or text.startswith("="):
        return None
    if _PURE_MACRO_RE.fullmatch(text):
        return None
    return text


def extract_pvs(file_path: str | Path) -> list[str]:
    """Extract the sorted, de-duplicated raw PV names referenced in one ``.bob`` file.

    Raises ``OSError``/``ET.ParseError`` is suppressed: an unreadable/malformed file
    yields an empty list (the caller decides how to surface parse failures).
    """
    try:
        root = ET.parse(file_path).getroot()
    except (OSError, ET.ParseError):
        return []
    pvs: set[str] = set()
    for elem in root.iter():
        if elem.tag in _PV_ELEMENTS or _TRACE_PV_RE.match(elem.tag):
            text = _pv_text(elem)
            if text is not None:
                pvs.add(text)
    return sorted(pvs)


def extract_pvs_from_dir(directory: str | Path) -> dict[str, list[str]]:
    """Map each ``.bob`` under *directory* (recursive) to its raw PV list.

    Keys are POSIX paths relative to *directory* (deterministic, sorted).
    """
    base = Path(directory)
    result: dict[str, list[str]] = {}
    for path in sorted(base.rglob("*.bob")):
        rel = path.relative_to(base).as_posix()
        result[rel] = extract_pvs(path)
    return result
