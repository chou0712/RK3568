# RK3568 摄像头、惯导与转台系统软件实现说明

> 最后更新：2026-10-20（Asia/Shanghai）
> 本文重点说明系统的软件实现、数据流，以及 RK3568 和上位机分别部署的程序与配置。硬件接线和完整验收信息参见 `HANDOFF.md`。

## 1. 软件总体架构

系统包含三条彼此独立、可以同时运行的数据通道：

```text
摄像头通道
摄像头 Ethernet
    -> RK3568 eth0
    -> ether_relay 用户态二层转发
    -> RK3568 eth1
    -> RK3568网口2 / eth1
    -> 上位机浏览器/VLC/摄像头客户端
```

```text
惯导通道
WTGAHRS3-TTL -> CP2102 TTL转USB（下方USB口）
    -> RK3568 CP2102稳定by-id路径
    -> wtgahrs3_reader.py 接收、校验、解析
    -> 项目 EB90 UDP 协议封装
    -> RK3568 eth1
    -> RK3568网口2 / eth1
    -> 上位机 imu_udp_receiver.py
```

```text
转台通道
上位机 turntable_control.py
    -> 原生 AA 协议 / UDP 20003
    -> RK3568 turntable_gateway.py
    -> FTDI USB-RS422 串口
    -> 转台
    -> 状态帧按原路径返回上位机
```

摄像头通道工作在以太网二层，不理解视频协议；惯导通道在应用层将USB串口数据转换成项目EB90/JSON UDP协议；转台通道保留设备原生AA二进制帧，仅由RK3568在UDP与RS422之间进行校验和转发。摄像头接网口1，上位机接网口2；上方USB属于转台，下方USB属于惯导。三个通道使用独立进程、串口和UDP端口。

## 2. RK3568 端部署内容

### 2.1 操作系统与基础配置

RK3568 运行：

```text
Ubuntu 20.04.6 LTS
Linux 5.10.226 aarch64
```

主要网络配置：

```text
eth0：摄像头侧，不配置业务IP
eth1（网口2）：上位机侧，192.168.1.101/24
```

厂家内核没有启用 `CONFIG_BRIDGE`，不能使用标准 Linux bridge。因此摄像头透明转发不是通过 `br0` 实现，而是使用自研的用户态原始套接字程序。

RK3568 端部署三个 systemd 服务：

| 服务 | 作用 | 开机启动 |
|---|---|---|
| `ether-relay.service` | 在 `eth0` 与 `eth1` 之间透明转发完整以太网帧 | enabled |
| `wtgahrs3-reader.service` | 采集 TTL 惯导、解析并向电脑发送 UDP | enabled |
| `turntable-gateway.service` | 在 USB-RS422 与上位机 UDP 之间双向转发转台原生帧 | enabled |

惯导服务声明：

```ini
Wants=ether-relay.service
After=ether-relay.service
```

转台服务同样在网络就绪和`ether-relay.service`之后启动。三个程序仍然是独立进程：摄像头转发使用`eth0/eth1`原始套接字；惯导使用下方USB口CP2102的稳定`by-id`路径和UDP `20002`；转台使用上方USB口FTDI的稳定`by-id`路径和UDP `20003`。两个串口程序还使用排他文件锁。

## 3. RK3568 摄像头转发软件

### 3.1 部署文件

板端当前部署：

```text
/home/teamhd/camera-relay/ether_relay
/etc/systemd/system/ether-relay.service
```

运行命令：

```text
/home/teamhd/camera-relay/ether_relay eth0 eth1
```

`ether_relay` 是本机 AArch64 可执行程序，由 C 语言实现。

### 3.2 为什么采用用户态转发

初始方案考虑使用 Linux bridge：

```bash
ip link add name br0 type bridge
```

但当前内核显示：

```text
# CONFIG_BRIDGE is not set
```

系统也没有与运行内核匹配的 bridge 模块，因此安装 `bridge-utils` 或调整 `iproute2` 都不能解决。最终采用 `AF_PACKET` 原始套接字进行二层透明转发。

### 3.3 程序实现方式

程序分别为 `eth0` 和 `eth1` 创建：

```text
AF_PACKET / SOCK_RAW / ETH_P_ALL
```

核心处理流程：

