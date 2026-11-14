"""
Log-server ingestion stubs: vmagent and vlagent.

The FMS pushes to two endpoints on `hist-01` (`fms.md` §5.4): ATG readings and
alarms as VictoriaMetrics import-format lines to vmagent, and job/transaction
events as JSON lines to vlagent. These stubs stand in for both so the FMS suite
runs without the log servers deployed.

They replace the earlier Ignition stub, which was residue from when the log
server was an Ignition historian. The wire formats here are the ones
`hist.md` §4.1 and §4.2 specify, and the same ones
`tests/integration/hist/stubs/fms_pusher.py` emits against the real instances.

Both servers reject any path but their own with 404. That is deliberate: a
target that accepts everything cannot distinguish the FMS pushing to the right
endpoint from it pushing to the wrong one, which is the class of defect that let
`FMS_LOG_SERVER_URL` point at a stub port unnoticed.

  vmagent   POST /api/v1/import      newline-delimited import-format JSON -> 204
  vlagent   POST /insert/jsonline    newline-delimited JSON objects       -> 200
  vlagent   TLS  <syslog port>       LF-terminated RFC 5424 messages (RFC 5425)

The syslog listener terminates TLS, as vlagent's does on hist-01 (hist.md §5
path 3). The FMS dials it by the name its certificate is issued for, and
verifies it against fms-01's system trust store (fms.md §3), so a plain TCP
listener would receive a ClientHello and nothing else. The certificate and key
come from the Makefile's `stub-tls` target (stubs/issue-stub-cert.sh).
"""

import json
import re
import socketserver
import ssl
import threading
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

VMAGENT_PATH = "/api/v1/import"
VLAGENT_PATH = "/insert/jsonline"
# hist.md §4.3: facility user(1), structured-data identifier fms@99999.
SD_ID = "fms@99999"


@dataclass
class Sample:
    """One value of one metric at one timestamp (hist.md §4.1)."""

    name: str
    labels: dict[str, str]
    value: float
    timestamp_ms: int
    received_at: datetime = field(default_factory=datetime.now)


@dataclass
class Record:
    """One structured domain event (hist.md §4.2), flat as it arrives."""

    event_type: str
    fields: dict[str, Any]
    received_at: datetime = field(default_factory=datetime.now)


@dataclass
class SyslogMessage:
    """One RFC 5424 message (hist.md §4.3), decoded into its fields."""

    pri: int
    msgid: str
    hostname: str
    app_name: str
    timestamp: str
    sd: dict[str, str]
    msg: str
    raw: str
    received_at: datetime = field(default_factory=datetime.now)

    @property
    def facility(self) -> int:
        return self.pri // 8

    @property
    def severity(self) -> int:
        return self.pri % 8

    # `event_type` lets a syslog message share _Store's filtering helpers with
    # the JSON-line records; MSGID is its equivalent identifier.
    @property
    def event_type(self) -> str:
        return self.msgid


_HEADER_RE = re.compile(
    r"^<(?P<pri>\d+)>1 (?P<ts>\S+) (?P<host>\S+) (?P<app>\S+) (?P<procid>\S+) (?P<msgid>\S+) "
)


def _split_structured_data(rest: str) -> tuple[str, str]:
    """Split `[sd-element] MSG` at the element's real closing bracket.

    A `]` inside a PARAM-VALUE is backslash-escaped (RFC 5424 §6.3.3), so
    scanning for the first `]` would truncate a message whose username contains
    one — which is attacker-controlled input on a failed login.
    """
    if rest.startswith("-"):
        return "", rest[1:].lstrip()
    if not rest.startswith("["):
        return "", rest
    i, escaped = 1, False
    while i < len(rest):
        c = rest[i]
        if escaped:
            escaped = False
        elif c == "\\":
            escaped = True
        elif c == "]":
            return rest[1:i], rest[i + 1 :].lstrip()
        i += 1
    raise ValueError(f"unterminated structured data: {rest!r}")


_PARAM_RE = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')


