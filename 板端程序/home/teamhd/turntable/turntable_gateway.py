#!/usr/bin/env python3
"""Bridge the HZZT turntable's RS-422 byte stream to a dedicated UDP port.

The gateway deliberately uses the turntable's native AA protocol on the wire:
one validated turntable frame is carried in one UDP datagram.  This keeps the
gateway transparent while isolating it from the existing camera relay and IMU
UDP service.
"""

from __future__ import annotations

import argparse
import logging
import os
import select
import signal
import socket
import time
from dataclasses import dataclass
from typing import Sequence

if os.name == "posix":
    import fcntl
    import termios
else:
    fcntl = None
    termios = None


HEADER = 0xAA
DEVICE_ID = 0x01
MIN_FRAME_LENGTH = 6
MAX_FRAME_LENGTH = 64


class ProtocolError(ValueError):
    """A UDP or serial payload is not a valid turntable protocol frame."""


def checksum_sum8(data: bytes) -> int:
    return sum(data) & 0xFF


def make_frame(body: bytes) -> bytes:
    if not body or body[0] != HEADER:
        raise ProtocolError("frame body must start with AA")
    return body + bytes((checksum_sum8(body),))


def frame_hex(frame: bytes) -> str:
    return frame.hex(" ").upper()


def _candidate_lengths(buffer: bytearray) -> list[int]:
    """Return plausible lengths, including documented firmware exceptions."""
    if len(buffer) < 4:
        return []
    declared = buffer[1]
    command = buffer[3]
    candidates: list[int] = []
    if command in (0x02, 0x07) and declared == 0x10:
        candidates.extend((15, 16))
    elif command == 0x05 and declared == 0x0E:
        candidates.extend((10, 14))
    else:
        candidates.append(declared)
    return [n for n in dict.fromkeys(candidates) if MIN_FRAME_LENGTH <= n <= MAX_FRAME_LENGTH]


class FrameStreamParser:
    """Recover checksum-valid AA frames from an arbitrary serial byte stream."""

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.discarded_bytes = 0
        self.checksum_errors = 0

    def feed(self, data: bytes) -> list[bytes]:
        self.buffer.extend(data)
        frames: list[bytes] = []
        while True:
            try:
                start = self.buffer.index(HEADER)
            except ValueError:
                self.discarded_bytes += len(self.buffer)
                self.buffer.clear()
                break
            if start:
                self.discarded_bytes += start
                del self.buffer[:start]
            if len(self.buffer) < 4:
                break

            candidates = _candidate_lengths(self.buffer)
            if not candidates:
                self.discarded_bytes += 1
                del self.buffer[0]
                continue

            matched = False
            for length in candidates:
                if len(self.buffer) < length:
                    continue
                candidate = bytes(self.buffer[:length])
                if checksum_sum8(candidate[:-1]) == candidate[-1]:
                    frames.append(candidate)
                    del self.buffer[:length]
                    matched = True
                    break
            if matched:
                continue
            if len(self.buffer) < max(candidates):
                break
            self.checksum_errors += 1
            del self.buffer[0]
        return frames


def validate_command_frame(frame: bytes) -> None:
    """Accept only complete, checksum-valid commands defined by the protocol."""
    if not MIN_FRAME_LENGTH <= len(frame) <= MAX_FRAME_LENGTH:
        raise ProtocolError(f"invalid frame length: {len(frame)}")
    if frame[0] != HEADER:
        raise ProtocolError("frame header is not AA")
    if frame[2] != DEVICE_ID:
        raise ProtocolError(f"device id is not 01: {frame[2]:02X}")
    if checksum_sum8(frame[:-1]) != frame[-1]:
        raise ProtocolError("checksum mismatch")

    command = frame[3]
    if command in range(0x01, 0x06):
        expected = 6
    elif command == 0x06:
        if len(frame) < 6:
            raise ProtocolError("motion frame has no subcommand")
        subcommand = frame[4]
        if subcommand == 0x06:
            raise ProtocolError(
                "step subcommand is disabled: the device treated a documented "
                "small step as a large move toward zero during the 2026-10-16 test"
            )
        expected_by_subcommand = {0x01: 12, 0x02: 12, 0x03: 12, 0x04: 12,
                                  0x05: 15, 0x07: 11}
        if subcommand not in expected_by_subcommand:
            raise ProtocolError(f"unsupported motion subcommand: {subcommand:02X}")
        expected = expected_by_subcommand[subcommand]
    elif command == 0x08:
        expected = 7
    else:
        raise ProtocolError(f"unsupported device command: {command:02X}")

    expected_lengths = (expected,) if isinstance(expected, int) else expected
    if len(frame) not in expected_lengths:
        raise ProtocolError(
            f"command {command:02X} has {len(frame)} bytes; expected {expected_lengths}"
        )
    declared_ok = frame[1] == len(frame)
    speed_length_exception = command == 0x06 and frame[4] == 0x07 and frame[1] == 0x09
    if not (declared_ok or speed_length_exception):
        raise ProtocolError(
            f"length field says {frame[1]}, datagram contains {len(frame)} bytes"
        )


