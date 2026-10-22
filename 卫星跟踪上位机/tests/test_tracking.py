import unittest
from datetime import datetime, timedelta, timezone

from satellite_tracker.orbit import LookAngle
from satellite_tracker.passes import PassWindow
from satellite_tracker.tracking import TrackingAction, TrackingEngine, TrackingState


class FixedOrbit:
    def __init__(self, elevation: float = 25.0) -> None:
        self.elevation = elevation

    def look_angle(self, when=None) -> LookAngle:
        return LookAngle(azimuth_deg=123.4, elevation_deg=self.elevation, range_km=700.0)


class TrackingEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
        self.window = PassWindow(
            aos=self.now + timedelta(seconds=10),
            los=self.now + timedelta(seconds=30),
            max_elevation_deg=30.0,
            max_elevation_time=self.now + timedelta(seconds=20),
            aos_azimuth_deg=100.0,
            los_azimuth_deg=200.0,
        )

    def test_wait_track_and_complete(self) -> None:
        engine = TrackingEngine(FixedOrbit(), self.window, 10.0)
        waiting = engine.tick(self.now)
        self.assertEqual(waiting.state, TrackingState.WAITING)
        self.assertEqual(waiting.action, TrackingAction.NONE)

        tracking = engine.tick(self.now + timedelta(seconds=20))
        self.assertEqual(tracking.state, TrackingState.TRACKING)
        self.assertEqual(tracking.action, TrackingAction.TARGET)

        completed = engine.tick(self.now + timedelta(seconds=31))
        self.assertEqual(completed.state, TrackingState.COMPLETED)
        self.assertEqual(completed.action, TrackingAction.STOP)

    def test_below_threshold_never_moves(self) -> None:
        engine = TrackingEngine(FixedOrbit(5.0), self.window, 10.0)
        update = engine.tick(self.now + timedelta(seconds=20))
        self.assertEqual(update.state, TrackingState.WAITING)
        self.assertEqual(update.action, TrackingAction.NONE)


if __name__ == "__main__":
    unittest.main()
