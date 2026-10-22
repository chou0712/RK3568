import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from protocol import (
    ProtocolError,
    build_point_command,
    build_status_query,
    build_stop_command,
    build_track_command,
    checksum_sum8,
    parse_status_frame,
)


class StatusQueryTests(unittest.TestCase):
    def test_status_query_frame(self):
        self.assertEqual(
            build_status_query(), bytes.fromhex("AA 06 01 02 01 B4")
        )

    def test_checksum_sum8(self):
        self.assertEqual(checksum_sum8(bytes.fromhex("AA 06 01 02 01")), 0xB4)


class MotionCommandTests(unittest.TestCase):
    def test_point_command(self):
        self.assertEqual(
            build_point_command(10.0, 5.0),
            bytes.fromhex("AA 0C 01 06 02 01 00 00 64 00 32 56"),
        )

    def test_track_command(self):
        self.assertEqual(
            build_track_command(12.3, 4.5),
            bytes.fromhex("AA 0C 01 06 03 01 00 00 7B 00 2D 69"),
        )

    def test_stop_command(self):
        self.assertEqual(
            build_stop_command(),
            bytes.fromhex("AA 0C 01 06 04 01 00 00 00 00 00 C2"),
        )


class StatusParseTests(unittest.TestCase):
    def test_real_status_sample(self):
        frame = bytes.fromhex("AA 10 01 07 00 00 64 00 31 00 36 00 00 00 8D")
        status = parse_status_frame(frame)
        self.assertAlmostEqual(status.az_deg, 10.0)
        self.assertAlmostEqual(status.el_deg, 4.9)
        self.assertAlmostEqual(status.az_speed_dps, 5.4)
        self.assertAlmostEqual(status.el_speed_dps, 0.0)
        self.assertEqual(status.fault, 0)

    def test_16_byte_status_variant(self):
        body = bytes.fromhex("AA 10 01 07 00 00 64 00 31 00 36 00 00 00 00")
        frame = body + bytes((checksum_sum8(body),))
        status = parse_status_frame(frame)
        self.assertAlmostEqual(status.az_deg, 10.0)
        self.assertAlmostEqual(status.el_deg, 4.9)

    def test_bad_checksum(self):
        frame = bytearray(
            bytes.fromhex("AA 10 01 07 00 00 64 00 31 00 36 00 00 00 8D")
        )
        frame[-1] ^= 0xFF
        with self.assertRaises(ProtocolError):
            parse_status_frame(bytes(frame))


if __name__ == "__main__":
    unittest.main()
