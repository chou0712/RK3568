import queue
import socket
import time
import unittest

from satellite_tracker.config import NetworkConfig
from satellite_tracker.protocol import build_status_query
from satellite_tracker.transport import TransportEvent, UdpTransport


class UdpTransportTests(unittest.TestCase):
    STATUS = bytes.fromhex("AA 10 01 07 00 00 64 00 31 00 36 00 00 00 8D")

    def test_local_round_trip(self) -> None:
        gateway = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        gateway.bind(("127.0.0.1", 0))
        gateway.settimeout(2.0)
        events: queue.Queue[TransportEvent] = queue.Queue()
        transport = UdpTransport(events)
        try:
            transport.connect(
                NetworkConfig(
                    gateway_host="127.0.0.1",
                    gateway_port=gateway.getsockname()[1],
                    local_host="127.0.0.1",
                    local_port=0,
                )
            )
            transport.send(build_status_query())
            request, peer = gateway.recvfrom(1024)
            self.assertEqual(request, build_status_query())
            gateway.sendto(self.STATUS, peer)

            status = None
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and status is None:
                event = events.get(timeout=0.3)
                if event.kind == "status":
                    status = event.payload
            self.assertIsNotNone(status)
            self.assertEqual(status.azimuth_deg, 10.0)
        finally:
            transport.close()
            gateway.close()


if __name__ == "__main__":
    unittest.main()
