import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tle_calculator import HAVE_SGP4, TLECalculator


ISS_TLE = (
    "1 25544U 98067A   20312.55704213  .00002455  00000-0  43744-4 0  9997",
    "2 25544  51.6449 149.0584 0001918  98.1837  53.6098 15.47650388246570",
)


@unittest.skipUnless(HAVE_SGP4, "sgp4 not installed")
class TLETests(unittest.TestCase):
    def test_az_el_ranges(self):
        calc = TLECalculator(
            ISS_TLE[0], ISS_TLE[1], lat_deg=30.0, lon_deg=120.0, alt_m=50.0
        )
        az, el = calc.compute_az_el()
        self.assertGreaterEqual(az, 0.0)
        self.assertLess(az, 360.0)
        self.assertGreaterEqual(el, -90.0)
        self.assertLessEqual(el, 90.0)

    def test_missing_line_numbers_are_accepted(self):
        calc = TLECalculator(
            ISS_TLE[0][2:],
            ISS_TLE[1][2:],
            lat_deg=30.0,
            lon_deg=120.0,
            alt_m=50.0,
        )
        az, el = calc.compute_az_el()
        self.assertGreaterEqual(az, 0.0)
        self.assertLess(az, 360.0)
        self.assertGreaterEqual(el, -90.0)
        self.assertLessEqual(el, 90.0)


if __name__ == "__main__":
    unittest.main()
