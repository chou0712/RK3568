from __future__ import annotations

import argparse
import ctypes
import math
import os
import queue
import socket
import sys
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


APP_TITLE = "HZZT-2-3kg 转台控制器"
BAUD_RATE = 115200
DEFAULT_GATEWAY_HOST = "192.168.1.101"
DEFAULT_GATEWAY_PORT = 20003
DEFAULT_LOCAL_HOST = "192.168.1.102"
DEFAULT_LOCAL_PORT = 20003
AZIMUTH_MIN = 0.0
AZIMUTH_MAX = 360.0
ELEVATION_MIN = 0.0
ELEVATION_MAX = 90.0
MAX_AZIMUTH_SPEED = 30.0  # 安全默认值；协议写 40°/s，任务书要求不低于 30°/s
MAX_ELEVATION_SPEED = 30.0


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


def checksum_sum8(data: bytes) -> int:
    return sum(data) & 0xFF


def with_checksum(body: bytes) -> bytes:
    return body + bytes((checksum_sum8(body),))


def u16_be(value: int) -> bytes:
    if not 0 <= value <= 0xFFFF:
        raise ValueError("数值超出 uint16 范围")
    return value.to_bytes(2, "big", signed=False)


def i16_be(value: int) -> bytes:
    if not -32768 <= value <= 32767:
        raise ValueError("数值超出 int16 范围")
    return value.to_bytes(2, "big", signed=True)


def scaled_u16(value: float, minimum: float, maximum: float, name: str) -> bytes:
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{name}必须在 {minimum:g}～{maximum:g} 之间")
    return u16_be(round(value * 10))


def scaled_i16(value: float, minimum: float, maximum: float, name: str) -> bytes:
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{name}必须在 {minimum:g}～{maximum:g} 之间")
    return i16_be(round(value * 10))


class Protocol:
    @staticmethod
    def reset() -> bytes:
        return with_checksum(bytes.fromhex("AA 06 01 01 01"))

    @staticmethod
    def query_status() -> bytes:
        return with_checksum(bytes.fromhex("AA 06 01 02 01"))

    @staticmethod
    def query_driver() -> bytes:
        return with_checksum(bytes.fromhex("AA 06 01 03 01"))

    @staticmethod
    def query_fault() -> bytes:
        return with_checksum(bytes.fromhex("AA 06 01 04 01"))

    @staticmethod
    def query_settings() -> bytes:
        return with_checksum(bytes.fromhex("AA 06 01 05 01"))

    @staticmethod
    def continuous(clockwise: bool) -> bytes:
        direction = 0x00 if clockwise else 0x01
        return with_checksum(bytes((0xAA, 0x0C, 0x01, 0x06, 0x01, 0x01, direction, 0, 0, 0, 0)))

    @staticmethod
    def point(azimuth: float, elevation: float, tracking: bool = False) -> bytes:
        subcommand = 0x03 if tracking else 0x02
        body = bytes((0xAA, 0x0C, 0x01, 0x06, subcommand, 0x01, 0x00))
        body += scaled_u16(azimuth, AZIMUTH_MIN, AZIMUTH_MAX, "方位角")
        body += scaled_u16(elevation, ELEVATION_MIN, ELEVATION_MAX, "俯仰角")
        return with_checksum(body)

    @staticmethod
    def stop() -> bytes:
        return with_checksum(bytes.fromhex("AA 0C 01 06 04 01 00 00 00 00 00"))

    @staticmethod
    def spatial_scan(az_start: float, az_end: float, el_start: float, el_end: float) -> bytes:
        if az_start > az_end:
            raise ValueError("方位起始角不能大于终止角；当前固件是否支持跨零扫描尚未确认")
        if el_start > el_end:
            raise ValueError("俯仰起始角不能大于终止角")
        body = bytes.fromhex("AA 0F 01 06 05 01")
        body += scaled_u16(az_start, AZIMUTH_MIN, AZIMUTH_MAX, "方位起始角")
        body += scaled_u16(az_end, AZIMUTH_MIN, AZIMUTH_MAX, "方位终止角")
        body += scaled_u16(el_start, ELEVATION_MIN, ELEVATION_MAX, "俯仰起始角")
        body += scaled_u16(el_end, ELEVATION_MIN, ELEVATION_MAX, "俯仰终止角")
        return with_checksum(body)

    @staticmethod
    def step(azimuth_delta: float, elevation_delta: float) -> bytes:
        # 协议虽标为 uint16，但同时定义正负方向；这里按 int16 补码编码。
        body = bytes.fromhex("AA 0B 01 06 06 01")
        body += scaled_i16(azimuth_delta, -360.0, 360.0, "方位步进")
        body += scaled_i16(elevation_delta, -90.0, 90.0, "俯仰步进")
        return with_checksum(body)

    @staticmethod
    def azimuth_step(delta: float) -> bytes:
        body = bytes.fromhex("AA 09 01 06 06 01")
        body += scaled_i16(delta, -360.0, 360.0, "方位步进")
        return with_checksum(body)

    @staticmethod
    def set_speed(azimuth_speed: float, elevation_speed: float, length_field: int = 0x0B) -> bytes:
        if length_field not in (0x09, 0x0B):
            raise ValueError("速度帧长度字段只能为 0x09 或 0x0B")
        body = bytes((0xAA, length_field, 0x01, 0x06, 0x07, 0x01))
        body += scaled_u16(azimuth_speed, 0.0, MAX_AZIMUTH_SPEED, "方位速度")
        body += scaled_u16(elevation_speed, 0.0, MAX_ELEVATION_SPEED, "俯仰速度")
        return with_checksum(body)

    @staticmethod
    def set_zero(azimuth_axis: bool) -> bytes:
        subcommand = 0x01 if azimuth_axis else 0x02
        return with_checksum(bytes((0xAA, 0x07, 0x01, 0x08, subcommand, 0x01)))


