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

# 上位机将两轴转速限制在低速区间，降低误操作带来的机械风险。
MAX_AZIMUTH_SPEED_DPS = 5.0
MAX_ELEVATION_SPEED_DPS = 5.0
