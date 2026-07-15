"""Tests for the shared time-window classifier and the two per-plane wire formats.

The contract under test was established by LIVE measurement against both an Olog sandbox and a real
Alarm Logger, not by reading the server source — two prior reviewers read the same Java and drew
opposite (both wrong) conclusions, and a third assessment cleared the alarm plane on a partial read.
Every expectation below mirrors an observed server response; see ``services/_time_window`` for the
mechanism.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from epics_pv_mcp.services._time_window import TimeWindowFormatError, classify_time_value
from epics_pv_mcp.services.alarm_time import normalize_alarm_time
from epics_pv_mcp.services.olog_time import OLOG_WIRE_TZ, normalize_olog_time


def _classify(value: str) -> datetime | None:
    return classify_time_value(value, param="start")


class TestClassifyAbsolute:
    """Absolute values resolve to an aware UTC datetime, whatever notation they arrive in."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("2026-01-01T00:00:00Z", datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)),
            # The offset must be APPLIED, not dropped: 02:00+02:00 IS 00:00Z. Dropping it would
            # shift the window by two hours while still looking well-formed.
            ("2026-01-01T02:00:00+02:00", datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)),
            # Naive: read as UTC. The server's own zone is unknowable from here, and guessing is
            # the silent offset this module removes.
            ("2026-01-01T00:00:00", datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)),
            ("2026-07-15", datetime(2026, 7, 15, 0, 0, 0, tzinfo=UTC)),
            ("2026-01-01 00:00:00.000", datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)),
        ],
    )
    def test_absolute_values_resolve_to_utc(self, value: str, expected: datetime) -> None:
        assert _classify(value) == expected


class TestClassifyRelative:
    """A valid relative amount returns None = 'pass it through, the server's clock owns it'."""

    @pytest.mark.parametrize(
        "value",
        ["7 days", "1 hour", "30 minutes", "2 weeks", "500 ms", "400 days", "1.5 hour", "90 min"],
    )
    def test_relative_amount_is_not_absolute(self, value: str) -> None:
        assert _classify(value) is None

    def test_now_literal(self) -> None:
        assert _classify("now") is None


class TestRejected:
    """Each of these was measured returning a WRONG answer silently — 200 with an empty list.

    That is the failure this module exists to prevent, so they must raise before any request.
    """

    @pytest.mark.parametrize("value", ["1 year", "2 months", "1 y", "1 mo", "month", "3 years"])
    def test_months_and_years_rejected_naming_the_alternative(self, value: str) -> None:
        with pytest.raises(TimeWindowFormatError, match="days or weeks"):
            _classify(value)

    def test_millis_rejected_because_it_means_minutes(self) -> None:
        # Measured live on the Alarm Logger: '500 millis' RETURNED DATA — for a 500-MINUTE window.
        # The unit dispatch tests startsWith("mi") before equals("ms"). A 1000x error that never
        # announces itself, which is worse than an empty result.
        with pytest.raises(TimeWindowFormatError, match="millis"):
            _classify("500 millis")

    def test_bare_m_rejected(self) -> None:
        with pytest.raises(TimeWindowFormatError, match="use 'min'"):
            _classify("5 m")

    def test_compound_amount_rejected(self) -> None:
        # Once a week/month unit is present all sub-day parts are dropped ('2 weeks 3 hours' ==
        # '2 weeks'), so a compound amount cannot be honoured faithfully.
        with pytest.raises(TimeWindowFormatError):
            _classify("1 day 20 seconds")

    @pytest.mark.parametrize("value", ["1767225600", "garbage", "", "   ", "yesterday", "-1 days"])
    def test_unparseable_value_rejected(self, value: str) -> None:
        """The root-cause regression: none of these may ever reach the wire."""
        with pytest.raises(TimeWindowFormatError):
            _classify(value)

    def test_trailing_junk_rejected(self) -> None:
        # The server substring-scans and would accept this, silently using the '7 days'.
        with pytest.raises(TimeWindowFormatError):
            _classify("7 days please")

    def test_error_names_the_offending_param(self) -> None:
        with pytest.raises(TimeWindowFormatError, match="end"):
            classify_time_value("garbage", param="end")

    def test_error_is_a_value_error_not_a_plane_error(self) -> None:
        """Must not be a per-plane RestClientError: checkers maps those to 'cannot reach it',
        which is a lie — nothing was sent. Pinned so the class is not 'tidied' into a
        *_exceptions module."""
        from epics_pv_mcp.services.rest_exceptions import RestClientError

        with pytest.raises(TimeWindowFormatError) as excinfo:
            _classify("garbage")
        assert not isinstance(excinfo.value, RestClientError)
        assert isinstance(excinfo.value, ValueError)


