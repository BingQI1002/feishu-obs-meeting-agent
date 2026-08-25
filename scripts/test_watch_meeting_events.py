#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).with_name("watch_meeting_events.py")
SPEC = importlib.util.spec_from_file_location("watch_meeting_events", SCRIPT)
assert SPEC and SPEC.loader
WATCHER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(WATCHER)


def transcript_event(event_id: str, text: str, speaker: str = "A") -> dict:
    return {
        "event_id": event_id,
        "event_type": "transcript_received",
        "event_time": "2026-08-25T10:00:00+08:00",
        "payload": {
            "transcript_received_items": [
                {"speaker": {"user_name": speaker}, "text": text}
            ]
        },
    }


class WatchMeetingEventsTest(unittest.TestCase):
    def test_normalizes_top_level_and_data_envelopes(self) -> None:
        top = WATCHER.normalize_response(
            {"meeting": {"status": "ongoing"}, "events": [], "page_token": "p1"}
        )
        nested = WATCHER.normalize_response(
            {
                "data": {
                    "meeting": {"status": "ongoing"},
                    "events": [],
                    "page_token": "p2",
                }
            }
        )
        self.assertEqual(top["page_token"], "p1")
        self.assertEqual(nested["page_token"], "p2")

    def test_deduplicates_replayed_events(self) -> None:
        state = WATCHER.default_state("123", "bot", "profile")
        response = WATCHER.normalize_response({"events": [transcript_event("e1", "hello")]})
        first = WATCHER.collect_new_events(response, state)
        second = WATCHER.collect_new_events(response, state)
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])

    def test_preserves_long_transcript_without_truncation(self) -> None:
        text = "重要内容" * 100
        event = transcript_event("e2", text, speaker="用户")
        lines = WATCHER.transcript_lines([event])
        self.assertEqual(lines[0]["text"], text)
        self.assertEqual(lines[0]["speaker"], "用户")

    def test_batch_marks_baseline_and_contains_raw_events(self) -> None:
        state = WATCHER.default_state("123", "bot", "profile")
        event = transcript_event("e3", "baseline")
        WATCHER.add_pending(state, [event], baseline=True, now_epoch=1.0)
        batch = WATCHER.build_batch(state)
        self.assertIsNotNone(batch)
        assert batch is not None
        self.assertTrue(batch["baseline"])
        self.assertEqual(batch["events"][0]["event_id"], "e3")
        self.assertEqual(state["pending_events"], [])

    def test_large_baseline_is_split_without_truncating_events(self) -> None:
        state = WATCHER.default_state("123", "bot", "profile")
        events = [
            transcript_event("e1", "一" * 20),
            transcript_event("e2", "二" * 20),
            transcript_event("e3", "三" * 20),
        ]
        WATCHER.add_pending(state, events, baseline=True, now_epoch=1.0)
        first = WATCHER.build_batch(state, max_events=2, max_output_bytes=100000)
        second = WATCHER.build_batch(state, max_events=2, max_output_bytes=100000)
        assert first is not None and second is not None
        self.assertEqual(len(first["events"]), 2)
        self.assertEqual(len(second["events"]), 1)
        self.assertTrue(first["baseline"])
        self.assertTrue(second["baseline"])
        self.assertEqual(second["transcript"][0]["text"], "三" * 20)

    def test_state_identity_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "watcher-state.json"
            WATCHER.save_json_atomic(
                path, WATCHER.default_state("123", "bot", "profile-a")
            )
            with self.assertRaises(ValueError):
                WATCHER.load_state(path, "123", "bot", "profile-b")

    def test_nested_response_processes_events_and_status(self) -> None:
        state = WATCHER.default_state("123", "bot", "profile")
        payload = {
            "data": {
                "meeting": {"status": "ended"},
                "events": [transcript_event("e4", "final")],
                "page_token": "next",
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            events_log = Path(directory) / "events.jsonl"
            new_events, status = WATCHER.process_snapshot(
                payload, state, baseline=False, events_log=events_log, now_epoch=5.0
            )
            self.assertEqual(len(new_events), 1)
            self.assertEqual(status, "ended")
            self.assertEqual(state["page_token"], "next")
            self.assertTrue(events_log.exists())

    def test_stale_cursor_falls_back_to_full_fetch(self) -> None:
        args = mock.Mock()
        args.lark_cli = "lark-cli"
        args.identity = "bot"
        args.meeting_id = "123"
        args.profile = "profile"
        state = WATCHER.default_state("123", "bot", "profile")
        state["page_token"] = "stale"
        expected = {"events": []}
        with mock.patch.object(
            WATCHER,
            "run_command",
            side_effect=[RuntimeError("cursor expired"), expected],
        ) as runner:
            result = WATCHER.fetch_snapshot(args, state)
        self.assertEqual(result, expected)
        self.assertEqual(runner.call_count, 2)
        self.assertIn("--page-token", runner.call_args_list[0].args[0])
        self.assertNotIn("--page-token", runner.call_args_list[1].args[0])

    def test_non_cursor_error_does_not_fall_back(self) -> None:
        args = mock.Mock()
        args.lark_cli = "lark-cli"
        args.identity = "bot"
        args.meeting_id = "123"
        args.profile = "profile"
        state = WATCHER.default_state("123", "bot", "profile")
        state["page_token"] = "cursor"
        with mock.patch.object(
            WATCHER, "run_command", side_effect=RuntimeError("permission denied")
        ) as runner:
            with self.assertRaises(RuntimeError):
                WATCHER.fetch_snapshot(args, state)
        self.assertEqual(runner.call_count, 1)

    def test_command_timeout_is_bounded(self) -> None:
        with mock.patch.object(
            WATCHER.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd=["lark-cli"], timeout=3),
        ):
            with self.assertRaisesRegex(RuntimeError, "timed out after 3 seconds"):
                WATCHER.run_command(["lark-cli"], timeout=3)

    def test_meeting_end_error_becomes_terminal_signal(self) -> None:
        result = mock.Mock()
        result.returncode = 1
        result.stderr = "code=20001 meeting_status_MEETING_END"
        result.stdout = ""
        with mock.patch.object(WATCHER.subprocess, "run", return_value=result):
            with self.assertRaises(WATCHER.MeetingEnded):
                WATCHER.run_command(["lark-cli"], timeout=3)

    def test_second_watcher_lock_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".watcher.lock"
            first = WATCHER.acquire_lock(path)
            try:
                with self.assertRaisesRegex(RuntimeError, "another watcher"):
                    WATCHER.acquire_lock(path)
            finally:
                first.close()

    def test_response_meeting_id_mismatch_fails_closed(self) -> None:
        state = WATCHER.default_state("123", "bot", "profile")
        payload = {"meeting": {"id": "999", "status": "ongoing"}, "events": []}
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "meeting id"):
                WATCHER.process_snapshot(
                    payload,
                    state,
                    baseline=False,
                    events_log=Path(directory) / "events.jsonl",
                    now_epoch=1.0,
                )


if __name__ == "__main__":
    unittest.main()
