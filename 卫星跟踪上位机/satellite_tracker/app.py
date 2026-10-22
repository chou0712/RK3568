from __future__ import annotations

import argparse
import queue
import threading
import time
import tkinter as tk
from datetime import datetime, timezone
from tkinter import messagebox, ttk
from typing import Iterable

from .config import (
    DEFAULT_NETWORK,
    HOMING_ANGLE_TOLERANCE_DEG,
    HOMING_SPEED_TOLERANCE_DPS,
    HOMING_TIMEOUT_SECONDS,
    STATUS_QUERY_INTERVAL_MS,
    STATUS_TIMEOUT_SECONDS,
    TRACK_INTERVAL_MS,
    NetworkConfig,
)
from .orbit import Observer, TLEOrbit
from .passes import PassWindow, predict_passes
from .protocol import (
    TurntableStatus,
    build_fault_query,
    build_point_command,
    build_speed_command,
    build_status_query,
    build_stop_command,
    target_reached,
)
from .tracking import TrackingAction, TrackingEngine, TrackingState
from .transport import TransportEvent, UdpTransport


def _local_time(value: datetime) -> str:
    return value.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _duration(seconds: float) -> str:
    minutes, second = divmod(max(0, int(round(seconds))), 60)
    return f"{minutes}分{second:02d}秒"


