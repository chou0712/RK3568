import socket
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import NetworkConfig
from protocol import build_point_command
from udp_client import RK3568UDPClient


SEND_PORT = 21200
RECV_PORT = 21201
STATUS_FRAME = bytes.fromhex(
    "AA 10 01 07 00 00 64 00 31 00 36 00 00 00 8D"
)


class UDPResponder:
    def __init__(self, recv_port: int, send_host: str, send_port: int):
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.bind(("0.0.0.0", recv_port))
        self._socket.settimeout(0.05)
        self._target = (send_host, send_port)
        self._running = False
        self._thread: threading.Thread | None = None
        self.last_received = b""

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        self._socket.close()

    def _loop(self) -> None:
        while self._running:
            try:
                data, _addr = self._socket.recvfrom(2048)
                self.last_received = data
                self._socket.sendto(STATUS_FRAME, self._target)
            except socket.timeout:
                continue
            except OSError:
                break


class UDPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.responder = UDPResponder(SEND_PORT, "127.0.0.1", RECV_PORT)
        cls.responder.start()
        cls.client = RK3568UDPClient(
            NetworkConfig(
                rk3568_ip="127.0.0.1",
                send_port=SEND_PORT,
                recv_port=RECV_PORT,
                timeout=0.5,
            )
        )

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.responder.stop()

    def test_status_query_and_receive_status(self):
        self.client.send_query()
        status = self.client.recv_status()
        self.assertAlmostEqual(status.az_deg, 10.0)
        self.assertAlmostEqual(status.el_deg, 4.9)
        self.assertAlmostEqual(status.az_speed_dps, 5.4)
        self.assertEqual(status.fault, 0)

    def test_point_command_is_sent(self):
        self.client.send_target(10.0, 5.0)
        time.sleep(0.1)
        self.assertEqual(
            self.responder.last_received, build_point_command(10.0, 5.0)
        )


if __name__ == "__main__":
    unittest.main()
