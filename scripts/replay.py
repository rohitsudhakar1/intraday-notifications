"""Replay events.jsonl into the running API.

The feed covers 90 minutes. Replaying it in real time would make for a poor
demo, so we compress: the gap between two events is divided by --speed. At the
default 120x the morning plays out in about 45 seconds and you watch the
billing queue back up, breach, and recover.

    python scripts/replay.py                 # 120x
    python scripts/replay.py --speed 0       # as fast as the API accepts
    python scripts/replay.py --no-sort       # preserve file order, traps included

By default the file is sent in file order, NOT sorted by timestamp, because the
out-of-order events at the end are part of what the engine has to survive.
"""

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FEED = ROOT / "data" / "events.jsonl"


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def load(path: Path) -> list[dict]:
    events = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError as error:
            print(f"skipping malformed line {line_no}: {error}", file=sys.stderr)
    return events


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--feed", default=str(DEFAULT_FEED))
    parser.add_argument("--speed", type=float, default=120.0,
                        help="time compression; 0 means no waiting at all")
    parser.add_argument("--sort", action="store_true",
                        help="sort by timestamp first (hides the out-of-order events)")
    parser.add_argument("--reset", action="store_true",
                        help="clear events, state and notifications before replaying")
    args = parser.parse_args()

    events = load(Path(args.feed))
    if args.sort:
        events.sort(key=lambda event: event["ts"])
    if not events:
        print("no events to replay", file=sys.stderr)
        return 1

    client = httpx.Client(base_url=args.url, timeout=30.0)

    try:
        client.get("/api/state").raise_for_status()
    except Exception:
        print(f"cannot reach the API at {args.url}. Start it with:\n"
              f"  uvicorn app.main:app --reload", file=sys.stderr)
        return 1

    if args.reset:
        client.post("/api/reset")
        print("state cleared")

    totals = {"accepted": 0, "duplicates": 0, "stale": 0, "notifications": 0}
    previous = None
    started = time.monotonic()

    print(f"replaying {len(events)} events at {args.speed or 'max'}x\n" + "-" * 78)

    for event in events:
        ts = parse_ts(event["ts"])
        if previous is not None and args.speed > 0:
            # Negative gaps come from the deliberately out-of-order events; do
            # not sleep backwards.
            gap = max(0.0, (ts - previous).total_seconds()) / args.speed
            if gap:
                time.sleep(min(gap, 2.0))
        previous = ts

        response = client.post("/api/events", json=event)
        if response.status_code >= 400:
            print(f"  rejected {event['event_id']}: {response.text}", file=sys.stderr)
            continue
        for key, value in response.json().items():
            totals[key] += value

    elapsed = time.monotonic() - started
    print("-" * 78)
    print(f"done in {elapsed:.1f}s | "
          f"accepted {totals['accepted']}, duplicates {totals['duplicates']}, "
          f"stale {totals['stale']}, notifications {totals['notifications']}")

    summary = client.get("/api/suppressions").json()["by_reason"]
    if summary:
        print("\nsuppressed, by reason:")
        for row in summary:
            print(f"  {row['count']:>5}  {row['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
