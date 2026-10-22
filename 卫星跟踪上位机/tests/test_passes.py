import unittest
from datetime import datetime, timedelta, timezone

from satellite_tracker.orbit import LookAngle
from satellite_tracker.passes import predict_passes


class SyntheticOrbit:
    """One pass above 0° from t=40 s to t=60 s, peaking at 10°."""

    def __init__(self, epoch: datetime) -> None:
        self.epoch = epoch

    def look_angle(self, when=None) -> LookAngle:
        seconds = (when - self.epoch).total_seconds()
        elevation = 10.0 - abs(seconds - 50.0)
        return LookAngle(azimuth_deg=seconds % 360.0, elevation_deg=elevation, range_km=500.0)


class PassPredictionTests(unittest.TestCase):
    def test_threshold_crossing_and_maximum_are_refined(self) -> None:
        epoch = datetime(2026, 1, 1, tzinfo=timezone.utc)
        passes = predict_passes(
            SyntheticOrbit(epoch),
            epoch,
            hours=120 / 3600,
            min_elevation_deg=0.0,
            step_seconds=13.0,
        )
        self.assertEqual(len(passes), 1)
        item = passes[0]
        self.assertAlmostEqual((item.aos - epoch).total_seconds(), 40.0, delta=0.01)
        self.assertAlmostEqual((item.los - epoch).total_seconds(), 60.0, delta=0.01)
        self.assertAlmostEqual(item.max_elevation_deg, 10.0, delta=0.01)
        self.assertAlmostEqual((item.max_elevation_time - epoch).total_seconds(), 50.0, delta=0.01)

    def test_invalid_threshold_is_rejected(self) -> None:
        epoch = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with self.assertRaises(ValueError):
            predict_passes(SyntheticOrbit(epoch), epoch, min_elevation_deg=90.0)


if __name__ == "__main__":
    unittest.main()
