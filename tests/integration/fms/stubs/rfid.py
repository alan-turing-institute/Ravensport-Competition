"""
RFID card injector — TCP server mode.

Under Incus, fms-01 connects outward to its serial peer (sim-layer in
production).  The test runner substitutes for sim-layer by listening on
the same TCP ports; fms-01 dials in and the injector holds the accepted
connection open for the duration of the test session.

In VM mode fms-01 uses raw.qemu:
    -chardev socket,host=<peer>,port=9001,reconnect=2

In container mode fms-01 uses socat:
    socat PTY ... TCP:${FMS_VESSEL_RFID_HOST}:9001,forever,interval=2

Either way fms-01 is the TCP client; the test runner is the server.

Wire format: "{card_number:08d}\\r\\n"  (8-digit zero-padded decimal, CRLF)
"""

import socket
import threading
import time
from typing import Generator


class RFIDServer:
    """
    Listens on one TCP port for an inbound connection from fms-01's serial
    client, then accepts card-number injections from the test suite.

    Reconnections are handled transparently: the accept loop replaces the
    stored socket whenever fms-01 reconnects, matching the behaviour of
    fms-01's socat `forever` / raw.qemu `reconnect` options.
    """

    def __init__(self, bind_host: str, port: int) -> None:
        self._port = port
        self._lock = threading.Lock()
        self._client_sock: socket.socket | None = None
        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_sock.bind((bind_host, port))
        self._server_sock.listen(1)
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()

    def _accept_loop(self) -> None:
        while True:
            try:
                conn, _ = self._server_sock.accept()
            except OSError:
                return  # server socket closed during teardown
            with self._lock:
                if self._client_sock is not None:
                    try:
                        self._client_sock.close()
                    except OSError:
                        pass
                self._client_sock = conn

    def inject(self, card_number: int, timeout: float = 5.0) -> None:
        """Send one card swipe to the connected fms-01 serial client."""
        assert 0 <= card_number < 2**26, "card_number must fit in 26 bits"
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                sock = self._client_sock
            if sock is not None:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"fms-01 did not connect to RFID server on port {self._port} "
                    f"within {timeout:.0f} s — check FMS_VESSEL_RFID_HOST on fms-01"
                )
            time.sleep(0.1)
        payload = f"{card_number:08d}\r\n".encode("ascii")
        with self._lock:
            self._client_sock.sendall(payload)  # type: ignore[union-attr]

    def close(self) -> None:
        self._server_sock.close()
        with self._lock:
            if self._client_sock is not None:
                self._client_sock.close()


class RFIDPair:
    """Servers for the vessel reader and operator reader on fms-01."""

    def __init__(self, vessel: RFIDServer, operator: RFIDServer) -> None:
        self.vessel = vessel
        self.operator = operator


def rfid_pair_fixture(
    bind_host: str, vessel_port: int, operator_port: int
) -> Generator[RFIDPair, None, None]:
    vessel = RFIDServer(bind_host, vessel_port)
    operator_srv = RFIDServer(bind_host, operator_port)
    try:
        yield RFIDPair(vessel, operator_srv)
    finally:
        vessel.close()
        operator_srv.close()
