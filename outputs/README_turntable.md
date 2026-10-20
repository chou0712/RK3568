# RK3568 转台接入说明

转台通过四线全双工 USB-RS422 转换器接入 RK3568。RK3568 在独立 UDP 端口 `20003` 上透明转发转台原生 `AA` 协议帧，不修改现有摄像头二层转发服务，也不占用惯导的 UDP `20002` 或 `/dev/ttyS0`。

## 最终通路

```text
转台 RS422 115200 8N1
  <-> FTDI USB-RS422 /dev/serial/by-id/usb-FTDI_FT232R_USB_UART_A9UOWY7G-if00-port0
  <-> RK3568 turntable-gateway.service 192.168.1.101:20003
  <-> 上位机 192.168.1.102:20003
```

每个 UDP 数据报只承载一个完整的转台原生帧。网关仅接受源地址为 `192.168.1.102`、设备 ID 为 `01`、命令/长度合法且累加和正确的控制帧。串口回传则按帧头、命令、长度和校验和重组后发送至上位机。

## 上位机命令

在 `E:\RK3568` 运行：

```powershell
python outputs\turntable_udp_client.py status
python outputs\turntable_udp_client.py --count 0 listen
python outputs\turntable_udp_client.py fault
python outputs\turntable_udp_client.py driver-status
python outputs\turntable_udp_client.py settings
python outputs\turntable_udp_client.py stop
```

运动命令只有在现场机械范围和人员安全确认后执行：

```powershell
python outputs\turntable_udp_client.py point 10 5
```

`step` 子命令已经禁用。2026-10-16 实机测试时，文档所述“双轴步进 +1°、0°”帧被设备表现为朝零位大幅运动，而不是小角度相对步进。网关会拒绝该子命令，除非以后通过厂家确认协议语义并重新验证。

## 板端检查

```bash
systemctl is-active turntable-gateway.service
systemctl is-enabled turntable-gateway.service
sudo journalctl -u turntable-gateway.service -n 50 --no-pager
ss -lunp | grep 20003
```

服务文件使用 USB 转换器的稳定 `by-id` 路径，避免重启或插拔后 `ttyUSB` 编号变化。当前设备 ID 为 `usb-FTDI_FT232R_USB_UART_A9UOWY7G-if00-port0`；更换转换器后必须同步修改服务文件。

## 接线和安全

| USB-RS422 | 转台控制口 |
|---|---|
| TX+ | 8脚 RS422B_RX+ |
| TX- | 9脚 RS422B_RX- |
| RX+ | 6脚 RS422B_TX+ |
| RX- | 7脚 RS422B_TX- |
| GND | 10脚 RS422B_GND |

如果约每 `100 ms` 有数据但始终不是 `AA` 帧，断电后只交换 USB 转换器的 `RX+` 和 `RX-`。如果能收到状态但状态查询无应答，断电后检查或交换 USB 转换器的 `TX+` 和 `TX-`。不要带电改接差分线。
