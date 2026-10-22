"""Threaded UDP transport. Tk widgets are never touched from the reader thread."""

from __future__ import annotations

import os
import queue
import socket
import threading
from dataclasses import dataclass

from .config import NetworkConfig
from .protocol import ProtocolError, parse_status, validate_frame


@dataclass(frozen=True)
class TransportEvent:
    kind: str
    payload: object


class UdpTransport:
    def __init__(self, events: queue.Queue[TransportEvent]) -> None:
        self._events = events
        self._socket: socket.socket | None = None
        self._reader_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._send_lock = threading.Lock()
        self.local_address: tuple[str, int] | None = None
        self.remote_address: tuple[str, int] | None = None

    @property
    def connected(self) -> bool:
        return self._socket is not None

    def connect(self, config: NetworkConfig) -> None:
        if self.connected:
            raise RuntimeError("UDP 已连接")
        if not 1 <= config.gateway_port <= 65535:
            raise ValueError("网关端口必须在 1～65535 之间")
        if not 0 <= config.local_port <= 65535:
            raise ValueError("本机端口必须在 0～65535 之间")

        remote_host = socket.gethostbyname(config.gateway_host)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            sock.bind((config.local_host, config.local_port))
            sock.connect((remote_host, config.gateway_port))
            sock.settimeout(0.2)
            self._socket = sock
            self.local_address = sock.getsockname()
            self.remote_address = (remote_host, config.gateway_port)
            self._stop.clear()
            self._reader_thread = threading.Thread(
                target=self._reader,
                name="turntable-udp-reader",
                daemon=True,
            )
            self._reader_thread.start()
        except Exception:
            sock.close()
            self._socket = None
            raise

    def _reader(self) -> None:
        while not self._stop.is_set():
            try:
                sock = self._socket
                if sock is None:
                    return
                data = sock.recv(4096)
                self._events.put(TransportEvent("rx", bytes(data)))
                try:
                    frame = validate_frame(data)
                    if len(frame) in (15, 16) and frame[3] in (0x02, 0x07):
                        self._events.put(TransportEvent("status", parse_status(frame)))
                    else:
                        self._events.put(TransportEvent("response", frame))
                except ProtocolError as exc:
                    self._events.put(TransportEvent("protocol_error", str(exc)))
            except socket.timeout:
                continue
            except OSError as exc:
                if not self._stop.is_set():
                    self._events.put(TransportEvent("network_error", str(exc)))
                return

    def send(self, frame: bytes) -> None:
        sock = self._socket
        if sock is None:
            raise RuntimeError("请先连接 RK3568 UDP 网关")
        with self._send_lock:
            written = sock.send(frame)
        if written != len(frame):
            raise OSError(f"UDP 仅发送 {written}/{len(frame)} 字节")
        self._events.put(TransportEvent("tx", bytes(frame)))

    def close(self) -> None:
        self._stop.set()
        sock, self._socket = self._socket, None
        if sock is not None:
            sock.close()
        thread, self._reader_thread = self._reader_thread, None
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=0.6)
        self.local_address = None
        self.remote_address = None
