"""Configuration for the antenna upper-computer application."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NetworkConfig:
    rk3568_ip: str = "192.168.1.101"
    send_port: int = 20003
    recv_port: int = 20003
    recv_ip: str = "0.0.0.0"
    timeout: float = 1.0


DEFAULT_NETWORK = NetworkConfig()
