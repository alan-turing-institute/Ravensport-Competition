"""
Modbus TCP stub server for the dispenser appliance.

Register map (dispenser-modbus-tcp-spec.md §8 — the single source of truth):

  0x0001  R/W  Preset volume (litres × 10, integer).  Written by FMS before each dispense.
  0x0002  R/W  Pump enable (1 = run, 0 = stop).        Written by FMS to start/stop dispense.
  0x0003  R    Cumulative totaliser, high word (bits 31–16).
  0x0004  R    Cumulative totaliser, low word  (bits 15–0).  Non-resettable.
  0x0005  R    Transaction totaliser, high word (bits 31–16).
  0x0006  R    Transaction totaliser, low word  (bits 15–0).  Resets on each preset write.
  0x0007  R    Status bitfield (bit 0 = Running).

Both totalisers are 32-bit pulse counts occupying a consecutive register pair,
high word at the lower address; the FMS reads each pair together (quantity 2)
and reconstructs (high << 16) | low.  Address 0x0000 and any address ≥ 0x0008
are unmapped.

K-factor: 100 pulses per litre (configurable via K_FACTOR).

Tests drive the totalisers with advance_totaliser(litres); the stub auto-stops
(clears enable and status) when the transaction totaliser reaches the preset.

Function codes: only 0x03 (Read Holding Registers) and 0x06 (Write Single
Register) are implemented, matching the real dispenser; any other function code
returns exception 0x01 (Illegal Function).

Implementation note: this stub implements Modbus TCP directly using asyncio
rather than delegating to pymodbus's datastore layer, which routes register
access through an internal SimDevice object that bypasses custom block
subclasses.
"""

import asyncio
import struct
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Generator

PRESET_REG = 0x0001
ENABLE_REG = 0x0002
CUMULATIVE_REG = 0x0003  # high word; 0x0004 holds the low word
TRANSACTION_REG = 0x0005  # high word; 0x0006 holds the low word
STATUS_REG = 0x0007

K_FACTOR = 100  # pulses per litre

STATUS_STOPPED = 0x0000
STATUS_RUNNING = 0x0001  # bit 0 = Running

# Modbus exception codes (spec §5).
ILLEGAL_FUNCTION = 0x01
ILLEGAL_DATA_ADDRESS = 0x02
ILLEGAL_DATA_VALUE = 0x03

# Mapped registers: readable 0x0001–0x0007, writable 0x0001–0x0002 (spec §8).
_READABLE_REGS: frozenset[int] = frozenset(range(0x0001, 0x0008))
_WRITABLE_REGS: frozenset[int] = frozenset({PRESET_REG, ENABLE_REG})
_MAX_READ_QUANTITY = 125


@dataclass
class WriteRecord:
    address: int
    value: int
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class ReadRecord:
    address: int
    count: int
    timestamp: datetime = field(default_factory=datetime.now)

    def covers(self, address: int) -> bool:
        return self.address <= address < self.address + self.count


class _RegisterStore:
    """Thread-safe holding-register store with read/write recording."""

    def __init__(self) -> None:
        self._store: dict[int, int] = {addr: 0 for addr in _READABLE_REGS}
        self._writes: list[WriteRecord] = []
        self._reads: list[ReadRecord] = []
        self._lock = threading.Lock()
        self.on_write: "threading.Event | None" = None
        # Pump spin-up model: the number of status reads after an enable=1 write
        # during which the Running bit stays clear before the dispenser asserts
        # it. 0 (the default) asserts Running synchronously on the enable write.
        self._spin_up_reads = 0
        self._spin_up_remaining: "int | None" = None

    # --- raw 16-bit access -------------------------------------------------

    def read(self, address: int) -> int:
        with self._lock:
            return self._store.get(address, 0)

    def read_many(self, address: int, count: int) -> list[int]:
        with self._lock:
            self._reads.append(ReadRecord(address=address, count=count))
            values = [self._store.get(address + i, 0) for i in range(count)]
            # Spin-up: a read that covers the status register counts towards the
            # pump asserting Running. The pre-update snapshot is returned, so the
            # first status read after enable still sees the clear bit (the gap a
            # real dispenser exhibits between enable and Running).
            if (
                self._spin_up_remaining is not None
                and address <= STATUS_REG < address + count
            ):
                self._spin_up_remaining -= 1
                if self._spin_up_remaining <= 0:
                    self._store[STATUS_REG] = STATUS_RUNNING
                    self._spin_up_remaining = None
            return values

    def set_quiet(self, address: int, value: int) -> None:
        """Update a register without logging the access or signalling on_write."""
        with self._lock:
            self._store[address] = value & 0xFFFF

    def set_spin_up_reads(self, n: int) -> None:
        """Configure how many status reads after enable=1 precede Running."""
        with self._lock:
            self._spin_up_reads = n
            self._spin_up_remaining = None

    # --- 32-bit totaliser pair access (high word at the lower address) -----

    def get_u32(self, hi_address: int) -> int:
        with self._lock:
            return (self._store[hi_address] << 16) | self._store[hi_address + 1]

    def set_u32(self, hi_address: int, value: int) -> None:
        with self._lock:
            self._store[hi_address] = (value >> 16) & 0xFFFF
            self._store[hi_address + 1] = value & 0xFFFF

    # --- client write (records, may reset transaction totaliser) -----------

    def client_write(self, address: int, value: int) -> None:
        """Apply and record a Write Single Register from the FMS."""
        with self._lock:
            self._store[address] = value & 0xFFFF
            self._writes.append(WriteRecord(address=address, value=value))
            # A new preset starts a transaction: reset the transaction totaliser
            # (spec §9).  Cumulative is never cleared.
            if address == PRESET_REG:
                self._store[TRANSACTION_REG] = 0
                self._store[TRANSACTION_REG + 1] = 0
            # Enable drives the Running status bit (spec §7/§9). With a spin-up
            # configured, Running asserts only after the FMS has polled status
            # _spin_up_reads times, modelling the pump taking time to start.
            if address == ENABLE_REG:
                if value:
                    if self._spin_up_reads > 0:
                        self._store[STATUS_REG] = STATUS_STOPPED
                        self._spin_up_remaining = self._spin_up_reads
                    else:
                        self._store[STATUS_REG] = STATUS_RUNNING
                else:
                    self._store[STATUS_REG] = STATUS_STOPPED
                    self._spin_up_remaining = None
        if self.on_write is not None:
            self.on_write.set()

    # --- recording accessors ----------------------------------------------

    def get_writes(self) -> list[WriteRecord]:
        with self._lock:
            return list(self._writes)

    def get_reads(self) -> list[ReadRecord]:
        with self._lock:
            return list(self._reads)

    def clear_log(self) -> None:
        with self._lock:
            self._writes.clear()
            self._reads.clear()


