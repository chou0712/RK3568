#!/usr/bin/env python3
"""Receive and validate WTGAHRS3 UDP samples."""

from __future__ import annotations

import argparse
import json
import socket
import time

from wtgahrs3_reader import FRAME_TYPES, ProtocolError, parse_protocol_frame


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=20002)
    parser.add_argument("--count", type=int, default=0, help="0 means run forever")
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--type", type=lambda value: int(value, 0), default=0x82)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    received = lost = 0
    last_sequence: int | None = None
    started = time.monotonic()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((args.bind, args.port))
    sock.settimeout(args.timeout)
    try:
        while args.count == 0 or received < args.count:
            payload, peer = sock.recvfrom(4096)
            frame = parse_protocol_frame(payload)
            if frame.message_type != args.type:
                continue
            document = json.loads(frame.data)
            if document.get("schema") != "wtgahrs3.v1":
                continue
            sequence = int(document["sequence"])
            if last_sequence is not None and sequence > last_sequence + 1:
                lost += sequence - last_sequence - 1
            last_sequence = sequence
            received += 1
            if not args.quiet:
                print(
                    f"{peer[0]}:{peer[1]} type=0x{frame.message_type:02X} "
                    f"{frame.data.decode().strip()}",
                    flush=True,
                )
    finally:
        sock.close()

    elapsed = time.monotonic() - started
    print(
        f"received={received} lost={lost} elapsed={elapsed:.3f}s "
        f"rate={received / elapsed:.2f}/s"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError, ProtocolError) as exc:
        print(f"error: {exc}")
        raise SystemExit(1)
