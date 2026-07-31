"""QA-44: the corrected claim about pydantic-settings' ``extra`` cannot come back.

Three docstrings stated that pydantic-settings defaults to ``extra="ignore"`` and that THIS is
what discards an unknown ``EPICS_MCP_*`` variable. Both halves were wrong, measured on the pinned
version, and ``c42dd16`` replaced them. Nothing held the correction, so the claim was free to
return; it had already survived a full QA round once.

TWO GUARDS, and the split is the point, because the obvious single guard INVERTS.

The acceptance criterion this ticket carried was: build the needle at run time as
``{"allow", "ignore", "forbid"}`` minus the measured default, and scan the tracked files. Followed
literally that is green today (default ``forbid``, so the needle is ``{allow, ignore}``, no hits)
and it goes wrong at exactly the event it exists for. Let pydantic-settings change its default to
``ignore`` and the needle becomes ``{allow, forbid}``, at which point the ELEVEN legitimate
``ConfigDict(..., extra="forbid")` lines in this repository all become hits. The guard would then
redden over correct code while the stale claim it was built for went unnoticed. A needle derived
from the thing it watches inverts with it, so neither guard below derives one.

* :func:`test_the_wrong_extra_default_claim_is_not_back` holds the SPELLING, with a fixed pattern.
* :func:`test_the_measured_pydantic_settings_default_is_still_forbid` holds the LIBRARY FACT the
  corrected docstrings cite by version and number.

What the second one is NOT, stated because the tempting label is wrong and was written once: it is
not a behaviour pin. Flipping the effective ``extra`` changes nothing about what the docstrings
describe, measured: with ``forbid`` and with ``ignore``, an unknown ``EPICS_MCP_*`` variable is
dropped either way, because ``env_prefix`` asks per declared field and a name no field asks for is
never read at all. The docstrings say so in those words. What the pin protects is a DATED FIGURE
quoted in shipped prose, which is the category ``docs/known-limits.md`` calls an unguarded number.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from pydantic_settings import BaseSettings

from epics_mcp.config import EpicsConfig

_REPO = Path(__file__).resolve().parents[1]

#: The dead spelling, as a pattern rather than a literal, and the width is measured rather than
#: assumed. ``git grep 'extra="ignore"'`` finds only TWO of the three sites ``c42dd16`` repaired:
#: the third (``tests/test_config.py:147`` at that revision) wrote it WITHOUT quotes, inside prose,
#: which is the form a returning claim naturally takes. A fixed literal would have shipped having
#: provably missed a third of its own class.
#:
#: It also cannot match its own source text, because the pattern carries the escapes and the needle
#: does not. So this module needs no self-exemption, and therefore none of the machinery a
#: self-exemption drags with it: an entry that can never be checked, kept out of the reverse
#: direction for a reason that has to be argued each time somebody reads it.
_DEAD_SPELLING = re.compile(r"""extra\s*=\s*["']?ignore""")

#: Files allowed to carry the dead spelling, each with the reason, because an exception without one
#: becomes a blanket permission the moment nobody remembers what it was for. Empty today, and that
#: is measured: the repository carries zero hits. Add an entry the day a legitimate
#: ``ConfigDict(extra="ignore")`` appears, rather than widening the pattern.
_ALLOWED_TO_CARRY_IT: dict[str, str] = {}


def _tracked_text() -> dict[str, str]:
    """Every tracked file that decodes as UTF-8, keyed by its git path.

    Its own population rather than a helper borrowed from ``tests/test_guide.py``: that one filters
    by suffix and by name, and inheriting those exclusions would silently narrow a scan meant to be
    repo-wide. ``tests/test_product_name.py`` argues the same point about the same question.
    """
    listing = subprocess.run(
        ["git", "-C", str(_REPO), "ls-files"],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    ).stdout.splitlines()
    # A successful git call can still return an empty listing, and an empty population makes the
    # assertion below pass while checking nothing. Anchored on a file that must exist.
    assert "src/epics_mcp/config.py" in listing, (
        f"git ls-files returned a tree without config.py ({len(listing)} entries), the population "
        "anchor broke and this scan would pass vacuously"
    )

    files: dict[str, str] = {}
    for name in listing:
        path = _REPO / name
        if not path.is_file():
            continue
        try:
            files[name] = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
    return files


