# RK3568 摄像头、UART0惯导与USB转台系统交接说明

> 最后更新：2026-10-22（Asia/Shanghai）
> 当前配置：网口1接摄像头，网口2接上位机；上方USB接转台，WTGAHRS3-TTL直接接RK3568 UART0（`/dev/ttyS0`）。2026-10-22已从UART0连续解析有效惯导样本，服务已切换到该接口。

## 1. 系统目标与当前状态

本系统以 CPG-3568（RK3568）为核心，同时完成三项功能：

1. 摄像头以太网数据经 RK3568 双网口透明转发到电脑。
2. WTGAHRS3-TTL 惯导直接接入RK3568 UART0，数据经解析并封装成UDP发送到电脑。
3. 伺服转台通过四线全双工 USB-RS422 接入 RK3568，以独立 UDP 端口向上位机透明转发转台原生协议帧。

当前直连拓扑为网口1接摄像头、网口2接电脑，惯导使用UART0。此前USB惯导链路的验收记录保留作历史参考。

## 2. 最终拓扑

```text
摄像头 192.168.1.100
        |
        | 以太网
        v
RK3568 eth0
        |
        | 用户态二层透明转发 ether-relay.service
        v
RK3568 eth1 / 网口2（管理地址 192.168.1.101/24）
        |
        | 网线
        v
电脑 192.168.1.102/24
```

```text
WTGAHRS3-TTL TTL串口
        |
        | TTL UART，9600 8N1，TX/RX交叉并共地
        v
RK3568 UART0（/dev/ttyS0）
        |
        | WIT 0x55 标准协议解析
        v
UDP EB90 协议封装
        |
        | 192.168.1.101 -> 192.168.1.102:20002
        v
RK3568网口2 -> 电脑
```

如恢复此前的TP-Link光纤收发器，它只负责二层介质转换，不解析摄像头协议或惯导UDP协议，也不需要在RK3568上安装专用驱动。

## 3. 已确认硬件与网络信息

### 3.1 RK3568

- 开发板：CPG-3568。
- SoC：Rockchip RK3568。
- 系统：Ubuntu 20.04.6 LTS。
- 内核：Linux 5.10.226，AArch64。
- 登录用户：`teamhd`。
- 有线管理地址：`192.168.1.101/24`，配置在 `eth1`。
- 双网口：`eth0`、`eth1`。
- 内核没有启用 `CONFIG_BRIDGE`，因此不能创建标准 Linux `br0`。
- 当前摄像头转发使用 AF_PACKET 用户态二层透明转发程序。

### 3.2 电脑

- 有线地址：`192.168.1.102/24`。
- 惯导 UDP 接收端口：`20002`。
- 网关和 DNS 对当前点对点测试链路不是必需项。

### 3.3 摄像头

- IP：`192.168.1.100/24`。
- MAC：`8c-22-d2-86-cc-25`。
- 型号：海康威视 `HK-H3S4M-P`。
- HTTP：TCP 80。
- RTSP：TCP 554。
- 设备服务端口：TCP 8000。
- 主码流：H.265，2560×1440，25 fps。
- 子码流：H.265，768×432，25 fps。
- 主码流通常使用 `/Streaming/Channels/101`，子码流通常使用 `/Streaming/Channels/102`。
- 摄像头账号和密码不写入本交接文档，应从项目凭据保管位置获取。

### 3.4 惯导

- 最终型号：`WTGAHRS3-TTL`。
- 接口：TTL UART，直接接入RK3568 UART0；不能接到RS232或RS485接口。
- 串口参数：`9600 8N1`。
- 输出协议：WIT 标准串口协议，每帧 11 字节、帧头 `0x55`、末字节为累加校验。
- 实测周期：约 10 Hz。
- UART0 Linux设备节点为`/dev/ttyS0`，`serial-getty@ttyS0.service`保持未启用。
- 上方USB口实测为FTDI `0403:6001`，转台使用`usb-FTDI_FT232R_USB_UART_A9UOWY7G-if00-port0`；两个服务不依赖可变的`ttyUSB`编号。

## 4. 惯导当前UART0接线

WTGAHRS3-TTL直接连接RK3568 UART0：

| WTGAHRS3-TTL | RK3568 UART0 | 说明 |
|---|---:|---|
| VCC | 5V/VCC | 按惯导规格供电 |
| GND | GND | 必须共地 |
| TX | RXD | 惯导发送接RK3568接收 |
| RX | TXD | 惯导接收接RK3568发送 |

