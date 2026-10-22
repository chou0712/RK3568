# WTGAHRS3-TTL USB数据采集

## 已确认参数

- 型号：WTGAHRS3-TTL
- 协议：WIT标准串口协议，11字节`0x55`帧
- 波特率：9600，8N1
- 连接方式：WTGAHRS3-TTL -> TTL转USB模块 -> RK3568下方USB口
- 上方USB口：转台FTDI USB-RS422
- 下方USB口：Silicon Labs CP2102 TTL转USB（`10c4:ea60`），服务使用稳定`by-id`路径

程序当前读取加速度、角速度、姿态角和温度，以一行一个 JSON 对象输出到标准输出，同时可将每个样本作为一个 UDP 数据报发送。当前阶段不写数据文件，也不验证 GNSS 定位。

2026-10-20已在当前RK3568完成部署：进程实际打开CP2102对应的`/dev/ttyUSB1`，
上位机连续接收50包，丢包0，频率10.02Hz；转台和摄像头转发服务同时保持`active`。

## 通过网口2发送惯导数据

网口1连接摄像头，网口2连接上位机。惯导程序从连接上位机的`eth1`发出普通UDP数据。当前配置为：

- RK3568 源地址：`192.168.1.101`
- 目标地址：`192.168.1.102`（当前上位机）
- 目标端口：`20002`（专用于惯导，避免与 RS-422 的 `20000/20001` 冲突）
- 一条惯导样本对应一个 UDP 数据报
- 外层二进制帧：`EB 90 + 总长度(2 字节大端) + type + 变长数据 + checksum`
- 当前周期惯导数据的 type 为 `0x82`（角度应答）
- 数据区为 UTF-8 JSON，协议标识 `wtgahrs3.v1`，含连续序号和 UTC 时间戳
- checksum 为整帧除 checksum 自身外所有字节累加后取低 8 位

帧长是整帧总长度，包含帧头、帧长字段、type、数据和 checksum，最小值为 6。例如 type `0x82`、数据 `01 02` 的完整帧为：

```text
EB 90 00 08 82 01 02 08
```

已登记的 type：

| type | 含义 |
|---|---|
| `0x01` | 查询状态 |
| `0x02` | 设置目标角度 |
| `0x03` | 开始跟踪 |
| `0x04` | 停止 |
| `0x81` | 状态应答 |
| `0x82` | 角度应答 |

RK3568 同时监听 `192.168.1.101:20002`，支持电脑发送以下控制帧：

- `0x01` 查询状态：数据区为空；RK3568 单播返回 `0x81`。
- `0x02` 设置目标角度：数据区为 `{"x":1.5,"y":-2.0,"z":90.0}` UTF-8 JSON；目标值保存在软件内存中，不驱动物理云台；返回 `0x81`。
- `0x03` 开始跟踪：数据区为空；恢复周期发送 `0x82`，返回 `0x81`。
- `0x04` 停止：数据区为空；暂停周期发送 `0x82`，传感器采集仍继续，返回 `0x81`。

`0x81` 数据区是 `wtgahrs3.status.v1` JSON，包含传感器在线状态、跟踪开关、目标角度和最近序号。

Windows 命令示例：

```powershell
python outputs\imu_udp_command.py status
python outputs\imu_udp_command.py stop
python outputs\imu_udp_command.py start
python outputs\imu_udp_command.py set-angle --x 10 --y -5 --z 90
```

远端 Linux 接收示例：

```bash
nc -u -l 20002
```

项目自带接收与丢包检查工具：

```bash
python3 imu_udp_receiver.py --bind 0.0.0.0 --port 20002 --count 100
```

远端 Python 接收示例：

```python
import socket

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind(("0.0.0.0", 20002))
while True:
    payload, peer = s.recvfrom(4096)
    print(peer, payload.decode("utf-8").strip())
```

当前使用`192.168.1.102`单播；更换上位机地址时需要同步修改服务。

## 在 RK3568 上部署

将转台接上方USB口、TTL转USB惯导接下方USB口后先检查：

```bash
ls -l /dev/ttyUSB* /dev/ttyACM* 2>/dev/null
ls -l /dev/serial/by-id/ /dev/serial/by-path/ 2>/dev/null
dmesg | tail -n 30
```

`ttyUSB0/1`会随插拔顺序变化，不能据此判断上下USB口。实测下方USB的CP2102
稳定路径如下，服务和前台验证均使用该路径：

```text
/dev/serial/by-id/usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_0001-if00-port0
```

程序同时保留`--port auto`作为更换转换器后的后备功能；自动模式通过有效WIT帧
识别惯导，并使用排他锁避免与转台服务共同读取同一串口。部署程序：

```bash
mkdir -p /home/teamhd/imu-reader
cp wtgahrs3_reader.py /home/teamhd/imu-reader/
chmod +x /home/teamhd/imu-reader/wtgahrs3_reader.py
python3 /home/teamhd/imu-reader/wtgahrs3_reader.py \
  --transport ttl \
  --port /dev/serial/by-id/usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_0001-if00-port0 \
  --baud 9600 --count 10 --pretty \
  --udp-host 192.168.1.102 --udp-port 20002 --udp-source 192.168.1.101
```

如果普通用户没有串口权限：

```bash
sudo usermod -aG dialout teamhd
```

重新登录后生效。也可以在首次测试时使用 `sudo`，但不建议长期以 root 运行。

## systemd 服务

只有在前台读取稳定后再安装服务：

```bash
sudo cp wtgahrs3-reader.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wtgahrs3-reader.service
sudo journalctl -u wtgahrs3-reader.service -f
```

服务日志直接进入 journal，不会在磁盘上创建采集数据文件。

## 常见故障

- `no USB serial ports were found`：检查下方USB口、转换器供电及内核日志。
- `no USB serial port produced valid ... frames`：确认惯导为WIT标准输出、9600 8N1、TX/RX交叉并共地。
- `Permission denied`：将 `teamhd` 加入 `dialout` 组，或检查 udev 权限。
- 转台异常：确认转台服务使用FTDI稳定路径，惯导服务使用CP2102稳定路径。
- 校验错误：确认使用TTL电平转换器而不是RS232/RS485转换器，并检查供电和共地。

## 命令行参数

```text
--transport ttl       WTGAHRS3-TTL被动接收模式（默认）
--port auto           自动识别有效WIT数据的USB串口（默认）
--exclude-port PATH   自动识别时排除指定串口，可重复使用
--probe-timeout 1.5   每个候选USB串口的探测时间（秒）
--baud 9600           串口波特率
--address 0x10        Modbus 地址，支持十进制或 0x 前缀
--interval 0.2        轮询周期，单位秒
--timeout 1.0         单次响应超时
--count 10            读取指定次数；0 表示持续运行
--pretty              格式化 JSON 输出
--udp-host ADDRESS    UDP 目标 IPv4 地址；省略时不发送 UDP
--udp-port 20002      UDP 目标端口
--udp-source ADDRESS  本机源 IPv4，用于选择输出网口
--udp-listen-port 20002  本机命令监听端口；0 表示关闭接收
--udp-type 0x82       外层协议 type
--decode-hex FRAME    离线解析抓取的 Modbus 响应
```
