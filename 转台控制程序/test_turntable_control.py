import queue
import socket
import time
import unittest

from turntable_control import (
    FrameParser,
    Protocol,
    UdpSession,
    checksum_sum8,
    parse_status,
    validate_udp_frame,
)


class ProtocolTests(unittest.TestCase):
    def assert_hex(self, actual: bytes, expected: str) -> None:
        self.assertEqual(actual.hex(" ").upper(), expected)
        self.assertEqual(checksum_sum8(actual[:-1]), actual[-1])

    def test_documented_frames(self) -> None:
        cases = [
            (Protocol.reset(), "AA 06 01 01 01 B3"),
            (Protocol.query_status(), "AA 06 01 02 01 B4"),
            (Protocol.query_driver(), "AA 06 01 03 01 B5"),
            (Protocol.query_fault(), "AA 06 01 04 01 B6"),
            (Protocol.query_settings(), "AA 06 01 05 01 B7"),
            (Protocol.continuous(True), "AA 0C 01 06 01 01 00 00 00 00 00 BF"),
            (Protocol.continuous(False), "AA 0C 01 06 01 01 01 00 00 00 00 C0"),
            (Protocol.point(10.0, 5.0), "AA 0C 01 06 02 01 00 00 64 00 32 56"),
            (Protocol.stop(), "AA 0C 01 06 04 01 00 00 00 00 00 C2"),
            (Protocol.spatial_scan(10.0, 20.0, 0.0, 5.0), "AA 0F 01 06 05 01 00 64 00 C8 00 00 00 32 24"),
            (Protocol.step(1.0, 0.5), "AA 0B 01 06 06 01 00 0A 00 05 D2"),
            (Protocol.azimuth_step(1.0), "AA 09 01 06 06 01 00 0A CB"),
            (Protocol.set_zero(True), "AA 07 01 08 01 01 BC"),
            (Protocol.set_zero(False), "AA 07 01 08 02 01 BD"),
        ]
        for actual, expected in cases:
            with self.subTest(expected=expected):
                self.assert_hex(actual, expected)

    def test_unverified_speed_candidate(self) -> None:
        self.assert_hex(Protocol.set_speed(5.0, 5.0), "AA 0B 01 06 07 01 00 32 00 32 28")

    def test_range_checks(self) -> None:
        with self.assertRaises(ValueError):
            Protocol.point(10.0, 91.0)
        with self.assertRaises(ValueError):
            Protocol.set_speed(31.0, 5.0)
        with self.assertRaises(ValueError):
            Protocol.spatial_scan(20.0, 10.0, 0.0, 5.0)

    def test_negative_step_uses_twos_complement(self) -> None:
        frame = Protocol.azimuth_step(-1.0)
        self.assertEqual(frame[6:8], bytes.fromhex("FF F6"))
        self.assertEqual(checksum_sum8(frame[:-1]), frame[-1])

class ParserTests(unittest.TestCase):
    SAMPLE = bytes.fromhex("AA 10 01 07 00 00 64 00 31 00 36 00 00 00 8D")

    def test_parse_documented_status(self) -> None:
        status = parse_status(self.SAMPLE)
        self.assertEqual(status.azimuth_deg, 10.0)
        self.assertEqual(status.elevation_deg, 4.9)
        self.assertEqual(status.azimuth_speed_dps, 5.4)
        self.assertEqual(status.elevation_speed_dps, 0.0)
        self.assertEqual(status.fault, 0)

    def test_stream_resynchronization_and_split_reads(self) -> None:
        parser = FrameParser()
        self.assertEqual(parser.feed(b"\x95\xD7" + self.SAMPLE[:7]), [])
        self.assertEqual(parser.feed(self.SAMPLE[7:]), [self.SAMPLE])

    def test_two_frames_in_one_read(self) -> None:
        parser = FrameParser()
        query = Protocol.query_status()
        self.assertEqual(parser.feed(query + self.SAMPLE), [query, self.SAMPLE])

    def test_status_with_negative_speed_and_fault(self) -> None:
        body = bytes.fromhex("AA 10 01 07 00 00 0A 00 14 FF F6 00 05 05 00")
        frame = body + bytes((checksum_sum8(body),))
        status = parse_status(frame)
        self.assertEqual(status.azimuth_speed_dps, -1.0)
        self.assertEqual(status.elevation_speed_dps, 0.5)
        self.assertEqual(status.fault, 0x05)
        self.assertEqual(len(status.faults), 2)


class UdpTransportTests(unittest.TestCase):
    STATUS = bytes.fromhex("AA 10 01 07 00 00 64 00 31 00 36 00 00 00 8D")
    SETTINGS = bytes.fromhex("AA 0E 01 05 00 01 90 01 2C 7C")

    def test_udp_validation_accepts_protocol_length_exceptions(self) -> None:
        self.assertEqual(validate_udp_frame(self.STATUS), self.STATUS)
        self.assertEqual(validate_udp_frame(self.SETTINGS), self.SETTINGS)

    def test_udp_validation_rejects_bad_checksum(self) -> None:
        damaged = self.STATUS[:-1] + bytes((self.STATUS[-1] ^ 1,))
        with self.assertRaises(ValueError):
            validate_udp_frame(damaged)

    def test_udp_session_round_trip(self) -> None:
        gateway = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        gateway.bind(("127.0.0.1", 0))
        gateway.settimeout(2.0)
        events: queue.Queue[tuple[str, object]] = queue.Queue()
        session = UdpSession(events)
        try:
            session.connect("127.0.0.1", gateway.getsockname()[1], "127.0.0.1", 0)
            session.send(Protocol.query_status())
            request, peer = gateway.recvfrom(4096)
            self.assertEqual(request, Protocol.query_status())
            gateway.sendto(self.STATUS, peer)

            received_frame = None
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and received_frame is None:
                try:
                    kind, payload = events.get(timeout=0.2)
                except queue.Empty:
                    continue
                if kind == "frame":
                    received_frame = payload
            self.assertEqual(received_frame, self.STATUS)
        finally:
            session.disconnect()
            gateway.close()

if __name__ == "__main__":
    unittest.main()