1. 打开并绑定 `eth0`、`eth1` 原始套接字。
2. 将两个接口设置为混杂接收。
3. 使用 `poll()` 同时等待两个方向的数据。
4. 从 `eth0` 收到完整 Ethernet 帧后，不修改内容，直接从 `eth1` 发出。
5. 从 `eth1` 收到完整 Ethernet 帧后，不修改内容，直接从 `eth0` 发出。
6. 过滤本程序自己发出的 `PACKET_OUTGOING` 数据，避免形成软件循环。
7. 每5秒记录两个方向的帧数、字节数和错误计数。

由于转发的是完整以太网帧，所以程序自然支持：

- ARP；
- IPv4、IPv6；
- TCP、UDP；
- 广播和组播；
- 摄像头 HTTP、RTSP、设备管理协议及其后续数据连接。

程序不执行以下工作：

- 不解码 H.264/H.265；
- 不修改摄像头 IP；
- 不重新封装视频；
- 不代理 RTSP；
- 不使用惯导端口 `20002`。

### 3.4 网卡 offload 处理

原始套接字转发时，GRO/GSO/TSO 可能让程序收到大于物理 MTU 的聚合包，重新从另一网口发送时会出现：

```text
send failed: Message too long
```

服务启动前关闭两个接口的相关 offload：

```bash
ethtool -K eth0 gro off lro off gso off tso off
ethtool -K eth1 gro off lro off gso off tso off
```

关闭后，程序按物理以太网帧转发，现场验收期间 `ERR=0`。

### 3.5 systemd 的职责

`ether-relay.service` 负责：

1. 避免 NetworkManager同时修改两个业务网口。
2. 清除不需要的旧地址。
3. 拉起 `eth0`、`eth1`。
4. 在 `eth1` 配置 `192.168.1.101/24`。
5. 关闭网卡 offload。
6. 启动 `ether_relay eth0 eth1`。
7. 程序异常退出时自动重启。

因此，停止该服务后，网口不会自动恢复到普通 NetworkManager 管理状态。

## 4. RK3568 惯导采集与UDP网关

### 4.1 部署文件

板端部署：

```text
/home/teamhd/imu-reader/wtgahrs3_reader.py
/etc/systemd/system/wtgahrs3-reader.service
```

工作区对应文件：

```text
outputs/wtgahrs3_reader.py
outputs/wtgahrs3-reader.service
```

程序只使用 Python 3 标准库，不依赖 `pyserial` 或其他第三方 Python 包。

### 4.2 服务运行参数

板端服务等价于执行：

```bash
/usr/bin/python3 /home/teamhd/imu-reader/wtgahrs3_reader.py \
  --transport ttl \
  --port /dev/serial/by-id/usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_0001-if00-port0 \
  --baud 9600 \
  --udp-host 192.168.1.102 \
  --udp-port 20002 \
  --udp-source 192.168.1.101 \
  --udp-listen-port 20002 \
  --udp-type 0x82
```

服务以普通用户 `teamhd` 运行，通过补充组 `dialout` 获得串口访问权限：

```ini
User=teamhd
Group=teamhd
SupplementaryGroups=dialout
```

正常样本不持续写入 journal，避免10Hz日志长期占用磁盘；错误信息仍进入 journal：

```ini
StandardOutput=null
StandardError=journal
Restart=on-failure
RestartSec=2
```

### 4.3 串口层实现

惯导使用：

```text
下方USB口的CP2102稳定`by-id`串口
9600 baud
8 data bits
no parity
1 stop bit
```

程序直接通过`os.open`、`flock`、`termios`和`select`操作串口：

- `termios` 配置9600 8N1和 raw模式；
- `flock`取得排他锁，避免与转台或其他串口程序同时读取一个设备；
- 自动枚举`by-id`、`by-path`、`ttyUSB`和`ttyACM`，以有效WIT帧识别惯导；
- 当前服务直接绑定下方CP2102稳定`by-id`路径；`--port auto`仅作为更换转换器后的后备识别方式；
- `select` 等待数据，避免永久阻塞；
- 内部字节缓冲区处理任意长度的分片读取；
- 搜索 `0x55` 帧头完成流重新同步；
- 每11字节构成一帧；
- 校验前10字节累加值的低8位是否等于第11字节。

无效帧不会用于生成 UDP 样本。若发生错位，程序逐字节移动，直到重新找到一个校验正确的11字节帧。

### 4.4 WIT数据解析

当前使用以下子帧：

| 子帧类型 | 内容 | 换算 |
|---|---|---|
| `0x51` | 三轴加速度、温度 | 加速度原始值 `/32768×16g`；温度 `/100°C` |
| `0x52` | 三轴角速度 | 原始值 `/32768×2000°/s` |
| `0x53` | 三轴姿态角 | 原始值 `/32768×180°` |