def _unescape(value: str) -> str:
    return re.sub(r"\\(.)", r"\1", value)


def parse_syslog(line: str) -> SyslogMessage:
    m = _HEADER_RE.match(line)
    if not m:
        raise ValueError(f"not an RFC 5424 message: {line!r}")
    sd_text, msg = _split_structured_data(line[m.end() :])
    params = {k: _unescape(v) for k, v in _PARAM_RE.findall(sd_text)}
    return SyslogMessage(
        pri=int(m.group("pri")),
        timestamp=m.group("ts"),
        hostname=m.group("host"),
        app_name=m.group("app"),
        msgid=m.group("msgid"),
        sd=params,
        msg=msg,
        raw=line,
    )


class _Store:
    """Thread-safe append-and-wait list, shared by all three stubs."""

    def __init__(self) -> None:
        self._items: list[Any] = []
        self._lock = threading.Lock()
        self._added = threading.Condition(self._lock)

    def append(self, item: Any) -> None:
        with self._added:
            self._items.append(item)
            self._added.notify_all()

    def all(
        self,
        match: Callable[[Any], bool] | None = None,
        event_type: str | None = None,
    ) -> list[Any]:
        """Every item, or those matching `match` and/or a given `event_type`."""
        with self._lock:
            items = list(self._items)
        if event_type is not None:
            items = [i for i in items if getattr(i, "event_type", None) == event_type]
        if match is not None:
            items = [i for i in items if match(i)]
        return items

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def wait_for(
        self, predicate: Callable[[list[Any]], bool], timeout: float = 10.0
    ) -> bool:
        deadline = time.monotonic() + timeout
        with self._added:
            while not predicate(self._items):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._added.wait(timeout=remaining)
            return True

    def wait_for_count(
        self, match: Callable[[Any], bool], count: int = 1, timeout: float = 10.0
    ) -> bool:
        return self.wait_for(
            lambda items: sum(1 for i in items if match(i)) >= count, timeout=timeout
        )

    # Convenience wrappers for the two item types.

    def wait_for_metric(self, name: str, count: int = 1, timeout: float = 10.0) -> bool:
        return self.wait_for_count(lambda s: s.name == name, count, timeout)

    def wait_for_type(
        self, event_type: str, count: int = 1, timeout: float = 10.0
    ) -> bool:
        return self.wait_for_count(lambda r: r.event_type == event_type, count, timeout)

    def of_type(self, event_type: str) -> list[Record]:
        return self.all(lambda r: r.event_type == event_type)

    def named(self, name: str) -> list[Sample]:
        return self.all(lambda s: s.name == name)

    def wait_for_msgid(self, msgid: str, count: int = 1, timeout: float = 10.0) -> bool:
        return self.wait_for_count(lambda m: m.msgid == msgid, count, timeout)

    def with_msgid(self, msgid: str) -> list[SyslogMessage]:
        return self.all(lambda m: m.msgid == msgid)


def _parse_import_lines(body: str) -> list[Sample]:
    """Decode VictoriaMetrics import format into one Sample per value.

    Each line is {"metric": {"__name__": …, <labels>}, "values": [...],
    "timestamps": [...]}. A malformed line raises, which surfaces in the stub's
    404/500 rather than being silently discarded — an unparseable body means the
    FMS is not emitting what hist.md §4.1 specifies.
    """
    samples: list[Sample] = []
    for line in body.splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        metric = dict(obj["metric"])
        name = metric.pop("__name__")
        values = obj["values"]
        timestamps = obj["timestamps"]
        for value, ts in zip(values, timestamps):
            samples.append(
                Sample(
                    name=name, labels=metric, value=float(value), timestamp_ms=int(ts)
                )
            )
    return samples


def _parse_jsonlines(body: str) -> list[Record]:
    records: list[Record] = []
    for line in body.splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        records.append(Record(event_type=obj.get("event_type", ""), fields=obj))
    return records


