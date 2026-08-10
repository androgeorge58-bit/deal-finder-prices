"""
Deal Finder verdict engine.

Reads data/history.jsonl, computes a verdict per SKU, writes data/latest.json.
That file is the entire API your app talks to -- served free and fast from
raw.githubusercontent.com. No server, no database, no monthly bill.

The one rule that keeps you honest: a SKU with fewer than MIN_DAYS of history
gets the verdict "learning", never a jade stamp. You cannot call a price the
lowest ever when you have watched it for four days. Showing a green badge on
thin data is exactly the lie this product exists to catch.

Run:  python tracker/verdict.py
"""

import csv
import json
import statistics
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HISTORY = ROOT / "data" / "history.jsonl"
PRODUCTS = ROOT / "tracker" / "products.csv"
OUTPUT = ROOT / "data" / "latest.json"

# Verdict thresholds
# Two different knobs that are easy to confuse:
#   MIN_DAYS        how long before ANY verdict is shown. Raise this and the app
#                   shows nothing but grey stamps until the clock runs out.
#   BASELINE_WINDOW how much price memory forms the "usual" price. Raise this and
#                   verdicts get SMARTER, with no blackout period.
# For "6 months of price history", BASELINE_WINDOW is the one you want.
MIN_DAYS = 14           # below this, no verdict is trustworthy
BASELINE_WINDOW = 180   # six months of price memory
REAL_DEAL_PCT = 15.0    # >= this much below baseline -> real deal
SMALL_DROP_PCT = 5.0    # >= this much below baseline -> small drop
RAISE_LOOKBACK = 60     # days to search for a quiet pre-discount price hike
RAISE_TRIGGER_PCT = 5.0 # a hike of at least this much counts as suspicious
CHART_DAYS = 365        # a full year of chart, once you have it


def load_products():
    meta = {}
    with PRODUCTS.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            sku = row.get("sku_id", "").strip()
            if sku:
                meta[sku] = {k: (v or "").strip() for k, v in row.items()}
    return meta


def load_history():
    series = defaultdict(dict)  # sku -> {date: [prices]}
    if not HISTORY.exists():
        return series
    with HISTORY.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("price") is None or rec.get("status") != "ok":
                continue
            series[rec["sku_id"]].setdefault(rec["date"], []).append(rec["price"])
    return series


def daily_points(by_date):
    """One price per day (median of that day's captures), oldest first."""
    return [
        {"date": d, "price": round(statistics.median(prices), 2)}
        for d, prices in sorted(by_date.items())
    ]


def detect_quiet_raise(points):
    """
    The Sony pattern: price is quietly raised, then 'discounted' off the
    inflated number. Returns the raise details if the current price is still
    above what the item cost before the hike.
    """
    if len(points) < 10:
        return None
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RAISE_LOOKBACK)).strftime("%Y-%m-%d")
    window = [p for p in points if p["date"] >= cutoff]
    if len(window) < 10:
        return None

    current = points[-1]["price"]
    best_gap, result = 0.0, None

    for i in range(1, len(window)):
        before = window[i - 1]["price"]
        after = window[i]["price"]
        if before <= 0:
            continue
        jump_pct = (after - before) / before * 100
        # a real hike, and the "discounted" price never came back below it
        if jump_pct >= RAISE_TRIGGER_PCT and current > before and jump_pct > best_gap:
            best_gap = jump_pct
            result = {
                "raised_on": window[i]["date"],
                "price_before": before,
                "price_after": after,
                "raise_pct": round(jump_pct, 1),
                "still_above_pre_raise_pct": round((current - before) / before * 100, 1),
            }
    return result