@dataclass(frozen=True)
class Status:
    azimuth_deg: float
    elevation_deg: float
    azimuth_speed_dps: float
    elevation_speed_dps: float
    fault: int
    raw: bytes

    @property
    def faults(self) -> list[str]:
        return [name for mask, name in FAULT_NAMES.items() if self.fault & mask]


def decode_i16(high: int, low: int) -> int:
    value = (high << 8) | low
    return value - 0x10000 if value >= 0x8000 else value


def parse_status(frame: bytes) -> Status:
    if len(frame) not in (15, 16):
        raise ValueError("状态帧长度不是 15 或 16 字节")
    if frame[0] != 0xAA or frame[2] != 0x01 or frame[3] not in (0x02, 0x07):
        raise ValueError("不是状态帧")
    if checksum_sum8(frame[:-1]) != frame[-1]:
        raise ValueError("状态帧校验失败")
    return Status(
        azimuth_deg=((frame[5] << 8) | frame[6]) / 10.0,
        elevation_deg=((frame[7] << 8) | frame[8]) / 10.0,
        azimuth_speed_dps=decode_i16(frame[9], frame[10]) / 10.0,
        elevation_speed_dps=decode_i16(frame[11], frame[12]) / 10.0,
        # 协议列出的逻辑字段把故障字节固定在索引 13。若收到 16 字节帧，
        # 将第 15 个字节视为尚未定义的保留字节，而不是移动既有字段。
        fault=frame[13],
        raw=frame,
    )


class FrameParser:
    """面向连续串口字节流的帧解析器，兼容 15/16 字节状态帧。"""

    def __init__(self) -> None:
        self.buffer = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        self.buffer.extend(data)
        result: list[bytes] = []
        while True:
            try:
                start = self.buffer.index(0xAA)
            except ValueError:
                self.buffer.clear()
                break
            if start:
                del self.buffer[:start]
            if len(self.buffer) < 2:
                break

            command = self.buffer[3] if len(self.buffer) >= 4 else None
            # 0x02 既是 6 字节的状态查询命令，也是状态应答命令。
            # 只有长度字段为 0x10 时才走状态帧的 15/16 字节兼容逻辑。
            if command in (0x02, 0x07) and self.buffer[1] == 0x10:
                if len(self.buffer) < 15:
                    break
                frame15 = bytes(self.buffer[:15])
                if checksum_sum8(frame15[:-1]) == frame15[-1]:
                    result.append(frame15)
                    del self.buffer[:15]
                    continue
                if len(self.buffer) < 16:
                    break
                frame16 = bytes(self.buffer[:16])
                if checksum_sum8(frame16[:-1]) == frame16[-1]:
                    result.append(frame16)
                    del self.buffer[:16]
                    continue
                del self.buffer[0]
                continue

            declared = self.buffer[1]
            if not 6 <= declared <= 64:
                del self.buffer[0]
                continue
            if len(self.buffer) < declared:
                break
            candidate = bytes(self.buffer[:declared])
            if checksum_sum8(candidate[:-1]) == candidate[-1]:
                result.append(candidate)
                del self.buffer[:declared]
            else:
                del self.buffer[0]
        return result


def hex_text(data: bytes) -> str:
    return data.hex(" ").upper()


def validate_udp_frame(frame: bytes) -> bytes:
    """Validate one native turntable frame carried by one UDP datagram."""
    if not 6 <= len(frame) <= 64:
        raise ValueError(f"UDP 数据报长度无效：{len(frame)} 字节")
    if frame[0] != 0xAA or frame[2] != 0x01:
        raise ValueError("UDP 数据报不是设备 01 的 AA 协议帧")
    if checksum_sum8(frame[:-1]) != frame[-1]:
        raise ValueError("UDP 数据报累加校验失败")

    declared = frame[1]
    command = frame[3]
    normal = declared == len(frame)
    status_exception = command in (0x02, 0x07) and declared == 0x10 and len(frame) in (15, 16)
    settings_exception = command == 0x05 and declared == 0x0E and len(frame) == 10
    speed_exception = (
        command == 0x06
        and len(frame) >= 5
        and frame[4] == 0x07
        and declared == 0x09
        and len(frame) == 11
    )
    if not (normal or status_exception or settings_exception or speed_exception):
        raise ValueError(f"协议长度字段为 {declared}，UDP 数据报为 {len(frame)} 字节")
    return frame


def list_serial_ports() -> list[str]:
    if os.name == "nt":
        try:
            import winreg

            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DEVICEMAP\SERIALCOMM")
            ports: set[str] = set()
            index = 0
            while True:
                try:
                    _, value, _ = winreg.EnumValue(key, index)
                    ports.add(str(value))
                    index += 1
                except OSError:
                    break
            winreg.CloseKey(key)
            return sorted(ports, key=lambda value: (len(value), value))
        except OSError:
            return []
    try:
        from serial.tools import list_ports

        return sorted(port.device for port in list_ports.comports())
    except ImportError:
        return []


class SerialLike:
    def read(self, size: int = 256) -> bytes:
        raise NotImplementedError

    def write(self, data: bytes) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