def _handler_for(path: str, store: _Store, parse, status: int):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:  # suppress access log
            pass

        def do_POST(self) -> None:
            if self.path != path:
                self.send_response(404)
                self.end_headers()
                return
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode()
            try:
                items = parse(body)
            except (ValueError, KeyError, TypeError):
                # Malformed body: refuse it. The FMS retries, and the test fails
                # on the wait rather than passing with nothing recorded.
                self.send_response(400)
                self.end_headers()
                return
            for item in items:
                store.append(item)
            self.send_response(status)
            self.end_headers()

    return Handler


def _syslog_handler_for(store: _Store):
    class Handler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            # The handshake runs here rather than in the accept loop, so a
            # client that stalls or fails it holds up only its own thread.
            try:
                self.request.do_handshake()
            except (ssl.SSLError, OSError):
                return
            # One persistent connection carrying LF-terminated messages
            # (hist.md §5 path 3): read until the FMS closes it.
            for raw in self.rfile:
                line = raw.decode(errors="replace").rstrip("\n")
                if not line:
                    continue
                try:
                    store.append(parse_syslog(line))
                except ValueError:
                    # Keep the connection open: a malformed message is a test
                    # failure to be asserted on, not a reason to disconnect and
                    # make the FMS reconnect in a loop.
                    pass

    return Handler


class _SyslogServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, handler, context: ssl.SSLContext) -> None:
        self._context = context
        super().__init__(address, handler)

    def get_request(self):
        sock, addr = super().get_request()
        return (
            self._context.wrap_socket(
                sock, server_side=True, do_handshake_on_connect=False
            ),
            addr,
        )


class LogServerStub:
    """vmagent and vlagent stand-ins, each on its own port."""

    def __init__(
        self,
        host: str,
        vmagent_port: int,
        vlagent_port: int,
        syslog_port: int,
        syslog_cert: str,
        syslog_key: str,
    ) -> None:
        self._host = host
        self._vmagent_port = vmagent_port
        self._vlagent_port = vlagent_port
        self._syslog_port = syslog_port
        self._syslog_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        self._syslog_context.load_cert_chain(syslog_cert, syslog_key)
        self.metrics = _Store()
        self.events = _Store()
        self.syslog = _Store()
        self._servers: list[object] = []
        self._threads: list[threading.Thread] = []

    def clear(self) -> None:
        self.metrics.clear()
        self.events.clear()
        self.syslog.clear()

    # Kept under the old name so the shared conftest fixtures read the same.
    def clear_events(self) -> None:
        self.clear()

    def start(self) -> None:
        specs = [
            (self._vmagent_port, VMAGENT_PATH, self.metrics, _parse_import_lines, 204),
            (self._vlagent_port, VLAGENT_PATH, self.events, _parse_jsonlines, 200),
        ]
        for port, path, store, parse, status in specs:
            server = ThreadingHTTPServer(
                (self._host, port), _handler_for(path, store, parse, status)
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self._servers.append(server)
            self._threads.append(thread)

        syslog_server = _SyslogServer(
            (self._host, self._syslog_port),
            _syslog_handler_for(self.syslog),
            self._syslog_context,
        )
        syslog_thread = threading.Thread(
            target=syslog_server.serve_forever, daemon=True
        )
        syslog_thread.start()
        self._servers.append(syslog_server)
        self._threads.append(syslog_thread)

    def stop(self) -> None:
        for server in self._servers:
            server.shutdown()
            server.server_close()
        for thread in self._threads:
            thread.join(timeout=5)
        self._servers.clear()
        self._threads.clear()

    def __enter__(self) -> "LogServerStub":
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()


def log_server_stub_fixture(
    host: str,
    vmagent_port: int,
    vlagent_port: int,
    syslog_port: int,
    syslog_cert: str,
    syslog_key: str,
) -> Generator[LogServerStub, None, None]:
    stub = LogServerStub(
        host, vmagent_port, vlagent_port, syslog_port, syslog_cert, syslog_key
    )
    stub.start()
    yield stub
    stub.stop()
