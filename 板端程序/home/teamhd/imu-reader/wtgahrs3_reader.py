#!/usr/bin/env python3
"""Read a WIT Motion WTGAHRS3 sensor over TTL or Modbus RTU.

The runtime path targets Linux (including the RK3568 Ubuntu image) and uses
only the Python standard library.  Protocol helpers are platform independent
so captured frames can also be decoded during development on Windows.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import select
import socket
import struct
import sys
import time
from dataclasses import asdict, dataclass
from typing import Sequence

if os.name == "posix":
    import fcntl
    import termios
else:
    fcntl = None
    termios = None


START_REGISTER = 0x34
REGISTER_COUNT = 13
UDP_SCHEMA = "wtgahrs3.v1"
FRAME_HEADER = b"\xEB\x90"
FRAME_TYPES = {
    0x01: "query_status",
    0x02: "set_target_angle",
    0x03: "start_tracking",
    0x04: "stop",
    0x81: "status_response",
    0x82: "angle_response",
}
MIN_FRAME_LENGTH = 6
MAX_FRAME_LENGTH = 0xFFFF
WIT_TTL_FRAME_LENGTH = 11


class SensorError(RuntimeError):
    """Base error for transport or protocol failures."""


class ProtocolError(SensorError):
    """The sensor returned an invalid or unexpected Modbus frame."""


def crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def append_crc(data: bytes) -> bytes:
    return data + struct.pack("<H", crc16_modbus(data))


def build_read_request(address: int, start: int, count: int) -> bytes:
    if not 1 <= address <= 247:
        raise ValueError("Modbus address must be in the range 1..247")
    if not 1 <= count <= 125:
        raise ValueError("Modbus register count must be in the range 1..125")
    return append_crc(struct.pack(">BBHH", address, 0x03, start, count))


def _signed(value: int) -> int:
    return value - 0x10000 if value & 0x8000 else value


@dataclass(frozen=True)
class ImuSample:
    timestamp_utc: str
    device_address: int
    acceleration_g: dict[str, float]
    angular_velocity_dps: dict[str, float]
    angle_deg: dict[str, float]
    temperature_c: float


@dataclass(frozen=True)
class ProtocolFrame:
    message_type: int
    data: bytes


@dataclass
class GatewayState:
    tracking: bool = True
    target_angle_deg: dict[str, float] | None = None
    last_sequence: int = 0
    sensor_online: bool = False


def build_protocol_frame(message_type: int, data: bytes) -> bytes:
    """Build EB90 + total length (BE) + type + data + sum8 checksum."""
    if message_type not in FRAME_TYPES:
        raise ValueError(f"unsupported frame type: 0x{message_type:02X}")
    total_length = MIN_FRAME_LENGTH + len(data)
    if total_length > MAX_FRAME_LENGTH:
        raise ValueError(f"frame is too large: {total_length} bytes")
    without_checksum = (
        FRAME_HEADER + struct.pack(">H", total_length) + bytes((message_type,)) + data
    )
    return without_checksum + bytes((sum(without_checksum) & 0xFF,))


def parse_protocol_frame(frame: bytes) -> ProtocolFrame:
    if len(frame) < MIN_FRAME_LENGTH:
        raise ProtocolError(f"protocol frame is too short: {len(frame)} bytes")
    if frame[:2] != FRAME_HEADER:
        raise ProtocolError("protocol frame header is not EB 90")
    declared_length = int.from_bytes(frame[2:4], "big")
    if declared_length != len(frame):
        raise ProtocolError(
            f"protocol frame length says {declared_length}, received {len(frame)}"
        )
    if frame[4] not in FRAME_TYPES:
        raise ProtocolError(f"unsupported frame type: 0x{frame[4]:02X}")
    if (sum(frame[:-1]) & 0xFF) != frame[-1]:
        raise ProtocolError("protocol frame checksum failed")
    return ProtocolFrame(message_type=frame[4], data=frame[5:-1])


def parse_read_response(frame: bytes, address: int) -> ImuSample:
    expected_size = 5 + REGISTER_COUNT * 2
    if len(frame) != expected_size:
        raise ProtocolError(
            f"expected {expected_size} response bytes, received {len(frame)}"
        )
    if crc16_modbus(frame[:-2]) != int.from_bytes(frame[-2:], "little"):
        raise ProtocolError("response CRC check failed")
    if frame[0] != address:
        raise ProtocolError(
            f"response address 0x{frame[0]:02X} does not match 0x{address:02X}"
        )
    if frame[1] & 0x80:
        raise ProtocolError(f"sensor returned Modbus exception 0x{frame[2]:02X}")
    if frame[1] != 0x03 or frame[2] != REGISTER_COUNT * 2:
        raise ProtocolError("unexpected Modbus function or byte count")

    registers = struct.unpack(">13H", frame[3:-2])
    signed = [_signed(value) for value in registers]
    acceleration = [value / 32768.0 * 16.0 for value in signed[0:3]]
    angular_velocity = [value / 32768.0 * 2000.0 for value in signed[3:6]]
    angles = [value / 32768.0 * 180.0 for value in signed[9:12]]

    return ImuSample(
        timestamp_utc=dt.datetime.now(dt.timezone.utc).isoformat(
            timespec="milliseconds"
        ),
        device_address=address,
        acceleration_g=dict(zip("xyz", acceleration)),
        angular_velocity_dps=dict(zip("xyz", angular_velocity)),
        angle_deg=dict(zip("xyz", angles)),
        temperature_c=signed[12] / 100.0,
    )


def _baud_constant(baud: int) -> int:
    if termios is None:
        raise RuntimeError("termios is only available on POSIX systems")
    constants = {
        4800: termios.B4800,
        9600: termios.B9600,
        19200: termios.B19200,
        38400: termios.B38400,
        57600: termios.B57600,
        115200: termios.B115200,
        230400: termios.B230400,
    }
    try:
        return constants[baud]
    except KeyError as exc:
        raise ValueError(f"unsupported baud rate: {baud}") from exc


class LinuxSerial:
    def __init__(self, path: str, baud: int, timeout: float) -> None:
        if fcntl is None or termios is None:
            raise RuntimeError("LinuxSerial is only available on POSIX systems")
        self.path = path
        self.timeout = timeout
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            attrs = termios.tcgetattr(self.fd)
            attrs[0] = 0
            attrs[1] = 0
            attrs[2] = termios.CLOCAL | termios.CREAD | termios.CS8
            attrs[3] = 0
            speed = _baud_constant(baud)
            attrs[4] = speed
            attrs[5] = speed
            attrs[6][termios.VMIN] = 0
            attrs[6][termios.VTIME] = 0
            termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
            flags = fcntl.fcntl(self.fd, fcntl.F_GETFL)
            fcntl.fcntl(self.fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)
            termios.tcflush(self.fd, termios.TCIOFLUSH)
        except Exception:
            os.close(self.fd)
            raise

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self) -> "LinuxSerial":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def transact(self, request: bytes, response_size: int) -> bytes:
        termios.tcflush(self.fd, termios.TCIFLUSH)
        written = 0
        while written < len(request):
            written += os.write(self.fd, request[written:])
        termios.tcdrain(self.fd)

        deadline = time.monotonic() + self.timeout
        response = bytearray()
        while len(response) < response_size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            readable, _, _ = select.select([self.fd], [], [], remaining)
            if not readable:
                break
            chunk = os.read(self.fd, response_size - len(response))
            if chunk:
                response.extend(chunk)
        if len(response) != response_size:
            raise SensorError(
                f"timeout reading {self.path}: expected {response_size} bytes, "
                f"received {len(response)}"
            )
        return bytes(response)


def parse_wit_ttl_frame(frame: bytes) -> tuple[int, tuple[int, int, int, int]]:
    """Validate and decode one 11-byte WIT standard-protocol frame."""
    if len(frame) != WIT_TTL_FRAME_LENGTH:
        raise ProtocolError(
            f"expected {WIT_TTL_FRAME_LENGTH} TTL bytes, received {len(frame)}"
        )
    if frame[0] != 0x55:
        raise ProtocolError("TTL frame header is not 0x55")
    if (sum(frame[:10]) & 0xFF) != frame[10]:
        raise ProtocolError("TTL frame checksum failed")
    return frame[1], struct.unpack("<hhhh", frame[2:10])


class WitTtlSerial(LinuxSerial):
    """Passive WIT standard-protocol stream reader used by WTGAHRS3-TTL."""

    def __init__(self, path: str, baud: int, timeout: float) -> None:
        super().__init__(path, baud, timeout)
        self.buffer = bytearray()
        self.acceleration_g: dict[str, float] | None = None
        self.angular_velocity_dps: dict[str, float] | None = None
        self.temperature_c = 0.0

    def _read_frame(self) -> bytes:
        deadline = time.monotonic() + self.timeout
        while True:
            while self.buffer and self.buffer[0] != 0x55:
                del self.buffer[0]
            if len(self.buffer) >= WIT_TTL_FRAME_LENGTH:
                frame = bytes(self.buffer[:WIT_TTL_FRAME_LENGTH])
                if (sum(frame[:10]) & 0xFF) == frame[10]:
                    del self.buffer[:WIT_TTL_FRAME_LENGTH]
                    return frame
                del self.buffer[0]
                continue

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SensorError(f"timeout reading a valid TTL frame from {self.path}")
            readable, _, _ = select.select([self.fd], [], [], remaining)
            if not readable:
                continue
            chunk = os.read(self.fd, 4096)
            if chunk:
                self.buffer.extend(chunk)

    def read_sample(self) -> ImuSample:
        while True:
            frame_type, values = parse_wit_ttl_frame(self._read_frame())
            if frame_type == 0x51:
                self.acceleration_g = dict(
                    zip("xyz", (value / 32768.0 * 16.0 for value in values[:3]))
                )
                self.temperature_c = values[3] / 100.0
            elif frame_type == 0x52:
                self.angular_velocity_dps = dict(
                    zip("xyz", (value / 32768.0 * 2000.0 for value in values[:3]))
                )
            elif (
                frame_type == 0x53
                and self.acceleration_g is not None
                and self.angular_velocity_dps is not None
            ):
                angles = dict(
                    zip("xyz", (value / 32768.0 * 180.0 for value in values[:3]))
                )
                sample = ImuSample(
                    timestamp_utc=dt.datetime.now(dt.timezone.utc).isoformat(
                        timespec="milliseconds"
                    ),
                    device_address=0,
                    acceleration_g=self.acceleration_g,
                    angular_velocity_dps=self.angular_velocity_dps,
                    angle_deg=angles,
                    temperature_c=self.temperature_c,
                )
                self.acceleration_g = None
                self.angular_velocity_dps = None
                return sample


class UdpPublisher:
    """Bidirectional UDP endpoint for samples, commands, and responses."""

    def __init__(
        self,
        host: str,
        port: int,
        source: str | None = None,
        listen_port: int | None = None,
    ) -> None:
        self.destination = (host, port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if source or listen_port is not None:
            self.sock.bind((source or "0.0.0.0", listen_port or 0))
        self.sock.setblocking(False)

    def close(self) -> None:
        self.sock.close()

    def __enter__(self) -> "UdpPublisher":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def send(
        self, payload: bytes, destination: tuple[str, int] | None = None
    ) -> None:
        written = self.sock.sendto(payload, destination or self.destination)
        if written != len(payload):
            raise OSError(f"short UDP send: {written} of {len(payload)} bytes")

    def receive_available(self) -> list[tuple[bytes, tuple[str, int]]]:
        packets: list[tuple[bytes, tuple[str, int]]] = []
        while True:
            try:
                packets.append(self.sock.recvfrom(65535))
            except BlockingIOError:
                return packets


def read_sample(transport: LinuxSerial, address: int) -> ImuSample:
    request = build_read_request(address, START_REGISTER, REGISTER_COUNT)
    response = transport.transact(request, 5 + REGISTER_COUNT * 2)
    return parse_read_response(response, address)


def _parse_address(value: str) -> int:
    return int(value, 0)


def _parse_hex_frame(value: str) -> bytes:
    compact = "".join(value.replace(",", " ").split())
    return bytes.fromhex(compact)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="/dev/ttyUSB0")
    parser.add_argument("--baud", type=int, default=9600)
    parser.add_argument(
        "--transport",
        choices=("modbus", "ttl"),
        default="modbus",
        help="485 uses Modbus polling; TTL passively receives WIT 0x55 frames",
    )
    parser.add_argument("--address", type=_parse_address, default=0x10)
    parser.add_argument("--interval", type=float, default=0.2)
    parser.add_argument("--timeout", type=float, default=1.0)
    parser.add_argument(
        "--count", type=int, default=0, help="samples to read; 0 means run forever"
    )
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument(
        "--udp-host",
        help="destination IPv4 address; omit to disable UDP publishing",
    )
    parser.add_argument("--udp-port", type=int, default=20002)
    parser.add_argument(
        "--udp-type",
        type=_parse_address,
        default=0x82,
        help="outer protocol type; WTGAHRS3 periodic data defaults to 0x82",
    )
    parser.add_argument(
        "--udp-source",
        help="optional local IPv4 address used to select the output interface",
    )
    parser.add_argument(
        "--udp-listen-port",
        type=int,
        default=20002,
        help="local command port; 0 disables command reception",
    )
    parser.add_argument(
        "--decode-hex",
        metavar="FRAME",
        help="decode a captured response without opening a serial port",
    )
    return parser


def encode_sample(
    sample: ImuSample, *, sequence: int | None = None, pretty: bool = False
) -> bytes:
    document = asdict(sample)
    if sequence is not None:
        document = {"schema": UDP_SCHEMA, "sequence": sequence, **document}
    return json.dumps(
            document,
            ensure_ascii=False,
            indent=2 if pretty else None,
            separators=None if pretty else (",", ":"),
        ).encode("utf-8")


def emit(sample: ImuSample, pretty: bool) -> None:
    sys.stdout.buffer.write(encode_sample(sample, pretty=pretty) + b"\n")
    sys.stdout.buffer.flush()


def encode_status(state: GatewayState) -> bytes:
    return json.dumps(
        {
            "schema": "wtgahrs3.status.v1",
            "timestamp_utc": dt.datetime.now(dt.timezone.utc).isoformat(
                timespec="milliseconds"
            ),
            "sensor_online": state.sensor_online,
            "tracking": state.tracking,
            "target_angle_deg": state.target_angle_deg,
            "last_sequence": state.last_sequence,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def handle_command(frame: ProtocolFrame, state: GatewayState) -> ProtocolFrame | None:
    if frame.message_type == 0x01:
        if frame.data:
            raise ProtocolError("query status command must have an empty data field")
    elif frame.message_type == 0x02:
        try:
            target = json.loads(frame.data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolError("target angle data must be UTF-8 JSON") from exc
        if not isinstance(target, dict) or set(target) != {"x", "y", "z"}:
            raise ProtocolError("target angle must contain exactly x, y, and z")
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in target.values()):
            raise ProtocolError("target angle values must be numbers")
        state.target_angle_deg = {axis: float(target[axis]) for axis in "xyz"}
    elif frame.message_type == 0x03:
        if frame.data:
            raise ProtocolError("start tracking command must have an empty data field")
        state.tracking = True
    elif frame.message_type == 0x04:
        if frame.data:
            raise ProtocolError("stop command must have an empty data field")
        state.tracking = False
    else:
        return None
    return ProtocolFrame(message_type=0x81, data=encode_status(state))


def process_commands(endpoint: UdpPublisher, state: GatewayState) -> None:
    for payload, peer in endpoint.receive_available():
        try:
            command = parse_protocol_frame(payload)
            response = handle_command(command, state)
            if response is None:
                continue
            endpoint.send(
                build_protocol_frame(response.message_type, response.data), peer
            )
            print(
                f"command type=0x{command.message_type:02X} from "
                f"{peer[0]}:{peer[1]} accepted",
                file=sys.stderr,
                flush=True,
            )
        except (ProtocolError, ValueError) as exc:
            print(
                f"invalid UDP command from {peer[0]}:{peer[1]}: {exc}",
                file=sys.stderr,
                flush=True,
            )


def run_reader(args: argparse.Namespace) -> None:
    publisher = (
        UdpPublisher(
            args.udp_host,
            args.udp_port,
            args.udp_source,
            args.udp_listen_port or None,
        )
        if args.udp_host
        else None
    )
    state = GatewayState()
    try:
        emitted = 0
        transport_class = WitTtlSerial if args.transport == "ttl" else LinuxSerial
        with transport_class(args.port, args.baud, args.timeout) as transport:
            while args.count == 0 or emitted < args.count:
                started = time.monotonic()
                sample = (
                    transport.read_sample()
                    if args.transport == "ttl"
                    else read_sample(transport, args.address)
                )
                state.sensor_online = True
                emit(sample, args.pretty)
                emitted += 1
                state.last_sequence = emitted
                if publisher and state.tracking:
                    data = encode_sample(sample, sequence=emitted)
                    publisher.send(build_protocol_frame(args.udp_type, data))
                if publisher:
                    process_commands(publisher, state)
                delay = (
                    args.interval - (time.monotonic() - started)
                    if args.transport == "modbus"
                    else 0
                )
                if delay > 0 and (args.count == 0 or emitted < args.count):
                    time.sleep(delay)
    finally:
        if publisher:
            publisher.close()


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.decode_hex:
        emit(parse_read_response(_parse_hex_frame(args.decode_hex), args.address), args.pretty)
        return 0
    if os.name != "posix":
        print(
            "live serial acquisition is intended for Linux; use --decode-hex "
            "for captured frames on this platform",
            file=sys.stderr,
        )
        return 2
    if args.interval < 0 or args.timeout <= 0 or args.count < 0:
        raise ValueError("interval/count must be non-negative and timeout must be positive")
    if not 1 <= args.udp_port <= 65535:
        raise ValueError("UDP port must be in the range 1..65535")
    if not 0 <= args.udp_listen_port <= 65535:
        raise ValueError("UDP listen port must be in the range 0..65535")
    if args.udp_type not in FRAME_TYPES:
        raise ValueError(f"unsupported UDP frame type: 0x{args.udp_type:02X}")

    run_reader(args)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, SensorError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
