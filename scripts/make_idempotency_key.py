#!/usr/bin/env python3
"""Create a stable Feishu message idempotency key for one judgment."""

from __future__ import annotations

import argparse
import hashlib


def make_key(
    meeting_id: str,
    recipient: str,
    event_ids: list[str],
    kind: str,
    message: str,
) -> str:
    normalized_ids = sorted({event_id.strip() for event_id in event_ids if event_id.strip()})
    message_fingerprint = hashlib.sha256(message.strip().encode("utf-8")).hexdigest()
    material = (
        meeting_id.strip()
        + "\n"
        + recipient.strip()
        + "\n"
        + "\n".join(normalized_ids)
        + "\n"
        + kind.strip()
        + "\n"
        + message_fingerprint
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
    return "fma-" + digest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--meeting-id", required=True)
    parser.add_argument("--recipient", required=True)
    parser.add_argument("--event-id", action="append", default=[])
    parser.add_argument("--kind", required=True)
    parser.add_argument("--message", required=True)
    args = parser.parse_args()
    print(
        make_key(
            args.meeting_id,
            args.recipient,
            args.event_id,
            args.kind,
            args.message,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
