"""Focused private wire-tail key-fragment redaction tests."""

import unittest

import deepseek_adapter as adapter
from test_deepseek_adapter import AdapterFixture, FAST, SYNTHETIC_KEY


class BoundedWireTailTests(unittest.TestCase):
    def test_every_trailing_proper_key_prefix_is_removed(self):
        config = {**FAST, "max_wire_tail_bytes": 4096}
        key = SYNTHETIC_KEY.encode("ascii")
        filler = b"\x01" * 100
        for length in range(1, len(key)):
            with self.subTest(length=length):
                tail = adapter._bounded_tail(filler + key[:length], config, SYNTHETIC_KEY)
                self.assertEqual(tail, filler)
                self.assertLessEqual(len(tail), config["max_wire_tail_bytes"])
                self.assertNotIn(key, tail)
                self.assertFalse(any(tail.endswith(key[:size]) for size in range(1, len(key))))

    def test_complete_key_is_redacted_and_following_bytes_are_preserved(self):
        config = {**FAST, "max_wire_tail_bytes": 4096}
        key = SYNTHETIC_KEY.encode("ascii")
        tail = adapter._bounded_tail(b"before:" + key + b":after", config, SYNTHETIC_KEY)
        self.assertEqual(tail, b"before:[REDACTED]:after")
        self.assertLessEqual(len(tail), config["max_wire_tail_bytes"])
        self.assertNotIn(key, tail)

    def test_both_edge_fragments_are_removed_from_a_small_window(self):
        config = {**FAST, "max_wire_tail_bytes": 58}
        key = SYNTHETIC_KEY.encode("ascii")
        filler = b"\x01" * 50
        data = key[-5:] + filler + key[:7]
        tail = adapter._bounded_tail(data, config, SYNTHETIC_KEY)
        self.assertEqual(tail, filler)
        self.assertLessEqual(len(tail), config["max_wire_tail_bytes"])
        self.assertFalse(any(tail.startswith(key[-size:]) for size in range(1, len(key))))
        self.assertFalse(any(tail.endswith(key[:size]) for size in range(1, len(key))))

    def test_short_key_substring_has_no_key_fragment_at_either_edge(self):
        config = {**FAST, "max_wire_tail_bytes": 4096}
        key = SYNTHETIC_KEY.encode("ascii")
        data = key[3:9]
        tail = adapter._bounded_tail(data, config, SYNTHETIC_KEY)
        self.assertEqual(tail, data)
        self.assertLessEqual(len(tail), config["max_wire_tail_bytes"])
        self.assertFalse(any(tail.startswith(key[-size:]) for size in range(1, len(key))))
        self.assertFalse(any(tail.endswith(key[:size]) for size in range(1, len(key))))

    def test_bound_empty_input_and_missing_key(self):
        config = {**FAST, "max_wire_tail_bytes": 58}
        key = SYNTHETIC_KEY.encode("ascii")
        unredacted = b"wire:" + key + b":end"
        self.assertEqual(adapter._bounded_tail(unredacted, config, None), unredacted)
        oversized = b"\x01" * 100
        tail = adapter._bounded_tail(oversized, config, None)
        self.assertEqual(tail, oversized[-config["max_wire_tail_bytes"]:])
        self.assertLessEqual(len(tail), config["max_wire_tail_bytes"])
        self.assertEqual(adapter._bounded_tail(b"", config, SYNTHETIC_KEY), b"")
        self.assertEqual(adapter._bounded_tail(b"", config, None), b"")


class WireTailTransportTests(AdapterFixture):
    def test_stored_error_wire_tail_drops_a_trailing_key_prefix(self):
        key = SYNTHETIC_KEY.encode("ascii")
        body = b"\x01" * 100 + key[:7]
        terminal = self.run_to_terminal(
            self.adapter, "Task", "wire-tail-prefix", {"kind": "http_error", "status": 401, "body": body})
        tail_path = self.job_dir(terminal["job_id"]) / "wire-tail"
        tail = tail_path.read_bytes()
        self.assertEqual(tail, b"\x01" * 100)
        self.assertLessEqual(len(tail), FAST["max_wire_tail_bytes"])
        self.assertFalse(any(tail.endswith(key[:size]) for size in range(1, len(key))))
