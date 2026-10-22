import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pass_predictor import predict_passes
from tle_calculator import HAVE_SGP4


ISS_TLE = (
    "1 25544U 98067A   26291.78980200  .00007771  00000+0  14938-3 0  9993",
    "2 25544  51.6325  48.4339 0006764 280.4294  79.5932 15.48929442590914",
)


@unittest.skipUnless(HAVE_SGP4, "sgp4 not installed")
class PassPredictorTests(unittest.TestCase):
    def test_predicts_at_least_one_pass(self):
        start = datetime(2026, 10, 19, 0, 0, tzinfo=timezone.utc)
        passes = predict_passes(
            ISS_TLE[0],
            ISS_TLE[1],
            30.0,
            120.0,
            0.0,
            start=start,
            hours=24.0,
            step_seconds=30.0,
        )
        self.assertGreaterEqual(len(passes), 1)
        for item in passes:
            self.assertLess(item.start, item.end)
            self.assertGreater(item.max_elevation_deg, 0.0)
            self.assertGreater(item.duration_seconds, 0.0)


if __name__ == "__main__":
    unittest.main()
