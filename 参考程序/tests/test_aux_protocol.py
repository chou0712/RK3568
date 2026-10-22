import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aux_protocol import (
    TYPE_IMU_STATE,
    TYPE_TLE,
    build_frame,
    decode_imu,
    decode_tle,
    encode_imu,
    encode_tle,
    parse_frame,
)


class AuxProtocolTests(unittest.TestCase):
    def test_tle_roundtrip(self):
        line1 = "1 25544U 98067A   20312.55704213  .00002455  00000-0  43744-4 0  9997"
        line2 = "2 25544  51.6449 149.0584 0001918  98.1837  53.6098 15.47650388246570"
        parsed = parse_frame(build_frame(TYPE_TLE, encode_tle(line1, line2)))
        out1, out2 = decode_tle(parsed.data)
        self.assertEqual(out1, line1)
        self.assertEqual(out2, line2)

    def test_imu_roundtrip(self):
        parsed = parse_frame(
            build_frame(TYPE_IMU_STATE, encode_imu(15.5, -3.25, 90.0))
        )
        yaw, pitch, roll = decode_imu(parsed.data)
        self.assertAlmostEqual(yaw, 15.5)
        self.assertAlmostEqual(pitch, -3.25)
        self.assertAlmostEqual(roll, 90.0)


if __name__ == "__main__":
    unittest.main()
