"""HZZT-2-3kg native AA protocol used through the RK3568 UDP gateway."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .config import (
    AZIMUTH_MAX_DEG,
    AZIMUTH_MIN_DEG,
    ELEVATION_MAX_DEG,
    ELEVATION_MIN_DEG,
    MAX_AZIMUTH_SPEED_DPS,
    MAX_ELEVATION_SPEED_DPS,
)

HEADER = 0xAA
DEVICE_ID = 0x01

FAULT_NAMES = {
    0x80: "零位异常",
    0x40: "存储器故障",
    0x20: "俯仰末端编码器通信中断",
    0x10: "方位末端编码器通信中断",
    0x08: "俯仰驱动器连接中断",
    0x04: "方位驱动器连接中断",
    0x02: "俯仰驱动器故障",
    0x01: "方位驱动器故障",
}


class ProtocolError(ValueError):
    pass


def checksum_sum8(data: bytes) -> int:
    return sum(data) & 0xFF


def _with_checksum(body: bytes) -> bytes:
    return body + bytes((checksum_sum8(body),))


def _scaled_u16(value: float, minimum: float, maximum: float, name: str) -> bytes:
    value = float(value)
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{name}必须在 {minimum:g}～{maximum:g} 之间")
    return round(value * 10.0).to_bytes(2, "big", signed=False)


def build_status_query() -> bytes:
    return _with_checksum(bytes.fromhex("AA 06 01 02 01"))


def build_fault_query() -> bytes:
    return _with_checksum(bytes.fromhex("AA 06 01 04 01"))


def build_stop_command() -> bytes:
    return _with_checksum(bytes.fromhex("AA 0C 01 06 04 01 00 00 00 00 00"))


def build_speed_command(
    azimuth_speed_dps: float, elevation_speed_dps: float
) -> bytes:
    """Build the 11-byte speed-setting frame accepted by the RK3568 gateway."""
    body = bytes.fromhex("AA 0B 01 06 07 01")
    body += _scaled_u16(
        azimuth_speed_dps, 0.0, MAX_AZIMUTH_SPEED_DPS, "方位转速"
    )
    body += _scaled_u16(
        elevation_speed_dps, 0.0, MAX_ELEVATION_SPEED_DPS, "俯仰转速"
    )
    return _with_checksum(body)


def build_point_command(azimuth_deg: float, elevation_deg: float) -> bytes:
    body = bytes.fromhex("AA 0C 01 06 02 01 00")
    body += _scaled_u16(
        azimuth_deg, AZIMUTH_MIN_DEG, AZIMUTH_MAX_DEG, "方位角"
    )
    body += _scaled_u16(
        elevation_deg, ELEVATION_MIN_DEG, ELEVATION_MAX_DEG, "俯仰角"
    )
    return _with_checksum(body)


@dataclass(frozen=True)
class TurntableStatus:
    azimuth_deg: float
    elevation_deg: float
    azimuth_speed_dps: float
    elevation_speed_dps: float
    fault: int
    raw: bytes

    @property
    def faults(self) -> tuple[str, ...]:
        return tuple(name for mask, name in FAULT_NAMES.items() if self.fault & mask)


def _decode_i16(high: int, low: int) -> int:
    value = (high << 8) | low
    return value - 0x10000 if value >= 0x8000 else value


def validate_frame(frame: bytes) -> bytes:
    """Validate one native AA frame carried in one UDP datagram."""
    if not 6 <= len(frame) <= 64:
        raise ProtocolError(f"帧长度无效：{len(frame)} 字节")
    if frame[0] != HEADER or frame[2] != DEVICE_ID:
        raise ProtocolError("不是设备 01 的 AA 协议帧")
    if checksum_sum8(frame[:-1]) != frame[-1]:
        raise ProtocolError("累加校验失败")

    declared = frame[1]
    command = frame[3]
    normal = declared == len(frame)
    status_exception = (
        command in (0x02, 0x07) and declared == 0x10 and len(frame) in (15, 16)
    )
    settings_exception = command == 0x05 and declared == 0x0E and len(frame) == 10
    if not (normal or status_exception or settings_exception):
        raise ProtocolError(
            f"协议长度字段为 {declared}，实际数据报为 {len(frame)} 字节"
        )
    return frame


def is_status_frame(frame: bytes) -> bool:
    return (
        len(frame) in (15, 16)
        and len(frame) > 3
        and frame[0] == HEADER
        and frame[2] == DEVICE_ID
        and frame[3] in (0x02, 0x07)
    )


def parse_status(frame: bytes) -> TurntableStatus:
    validate_frame(frame)
    if not is_status_frame(frame):
        raise ProtocolError("收到的不是状态帧")
    # 15/16 字节状态帧的故障字段固定在索引 13；16 字节变体多一个保留字节。
    return TurntableStatus(
        azimuth_deg=((frame[5] << 8) | frame[6]) / 10.0,
        elevation_deg=((frame[7] << 8) | frame[8]) / 10.0,
        azimuth_speed_dps=_decode_i16(frame[9], frame[10]) / 10.0,
        elevation_speed_dps=_decode_i16(frame[11], frame[12]) / 10.0,
        fault=frame[13],
        raw=bytes(frame),
    )


def azimuth_distance_deg(current: float, target: float) -> float:
    """Return the shortest unsigned distance between two circular azimuths."""
    return abs((float(current) - float(target) + 180.0) % 360.0 - 180.0)


def target_reached(
    status: TurntableStatus,
    target_azimuth_deg: float,
    target_elevation_deg: float,
    angle_tolerance_deg: float = 0.2,
    speed_tolerance_dps: float = 0.2,
) -> bool:
    """Whether both axes are at the target and have settled."""
    return (
        azimuth_distance_deg(status.azimuth_deg, target_azimuth_deg)
        <= angle_tolerance_deg
        and abs(status.elevation_deg - target_elevation_deg)
        <= angle_tolerance_deg
        and abs(status.azimuth_speed_dps) <= speed_tolerance_dps
        and abs(status.elevation_speed_dps) <= speed_tolerance_dps
    )