class _ModbusTCPProtocol(asyncio.Protocol):
    """
    Asyncio protocol instance for one Modbus TCP connection.

    Handles FC3 (read holding registers) and FC6 (write single register), the
    only function codes the real dispenser implements.  Every other function
    code returns exception 0x01 (Illegal Function).
    """

    def __init__(self, store: _RegisterStore) -> None:
        self._store = store
        self._buf = b""
        self.transport: asyncio.BaseTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport

    def data_received(self, data: bytes) -> None:
        self._buf += data
        # Modbus TCP MBAP header is 6 bytes; bytes 4-5 give the remaining length.
        while len(self._buf) >= 6:
            remaining = struct.unpack(">H", self._buf[4:6])[0]
            total = 6 + remaining
            if len(self._buf) < total:
                break
            frame = self._buf[:total]
            self._buf = self._buf[total:]
            response = self._handle(frame)
            if response and self.transport is not None:
                self.transport.write(response)

    def _handle(self, frame: bytes) -> bytes:
        trans_id = frame[0:2]
        unit_id = frame[6:7]
        fc = frame[7]
        pdu_data = frame[8:]
        response_pdu = self._dispatch(fc, pdu_data)
        # MBAP length field counts unit_id (1) + response PDU bytes.
        resp_length = 1 + len(response_pdu)
        header = trans_id + b"\x00\x00" + struct.pack(">H", resp_length) + unit_id
        return header + response_pdu

    @staticmethod
    def _exception(fc: int, code: int) -> bytes:
        return bytes([fc | 0x80, code])

    def _dispatch(self, fc: int, pdu: bytes) -> bytes:
        if fc == 3:  # Read Holding Registers
            if len(pdu) < 4:
                return self._exception(fc, ILLEGAL_DATA_VALUE)
            start_addr, quantity = struct.unpack(">HH", pdu[0:4])
            if not (1 <= quantity <= _MAX_READ_QUANTITY):
                return self._exception(fc, ILLEGAL_DATA_VALUE)
            if any(start_addr + i not in _READABLE_REGS for i in range(quantity)):
                return self._exception(fc, ILLEGAL_DATA_ADDRESS)
            values = self._store.read_many(start_addr, quantity)
            byte_count = quantity * 2
            return bytes([fc, byte_count]) + struct.pack(f">{quantity}H", *values)

        if fc == 6:  # Write Single Register
            if len(pdu) < 4:
                return self._exception(fc, ILLEGAL_DATA_VALUE)
            addr, value = struct.unpack(">HH", pdu[0:4])
            if addr not in _WRITABLE_REGS:
                return self._exception(fc, ILLEGAL_DATA_ADDRESS)
            self._store.client_write(addr, value)
            return bytes([fc]) + pdu[0:4]  # echo

        return self._exception(fc, ILLEGAL_FUNCTION)