程序保存最近一组有效 `0x51` 和 `0x52` 数据。当收到后续有效 `0x53` 时，将三种数据组合为一条完整 `ImuSample`：

```json
{
  "timestamp_utc": "2026-09-22T11:50:35.049+00:00",
  "device_address": 0,
  "acceleration_g": {"x": 0.0, "y": 0.0, "z": 1.0117},
  "angular_velocity_dps": {"x": 0.0, "y": 0.0, "z": 0.0},
  "angle_deg": {"x": -0.1099, "y": 0.5054, "z": -2.2632},
  "temperature_c": 27.99
}
```

设备还会输出时间、端口状态和GNSS相关帧；当前业务样本只使用加速度、角速度、姿态角和温度。

### 4.5 UDP封装

生成一条完整惯导样本后，程序增加：

```text
schema = wtgahrs3.v1
sequence = 连续递增序号
timestamp_utc = UTC时间
```

JSON数据再封装进项目二进制外层协议：

```text
偏移  长度  内容
0     2     EB 90
2     2     整帧总长度，大端
4     1     type
5     N     UTF-8 JSON数据
末尾  1     累加checksum
```

checksum算法：

```text
checksum = sum(从EB到数据区最后一个字节) & 0xFF
```

周期惯导数据使用：

```text
type = 0x82
目标 = 192.168.1.102:20002
源地址 = 192.168.1.101
```

当前使用单播而不是广播，减少无关设备处理惯导数据，并明确数据只发送到验收电脑。

### 4.6 反向控制

同一个 Python 进程监听 RK3568 的 UDP `20002`，支持上位机发送：

| type | 功能 |
|---|---|
| `0x01` | 查询网关状态 |
| `0x02` | 设置软件目标角度 |
| `0x03` | 开始发送周期数据 |
| `0x04` | 停止发送周期数据 |

板端返回 `0x81` 状态应答。状态内容包括：

- 惯导是否在线；
- 周期发送是否启用；
- 软件目标角度；
- 最近发送序号。

`0x02` 只保存软件目标值，不直接驱动物理云台。

## 5. RK3568 转台 RS422-UDP 网关

### 5.1 部署文件

板端当前部署：

```text
/home/teamhd/turntable/turntable_gateway.py
/etc/systemd/system/turntable-gateway.service
```

工作区对应文件：

```text
outputs/turntable_gateway.py
outputs/turntable-gateway.service
outputs/turntable_udp_client.py
outputs/README_turntable.md
```

网关只使用 Python 3 标准库，不依赖第三方串口包。

### 5.2 服务运行参数

板端服务等价于执行：

```bash
/usr/bin/python3 /home/teamhd/turntable/turntable_gateway.py \
  --port /dev/serial/by-id/usb-FTDI_FT232R_USB_UART_A9UOWY7G-if00-port0 \
  --baud 115200 \
  --bind 192.168.1.101 \
  --listen-port 20003 \
  --udp-host 192.168.1.102 \
  --udp-port 20003 \
  --allowed-host 192.168.1.102
```

使用 `/dev/serial/by-id/...` 而不是易变化的 `/dev/ttyUSB0`，避免 USB 设备枚举顺序变化后连接到错误串口。串口参数为 `115200 8N1`，服务以 `teamhd` 用户运行并通过 `dialout` 组取得串口访问权限。

### 5.3 双向数据流

上位机到转台：

1. 上位机生成一帧完整的转台原生 AA 协议命令。
2. 每个 UDP 数据报只承载一帧转台命令，并发送到 `192.168.1.101:20003`。
3. 网关验证来源 IP、帧头、长度、命令、子命令和 checksum。
4. 验证通过后，将原始帧写入 FTDI USB-RS422 串口。

转台到上位机：

1. 网关持续读取串口字节流并按 AA 帧头和长度重组完整响应。
2. 校验正确的响应帧保持原内容不变。
3. 每帧单独封装为一个 UDP 数据报，返回 `192.168.1.102:20003`。

该通路不增加 EB90 或 JSON 外层封装，便于现有转台协议程序直接复用，同时由网关承担串口分片、错位重同步和异常帧隔离。

### 5.4 协议兼容与安全限制

网关按实机响应兼容状态帧 `15/16` 字节以及设置帧 `10/14` 字节两种长度，并记录命令包、响应包、拒绝包、解析丢弃字节和 checksum 错误计数。