class SatelliteTrackerApp(tk.Tk):
    def __init__(self, network: NetworkConfig = DEFAULT_NETWORK, auto_connect: bool = True):
        super().__init__()
        self.title("RK3568 卫星跟踪上位机")
        self.geometry("1180x820")
        self.minsize(1040, 720)

        self.events: queue.Queue[TransportEvent] = queue.Queue()
        self.transport = UdpTransport(self.events)
        self.last_status: TurntableStatus | None = None
        self.last_status_monotonic: float | None = None
        self.manual_motion_status_after: float | None = None
        self.tracking_engine: TrackingEngine | None = None
        self.homing_active = False
        self.homing_deadline: float | None = None
        self.homing_started_monotonic: float | None = None
        self.pass_rows: dict[str, PassWindow] = {}
        self.predicted_orbit: TLEOrbit | None = None
        self.prediction_signature: tuple[object, ...] | None = None
        self.prediction_token = 0
        self.closing = False

        self._build_ui(network)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(50, self._poll_events)
        self.after(200, self._clock_tick)
        self.after(STATUS_QUERY_INTERVAL_MS, self._status_query_tick)
        self.after(TRACK_INTERVAL_MS, self._tracking_tick)
        self.after(200, self._safety_tick)
        if auto_connect:
            self.after(250, self.connect)

    def _build_ui(self, network: NetworkConfig) -> None:
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("TNotebook.Tab", padding=(20, 9), font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Treeview", rowheight=25, font=("Microsoft YaHei UI", 9))
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 16, "bold"))
        style.configure("Value.TLabel", font=("Microsoft YaHei UI", 18, "bold"), foreground="#1565c0")
        style.configure("State.TLabel", font=("Microsoft YaHei UI", 11, "bold"), foreground="#1565c0")

        top = ttk.Frame(self, padding=(10, 8))
        top.pack(fill="x")
        ttk.Label(top, text="RK3568 卫星跟踪上位机", style="Title.TLabel").pack(side="left")
        self.clock_var = tk.StringVar(value="--")
        ttk.Label(top, textvariable=self.clock_var).pack(side="right")

        self.gateway_var = tk.StringVar(value=network.gateway_host)
        self.gateway_port_var = tk.StringVar(value=str(network.gateway_port))
        self.local_var = tk.StringVar(value=network.local_host)
        self.local_port_var = tk.StringVar(value=str(network.local_port))
        self.connection_var = tk.StringVar(value="未连接")
        connection = ttk.LabelFrame(self, text="RK3568 UDP 通信", padding=8)
        connection.pack(fill="x", padx=10)
        fields = (
            ("网关 IP", self.gateway_var, 15),
            ("网关端口", self.gateway_port_var, 7),
            ("本机 IP", self.local_var, 15),
            ("本机端口", self.local_port_var, 7),
        )
        for index, (label, variable, width) in enumerate(fields):
            ttk.Label(connection, text=label).grid(row=0, column=index * 2, sticky="w")
            ttk.Entry(connection, textvariable=variable, width=width).grid(row=0, column=index * 2 + 1, padx=(3, 10))
        self.connect_button = ttk.Button(connection, text="连接", command=self.connect)
        self.connect_button.grid(row=0, column=8, padx=4)
        ttk.Button(connection, text="安全停止", command=self.stop_all).grid(row=0, column=9, padx=4)
        ttk.Label(connection, textvariable=self.connection_var, foreground="#1565c0").grid(
            row=1, column=0, columnspan=10, sticky="w", pady=(6, 0)
        )

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=10, pady=8)
        self.control_tab = ttk.Frame(notebook, padding=12)
        self.tracking_tab = ttk.Frame(notebook, padding=12)
        notebook.add(self.control_tab, text="1  转台控制")
        notebook.add(self.tracking_tab, text="2  卫星任务跟随")
        self._build_control_tab()
        self._build_tracking_tab()

        self.footer_var = tk.StringVar(value="系统就绪")
        footer = tk.Label(
            self,
            textvariable=self.footer_var,
            anchor="w",
            bg="#1f2937",
            fg="#f9fafb",
            padx=12,
            pady=8,
        )
        footer.pack(fill="x", side="bottom")

    def _build_control_tab(self) -> None:
        self.actual_az_var = tk.StringVar(value="--")
        self.actual_el_var = tk.StringVar(value="--")
        self.actual_az_speed_var = tk.StringVar(value="--")
        self.actual_el_speed_var = tk.StringVar(value="--")
        self.fault_var = tk.StringVar(value="未收到状态")

        status = ttk.LabelFrame(self.control_tab, text="转台实时状态", padding=12)
        status.pack(fill="x")
        values = (
            ("方位", self.actual_az_var, "Value.TLabel"),
            ("俯仰", self.actual_el_var, "Value.TLabel"),
            ("方位速度", self.actual_az_speed_var, None),
            ("俯仰速度", self.actual_el_speed_var, None),
            ("故障", self.fault_var, None),
        )
        for index, (label, variable, label_style) in enumerate(values):
            ttk.Label(status, text=label).grid(row=0, column=index, padx=10)
            options = {"style": label_style} if label_style else {}
            ttk.Label(status, textvariable=variable, **options).grid(row=1, column=index, padx=10, pady=4)

        self.point_az_var = tk.StringVar(value="0.0")
        self.point_el_var = tk.StringVar(value="0.0")
        self.speed_az_var = tk.StringVar(value="5.0")
        self.speed_el_var = tk.StringVar(value="5.0")
        point = ttk.LabelFrame(self.control_tab, text="定点控制", padding=12)
        point.pack(fill="x", pady=(10, 0))
        ttk.Label(point, text="方位角（0～360°）").grid(row=0, column=0, sticky="w")
        ttk.Entry(point, textvariable=self.point_az_var, width=14).grid(row=0, column=1, padx=6)
        ttk.Label(point, text="俯仰角（0～90°）").grid(row=0, column=2, sticky="w")
        ttk.Entry(point, textvariable=self.point_el_var, width=14).grid(row=0, column=3, padx=6)
        ttk.Button(point, text="执行定点", command=self.send_point).grid(row=0, column=4, padx=5)
        ttk.Button(point, text="机械回零 0°/0°", command=self.mechanical_home).grid(row=0, column=5, padx=5)
        ttk.Button(point, text="天顶姿态 0°/90°", command=self.zenith_position).grid(row=0, column=6, padx=5)
        ttk.Button(point, text="查询状态", command=lambda: self._safe_send(build_status_query())).grid(row=0, column=7, padx=5)
        ttk.Button(point, text="查询故障", command=lambda: self._safe_send(build_fault_query())).grid(row=0, column=8, padx=5)

        ttk.Label(point, text="方位转速（0～5°/s）").grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(point, textvariable=self.speed_az_var, width=14).grid(row=1, column=1, padx=6, pady=(10, 0))
        ttk.Label(point, text="俯仰转速（0～5°/s）").grid(row=1, column=2, sticky="w", pady=(10, 0))
        ttk.Entry(point, textvariable=self.speed_el_var, width=14).grid(row=1, column=3, padx=6, pady=(10, 0))
        ttk.Button(point, text="应用转速", command=self.apply_speed).grid(row=1, column=4, padx=5, pady=(10, 0))
        ttk.Label(point, text="应用前会先停止当前运动；该转速同时用于定点、回零和卫星跟随。", foreground="#607d8b").grid(
            row=1, column=5, columnspan=4, sticky="w", padx=(8, 0), pady=(10, 0)
        )

        log_frame = ttk.LabelFrame(self.control_tab, text="通信与运行日志", padding=6)
        log_frame.pack(fill="both", expand=True, pady=(10, 0))
        self.log_text = tk.Text(log_frame, height=18, wrap="none", state="disabled", font=("Consolas", 9))
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def _build_tracking_tab(self) -> None:
        self.tle1_var = tk.StringVar()
        self.tle2_var = tk.StringVar()
        self.latitude_var = tk.StringVar(value="30.0")
        self.longitude_var = tk.StringVar(value="120.0")
        self.altitude_var = tk.StringVar(value="0")
        self.min_elevation_var = tk.StringVar(value="10.0")
        self.forecast_hours_var = tk.StringVar(value="24")

        settings = ttk.LabelFrame(self.tracking_tab, text="TLE 与观测站", padding=10)
        settings.pack(fill="x")
        ttk.Label(settings, text="TLE 第1行").grid(row=0, column=0, sticky="w")
        ttk.Entry(settings, textvariable=self.tle1_var).grid(row=0, column=1, columnspan=7, sticky="ew", padx=5, pady=2)
        ttk.Label(settings, text="TLE 第2行").grid(row=1, column=0, sticky="w")
        ttk.Entry(settings, textvariable=self.tle2_var).grid(row=1, column=1, columnspan=7, sticky="ew", padx=5, pady=2)
        labels = (
            ("纬度(°)", self.latitude_var),
            ("经度(°)", self.longitude_var),
            ("海拔(m)", self.altitude_var),
            ("最低跟踪俯仰(°)", self.min_elevation_var),
            ("预报时长(h)", self.forecast_hours_var),
        )
        for index, (label, variable) in enumerate(labels):
            ttk.Label(settings, text=label).grid(row=2, column=index * 2, sticky="w", pady=(5, 0))
            ttk.Entry(settings, textvariable=variable, width=10).grid(row=2, column=index * 2 + 1, padx=(3, 10), pady=(5, 0))
        settings.columnconfigure(1, weight=1)
        ttk.Button(settings, text="计算可跟踪过境", command=self.compute_passes).grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(settings, text="过境窗口按最低跟踪俯仰角计算，窗口外不会下发运动命令。", foreground="#607d8b").grid(
            row=3, column=2, columnspan=6, sticky="w", pady=(8, 0)
        )

        passes = ttk.LabelFrame(self.tracking_tab, text="可跟踪过境", padding=6)
        passes.pack(fill="both", expand=True, pady=(8, 0))
        columns = ("aos", "los", "duration", "max_el", "max_time", "aos_az", "los_az")
        self.pass_tree = ttk.Treeview(passes, columns=columns, show="headings", height=8)
        headings = {
            "aos": "开始时间",
            "los": "结束时间",
            "duration": "持续时间",
            "max_el": "最大俯仰",
            "max_time": "最大俯仰时间",
            "aos_az": "入境方位",
            "los_az": "离境方位",
        }
        widths = {"aos": 145, "los": 145, "duration": 85, "max_el": 85, "max_time": 145, "aos_az": 85, "los_az": 85}
        for column in columns:
            self.pass_tree.heading(column, text=headings[column])
            self.pass_tree.column(column, width=widths[column], anchor="center")
        pass_scroll = ttk.Scrollbar(passes, command=self.pass_tree.yview)
        self.pass_tree.configure(yscrollcommand=pass_scroll.set)
        self.pass_tree.pack(side="left", fill="both", expand=True)
        pass_scroll.pack(side="right", fill="y")

        action = ttk.Frame(self.tracking_tab)
        action.pack(fill="x", pady=8)
        ttk.Button(action, text="启动选中过境任务", command=self.start_selected_pass).pack(side="left")
        ttk.Button(action, text="停止跟随任务", command=self.stop_tracking).pack(side="left", padx=6)
        self.task_state_var = tk.StringVar(value="未启动")
        ttk.Label(action, textvariable=self.task_state_var, style="State.TLabel").pack(side="left", padx=15)

        self.satellite_position_var = tk.StringVar(value="卫星目标：--")
        self.task_window_var = tk.StringVar(value="任务窗口：--")
        detail = ttk.LabelFrame(self.tracking_tab, text="任务状态", padding=8)
        detail.pack(fill="x")
        ttk.Label(detail, textvariable=self.satellite_position_var).pack(anchor="w")
        ttk.Label(detail, textvariable=self.task_window_var).pack(anchor="w", pady=(4, 0))

    def _log(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{stamp}] {message}\n")
        line_count = int(self.log_text.index("end-1c").split(".")[0])
        if line_count > 1500:
            self.log_text.delete("1.0", "301.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def connect(self) -> None:
        if self.transport.connected:
            self.disconnect()
            return
        try:
            config = NetworkConfig(
                gateway_host=self.gateway_var.get().strip(),
                gateway_port=int(self.gateway_port_var.get()),
                local_host=self.local_var.get().strip(),
                local_port=int(self.local_port_var.get()),
            )
            self.transport.connect(config)
            self.connect_button.configure(text="断开")
            self.connection_var.set(
                f"已连接网关 {config.gateway_host}:{config.gateway_port}；本机 {self.transport.local_address[0]}:{self.transport.local_address[1]}"
            )
            self._log("UDP 已连接，正在查询转台状态")
            self._safe_send(build_status_query())
        except Exception as exc:
            self.transport.close()
            messagebox.showerror("连接失败", str(exc))

    def disconnect(self, send_stop: bool = True) -> None:
        if send_stop and self.transport.connected:
            try:
                self.transport.send(build_stop_command())
            except Exception:
                pass
        if self.tracking_engine is not None:
            self.tracking_engine.stop()
            self.tracking_engine = None
        self.transport.close()
        self.last_status = None
        self.last_status_monotonic = None
        self.manual_motion_status_after = None
        self.homing_active = False
        self.homing_deadline = None
        self.homing_started_monotonic = None
        self.connect_button.configure(text="连接")
        self.connection_var.set("未连接")
        self.task_state_var.set("未启动")
        self._log("连接已断开")

    def _safe_send(self, frame: bytes, popup: bool = True) -> bool:
        try:
            self.transport.send(frame)
            return True
        except Exception as exc:
            self._log(f"发送失败：{exc}")
            if popup:
                messagebox.showerror("发送失败", str(exc))
            return False

    def _motion_ready(self) -> bool:
        if not self.transport.connected:
            messagebox.showwarning("尚未连接", "请先连接 RK3568 UDP 网关。")
            return False
        if self.last_status is None or self.last_status_monotonic is None:
            messagebox.showerror("无状态反馈", "尚未收到有效转台状态，禁止发送运动命令。")
            return False
        age = time.monotonic() - self.last_status_monotonic
        if age > STATUS_TIMEOUT_SECONDS:
            messagebox.showerror("状态超时", "转台状态反馈已超时，禁止发送运动命令。")
            return False
        if self.last_status.fault:
            messagebox.showerror("转台故障", "；".join(self.last_status.faults) or f"故障码 0x{self.last_status.fault:02X}")
            return False
        return True

    def send_point(self) -> None:
        if not self._motion_ready():
            return
        try:
            azimuth = float(self.point_az_var.get())
            elevation = float(self.point_el_var.get())
            frame = build_point_command(azimuth, elevation)
        except ValueError as exc:
            messagebox.showerror("参数错误", str(exc))
            return
        self._cancel_homing()
        self.stop_tracking(send_stop=False)
        sent_at = time.monotonic()
        if self._safe_send(frame):
            self.manual_motion_status_after = sent_at
            self.footer_var.set(f"定点目标：方位 {azimuth:.1f}° / 俯仰 {elevation:.1f}°")

    def apply_speed(self) -> None:
        if not self.transport.connected:
            messagebox.showwarning("尚未连接", "请先连接 RK3568 UDP 网关。")
            return
        try:
            azimuth_speed = float(self.speed_az_var.get())
            elevation_speed = float(self.speed_el_var.get())
            frame = build_speed_command(azimuth_speed, elevation_speed)
        except ValueError as exc:
            messagebox.showerror("参数错误", str(exc))
            return

        # 避免在运动途中突变速度；停止当前任务后再更新设备的目标速度。
        self.stop_tracking(send_stop=False)
        self._cancel_homing()
        self.manual_motion_status_after = None
        self._safe_send(build_stop_command(), popup=False)
        if self._safe_send(frame):
            message = (
                f"转速已设置：方位 {azimuth_speed:.1f}°/s / "
                f"俯仰 {elevation_speed:.1f}°/s"
            )
            self.footer_var.set(message)
            self._log(message)

    def mechanical_home(self) -> None:
        if not self._motion_ready():
            return
        if not messagebox.askyesno(
            "确认机械回零",
            "机械回零将转台定点至编码器方位 0°、俯仰 0°。\n\n"
            "请确认零位已正确标定，运动路径内无人、无线缆和其他障碍物。是否继续？",
        ):
            return
        self.stop_tracking(send_stop=False)
        self._cancel_homing()
        self.point_az_var.set("0.0")
        self.point_el_var.set("0.0")
        # 先停止当前运动，再下发零位定点，避免与上一条运动任务叠加。
        self._safe_send(build_stop_command(), popup=False)
        sent_at = time.monotonic()
        if self._safe_send(build_point_command(0.0, 0.0)):
            self.homing_active = True
            self.homing_started_monotonic = sent_at
            self.homing_deadline = sent_at + HOMING_TIMEOUT_SECONDS
            self.manual_motion_status_after = sent_at
            self.footer_var.set("机械回零中：目标方位 0.0° / 俯仰 0.0°")
            self._log("机械回零开始：目标方位 0.0° / 俯仰 0.0°")

    def zenith_position(self) -> None:
        if not messagebox.askyesno("确认天顶姿态", "确认定点至方位 0°、俯仰 90°（天顶）？"):
            return
        self.point_az_var.set("0.0")
        self.point_el_var.set("90.0")
        self.send_point()

    def _cancel_homing(self) -> None:
        self.homing_active = False
        self.homing_deadline = None
        self.homing_started_monotonic = None

    def stop_all(self) -> None:
        self.stop_tracking(send_stop=False)
        self._cancel_homing()
        self.manual_motion_status_after = None
        if self.transport.connected:
            self._safe_send(build_stop_command())
        self.footer_var.set("已发送安全停止")

    def _orbit_inputs(self) -> tuple[TLEOrbit, float, float, tuple[object, ...]]:
        line1, line2 = self.tle1_var.get().strip(), self.tle2_var.get().strip()
        observer = Observer(
            latitude_deg=float(self.latitude_var.get()),
            longitude_deg=float(self.longitude_var.get()),
            altitude_m=float(self.altitude_var.get()),
        )
        minimum = float(self.min_elevation_var.get())
        hours = float(self.forecast_hours_var.get())
        if not 0.0 <= minimum < 90.0:
            raise ValueError("最低跟踪俯仰角必须在 0～90° 之间")
        orbit = TLEOrbit(line1, line2, observer)
        signature = (orbit.line1, orbit.line2, observer.latitude_deg, observer.longitude_deg, observer.altitude_m, minimum, hours)
        return orbit, minimum, hours, signature

    def compute_passes(self) -> None:
        try:
            orbit, minimum, hours, signature = self._orbit_inputs()
        except Exception as exc:
            messagebox.showerror("参数错误", str(exc))
            return
        self.prediction_token += 1
        token = self.prediction_token
        self.footer_var.set("正在计算可跟踪过境……")
        thread = threading.Thread(
            target=self._prediction_worker,
            args=(token, orbit, minimum, hours, signature),
            name="pass-predictor",
            daemon=True,
        )
        thread.start()

    def _prediction_worker(self, token: int, orbit: TLEOrbit, minimum: float, hours: float, signature: tuple[object, ...]) -> None:
        try:
            passes = predict_passes(
                orbit,
                datetime.now(timezone.utc),
                hours=hours,
                min_elevation_deg=minimum,
                step_seconds=20.0,
            )
            self.events.put(TransportEvent("prediction", (token, orbit, signature, passes)))
        except Exception as exc:
            self.events.put(TransportEvent("prediction_error", (token, str(exc))))

    def _apply_prediction(self, orbit: TLEOrbit, signature: tuple[object, ...], passes: list[PassWindow]) -> None:
        self.predicted_orbit = orbit
        self.prediction_signature = signature
        self.pass_rows.clear()
        for item in self.pass_tree.get_children():
            self.pass_tree.delete(item)
        for index, item in enumerate(passes, 1):
            iid = f"pass-{index}"
            self.pass_rows[iid] = item
            self.pass_tree.insert(
                "",
                "end",
                iid=iid,
                values=(
                    _local_time(item.aos),
                    _local_time(item.los),
                    _duration(item.duration_seconds),
                    f"{item.max_elevation_deg:.1f}°",
                    _local_time(item.max_elevation_time),
                    f"{item.aos_azimuth_deg:.1f}°",
                    f"{item.los_azimuth_deg:.1f}°",
                ),
            )
        children = self.pass_tree.get_children()
        if children:
            self.pass_tree.selection_set(children[0])
            self.pass_tree.focus(children[0])
        self.footer_var.set(f"过境计算完成：找到 {len(passes)} 个可跟踪窗口")

    def start_selected_pass(self) -> None:
        if not self._motion_ready():
            return
        selection = self.pass_tree.selection()
        if not selection:
            messagebox.showwarning("未选择任务", "请先计算过境并选择一个可跟踪窗口。")
            return
        try:
            orbit, minimum, _hours, signature = self._orbit_inputs()
        except Exception as exc:
            messagebox.showerror("参数错误", str(exc))
            return
        if signature != self.prediction_signature or self.predicted_orbit is None:
            messagebox.showwarning("参数已变化", "TLE、观测站或阈值已变化，请重新计算过境。")
            return
        window = self.pass_rows[selection[0]]
        now = datetime.now(timezone.utc)
        if now > window.los:
            messagebox.showwarning("任务已过期", "选中的过境窗口已经结束，请重新计算过境。")
            return
        self.stop_tracking(send_stop=True)
        self.tracking_engine = TrackingEngine(orbit, window, minimum)
        self.task_state_var.set(TrackingState.WAITING.value)
        self.task_window_var.set(f"任务窗口：{_local_time(window.aos)} ～ {_local_time(window.los)}")
        self._log(f"卫星任务已创建：{_local_time(window.aos)} ～ {_local_time(window.los)}，最低俯仰 {minimum:.1f}°")

    def stop_tracking(self, send_stop: bool = True) -> None:
        engine, self.tracking_engine = self.tracking_engine, None
        if engine is not None:
            engine.stop()
            self.task_state_var.set(TrackingState.STOPPED.value)
            self._log("卫星跟随任务已停止")
        if send_stop and self.transport.connected:
            self._safe_send(build_stop_command(), popup=False)

    def _tracking_tick(self) -> None:
        try:
            engine = self.tracking_engine
            if engine is not None:
                now = datetime.now(timezone.utc)
                if now >= engine.window.aos and not self._status_is_fresh():
                    self._abort_tracking("转台状态反馈超时")
                else:
                    update = engine.tick(now)
                    self.task_state_var.set(update.state.value)
                    self.footer_var.set(update.message)
                    if update.look is not None:
                        self.satellite_position_var.set(
                            f"卫星目标：方位 {update.look.azimuth_deg:.3f}° / 俯仰 {update.look.elevation_deg:.3f}° / 距离 {update.look.range_km:.1f} km"
                        )
                    if update.action == TrackingAction.TARGET and update.look is not None:
                        self._safe_send(
                            build_point_command(update.look.azimuth_deg, update.look.elevation_deg),
                            popup=False,
                        )
                    elif update.action == TrackingAction.STOP:
                        self._safe_send(build_stop_command(), popup=False)
                    if update.state == TrackingState.COMPLETED:
                        self._log("卫星已离境，跟随任务完成并停止转台")
                        self.tracking_engine = None
        except Exception as exc:
            self._abort_tracking(f"跟踪计算失败：{exc}")
        finally:
            if not self.closing:
                self.after(TRACK_INTERVAL_MS, self._tracking_tick)

    def _status_is_fresh(self) -> bool:
        return (
            self.last_status is not None
            and self.last_status.fault == 0
            and self.last_status_monotonic is not None
            and time.monotonic() - self.last_status_monotonic <= STATUS_TIMEOUT_SECONDS
        )

    def _abort_tracking(self, reason: str) -> None:
        engine, self.tracking_engine = self.tracking_engine, None
        if engine is not None:
            engine.stop(error=True)
        if self.transport.connected:
            self._safe_send(build_stop_command(), popup=False)
        self.task_state_var.set(TrackingState.ERROR.value)
        self.footer_var.set(reason)
        self._log(f"安全停止：{reason}")

    def _apply_status(self, status: TurntableStatus) -> None:
        received_at = time.monotonic()
        self.last_status = status
        self.last_status_monotonic = received_at
        if self.manual_motion_status_after is not None and received_at >= self.manual_motion_status_after:
            self.manual_motion_status_after = None
        self.actual_az_var.set(f"{status.azimuth_deg:.1f}°")
        self.actual_el_var.set(f"{status.elevation_deg:.1f}°")
        self.actual_az_speed_var.set(f"{status.azimuth_speed_dps:.1f}°/s")
        self.actual_el_speed_var.set(f"{status.elevation_speed_dps:.1f}°/s")
        if status.fault:
            fault_text = "；".join(status.faults) or f"未知故障 0x{status.fault:02X}"
            self.fault_var.set(fault_text)
            if self.tracking_engine is not None:
                self._abort_tracking(f"转台故障：{fault_text}")
            if self.homing_active:
                self._abort_homing(f"转台故障：{fault_text}")
        else:
            self.fault_var.set("正常")
            if self.homing_active and target_reached(
                status,
                0.0,
                0.0,
                HOMING_ANGLE_TOLERANCE_DEG,
                HOMING_SPEED_TOLERANCE_DPS,
            ):
                self._cancel_homing()
                self.manual_motion_status_after = None
                self._safe_send(build_stop_command(), popup=False)
                self.footer_var.set("机械回零完成：方位 0.0° / 俯仰 0.0°")
                self._log("机械回零完成并停止锁定")

    def _abort_homing(self, reason: str) -> None:
        if not self.homing_active:
            return
        self._cancel_homing()
        self.manual_motion_status_after = None
        if self.transport.connected:
            self._safe_send(build_stop_command(), popup=False)
        self.footer_var.set(f"机械回零失败：{reason}")
        self._log(f"机械回零安全停止：{reason}")

    def _poll_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                if event.kind == "status":
                    self._apply_status(event.payload)  # type: ignore[arg-type]
                elif event.kind in ("tx", "rx"):
                    frame = event.payload
                    assert isinstance(frame, bytes)
                    # 连续跟踪期间不逐条输出定点帧，避免日志淹没关键故障信息。
                    if not (event.kind == "tx" and len(frame) > 4 and frame[3:5] == b"\x06\x02" and self.tracking_engine):
                        self._log(f"{event.kind.upper()}  {frame.hex(' ').upper()}")
                elif event.kind == "response":
                    frame = event.payload
                    assert isinstance(frame, bytes)
                    self._log(f"有效应答 CMD=0x{frame[3]:02X}")
                elif event.kind == "protocol_error":
                    self._log(f"丢弃无效数据报：{event.payload}")
                elif event.kind == "network_error":
                    self._log(f"UDP 网络错误：{event.payload}")
                    self.disconnect(send_stop=False)
                elif event.kind == "prediction":
                    token, orbit, signature, passes = event.payload  # type: ignore[misc]
                    if token == self.prediction_token:
                        self._apply_prediction(orbit, signature, passes)
                elif event.kind == "prediction_error":
                    token, error = event.payload  # type: ignore[misc]
                    if token == self.prediction_token:
                        self.footer_var.set(f"过境计算失败：{error}")
                        messagebox.showerror("过境计算失败", error)
        except queue.Empty:
            pass
        if not self.closing:
            self.after(50, self._poll_events)

    def _status_query_tick(self) -> None:
        if self.transport.connected:
            self._safe_send(build_status_query(), popup=False)
        if not self.closing:
            self.after(STATUS_QUERY_INTERVAL_MS, self._status_query_tick)

    def _safety_tick(self) -> None:
        if self.homing_active:
            now = time.monotonic()
            if self.homing_deadline is not None and now >= self.homing_deadline:
                self._abort_homing(f"超过 {HOMING_TIMEOUT_SECONDS:.0f} 秒仍未到位")
            elif (
                self.homing_started_monotonic is not None
                and now - self.homing_started_monotonic >= STATUS_TIMEOUT_SECONDS
                and (
                    self.last_status_monotonic is None
                    or self.last_status_monotonic < self.homing_started_monotonic
                    or now - self.last_status_monotonic > STATUS_TIMEOUT_SECONDS
                )
            ):
                self._abort_homing("状态反馈超时")
        if self.manual_motion_status_after is not None:
            if time.monotonic() - self.manual_motion_status_after > 1.2:
                if self.last_status_monotonic is None or self.last_status_monotonic < self.manual_motion_status_after:
                    if self.homing_active:
                        self._abort_homing("运动后 1.2 秒内未收到有效状态")
                    else:
                        self.manual_motion_status_after = None
                        if self.transport.connected:
                            self._safe_send(build_stop_command(), popup=False)
                        self.footer_var.set("运动后未收到新状态，已自动停止")
                        self._log("安全停止：定点运动后 1.2 秒内未收到有效状态")
        if not self.closing:
            self.after(200, self._safety_tick)

    def _clock_tick(self) -> None:
        self.clock_var.set(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        if not self.closing:
            self.after(1000, self._clock_tick)

    def _on_close(self) -> None:
        self.closing = True
        self.disconnect(send_stop=True)
        self.destroy()


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RK3568 卫星跟踪上位机")
    parser.add_argument("--gateway", default=DEFAULT_NETWORK.gateway_host)
    parser.add_argument("--gateway-port", type=int, default=DEFAULT_NETWORK.gateway_port)
    parser.add_argument("--local", default=DEFAULT_NETWORK.local_host)
    parser.add_argument("--local-port", type=int, default=DEFAULT_NETWORK.local_port)
    parser.add_argument("--no-auto-connect", action="store_true")
    args = parser.parse_args(argv)
    network = NetworkConfig(args.gateway, args.gateway_port, args.local, args.local_port)
    app = SatelliteTrackerApp(network, auto_connect=not args.no_auto_connect)
    app.mainloop()
    return 0
