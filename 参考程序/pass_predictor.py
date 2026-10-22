"""Predict satellite passes from a TLE and an observer location."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from tle_calculator import TLECalculator


@dataclass
class PassInfo:
    start: datetime
    end: datetime
    max_elevation_deg: float
    max_elevation_time: datetime
    max_azimuth_deg: float

    @property
    def duration_seconds(self) -> float:
        return max(0.0, (self.end - self.start).total_seconds())


def _bisect_crossing(
    calc: TLECalculator,
    t_below: datetime,
    t_above: datetime,
    threshold_deg: float,
) -> datetime:
    """Return the time on the above-threshold side of a crossing."""
    for _ in range(14):
        mid = t_below + (t_above - t_below) / 2
        _, elevation = calc.compute_az_el(mid)
        if elevation >= threshold_deg:
            t_above = mid
        else:
            t_below = mid
    return t_above


def _refine_max(
    calc: TLECalculator,
    around: datetime,
    seconds: int = 20,
) -> tuple[float, float, datetime]:
    best_el = -90.0
    best_az = 0.0
    best_time = around
    for offset in range(-seconds, seconds + 1):
        t = around + timedelta(seconds=offset)
        az, el = calc.compute_az_el(t)
        if el > best_el:
            best_el = el
            best_az = az
            best_time = t
    return best_el, best_az, best_time


def predict_passes(
    tle_line1: str,
    tle_line2: str,
    lat_deg: float,
    lon_deg: float,
    alt_m: float,
    start: Optional[datetime] = None,
    hours: float = 24.0,
    step_seconds: float = 20.0,
    min_elevation_deg: float = 0.0,
) -> list[PassInfo]:
    """Return passes above *min_elevation_deg* in the requested window."""
    calc = TLECalculator(
        tle_line1, tle_line2, lat_deg, lon_deg, alt_m
    )
    if start is None:
        start = datetime.now(timezone.utc)
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    start = start.astimezone(timezone.utc)
    window_end = start + timedelta(hours=hours)
    step = timedelta(seconds=max(1.0, float(step_seconds)))

    az, el = calc.compute_az_el(start)
    in_pass = el >= min_elevation_deg
    pass_start = start if in_pass else None
    max_el = el
    max_az = az
    max_time = start

    passes: list[PassInfo] = []
    prev_t = start
    prev_az = az
    prev_el = el
    t = start

    while t < window_end:
        t_next = min(t + step, window_end)
        az_next, el_next = calc.compute_az_el(t_next)

        if not in_pass and el_next >= min_elevation_deg:
            pass_start = _bisect_crossing(
                calc, prev_t, t_next, min_elevation_deg
            )
            in_pass = True
            max_el, max_az, max_time = el_next, az_next, t_next
        elif in_pass and el_next < min_elevation_deg:
            pass_end = _bisect_crossing(
                calc, t_next, prev_t, min_elevation_deg
            )
            max_el, max_az, max_time = _refine_max(calc, max_time)
            passes.append(
                PassInfo(
                    start=pass_start,
                    end=pass_end,
                    max_elevation_deg=max_el,
                    max_elevation_time=max_time,
                    max_azimuth_deg=max_az,
                )
            )
            in_pass = False
            pass_start = None
        elif in_pass and el_next > max_el:
            max_el, max_az, max_time = el_next, az_next, t_next

        prev_t, prev_az, prev_el = t_next, az_next, el_next
        t = t_next

    if in_pass and pass_start is not None:
        max_el, max_az, max_time = _refine_max(calc, max_time)
        passes.append(
            PassInfo(
                start=pass_start,
                end=window_end,
                max_elevation_deg=max_el,
                max_elevation_time=max_time,
                max_azimuth_deg=max_az,
            )
        )
    return passes