必须交叉连接TX/RX，不能接成TX对TX、RX对RX；信号侧必须是TTL电平。

历史UART0接法曾完成回环和双向测试，读取版本命令`FF AA 27 2E 00`曾返回：

```text
55 5F CF 3D CF 3D 00 00 00 00 CC
```

2026-10-22当前UART0链路已重新验证，可以连续接收并解析WIT `0x51/0x52/0x53`帧。

## 5. 摄像头透明转发

### 5.1 工作方式

`ether-relay.service` 启动 `/home/teamhd/camera-relay/ether_relay eth0 eth1`，使用两个 `AF_PACKET/SOCK_RAW` 套接字透明转发完整以太网帧：

- 不修改 Ethernet、ARP、IPv4、IPv6、TCP 或 UDP 内容。
- 对摄像头和电脑方向对称转发。
- 使用 `PACKET_IGNORE_OUTGOING` 等机制避免自身转发形成循环。
- 每5秒输出双向帧数、字节数和 `ERR` 计数。
- 关闭 GRO、LRO、GSO、TSO，避免聚合帧重新发送时出现 `Message too long`。

当前内核不支持 Linux bridge，不要再次尝试创建 `br0`，除非以后更换了启用 `CONFIG_BRIDGE` 的内核。

### 5.2 板端部署位置

```text
/home/teamhd/camera-relay/ether_relay
/etc/systemd/system/ether-relay.service
```

服务状态应为：

```text
active
enabled
```

### 5.3 已验收项目

- 摄像头通过 RK3568 和实际 TP-Link 光纤链路到电脑。
- 电脑能够 Ping `192.168.1.100`。
- 1400字节 Ping 测试通过，0%丢包。
- HTTP 返回 `200 OK`。
- TCP 554 和 TCP 8000 可达。
- 摄像头主码流已完成连续接收测试。
- 双向转发计数持续增长。
- 服务统计 `ERR=0`。
- `eth0`、`eth1` 没有持续增加的 errors、dropped、carrier 或 collisions。

电脑访问示例：

```text
网页：http://192.168.1.100/
主码流：rtsp://用户名:密码@192.168.1.100:554/Streaming/Channels/101
子码流：rtsp://用户名:密码@192.168.1.100:554/Streaming/Channels/102
```

## 6. 惯导采集与UDP传输

### 6.1 板端程序

工作区文件：

```text
outputs/wtgahrs3_reader.py
outputs/wtgahrs3-reader.service
outputs/imu_udp_receiver.py
outputs/imu_udp_command.py
tests/test_wtgahrs3_reader.py
```

板端部署位置：

```text
/home/teamhd/imu-reader/wtgahrs3_reader.py
/etc/systemd/system/wtgahrs3-reader.service
```

服务实际运行参数：

```text
--transport ttl
--port /dev/ttyS0
--baud 9600
--udp-host 192.168.1.102
--udp-port 20002
--udp-source 192.168.1.101
--udp-listen-port 20002
--udp-type 0x82
```

服务状态应为：

```text
active
enabled
```

### 6.2 串口解析

程序被动接收 WIT `0x55` 标准协议，并组合以下数据帧：

- `0x51`：三轴加速度和温度。
- `0x52`：三轴角速度。
- `0x53`：三轴姿态角。
- 设备还实测输出 `0x50`、`0x55`、`0x57`、`0x58`，当前周期 UDP 样本主要使用 `0x51/0x52/0x53`。

程序校验每个11字节帧的累加校验和，只有取得有效加速度、角速度和姿态角后才产生一条完整样本。

### 6.3 UDP外层协议

每条惯导样本封装为一个 UDP 数据报：

```text
EB 90 + 整帧总长度（2字节大端）+ type + 数据区 + checksum
```

- 周期惯导数据 type：`0x82`。
- 数据区：UTF-8 JSON。
- JSON schema：`wtgahrs3.v1`。
- JSON包含连续序号、UTC时间戳、三轴加速度、三轴角速度、三轴姿态角和温度。
- checksum：除 checksum 自身外，整帧所有字节累加后取低8位。
- 当前使用单播目标：`192.168.1.102:20002`。

已登记的控制 type：

| type | 含义 |
|---|---|
| `0x01` | 查询状态 |
| `0x02` | 设置软件目标角度 |
| `0x03` | 开始周期发送 |
| `0x04` | 停止周期发送 |
| `0x81` | 状态应答 |
| `0x82` | 惯导周期数据 |

### 6.4 已验收项目

