"""GP-19: every named error class carries its code into the guide's error-signature section.

An error code is a wire contract: it is what a caller branches on, and the only place this server
explains what one MEANS is `src/epics_mcp/operator_guide.md`, section "Error signatures". Two codes
(`UNKNOWN_TOPIC`, `GUIDE_DRIFT`) went out on the wire documented nowhere but the changelog, which
is history rather than a reference. Re-measured while fixing that, the gap was larger than the
report: of the 11 classes in `errors.py` that set their own code, EIGHT were absent from the
section and FIVE from the whole guide.

THE POPULATION IS DERIVED, NEVER PINNED, and that is the design decision worth keeping. A list in
this file would document the codes of 2026-08-15 and quietly stop growing; the twelfth class would
be as undocumented as the two that started this, with the guard green. So the population is read
out of `errors.py` itself by AST: every class whose body constructs its base with a literal
`error_code`. Adding a class therefore reddens this test on the day it is written, and nobody has
to remember a table.

SCOPE, and it is deliberately one direction:

* CHECKED: every code `errors.py` names appears in the section. That is the failure that happened.
* NOT CHECKED: the reverse, that the section names no code the server cannot raise. The section
  legitimately names codes raised outside `errors.py` (`INVALID_INPUT` and its four siblings) and
  members of the four dynamic families (`ARCHIVER_HTTP_{status}` and friends), whose literal
  spellings exist nowhere in the source. A guard over that direction would report those as
  inventions, and a guard whose noise outnumbers its findings gets suppressed.
* NOT COVERED AT ALL: the codes raised outside `errors.py`. There are three counting rules for
  that population and they give three different numbers, so `docs/known-limits.md` carries the
  split rather than a single figure. Five of them are input validation and are out by decision;
  five are operational signatures and are open, recorded there as such.

Pattern taken from `tests/test_readme_resources.py`: one source of truth read out of the code, one
owning page, a set difference, and an anchor assertion so an empty population cannot pass as a
clean result.
"""

from __future__ import annotations

import ast
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_ERRORS = _REPO / "src" / "epics_mcp" / "errors.py"
_GUIDE = _REPO / "src" / "epics_mcp" / "operator_guide.md"

#: The section that OWNS the explanation of an error code. If it is renamed, rename it here; the
#: anchor test below turns a rename into a red test rather than into a silent empty search.
OWNING_SECTION = "## Error signatures"

#: The base class default, which no subclass sets on purpose. It is the code an error carries when
#: nobody chose one, so documenting it would document the absence of a decision.
_BASE_DEFAULT = "UNKNOWN"


def error_codes_of(source: str) -> dict[str, str]:
    """`{error code: class name}` for every class in *source* that sets a literal `error_code`.

    Read from the syntax tree rather than by grep: a keyword argument in a constructor call is what
    "this class has its own code" actually means, while a grep for the name also finds the base
    class's default parameter, docstring prose about a sibling's code, and any comment mentioning
    one. All three exist in this file.
    """
    found: dict[str, str] = {}
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.ClassDef):
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            for keyword in inner.keywords:
                if keyword.arg != "error_code":
                    continue
                value = keyword.value
                if (
                    isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                    and value.value != _BASE_DEFAULT
                ):
                    found[value.value] = node.name
    return found


def section_of(text: str, heading_prefix: str) -> str:
    """The `##` section whose heading starts with *heading_prefix*, or the empty string."""
    lines = text.split("\n")
    starts = [index for index, line in enumerate(lines) if line.startswith("## ")]
    for position, start in enumerate(starts):
        if lines[start].startswith(heading_prefix):
            end = starts[position + 1] if position + 1 < len(starts) else len(lines)
            return "\n".join(lines[start:end])
    return ""


def test_the_population_and_the_section_both_exist() -> None:
    """The anchor. An empty population minus anything is empty, which reads as a clean result."""
    codes = error_codes_of(_ERRORS.read_text(encoding="utf-8"))
    assert len(codes) >= 11, (
        f"only {len(codes)} error classes found in {_ERRORS.name}, which is fewer than the 11 "
        "measured on 2026-08-15. Either classes were removed (then lower this floor in the same "
        "commit) or the AST walk stopped matching, in which case every test below is passing on "
        "an empty set."
    )
    assert section_of(_GUIDE.read_text(encoding="utf-8"), OWNING_SECTION), (
        f"the guide has no section starting {OWNING_SECTION!r}. It is the page that owns the "
        "explanation of an error code; if it was renamed, move the constant with it."
    )


def test_every_named_error_code_is_in_the_owning_section() -> None:
    """The rule: a code a caller can branch on is explained where a caller is told to look."""
    codes = error_codes_of(_ERRORS.read_text(encoding="utf-8"))
    section = section_of(_GUIDE.read_text(encoding="utf-8"), OWNING_SECTION)
    missing = sorted(code for code in codes if code not in section)
    assert not missing, (
        f"{len(missing)} error code(s) reach a caller and are explained nowhere in "
        f"{OWNING_SECTION!r}: "
        + ", ".join(f"{code} ({codes[code]})" for code in missing)
        + ". Add a signature entry for each, in the form the section already uses: a bold symptom "
        "sentence, then what the caller should do about it. A mention elsewhere in the guide does "
        "not count, because this is the section a caller is sent to."
    )


def test_a_new_error_class_is_caught_without_touching_a_list() -> None:
    """RED-PROOF, and simultaneously the proof that the population is derived rather than pinned.

    The twelfth class is the case that matters: the two codes this guard was built for were
    undocumented precisely because nothing noticed a NEW one. Constructed source rather than a
    mutation of the real file, so the proof does not depend on anyone editing `errors.py`.
    """
    invented = (
        "class EpicsError(Exception):\n"
        "    def __init__(self, message, error_code='UNKNOWN'):\n"
        "        pass\n"
        "\n"
        "class BrandNewError(EpicsError):\n"
        "    def __init__(self, message):\n"
        "        super().__init__(message, error_code='A_CODE_NOBODY_DOCUMENTED')\n"
    )
    codes = error_codes_of(invented)
    assert codes == {"A_CODE_NOBODY_DOCUMENTED": "BrandNewError"}, (
        "the AST walk no longer finds a newly added error class, so the guard would stay green "
        "while an undocumented code went out on the wire"
    )
    section = section_of(_GUIDE.read_text(encoding="utf-8"), OWNING_SECTION)
    assert "A_CODE_NOBODY_DOCUMENTED" not in section, "the fixture code leaked into the guide"


def test_the_base_default_is_not_treated_as_a_documented_code() -> None:
    """`UNKNOWN` is what an error carries when nobody chose a code, so it is not one to document.

    Kept as its own test because the exclusion is a judgement, not a mechanism: if a future class
    ever sets `UNKNOWN` deliberately, this is the test that has to be argued with first.
    """
    assert _BASE_DEFAULT not in error_codes_of(_ERRORS.read_text(encoding="utf-8"))
