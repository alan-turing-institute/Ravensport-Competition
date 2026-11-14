"""The client of the QEMU Machine Protocol (QMP), against a fake QEMU on a
UNIX socket.

Run with: pytest competition/tests -q
"""

from __future__ import annotations

import threading
import time

import pytest
from living_fakes import FakeQEMU

from competition.living.qmp import QMP, QMPCommandError, QMPError


def test_the_client_enters_command_mode_and_returns_a_commands_value(short_tmp) -> None:
    server = FakeQEMU(short_tmp / "qmp.sock", status="paused")
    try:
        with QMP(server.path) as qmp:
            assert qmp.greeting["version"]["qemu"]["major"] == 11
            assert qmp.execute("query-status") == {"status": "paused", "running": False}
        assert [name for name, _ in server.commands] == [
            "qmp_capabilities",
            "query-status",
        ]
    finally:
        server.close()


def test_arguments_are_sent_as_given(short_tmp) -> None:
    server = FakeQEMU(short_tmp / "qmp.sock")
    try:
        with QMP(server.path) as qmp:
            qmp.execute("migrate-set-parameters", {"max-bandwidth": 1 << 36})
        assert server.commands[-1] == (
            "migrate-set-parameters",
            {"max-bandwidth": 1 << 36},
        )
    finally:
        server.close()


def test_events_before_a_reply_are_kept_and_skipped(short_tmp) -> None:
    server = FakeQEMU(short_tmp / "qmp.sock")
    server.events["stop"] = [
        {"event": "STOP", "timestamp": {"seconds": 1, "microseconds": 0}}
    ]
    try:
        with QMP(server.path) as qmp:
            assert qmp.execute("stop") == {}
            assert [event["event"] for event in qmp.events] == ["STOP"]
    finally:
        server.close()


def test_an_error_reply_raises_with_its_class_and_description(short_tmp) -> None:
    server = FakeQEMU(short_tmp / "qmp.sock")
    server.errors["migrate"] = {
        "class": "GenericError",
        "desc": "migration is disabled",
    }
    try:
        with QMP(server.path) as qmp, pytest.raises(QMPCommandError) as raised:
            qmp.execute("migrate", {"uri": "file:/x"})
        assert raised.value.error_class == "GenericError"
        assert "migration is disabled" in str(raised.value)
    finally:
        server.close()


def test_a_closed_connection_raises(short_tmp) -> None:
    server = FakeQEMU(short_tmp / "qmp.sock")
    with QMP(server.path) as qmp:
        assert qmp.execute("quit") == {}
        with pytest.raises(QMPError, match="closed|cannot"):
            qmp.execute("query-status")


def test_the_client_waits_for_the_socket_to_appear(short_tmp) -> None:
    path = short_tmp / "qmp.sock"
    servers: list[FakeQEMU] = []
    timer = threading.Timer(0.3, lambda: servers.append(FakeQEMU(path)))
    timer.start()
    try:
        with QMP(path, connect_timeout_s=5.0) as qmp:
            assert qmp.execute("query-status")["status"] == "running"
    finally:
        timer.join()
        for server in servers:
            server.close()


def test_no_socket_within_the_timeout_raises(short_tmp) -> None:
    started = time.monotonic()
    with pytest.raises(QMPError, match="cannot connect"):
        QMP(short_tmp / "absent.sock", connect_timeout_s=0.3)
    assert time.monotonic() - started < 3.0


def test_a_socket_path_too_long_for_unix_sockets_is_refused(short_tmp) -> None:
    with pytest.raises(QMPError, match="longer than"):
        QMP(short_tmp / ("x" * 120) / "qmp.sock", connect_timeout_s=0.1)