- 原始串口5秒采集到约3880字节。
- 同一轮测试得到351～352个有效 WIT 子帧，校验错误为0。
- 板端稳定解析加速度、角速度、姿态角和温度。
- 电脑通过实际光纤链路持续收到惯导 UDP 数据。
- 多轮测试结果包括：连续20包、30包、50包，丢包均为0。
- 实测频率约10包/秒。
- 序号连续，外层帧头、长度、type和checksum均正确。
- 摄像头流量和惯导 UDP 同时运行测试通过。

电脑接收命令：

```powershell
cd E:\RK3568
python outputs\imu_udp_receiver.py --bind 192.168.1.102 --port 20002
```

只测试固定数量：

```powershell
python outputs\imu_udp_receiver.py --bind 192.168.1.102 --port 20002 --count 50 --timeout 8
```

## 7. 服务管理与检查命令

### 7.1 查看状态

```bash
systemctl is-active ether-relay.service
systemctl is-active wtgahrs3-reader.service
systemctl is-enabled ether-relay.service
systemctl is-enabled wtgahrs3-reader.service
```

正常应全部返回 `active` 或 `enabled`。

### 7.2 查看日志

```bash
sudo journalctl -u ether-relay.service -n 50 --no-pager
sudo journalctl -u wtgahrs3-reader.service -n 50 --no-pager
```

惯导服务配置了 `StandardOutput=null`，正常样本不会以10Hz持续写入 journal；错误仍写入 journal。

### 7.3 查看接口和USB串口

```bash
ip -br addr
ip -br link show eth0
ip -br link show eth1
ip -s link show eth0
ip -s link show eth1
ls -l /dev/ttyUSB* /dev/ttyACM* 2>/dev/null
ls -l /dev/serial/by-id/ /dev/serial/by-path/ 2>/dev/null
```

确认惯导服务参数指向`/dev/ttyS0`，UART0必须能输出有效WIT帧，并确认串口登录服务未占用该节点。

### 7.4 重启服务

```bash
sudo systemctl restart ether-relay.service
sudo systemctl restart wtgahrs3-reader.service
```

## 8. 故障排查顺序

### 8.1 电脑收不到惯导 UDP

1. 检查电脑有线地址是否仍是 `192.168.1.102/24`。
2. Ping RK3568：`ping 192.168.1.101`。
3. 查看 `wtgahrs3-reader.service` 是否为 `active`。
4. 查看`journalctl`中自动选择的USB串口，确认没有误选上方转台FTDI。
5. 确认TTL转USB模块出现在`/dev/ttyUSB*`或`/dev/ttyACM*`，必要时查看`dmesg`。
6. 确认惯导为 `9600 8N1`。
7. 确认电脑防火墙允许 UDP 20002。
8. 检查网口2与电脑之间的链路灯；若现场使用光纤收发器，再检查两端光纤链路。

如果USB串口存在但没有有效WIT帧，先检查供电、共地、线序和电平：

```text
VCC -> 5V
GND -> GND
TX  -> TTL转USB RXD
RX  -> TTL转USB TXD
```

### 8.2 验证TTL转USB模块

停止惯导服务，断开惯导信号线，在TTL转USB模块的TXD与RXD之间做回环测试。测试后拆除短接并恢复惯导接线。不要在转台的RS422转换器上执行TTL回环。

### 8.3 验证惯导双向通信

停止惯导服务后，先从`/dev/serial/by-id`或`by-path`确认下方TTL转USB对应节点，再以9600 8N1向该节点发送：

```text
FF AA 27 2E 00
```

这是读取版本寄存器的命令，不修改配置。正常设备会返回以 `55 5F` 开头的11字节帧。测试完成后重新启动服务。

### 8.4 电脑访问不了摄像头

1. 检查电脑是否能 Ping `192.168.1.101`。
2. 检查 `eth0`、`eth1` 是否均为 `UP,LOWER_UP`。
3. 检查 `ether-relay.service` 是否为 `active`。
4. 查看转发日志中两个方向的计数是否增长，`ERR` 是否仍为0。
5. 检查摄像头、两端光纤收发器、光纤和网线的链路灯。
6. 检查电脑是否仍配置 `192.168.1.102/24`。
7. 不要创建 `br0`，也不要让两个物理口同时由 NetworkManager配置同一网段地址。

## 9. 重要注意事项