当前明确禁用运动子命令 `0x06` 的步进功能。实机测试中，资料所述的“双轴步进 +1°、0°”帧表现为朝零位大幅运动，而非小角度相对步进。为防止误动作，网关会直接拒绝该子命令；只有在厂家确认协议语义并完成受控复测后才能解除限制。停止、定点、周扫、空间扫描、查询、设置和维护类命令仍按白名单及长度规则处理。

### 5.5 与既有通路的隔离

转台接入没有修改摄像头二层转发程序或惯导协议：

- 不占用摄像头侧 `eth0`，只通过已有 `eth1` 地址收发 UDP；
- 使用 UDP `20003`，与惯导 UDP `20002` 分离；
- 使用上方USB口的独立FTDI USB-RS422设备，与下方USB惯导通过稳定路径、协议探测和排他锁分离；
- 使用独立 systemd 服务、日志和自动重启策略；
- 只接受 `192.168.1.102` 发来的控制数据报，减少误控制风险。

## 6. 上位机部署内容

上位机是 Windows 电脑，固定有线地址：

```text
192.168.1.102/24
```

工作目录：

```text
E:\RK3568
```

### 6.1 摄像头侧

上位机不需要安装专用透明转发程序，因为二层转发全部在 RK3568 上完成。

上位机使用普通网络应用访问摄像头：

```text
浏览器：http://192.168.1.100/
主码流：rtsp://用户名:密码@192.168.1.100:554/Streaming/Channels/101
子码流：rtsp://用户名:密码@192.168.1.100:554/Streaming/Channels/102
```

可以使用：

- 浏览器访问摄像头管理页面；
- VLC播放 RTSP；
- ffplay/ffprobe/ffmpeg检查视频流；
- 海康客户端进行设备管理和预览。

摄像头账号密码由现场凭据管理，不写入脚本。

### 6.2 惯导接收程序

文件：

```text
E:\RK3568\outputs\imu_udp_receiver.py
E:\RK3568\outputs\wtgahrs3_reader.py
```

`imu_udp_receiver.py` 复用 `wtgahrs3_reader.py` 中的协议解析函数，因此两个文件应保存在同一目录。

运行：

```powershell
cd E:\RK3568
python outputs\imu_udp_receiver.py --bind 192.168.1.102 --port 20002
```

接收程序执行以下校验：

1. 检查 `EB 90` 帧头。
2. 检查声明总长度是否等于实际UDP载荷长度。
3. 检查 type 是否为支持的值。
4. 检查累加checksum。
5. 解析 UTF-8 JSON。
6. 检查 schema 是否为 `wtgahrs3.v1`。
7. 根据 sequence 统计丢包。
8. 退出时显示接收数量、丢包数量、耗时和平均速率。

固定数量验收示例：

```powershell
python outputs\imu_udp_receiver.py \
  --bind 192.168.1.102 \
  --port 20002 \
  --count 50 \
  --timeout 8
```

### 6.3 惯导控制程序

文件：

```text
E:\RK3568\outputs\imu_udp_command.py
```

使用示例：

```powershell
python outputs\imu_udp_command.py status
python outputs\imu_udp_command.py stop
python outputs\imu_udp_command.py start
python outputs\imu_udp_command.py set-angle --x 10 --y -5 --z 90
```

程序把命令封装为相同的 EB90外层协议，发送到：

```text
192.168.1.101:20002
```

收到板端 `0x81` 后解析并显示状态JSON。

### 6.4 转台图形控制程序

文件：

```text
E:\RK3568\转台控制程序\turntable_control.py
E:\RK3568\转台控制程序\启动转台控制器.bat
E:\RK3568\转台控制程序\README.md
```

正常使用时双击批处理文件，或执行：

```powershell
cd E:\RK3568\转台控制程序
python turntable_control.py
```

程序默认绑定 `192.168.1.102:20003`，连接 `192.168.1.101:20003`，启动后自动连接并查询状态。界面提供实时角度、速度、故障状态、原始十六进制日志，以及停止、定点、周扫、空间扫描、状态查询、故障查询、驱动器查询和维护命令。

上位机程序对方位 `0～360°`、俯仰 `0～90°`和速度范围进行输入检查；运动期间 1 秒收不到有效状态会自动停止；收到故障状态也会自动停止。设置零点和设备复位要求二次确认。界面虽然保留步进命令构造能力，但当前板端安全策略会拒绝步进子命令，不能将其作为可用功能。

