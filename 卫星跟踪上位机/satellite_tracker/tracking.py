"""Pure tracking state machine, separated from Tk and UDP for testability."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from .orbit import LookAngle
from .passes import OrbitLike, PassWindow


class TrackingState(str, Enum):
    IDLE = "未启动"
    WAITING = "等待过境"
    TRACKING = "跟踪中"
    COMPLETED = "已完成"
    STOPPED = "已停止"
    ERROR = "异常停止"


class TrackingAction(str, Enum):
    NONE = "none"
    TARGET = "target"
    STOP = "stop"


@dataclass(frozen=True)
class TrackingUpdate:
    state: TrackingState
    action: TrackingAction
    look: LookAngle | None
    message: str


class TrackingEngine:
    def __init__(
        self,
        orbit: OrbitLike,
        window: PassWindow,
        min_elevation_deg: float,
    ) -> None:
        self.orbit = orbit
        self.window = window
        self.min_elevation_deg = float(min_elevation_deg)
        self.state = TrackingState.WAITING
        self._movement_sent = False

    def tick(self, now: datetime) -> TrackingUpdate:
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now = now.astimezone(timezone.utc)
        if self.state in (TrackingState.STOPPED, TrackingState.ERROR):
            return TrackingUpdate(self.state, TrackingAction.NONE, None, self.state.value)
        if self.state == TrackingState.COMPLETED:
            return TrackingUpdate(self.state, TrackingAction.NONE, None, "任务已完成")
        if now < self.window.aos:
            self.state = TrackingState.WAITING
            remaining = max(0, int((self.window.aos - now).total_seconds()))
            return TrackingUpdate(
                self.state, TrackingAction.NONE, None, f"等待过境，距开始 {remaining} 秒"
            )
        if now > self.window.los:
            self.state = TrackingState.COMPLETED
            action = TrackingAction.STOP if self._movement_sent else TrackingAction.NONE
            self._movement_sent = False
            return TrackingUpdate(self.state, action, None, "卫星已离境，任务完成")

        look = self.orbit.look_angle(now)
        if look.elevation_deg < self.min_elevation_deg or look.elevation_deg > 90.0:
            self.state = TrackingState.WAITING
            action = TrackingAction.STOP if self._movement_sent else TrackingAction.NONE
            self._movement_sent = False
            return TrackingUpdate(
                self.state,
                action,
                look,
                f"卫星未达到跟踪俯仰阈值 {self.min_elevation_deg:.1f}°",
            )

        self.state = TrackingState.TRACKING
        self._movement_sent = True
        return TrackingUpdate(
            self.state,
            TrackingAction.TARGET,
            look,
            f"实时跟踪 方位 {look.azimuth_deg:.2f}° / 俯仰 {look.elevation_deg:.2f}°",
        )

    def stop(self, error: bool = False) -> TrackingAction:
        action = TrackingAction.STOP if self._movement_sent else TrackingAction.NONE
        self._movement_sent = False
        self.state = TrackingState.ERROR if error else TrackingState.STOPPED
        return action
