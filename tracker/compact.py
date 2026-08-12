"""
Keep history.jsonl from outgrowing GitHub.

At 1,200 products checked once a day, the raw log adds about 84 MB a year.
GitHub refuses files over 100 MB, so left alone this quietly breaks the whole
pipeline in under a year -- and it breaks at the worst moment, once you have
a year of irreplaceable price history in it.

Two safe reductions, neither of which changes a single verdict:

  1. One row per product per day. Extra rows from a re-run are the same day's
     price recorded twice; we keep their median.
  2. Drop days older than KEEP_DAYS. The chart shows a year and the baseline
     looks back six months, so older rows are dead weight.

Runs after every harvest. Prints what it saved.
"""

import json
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HISTORY = ROOT / "data" / "history.jsonl"

KEEP_DAYS = 400          # a year of chart plus a safety margin
WARN_MB = 60             # shout well before GitHub's 100 MB wall


def main():
    if not HISTORY.exists():
        print("No history file yet.")
        return 0

    before_bytes = HISTORY.stat().st_size
    cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")

    # (sku, date) -> prices seen that day
    buckets = defaultdict(list)
    meta = {}
    read = dropped_old = malformed = 0

    with HISTORY.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            read += 1
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            if rec.get("price") is None or rec.get("status") != "ok":
                malformed += 1
                continue
            if rec.get("date", "") < cutoff:
                dropped_old += 1
                continue
            key = (rec["sku_id"], rec["date"])
            buckets[key].append(rec["price"])
            meta[key] = rec

    if not buckets:
        print("Nothing left after compaction -- refusing to write an empty file.",
              file=sys.stderr)
        return 1

    rows = []
    for key in sorted(buckets):
        rec = meta[key]
        rows.append({
            "sku_id": rec["sku_id"],
            "retailer": rec.get("retailer", ""),
            "captured_at": rec.get("captured_at", ""),
            "date": rec["date"],
            "price": round(statistics.median(buckets[key]), 2),
            "currency": "EGP",
            "method": rec.get("method"),
            "status": "ok",
            "note": None,
        })

    tmp = HISTORY.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(HISTORY)

    after_bytes = HISTORY.stat().st_size
    skus = len({r["sku_id"] for r in rows})
    saved = (before_bytes - after_bytes) / 1024 / 1024

    print(f"Compacted history: {read:,} rows -> {len(rows):,}")
    print(f"  merged same-day duplicates : {read - dropped_old - malformed - len(rows):,}")
    print(f"  dropped older than {KEEP_DAYS}d    : {dropped_old:,}")
    print(f"  unusable rows discarded    : {malformed:,}")
    print(f"  size {before_bytes/1024/1024:.1f} MB -> {after_bytes/1024/1024:.1f} MB "
          f"(saved {saved:.1f} MB) across {skus:,} products")

    if after_bytes / 1024 / 1024 > WARN_MB:
        print(f"\nWARNING: history is {after_bytes/1024/1024:.0f} MB. GitHub's limit is 100 MB.\n"
              f"Lower KEEP_DAYS in tracker/compact.py, or trim sources.csv.",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