class TestOlogWireFormat:
    """Olog cannot read ISO at all — it gets a space-separated wall clock plus an explicit tz."""

    def test_iso_becomes_wall_clock(self) -> None:
        wire, is_absolute = normalize_olog_time("2026-01-01T00:00:00Z", param="start")
        assert wire == "2026-01-01 00:00:00.000"
        assert is_absolute is True
        assert "T" not in wire  # the 'T' is the whole bug

    def test_date_only_becomes_midnight(self) -> None:
        # Live: Olog REJECTS a bare date (its date-only pattern cannot resolve an instant), so we
        # expand it — exactly what upstream Phoebus' own parser does.
        assert normalize_olog_time("2026-07-15", param="start")[0] == "2026-07-15 00:00:00.000"

    def test_native_format_round_trips(self) -> None:
        assert normalize_olog_time("2026-01-01 00:00:00.000", param="start")[0] == (
            "2026-01-01 00:00:00.000"
        )

    def test_sub_millisecond_truncated(self) -> None:
        assert normalize_olog_time("2026-07-15T10:00:00.123456Z", param="start")[0] == (
            "2026-07-15 10:00:00.123"
        )

    def test_year_is_zero_padded(self) -> None:
        # Guards the explicit f-string against a 'tidy-up' to strftime, whose %Y is not portably
        # zero-padded below year 1000.
        assert normalize_olog_time("0099-01-02T03:04:05Z", param="start")[0] == (
            "0099-01-02 03:04:05.000"
        )

    def test_relative_passes_through_and_is_not_absolute(self) -> None:
        assert normalize_olog_time("  7 days  ", param="start") == ("7 days", False)

    def test_wire_tz_is_utc(self) -> None:
        """Must match the zone _format_wire renders in, or every absolute window is offset."""
        assert OLOG_WIRE_TZ == "UTC"


class TestAlarmWireFormat:
    """The Alarm Logger reads zone-explicit ISO natively — so it gets the zone, not a bare clock."""

    def test_naive_iso_gains_the_zone(self) -> None:
        """THE alarm regression. Measured live: sent naive, this returned 0 events for a window
        holding 20+; with the zone, it returns them. One character, no error."""
        assert normalize_alarm_time("2026-07-08T12:45:58", param="start") == (
            "2026-07-08T12:45:58.000Z"
        )

    def test_zoned_iso_is_normalized_not_passed_through(self) -> None:
        """Already correct, but still canonicalized — one wire form regardless of input."""
        assert normalize_alarm_time("2026-07-08T12:45:58Z", param="start") == (
            "2026-07-08T12:45:58.000Z"
        )

    def test_offset_is_applied(self) -> None:
        assert normalize_alarm_time("2026-07-08T14:45:58+02:00", param="start") == (
            "2026-07-08T12:45:58.000Z"
        )

    def test_wall_clock_is_anchored_to_utc(self) -> None:
        """The Alarm Logger reads a bare wall clock in ITS OWN zone — sending the zone removes
        that dependency on where the server happens to run."""
        assert normalize_alarm_time("2026-07-08 12:45:58", param="start") == (
            "2026-07-08T12:45:58.000Z"
        )

    def test_millis_are_kept(self) -> None:
        """Truncating to the second would move a caller's boundary silently — the same class of
        quiet inaccuracy this module removes."""
        assert normalize_alarm_time("2026-07-08T12:45:58.123456Z", param="start") == (
            "2026-07-08T12:45:58.123Z"
        )

    def test_relative_passes_through(self) -> None:
        assert normalize_alarm_time("  8 hours  ", param="start") == "8 hours"

    def test_year_is_zero_padded(self) -> None:
        assert normalize_alarm_time("0099-01-02T03:04:05Z", param="start") == (
            "0099-01-02T03:04:05.000Z"
        )


def test_both_planes_share_one_classifier() -> None:
    """The traps are subtle and identical on both planes — pinning that they are enforced from ONE
    implementation, so a fix or a newly-found trap can never land on only one of them."""
    for normalize in (
        lambda v: normalize_olog_time(v, param="start"),
        lambda v: normalize_alarm_time(v, param="start"),
    ):
        for bad in ("1 year", "500 millis", "5 m", "garbage", "1767225600"):
            with pytest.raises(TimeWindowFormatError):
                normalize(bad)
