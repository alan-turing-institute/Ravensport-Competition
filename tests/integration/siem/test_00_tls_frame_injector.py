"""
Wire-format tests for the TLS-protocol frame injector (tls_frame_injector.py).

These tests require no Incus infrastructure: the injector's frame builder and
checksum logic are pure functions, so their byte-level output is pinned here
against `sim/tls-protocol.md` §2 and §4 before the boundary-inspection tests
(§5) trust it to construct both conforming and adversarial frames correctly.

Run locally: python -m pytest tests/integration/siem/test_00_tls_frame_injector.py
"""

from __future__ import annotations

from tests.integration.siem.tls_frame_injector import (
    ETX,
    SOH,
    build_command,
    checksum,
    verify_response_checksum,
)

# ---------------------------------------------------------------------------
# build_command — §2's three read-only report codes, no data field, no
# security code, no line terminator.
# ---------------------------------------------------------------------------


def test_system_status_report_command() -> None:
    assert build_command("i10100") == b"\x01i10100"


def test_in_tank_inventory_report_command() -> None:
    assert build_command("i20100") == b"\x01i20100"


def test_in_tank_status_report_command() -> None:
    assert build_command("i20500") == b"\x01i20500"


def test_command_carries_no_line_terminator() -> None:
    """Earlier drafts appended '\\n'; tls-protocol.md §2 corrects this — the
    function code is fixed-length, and a trailing byte would be read as the
    start of the next command."""
    frame = build_command("i20100")
    assert not frame.endswith(b"\n")
    assert len(frame) == len(SOH) + len("i20100")


def test_command_with_a_data_field() -> None:
    """A set command (§5's rejected case) may carry a value; the
    injector must not restrict data to the empty string the three benign
    codes happen to use."""
    frame = build_command("s60200", data=b"461C4000")
    assert frame == b"\x01s60200461C4000"


# ---------------------------------------------------------------------------
# checksum / verify_response_checksum — §4
# ---------------------------------------------------------------------------


def test_checksum_is_four_uppercase_hex_digits() -> None:
    value = checksum(b"\x01i20100" + b"2607011200" + b"&&")
    assert len(value) == 4
    assert value == value.upper()
    int(value, 16)  # does not raise


def test_checksum_verification_holds_for_a_constructed_response() -> None:
    """A synthetic but well-formed response: SOH + echoed function code +
    timestamp + body + '&&' + checksum + ETX. The checksum is computed by the
    module under test itself, so this is a round-trip consistency check —
    encode, then verify — rather than a claim about a specific external
    value, since tls-protocol.md gives no worked checksum example."""
    covered = SOH + b"i20100" + b"2607011200" + b"00" + b"&&"
    frame = covered + checksum(covered).encode("ascii") + ETX
    assert verify_response_checksum(frame) is True


def test_checksum_covers_soh() -> None:
    """tls-protocol.md §4: 'SOH is included'. Omitting it from the covered
    span must change the checksum, or this property is not actually being
    exercised."""
    with_soh = SOH + b"i20100" + b"&&"
    without_soh = b"i20100" + b"&&"
    assert checksum(with_soh) != checksum(without_soh)


def test_checksum_excludes_etx() -> None:
    """§4: ETX follows the checksum and is not itself covered."""
    covered = SOH + b"i20100" + b"&&"
    assert checksum(covered) == checksum(covered)  # covered never includes ETX
    frame = covered + checksum(covered).encode("ascii") + ETX
    assert verify_response_checksum(frame) is True
    # Appending a byte before ETX changes what is covered and must invalidate.
    tampered = covered + checksum(covered).encode("ascii") + b"X" + ETX
    assert verify_response_checksum(tampered) is False


def test_verify_response_checksum_rejects_a_tampered_body() -> None:
    covered = SOH + b"i20100" + b"2607011200" + b"00" + b"&&"
    frame = covered + checksum(covered).encode("ascii") + ETX
    tampered = frame.replace(b"2607011200", b"2607011201", 1)
    assert verify_response_checksum(tampered) is False


def test_verify_response_checksum_rejects_missing_terminator() -> None:
    covered = SOH + b"i20100" + b"&&"
    frame = covered + checksum(covered).encode("ascii")  # no ETX
    assert verify_response_checksum(frame) is False


def test_verify_response_checksum_rejects_no_data_terminator() -> None:
    assert verify_response_checksum(SOH + b"i20100" + b"0000" + ETX) is False
