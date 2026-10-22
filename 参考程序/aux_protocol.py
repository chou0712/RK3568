"""Auxiliary UDP packets for TLE and nine-axis initial attitude.

These packets are separate from the HZZT turntable AA protocol. They use:

    EB 90 + total length (2 bytes, big-endian) + type + data + checksum

with a one-byte byte-sum checksum.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass


FRAME_HEADER = b"\xEB\x90"
HEADER_LEN = len(FRAME_HEADER)
LENGTH_LEN = 2
TYPE_LEN = 1
CHECKSUM_LEN = 1
MIN_FRAME_LEN = HEADER_LEN + LENGTH_LEN + TYPE_LEN + CHECKSUM_LEN

TYPE_TLE = 0x11
TYPE_IMU_STATE = 0x12


class AuxProtocolError(Exception):
    pass


@dataclass
class ParsedFrame:
    frame_type: int
    data: bytes


def checksum(data: bytes) -> int:
    return sum(data) & 0xFF


def build_frame(frame_type: int, payload: bytes = b"") -> bytes:
    length = (
        HEADER_LEN + LENGTH_LEN + TYPE_LEN + len(payload) + CHECKSUM_LEN
    )
    body = (
        FRAME_HEADER
        + struct.pack(">H", length)
        + bytes((frame_type & 0xFF,))
        + payload
    )
    return body + bytes((checksum(body),))


def parse_frame(frame: bytes) -> ParsedFrame:
    if len(frame) < MIN_FRAME_LEN:
        raise AuxProtocolError(
            f"frame too short: got {len(frame)}, expected >= {MIN_FRAME_LEN}"
        )
    if frame[:HEADER_LEN] != FRAME_HEADER:
        raise AuxProtocolError(f"bad frame header: {frame[:2].hex()}")
    declared_length = struct.unpack(">H", frame[2:4])[0]
    if declared_length != len(frame):
        raise AuxProtocolError(
            f"frame length mismatch: header says {declared_length}, "
            f"actual {len(frame)}"
        )
    if frame[-1] != checksum(frame[:-1]):
        raise AuxProtocolError("checksum mismatch")
    return ParsedFrame(frame_type=frame[4], data=frame[5:-1])


def encode_tle(line1: str, line2: str) -> bytes:
    return (
        line1.strip().encode("utf-8")
        + b"\n"
        + line2.strip().encode("utf-8")
    )


def decode_tle(data: bytes) -> tuple[str, str]:
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if len(lines) < 2:
        raise AuxProtocolError(
            f"TLE payload must contain two lines, got {len(lines)}"
        )
    return lines[0].strip(), lines[1].strip()


def encode_imu(yaw_deg: float, pitch_deg: float, roll_deg: float) -> bytes:
    return struct.pack(">fff", yaw_deg, pitch_deg, roll_deg)


def decode_imu(data: bytes) -> tuple[float, float, float]:
    if len(data) != 12:
        raise AuxProtocolError(
            f"IMU payload must be 12 bytes, got {len(data)}"
        )
    return struct.unpack(">fff", data)