需要修改地址时可使用：

```powershell
python turntable_control.py --gateway 192.168.1.101 --gateway-port 20003 --local 192.168.1.102 --local-port 20003
```

### 6.5 转台命令行诊断程序

文件：

```text
E:\RK3568\outputs\turntable_udp_client.py
```

该程序用于部署检查和无界面诊断，可执行状态、故障、驱动器和设置查询，也可发送停止等受支持命令。例如：

```powershell
python outputs\turntable_udp_client.py status
python outputs\turntable_udp_client.py fault
python outputs\turntable_udp_client.py driver
python outputs\turntable_udp_client.py stop
```

命令行程序发送的所有命令仍须通过 RK3568 网关验证；原始帧模式也不能绕过来源、长度、checksum、命令白名单和步进禁用规则。

### 6.6 自动化测试

文件：

```text
E:\RK3568\tests\test_turntable_gateway.py
E:\RK3568\转台控制程序\test_turntable_control.py
```

运行：

```powershell
cd E:\RK3568
python -m unittest discover -s tests -v

cd E:\RK3568\转台控制程序
python -m unittest -v test_turntable_control.py
```

转台测试覆盖：

- 网关命令白名单、长度和 checksum 验证；
- 步进子命令安全拒绝；
- 串口流错位重同步和分片重组；
- 状态帧与设置帧的实机长度兼容；
- 上位机协议帧构造、范围检查和状态解析；
- UDP 本机往返和异常数据拒绝。

2026-10-19 本机复测结果：网关7项、图形上位机11项，全部通过。

## 7. RK3568与上位机部署清单对照

| 部署位置 | 文件/软件 | 用途 |
|---|---|---|
| RK3568 | `/home/teamhd/camera-relay/ether_relay` | 摄像头二层透明转发可执行程序 |
| RK3568 | `/etc/systemd/system/ether-relay.service` | 摄像头转发启动、网卡准备和故障重启 |
| RK3568 | `/home/teamhd/imu-reader/wtgahrs3_reader.py` | TTL惯导采集、解析、UDP发送和命令接收 |
| RK3568 | `/etc/systemd/system/wtgahrs3-reader.service` | 惯导程序开机启动和故障重启 |
| RK3568 | `/home/teamhd/turntable/turntable_gateway.py` | 转台原生帧校验、RS422-UDP双向转发和安全限制 |
| RK3568 | `/etc/systemd/system/turntable-gateway.service` | 转台网关开机启动和故障重启 |
| 上位机 | `outputs/imu_udp_receiver.py` | 接收、校验、显示惯导UDP并统计丢包 |
| 上位机 | `outputs/imu_udp_command.py` | 查询状态和发送开始/停止等控制命令 |
| 上位机 | `outputs/wtgahrs3_reader.py` | 为接收器和命令程序提供共用协议函数 |
| 上位机 | `转台控制程序/turntable_control.py` | 转台图形控制、状态显示、超时和故障保护 |
| 上位机 | `转台控制程序/启动转台控制器.bat` | Windows 一键启动转台控制界面 |
| 上位机 | `outputs/turntable_udp_client.py` | 转台命令行部署检查和诊断 |
| 上位机 | 浏览器/VLC/ffmpeg等 | 访问摄像头网页或RTSP视频流 |
| 开发/验收 | `tests/test_turntable_gateway.py` | 转台网关协议、流解析和安全拒绝测试 |
| 开发/验收 | `转台控制程序/test_turntable_control.py` | 转台上位机协议与UDP收发测试 |

## 8. 启动后的完整时序

RK3568开机后的软件时序如下：

1. systemd启动 `ether-relay.service`。
2. 服务准备 `eth0`、`eth1`，配置 `192.168.1.101/24` 并关闭offload。
3. `ether_relay` 开始双向转发摄像头以太网帧。
4. systemd启动 `wtgahrs3-reader.service`。
5. Python程序打开下方CP2102稳定`by-id`串口并监听`192.168.1.101:20002`；更换转换器时可用`--port auto`通过有效WIT帧重新识别。
6. 每收到一组有效 `0x51/0x52/0x53`，生成一条约10Hz的惯导样本。
7. 样本封装为 `0x82` 数据报并单播至 `192.168.1.102:20002`。
8. systemd启动 `turntable-gateway.service`，网关打开 FTDI USB-RS422 串口并监听 `192.168.1.101:20003`。
9. 上位机可以同时访问摄像头、运行惯导接收器和启动转台控制界面。
10. 上位机向 UDP `20003` 发送转台原生命令，网关验证后写入 RS422；转台响应经同一网关返回上位机。
11. 上位机发送惯导控制命令时，板端在 UDP `20002` 套接字处理并返回 `0x81`。

