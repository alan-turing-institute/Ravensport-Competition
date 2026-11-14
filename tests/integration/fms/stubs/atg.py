"""
Veeder-Root Tank Level System (TLS) serial-over-TCP stub server.

Wire format per specs/host_specs/sim/tls-protocol.md (computer format). This
stub must emit bytes a real TLS console would, so that the FMS ATG poller
(fms/src/background/atg.rs) parses them unchanged.

  Command  (client → server), §2:
    SOH[0x01] | function_code[6 ASCII]
    No security code, no data field, no terminator. The six-character code is a
    format letter (lower-case 'i' = computer format), a three-digit function
    number, and a two-digit device number TT ("00" = all devices).
      \x01i10100  System Status Report,    all devices
      \x01i20100  In-Tank Inventory Report, all tanks
      \x01i20500  In-Tank Status Report,    all tanks

  Response (server → client), §3:
    SOH[0x01] | function_code[6 ASCII echo] | timestamp[10 ASCII, YYMMDDHHmm]
      | data | "&&" | checksum[4 hex ASCII] | ETX[0x03]

  Checksum (§4): the two's complement of the modulo-2^16 sum of every byte
  preceding the checksum field — SOH, the echoed code, the timestamp, the body,
  and both '&' characters — emitted as four upper-case hex digits. A valid frame
  satisfies (sum + checksum) mod 2^16 == 0.

  Floats (§5): IEEE-754 single-precision, big-endian, as eight upper-case
  ASCII-hex nibbles.

Supported function codes and response data layouts:

  i101 — System Status Report (§6.1)
    Zero or more 6-char AANNTT entries. The testbed console emits exactly one:
    "000000" (All Functions Normal) normally, or "030301" (bund liquid-sensor
    fuel alarm) when the bund alarm is latched.

  i201 — In-Tank Inventory Report (§6.2)
    Per-tank record: TT[2] p[1] ssss[4] NN[2] then NN=07 eight-char floats —
    Volume, TC Volume, Ullage, Height, Water (height), Temperature, Water
    Volume. The FMS reconciles on field 2 (TC Volume).

  i205 — In-Tank Status Report (§6.3)
    Per-tank record: TT[2] nn[2] then nn alarm-type numbers. The testbed models
    no tank alarms, so nn is always "00".
"""

import asyncio
import struct
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Generator


SOH = 0x01
ETX = 0x03

TANK_CAPACITY_L: float = 50_000.0
# Litres per millimetre of fuel height (matches infra/sim atg_console geometry).
K_L_PER_MM: float = 7.0
# Single tank, two ASCII decimal digits; distillate product code.
TANK_NUMBER: str = "01"
PRODUCT_CODE: str = "D"

# i101 bodies (§6.1, §8).
ALL_NORMAL_ENTRY: str = "000000"
BUND_ALARM_ENTRY: str = "030301"


@dataclass
class ATGState:
    volume_L: float = 25_000.0
    temperature_C: float = 15.0
    # Whether the bund liquid-sensor fuel alarm (i101 entry 030301) is latched.
    bund_alarm: bool = False


@dataclass
class ReceivedRequest:
    # The six-character wire function code, e.g. "i20100".
    function_code: str
    timestamp: datetime = field(default_factory=datetime.now)


def _encode_float(value: float) -> str:
    """One IEEE-754 single-precision float as eight upper-case big-endian hex chars."""
    return struct.pack(">f", value).hex().upper()