- 当前最终惯导是 `WTGAHRS3-TTL`，不要再按旧的 WTGAHRS3-485 Modbus方式轮询。
- 当前惯导直接接入UART0，服务固定使用`/dev/ttyS0`；不要改成转台所用的`ttyUSB`设备。
- 惯导供电使用5V；UART信号连接必须交叉并共地。
- 不要把5V直接接到 UART RX/TX 信号脚。
- 不要覆盖或修改当前带签名的 FIT 启动镜像。
- 不要在当前厂家内核上反复尝试创建 `br0`；内核没有启用 bridge功能。
- 网口转光纤设备是透明介质转换设备，不需要修改摄像头地址或UDP帧格式。
- 两个透明转发网口不能同时接入同一个已有交换网络形成物理环路；当前用户态程序不实现STP。
- 已知电脑地址变化时，需要同步修改 `wtgahrs3-reader.service` 中的 `--udp-host`，然后执行 `daemon-reload` 和服务重启。
- 摄像头和系统密码不应写入项目文档或提交到代码库。

## 10. 当前验收结论

截至2026-09-22，旧UART0/光纤接法曾完成以下验收：

- RK3568双网口摄像头二层透明转发。
- 摄像头经TP-Link网口转光纤设备到电脑的实际光纤链路。
- 摄像头Ping、HTTP、RTSP端口和视频数据验证。
- WTGAHRS3-TTL经当时的UART0链路采集。
- WIT标准协议帧校验和姿态数据解析。
- 惯导数据封装为项目EB90/`0x82` UDP协议。
- 惯导UDP经实际光纤链路发送到电脑。
- 摄像头与惯导同时运行。
- 两个服务开机自启。

2026-10-20 USB接法历史验证记录：

- 下方USB口识别为CP2102 `10c4:ea60`，稳定路径当前映射到`/dev/ttyUSB1`；
- 上方转台识别为FTDI `0403:6001`，稳定路径当前映射到`/dev/ttyUSB0`；
- `wtgahrs3-reader.service`已启用并保持`active`，进程实际打开`/dev/ttyUSB1`；
- 部署后无新warning/error，服务重启计数没有继续增加；
- 上位机连续接收50包，`sequence`连续、丢包0、耗时4.992秒、频率10.02包/秒；
- `wtgahrs3-reader.service`、`turntable-gateway.service`和`ether-relay.service`同时为`active`。

上述内容仅保留作历史记录。2026-10-22当前部署已恢复UART0：前台从`/dev/ttyS0`连续读取5条校验正确的惯导样本；服务切换后上位机连续收到20包，`sequence`连续、丢包0，耗时1.906秒、频率10.49包/秒。后续若更改电脑IP、惯导波特率或UDP目标地址，应同步更新本文档和对应systemd服务配置。

## 11. 转台 RS422 接入

### 11.1 最终通路和资源隔离

```text
伺服转台 RS422，115200 8N1
        |
        | 四线全双工 RS422
        v
FTDI USB-RS422 转换器
        |
        | /dev/serial/by-id/usb-FTDI_FT232R_USB_UART_A9UOWY7G-if00-port0
        v
RK3568 turntable-gateway.service
        |
        | 原生 AA 帧，每帧一个 UDP 数据报
        | 192.168.1.101:20003 <-> 192.168.1.102:20003
        v
远端上位机
```

转台通路与原系统隔离：

- 转台使用FTDI稳定`by-id`路径，惯导使用独立的`/dev/ttyS0`，两个程序同时使用排他锁隔离。
- 转台使用 UDP `20003`，不占用惯导的 UDP `20002`。
- 未修改 `ether-relay.service`、`wtgahrs3-reader.service`、两块网卡配置或现有惯导程序。
- 网关只接受来自 `192.168.1.102`、设备 ID 为 `01`、命令长度合法且累加校验正确的控制帧。
- 串口回传按帧头、命令、候选长度和校验和重组；兼容状态帧长度字段为 `0x10`、实收 15 或 16 字节的协议差异。

### 11.2 部署位置

板端文件：

```text
/home/teamhd/turntable/turntable_gateway.py
/etc/systemd/system/turntable-gateway.service
```

工作区文件：

```text
outputs/turntable_gateway.py
outputs/turntable_udp_client.py
outputs/turntable-gateway.service
outputs/README_turntable.md
tests/test_turntable_gateway.py
```

服务已设置为开机自启，检查命令：

```bash
systemctl is-active turntable-gateway.service
systemctl is-enabled turntable-gateway.service
sudo journalctl -u turntable-gateway.service -n 50 --no-pager
ss -lun | grep 20003
```