class ModbusStub:
    """
    Stateful Modbus TCP stub for the dispenser.  Runs a minimal asyncio TCP
    server in a background thread.  Tests control dispenser state through the
    control API; register reads and writes from the FMS are recorded and
    retrievable.
    """

    def __init__(self, host: str, port: int, auto_stop: bool = True) -> None:
        self._host = host
        self._port = port
        self.auto_stop = auto_stop
        self._store = _RegisterStore()
        self._write_event = threading.Event()
        self._store.on_write = self._write_event
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server_thread: threading.Thread | None = None
        self._asyncio_server: asyncio.AbstractServer | None = None

    # --- Control API ---

    def reset(self) -> None:
        """Reset all registers to zero and clear the access log.  Call between tests."""
        self._store.clear_log()
        self._write_event.clear()
        for addr in _READABLE_REGS:
            self._store.set_quiet(addr, 0)
        self._store.set_spin_up_reads(0)

    def set_spin_up_reads(self, n: int) -> None:
        """Model pump spin-up: Running asserts only after the FMS has polled the
        status register `n` times following an enable=1 write. The default of 0
        asserts Running synchronously on the enable write."""
        self._store.set_spin_up_reads(n)

    def advance_totaliser(self, litres: float) -> None:
        """Advance the transaction and cumulative totalisers by litres (32-bit)."""
        pulses = int(litres * K_FACTOR)
        self._store.set_u32(
            TRANSACTION_REG, self._store.get_u32(TRANSACTION_REG) + pulses
        )
        self._store.set_u32(
            CUMULATIVE_REG, self._store.get_u32(CUMULATIVE_REG) + pulses
        )
        self._check_auto_stop()

    def complete_transaction(self) -> None:
        """Advance the totalisers to the preset the FMS wrote, so the
        transaction reaches its auto-stop target as a dispense left to run
        does. Counted in pulses, so the target is met exactly."""
        preset_pulses = (self._store.read(PRESET_REG) * K_FACTOR) // 10
        remaining = preset_pulses - self._store.get_u32(TRANSACTION_REG)
        if remaining > 0:
            self._store.set_u32(
                TRANSACTION_REG, self._store.get_u32(TRANSACTION_REG) + remaining
            )
            self._store.set_u32(
                CUMULATIVE_REG, self._store.get_u32(CUMULATIVE_REG) + remaining
            )
        self._check_auto_stop()

    @property
    def received_writes(self) -> list[WriteRecord]:
        return self._store.get_writes()

    @property
    def received_reads(self) -> list[ReadRecord]:
        return self._store.get_reads()

    def writes_to(self, address: int) -> list[WriteRecord]:
        return [w for w in self.received_writes if w.address == address]

    def read_covering(self, address: int) -> list[ReadRecord]:
        """Reads whose register range includes the given address."""
        return [r for r in self.received_reads if r.covers(address)]

    def did_read(self, address: int) -> bool:
        """Whether the FMS has read a range covering the given address."""
        return bool(self.read_covering(address))

    def wait_for_write(self, timeout: float = 5.0) -> bool:
        """Block until the FMS writes any register, or timeout expires."""
        fired = self._write_event.wait(timeout=timeout)
        self._write_event.clear()
        return fired

    # --- Lifecycle ---

    def start(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._server_thread = threading.Thread(
            target=self._loop.run_forever, daemon=True
        )
        self._server_thread.start()
        asyncio.run_coroutine_threadsafe(self._start_server(), self._loop).result(
            timeout=5
        )

    def stop(self) -> None:
        if self._asyncio_server is not None and self._loop is not None:
            asyncio.run_coroutine_threadsafe(self._stop_server(), self._loop).result(
                timeout=5
            )
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._server_thread is not None:
            self._server_thread.join(timeout=5)

    def __enter__(self) -> "ModbusStub":
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()

    # --- Internal ---

    async def _start_server(self) -> None:
        assert self._loop is not None
        store = self._store
        self._asyncio_server = await self._loop.create_server(
            lambda: _ModbusTCPProtocol(store),
            self._host,
            self._port,
        )

    async def _stop_server(self) -> None:
        if self._asyncio_server is not None:
            self._asyncio_server.close()
            await self._asyncio_server.wait_closed()

    def _get_reg(self, address: int) -> int:
        return self._store.read(address)

    def _check_auto_stop(self) -> None:
        if not self.auto_stop:
            return
        preset_raw = self._store.read(PRESET_REG)
        if preset_raw == 0:
            return
        # Preset is litres × 10; auto-stop target is preset litres × K-factor.
        preset_pulses = (preset_raw * K_FACTOR) // 10
        if self._store.get_u32(TRANSACTION_REG) >= preset_pulses:
            self._store.set_quiet(ENABLE_REG, 0)
            self._store.set_quiet(STATUS_REG, STATUS_STOPPED)


def modbus_stub_fixture(host: str, port: int) -> Generator[ModbusStub, None, None]:
    stub = ModbusStub(host, port)
    stub.start()
    yield stub
    stub.stop()
