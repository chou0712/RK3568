import unittest

from satellite_tracker.protocol import (
    ProtocolError,
    build_point_command,
    build_status_query,
    build_stop_command,
    checksum_sum8,
    parse_status,
    target_reached,
    validate_frame,
)


class ProtocolTests(unittest.TestCase):
    def assert_frame(self, frame: bytes, expected: str) -> None:
        self.assertEqual(frame.hex(" ").upper(), expected)
        self.assertEqual(checksum_sum8(frame[:-1]), frame[-1])

    def test_documented_commands(self) -> None:
        self.assert_frame(build_status_query(), "AA 06 01 02 01 B4")
        self.assert_frame(build_stop_command(), "AA 0C 01 06 04 01 00 00 00 00 00 C2")
        self.assert_frame(
            build_point_command(10.0, 5.0),
            "AA 0C 01 06 02 01 00 00 64 00 32 56",
        )

    def test_point_range_is_checked(self) -> None:
        with self.assertRaises(ValueError):
            build_point_command(-0.1, 5.0)
        with self.assertRaises(ValueError):
            build_point_command(10.0, 90.1)

    def test_status_15_byte_variant(self) -> None:
        frame = bytes.fromhex("AA 10 01 07 00 00 64 00 31 00 36 00 00 00 8D")
        status = parse_status(frame)
        self.assertEqual(status.azimuth_deg, 10.0)
        self.assertEqual(status.elevation_deg, 4.9)
        self.assertEqual(status.azimuth_speed_dps, 5.4)
        self.assertEqual(status.fault, 0)

    def test_status_16_byte_variant_keeps_fault_at_index_13(self) -> None:
        body = bytes.fromhex("AA 10 01 07 00 00 64 00 31 00 36 00 00 05 00")
        frame = body + bytes((checksum_sum8(body),))
        status = parse_status(frame)
        self.assertEqual(status.fault, 5)
        self.assertEqual(len(status.faults), 2)

    def test_bad_checksum_is_rejected(self) -> None:
        valid = build_status_query()
        invalid = valid[:-1] + bytes((valid[-1] ^ 1,))
        with self.assertRaises(ProtocolError):
            validate_frame(invalid)

    def test_home_target_requires_position_and_stopped_axes(self) -> None:
        body = bytes.fromhex("AA 10 01 07 00 0E 10 00 01 00 00 00 00 00 00")
        status = parse_status(body + bytes((checksum_sum8(body),)))
        # 方位 360.0° 与 0.0° 是同一个方向，俯仰误差 0.1°。
        self.assertTrue(target_reached(status, 0.0, 0.0))

        moving_body = bytes.fromhex("AA 10 01 07 00 00 00 00 00 00 03 00 00 00 00")
        moving = parse_status(moving_body + bytes((checksum_sum8(moving_body),)))
        self.assertFalse(target_reached(moving, 0.0, 0.0))


if __name__ == "__main__":
    unittest.main()
