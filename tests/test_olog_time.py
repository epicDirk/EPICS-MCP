"""Tests for the Olog search time-window normalization.

The contract under test was established by LIVE measurement against an Olog sandbox, not by reading
the server source — two prior reviewers read the same Java and drew opposite (both wrong)
conclusions. Every expectation below mirrors an observed server response; see
``services/olog_time`` for the mechanism.
"""

from __future__ import annotations

import pytest

from epics_pv_mcp.services.olog_time import (
    OLOG_WIRE_TZ,
    OlogTimeFormatError,
    normalize_olog_time,
)


def _wire(value: str) -> str:
    return normalize_olog_time(value, param="start")[0]


class TestAbsoluteTimes:
    """Absolute values become Olog's space-separated wall clock, re-anchored to UTC."""

    def test_iso_with_z_becomes_wall_clock(self) -> None:
        wire, is_absolute = normalize_olog_time("2026-01-01T00:00:00Z", param="start")
        assert wire == "2026-01-01 00:00:00.000"
        assert is_absolute is True
        # The 'T' is the whole bug: Olog's vendored parser only knows a space separator.
        assert "T" not in wire

    def test_offset_is_applied_not_dropped(self) -> None:
        # 02:00+02:00 IS 00:00Z. Dropping the offset instead of applying it would shift the
        # window by two hours - silently, since the result still looks well-formed.
        assert _wire("2026-01-01T02:00:00+02:00") == "2026-01-01 00:00:00.000"

    def test_naive_iso_is_read_as_utc(self) -> None:
        assert _wire("2026-01-01T00:00:00") == "2026-01-01 00:00:00.000"

    def test_date_only_becomes_midnight_utc(self) -> None:
        # Live: a bare date is REJECTED by Olog (its date-only pattern cannot resolve an instant),
        # so we expand it ourselves - exactly what upstream Phoebus' own parser does.
        assert _wire("2026-07-15") == "2026-07-15 00:00:00.000"

    def test_ologs_native_format_round_trips(self) -> None:
        """Normalization is idempotent: feeding back what we emit changes nothing."""
        assert _wire("2026-01-01 00:00:00.000") == "2026-01-01 00:00:00.000"

    def test_sub_millisecond_is_truncated_to_millis(self) -> None:
        # Olog's first-tried pattern demands exactly three fraction digits.
        assert _wire("2026-07-15T10:00:00.123456Z") == "2026-07-15 10:00:00.123"

    def test_year_is_zero_padded(self) -> None:
        # Guards the explicit f-string formatting against a 'tidy-up' to strftime, whose %Y is not
        # portably zero-padded below year 1000.
        assert _wire("0099-01-02T03:04:05Z") == "0099-01-02 03:04:05.000"


class TestRelativeAmounts:
    """Relative amounts pass through verbatim — the server's clock is the authoritative one."""

    @pytest.mark.parametrize(
        "value",
        ["7 days", "1 hour", "30 minutes", "2 weeks", "500 ms", "400 days", "1.5 hour", "90 min"],
    )
    def test_relative_amount_passes_through_verbatim(self, value: str) -> None:
        wire, is_absolute = normalize_olog_time(value, param="start")
        assert wire == value
        assert is_absolute is False

    def test_now_literal_passes_through(self) -> None:
        assert normalize_olog_time("now", param="start") == ("now", False)

    def test_surrounding_whitespace_is_stripped(self) -> None:
        assert _wire("  7 days  ") == "7 days"


class TestRejected:
    """Every value here would otherwise degrade to 'now' server-side → HTTP 200 + empty result.

    That silent wrong answer is the bug this module exists to prevent, so each of these must raise
    BEFORE any request is made.
    """

    @pytest.mark.parametrize("value", ["1 year", "2 months", "1 y", "1 mo", "month", "3 years"])
    def test_months_and_years_rejected_naming_the_alternative(self, value: str) -> None:
        # Olog subtracts the amount from an instant, which cannot carry months/years.
        with pytest.raises(OlogTimeFormatError, match="days or weeks"):
            normalize_olog_time(value, param="start")

    def test_millis_rejected_because_olog_reads_it_as_minutes(self) -> None:
        # Olog's unit dispatch tests startsWith("mi") before equals("ms"): '500 millis' silently
        # means 500 MINUTES. A 1000x error that never announces itself.
        with pytest.raises(OlogTimeFormatError, match="millis"):
            normalize_olog_time("500 millis", param="start")

    def test_bare_m_rejected_because_olog_has_no_such_unit(self) -> None:
        with pytest.raises(OlogTimeFormatError, match="use 'min'"):
            normalize_olog_time("5 m", param="start")

    def test_compound_amount_rejected(self) -> None:
        # Olog discards all sub-day units once a week/month unit is present ('2 weeks 3 hours'
        # == '2 weeks'), so a compound amount cannot be honoured faithfully.
        with pytest.raises(OlogTimeFormatError):
            normalize_olog_time("1 day 20 seconds", param="start")

    @pytest.mark.parametrize("value", ["1767225600", "garbage", "", "   ", "yesterday", "-1 days"])
    def test_unparseable_value_is_rejected(self, value: str) -> None:
        """The root-cause regression: none of these may ever reach the wire."""
        with pytest.raises(OlogTimeFormatError):
            normalize_olog_time(value, param="start")

    def test_trailing_junk_rejected(self) -> None:
        # Olog's own parser substring-scans, so it would accept this and silently use the '7 days'.
        with pytest.raises(OlogTimeFormatError):
            normalize_olog_time("7 days please", param="start")

    def test_error_names_the_offending_param(self) -> None:
        with pytest.raises(OlogTimeFormatError, match="end"):
            normalize_olog_time("garbage", param="end")

    def test_error_is_not_an_olog_error(self) -> None:
        """Must not be an OlogError: checkers maps those to 'cannot reach Olog', which is a lie —
        nothing was sent. Pinning it here so the class is not 'tidied' into olog_exceptions."""
        from epics_pv_mcp.services.olog_exceptions import OlogError

        with pytest.raises(OlogTimeFormatError) as excinfo:
            normalize_olog_time("garbage", param="start")
        assert not isinstance(excinfo.value, OlogError)
        assert isinstance(excinfo.value, ValueError)


def test_wire_tz_is_utc() -> None:
    """Pinned: the wire tz must match the zone _format_wire renders in, or every absolute window
    is silently offset."""
    assert OLOG_WIRE_TZ == "UTC"
