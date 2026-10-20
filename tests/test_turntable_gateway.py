import importlib.util
import pathlib
import sys
import unittest


MODULE_PATH = pathlib.Path(__file__).parents[1] / "outputs" / "turntable_gateway.py"
SPEC = importlib.util.spec_from_file_location("turntable_gateway", MODULE_PATH)
gateway = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = gateway
SPEC.loader.exec_module(gateway)


class TurntableProtocolTests(unittest.TestCase):
    def test_status_query_checksum(self):
        frame = gateway.make_frame(bytes.fromhex("AA 06 01 02 01"))
        self.assertEqual(frame, bytes.fromhex("AA 06 01 02 01 B4"))
        gateway.validate_command_frame(frame)

    def test_stop_checksum(self):
        frame = gateway.make_frame(bytes.fromhex("AA 0C 01 06 04 01 00 00 00 00 00"))
        self.assertEqual(frame, bytes.fromhex("AA 0C 01 06 04 01 00 00 00 00 00 C2"))
        gateway.validate_command_frame(frame)

    def test_parser_accepts_documented_15_byte_status(self):
        frame = bytes.fromhex("AA 10 01 07 00 00 64 00 31 00 36 00 00 00 8D")
        parser = gateway.FrameStreamParser()
        self.assertEqual(parser.feed(frame[:7]), [])
        self.assertEqual(parser.feed(frame[7:]), [frame])

    def test_parser_resynchronizes_after_noise(self):
        frame = bytes.fromhex("AA 10 01 07 00 00 64 00 31 00 36 00 00 00 8D")
        parser = gateway.FrameStreamParser()
        self.assertEqual(parser.feed(b"\x95\xD7\xF1" + frame), [frame])
        self.assertEqual(parser.discarded_bytes, 3)

    def test_invalid_checksum_is_rejected(self):
        frame = bytearray(bytes.fromhex("AA 06 01 02 01 B4"))
        frame[-1] ^= 1
        with self.assertRaises(gateway.ProtocolError):
            gateway.validate_command_frame(bytes(frame))

    def test_unknown_command_is_rejected(self):
        frame = gateway.make_frame(bytes.fromhex("AA 06 01 09 01"))
        with self.assertRaises(gateway.ProtocolError):
            gateway.validate_command_frame(frame)

    def test_ambiguous_step_command_is_rejected(self):
        frame = gateway.make_frame(bytes.fromhex("AA 0B 01 06 06 01 00 0A 00 00"))
        with self.assertRaisesRegex(gateway.ProtocolError, "step subcommand is disabled"):
            gateway.validate_command_frame(frame)


if __name__ == "__main__":
    unittest.main()
