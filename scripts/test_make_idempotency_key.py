#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


SCRIPT = Path(__file__).with_name("make_idempotency_key.py")
SPEC = importlib.util.spec_from_file_location("make_idempotency_key", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class IdempotencyKeyTest(unittest.TestCase):
    def test_key_is_stable_across_event_order_and_duplicates(self) -> None:
        first = MODULE.make_key("123", "user", ["e2", "e1", "e1"], "advice", "判断一")
        second = MODULE.make_key("123", "user", ["e1", "e2"], "advice", "判断一")
        self.assertEqual(first, second)

    def test_key_fits_feishu_limit(self) -> None:
        key = MODULE.make_key("123", "user", ["event"], "challenge", "判断")
        self.assertTrue(key.startswith("fma-"))
        self.assertLessEqual(len(key), 50)

    def test_two_judgments_on_same_events_do_not_collide(self) -> None:
        first = MODULE.make_key("123", "user", ["e1"], "advice", "判断一")
        second = MODULE.make_key("123", "user", ["e1"], "advice", "判断二")
        self.assertNotEqual(first, second)

    def test_same_message_to_different_recipient_does_not_collide(self) -> None:
        first = MODULE.make_key("123", "user-a", ["e1"], "advice", "判断")
        second = MODULE.make_key("123", "user-b", ["e1"], "advice", "判断")
        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
