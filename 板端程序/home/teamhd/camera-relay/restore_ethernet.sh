#!/bin/sh
set -eu

systemctl stop ether-relay.service 2>/dev/null || true
nmcli device set eth0 managed yes
nmcli device set eth1 managed yes
ip link set eth0 up
ip link set eth1 up

echo "NetworkManager control restored for eth0 and eth1."
