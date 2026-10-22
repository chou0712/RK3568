from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NetworkConfig:
    gateway_host: str = "192.168.1.101"
    gateway_port: int = 20003
    local_host: str = "192.168.1.102"
    local_port: int = 20003


DEFAULT_NETWORK = NetworkConfig()

TRACK_INTERVAL_MS = 150
STATUS_QUERY_INTERVAL_MS = 500
STATUS_TIMEOUT_SECONDS = 2.0
HOMING_TIMEOUT_SECONDS = 60.0
HOMING_ANGLE_TOLERANCE_DEG = 0.2
HOMING_SPEED_TOLERANCE_DPS = 0.2

AZIMUTH_MIN_DEG = 0.0
AZIMUTH_MAX_DEG = 360.0
ELEVATION_MIN_DEG = 0.0
ELEVATION_MAX_DEG = 90.0
