import importlib.util
import pathlib
import struct
import sys
import unittest
from unittest import mock


MODULE_PATH = pathlib.Path(__file__).parents[1] / "outputs" / "wtgahrs3_reader.py"
SPEC = importlib.util.spec_from_file_location("wtgahrs3_reader", MODULE_PATH)
reader = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = reader
SPEC.loader.exec_module(reader)


def wit_frame(frame_type, values):
    body = bytes((0x55, frame_type)) + struct.pack("<hhhh", *values)
    return body + bytes((sum(body) & 0xFF,))


class Wtgahrs3TtlTests(unittest.TestCase):
    def test_cli_defaults_to_ttl_usb_auto_detection(self):
        args = reader.build_parser().parse_args([])
        self.assertEqual(args.transport, "ttl")
        self.assertEqual(args.port, "auto")
        self.assertEqual(args.baud, 9600)

    def test_parse_valid_ttl_frame(self):
        frame = wit_frame(0x53, (16384, -16384, 8192, 2500))
        frame_type, values = reader.parse_wit_ttl_frame(frame)
        self.assertEqual(frame_type, 0x53)
        self.assertEqual(values, (16384, -16384, 8192, 2500))

    def test_invalid_ttl_checksum_is_rejected(self):
        frame = bytearray(wit_frame(0x51, (1, 2, 3, 4)))
        frame[-1] ^= 1
        with self.assertRaises(reader.ProtocolError):
            reader.parse_wit_ttl_frame(bytes(frame))

    def test_ttl_frames_are_combined_into_one_sample(self):
        frames = iter(
            (
                wit_frame(0x51, (16384, -16384, 8192, 2500)),
                wit_frame(0x52, (16384, -16384, 8192, 0)),
                wit_frame(0x53, (16384, -16384, 8192, 0)),
            )
        )
        transport = object.__new__(reader.WitTtlSerial)
        transport.acceleration_g = None
        transport.angular_velocity_dps = None
        transport.temperature_c = 0.0
        transport._read_frame = lambda: next(frames)

        sample = transport.read_sample()

        self.assertEqual(sample.acceleration_g, {"x": 8.0, "y": -8.0, "z": 4.0})
        self.assertEqual(
            sample.angular_velocity_dps,
            {"x": 1000.0, "y": -1000.0, "z": 500.0},
        )
        self.assertEqual(sample.angle_deg, {"x": 90.0, "y": -90.0, "z": 45.0})
        self.assertEqual(sample.temperature_c, 25.0)

    @mock.patch.object(reader.glob, "glob")
    @mock.patch.object(reader.os.path, "realpath")
    def test_usb_candidates_prefer_stable_link_and_exclude_turntable(
        self, realpath, glob
    ):
        values = {
            "/dev/serial/by-id/imu": "/dev/ttyUSB1",
            "/dev/serial/by-id/turntable": "/dev/ttyUSB0",
            "/dev/serial/by-path/lower": "/dev/ttyUSB1",
            "/dev/ttyUSB0": "/dev/ttyUSB0",
            "/dev/ttyUSB1": "/dev/ttyUSB1",
        }
        realpath.side_effect = lambda path: values.get(path, path)
        glob.side_effect = (
            lambda pattern: {
                "/dev/serial/by-id/*": [
                    "/dev/serial/by-id/turntable",
                    "/dev/serial/by-id/imu",
                ],
                "/dev/serial/by-path/*": ["/dev/serial/by-path/lower"],
                "/dev/ttyUSB*": ["/dev/ttyUSB0", "/dev/ttyUSB1"],
                "/dev/ttyACM*": [],
            }[pattern]
        )

        candidates = reader.serial_port_candidates(
            ["/dev/serial/by-id/turntable"]
        )

        self.assertEqual(candidates, ["/dev/serial/by-id/imu"])


if __name__ == "__main__":
    unittest.main()
