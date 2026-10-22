"""HZZT-2-3kg turntable protocol (AA frame, checksum-sum8).

Frame layout:
    AA | length (1 byte) | device id (0x01) | command | params | checksum

The length field normally means total frame length. The status report has a
known length inconsistency in the vendor document, so 15- and 16-byte status
frames are both accepted when command code and checksum are valid.
"""

from __future__ import annotations

from dataclasses import dataclass


HEADER = 0xAA
DEVICE_ID = 0x01

CMD_RESET = 0x01
CMD_STATUS_QUERY = 0x02
CMD_DRIVER_STATUS_QUERY = 0x03
CMD_FAULT_QUERY = 0x04
CMD_SETTINGS_QUERY = 0x05
CMD_MOTION = 0x06
CMD_ZERO_SET = 0x08
REPORT_STATUS = 0x07

SUB_SWEEP = 0x01
SUB_POINT = 0x02
SUB_TRACK = 0x03
SUB_STOP = 0x04
SUB_AREA_SCAN = 0x05
SUB_STEP = 0x06
SUB_SET_SPEED = 0x07


class ProtocolError(Exception):
    """Raised when a turntable frame is malformed or fails validation."""


@dataclass
class StatusData:
    az_deg: float
    el_deg: float
    az_speed_dps: float
    el_speed_dps: float
    fault: int
    raw: bytes


def checksum_sum8(data: bytes) -> int:
    return sum(data) & 0xFF


def _make_frame(body: bytes) -> bytes:
    return body + bytes((checksum_sum8(body),))


def _u16_be(value: int) -> bytes:
    if not 0 <= value <= 0xFFFF:
        raise ValueError(f"u16 out of range: {value}")
    return value.to_bytes(2, "big", signed=False)


def _i16_be(value: int) -> bytes:
    if not -32768 <= value <= 32767:
        raise ValueError(f"i16 out of range: {value}")
    return value.to_bytes(2, "big", signed=True)


def value_to_raw(value: float) -> int:
    """Convert angle/speed in 0.1 units to the protocol integer."""
    return int(round(float(value) * 10.0))


def raw_to_value(raw: int) -> float:
    return raw / 10.0


def angle_bytes(degrees: float) -> bytes:
    return _u16_be(value_to_raw(degrees))


def speed_bytes(dps: float) -> bytes:
    return _u16_be(value_to_raw(dps))


def _decode_i16(high: int, low: int) -> int:
    raw = (high << 8) | low
    return raw - 0x10000 if raw >= 0x8000 else raw


def build_status_query() -> bytes:
    return _make_frame(bytes((HEADER, 0x06, DEVICE_ID, CMD_STATUS_QUERY, 0x01)))


def build_reset_command() -> bytes:
    return _make_frame(bytes((HEADER, 0x06, DEVICE_ID, CMD_RESET, 0x01)))


def build_driver_status_query() -> bytes:
    return _make_frame(
        bytes((HEADER, 0x06, DEVICE_ID, CMD_DRIVER_STATUS_QUERY, 0x01))
    )


def build_fault_query() -> bytes:
    return _make_frame(
        bytes((HEADER, 0x06, DEVICE_ID, CMD_FAULT_QUERY, 0x01))
    )


def build_settings_query() -> bytes:
    return _make_frame(
        bytes((HEADER, 0x06, DEVICE_ID, CMD_SETTINGS_QUERY, 0x01))
    )


def build_motion_frame(
    subcommand: int,
    az_deg: float = 0.0,
    el_deg: float = 0.0,
    direction: int = 0x00,
) -> bytes:
    body = (
        bytes(
            (
                HEADER,
                0x0C,
                DEVICE_ID,
                CMD_MOTION,
                subcommand,
                0x01,
                direction,
            )
        )
        + angle_bytes(az_deg)
        + angle_bytes(el_deg)
    )
    return _make_frame(body)


def build_point_command(az_deg: float, el_deg: float) -> bytes:
    return build_motion_frame(SUB_POINT, az_deg, el_deg)


def build_track_command(az_deg: float, el_deg: float) -> bytes:
    return build_motion_frame(SUB_TRACK, az_deg, el_deg)


def build_stop_command() -> bytes:
    return build_motion_frame(SUB_STOP, 0.0, 0.0)


def build_sweep_command(clockwise: bool = True) -> bytes:
    return build_motion_frame(
        SUB_SWEEP, 0.0, 0.0, 0x00 if clockwise else 0x01
    )


def build_set_speed_command(az_speed_dps: float, el_speed_dps: float) -> bytes:
    """Build a speed command.

    The vendor document lists the length as 0x09 but the listed fields add up
    to 11 bytes; this implementation uses 0x0B. Verify on hardware first.
    """
    body = (
        bytes(
            (
                HEADER,
                0x0B,
                DEVICE_ID,
                CMD_MOTION,
                SUB_SET_SPEED,
                0x01,
            )
        )
        + speed_bytes(az_speed_dps)
        + speed_bytes(el_speed_dps)
    )
    return _make_frame(body)


def build_step_command(az_step_deg: float, el_step_deg: float) -> bytes:
    """Build a two-axis step command (signed 16-bit, verify on hardware)."""
    body = (
        bytes(
            (
                HEADER,
                0x0B,
                DEVICE_ID,
                CMD_MOTION,
                SUB_STEP,
                0x01,
            )
        )
        + _i16_be(value_to_raw(az_step_deg))
        + _i16_be(value_to_raw(el_step_deg))
    )
    return _make_frame(body)


def build_zero_command(axis: int) -> bytes:
    if axis not in (1, 2):
        raise ValueError("axis must be 1 (AZ) or 2 (EL)")
    return _make_frame(
        bytes((HEADER, 0x07, DEVICE_ID, CMD_ZERO_SET, axis, 0x01))
    )


def parse_status_frame(frame: bytes) -> StatusData:
    if len(frame) not in (15, 16):
        raise ProtocolError(
            f"status frame length must be 15 or 16, got {len(frame)}"
        )
    if frame[0] != HEADER or frame[2] != DEVICE_ID:
        raise ProtocolError("invalid header or device id")
    if frame[3] not in (CMD_STATUS_QUERY, REPORT_STATUS):
        raise ProtocolError(
            f"not a status frame, command=0x{frame[3]:02X}"
        )
    if checksum_sum8(frame[:-1]) != frame[-1]:
        raise ProtocolError("status checksum mismatch")

    fault_index = 13 if len(frame) == 15 else 14
    return StatusData(
        az_deg=((frame[5] << 8) | frame[6]) / 10.0,
        el_deg=((frame[7] << 8) | frame[8]) / 10.0,
        az_speed_dps=_decode_i16(frame[9], frame[10]) / 10.0,
        el_speed_dps=_decode_i16(frame[11], frame[12]) / 10.0,
        fault=frame[fault_index],
        raw=bytes(frame),
    )


def is_status_frame(frame: bytes) -> bool:
    return (
        len(frame) in (15, 16)
        and len(frame) > 3
        and frame[0] == HEADER
        and frame[2] == DEVICE_ID
        and frame[3] in (CMD_STATUS_QUERY, REPORT_STATUS)
    )