def build(sku, by_date, meta):
    points = daily_points(by_date)
    if not points:
        return None

    days_tracked = len(points)
    current = points[-1]["price"]
    lowest = min(p["price"] for p in points)
    highest = max(p["price"] for p in points)

    cutoff = (datetime.now(timezone.utc) - timedelta(days=BASELINE_WINDOW)).strftime("%Y-%m-%d")
    window = [p["price"] for p in points if p["date"] >= cutoff][:-1] or [current]
    baseline = round(statistics.mean(window), 2)

    delta_pct = round((baseline - current) / baseline * 100, 1) if baseline else 0.0
    quiet_raise = detect_quiet_raise(points)

    if days_tracked < MIN_DAYS:
        verdict, tone = "learning", "neutral"
        headline_en = f"Still learning this price ({days_tracked}/{MIN_DAYS} days)"
        headline_ar = f"لسه بنتابع السعر ({days_tracked}/{MIN_DAYS} يوم)"
    elif current <= lowest:
        verdict, tone = "lowest", "jade"
        headline_en = "Lowest price we've ever recorded"
        headline_ar = "أقل سعر سجلناه على الإطلاق"
    elif delta_pct >= REAL_DEAL_PCT:
        verdict, tone = "real", "jade"
        headline_en = f"Real deal — {delta_pct}% below its usual price"
        headline_ar = f"خصم حقيقي — أقل بـ {delta_pct}% من سعره المعتاد"
    elif delta_pct >= SMALL_DROP_PCT:
        verdict, tone = "small", "ink"
        headline_en = f"Small drop — {delta_pct}% below usual"
        headline_ar = f"انخفاض بسيط — أقل بـ {delta_pct}% من المعتاد"
    else:
        verdict, tone = "fake", "red"
        if quiet_raise:
            headline_en = f"Not a deal — price was raised {quiet_raise['raise_pct']}% on {quiet_raise['raised_on']}"
            headline_ar = f"مش خصم — السعر اتزود {quiet_raise['raise_pct']}% يوم {quiet_raise['raised_on']}"
        else:
            headline_en = "Not a deal — this is its normal price"
            headline_ar = "مش خصم — ده سعره الطبيعي"

    info = meta.get(sku, {})
    return {
        "sku_id": sku,
        "name_en": info.get("name_en") or sku,
        "name_ar": info.get("name_ar") or info.get("name_en") or sku,
        "category": info.get("category", "other"),
        "retailer": info.get("retailer", ""),
        "url": info.get("url", ""),
        "affiliate_url": info.get("affiliate_url", ""),
        "image": info.get("image", ""),
        "current_price": current,
        "baseline_price": baseline,
        "lowest_price": lowest,
        "highest_price": highest,
        "delta_pct": delta_pct,
        "days_tracked": days_tracked,
        "verdict": verdict,
        "tone": tone,
        "headline_en": headline_en,
        "headline_ar": headline_ar,
        "quiet_raise": quiet_raise,
        "last_seen": points[-1]["date"],
        "history": points[-CHART_DAYS:],
    }


def previous_verdicts():
    """What each SKU's verdict was on the last run -- used to spot new deals."""
    if not OUTPUT.exists():
        return {}
    try:
        old = json.loads(OUTPUT.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return {d["sku_id"]: d.get("verdict") for d in old.get("deals", [])}


def main():
    meta = load_products()
    series = load_history()
    before = previous_verdicts()

    deals = [d for sku, by_date in series.items() if (d := build(sku, by_date, meta))]

    # A deal is "new" the first run it crosses into jade. This is the trigger
    # the app watches to notify people who have the item on their wishlist --
    # notify on the transition, never on every run, or you become spam.
    for d in deals:
        was = before.get(d["sku_id"])
        d["is_new"] = d["verdict"] in ("lowest", "real") and was not in ("lowest", "real")

    order = {"lowest": 0, "real": 1, "small": 2, "learning": 3, "fake": 4}
    deals.sort(key=lambda d: (order.get(d["verdict"], 9), -d["delta_pct"]))

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "schema_version": 2,
        "thresholds": {
            "min_days": MIN_DAYS,
            "real_deal_pct": REAL_DEAL_PCT,
            "small_drop_pct": SMALL_DROP_PCT,
        },
        "counts": {
            v: sum(1 for d in deals if d["verdict"] == v)
            for v in ("lowest", "real", "small", "learning", "fake")
        },
        "new_today": [d["sku_id"] for d in deals if d["is_new"]],
        "deals": deals,
    }

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    c = payload["counts"]
    print(f"Wrote {OUTPUT.relative_to(ROOT)} — {len(deals)} products")
    print(f"  lowest {c['lowest']} | real {c['real']} | small {c['small']} | learning {c['learning']} | not-a-deal {c['fake']}")
    if payload["new_today"]:
        print(f"  NEW deals this run: {', '.join(payload['new_today'])}")
    if c["learning"] == len(deals) and deals:
        print(f"  (everything is still learning -- normal until day {MIN_DAYS})")


if __name__ == "__main__":
    main()