正常结果应为 `active`、`enabled`，并监听 `192.168.1.101:20003`。

### 11.3 上位机测试

状态查询为只读命令，应作为首次测试：

```powershell
cd E:\RK3568
python outputs\turntable_udp_client.py status
```

持续接收主动上报：

```powershell
python outputs\turntable_udp_client.py --count 0 listen
```

停止命令：

```powershell
python outputs\turntable_udp_client.py stop
```

运动命令必须在确认机械范围、负载固定和现场人员安全后执行：

```powershell
python outputs\turntable_udp_client.py point 10 5
```

不要使用双轴步进子命令 `0x06`。2026-10-16 实机测试表明，文档所述小角度步进帧会被当前设备表现为朝零位大幅运动；网关已主动禁用该子命令，等待厂家确认协议语义。

### 11.4 2026-10-16 部署验证

- RK3568 已识别 FTDI 转换器 `0403:6001`，创建 `/dev/ttyUSB0` 和稳定 `by-id` 链接。
- `turntable-gateway.service` 已部署、启动并设为开机自启。
- 上位机发送状态查询 `AA 06 01 02 01 B4` 后，板端日志确认该帧由 UDP `20003` 正确写入 RS422 串口。
- 转台上电后已收到状态查询应答和约 `100 ms` 周期状态上报；帧头、15 字节兼容解析和累加校验均正确，故障位为 `0`。
- 控制测试发送了文档所述双轴步进 `+1.0°/0°` 帧，但设备实际从约 `99.7°/89.9°` 朝零位大幅运动，而不是相对移动 `1°`。随即发送停止命令并收到成功应答，最终稳定在约 `69.7°/60.3°`，两轴速度为 `0.0°/s`，故障位为 `0`。
- 上述结果证明上位机到 RK3568、RS422 控制、转台应答和状态回传全链路可用，同时证明当前协议文档的步进语义不能直接用于正式控制。网关现已拒绝步进子命令；在厂家确认前仅使用状态查询、停止及经过现场审查的定点命令。
- 摄像头本次未上电，`eth0` 无链路符合现场条件；`ether-relay.service` 保持运行，`eth0`、`eth1` 的 errors、dropped、carrier 和 collisions 均为 0。
- 该段为2026-10-16历史记录：当时惯导走`/dev/ttyS0`但未收到有效帧。2026-10-22重新接线后UART0已收到有效数据；转台继续使用独立FTDI串口和UDP `20003`。

### 11.5 接线复核

| USB-RS422 | 转台控制口 |
|---|---|
| `TX+` | 8脚 `RS422B_RX+` |
| `TX-` | 9脚 `RS422B_RX-` |
| `RX+` | 6脚 `RS422B_TX+` |
| `RX-` | 7脚 `RS422B_TX-` |
| `GND` | 10脚 `RS422B_GND` |

不要带电交换差分线。若能收到主动状态但查询命令无应答，接收方向正常，应断电后检查发送差分对 `TX+`/`TX-`；若收到固定周期乱码或大量 `FF`，优先断电后交换接收差分对 `RX+`/`RX-`。

### 11.6 Windows 图形化上位机

工作区中的图形化程序已改为 RK3568 网络版：

```text
转台控制程序/turntable_control.py
转台控制程序/启动转台控制器.bat
转台控制程序/README.md
转台控制程序/test_turntable_control.py
```

默认通信参数：

```text
RK3568：192.168.1.101:20003
上位机：192.168.1.102:20003
```

双击 `启动转台控制器.bat` 后，程序自动绑定上位机有线地址、连接 RK3568 网关并查询转台状态。界面发送和接收的仍是转台原生 `AA` 二进制帧，每个 UDP 数据报承载一个完整帧。程序支持状态、故障、驱动器和设置查询，以及停止、定点、步进、周扫和空间扫描等控制。

2026-10-17 实机验证：

- 图形程序的 UDP 传输层成功连接 RK3568，并取得转台状态：方位 `110.3°`、俯仰 `89.9°`、故障 `0x00`。
- 关闭程序时发送的停止帧已由 RK3568 日志确认写入 RS422，转台返回成功应答。
- RK3568 网关保持 `active`、`enabled`，监听 `192.168.1.101:20003`。
- 11 项自动测试全部通过，包括协议帧、状态解析、UDP 长度异常兼容和本机 UDP 往返测试。
- 首次有效状态会自动写入定点目标框，避免界面默认角度导致大幅误运动；俯仰步进还会按当前角度检查 `0°～90°`机械范围。