class ATGStub:
    """
    Stateful Veeder-Root TLS stub.  Runs a TCP server in a background thread.
    Tests drive tank state via set_state() and inspect received requests via
    received_requests.
    """

    def __init__(self, host: str, port: int) -> None:
        self._host = host
        self._port = port
        self._state = ATGState()
        # A Condition, not a plain Lock: wait_for_request() below needs to
        # block until a matching request arrives rather than poll, the same
        # way LogServerStub's wait_for() does. A Condition is a drop-in
        # replacement everywhere the existing `with self._lock:` blocks
        # already use it as a plain mutex.
        self._lock = threading.Condition()
        self._received: list[ReceivedRequest] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: asyncio.AbstractServer | None = None
        self._thread: threading.Thread | None = None

    # --- Control API ---

    def set_state(
        self,
        volume_L: float | None = None,
        temperature_C: float | None = None,
        bund_alarm: bool | None = None,
    ) -> None:
        with self._lock:
            if volume_L is not None:
                self._state.volume_L = volume_L
            if temperature_C is not None:
                self._state.temperature_C = temperature_C
            if bund_alarm is not None:
                self._state.bund_alarm = bund_alarm

    @property
    def received_requests(self) -> list[ReceivedRequest]:
        with self._lock:
            return list(self._received)

    def clear_requests(self) -> None:
        with self._lock:
            self._received.clear()

    def wait_for_request(self, function_code: str, timeout: float = 5.0) -> bool:
        """Block until a request carrying `function_code` has been recorded,
        or `timeout` elapses. Use this rather than a point-in-time check of
        `received_requests` whenever a request may still be in flight: the
        FMS's response to an earlier poll cycle (and any metric it produces)
        can arrive after this test's own clear_requests(), while the request
        that caused it landed just before — a request recorded a moment from
        now still satisfies "the FMS did poll", which a single synchronous
        check at call time would miss."""
        deadline = time.monotonic() + timeout
        with self._lock:
            while not any(r.function_code == function_code for r in self._received):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._lock.wait(timeout=remaining)
            return True

    # --- Lifecycle ---

    def start(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        asyncio.run_coroutine_threadsafe(self._start_server(), self._loop).result(
            timeout=5
        )

    def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            asyncio.run_coroutine_threadsafe(
                self._server.wait_closed(),
                self._loop,  # type: ignore[arg-type]
            ).result(timeout=5)
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5)

    def __enter__(self) -> "ATGStub":
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()

    # --- Server implementation ---

    async def _start_server(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_connection, self._host, self._port
        )

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        # Connections are persistent: a real FMS polls repeatedly on one socket.
        # Each command is SOH followed by exactly six code characters, with no
        # terminator, so read the fixed-length code rather than buffering to a
        # delimiter (tls-protocol.md §2).
        try:
            while True:
                first = await reader.readexactly(1)
                if first[0] != SOH:
                    continue  # resynchronise on the next SOH
                code = await reader.readexactly(6)
                func_code = code.decode("ascii", errors="replace")
                with self._lock:
                    self._received.append(ReceivedRequest(func_code))
                    self._lock.notify_all()
                    state = ATGState(
                        volume_L=self._state.volume_L,
                        temperature_C=self._state.temperature_C,
                        bund_alarm=self._state.bund_alarm,
                    )
                writer.write(self._build_response(func_code, state))
                await writer.drain()
        except (
            asyncio.IncompleteReadError,
            asyncio.CancelledError,
            ConnectionResetError,
        ):
            pass
        finally:
            writer.close()

    def _build_response(self, func_code: str, state: ATGState) -> bytes:
        body = self._encode_body(func_code, state)
        if body is None:
            # Unrecognised function code: the console's fixed error frame, where
            # FF1B is the checksum of "9999" (tls-protocol.md §2).
            return b"\x019999FF1B\x03"

        timestamp = datetime.now().strftime("%y%m%d%H%M")  # YYMMDDHHmm
        message = bytearray([SOH])
        message.extend(func_code.encode("ascii"))
        message.extend(timestamp.encode("ascii"))
        message.extend(body.encode("ascii"))
        message.extend(b"&&")
        checksum = (0x10000 - (sum(message) & 0xFFFF)) & 0xFFFF
        message.extend(f"{checksum:04X}".encode("ascii"))
        message.append(ETX)
        return bytes(message)

    def _encode_body(self, func_code: str, state: ATGState) -> str | None:
        # Dispatch on the three-digit function number (chars 1..4 of the code).
        function = func_code[1:4]
        if function == "101":
            return BUND_ALARM_ENTRY if state.bund_alarm else ALL_NORMAL_ENTRY
        if function == "201":
            volume = state.volume_L
            fields = [
                volume,  # 1 Volume
                volume,  # 2 TC Volume (identity at 15 °C)
                TANK_CAPACITY_L - volume,  # 3 Ullage
                volume / K_L_PER_MM,  # 4 Height (mm)
                0.0,  # 5 Water (height) — no water sensor
                state.temperature_C,  # 6 Temperature
                0.0,  # 7 Water Volume
            ]
            body = f"{TANK_NUMBER}{PRODUCT_CODE}000007"  # p, ssss=0000, NN=07
            return body + "".join(_encode_float(f) for f in fields)
        if function == "205":
            return f"{TANK_NUMBER}00"  # no active tank alarms
        return None


def atg_stub_fixture(host: str, port: int) -> Generator[ATGStub, None, None]:
    stub = ATGStub(host, port)
    stub.start()
    yield stub
    stub.stop()
