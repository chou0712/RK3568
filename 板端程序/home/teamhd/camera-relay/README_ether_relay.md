# CPG-3568 双网口用户态透明转发测试

该程序在内核未启用 `CONFIG_BRIDGE` 时，使用 Linux `AF_PACKET` 原始套接字，在两个网口之间原样转发完整以太网帧。

它转发 ARP、IPv4、IPv6、广播和组播帧，不解析或重新封装摄像头视频，也不使用 UDP 20000/20001。

## 1. 把源文件复制到开发板

将 `ether_relay.c` 放到开发板的一个工作目录，例如：

```bash
mkdir -p ~/camera-relay
cd ~/camera-relay
```

## 2. 编译

```bash
gcc -O2 -Wall -Wextra -o ether_relay ether_relay.c
```

## 3. 准备网口

请在开发板本地显示器和键盘上操作，继续使用 Wi-Fi 管理开发板。以下配置在重启后不会保留。

```bash
sudo nmcli device set eth0 managed no
sudo nmcli device set eth1 managed no
sudo ip addr flush dev eth0
sudo ip addr flush dev eth1
sudo ip link set eth0 up
sudo ip link set eth1 up
```

## 4. 运行

```bash
sudo ./ether_relay eth0 eth1
```

程序每 5 秒显示两个方向的帧数、字节数和错误数。按 `Ctrl+C` 停止。

## 5. 第一阶段接线和测试

```text
摄像头 192.168.1.100 -> eth0
测试电脑 192.168.1.102 -> eth1
```

测试电脑有线网卡设置为：

```text
IP: 192.168.1.102
掩码: 255.255.255.0
网关: 留空
DNS: 留空
```

Windows PowerShell 测试：

```powershell
ping 192.168.1.100 -n 100
```

随后使用摄像头厂商软件、浏览器或播放器打开摄像头视频。

## 6. 第二阶段加入光纤模块

```text
摄像头 -> eth0 -> 用户态转发 -> eth1 -> 光电模块A
       -> 光纤 -> 光电模块B -> 测试电脑
```

再次进行 100 次 Ping，并连续播放视频至少 30 分钟。

## 7. 恢复网口管理

停止程序后执行：

```bash
sudo nmcli device set eth0 managed yes
sudo nmcli device set eth1 managed yes
```

或者重启开发板后再执行上述两条命令。

## 8. 当前版本的定位

这是功能验证版本，不是完整的生产网桥。高码率或长期运行还需要测试丢包、CPU占用、链路重连和吞吐量；如果性能不足，应升级为 `PACKET_MMAP/TPACKET_V3`，或使用启用了 `CONFIG_BRIDGE` 的厂家内核。