if os.name == "nt":
    INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    OPEN_EXISTING = 3
    PURGE_RXCLEAR = 0x0008
    PURGE_TXCLEAR = 0x0004

    class DCB(ctypes.Structure):
        _fields_ = [
            ("DCBlength", wintypes.DWORD),
            ("BaudRate", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("wReserved", wintypes.WORD),
            ("XonLim", wintypes.WORD),
            ("XoffLim", wintypes.WORD),
            ("ByteSize", ctypes.c_ubyte),
            ("Parity", ctypes.c_ubyte),
            ("StopBits", ctypes.c_ubyte),
            ("XonChar", ctypes.c_char),
            ("XoffChar", ctypes.c_char),
            ("ErrorChar", ctypes.c_char),
            ("EofChar", ctypes.c_char),
            ("EvtChar", ctypes.c_char),
            ("wReserved1", wintypes.WORD),
        ]

    class COMMTIMEOUTS(ctypes.Structure):
        _fields_ = [
            ("ReadIntervalTimeout", wintypes.DWORD),
            ("ReadTotalTimeoutMultiplier", wintypes.DWORD),
            ("ReadTotalTimeoutConstant", wintypes.DWORD),
            ("WriteTotalTimeoutMultiplier", wintypes.DWORD),
            ("WriteTotalTimeoutConstant", wintypes.DWORD),
        ]


class Win32Serial(SerialLike):
    """不依赖第三方包的 Windows 串口后端。"""

    def __init__(self, port: str, baudrate: int = BAUD_RATE) -> None:
        if os.name != "nt":
            raise OSError("Win32Serial 仅支持 Windows")
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel32.CreateFileW.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        self.kernel32.CreateFileW.restype = wintypes.HANDLE
        self.kernel32.GetCommState.argtypes = (wintypes.HANDLE, ctypes.POINTER(DCB))
        self.kernel32.GetCommState.restype = wintypes.BOOL
        self.kernel32.BuildCommDCBW.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(DCB))
        self.kernel32.BuildCommDCBW.restype = wintypes.BOOL
        self.kernel32.SetCommState.argtypes = (wintypes.HANDLE, ctypes.POINTER(DCB))
        self.kernel32.SetCommState.restype = wintypes.BOOL
        self.kernel32.SetCommTimeouts.argtypes = (wintypes.HANDLE, ctypes.POINTER(COMMTIMEOUTS))
        self.kernel32.SetCommTimeouts.restype = wintypes.BOOL
        self.kernel32.PurgeComm.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        self.kernel32.PurgeComm.restype = wintypes.BOOL
        self.kernel32.ReadFile.argtypes = (
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        )
        self.kernel32.ReadFile.restype = wintypes.BOOL
        self.kernel32.WriteFile.argtypes = self.kernel32.ReadFile.argtypes
        self.kernel32.WriteFile.restype = wintypes.BOOL
        self.kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.kernel32.CloseHandle.restype = wintypes.BOOL
        path = port if port.startswith("\\\\.\\") else f"\\\\.\\{port}"
        self.handle = self.kernel32.CreateFileW(
            path, GENERIC_READ | GENERIC_WRITE, 0, None, OPEN_EXISTING, 0, None
        )
        if self.handle == INVALID_HANDLE_VALUE:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            dcb = DCB()
            dcb.DCBlength = ctypes.sizeof(DCB)
            if not self.kernel32.GetCommState(self.handle, ctypes.byref(dcb)):
                raise ctypes.WinError(ctypes.get_last_error())
            config = f"baud={baudrate} parity=N data=8 stop=1"
            if not self.kernel32.BuildCommDCBW(config, ctypes.byref(dcb)):
                raise ctypes.WinError(ctypes.get_last_error())
            # 仅保留 fBinary=1，关闭软流控、硬件流控、DTR/RTS 和奇偶校验。
            dcb.flags = 0x00000001
            if not self.kernel32.SetCommState(self.handle, ctypes.byref(dcb)):
                raise ctypes.WinError(ctypes.get_last_error())
            timeouts = COMMTIMEOUTS(20, 0, 100, 0, 500)
            if not self.kernel32.SetCommTimeouts(self.handle, ctypes.byref(timeouts)):
                raise ctypes.WinError(ctypes.get_last_error())
            self.kernel32.PurgeComm(self.handle, PURGE_RXCLEAR | PURGE_TXCLEAR)
        except Exception:
            self.close()
            raise

    def read(self, size: int = 256) -> bytes:
        buffer = ctypes.create_string_buffer(size)
        count = wintypes.DWORD()
        ok = self.kernel32.ReadFile(self.handle, buffer, size, ctypes.byref(count), None)
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
        return buffer.raw[: count.value]

    def write(self, data: bytes) -> None:
        count = wintypes.DWORD()
        buffer = ctypes.create_string_buffer(data)
        ok = self.kernel32.WriteFile(self.handle, buffer, len(data), ctypes.byref(count), None)
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
        if count.value != len(data):
            raise OSError(f"串口仅写入 {count.value}/{len(data)} 字节")

    def close(self) -> None:
        handle = getattr(self, "handle", None)
        if handle not in (None, INVALID_HANDLE_VALUE):
            self.kernel32.CloseHandle(handle)
            self.handle = None


class PySerialBackend(SerialLike):
    def __init__(self, port: str, baudrate: int = BAUD_RATE) -> None:
        import serial

        self.serial = serial.Serial(
            port=port,
            baudrate=baudrate,
            bytesize=8,
            parity="N",
            stopbits=1,
            timeout=0.1,
            write_timeout=0.5,
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
        )

    def read(self, size: int = 256) -> bytes:
        return self.serial.read(size)

    def write(self, data: bytes) -> None:
        count = self.serial.write(data)
        self.serial.flush()
        if count != len(data):
            raise OSError(f"串口仅写入 {count}/{len(data)} 字节")

    def close(self) -> None:
        self.serial.close()