def test_the_wrong_extra_default_claim_is_not_back() -> None:
    """No tracked file states the dead spelling, except where it is declared on purpose.

    Red-proof, measured rather than asserted: at ``3ea7b84``, the revision before the repair, this
    pattern finds THREE hits (``config.py:15``, ``config.py:225``, ``tests/test_config.py:147``)
    while the literal ``extra="ignore"`` finds two. The ticket's own acceptance criterion said
    "exactly three", so the literal version would have reported two, been accepted, and shipped
    blind to the unquoted form.
    """
    offenders = {
        name: [i for i, line in enumerate(text.splitlines(), 1) if _DEAD_SPELLING.search(line)]
        for name, text in _tracked_text().items()
        if _DEAD_SPELLING.search(text) and name not in _ALLOWED_TO_CARRY_IT
    }

    assert not offenders, (
        f"the retired claim about pydantic-settings' extra default is back in {sorted(offenders)}, "
        f"at these lines: {offenders}. An unknown EPICS_MCP_* variable is dropped by env_prefix "
        "before extra is consulted; see UnknownEpicsEnvVarWarning. If a legitimate "
        'ConfigDict(extra="ignore") is meant, add the file to _ALLOWED_TO_CARRY_IT with the reason.'
    )


def test_each_allowed_file_still_carries_the_dead_spelling() -> None:
    """An exception list rots into a blanket permission unless its entries are checked too.

    Vacuous while the list is empty, and deliberately kept: the first entry added would otherwise
    arrive unguarded, which is the moment the reasoning is least likely to be re-derived.
    """
    files = _tracked_text()
    for name, why in _ALLOWED_TO_CARRY_IT.items():
        assert name in files, f"{name} is gone; drop its exception rather than leaving it standing"
        assert _DEAD_SPELLING.search(files[name]), (
            f"{name} no longer carries the spelling, so its exception ({why}) protects nothing and "
            "now only widens the guard. Remove the entry"
        )


def test_the_measured_pydantic_settings_default_is_still_forbid() -> None:
    """The library fact the corrected docstrings quote by version and number.

    ``UnknownEpicsEnvVarWarning`` states "Measured on pydantic-settings 2.14.2: the effective
    ``extra`` of this class is ``forbid``", and a figure that lives only in prose is an unguarded
    figure. Read from ``EpicsConfig``, which is what the sentence is about; ``EpicsConfig`` sets
    only ``env_prefix``, so the value it reports is inherited and a library change moves it.

    ⚠️ If this reddens, the repair is to re-measure and update the three docstrings and this
    assertion. It is NOT to widen the spelling guard above: that one holds a claim about a
    MECHANISM, and the mechanism does not move when this value does. Measured both ways, an
    unknown EPICS_MCP_* variable is dropped under ``forbid`` and under ``ignore`` alike.
    """
    effective = EpicsConfig.model_config.get("extra")
    assert effective == "forbid", (
        f"EpicsConfig's effective extra is {effective!r}, not 'forbid'. "
        "The docstring of UnknownEpicsEnvVarWarning quotes that value with a version and a date; "
        "re-measure it and update the prose. Do NOT widen the spelling guard in this module: what "
        "drops an unknown variable is env_prefix, and that is unaffected by this value."
    )
    assert BaseSettings.model_config.get("extra") == "forbid", (
        "the value above is inherited, so this states where it comes from: if the two ever "
        "disagree, EpicsConfig has started setting extra itself and the docstrings need to say so"
    )
