"""Visible-pass prediction with threshold crossing refinement."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from .orbit import LookAngle


class OrbitLike(Protocol):
    def look_angle(self, when: datetime | None = None) -> LookAngle: ...


@dataclass(frozen=True)
class PassWindow:
    aos: datetime
    los: datetime
    max_elevation_deg: float
    max_elevation_time: datetime
    aos_azimuth_deg: float
    los_azimuth_deg: float

    @property
    def duration_seconds(self) -> float:
        return max(0.0, (self.los - self.aos).total_seconds())


def _utc(when: datetime) -> datetime:
    if when.tzinfo is None:
        return when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc)


def _refine_crossing(
    orbit: OrbitLike,
    earlier: datetime,
    later: datetime,
    threshold_deg: float,
    rising: bool,
) -> datetime:
    for _ in range(18):
        middle = earlier + (later - earlier) / 2
        above = orbit.look_angle(middle).elevation_deg >= threshold_deg
        if above == rising:
            later = middle
        else:
            earlier = middle
    return later if rising else earlier


def _refine_maximum(
    orbit: OrbitLike, start: datetime, end: datetime
) -> tuple[float, datetime]:
    left, right = start, end
    # Elevation across one ordinary pass is unimodal. Ternary refinement avoids
    # locking the maximum to the coarse prediction step.
    for _ in range(28):
        third = (right - left) / 3
        t1, t2 = left + third, right - third
        if orbit.look_angle(t1).elevation_deg < orbit.look_angle(t2).elevation_deg:
            left = t1
        else:
            right = t2
    peak_time = left + (right - left) / 2
    return orbit.look_angle(peak_time).elevation_deg, peak_time


def predict_passes(
    orbit: OrbitLike,
    start: datetime,
    hours: float = 24.0,
    min_elevation_deg: float = 10.0,
    step_seconds: float = 20.0,
) -> list[PassWindow]:
    if not 0.0 <= min_elevation_deg < 90.0:
        raise ValueError("最低跟踪俯仰角必须在 0～90° 之间")
    if not 0.0 < hours <= 168.0:
        raise ValueError("预报时长必须在 0～168 小时之间")

    start = _utc(start)
    end = start + timedelta(hours=hours)
    step = timedelta(seconds=max(1.0, float(step_seconds)))
    previous_time = start
    previous_look = orbit.look_angle(start)
    in_pass = previous_look.elevation_deg >= min_elevation_deg
    pass_start = start if in_pass else None
    aos_azimuth = previous_look.azimuth_deg
    result: list[PassWindow] = []

    while previous_time < end:
        current_time = min(previous_time + step, end)
        current_look = orbit.look_angle(current_time)
        current_above = current_look.elevation_deg >= min_elevation_deg

        if not in_pass and current_above:
            pass_start = _refine_crossing(
                orbit, previous_time, current_time, min_elevation_deg, True
            )
            aos_azimuth = orbit.look_angle(pass_start).azimuth_deg
            in_pass = True
        elif in_pass and not current_above and pass_start is not None:
            pass_end = _refine_crossing(
                orbit, previous_time, current_time, min_elevation_deg, False
            )
            peak_elevation, peak_time = _refine_maximum(orbit, pass_start, pass_end)
            result.append(
                PassWindow(
                    aos=pass_start,
                    los=pass_end,
                    max_elevation_deg=peak_elevation,
                    max_elevation_time=peak_time,
                    aos_azimuth_deg=aos_azimuth,
                    los_azimuth_deg=orbit.look_angle(pass_end).azimuth_deg,
                )
            )
            pass_start = None
            in_pass = False

        previous_time, previous_look = current_time, current_look

    if in_pass and pass_start is not None:
        peak_elevation, peak_time = _refine_maximum(orbit, pass_start, end)
        result.append(
            PassWindow(
                aos=pass_start,
                los=end,
                max_elevation_deg=peak_elevation,
                max_elevation_time=peak_time,
                aos_azimuth_deg=aos_azimuth,
                los_azimuth_deg=orbit.look_angle(end).azimuth_deg,
            )
        )
    return result
