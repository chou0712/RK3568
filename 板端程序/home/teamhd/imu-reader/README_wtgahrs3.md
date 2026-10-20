# WTGAHRS3-485 数据采集

## 已确认参数

- 型号：WTGAHRS3-485
- 协议：Modbus RTU，功能码 `0x03`
- 波特率：9600，8N1
- 当前设备地址：`0x10`
- 数据寄存器：从 `0x34` 连续读取 13 个寄存器
- 连接方式：WTGAHRS3-485 A/B -> USB-RS485 -> RK3568 USB

程序当前读取加速度、角速度、姿态角和温度，以一行一个 JSON 对象输出到标准输出，同时可将每个样本作为一个 UDP 数据报发送。当前阶段不写数据文件，也不验证 GNSS 定位。

## 通过网口/光纤发送惯导数据

网口转光纤模块是透明的二层介质转换器，不需要专用驱动。程序只需从连接光纤模块的 `eth1` 发出普通 UDP 数据。当前初始配置为：

- RK3568 源地址：`192.168.1.101`
- 目标地址：`192.168.1.255`（同网段广播，远端 IP 未确定时使用）
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

已知远端接收机 IP 后，建议把服务中的 `--udp-host 192.168.1.255` 改成该 IP，使用单播。

## 在 RK3568 上部署

插入 USB-RS485 后先检查：

```bash
ls -l /dev/ttyUSB*
dmesg | tail -n 30
```

CH340 转换器通常显示为 `/dev/ttyUSB0`。部署程序：

```bash
mkdir -p /home/teamhd/imu-reader
cp wtgahrs3_reader.py /home/teamhd/imu-reader/
chmod +x /home/teamhd/imu-reader/wtgahrs3_reader.py
python3 /home/teamhd/imu-reader/wtgahrs3_reader.py \
  --port /dev/ttyUSB0 --baud 9600 --address 0x10 --count 10 --pretty \
  --udp-host 192.168.1.255 --udp-port 20002 --udp-source 192.168.1.101
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

- `No such file or directory`：检查实际设备节点是否为 `/dev/ttyUSB1` 等。
- `Permission denied`：将 `teamhd` 加入 `dialout` 组，或检查 udev 权限。
- 超时、收到 0 字节：确认上位机已关闭、A/B 未接反、传感器供电正常。
- CRC 错误：缩短线缆，检查共地、屏蔽和终端电阻，确认只有一个程序占用串口。
- 设备无响应：确认地址仍是 `0x10`，不要在上位机中修改设备地址或波特率。

## 命令行参数

```text
--port /dev/ttyUSB0   串口设备
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
