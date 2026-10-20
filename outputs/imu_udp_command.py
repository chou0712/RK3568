#!/usr/bin/env python3
"""Send framed WTGAHRS3 control commands to the RK3568 gateway."""

from __future__ import annotations

import argparse
import json
import socket

from wtgahrs3_reader import build_protocol_frame, parse_protocol_frame


COMMAND_TYPES = {
    "status": 0x01,
    "set-angle": 0x02,
    "start": 0x03,
    "stop": 0x04,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=COMMAND_TYPES)
    parser.add_argument("--host", default="192.168.1.101")
    parser.add_argument("--port", type=int, default=20002)
    parser.add_argument("--timeout", type=float, default=3)
    parser.add_argument("--x", type=float)
    parser.add_argument("--y", type=float)
    parser.add_argument("--z", type=float)
    args = parser.parse_args()

    data = b""
    if args.command == "set-angle":
        if None in (args.x, args.y, args.z):
            parser.error("set-angle requires --x, --y, and --z")
        data = json.dumps(
            {"x": args.x, "y": args.y, "z": args.z},
            separators=(",", ":"),
        ).encode("utf-8")

    request = build_protocol_frame(COMMAND_TYPES[args.command], data)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(args.timeout)
    try:
        sock.sendto(request, (args.host, args.port))
        response, peer = sock.recvfrom(65535)
    finally:
        sock.close()

    frame = parse_protocol_frame(response)
    if frame.message_type != 0x81:
        raise RuntimeError(f"unexpected response type: 0x{frame.message_type:02X}")
    document = json.loads(frame.data)
    print(f"response_from={peer[0]}:{peer[1]} type=0x81")
    print(json.dumps(document, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError, RuntimeError) as exc:
        print(f"error: {exc}")
        raise SystemExit(1)
