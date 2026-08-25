#!/usr/bin/env python3
"""Persist and emit new Feishu meeting events as recoverable semantic batches.

This script is transport only. It never reads OBS, makes judgments, sends
messages, joins meetings, or leaves meetings.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import time
from typing import Any, Iterable


ENDED_STATUSES = {"ended", "finished", "terminated", "closed"}
MAX_SEEN_EVENT_IDS = 10000
CURSOR_ERROR_MARKERS = (
    "page_token",
    "page token",
    "invalid cursor",
    "cursor expired",
    "expired cursor",
)


class MeetingEnded(Exception):
    """The API says the live-event window has ended."""


def is_cursor_error(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in CURSOR_ERROR_MARKERS)


def is_meeting_ended_error(message: str) -> bool:
    upper = message.upper()
    return "20001" in upper and (
        "MEETING_STATUS_MEETING_END" in upper or "MEETING_END" in upper
    )


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def stable_event_id(event: dict[str, Any]) -> str:
    event_id = event.get("event_id")
    if event_id:
        return str(event_id)
    encoded = json.dumps(event, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def response_container(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    if isinstance(data, dict) and (
        "events" in data or "meeting" in data or "page_token" in data
    ):
        return data
    return payload


def normalize_response(payload: dict[str, Any]) -> dict[str, Any]:
    container = response_container(payload)
    events = container.get("events")
    if not isinstance(events, list):
        events = []
    events = [event for event in events if isinstance(event, dict)]

    meeting = container.get("meeting")
    if not isinstance(meeting, dict):
        meeting = {}

    identity = container.get("identity", payload.get("identity"))
    page_token = container.get("page_token")
    if page_token is None:
        page_token = payload.get("page_token")

    return {
        "meeting": meeting,
        "identity": identity,
        "events": events,
        "page_token": str(page_token) if page_token else "",
        "has_more": bool(container.get("has_more", payload.get("has_more", False))),
    }


def default_state(meeting_id: str, identity: str, profile: str) -> dict[str, Any]:
    return {
        "version": 1,
        "meeting_id": meeting_id,
        "identity": identity,
        "profile": profile,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "last_success_at": None,
        "page_token": "",
        "meeting_status": "",
        "seen_event_ids": [],
        "pending_events": [],
        "pending_baseline": False,
        "pending_started_epoch": None,
        "pending_last_event_epoch": None,
        "last_error": None,
        "consecutive_errors": 0,
        "last_error_fingerprint": None,
        "last_error_emitted_epoch": None,
    }


def load_state(path: Path, meeting_id: str, identity: str, profile: str) -> tuple[dict[str, Any], bool]:
    if not path.exists():
        return default_state(meeting_id, identity, profile), True
    with path.open("r", encoding="utf-8") as handle:
        state = json.load(handle)
    if str(state.get("meeting_id")) != meeting_id:
        raise ValueError("state meeting_id does not match requested meeting")
    if state.get("identity") != identity:
        raise ValueError("state identity does not match requested identity")
    if (state.get("profile") or "") != profile:
        raise ValueError("state profile does not match requested profile")
    return state, False


def save_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def acquire_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    os.chmod(path, 0o600)
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        handle.close()
        raise RuntimeError("another watcher already owns this state directory") from error
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()) + "\n")
    handle.flush()
    return handle


def append_jsonl(path: Path, items: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for item in items:
            handle.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")
        handle.flush()
    os.chmod(path, 0o600)


def collect_new_events(
    normalized: dict[str, Any], state: dict[str, Any]
) -> list[dict[str, Any]]:
    seen = set(str(value) for value in state.get("seen_event_ids", []))
    new_events: list[dict[str, Any]] = []
    for event in normalized["events"]:
        key = stable_event_id(event)
        if key in seen:
            continue
        event_copy = dict(event)
        event_copy["_watcher_event_key"] = key
        new_events.append(event_copy)
        seen.add(key)

    ordered_seen = list(state.get("seen_event_ids", []))
    ordered_seen.extend(event["_watcher_event_key"] for event in new_events)
    if len(ordered_seen) > MAX_SEEN_EVENT_IDS:
        ordered_seen = ordered_seen[-MAX_SEEN_EVENT_IDS:]
    state["seen_event_ids"] = ordered_seen
    return new_events


def add_pending(
    state: dict[str, Any], events: list[dict[str, Any]], baseline: bool, now_epoch: float
) -> None:
    if not events:
        return
    if not state.get("pending_events"):
        state["pending_started_epoch"] = now_epoch
        state["pending_baseline"] = baseline
    else:
        state["pending_baseline"] = bool(state.get("pending_baseline")) and baseline
    state.setdefault("pending_events", []).extend(events)
    state["pending_last_event_epoch"] = now_epoch


def _text_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_text_value(item) for item in value)
    if isinstance(value, dict):
        for key in ("text", "content", "value"):
            if key in value:
                return _text_value(value[key])
    return str(value)


def _speaker_name(item: dict[str, Any], event: dict[str, Any]) -> str:
    speaker = item.get("speaker")
    if isinstance(speaker, dict):
        for key in ("user_name", "name", "label"):
            if speaker.get(key):
                return str(speaker[key])
    actors = event.get("actors")
    if isinstance(actors, list) and actors and isinstance(actors[0], dict):
        for key in ("name", "label"):
            if actors[0].get(key):
                return str(actors[0][key])
    return "未知说话人"


def transcript_lines(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for event in events:
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        items = payload.get("transcript_received_items")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            text = _text_value(item.get("text")).strip()
            if not text:
                continue
            lines.append(
                {
                    "event_id": event.get("event_id"),
                    "event_time": event.get("event_time"),
                    "speaker": _speaker_name(item, event),
                    "text": text,
                }
            )
    return lines


def build_batch(
    state: dict[str, Any],
    max_events: int = 25,
    max_output_bytes: int = 131072,
) -> dict[str, Any] | None:
    events = state.get("pending_events")
    if not isinstance(events, list) or not events:
        return None
    selected: list[dict[str, Any]] = []
    estimated_bytes = 0
    for event in events:
        # The output includes both the raw event and a compact transcript view.
        event_bytes = len(
            json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ) * 2
        if selected and (
            len(selected) >= max_events
            or estimated_bytes + event_bytes > max_output_bytes
        ):
            break
        selected.append(event)
        estimated_bytes += event_bytes

    batch = {
        "type": "feishu_meeting_batch",
        "meeting_id": state.get("meeting_id"),
        "baseline": bool(state.get("pending_baseline")),
        "emitted_at": utc_now(),
        "event_ids": [event.get("_watcher_event_key") for event in selected],
        "transcript": transcript_lines(selected),
        "events": selected,
    }
    state["pending_events"] = events[len(selected) :]
    if not state["pending_events"]:
        state["pending_baseline"] = False
        state["pending_started_epoch"] = None
        state["pending_last_event_epoch"] = None
    return batch


def emit_pending_batches(state: dict[str, Any], args: argparse.Namespace) -> None:
    while state.get("pending_events"):
        batch = build_batch(
            state,
            max_events=args.max_events_per_batch,
            max_output_bytes=args.max_output_bytes,
        )
        if not batch:
            break
        emit(batch)


def should_flush(
    state: dict[str, Any], now_epoch: float, quiet_seconds: float, max_batch_seconds: float
) -> bool:
    if not state.get("pending_events"):
        return False
    started = state.get("pending_started_epoch")
    last_event = state.get("pending_last_event_epoch")
    if started is None or last_event is None:
        return True
    return (
        now_epoch - float(last_event) >= quiet_seconds
        or now_epoch - float(started) >= max_batch_seconds
    )


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)


def build_command(args: argparse.Namespace, page_token: str) -> list[str]:
    command = [
        args.lark_cli,
        "vc",
        "+meeting-events",
        "--as",
        args.identity,
        "--meeting-id",
        args.meeting_id,
        "--page-all",
        "--format",
        "json",
    ]
    if page_token:
        command.extend(["--page-token", page_token])
    if args.profile:
        command.extend(["--profile", args.profile])
    return command


def run_command(command: list[str], timeout: float) -> dict[str, Any]:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"lark-cli timed out after {timeout:g} seconds") from error
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "lark-cli failed").strip()
        if is_meeting_ended_error(message):
            raise MeetingEnded(message)
        raise RuntimeError(message)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("lark-cli returned invalid JSON") from error
    if not isinstance(payload, dict):
        raise RuntimeError("lark-cli returned a non-object JSON payload")
    return payload


def fetch_snapshot(args: argparse.Namespace, state: dict[str, Any]) -> dict[str, Any]:
    cursor = str(state.get("page_token") or "")
    try:
        return run_command(build_command(args, cursor), args.command_timeout)
    except MeetingEnded:
        raise
    except RuntimeError as error:
        if not cursor or not is_cursor_error(str(error)):
            raise
        # A stale cursor should not kill a long meeting. Fall back once to a
        # full fetch; event-id dedupe prevents replay.
        return run_command(build_command(args, ""), args.command_timeout)


def load_fixture(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    if text.startswith("["):
        payload = json.loads(text)
        if not isinstance(payload, list):
            raise ValueError("fixture JSON array expected")
        return [item for item in payload if isinstance(item, dict)]
    snapshots: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if not isinstance(item, dict):
            raise ValueError("fixture JSONL entries must be objects")
        snapshots.append(item)
    return snapshots


def process_snapshot(
    payload: dict[str, Any],
    state: dict[str, Any],
    baseline: bool,
    events_log: Path,
    now_epoch: float,
) -> tuple[list[dict[str, Any]], str]:
    normalized = normalize_response(payload)
    response_meeting_id = normalized["meeting"].get("id")
    if response_meeting_id and str(response_meeting_id) != str(state["meeting_id"]):
        raise ValueError("response meeting id does not match requested meeting")
    new_events = collect_new_events(normalized, state)
    if new_events:
        append_jsonl(events_log, new_events)
        add_pending(state, new_events, baseline=baseline, now_epoch=now_epoch)
    meeting_status = str(normalized["meeting"].get("status") or "")
    state["page_token"] = normalized["page_token"]
    state["meeting_status"] = meeting_status
    state["last_success_at"] = utc_now()
    state["last_error"] = None
    state["consecutive_errors"] = 0
    state["last_error_fingerprint"] = None
    state["updated_at"] = utc_now()
    return new_events, meeting_status


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--meeting-id", required=True)
    parser.add_argument("--identity", choices=("bot", "user"), required=True)
    parser.add_argument("--profile", default="")
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--poll-seconds", type=float, default=10.0)
    parser.add_argument("--quiet-seconds", type=float, default=8.0)
    parser.add_argument("--max-batch-seconds", type=float, default=30.0)
    parser.add_argument("--max-events-per-batch", type=int, default=25)
    parser.add_argument("--max-output-bytes", type=int, default=131072)
    parser.add_argument("--command-timeout", type=float, default=30.0)
    parser.add_argument("--max-backoff-seconds", type=float, default=60.0)
    parser.add_argument("--error-reminder-seconds", type=float, default=120.0)
    parser.add_argument("--lark-cli", default="lark-cli")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--fixture-interval", type=float, default=0.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if (
        args.poll_seconds < 0
        or args.quiet_seconds < 0
        or args.max_batch_seconds <= 0
        or args.max_events_per_batch <= 0
        or args.max_output_bytes <= 0
        or args.command_timeout <= 0
        or args.max_backoff_seconds <= 0
        or args.error_reminder_seconds <= 0
        or args.fixture_interval < 0
    ):
        raise ValueError("invalid timing parameters")

    os.umask(0o077)
    args.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(args.state_dir, 0o700)
    lock_handle = acquire_lock(args.state_dir / ".watcher.lock")
    state_path = args.state_dir / "watcher-state.json"
    events_log = args.state_dir / "events.jsonl"
    state, first_snapshot = load_state(
        state_path, args.meeting_id, args.identity, args.profile
    )

    stop_requested = False

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    fixtures = load_fixture(args.fixture) if args.fixture else None
    fixture_index = 0

    while not stop_requested:
        try:
            if fixtures is not None:
                if fixture_index >= len(fixtures):
                    break
                payload = fixtures[fixture_index]
                fixture_index += 1
            else:
                payload = fetch_snapshot(args, state)

            now_epoch = time.time()
            new_events, meeting_status = process_snapshot(
                payload,
                state,
                baseline=first_snapshot,
                events_log=events_log,
                now_epoch=now_epoch,
            )

            if first_snapshot and new_events:
                emit_pending_batches(state, args)
            elif fixtures is not None and new_events:
                emit_pending_batches(state, args)
            elif args.once and new_events:
                emit_pending_batches(state, args)
            elif should_flush(
                state, now_epoch, args.quiet_seconds, args.max_batch_seconds
            ):
                emit_pending_batches(state, args)

            first_snapshot = False
            save_json_atomic(state_path, state)

            if meeting_status.lower() in ENDED_STATUSES:
                emit_pending_batches(state, args)
                emit(
                    {
                        "type": "feishu_meeting_ended",
                        "meeting_id": args.meeting_id,
                        "status": meeting_status,
                        "emitted_at": utc_now(),
                    }
                )
                save_json_atomic(state_path, state)
                return 0

            if args.once:
                break
            if fixtures is None:
                time.sleep(args.poll_seconds)
            elif fixture_index < len(fixtures) and args.fixture_interval:
                time.sleep(args.fixture_interval)

        except MeetingEnded as error:
            emit_pending_batches(state, args)
            state["meeting_status"] = "ended"
            state["last_error"] = None
            state["updated_at"] = utc_now()
            save_json_atomic(state_path, state)
            emit(
                {
                    "type": "feishu_meeting_ended",
                    "meeting_id": args.meeting_id,
                    "status": "ended",
                    "source": "lark-cli-20001",
                    "emitted_at": utc_now(),
                }
            )
            return 0

        except Exception as error:  # keep a long meeting observable and recoverable
            now_epoch = time.time()
            message = str(error)
            fingerprint = hashlib.sha256(message.encode("utf-8")).hexdigest()
            previous_fingerprint = state.get("last_error_fingerprint")
            last_emitted = state.get("last_error_emitted_epoch")
            state["consecutive_errors"] = int(state.get("consecutive_errors") or 0) + 1
            state["last_error"] = message
            state["last_error_fingerprint"] = fingerprint
            should_emit_error = (
                fingerprint != previous_fingerprint
                or last_emitted is None
                or now_epoch - float(last_emitted) >= args.error_reminder_seconds
            )
            if should_emit_error:
                state["last_error_emitted_epoch"] = now_epoch
                emit(
                    {
                        "type": "feishu_meeting_watcher_error",
                        "meeting_id": args.meeting_id,
                        "error": message,
                        "consecutive_errors": state["consecutive_errors"],
                        "emitted_at": utc_now(),
                    }
                )
            state["updated_at"] = utc_now()
            save_json_atomic(state_path, state)
            if args.once or fixtures is not None:
                return 1
            base_delay = max(args.poll_seconds, 1.0)
            exponent = min(state["consecutive_errors"] - 1, 6)
            delay = min(args.max_backoff_seconds, base_delay * (2**exponent))
            time.sleep(delay)

    emit_pending_batches(state, args)
    state["updated_at"] = utc_now()
    save_json_atomic(state_path, state)
    emit(
        {
            "type": "feishu_meeting_watcher_stopped",
            "meeting_id": args.meeting_id,
            "emitted_at": utc_now(),
        }
    )
    lock_handle.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
