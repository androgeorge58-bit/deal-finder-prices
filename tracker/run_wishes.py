#!/usr/bin/env python3
"""
run_wishes.py — GitHub Actions entrypoint. Add one step to your daily workflow:

    - name: Run wish watch
      env:
        SCRAPERAPI_KEY: ${{ secrets.SCRAPERAPI_KEY }}
        ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}   # optional
      run: python run_wishes.py

Outputs (git-committed by your existing commit step):
    data/wish_results.json   -> the app reads this to render each wish
    data/notifications.json  -> queue for notify.py / OneSignal to push

Handing off to notifications: this only WRITES the queue. Your notify.py picks
it up and sends via OneSignal — same push path you already built. That keeps
one sender, not two.
"""
from __future__ import annotations
import json
import os
from dataclasses import asdict

from wishwatch import Store, Fetcher
from wishwatch.engine import run_cycle

DATA_DIR = os.getenv("WW_DATA_DIR", "data")


def _llm_client():
    if not os.getenv("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
        return anthropic.Anthropic()
    except Exception:
        return None


def main():
    store = Store(DATA_DIR)
    fetcher = Fetcher()  # reads SCRAPERAPI_KEY from env
    notifications, results = run_cycle(store, fetcher, llm_client=_llm_client())

    with open(os.path.join(DATA_DIR, "wish_results.json"), "w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in results], f, ensure_ascii=False, indent=2)

    # append to the notifications queue rather than clobbering unsent ones
    q_path = os.path.join(DATA_DIR, "notifications.json")
    queue = []
    if os.path.exists(q_path):
        with open(q_path, encoding="utf-8") as f:
            queue = json.load(f)
    queue.extend(asdict(n) for n in notifications)
    with open(q_path, "w", encoding="utf-8") as f:
        json.dump(queue, f, ensure_ascii=False, indent=2)

    print(f"wishes checked -> {len(results)} results, "
          f"{len(notifications)} new notifications")
    for n in notifications:
        arrow = "NEW" if n.kind == "found" else f"was {n.prev_price}"
        print(f"  [{n.kind}] {n.query}: {n.price} @ {n.supplier} ({arrow})")


if __name__ == "__main__":
    main()