class DemoSerial(SerialLike):
    """界面演示和无实机测试使用，不访问串口。"""

    def __init__(self) -> None:
        self.rx: queue.Queue[bytes] = queue.Queue()
        self.azimuth = 0.0
        self.elevation = 0.0
        self.closed = False
        self._emit_status()

    def _emit_status(self) -> None:
        body = bytes.fromhex("AA 10 01 07 00")
        body += u16_be(round(self.azimuth * 10))
        body += u16_be(round(self.elevation * 10))
        body += i16_be(0) + i16_be(0) + bytes((0,))
        self.rx.put(with_checksum(body))

    def read(self, size: int = 256) -> bytes:
        try:
            return self.rx.get(timeout=0.1)
        except queue.Empty:
            return b""

    def write(self, data: bytes) -> None:
        if self.closed:
            raise OSError("演示串口已关闭")
        if len(data) >= 5 and data[3] == 0x06:
            sub = data[4]
            if sub in (0x02, 0x03):
                self.azimuth = int.from_bytes(data[7:9], "big") / 10.0
                self.elevation = int.from_bytes(data[9:11], "big") / 10.0
            elif sub == 0x06 and len(data) == 11:
                self.azimuth = (self.azimuth + int.from_bytes(data[6:8], "big", signed=True) / 10.0) % 360.0
                self.elevation = max(0.0, min(90.0, self.elevation + int.from_bytes(data[8:10], "big", signed=True) / 10.0))
            elif sub == 0x06 and len(data) == 9:
                self.azimuth = (self.azimuth + int.from_bytes(data[6:8], "big", signed=True) / 10.0) % 360.0
        self._emit_status()

    def close(self) -> None:
        self.closed = True


def open_serial(port: str, demo: bool = False) -> SerialLike:
    if demo:
        return DemoSerial()
    if os.name == "nt":
        return Win32Serial(port)
    try:
        return PySerialBackend(port)
    except ImportError as exc:
        raise RuntimeError("非 Windows 系统需要安装 pyserial：python -m pip install pyserial") from exc


class SerialSession:
    def __init__(self, event_queue: queue.Queue[tuple[str, object]]) -> None:
        self.event_queue = event_queue
        self.serial: Optional[SerialLike] = None
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.write_lock = threading.Lock()

    @property
    def connected(self) -> bool:
        return self.serial is not None

    def connect(self, port: str, demo: bool = False) -> None:
        if self.connected:
            raise RuntimeError("串口已经连接")
        self.serial = open_serial(port, demo=demo)
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._reader, name="turntable-serial-reader", daemon=True)
        self.thread.start()

    def _reader(self) -> None:
        parser = FrameParser()
        while not self.stop_event.is_set():
            try:
                assert self.serial is not None
                data = self.serial.read(256)
                if not data:
                    continue
                self.event_queue.put(("raw_rx", data))
                for frame in parser.feed(data):
                    self.event_queue.put(("frame", frame))
            except Exception as exc:
                if not self.stop_event.is_set():
                    self.event_queue.put(("serial_error", exc))
                break

    def send(self, frame: bytes) -> None:
        if self.serial is None:
            raise RuntimeError("请先连接串口")
        with self.write_lock:
            self.serial.write(frame)
        self.event_queue.put(("tx", frame))

    def disconnect(self) -> None:
        self.stop_event.set()
        serial_port, self.serial = self.serial, None
        if serial_port is not None:
            serial_port.close()
        if self.thread and self.thread.is_alive() and self.thread is not threading.current_thread():
            self.thread.join(timeout=0.5)
        self.thread = None


class UdpSession:
    """Carry one native turntable frame per UDP datagram through the RK3568."""

    def __init__(self, event_queue: queue.Queue[tuple[str, object]]) -> None:
        self.event_queue = event_queue
        self.socket: Optional[socket.socket] = None
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.write_lock = threading.Lock()
        self.remote: Optional[tuple[str, int]] = None
        self.local: Optional[tuple[str, int]] = None

    @property
    def connected(self) -> bool:
        return self.socket is not None

    def connect(
        self,
        gateway_host: str,
        gateway_port: int,
        local_host: str,
        local_port: int,
    ) -> None:
        if self.connected:
            raise RuntimeError("UDP 网关已经连接")
        if not 1 <= gateway_port <= 65535 or not 0 <= local_port <= 65535:
            raise ValueError("UDP 端口必须在 1～65535 范围内")

        remote_host = socket.gethostbyname(gateway_host)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            sock.bind((local_host, local_port))
            sock.connect((remote_host, gateway_port))
            sock.settimeout(0.2)
            self.remote = (remote_host, gateway_port)
            self.local = sock.getsockname()
            self.socket = sock
            self.stop_event.clear()
            self.thread = threading.Thread(
                target=self._reader,
                name="turntable-udp-reader",
                daemon=True,
            )
            self.thread.start()
        except Exception:
            sock.close()
            raise

    def _reader(self) -> None:
        while not self.stop_event.is_set():
            try:
                sock = self.socket
                if sock is None:
                    break
                data = sock.recv(4096)
                self.event_queue.put(("raw_rx", data))
                try:
                    frame = validate_udp_frame(data)
                except ValueError as exc:
                    self.event_queue.put(("protocol_error", exc))
                    continue
                self.event_queue.put(("frame", frame))
            except socket.timeout:
                continue
            except OSError as exc:
                if not self.stop_event.is_set():
                    self.event_queue.put(("network_error", exc))
                break

    def send(self, frame: bytes) -> None:
        sock = self.socket
        if sock is None:
            raise RuntimeError("请先连接 RK3568 UDP 网关")
        with self.write_lock:
            written = sock.send(frame)
        if written != len(frame):
            raise OSError(f"UDP 仅发送 {written}/{len(frame)} 字节")
        self.event_queue.put(("tx", frame))

    def disconnect(self) -> None:
        self.stop_event.set()
        sock, self.socket = self.socket, None
        if sock is not None:
            sock.close()
        if self.thread and self.thread.is_alive() and self.thread is not threading.current_thread():
            self.thread.join(timeout=0.5)
        self.thread = None
        self.remote = None
        self.local = None


