import socket

import pytest


def test_offline_blocks_non_loopback(monkeypatch):
    monkeypatch.setenv("LOCKWIRE_OFFLINE", "1")
    with socket.socket() as s, pytest.raises(OSError, match="LOCKWIRE_OFFLINE"):
        s.connect(("203.0.113.1", 9))
    with pytest.raises(OSError, match="LOCKWIRE_OFFLINE"):
        socket.getaddrinfo("example.com", 80)


def test_offline_allows_loopback(monkeypatch):
    monkeypatch.setenv("LOCKWIRE_OFFLINE", "1")
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        with socket.socket() as c:
            c.connect(srv.getsockname())
        socket.getaddrinfo("localhost", 80)


def test_guard_inert_when_flag_unset(monkeypatch):
    monkeypatch.delenv("LOCKWIRE_OFFLINE", raising=False)
    with socket.socket() as s:
        s.setblocking(False)
        # Passes the guard; the TEST-NET address is never routable, so no error
        # other than a would-block / unreachable one can come from the OS.
        try:
            s.connect(("203.0.113.1", 9))
        except OSError as e:
            assert "LOCKWIRE_OFFLINE" not in str(e)
