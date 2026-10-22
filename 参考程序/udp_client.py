"""UDP transport for HZZT turntable AA frames."""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass, field

from config import DEFAULT_NETWORK, NetworkConfig
from protocol import (
    StatusData,
    build_point_command,
    build_set_speed_command,
    build_status_query,
    build_stop_command,
    build_track_command,
    parse_status_frame,
)


@dataclass
class DeviceStatus:
    az_deg: float = 0.0
    el_deg: float = 0.0
    az_speed_dps: float = 0.0
    el_speed_dps: float = 0.0
    fault: int = 0
    raw: bytes = b""
    timestamp: float = field(default_factory=time.monotonic)


class RK3568UDPClient:
    """Send AA command frames and receive AA status frames over UDP."""

    def __init__(
        self,
        config: NetworkConfig = DEFAULT_NETWORK,
        on_send=None,
        on_recv=None,
    ) -> None:
        self.config = config
        self._target = (config.rk3568_ip, config.send_port)
        self._on_send = on_send
        self._on_recv = on_recv
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.bind((config.recv_ip, config.recv_port))
        self._socket.settimeout(config.timeout)

    def send_target(self, az_deg: float, el_deg: float) -> None:
        self._send(build_point_command(az_deg, el_deg))

    def send_start_tracking(self, az_deg: float, el_deg: float) -> None:
        self._send(build_track_command(az_deg, el_deg))

    def send_stop(self) -> None:
        self._send(build_stop_command())

    def send_query(self) -> None:
        self._send(build_status_query())

    def send_set_speed(self, az_speed_dps: float, el_speed_dps: float) -> None:
        self._send(build_set_speed_command(az_speed_dps, el_speed_dps))

    def recv_raw(self) -> bytes:
        data, _addr = self._socket.recvfrom(2048)
        if self._on_recv is not None:
            self._on_recv(data)
        return data

    def recv_status(self) -> DeviceStatus:
        data = self.recv_raw()
        parsed: StatusData = parse_status_frame(data)
        return DeviceStatus(
            az_deg=parsed.az_deg,
            el_deg=parsed.el_deg,
            az_speed_dps=parsed.az_speed_dps,
            el_speed_dps=parsed.el_speed_dps,
            fault=parsed.fault,
            raw=parsed.raw,
        )

    def close(self) -> None:
        self._socket.close()

    def _send(self, frame: bytes) -> None:
        if self._on_send is not None:
            self._on_send(frame)
        self._socket.sendto(frame, self._target)