class TurntableApp(tk.Tk):
    def __init__(
        self,
        demo: bool = False,
        gateway_host: str = DEFAULT_GATEWAY_HOST,
        gateway_port: int = DEFAULT_GATEWAY_PORT,
        local_host: str = DEFAULT_LOCAL_HOST,
        local_port: int = DEFAULT_LOCAL_PORT,
        auto_connect: bool = True,
    ) -> None:
        super().__init__()
        self.title(APP_TITLE + ("（演示模式）" if demo else "（RK3568 网络版）"))
        self.geometry("1020x760")
        self.minsize(900, 680)
        self.demo = demo
        self.default_gateway_host = gateway_host
        self.default_gateway_port = gateway_port
        self.default_local_host = local_host
        self.default_local_port = local_port
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.session = SerialSession(self.events) if demo else UdpSession(self.events)
        self.current_status: Optional[Status] = None
        self.last_status_time: Optional[float] = None
        self.last_motion_time: Optional[float] = None
        self.watchdog_stopped = False
        self.fault_stopped = False
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(50, self.poll_events)
        self.after(200, self.watchdog_tick)
        if demo:
            self.after(100, self.connect)
        elif auto_connect:
            self.after(250, self.connect)

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)

        connection = ttk.LabelFrame(self, text="连接")
        connection.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 5))
        connection.columnconfigure(9, weight=1)
        ttk.Label(connection, text="RK3568").grid(row=0, column=0, padx=(6, 2), pady=7)
        self.gateway_var = tk.StringVar(value=self.default_gateway_host)
        ttk.Entry(connection, textvariable=self.gateway_var, width=15).grid(row=0, column=1, padx=2, pady=7)
        ttk.Label(connection, text=":").grid(row=0, column=2)
        self.gateway_port_var = tk.StringVar(value=str(self.default_gateway_port))
        ttk.Entry(connection, textvariable=self.gateway_port_var, width=6).grid(row=0, column=3, padx=(0, 8), pady=7)
        ttk.Label(connection, text="本机").grid(row=0, column=4, padx=(2, 2), pady=7)
        self.local_var = tk.StringVar(value=self.default_local_host)
        ttk.Entry(connection, textvariable=self.local_var, width=15).grid(row=0, column=5, padx=2, pady=7)
        ttk.Label(connection, text=":").grid(row=0, column=6)
        self.local_port_var = tk.StringVar(value=str(self.default_local_port))
        ttk.Entry(connection, textvariable=self.local_port_var, width=6).grid(row=0, column=7, padx=(0, 8), pady=7)
        self.connect_button = ttk.Button(connection, text="连接", command=self.connect)
        self.connect_button.grid(row=0, column=8, padx=4, sticky="w")
        self.connection_var = tk.StringVar(value="未连接")
        ttk.Label(connection, textvariable=self.connection_var).grid(row=0, column=9, padx=10, sticky="e")

        status_box = ttk.LabelFrame(self, text="实时状态")
        status_box.grid(row=1, column=0, sticky="ew", padx=10, pady=5)
        for col in range(5):
            status_box.columnconfigure(col, weight=1)
        self.az_status = tk.StringVar(value="--.-°")
        self.el_status = tk.StringVar(value="--.-°")
        self.az_speed_status = tk.StringVar(value="--.-°/s")
        self.el_speed_status = tk.StringVar(value="--.-°/s")
        self.fault_status = tk.StringVar(value="未收到状态")
        values = [
            ("方位角", self.az_status),
            ("俯仰角", self.el_status),
            ("方位速度", self.az_speed_status),
            ("俯仰速度", self.el_speed_status),
            ("故障", self.fault_status),
        ]
        for col, (label, variable) in enumerate(values):
            ttk.Label(status_box, text=label).grid(row=0, column=col, pady=(6, 0))
            ttk.Label(status_box, textvariable=variable, font=("Microsoft YaHei UI", 12, "bold")).grid(
                row=1, column=col, padx=8, pady=(0, 8)
            )

        controls = ttk.Notebook(self)
        controls.grid(row=2, column=0, sticky="ew", padx=10, pady=5)
        self._build_basic_tab(controls)
        self._build_scan_tab(controls)
        self._build_maintenance_tab(controls)

        log_box = ttk.LabelFrame(self, text="通信日志")
        log_box.grid(row=3, column=0, sticky="nsew", padx=10, pady=(5, 10))
        log_box.columnconfigure(0, weight=1)
        log_box.rowconfigure(0, weight=1)
        self.log_text = tk.Text(log_box, height=14, wrap="none", font=("Consolas", 9), state="disabled")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_box, orient="vertical", command=self.log_text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set)
        buttons = ttk.Frame(log_box)
        buttons.grid(row=1, column=0, columnspan=2, sticky="ew", pady=4)
        ttk.Button(buttons, text="保存日志", command=self.save_log).pack(side="left", padx=4)
        ttk.Button(buttons, text="清空", command=self.clear_log).pack(side="left", padx=4)
        self.watchdog_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(buttons, text="运动后 1 秒无有效状态则自动停止", variable=self.watchdog_var).pack(side="right", padx=8)

    def _build_basic_tab(self, notebook: ttk.Notebook) -> None:
        tab = ttk.Frame(notebook, padding=10)
        notebook.add(tab, text="基本控制")
        ttk.Button(tab, text="查询状态", command=lambda: self.send(Protocol.query_status(), "查询状态")).grid(row=0, column=0, padx=4, pady=4)
        ttk.Button(tab, text="查询故障", command=lambda: self.send(Protocol.query_fault(), "查询故障")).grid(row=0, column=1, padx=4, pady=4)
        ttk.Button(tab, text="查询驱动器", command=lambda: self.send(Protocol.query_driver(), "查询驱动器")).grid(row=0, column=2, padx=4, pady=4)
        ttk.Button(tab, text="设备复位", command=self.reset_device).grid(row=0, column=3, padx=4, pady=4)
        ttk.Button(tab, text="停止并锁定", command=self.stop_motion, style="Danger.TButton").grid(row=0, column=4, padx=18, pady=4)

        ttk.Separator(tab, orient="horizontal").grid(row=1, column=0, columnspan=6, sticky="ew", pady=8)
        ttk.Label(tab, text="定点：方位").grid(row=2, column=0, sticky="e")
        self.point_az = tk.StringVar(value="10.0")
        ttk.Entry(tab, textvariable=self.point_az, width=9).grid(row=2, column=1)
        ttk.Label(tab, text="俯仰").grid(row=2, column=2, sticky="e")
        self.point_el = tk.StringVar(value="5.0")
        ttk.Entry(tab, textvariable=self.point_el, width=9).grid(row=2, column=3)
        ttk.Button(tab, text="执行定点", command=self.point_move).grid(row=2, column=4, padx=8, pady=4)

        ttk.Label(tab, text="步进：方位").grid(row=3, column=0, sticky="e")
        self.step_az = tk.StringVar(value="1.0")
        ttk.Entry(tab, textvariable=self.step_az, width=9).grid(row=3, column=1)
        ttk.Label(tab, text="俯仰").grid(row=3, column=2, sticky="e")
        self.step_el = tk.StringVar(value="0.0")
        ttk.Entry(tab, textvariable=self.step_el, width=9).grid(row=3, column=3)
        ttk.Button(tab, text="执行步进", command=self.step_move).grid(row=3, column=4, padx=8, pady=4)
        ttk.Label(tab, text="负值按 int16 补码发送，首次请只用小角度验证").grid(row=3, column=5, sticky="w", padx=6)

        ttk.Button(tab, text="顺时针周扫", command=lambda: self.motion_send(Protocol.continuous(True), "顺时针周扫")).grid(row=4, column=0, columnspan=2, padx=4, pady=8)
        ttk.Button(tab, text="逆时针周扫", command=lambda: self.motion_send(Protocol.continuous(False), "逆时针周扫")).grid(row=4, column=2, columnspan=2, padx=4, pady=8)

    def _build_scan_tab(self, notebook: ttk.Notebook) -> None:
        tab = ttk.Frame(notebook, padding=10)
        notebook.add(tab, text="空间扫描")
        labels = ["方位起点", "方位终点", "俯仰起点", "俯仰终点"]
        defaults = ["10.0", "20.0", "0.0", "5.0"]
        self.scan_vars = [tk.StringVar(value=value) for value in defaults]
        for col, (label, variable) in enumerate(zip(labels, self.scan_vars)):
            ttk.Label(tab, text=label).grid(row=0, column=col, padx=6)
            ttk.Entry(tab, textvariable=variable, width=10).grid(row=1, column=col, padx=6, pady=4)
        ttk.Button(tab, text="开始空间扫描", command=self.spatial_scan).grid(row=1, column=4, padx=16)
        ttk.Button(tab, text="停止", command=self.stop_motion).grid(row=1, column=5, padx=4)
        ttk.Label(tab, text="限制：方位 0～360°，俯仰 0～90°；固件跨零扫描能力未确认。", foreground="#7A5200").grid(
            row=2, column=0, columnspan=6, sticky="w", pady=(8, 0)
        )

    def _build_maintenance_tab(self, notebook: ttk.Notebook) -> None:
        tab = ttk.Frame(notebook, padding=10)
        notebook.add(tab, text="维护与待验证功能")
        ttk.Label(tab, text="速度设置帧长度在协议中存在矛盾，默认使用按字段数计算的 0x0B。", foreground="#A00000").grid(
            row=0, column=0, columnspan=6, sticky="w", pady=(0, 8)
        )
        ttk.Label(tab, text="方位速度").grid(row=1, column=0)
        self.speed_az = tk.StringVar(value="5.0")
        ttk.Entry(tab, textvariable=self.speed_az, width=9).grid(row=1, column=1)
        ttk.Label(tab, text="俯仰速度").grid(row=1, column=2)
        self.speed_el = tk.StringVar(value="5.0")
        ttk.Entry(tab, textvariable=self.speed_el, width=9).grid(row=1, column=3)
        self.speed_length = tk.StringVar(value="0x0B")
        ttk.Combobox(tab, textvariable=self.speed_length, values=("0x0B", "0x09"), width=7, state="readonly").grid(row=1, column=4, padx=6)
        self.experimental_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(tab, text="我已确认按待验证协议发送", variable=self.experimental_var).grid(row=2, column=0, columnspan=3, sticky="w", pady=8)
        ttk.Button(tab, text="设置速度", command=self.set_speed).grid(row=2, column=3, padx=8)
        ttk.Button(tab, text="查询当前设置", command=lambda: self.send(Protocol.query_settings(), "查询当前设置")).grid(row=2, column=4, padx=8)

        ttk.Separator(tab, orient="horizontal").grid(row=3, column=0, columnspan=6, sticky="ew", pady=8)
        ttk.Button(tab, text="设置方位零点", command=lambda: self.set_zero(True)).grid(row=4, column=0, columnspan=2, padx=5)
        ttk.Button(tab, text="设置俯仰零点", command=lambda: self.set_zero(False)).grid(row=4, column=2, columnspan=2, padx=5)
        ttk.Button(tab, text="设备复位", command=self.reset_device).grid(row=4, column=4, padx=5)

    def log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{timestamp}] {message}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def connect(self) -> None:
        if self.session.connected:
            self.disconnect(send_stop=True)
            return
        try:
            if self.demo:
                assert isinstance(self.session, SerialSession)
                self.session.connect("演示模式", demo=True)
                endpoint = "演示模式"
                log_message = "已连接演示设备"
            else:
                assert isinstance(self.session, UdpSession)
                gateway_host = self.gateway_var.get().strip()
                local_host = self.local_var.get().strip()
                if not gateway_host or not local_host:
                    raise ValueError("RK3568 和本机 IPv4 地址不能为空")
                gateway_port = int(self.gateway_port_var.get())
                local_port = int(self.local_port_var.get())
                self.session.connect(gateway_host, gateway_port, local_host, local_port)
                endpoint = f"{gateway_host}:{gateway_port}"
                assert self.session.local is not None
                log_message = (
                    f"UDP 已连接 RK3568 {endpoint}，"
                    f"本机绑定 {self.session.local[0]}:{self.session.local[1]}"
                )
            self.connection_var.set(f"已连接：{endpoint}")
            self.connect_button.configure(text="断开")
            self.current_status = None
            self.last_status_time = None
            self.log(log_message)
            self.send(Protocol.query_status(), "连接后查询状态")
        except Exception as exc:
            self.session.disconnect()
            messagebox.showerror("连接失败", str(exc))

    def disconnect(self, send_stop: bool) -> None:
        if send_stop and self.session.connected:
            try:
                self.session.send(Protocol.stop())
                time.sleep(0.05)
            except Exception as exc:
                self.log(f"断开前停止命令发送失败：{exc}")
        self.session.disconnect()
        self.connection_var.set("未连接")
        self.connect_button.configure(text="连接")
        self.log("连接已断开")

    def send(self, frame: bytes, description: str) -> None:
        try:
            self.session.send(frame)
            self.log(f"命令：{description}")
        except Exception as exc:
            messagebox.showerror("发送失败", str(exc))

    def motion_send(self, frame: bytes, description: str) -> None:
        if self.fault_status.get() not in ("正常", "未收到状态"):
            messagebox.showerror("存在故障", "当前故障未清除，禁止发送运动命令。")
            return
        if self.last_status_time is None and not messagebox.askyesno(
            "尚无有效状态", "尚未收到有效状态帧。确认机械范围内无人、无障碍物，并仍要发送该运动命令吗？"
        ):
            return
        try:
            self.session.send(frame)
            self.last_motion_time = time.monotonic()
            self.watchdog_stopped = False
            self.log(f"运动命令：{description}")
        except Exception as exc:
            messagebox.showerror("发送失败", str(exc))

    def stop_motion(self, automatic_reason: Optional[str] = None) -> None:
        try:
            self.session.send(Protocol.stop())
            self.last_motion_time = None
            self.log("停止并锁定" + (f"（{automatic_reason}）" if automatic_reason else ""))
        except Exception as exc:
            if automatic_reason:
                self.log(f"自动停止发送失败：{exc}")
            else:
                messagebox.showerror("停止失败", str(exc))

    @staticmethod
    def _number(variable: tk.StringVar, name: str) -> float:
        try:
            return float(variable.get())
        except ValueError as exc:
            raise ValueError(f"{name}必须是数字") from exc

    def point_move(self) -> None:
        try:
            azimuth = self._number(self.point_az, "方位角")
            elevation = self._number(self.point_el, "俯仰角")
            self.motion_send(Protocol.point(azimuth, elevation), f"定点至 方位 {azimuth:.1f}° / 俯仰 {elevation:.1f}°")
        except ValueError as exc:
            messagebox.showerror("参数错误", str(exc))

    def step_move(self) -> None:
        try:
            azimuth = self._number(self.step_az, "方位步进")
            elevation = self._number(self.step_el, "俯仰步进")
            if self.current_status is not None:
                target_elevation = self.current_status.elevation_deg + elevation
                if not ELEVATION_MIN <= target_elevation <= ELEVATION_MAX:
                    raise ValueError(
                        f"当前俯仰 {self.current_status.elevation_deg:.1f}°，"
                        f"步进后将达到 {target_elevation:.1f}°，超出 0～90°机械范围"
                    )
            frame = Protocol.azimuth_step(azimuth) if elevation == 0 else Protocol.step(azimuth, elevation)
            self.motion_send(frame, f"步进 方位 {azimuth:+.1f}° / 俯仰 {elevation:+.1f}°")
        except ValueError as exc:
            messagebox.showerror("参数错误", str(exc))

    def spatial_scan(self) -> None:
        try:
            values = [self._number(variable, name) for variable, name in zip(self.scan_vars, ["方位起点", "方位终点", "俯仰起点", "俯仰终点"])]
            frame = Protocol.spatial_scan(*values)
            if messagebox.askyesno("确认空间扫描", "确认扫描范围内无人、无线缆和其他障碍物？"):
                self.motion_send(frame, f"空间扫描 {values}")
        except ValueError as exc:
            messagebox.showerror("参数错误", str(exc))

    def set_speed(self) -> None:
        if not self.experimental_var.get():
            messagebox.showwarning("待验证命令", "请先确认已理解速度帧长度字段尚未实机验证。")
            return
        try:
            azimuth = self._number(self.speed_az, "方位速度")
            elevation = self._number(self.speed_el, "俯仰速度")
            length = int(self.speed_length.get(), 16)
            frame = Protocol.set_speed(azimuth, elevation, length)
            if messagebox.askyesno("确认速度设置", f"发送方位 {azimuth:.1f}°/s、俯仰 {elevation:.1f}°/s、长度字段 {self.speed_length.get()}？"):
                self.send(frame, f"设置速度 方位 {azimuth:.1f} / 俯仰 {elevation:.1f}，LEN={self.speed_length.get()}")
        except ValueError as exc:
            messagebox.showerror("参数错误", str(exc))

    def set_zero(self, azimuth_axis: bool) -> None:
        axis = "方位" if azimuth_axis else "俯仰"
        if messagebox.askyesno("高风险操作", f"设置{axis}零点会改变坐标基准。确认机械姿态正确并继续？"):
            self.send(Protocol.set_zero(azimuth_axis), f"设置{axis}零点")

    def reset_device(self) -> None:
        if not self.session.connected:
            messagebox.showerror("尚未连接", "请先连接转台串口。")
            return
        if not messagebox.askyesno(
            "确认设备复位",
            "程序将先发送停止命令，再发送设备复位命令。复位可能重新初始化驱动状态，确认继续？",
        ):
            return
        try:
            self.session.send(Protocol.stop())
            self.last_motion_time = None
            self.log("设备复位前停止并锁定")
            self.after(150, self._send_reset_frame)
        except Exception as exc:
            messagebox.showerror("复位失败", str(exc))

    def _send_reset_frame(self) -> None:
        if not self.session.connected:
            return
        try:
            self.session.send(Protocol.reset())
            self.last_status_time = None
            self.log("设备复位命令已发送")
            self.after(1000, self._query_after_reset)
        except Exception as exc:
            messagebox.showerror("复位失败", str(exc))

    def _query_after_reset(self) -> None:
        if self.session.connected:
            self.send(Protocol.query_status(), "复位后查询状态")

    def poll_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "tx":
                    self.log(f"TX  {hex_text(payload)}")
                elif kind == "raw_rx":
                    self.log(f"RX  {hex_text(payload)}")
                elif kind == "frame":
                    self.handle_frame(payload)
                elif kind == "serial_error":
                    self.log(f"串口错误：{payload}")
                    self.disconnect(send_stop=False)
                elif kind == "network_error":
                    self.log(f"UDP 网络错误：{payload}")
                    self.disconnect(send_stop=False)
                elif kind == "protocol_error":
                    self.log(f"丢弃无效 UDP 数据报：{payload}")
        except queue.Empty:
            pass
        self.after(50, self.poll_events)

    def handle_frame(self, frame: bytes) -> None:
        if len(frame) in (15, 16) and frame[3] in (0x02, 0x07):
            try:
                status = parse_status(frame)
            except ValueError as exc:
                self.log(f"状态帧解析失败：{exc}")
                return
            first_status = self.current_status is None
            self.current_status = status
            self.last_status_time = time.monotonic()
            self.az_status.set(f"{status.azimuth_deg:.1f}°")
            self.el_status.set(f"{status.elevation_deg:.1f}°")
            self.az_speed_status.set(f"{status.azimuth_speed_dps:.1f}°/s")
            self.el_speed_status.set(f"{status.elevation_speed_dps:.1f}°/s")
            if first_status:
                self.point_az.set(f"{status.azimuth_deg:.1f}")
                self.point_el.set(f"{status.elevation_deg:.1f}")
            if status.fault:
                self.fault_status.set("；".join(status.faults) or f"未知故障 0x{status.fault:02X}")
                if not self.fault_stopped:
                    self.fault_stopped = True
                    self.stop_motion("检测到故障")
            else:
                self.fault_status.set("正常")
                self.fault_stopped = False
            self.log(
                f"状态：方位 {status.azimuth_deg:.1f}°，俯仰 {status.elevation_deg:.1f}°，"
                f"速度 {status.azimuth_speed_dps:.1f}/{status.elevation_speed_dps:.1f}°/s，故障 0x{status.fault:02X}"
            )
        else:
            self.log(f"有效应答帧：CMD=0x{frame[3]:02X}，{hex_text(frame)}")

    def watchdog_tick(self) -> None:
        if (
            self.watchdog_var.get()
            and self.session.connected
            and self.last_motion_time is not None
            and not self.watchdog_stopped
            and time.monotonic() - self.last_motion_time > 1.0
            and (self.last_status_time is None or self.last_status_time < self.last_motion_time)
        ):
            self.watchdog_stopped = True
            self.stop_motion("运动后 1 秒未收到有效状态")
        self.after(200, self.watchdog_tick)

    def save_log(self) -> None:
        path = filedialog.asksaveasfilename(
            title="保存通信日志",
            defaultextension=".log",
            initialfile=f"turntable_{datetime.now():%Y%m%d_%H%M%S}.log",
            filetypes=(("日志文件", "*.log"), ("文本文件", "*.txt"), ("所有文件", "*.*")),
        )
        if path:
            Path(path).write_text(self.log_text.get("1.0", "end-1c"), encoding="utf-8")

    def clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def on_close(self) -> None:
        if self.session.connected:
            self.disconnect(send_stop=True)
        self.destroy()


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="HZZT-2-3kg 双轴伺服转台网络控制器")
    parser.add_argument("--demo", action="store_true", help="演示模式，不访问 RK3568")
    parser.add_argument("--gateway", default=DEFAULT_GATEWAY_HOST, help="RK3568 IPv4 地址")
    parser.add_argument("--gateway-port", type=int, default=DEFAULT_GATEWAY_PORT)
    parser.add_argument("--local", default=DEFAULT_LOCAL_HOST, help="上位机有线 IPv4 地址")
    parser.add_argument("--local-port", type=int, default=DEFAULT_LOCAL_PORT)
    parser.add_argument("--no-auto-connect", action="store_true", help="启动后不自动连接")
    args = parser.parse_args(argv)
    app = TurntableApp(
        demo=args.demo,
        gateway_host=args.gateway,
        gateway_port=args.gateway_port,
        local_host=args.local,
        local_port=args.local_port,
        auto_connect=not args.no_auto_connect,
    )
    style = ttk.Style(app)
    if "vista" in style.theme_names():
        style.theme_use("vista")
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
