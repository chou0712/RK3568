"""Tabbed Tkinter desktop application for antenna control."""

from __future__ import annotations

import queue
import socket
import threading
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime, timezone
from tkinter import messagebox, ttk

from aux_protocol import (
    TYPE_IMU_STATE,
    TYPE_TLE,
    AuxProtocolError,
    decode_imu,
    decode_tle,
    parse_frame as parse_aux_frame,
)
from config import DEFAULT_NETWORK, NetworkConfig
from pass_predictor import PassInfo, predict_passes
from protocol import HEADER as TURNTABLE_HEADER
from protocol import parse_status_frame
from tle_calculator import TLECalculator
from udp_client import DeviceStatus, RK3568UDPClient


def _wrap_angle(angle: float) -> float:
    return (angle + 180.0) % 360.0 - 180.0


def _shortest_delta(current: float, target: float) -> float:
    delta = (target - current) % 360.0
    return delta - 360.0 if delta > 180.0 else delta


def _fmt_local(dt: datetime | None) -> str:
    if dt is None:
        return "-"
    return dt.astimezone().strftime("%m-%d %H:%M:%S")


def _fmt_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    minutes, sec = divmod(seconds, 60)
    return f"{minutes}分{sec:02d}秒"


@dataclass
class Task:
    name: str
    kind: str
    az: float = 0.0
    el: float = 0.0
    tle1: str = ""
    tle2: str = ""
    lat: float = 0.0
    lon: float = 0.0
    alt: float = 0.0
    start: datetime | None = None
    end: datetime | None = None
    max_el: float = 0.0
    threshold_el: float = 10.0
    status: str = "待执行"


