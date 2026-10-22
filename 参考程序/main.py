"""Entry point for the antenna upper-computer desktop application."""

from __future__ import annotations

import argparse

from config import DEFAULT_NETWORK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="室内电脑天线控制上位机（TLE + UDP + 桌面窗口）"
    )
    parser.add_argument("--ip", default=None, help="RK3568 IP 地址")
    parser.add_argument("--send-port", type=int, default=DEFAULT_NETWORK.send_port)
    parser.add_argument("--recv-port", type=int, default=DEFAULT_NETWORK.recv_port)
    return parser


def main() -> int:
    args = build_parser().parse_args()

    try:
        import tkinter as tk
        from app import UpperComputerApp

        root = tk.Tk()
        app = UpperComputerApp(
            root,
            ip=args.ip or DEFAULT_NETWORK.rk3568_ip,
            send_port=args.send_port,
            recv_port=args.recv_port,
        )
        root.mainloop()
    except Exception as exc:
        print(f"启动失败: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
