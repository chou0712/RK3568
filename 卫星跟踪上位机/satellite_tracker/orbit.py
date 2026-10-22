"""TLE propagation and observer-relative azimuth/elevation calculation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

try:
    from sgp4.api import Satrec, jday

    SGP4_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on deployment environment
    Satrec = None
    jday = None
    SGP4_AVAILABLE = False

WGS84_A_M = 6_378_137.0
WGS84_E2 = 6.69437999014e-3


@dataclass(frozen=True)
class Observer:
    latitude_deg: float
    longitude_deg: float
    altitude_m: float = 0.0

    def __post_init__(self) -> None:
        values = (self.latitude_deg, self.longitude_deg, self.altitude_m)
        if not all(math.isfinite(float(value)) for value in values):
            raise ValueError("观测站参数必须是有限数值")
        if not -90.0 <= self.latitude_deg <= 90.0:
            raise ValueError("纬度必须在 -90～90° 之间")
        if not -180.0 <= self.longitude_deg <= 180.0:
            raise ValueError("经度必须在 -180～180° 之间")


@dataclass(frozen=True)
class LookAngle:
    azimuth_deg: float
    elevation_deg: float
    range_km: float


def normalize_tle_line(line: str, expected_number: int) -> str:
    text = (line or "").strip()
    prefix = str(expected_number)
    if not text:
        raise ValueError(f"TLE 第{prefix}行为空")
    if text.startswith(prefix + " "):
        return text
    if text[0].isdigit():
        return f"{prefix} {text}"
    raise ValueError(f"TLE 第{prefix}行格式错误，应以 {prefix} 开头")


class TLEOrbit:
    def __init__(self, line1: str, line2: str, observer: Observer) -> None:
        if not SGP4_AVAILABLE:
            raise RuntimeError("缺少 sgp4 依赖，请先运行“安装依赖.bat”")
        self.line1 = normalize_tle_line(line1, 1)
        self.line2 = normalize_tle_line(line2, 2)
        self.observer = observer
        assert Satrec is not None
        self._satellite = Satrec.twoline2rv(self.line1, self.line2)

    def look_angle(self, when: datetime | None = None) -> LookAngle:
        if when is None:
            when = datetime.now(timezone.utc)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        when = when.astimezone(timezone.utc)

        assert jday is not None
        jd, fraction = jday(
            when.year,
            when.month,
            when.day,
            when.hour,
            when.minute,
            when.second + when.microsecond / 1_000_000.0,
        )
        error, position_eci_km, _velocity = self._satellite.sgp4(jd, fraction)
        if error:
            raise RuntimeError(f"SGP4 轨道外推失败，错误码 {error}")

        theta = math.radians(
            (280.46061837 + 360.98564736629 * (jd + fraction - 2451545.0))
            % 360.0
        )
        x_eci, y_eci, z_eci = position_eci_km
        satellite_ecef = (
            (math.cos(theta) * x_eci + math.sin(theta) * y_eci) * 1000.0,
            (-math.sin(theta) * x_eci + math.cos(theta) * y_eci) * 1000.0,
            z_eci * 1000.0,
        )
        observer_ecef = self._observer_ecef()
        delta = tuple(satellite_ecef[i] - observer_ecef[i] for i in range(3))

        latitude = math.radians(self.observer.latitude_deg)
        longitude = math.radians(self.observer.longitude_deg)
        sin_lat, cos_lat = math.sin(latitude), math.cos(latitude)
        sin_lon, cos_lon = math.sin(longitude), math.cos(longitude)
        east = (-sin_lon, cos_lon, 0.0)
        north = (-sin_lat * cos_lon, -sin_lat * sin_lon, cos_lat)
        up = (cos_lat * cos_lon, cos_lat * sin_lon, sin_lat)

        east_component = self._dot(delta, east)
        north_component = self._dot(delta, north)
        up_component = self._dot(delta, up)
        range_m = math.sqrt(sum(value * value for value in delta))
        if range_m <= 0.0:
            raise RuntimeError("卫星位置与观测站重合")
        azimuth = math.degrees(math.atan2(east_component, north_component)) % 360.0
        elevation = math.degrees(math.asin(up_component / range_m))
        return LookAngle(azimuth, elevation, range_m / 1000.0)

    def _observer_ecef(self) -> tuple[float, float, float]:
        latitude = math.radians(self.observer.latitude_deg)
        longitude = math.radians(self.observer.longitude_deg)
        sin_lat, cos_lat = math.sin(latitude), math.cos(latitude)
        radius = WGS84_A_M / math.sqrt(1.0 - WGS84_E2 * sin_lat * sin_lat)
        altitude = self.observer.altitude_m
        return (
            (radius + altitude) * cos_lat * math.cos(longitude),
            (radius + altitude) * cos_lat * math.sin(longitude),
            (radius * (1.0 - WGS84_E2) + altitude) * sin_lat,
        )

    @staticmethod
    def _dot(left: tuple[float, ...], right: tuple[float, ...]) -> float:
        return sum(a * b for a, b in zip(left, right))
