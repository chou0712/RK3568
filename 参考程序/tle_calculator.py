"""TLE propagation and Az/El calculation for the observer site."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Optional

try:
    from sgp4.api import Satrec, jday

    HAVE_SGP4 = True
except Exception:  # pragma: no cover - depends on environment
    HAVE_SGP4 = False


WGS84_A = 6378137.0
WGS84_E2 = 6.69437999014e-3


def _normalize_tle_line(line: str, expected_prefix: str) -> str:
    """Accept TLE lines with or without the leading line number."""
    text = (line or "").strip()
    if not text:
        raise ValueError(f"TLE 第{expected_prefix}行为空")
    if text.startswith(expected_prefix + " "):
        return text
    if text[0].isdigit():
        return f"{expected_prefix} {text}"
    raise ValueError(
        f"TLE 第{expected_prefix}行格式不正确，应以 {expected_prefix} 开头"
    )


class TLECalculator:
    """Compute look angles for a satellite from TLE and observer location."""

    def __init__(
        self,
        tle_line1: str,
        tle_line2: str,
        lat_deg: float,
        lon_deg: float,
        alt_m: float,
    ) -> None:
        if not HAVE_SGP4:
            raise RuntimeError(
                "sgp4 is not installed; run: pip install -r requirements.txt"
            )
        line1 = _normalize_tle_line(tle_line1, "1")
        line2 = _normalize_tle_line(tle_line2, "2")
        self.sat = Satrec.twoline2rv(line1, line2)
        self.lat_deg = float(lat_deg)
        self.lon_deg = float(lon_deg)
        self.alt_m = float(alt_m)

    def compute_az_el(
        self, when: Optional[datetime] = None
    ) -> tuple[float, float]:
        if when is None:
            when = datetime.now(timezone.utc)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        when_utc = when.astimezone(timezone.utc)

        jd, fr = jday(
            when_utc.year,
            when_utc.month,
            when_utc.day,
            when_utc.hour,
            when_utc.minute,
            when_utc.second + when_utc.microsecond / 1_000_000.0,
        )
        error, position_eci, _velocity = self.sat.sgp4(jd, fr)
        if error != 0:
            raise RuntimeError(f"sgp4 propagation error: {error}")

        jd_ut = jd + fr
        days_since_j2000 = jd_ut - 2451545.0
        gmst_deg = (280.46061837 + 360.98564736629 * days_since_j2000) % 360.0
        theta = math.radians(gmst_deg)

        x_ecef = (
            math.cos(theta) * position_eci[0]
            + math.sin(theta) * position_eci[1]
        ) * 1000.0
        y_ecef = (
            -math.sin(theta) * position_eci[0]
            + math.cos(theta) * position_eci[1]
        ) * 1000.0
        z_ecef = position_eci[2] * 1000.0
        sat_ecef = (x_ecef, y_ecef, z_ecef)

        obs_ecef = self._geodetic_to_ecef()
        delta = (
            sat_ecef[0] - obs_ecef[0],
            sat_ecef[1] - obs_ecef[1],
            sat_ecef[2] - obs_ecef[2],
        )
        east, north, up = self._enu_basis()

        east_comp = self._dot(delta, east)
        north_comp = self._dot(delta, north)
        up_comp = self._dot(delta, up)
        distance = math.sqrt(
            east_comp * east_comp + north_comp * north_comp + up_comp * up_comp
        )
        if distance <= 0:
            raise RuntimeError("satellite is at the observer position")

        az_deg = math.degrees(math.atan2(east_comp, north_comp)) % 360.0
        el_deg = math.degrees(math.asin(up_comp / distance))
        return az_deg, el_deg

    def _geodetic_to_ecef(self) -> tuple[float, float, float]:
        lat = math.radians(self.lat_deg)
        lon = math.radians(self.lon_deg)
        sin_lat = math.sin(lat)
        cos_lat = math.cos(lat)
        n_radius = WGS84_A / math.sqrt(
            1.0 - WGS84_E2 * sin_lat * sin_lat
        )
        x = (n_radius + self.alt_m) * cos_lat * math.cos(lon)
        y = (n_radius + self.alt_m) * cos_lat * math.sin(lon)
        z = (n_radius * (1.0 - WGS84_E2) + self.alt_m) * sin_lat
        return x, y, z

    def _enu_basis(self) -> tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]:
        lat = math.radians(self.lat_deg)
        lon = math.radians(self.lon_deg)
        sin_lat = math.sin(lat)
        cos_lat = math.cos(lat)
        sin_lon = math.sin(lon)
        cos_lon = math.cos(lon)
        east = (-sin_lon, cos_lon, 0.0)
        north = (-sin_lat * cos_lon, -sin_lat * sin_lon, cos_lat)
        up = (cos_lat * cos_lon, cos_lat * sin_lon, sin_lat)
        return east, north, up

    @staticmethod
    def _dot(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
        return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
