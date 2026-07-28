"""Unit tests for the EPICS client search-reach parser (E8, epics_mcp.epics_address)."""

from typing import ClassVar

import pytest

from epics_mcp.epics_address import (
    auto_addr_search_disabled,
    is_loopback_host,
    parse_address_list,
    split_host,
    write_reach_violations,
)


class TestSplitHost:
    @pytest.mark.parametrize(
        ("token", "host"),
        [
            ("127.0.0.1", "127.0.0.1"),
            ("127.0.0.1:5075", "127.0.0.1"),
            ("localhost:5064", "localhost"),
            ("[::1]:5075", "::1"),
            ("[2001:db8::1]:5075", "2001:db8::1"),
            ("::1", "::1"),  # bare IPv6, multiple colons, not port-split
            ("gateway.example.org:5075", "gateway.example.org"),
        ],
    )
    def test_split(self, token: str, host: str) -> None:
        assert split_host(token) == host

    def test_unclosed_bracket_returns_whole(self) -> None:
        # Malformed → returned whole so the loopback check fails closed on it.
        assert split_host("[::1") == "[::1"

    def test_non_numeric_port_returns_whole(self) -> None:
        assert split_host("host:abc") == "host:abc"


class TestParseAddressList:
    def test_space_separated(self) -> None:
        assert parse_address_list("127.0.0.1 10.0.0.1:5075 localhost") == [
            "127.0.0.1",
            "10.0.0.1",
            "localhost",
        ]

    def test_empty(self) -> None:
        assert parse_address_list("") == []

    def test_extra_whitespace(self) -> None:
        assert parse_address_list("  127.0.0.1   localhost  ") == ["127.0.0.1", "localhost"]


class TestIsLoopbackHost:
    @pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "127.5.6.7", "::1"])
    def test_loopback(self, host: str) -> None:
        assert is_loopback_host(host) is True

    @pytest.mark.parametrize(
        "host",
        [
            "192.0.2.10",
            "10.0.0.1",
            "host.docker.internal",  # a name is never resolved → fail closed
            "gateway.example.org",
            "2001:db8::1",
            "",
        ],
    )
    def test_not_loopback(self, host: str) -> None:
        assert is_loopback_host(host) is False


class TestAutoAddrSearchDisabled:
    @pytest.mark.parametrize(
        ("value", "disabled"),
        [
            ("NO", True),
            ("no", True),
            ("0", True),
            ("YES", False),
            ("", False),  # unset/empty → default ON
            ("false", False),  # a spelling the pvxs parser rejects → still broadcasts
            ("0 ", False),  # untrimmed, pvxs does not trim
        ],
    )
    def test_pva(self, value: str, disabled: bool) -> None:
        assert auto_addr_search_disabled("pva", value) is disabled

    @pytest.mark.parametrize(
        ("value", "disabled"),
        [
            ("no", True),
            ("NO", True),
            ("nope", True),  # libca strstr: any substring "no" disables
            ("No", False),  # mixed case matches neither strstr("no") nor strstr("NO")
            ("false", False),
            ("0", False),
            ("", False),
        ],
    )
    def test_ca(self, value: str, disabled: bool) -> None:
        assert auto_addr_search_disabled("ca", value) is disabled


class TestWriteReachViolations:
    _LOOPBACK: ClassVar[dict[str, str]] = {
        "EPICS_PVA_ADDR_LIST": "127.0.0.1",
        "EPICS_PVA_NAME_SERVERS": "127.0.0.1:5075",
        "EPICS_PVA_AUTO_ADDR_LIST": "NO",
        "EPICS_CA_ADDR_LIST": "127.0.0.1",
        "EPICS_CA_AUTO_ADDR_LIST": "NO",
    }

    def test_loopback_lane_no_violations(self) -> None:
        assert write_reach_violations(self._LOOPBACK) == []

    def test_empty_lists_but_auto_off_no_violations(self) -> None:
        # No search targets at all + broadcast off = loopback-only (nothing can leave).
        assert (
            write_reach_violations(
                {"EPICS_PVA_AUTO_ADDR_LIST": "NO", "EPICS_CA_AUTO_ADDR_LIST": "NO"}
            )
            == []
        )

    def test_empty_env_broadcasts(self) -> None:
        # Fully empty env: BOTH *_AUTO_ADDR_LIST unset = broadcast ON = two violations.
        violations = write_reach_violations({})
        assert len(violations) == 2
        assert any("EPICS_PVA_AUTO_ADDR_LIST" in v for v in violations)
        assert any("EPICS_CA_AUTO_ADDR_LIST" in v for v in violations)

    def test_public_addr_list(self) -> None:
        env = {**self._LOOPBACK, "EPICS_PVA_ADDR_LIST": "192.0.2.10"}
        violations = write_reach_violations(env)
        assert len(violations) == 1
        assert "EPICS_PVA_ADDR_LIST" in violations[0]
        assert "192.0.2.10" in violations[0]

    def test_mixed_loopback_and_public_addr_list(self) -> None:
        # One good + one bad token in the same list → only the bad one flagged.
        env = {**self._LOOPBACK, "EPICS_CA_ADDR_LIST": "127.0.0.1 10.0.0.5"}
        violations = write_reach_violations(env)
        assert len(violations) == 1
        assert "10.0.0.5" in violations[0]

    def test_auto_yes_is_violation(self) -> None:
        env = {**self._LOOPBACK, "EPICS_PVA_AUTO_ADDR_LIST": "YES"}
        violations = write_reach_violations(env)
        assert len(violations) == 1
        assert "EPICS_PVA_AUTO_ADDR_LIST" in violations[0]

    def test_name_servers_public(self) -> None:
        env = {**self._LOOPBACK, "EPICS_PVA_NAME_SERVERS": "gateway.example.org:5075"}
        violations = write_reach_violations(env)
        assert len(violations) == 1
        assert "EPICS_PVA_NAME_SERVERS" in violations[0]

    def test_ipv6_loopback_name_server_ok(self) -> None:
        env = {**self._LOOPBACK, "EPICS_PVA_NAME_SERVERS": "[::1]:5075"}
        assert write_reach_violations(env) == []