## 9. 软件修改时需要同步调整的位置

如果电脑IP发生变化，需要修改：

```text
outputs/wtgahrs3-reader.service
--udp-host 新电脑IP

outputs/turntable-gateway.service
--udp-host 新电脑IP
--allowed-host 新电脑IP

转台控制程序/turntable_control.py
DEFAULT_LOCAL_HOST = 新电脑IP
```

部署后执行：

```bash
sudo cp wtgahrs3-reader.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart wtgahrs3-reader.service

sudo cp turntable-gateway.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart turntable-gateway.service
```

如果惯导波特率发生变化，需要同时修改服务的 `--baud`，并确认设备真实配置与服务配置一致。

如果更换 USB-RS422 转换器，需要先在 RK3568 上通过 `ls -l /dev/serial/by-id/` 确认新设备的稳定名称，再同步修改 `turntable-gateway.service` 的 `--port`。不要直接长期绑定 `/dev/ttyUSB0`。

如果修改转台 UDP 端口，需要同步修改：

- `turntable-gateway.service` 的 `--listen-port` 和 `--udp-port`；
- `turntable_control.py` 的默认网关端口和本地端口，或启动时传入对应参数；
- 防火墙、验收命令和相关文档。

如果摄像头IP发生变化，透明转发程序本身不需要修改，但上位机访问地址、验收命令和文档需要更新。

如果更换网口名称，需要同步修改：

- `ether-relay.service` 中的网卡准备命令；
- `ether_relay` 的两个启动参数；
- `wtgahrs3-reader.service` 的 `--udp-source` 所在接口配置。
- `turntable-gateway.service` 的 `--bind` 所在接口配置。

## 10. 软件验收标准

摄像头软件链路合格条件：

- `ether-relay.service` 为 `active/enabled`；
- `eth0`、`eth1` 为 `UP,LOWER_UP`；
- 上位机可Ping摄像头；
- HTTP、RTSP和视频播放正常；
- 两个方向转发计数持续增长；
- `ERR=0`；
- 网卡 errors/dropped 不持续增长。

惯导软件链路合格条件：

- `wtgahrs3-reader.service` 为 `active/enabled`；
- 服务参数指向下方CP2102稳定路径，且样本序号持续增长；
- 串口帧checksum正确；
- 上位机收到 `0x82`；
- JSON schema为 `wtgahrs3.v1`；
- sequence连续；
- 接收频率约10Hz；
- 连续测试丢包为0；
- 摄像头流量与惯导UDP同时运行时均正常。

2026-10-20本次USB改造实测结果：板端进程实际打开`/dev/ttyUSB1`（CP2102稳定
`by-id`路径），服务保持`active/enabled`且部署后无新错误；上位机连续接收50包，
丢包0，耗时4.992秒，频率10.02Hz。同期转台和摄像头转发服务均为`active`。

转台软件链路合格条件：

- `turntable-gateway.service` 为 `active/enabled`；
- 服务监听 `192.168.1.101:20003`；
- USB-RS422 稳定设备路径存在，串口参数为 `115200 8N1`；
- 上位机图形程序能够自动连接、查询并持续显示状态；
- 状态、故障、驱动器和设置查询均能收到校验正确的响应；
- 停止命令能够收到成功响应；
- 网关拒绝非 `192.168.1.102` 来源、错误长度、错误checksum、未知命令和步进子命令；
- 串口解析错误计数在链路稳定后不持续增长；
- 转台通路运行期间，摄像头转发和惯导 UDP 的端口、进程及数据格式不被改变。

## 11. 软件实现结论

系统的软件划分是：RK3568负责实时数据入口、摄像头二层转发、惯导串口解析以及转台 RS422-UDP 网关；上位机负责摄像头业务访问、惯导数据显示与控制，以及转台图形控制和状态监视。

摄像头数据保持原始以太网协议，不经过应用层转换；惯导数据由板端从WIT USB串口协议转换为项目EB90/JSON UDP协议；转台数据保留设备原生AA帧，只在UDP与RS422之间转发并执行安全校验。三条通路通过独立服务、串口和端口隔离：网口1接摄像头、网口2接上位机、上方USB接转台、下方USB接惯导。