class UpperComputerApp:
    def __init__(
        self,
        root: tk.Tk,
        ip: str = DEFAULT_NETWORK.rk3568_ip,
        send_port: int = DEFAULT_NETWORK.send_port,
        recv_port: int = DEFAULT_NETWORK.recv_port,
    ) -> None:
        self.root = root
        self.root.title("卫星天线控制上位机")
        self.root.geometry("1180x820")
        self.root.minsize(1000, 720)

        self._client: RK3568UDPClient | None = None
        self._recv_queue: queue.Queue = queue.Queue()
        self._running = False
        self._thread: threading.Thread | None = None
        self._last_status: DeviceStatus | None = None

        self._tasks: dict[str, Task] = {}
        self._task_counter = 0
        self._active_task: str | None = None
        self._pass_after_id: str | None = None
        self._scheduler_after_id: str | None = None
        self._pass_task: Task | None = None
        self._pass_calculator: TLECalculator | None = None
        self._pass_in_track = False
        self._passes: list[PassInfo] = []
        self._pass_rows: dict[str, PassInfo] = {}

        self._build_ui(
            ip=ip,
            send_port=send_port,
            recv_port=recv_port,
        )
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self._poll_queue)
        self.root.after(1000, self._clock_tick)
        self.root.after(1000, self._status_log_tick)
        self.root.after(1000, self._scheduler_tick)

    # ------------------------------------------------------------------ UI
    def _build_ui(
        self,
        ip: str,
        send_port: int,
        recv_port: int,
    ) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "TNotebook.Tab",
            padding=(18, 9),
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        style.configure(
            "Treeview",
            rowheight=24,
            font=("Microsoft YaHei UI", 9),
        )
        style.configure("TButton", padding=(8, 4))
        style.configure(
            "TLabelframe.Label",
            font=("Microsoft YaHei UI", 10, "bold"),
        )

        self._build_status_bar(self.root)

        main = ttk.Frame(self.root, padding=(10, 8))
        main.pack(fill="both", expand=True)
        self._build_connection_bar(
            main, ip=ip, send_port=send_port, recv_port=recv_port
        )

        self.notebook = ttk.Notebook(main)
        self.notebook.pack(fill="both", expand=True, pady=(8, 0))

        self.control_tab = ttk.Frame(self.notebook, padding=12)
        self.pass_tab = ttk.Frame(self.notebook, padding=12)
        self.tle_tab = ttk.Frame(self.notebook, padding=12)
        self.task_tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(self.control_tab, text="1  转台控制")
        self.notebook.add(self.pass_tab, text="2  过境预报")
        self.notebook.add(self.tle_tab, text="3  TLE连续跟踪")
        self.notebook.add(self.task_tab, text="4  任务管理")

        self._build_control_tab()
        self._build_pass_tab()
        self._build_tle_tab()
        self._build_task_tab()

    def _build_connection_bar(
        self,
        parent: ttk.Frame,
        ip: str,
        send_port: int,
        recv_port: int,
    ) -> None:
        self.ip_var = tk.StringVar(value=ip)
        self.send_port_var = tk.StringVar(value=str(send_port))
        self.recv_port_var = tk.StringVar(value=str(recv_port))
        self.link_var = tk.StringVar(value="未连接")

        frame = ttk.LabelFrame(parent, text="通信设置（UDP / AA 转台协议）", padding=8)
        frame.pack(fill="x")
        ttk.Label(frame, text="RK3568 IP").grid(row=0, column=0, sticky="w")
        ttk.Entry(frame, textvariable=self.ip_var, width=16).grid(
            row=0, column=1, padx=4
        )
        ttk.Label(frame, text="发送端口").grid(row=0, column=2, sticky="w")
        ttk.Entry(frame, textvariable=self.send_port_var, width=8).grid(
            row=0, column=3, padx=4
        )
        ttk.Label(frame, text="接收端口").grid(row=0, column=4, sticky="w")
        ttk.Entry(frame, textvariable=self.recv_port_var, width=8).grid(
            row=0, column=5, padx=4
        )
        ttk.Button(frame, text="连接", command=self.connect).grid(
            row=0, column=6, padx=4
        )
        ttk.Button(frame, text="断开", command=self.disconnect).grid(
            row=0, column=7, padx=4
        )
        ttk.Label(frame, textvariable=self.link_var, foreground="#1565c0").grid(
            row=1, column=0, columnspan=10, sticky="w", pady=(6, 0)
        )

    def _build_control_tab(self) -> None:
        self.target_az_var = tk.StringVar(value="0.0")
        self.target_el_var = tk.StringVar(value="0.0")
        self.current_az_var = tk.StringVar(value="--")
        self.current_el_var = tk.StringVar(value="--")
        self.az_speed_var = tk.StringVar(value="--")
        self.el_speed_var = tk.StringVar(value="--")
        self.fault_var = tk.StringVar(value="0")
        self.status_var = tk.StringVar(value="未连接")
        self.north_var = tk.StringVar(value="未校准")

        reset = ttk.LabelFrame(self.control_tab, text="转台复位", padding=10)
        reset.pack(fill="x", pady=(0, 8))
        ttk.Label(
            reset,
            text="复位姿态：方位 0°（正北），俯仰 90°（天顶）",
            font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(side="left")
        ttk.Button(
            reset, text="执行转台复位", command=self.reset_turntable
        ).pack(side="right")

        point = ttk.LabelFrame(self.control_tab, text="定点运动", padding=10)
        point.pack(fill="x", pady=(0, 8))
        ttk.Label(point, text="方位角（°）").grid(row=0, column=0, sticky="w")
        ttk.Entry(point, textvariable=self.target_az_var, width=12).grid(
            row=0, column=1, padx=6
        )
        ttk.Label(point, text="俯仰角（°）").grid(row=0, column=2, sticky="w")
        ttk.Entry(point, textvariable=self.target_el_var, width=12).grid(
            row=0, column=3, padx=6
        )
        ttk.Button(point, text="执行定点", command=self.execute_point).grid(
            row=0, column=4, padx=4
        )
        ttk.Button(point, text="停止", command=self.send_stop).grid(
            row=0, column=5, padx=4
        )
        ttk.Button(point, text="查询状态", command=self.send_query).grid(
            row=0, column=6, padx=4
        )

        current = ttk.LabelFrame(self.control_tab, text="当前姿态", padding=10)
        current.pack(fill="x", pady=(0, 8))
        ttk.Label(current, text="方位").grid(row=0, column=0, sticky="w")
        ttk.Label(
            current,
            textvariable=self.current_az_var,
            font=("Microsoft YaHei UI", 16, "bold"),
            foreground="#1565c0",
        ).grid(row=0, column=1, padx=8)
        ttk.Label(current, text="俯仰").grid(row=0, column=2, sticky="w")
        ttk.Label(
            current,
            textvariable=self.current_el_var,
            font=("Microsoft YaHei UI", 16, "bold"),
            foreground="#1565c0",
        ).grid(row=0, column=3, padx=8)
        ttk.Label(current, text="方位速度").grid(row=1, column=0, sticky="w")
        ttk.Label(current, textvariable=self.az_speed_var).grid(
            row=1, column=1, sticky="w", padx=8
        )
        ttk.Label(current, text="俯仰速度").grid(row=1, column=2, sticky="w")
        ttk.Label(current, textvariable=self.el_speed_var).grid(
            row=1, column=3, sticky="w", padx=8
        )
        ttk.Label(current, text="故障码").grid(row=1, column=4, sticky="w")
        ttk.Label(current, textvariable=self.fault_var).grid(
            row=1, column=5, sticky="w", padx=8
        )
        ttk.Label(current, text="北向").grid(row=1, column=6, sticky="w")
        ttk.Label(current, textvariable=self.north_var).grid(
            row=1, column=7, sticky="w", padx=8
        )

    def _build_tle_fields(self, parent: ttk.Frame) -> None:
        self.tle1_var = getattr(self, "tle1_var", tk.StringVar())
        self.tle2_var = getattr(self, "tle2_var", tk.StringVar())
        self.lat_var = getattr(self, "lat_var", tk.StringVar(value="30.0"))
        self.lon_var = getattr(self, "lon_var", tk.StringVar(value="120.0"))
        self.alt_var = getattr(self, "alt_var", tk.StringVar(value="0"))
        self.threshold_var = getattr(
            self, "threshold_var", tk.StringVar(value="10.0")
        )

        ttk.Label(parent, text="TLE 第1行").grid(row=0, column=0, sticky="w")
        ttk.Entry(parent, textvariable=self.tle1_var, width=72).grid(
            row=0, column=1, columnspan=5, sticky="ew", padx=4, pady=2
        )
        ttk.Label(parent, text="TLE 第2行").grid(row=1, column=0, sticky="w")
        ttk.Entry(parent, textvariable=self.tle2_var, width=72).grid(
            row=1, column=1, columnspan=5, sticky="ew", padx=4, pady=2
        )
        ttk.Label(parent, text="观测纬度").grid(row=2, column=0, sticky="w")
        ttk.Entry(parent, textvariable=self.lat_var, width=10).grid(
            row=2, column=1, sticky="w", padx=4
        )
        ttk.Label(parent, text="观测经度").grid(row=2, column=2, sticky="w")
        ttk.Entry(parent, textvariable=self.lon_var, width=10).grid(
            row=2, column=3, sticky="w", padx=4
        )
        ttk.Label(parent, text="海拔(m)").grid(row=2, column=4, sticky="w")
        ttk.Entry(parent, textvariable=self.alt_var, width=10).grid(
            row=2, column=5, sticky="w", padx=4
        )
        ttk.Label(parent, text="起始跟踪俯仰角(°)").grid(
            row=3, column=0, sticky="w"
        )
        ttk.Entry(parent, textvariable=self.threshold_var, width=10).grid(
            row=3, column=1, sticky="w", padx=4
        )
        parent.columnconfigure(1, weight=1)

    def _build_pass_tab(self) -> None:
        settings = ttk.LabelFrame(self.pass_tab, text="TLE 与观测站", padding=10)
        settings.pack(fill="x", pady=(0, 8))
        self._build_tle_fields(settings)
        ttk.Button(
            settings, text="计算未来24小时过境", command=self.compute_passes
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Label(
            settings,
            text="过境按地平线(0°)统计；跟踪从上面的起始俯仰角开始",
            foreground="#607d8b",
        ).grid(row=4, column=3, columnspan=3, sticky="w", pady=(6, 0))

        tree_frame = ttk.LabelFrame(self.pass_tab, text="未来过境列表", padding=8)
        tree_frame.pack(fill="both", expand=True, pady=(0, 8))
        columns = ("start", "end", "duration", "max_el", "max_time", "max_az")
        self._pass_tree = ttk.Treeview(
            tree_frame, columns=columns, show="headings", height=10
        )
        labels = {
            "start": "开始时间",
            "end": "结束时间",
            "duration": "持续时间",
            "max_el": "最大俯仰(°)",
            "max_time": "最大俯仰时间",
            "max_az": "最大方位(°)",
        }
        widths = {
            "start": 135,
            "end": 135,
            "duration": 100,
            "max_el": 100,
            "max_time": 135,
            "max_az": 100,
        }
        for col in columns:
            self._pass_tree.heading(col, text=labels[col])
            self._pass_tree.column(col, width=widths[col], anchor="center")
        yscroll = ttk.Scrollbar(
            tree_frame, orient="vertical", command=self._pass_tree.yview
        )
        self._pass_tree.configure(yscrollcommand=yscroll.set)
        self._pass_tree.pack(side="left", fill="both", expand=True)
        yscroll.pack(side="right", fill="y")

        buttons = ttk.Frame(self.pass_tab)
        buttons.pack(fill="x")
        ttk.Button(
            buttons,
            text="添加选中过境为任务",
            command=self.add_pass_task,
        ).pack(side="left", padx=4)
        ttk.Button(
            buttons, text="清空过境列表", command=self.clear_passes
        ).pack(side="left", padx=4)

    def _build_tle_tab(self) -> None:
        settings = ttk.LabelFrame(
            self.tle_tab, text="TLE 连续跟踪设置", padding=10
        )
        settings.pack(fill="x", pady=(0, 8))
        self._build_tle_fields(settings)
        ttk.Label(
            settings,
            text="跟踪方式：02 连续定点（每 150ms）",
            foreground="#2e7d32",
            font=("Microsoft YaHei UI", 10, "bold"),
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Button(
            settings,
            text="开始TLE连续跟踪",
            command=self.start_tle_tracking,
        ).grid(row=4, column=3, sticky="w", pady=(6, 0))
        ttk.Button(
            settings, text="停止", command=self.send_stop
        ).grid(row=4, column=4, sticky="w", pady=(6, 0))

        self.tle_track_status_var = tk.StringVar(value="未启动")
        info = ttk.LabelFrame(self.tle_tab, text="跟踪状态", padding=10)
        info.pack(fill="x", pady=(0, 8))
        ttk.Label(
            info,
            textvariable=self.tle_track_status_var,
            font=("Microsoft YaHei UI", 12, "bold"),
            foreground="#1565c0",
        ).pack(anchor="w")
        ttk.Label(
            info,
            text="当卫星俯仰角达到设定阈值后开始跟踪；低于阈值时停止并等待。",
            foreground="#607d8b",
        ).pack(anchor="w", pady=(6, 0))

    def _build_task_tab(self) -> None:
        tasks = ttk.LabelFrame(self.task_tab, text="追踪任务列表", padding=8)
        tasks.pack(fill="x", pady=(0, 8))
        columns = (
            "name",
            "kind",
            "start",
            "end",
            "max_el",
            "threshold",
            "status",
        )
        self._task_tree = ttk.Treeview(
            tasks, columns=columns, show="headings", height=8
        )
        labels = {
            "name": "任务名称",
            "kind": "类型",
            "start": "开始时间",
            "end": "结束时间",
            "max_el": "最大俯仰(°)",
            "threshold": "起始俯仰(°)",
            "status": "状态",
        }
        widths = {
            "name": 150,
            "kind": 80,
            "start": 130,
            "end": 130,
            "max_el": 100,
            "threshold": 100,
            "status": 100,
        }
        for col in columns:
            self._task_tree.heading(col, text=labels[col])
            self._task_tree.column(col, width=widths[col], anchor="center")
        yscroll = ttk.Scrollbar(
            tasks, orient="vertical", command=self._task_tree.yview
        )
        self._task_tree.configure(yscrollcommand=yscroll.set)
        self._task_tree.pack(side="left", fill="both", expand=True)
        yscroll.pack(side="right", fill="y")

        buttons = ttk.Frame(self.task_tab)
        buttons.pack(fill="x", pady=(0, 8))
        ttk.Button(
            buttons, text="删除任务", command=self.delete_task
        ).pack(side="left", padx=4)
        ttk.Button(
            buttons, text="清空任务", command=self.clear_tasks
        ).pack(side="left", padx=4)
        ttk.Label(
            buttons,
            text="任务按开始时间自动执行，无需手动点击",
            foreground="#2e7d32",
        ).pack(side="left", padx=12)

        logs = ttk.Notebook(self.task_tab)
        logs.pack(fill="both", expand=True)
        status_log_frame = ttk.Frame(logs, padding=4)
        udp_log_frame = ttk.Frame(logs, padding=4)
        logs.add(status_log_frame, text="状态日志")
        logs.add(udp_log_frame, text="UDP日志")

        self._status_log = tk.Text(status_log_frame, height=8, wrap="none")
        status_scroll = ttk.Scrollbar(
            status_log_frame,
            orient="vertical",
            command=self._status_log.yview,
        )
        self._status_log.configure(yscrollcommand=status_scroll.set)
        self._status_log.pack(side="left", fill="both", expand=True)
        status_scroll.pack(side="right", fill="y")

        self._udp_log = tk.Text(udp_log_frame, height=8, wrap="none")
        udp_scroll = ttk.Scrollbar(
            udp_log_frame, orient="vertical", command=self._udp_log.yview
        )
        self._udp_log.configure(yscrollcommand=udp_scroll.set)
        self._udp_log.pack(side="left", fill="both", expand=True)
        udp_scroll.pack(side="right", fill="y")

    def _build_status_bar(self, root: tk.Tk) -> None:
        self.clock_var = tk.StringVar(value="--")
        self.bar_az_var = tk.StringVar(value="方位：--")
        self.bar_el_var = tk.StringVar(value="俯仰：--")
        self.bar_state_var = tk.StringVar(value="未连接")

        bar = tk.Frame(root, bg="#1f2937", height=46)
        bar.pack(side="bottom", fill="x")
        font = ("Microsoft YaHei UI", 12, "bold")
        tk.Label(
            bar,
            textvariable=self.clock_var,
            bg="#1f2937",
            fg="#f9fafb",
            font=font,
            padx=18,
        ).pack(side="left", pady=10)
        tk.Label(
            bar,
            textvariable=self.bar_az_var,
            bg="#1f2937",
            fg="#93c5fd",
            font=font,
            padx=18,
        ).pack(side="left", pady=10)
        tk.Label(
            bar,
            textvariable=self.bar_el_var,
            bg="#1f2937",
            fg="#93c5fd",
            font=font,
            padx=18,
        ).pack(side="left", pady=10)
        tk.Label(
            bar,
            textvariable=self.bar_state_var,
            bg="#1f2937",
            fg="#fbbf24",
            font=font,
            padx=18,
        ).pack(side="right", pady=10)

    # ------------------------------------------------------------ connection
    def connect(self) -> None:
        if self._client is not None:
            self.disconnect()
        ip = self.ip_var.get().strip()
        try:
            send_port = int(self.send_port_var.get())
            recv_port = int(self.recv_port_var.get())
        except ValueError:
            messagebox.showerror("错误", "端口必须是数字")
            return

        config = NetworkConfig(
            rk3568_ip=ip,
            send_port=send_port,
            recv_port=recv_port,
        )
        try:
            self._client = RK3568UDPClient(
                config,
                on_send=lambda data: self._log_udp("发送", data),
                on_recv=lambda data: self._log_udp("接收", data),
            )
        except OSError as exc:
            messagebox.showerror("连接失败", str(exc))
            self._client = None
            return

        self._running = True
        self._thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._thread.start()
        self.link_var.set(f"已连接 {ip}:{send_port}")
        self.bar_state_var.set("已连接")

    def disconnect(self) -> None:
        self._stop_all_tracking(send_stop=False)
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        if self._client is not None:
            self._client.close()
            self._client = None
        self.link_var.set("未连接")
        self.bar_state_var.set("未连接")

    # -------------------------------------------------------------- controls
    def reset_turntable(self) -> None:
        if self._client is None:
            messagebox.showwarning("提示", "请先连接")
            return
        if not messagebox.askyesno(
            "确认复位",
            "确认将转台复位到方位 0°（正北）、俯仰 90°（天顶）？",
        ):
            return
        self._stop_all_tracking(send_stop=False)
        self.target_az_var.set("0.0")
        self.target_el_var.set("90.0")
        self._client.send_target(0.0, 90.0)
        self.link_var.set("已发送复位定点命令：方位 0° / 俯仰 90°")

    def execute_point(self) -> None:
        if self._client is None:
            messagebox.showwarning("提示", "请先连接")
            return
        try:
            az = float(self.target_az_var.get())
            el = float(self.target_el_var.get())
        except ValueError:
            messagebox.showerror("错误", "方位/俯仰必须是数字")
            return
        if not 0.0 <= az <= 360.0 or not 0.0 <= el <= 90.0:
            messagebox.showerror("范围错误", "方位需在 0~360°，俯仰需在 0~90°")
            return
        self._stop_all_tracking(send_stop=False)
        self._client.send_target(az, el)
        self.link_var.set(f"已发送定点：方位 {az:.1f}° / 俯仰 {el:.1f}°")

    # ----------------------------------------------------------- pass predict
    def compute_passes(self) -> None:
        try:
            threshold = float(self.threshold_var.get())
            passes = predict_passes(
                self.tle1_var.get(),
                self.tle2_var.get(),
                float(self.lat_var.get()),
                float(self.lon_var.get()),
                float(self.alt_var.get()),
                hours=24.0,
                step_seconds=20.0,
                min_elevation_deg=0.0,
            )
        except Exception as exc:
            messagebox.showerror("过境计算失败", str(exc))
            return
        self._passes = passes
        self._pass_rows.clear()
        for item in self._pass_tree.get_children():
            self._pass_tree.delete(item)
        for index, pass_info in enumerate(passes, start=1):
            iid = f"pass{index}"
            self._pass_rows[iid] = pass_info
            self._pass_tree.insert(
                "",
                "end",
                iid=iid,
                values=(
                    _fmt_local(pass_info.start),
                    _fmt_local(pass_info.end),
                    _fmt_duration(pass_info.duration_seconds),
                    f"{pass_info.max_elevation_deg:.1f}",
                    _fmt_local(pass_info.max_elevation_time),
                    f"{pass_info.max_azimuth_deg:.1f}",
                ),
            )
        if not passes:
            messagebox.showinfo("过境预报", "未来24小时内没有过境")
        else:
            self.link_var.set(
                f"未来24小时过境 {len(passes)} 次，起始跟踪俯仰 {threshold:.1f}°"
            )

    def clear_passes(self) -> None:
        self._passes.clear()
        self._pass_rows.clear()
        for item in self._pass_tree.get_children():
            self._pass_tree.delete(item)

    def add_pass_task(self) -> None:
        selection = self._pass_tree.selection()
        if not selection:
            messagebox.showwarning("提示", "请先选择一个过境")
            return
        pass_info = self._pass_rows[selection[0]]
        try:
            threshold = float(self.threshold_var.get())
        except ValueError:
            messagebox.showerror("错误", "起始跟踪俯仰角必须是数字")
            return
        if threshold > pass_info.max_elevation_deg:
            messagebox.showwarning(
                "提示",
                "起始跟踪俯仰角高于本次过境最大俯仰角，任务不会开始跟踪。",
            )
            return
        self._task_counter += 1
        task_id = f"task{self._task_counter}"
        task = Task(
            name=f"过境{self._task_counter} {_fmt_local(pass_info.start)}",
            kind="pass",
            tle1=self.tle1_var.get().strip(),
            tle2=self.tle2_var.get().strip(),
            lat=float(self.lat_var.get()),
            lon=float(self.lon_var.get()),
            alt=float(self.alt_var.get()),
            start=pass_info.start,
            end=pass_info.end,
            max_el=pass_info.max_elevation_deg,
            threshold_el=threshold,
            status="待执行",
        )
        self._tasks[task_id] = task
        self._refresh_task_tree()

    def start_tle_tracking(self) -> None:
        if self._client is None:
            messagebox.showwarning("提示", "请先连接")
            return
        try:
            threshold = float(self.threshold_var.get())
            TLECalculator(
                self.tle1_var.get(),
                self.tle2_var.get(),
                float(self.lat_var.get()),
                float(self.lon_var.get()),
                float(self.alt_var.get()),
            )
        except Exception as exc:
            messagebox.showerror("TLE连续跟踪启动失败", str(exc))
            return
        task = Task(
            name="TLE即时跟踪",
            kind="tle",
            tle1=self.tle1_var.get().strip(),
            tle2=self.tle2_var.get().strip(),
            lat=float(self.lat_var.get()),
            lon=float(self.lon_var.get()),
            alt=float(self.alt_var.get()),
            threshold_el=threshold,
            status="待执行",
        )
        self._start_pass_tracking(None, task)

    # ----------------------------------------------------------- task control
    def _refresh_task_tree(self) -> None:
        for item in self._task_tree.get_children():
            self._task_tree.delete(item)
        ordered = sorted(
            self._tasks.items(),
            key=lambda item: item[1].start
            or datetime.max.replace(tzinfo=timezone.utc),
        )
        for task_id, task in ordered:
            kind = "过境"
            start_text = _fmt_local(task.start)
            end_text = _fmt_local(task.end)
            max_el = f"{task.max_el:.1f}"
            threshold = f"{task.threshold_el:.1f}"
            self._task_tree.insert(
                "",
                "end",
                iid=task_id,
                values=(
                    task.name,
                    kind,
                    start_text,
                    end_text,
                    max_el,
                    threshold,
                    task.status,
                ),
            )

    def _set_task_status(self, task_id: str | None, status: str) -> None:
        if task_id is None:
            return
        task = self._tasks.get(task_id)
        if task is None:
            return
        task.status = status
        if self._task_tree.exists(task_id):
            self._task_tree.set(task_id, "status", status)

    def delete_task(self) -> None:
        for task_id in self._task_tree.selection():
            if task_id == self._active_task:
                self._stop_all_tracking()
            self._tasks.pop(task_id, None)
            if self._task_tree.exists(task_id):
                self._task_tree.delete(task_id)

    def clear_tasks(self) -> None:
        self._stop_all_tracking()
        self._tasks.clear()
        for item in self._task_tree.get_children():
            self._task_tree.delete(item)

    def _start_pass_tracking(self, task_id: str | None, task: Task) -> None:
        if self._client is None:
            return
        try:
            calculator = TLECalculator(
                task.tle1,
                task.tle2,
                task.lat,
                task.lon,
                task.alt,
            )
        except Exception as exc:
            messagebox.showerror("跟踪启动失败", str(exc))
            return
        self._stop_all_tracking(send_stop=False)
        self._active_task = task_id
        self._pass_task = task
        self._pass_calculator = calculator
        self._pass_in_track = False
        self._set_task_status(task_id, "等待开始")
        self.tle_track_status_var.set("TLE跟踪已启动，等待卫星升起")
        self._pass_tracking_tick()

    def _pass_tracking_tick(self) -> None:
        self._pass_after_id = None
        if (
            not self._running
            or self._client is None
            or self._pass_task is None
            or self._pass_calculator is None
        ):
            return
        task = self._pass_task
        task_id = self._active_task
        now = datetime.now(timezone.utc)

        if task.start is not None and now < task.start:
            self._set_task_status(task_id, "等待开始")
            self.tle_track_status_var.set(
                f"等待过境开始：{_fmt_local(task.start)}"
            )
            self._pass_after_id = self.root.after(500, self._pass_tracking_tick)
            return
        if task.end is not None and now > task.end:
            self._client.send_stop()
            self._set_task_status(task_id, "已完成")
            self.tle_track_status_var.set("本次过境已结束")
            self._stop_all_tracking(send_stop=False)
            return

        try:
            az, el = self._pass_calculator.compute_az_el(now)
        except Exception as exc:
            self.tle_track_status_var.set(f"TLE计算失败：{exc}")
            self._stop_all_tracking()
            return

        az = az % 360.0
        self.target_az_var.set(f"{az:.3f}")
        self.target_el_var.set(f"{el:.3f}")

        if el < task.threshold_el or el > 90.0:
            if self._pass_in_track:
                self._client.send_stop()
                self._pass_in_track = False
            self._set_task_status(task_id, "等待升起")
            self.tle_track_status_var.set(
                f"当前俯仰 {el:.1f}°，低于起始阈值 "
                f"{task.threshold_el:.1f}°，等待"
            )
            self._pass_after_id = self.root.after(
                300, self._pass_tracking_tick
            )
            return

        self._client.send_target(az, el)
        self._pass_in_track = True
        self._set_task_status(task_id, "跟踪中")
        self.tle_track_status_var.set(
            f"02连续定点跟踪中：方位 {az:.2f}°，俯仰 {el:.2f}°"
        )
        self._pass_after_id = self.root.after(150, self._pass_tracking_tick)

    def _stop_all_tracking(self, send_stop: bool = True) -> None:
        if self._pass_after_id is not None:
            self.root.after_cancel(self._pass_after_id)
            self._pass_after_id = None
        if send_stop and self._client is not None:
            self._client.send_stop()
        if self._active_task is not None:
            task = self._tasks.get(self._active_task)
            if task is not None and task.status not in ("已完成",):
                self._set_task_status(self._active_task, "已停止")
        self._active_task = None
        self._pass_task = None
        self._pass_calculator = None
        self._pass_in_track = False
        if hasattr(self, "tle_track_status_var"):
            self.tle_track_status_var.set("未启动")

    def _scheduler_tick(self) -> None:
        self._scheduler_after_id = None
        if self._running and self._client is not None and self._pass_task is None:
            now = datetime.now(timezone.utc)
            ordered = sorted(
                self._tasks.items(),
                key=lambda item: item[1].start
                or datetime.max.replace(tzinfo=timezone.utc),
            )
            for task_id, task in ordered:
                if task.status != "待执行":
                    continue
                if task.end is not None and now > task.end:
                    self._set_task_status(task_id, "已过期")
                    continue
                if task.start is not None and now >= task.start:
                    self._start_pass_tracking(task_id, task)
                    break
        self._scheduler_after_id = self.root.after(1000, self._scheduler_tick)

    def send_stop(self) -> None:
        if self._client is None:
            messagebox.showwarning("提示", "请先连接")
            return
        self._stop_all_tracking(send_stop=True)

    def send_query(self) -> None:
        if self._client is None:
            messagebox.showwarning("提示", "请先连接")
            return
        self._client.send_query()

    # --------------------------------------------------------------- network
    def _recv_loop(self) -> None:
        timeout_count = 0
        while self._running and self._client is not None:
            try:
                data = self._client.recv_raw()
                timeout_count = 0
                self._handle_incoming(data)
            except socket.timeout:
                timeout_count += 1
                if timeout_count == 1:
                    self._recv_queue.put(("info", "已连接，等待转台状态..."))
            except OSError as exc:
                self._recv_queue.put(("error", f"通信错误: {exc}"))
                break

    def _handle_incoming(self, data: bytes) -> None:
        if data and data[0] == TURNTABLE_HEADER:
            try:
                status = parse_status_frame(data)
            except Exception as exc:
                self._recv_queue.put(("error", f"转台状态解析失败: {exc}"))
                return
            self._recv_queue.put(
                DeviceStatus(
                    az_deg=status.az_deg,
                    el_deg=status.el_deg,
                    az_speed_dps=status.az_speed_dps,
                    el_speed_dps=status.el_speed_dps,
                    fault=status.fault,
                    raw=status.raw,
                )
            )
            return
        if data[:2] == b"\xEB\x90":
            self._handle_aux_incoming(data)
            return
        self._recv_queue.put(
            ("error", f"未知帧头: {data[:4].hex(' ').upper()}")
        )

    def _handle_aux_incoming(self, data: bytes) -> None:
        frame = parse_aux_frame(data)
        if frame.frame_type == TYPE_TLE:
            line1, line2 = decode_tle(frame.data)
            self._recv_queue.put(("tle", line1, line2))
        elif frame.frame_type == TYPE_IMU_STATE:
            yaw, pitch, roll = decode_imu(frame.data)
            self._recv_queue.put(("imu", yaw, pitch, roll))

    # --------------------------------------------------------------- display
    def _poll_queue(self) -> None:
        try:
            while True:
                item = self._recv_queue.get_nowait()
                if isinstance(item, DeviceStatus):
                    self._apply_status(item)
                elif isinstance(item, tuple) and item and item[0] == "imu":
                    _tag, yaw, pitch, roll = item
                    self.current_az_var.set(f"{_wrap_angle(yaw):.3f}°")
                    self.current_el_var.set(f"{pitch:.3f}°")
                    self.north_var.set("已校准（方位0°为正北）")
                    self.bar_az_var.set(f"方位：{_wrap_angle(yaw):.1f}°")
                    self.bar_el_var.set(f"俯仰：{pitch:.1f}°")
                    self.link_var.set("已接收九轴初始姿态，北向已校准")
                    self.bar_state_var.set("北向已校准")
                elif isinstance(item, tuple) and item and item[0] == "tle":
                    _tag, line1, line2 = item
                    self.tle1_var.set(line1)
                    self.tle2_var.set(line2)
                    self.link_var.set("已接收TLE，请点击计算过境或开始跟踪")
                elif isinstance(item, tuple) and item and item[0] == "info":
                    self.link_var.set(item[1])
                elif isinstance(item, tuple) and item and item[0] == "error":
                    self.link_var.set(item[1])
                elif isinstance(item, tuple) and item and item[0] == "udp":
                    _tag, direction, hex_data = item
                    self._udp_log.insert(
                        "end", f"[{direction}] {hex_data}\n"
                    )
                    self._udp_log.see("end")
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _apply_status(self, status: DeviceStatus) -> None:
        self._last_status = status
        self.current_az_var.set(f"{status.az_deg:.3f}°")
        self.current_el_var.set(f"{status.el_deg:.3f}°")
        self.az_speed_var.set(f"{status.az_speed_dps:.3f}°/s")
        self.el_speed_var.set(f"{status.el_speed_dps:.3f}°/s")
        self.fault_var.set(f"0x{status.fault:02X}")
        self.status_var.set("状态正常" if status.fault == 0 else "故障")
        self.link_var.set("已连接（状态正常）")
        self.bar_az_var.set(f"方位：{status.az_deg:.1f}°")
        self.bar_el_var.set(f"俯仰：{status.el_deg:.1f}°")
        self.bar_state_var.set(
            "状态正常" if status.fault == 0 else f"故障 0x{status.fault:02X}"
        )
    def _clock_tick(self) -> None:
        self.clock_var.set(
            datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )
        self.root.after(1000, self._clock_tick)

    def _status_log_tick(self) -> None:
        if self._last_status is not None:
            status = self._last_status
            stamp = datetime.now().strftime("%H:%M:%S")
            self._status_log.insert(
                "end",
                f"[{stamp}] 方位={status.az_deg:.1f}° "
                f"俯仰={status.el_deg:.1f}° "
                f"方位速度={status.az_speed_dps:.1f}°/s "
                f"俯仰速度={status.el_speed_dps:.1f}°/s "
                f"故障=0x{status.fault:02X}\n",
            )
            self._status_log.see("end")
        self.root.after(1000, self._status_log_tick)

    def _log_udp(self, direction: str, data: bytes) -> None:
        self._recv_queue.put(("udp", direction, data.hex(" ").upper()))

    def on_close(self) -> None:
        self.disconnect()
        self.root.quit()
        self.root.destroy()
