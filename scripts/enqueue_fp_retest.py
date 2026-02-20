"""Re-enqueue known false-positive events with fresh UUIDs for second-look testing.

Usage (inside worker container):
    python -m scripts.enqueue_fp_retest [--dry-run]

Reads both false_positives.json and reprocess_reviewed.json, deduplicates by
original event_id, assigns new UUIDs (prefixed with 'fp-retest-'), and pushes
them onto the Redis queue.
"""

import json
import sys
import uuid
from pathlib import Path

import redis

QUEUE_KEY = "sharesentinel:jobs"
REDIS_URL = "redis://redis:6379/0"


def load_events() -> list[dict]:
    """Load and deduplicate FP events from both JSON files."""
    seen = set()
    events = []

    for fp in ("false_positives.json", "scripts/reprocess_reviewed.json"):
        path = Path(fp)
        if not path.exists():
            print(f"  [skip] {fp} not found")
            continue
        with open(path) as f:
            items = json.load(f)
        for item in items:
            eid = item.get("event_id", "")
            if eid and eid not in seen:
                seen.add(eid)
                events.append(item)

    return events


def main() -> None:
    dry_run = "--dry-run" in sys.argv

    events = load_events()
    print(f"Loaded {len(events)} unique FP events")

    if dry_run:
        for ev in events:
            print(f"  {ev['event_id'][:8]}... {ev.get('file_name', '?')}")
        print("\n[dry-run] No events enqueued.")
        return

    r = redis.from_url(REDIS_URL, decode_responses=True)
    r.ping()

    enqueued = 0
    for ev in events:
        original_id = ev["event_id"]
        new_id = f"fp-retest-{uuid.uuid4()}"
        ev["event_id"] = new_id
        ev["original_event_id"] = original_id  # keep reference
        r.rpush(QUEUE_KEY, json.dumps(ev))
        print(f"  enqueued {new_id[:24]}... <- {ev.get('file_name', '?')}")
        enqueued += 1

    print(f"\nEnqueued {enqueued} events for reprocessing.")


if __name__ == "__main__":
    main()