def baud_constant(baud: int) -> int:
    if termios is None:
        raise RuntimeError("serial access is only available on POSIX systems")
    try:
        return {9600: termios.B9600, 19200: termios.B19200,
                38400: termios.B38400, 57600: termios.B57600,
                115200: termios.B115200, 230400: termios.B230400}[baud]
    except KeyError as exc:
        raise ValueError(f"unsupported baud rate: {baud}") from exc


class LinuxSerial:
    def __init__(self, path: str, baud: int) -> None:
        if fcntl is None or termios is None:
            raise RuntimeError("serial access is only available on POSIX systems")
        self.path = path
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            attrs = termios.tcgetattr(self.fd)
            attrs[0] = 0
            attrs[1] = 0
            attrs[2] = termios.CLOCAL | termios.CREAD | termios.CS8
            attrs[3] = 0
            speed = baud_constant(baud)
            attrs[4] = speed
            attrs[5] = speed
            attrs[6][termios.VMIN] = 0
            attrs[6][termios.VTIME] = 0
            termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
        except Exception:
            os.close(self.fd)
            raise

    def write(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            try:
                written = os.write(self.fd, view)
                view = view[written:]
            except BlockingIOError:
                select.select([], [self.fd], [], 0.5)
        termios.tcdrain(self.fd)

    def read(self) -> bytes:
        try:
            return os.read(self.fd, 4096)
        except BlockingIOError:
            return b""

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


@dataclass
class Counters:
    serial_frames: int = 0
    serial_bytes: int = 0
    udp_commands: int = 0
    udp_command_bytes: int = 0
    rejected_packets: int = 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="/dev/ttyUSB0")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--bind", default="192.168.1.101")
    parser.add_argument("--listen-port", type=int, default=20003)
    parser.add_argument("--udp-host", default="192.168.1.102")
    parser.add_argument("--udp-port", type=int, default=20003)
    parser.add_argument("--allowed-host", default="192.168.1.102")
    parser.add_argument("--stats-interval", type=float, default=30.0)
    parser.add_argument("--verbose", action="store_true")
    return parser


def run(args: argparse.Namespace) -> None:
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    log = logging.getLogger("turntable-gateway")
    serial = LinuxSerial(args.port, args.baud)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((args.bind, args.listen_port))
    sock.setblocking(False)
    destination = (args.udp_host, args.udp_port)
    parser = FrameStreamParser()
    counters = Counters()
    stopping = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    log.info(
        "started serial=%s baud=%d udp=%s:%d -> %s:%d allowed_host=%s",
        args.port, args.baud, args.bind, args.listen_port,
        args.udp_host, args.udp_port, args.allowed_host,
    )
    next_stats = time.monotonic() + args.stats_interval
    try:
        while not stopping:
            readable, _, _ = select.select([serial.fd, sock], [], [], 0.5)
            if serial.fd in readable:
                chunk = serial.read()
                log.debug("serial raw %s", frame_hex(chunk))
                for frame in parser.feed(chunk):
                    sock.sendto(frame, destination)
                    counters.serial_frames += 1
                    counters.serial_bytes += len(frame)
                    log.debug("serial->udp %s", frame_hex(frame))
            if sock in readable:
                while True:
                    try:
                        payload, peer = sock.recvfrom(4096)
                    except BlockingIOError:
                        break
                    if peer[0] != args.allowed_host:
                        counters.rejected_packets += 1
                        log.warning("rejected UDP packet from unauthorized host %s:%d", *peer)
                        continue
                    try:
                        validate_command_frame(payload)
                    except ProtocolError as exc:
                        counters.rejected_packets += 1
                        log.warning("rejected UDP packet from %s:%d: %s", peer[0], peer[1], exc)
                        continue
                    serial.write(payload)
                    counters.udp_commands += 1
                    counters.udp_command_bytes += len(payload)
                    log.info("udp->serial from=%s:%d frame=%s", peer[0], peer[1], frame_hex(payload))
            now = time.monotonic()
            if args.stats_interval > 0 and now >= next_stats:
                log.info(
                    "stats serial_frames=%d serial_bytes=%d udp_commands=%d "
                    "udp_command_bytes=%d rejected=%d parser_discarded=%d checksum_errors=%d",
                    counters.serial_frames, counters.serial_bytes,
                    counters.udp_commands, counters.udp_command_bytes,
                    counters.rejected_packets, parser.discarded_bytes,
                    parser.checksum_errors,
                )
                next_stats = now + args.stats_interval
    finally:
        sock.close()
        serial.close()
        log.info("stopped")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 1 <= args.listen_port <= 65535 or not 1 <= args.udp_port <= 65535:
        raise ValueError("UDP ports must be in the range 1..65535")
    if args.stats_interval < 0:
        raise ValueError("stats interval must be non-negative")
    run(args)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as exc:
        logging.error("fatal: %s", exc)
        raise SystemExit(1)
