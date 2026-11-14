"""
TLS-protocol (Tank Level System) frame injector for the boundary-inspection
tests (siem-tests.md §0.5, §5).

Implements the command framing of `sim/tls-protocol.md` §2 and the checksum of
§4, so that a test can construct both conforming and non-conforming frames and
send them across the real network path Suricata inspects — the one component
in this suite that cannot be substituted with `incus exec`.

A command carries no checksum (§2): `SOH [security code] <function code>
[data field]`, with no line terminator, and the testbed's security code is
always omitted. The checksum applies only to the *response* the console
returns (§3, §4); `verify_response_checksum` is provided so a test can confirm
a received response is well-formed, distinct from asserting anything about
Suricata's own behaviour, which this module never touches — Suricata's
inspection is passive (`AF_PACKET`, siem.md §5) and never blocks or alters the
underlying conversation, so a test's only observable evidence of a match is
the alert it produces, read separately from `alerts.json` (see conftest.py's
`OutputReader`).
"""

from __future__ import annotations

import socket
from dataclasses import dataclass

SOH = b"\x01"
ETX = b"\x03"
_DATA_TERMINATOR = b"&&"


def checksum(payload: bytes) -> str:
    """The four-upper-case-hex-digit checksum of `payload` (tls-protocol.md
    §4): the two's-complement, modulo-2^16, of the sum of `payload`'s bytes.

    `payload` is everything the checksum covers — SOH through the second `&`
    inclusive for a real response — but the function itself is agnostic to
    what the caller includes.
    """
    total = sum(payload) % 0x10000
    complement = (0x10000 - total) % 0x10000
    return f"{complement:04X}"


def verify_response_checksum(raw: bytes) -> bool:
    """True if `raw` is a well-formed computer-format response (§3): starts
    with SOH, carries a `&&`-delimited checksum whose value satisfies
    `(S + C) mod 0x10000 == 0` (§4), and ends with ETX."""
    if not raw.startswith(SOH) or not raw.endswith(ETX):
        return False
    marker = raw.find(_DATA_TERMINATOR)
    if marker == -1:
        return False
    covered = raw[: marker + len(_DATA_TERMINATOR)]
    checksum_field = raw[marker + len(_DATA_TERMINATOR) : -len(ETX)]
    if len(checksum_field) != 4:
        return False
    try:
        received = int(checksum_field, 16)
    except ValueError:
        return False
    total = sum(covered) % 0x10000
    return (total + received) % 0x10000 == 0


def build_command(function_code: str, data: bytes = b"") -> bytes:
    """A command frame (§2): `SOH + function_code + data`, no security code
    (disabled in the testbed) and no line terminator.

    `function_code` is the full six-character code (format letter, three-digit
    function number, two-digit device number), e.g. `"i20100"`. `data` is
    empty for every read-only report code the testbed defines; a non-empty
    value lets a test construct an adversarial frame — a set command
    carrying a value siem.md §5 says must never reach the console unflagged.
    """
    return SOH + function_code.encode("ascii") + data


@dataclass(frozen=True)
class InjectorResult:
    """The outcome of one frame send: whichever bytes were read back within
    the timeout (empty if the peer sent nothing before it elapsed), whether
    the connection was still open when the read attempt ended, and the local
    port the connection used (0 if it never connected) — the source port
    Suricata records for it, so a caller can tell which of several sends an
    alert belongs to."""

    response: bytes
    connection_open: bool
    local_port: int = 0


def send_frame(
    host: str, port: int, frame: bytes, *, read_timeout: float = 5.0
) -> InjectorResult:
    """Open a fresh TCP connection to `host:port`, send `frame`, and return
    whatever response arrives before `read_timeout`.

    Suricata's boundary inspection is passive (siem.md §5): it never drops or
    alters this conversation, so a non-conforming frame is expected to reach
    the console and receive whatever reply the console's own protocol
    handling produces (including none, or its own "unrecognised function
    code" reply) — this function does not classify that outcome, only reports
    it. What makes a frame non-conforming observable is the alert it raises,
    read separately from `alerts.json`.

    A connection reset is one such outcome, not a failure of this function:
    the relay's own source-IP whitelist (`digital.md §2.5`) rejects traffic
    from anywhere but the real external supplier, and this injector — playing
    an adversary probing from outside that address — is exactly what it is
    designed to reject. The relay completes the handshake before it resets,
    so the frame reaches the wire, and Suricata's tap (`AF_PACKET`, siem.md
    §5), only if it is sent before the reset arrives. Confirmed live that it
    sometimes is not, leaving a connection that carries no data; a caller
    that needs the frame inspected must confirm the alert and resend.
    """
    local_port = 0
    try:
        with socket.create_connection((host, port), timeout=read_timeout) as sock:
            local_port = sock.getsockname()[1]
            sock.sendall(frame)
            sock.settimeout(read_timeout)
            chunks: list[bytes] = []
            connection_open = True
            try:
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        connection_open = False
                        break
                    chunks.append(chunk)
                    # A conforming reply is a complete computer-format
                    # response (§3): SOH ... && CCCC ETX. The console may
                    # keep the connection open for the client's next command
                    # rather than closing it, so waiting for EOF here would
                    # block for the full timeout on every well-formed reply.
                    # Stop as soon as a terminating ETX has arrived; a
                    # malformed or partial response with no ETX still falls
                    # through to the timeout.
                    if ETX in b"".join(chunks):
                        break
            except TimeoutError:
                pass
            except OSError:
                connection_open = False
    except OSError:
        return InjectorResult(
            response=b"", connection_open=False, local_port=local_port
        )
    return InjectorResult(
        response=b"".join(chunks),
        connection_open=connection_open,
        local_port=local_port,
    )
