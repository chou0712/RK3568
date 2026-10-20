#!/usr/bin/env python3
"""Send safe HZZT turntable commands through the RK3568 UDP gateway."""

from __future__ import annotations

import argparse
import json
import socket
import struct
import sys
import time
from typing import Sequence


def make_frame(body: bytes) -> bytes:
    return body + bytes((sum(body) & 0xFF,))


def u16_tenths(value: float, *, minimum: float, maximum: float) -> bytes:
    if not minimum <= value <= maximum:
        raise ValueError(f"value {value} is outside {minimum}..{maximum}")
    return struct.pack(">H", round(value * 10))


def i16_tenths(value: float, *, minimum: float, maximum: float) -> bytes:
    if not minimum <= value <= maximum:
        raise ValueError(f"value {value} is outside {minimum}..{maximum}")
    return struct.pack(">h", round(value * 10))


def build_command(args: argparse.Namespace) -> bytes | None:
    if args.command == "listen":
        return None
    if args.command == "status":
        return make_frame(bytes.fromhex("AA 06 01 02 01"))
    if args.command == "driver-status":
        return make_frame(bytes.fromhex("AA 06 01 03 01"))
    if args.command == "fault":
        return make_frame(bytes.fromhex("AA 06 01 04 01"))
    if args.command == "settings":
        return make_frame(bytes.fromhex("AA 06 01 05 01"))
    if args.command == "stop":
        return make_frame(bytes.fromhex("AA 0C 01 06 04 01 00 00 00 00 00"))
    if args.command == "point":
        body = bytes.fromhex("AA 0C 01 06 02 01 00")
        return make_frame(body + u16_tenths(args.azimuth, minimum=0, maximum=360)
                          + u16_tenths(args.elevation, minimum=0, maximum=90))
    if args.command == "step":
        raise ValueError(
            "step is disabled: the device treated a documented small step as a "
            "large move toward zero during the 2026-10-16 test"
        )
    if args.command == "raw":
        return bytes.fromhex("".join(args.hex_frame.replace(",", " ").split()))
    raise AssertionError(args.command)


def signed16(frame: bytes, offset: int) -> int:
    return int.from_bytes(frame[offset:offset + 2], "big", signed=True)


def decode_frame(frame: bytes) -> dict[str, object]:
    result: dict[str, object] = {
        "raw_hex": frame.hex(" ").upper(),
        "length": len(frame),
        "checksum_ok": bool(frame) and (sum(frame[:-1]) & 0xFF) == frame[-1],
    }
    if len(frame) in (15, 16) and frame[:1] == b"\xAA" and frame[3] in (0x02, 0x07):
        result.update({
            "kind": "status",
            "azimuth_deg": int.from_bytes(frame[5:7], "big") / 10.0,
            "elevation_deg": int.from_bytes(frame[7:9], "big") / 10.0,
            "azimuth_speed_dps": signed16(frame, 9) / 10.0,
            "elevation_speed_dps": signed16(frame, 11) / 10.0,
            "fault": frame[13],
        })
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway", default="192.168.1.101")
    parser.add_argument("--gateway-port", type=int, default=20003)
    parser.add_argument("--bind", default="192.168.1.102")
    parser.add_argument("--bind-port", type=int, default=20003)
    parser.add_argument("--timeout", type=float, default=2.0)
    parser.add_argument("--count", type=int, default=1,
                        help="frames to receive; 0 listens until Ctrl+C")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("listen")
    sub.add_parser("status")
    sub.add_parser("driver-status")
    sub.add_parser("fault")
    sub.add_parser("settings")
    sub.add_parser("stop")
    point = sub.add_parser("point")
    point.add_argument("azimuth", type=float)
    point.add_argument("elevation", type=float)
    step = sub.add_parser("step")
    step.add_argument("azimuth", type=float)
    step.add_argument("elevation", type=float)
    raw = sub.add_parser("raw")
    raw.add_argument("hex_frame")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command = build_command(args)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((args.bind, args.bind_port))
    sock.settimeout(args.timeout)
    try:
        if command is not None:
            sent = sock.sendto(command, (args.gateway, args.gateway_port))
            if sent != len(command):
                raise OSError(f"short UDP send: {sent} of {len(command)} bytes")
            print(f"TX {command.hex(' ').upper()}", file=sys.stderr)
        received = 0
        while args.count == 0 or received < args.count:
            try:
                payload, peer = sock.recvfrom(4096)
            except socket.timeout:
                print("timeout waiting for turntable data", file=sys.stderr)
                return 1
            print(json.dumps({"peer": f"{peer[0]}:{peer[1]}", **decode_frame(payload)},
                             ensure_ascii=False, separators=(",", ":")))
            received += 1
    finally:
        sock.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
