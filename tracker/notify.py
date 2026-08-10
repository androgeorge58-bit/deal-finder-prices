"""
Deal Finder push sender.

Runs right after verdict.py inside the same GitHub Action. Reads
data/latest.json, finds the deals that newly turned jade on this run, and
pushes each one ONLY to the people whose interests match it.

Targeting works without a database. When someone picks their interests in the
app, the app writes them to OneSignal as tags:

    cat_electronics = 1
    cat_supermarket = 1
    wish_iphone17    = 1

This script then asks OneSignal to deliver to "everyone tagged cat_electronics",
so a fashion shopper never gets pinged about a TV. No user list, no server,
no personal data leaving the phone.

Needs two GitHub secrets:
    ONESIGNAL_APP_ID     the app id from your OneSignal dashboard
    ONESIGNAL_API_KEY    the REST API key from the same page

If either is missing the script prints a note and exits cleanly -- price
tracking keeps working, you just don't get pushes yet.
"""

import json
import os
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
LATEST = ROOT / "data" / "latest.json"
SENT_LOG = ROOT / "data" / "pushed.json"

API = "https://api.onesignal.com/notifications"
APP_ID = os.environ.get("ONESIGNAL_APP_ID", "").strip()
API_KEY = os.environ.get("ONESIGNAL_API_KEY", "").strip()

# Don't wake people up for a 6% drop. Only genuinely good news gets a push.
PUSH_VERDICTS = ("lowest", "real")
MAX_PER_RUN = 5          # never fire more than this in one go, however many drop


def load_sent():
    if not SENT_LOG.exists():
        return {}
    try:
        return json.loads(SENT_LOG.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_sent(d):
    SENT_LOG.write_text(json.dumps(d, indent=1), encoding="utf-8")


def audience(deal):
    """
    Who should hear about this? Anyone who picked its category, OR anyone whose
    typed wish appears in the product name. OneSignal ORs these together.
    """
    filters = [{"field": "tag", "key": f"cat_{deal['category']}", "relation": "=", "value": "1"}]
    name = f"{deal.get('name_en','')} {deal.get('name_ar','')}".lower()
    for token in set(name.replace("-", " ").split()):
        token = "".join(c for c in token if c.isalnum())
        if len(token) >= 4:
            filters.append({"operator": "OR"})
            filters.append({"field": "tag", "key": f"wish_{token}", "relation": "=", "value": "1"})
    return filters


def send(deal):
    payload = {
        "app_id": APP_ID,
        "filters": audience(deal),
        "headings": {
            "en": "Real deal found",
            "ar": "لقينا خصم حقيقي",
        },
        "contents": {
            "en": f"{deal['name_en']} — {deal['headline_en']}",
            "ar": f"{deal['name_ar']} — {deal['headline_ar']}",
        },
        "url": deal.get("affiliate_url") or deal.get("url") or "",
        "web_push_topic": deal["sku_id"],   # a newer alert replaces the older one
    }
    r = requests.post(
        API,
        headers={"Authorization": f"Key {API_KEY}", "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )
    ok = r.status_code < 300
    try:
        body = r.json()
    except ValueError:
        body = {"raw": r.text[:200]}
    return ok, body


def main():
    if not APP_ID or not API_KEY:
        print("Push not configured (ONESIGNAL_APP_ID / ONESIGNAL_API_KEY missing).")
        print("Prices are still being tracked -- this step is optional until you set it up.")
        return 0

    if not LATEST.exists():
        print("No latest.json yet, nothing to push.")
        return 0

    data = json.loads(LATEST.read_text(encoding="utf-8"))
    sent = load_sent()

    # Push on any move INTO a jade verdict, or a fresh drop below the last price
    # we pushed. That is what makes several drops over six months notify several
    # times, instead of only the first one.
    queue = []
    for d in data.get("deals", []):
        if d["verdict"] not in PUSH_VERDICTS:
            continue
        prev = sent.get(d["sku_id"])
        if prev is None:
            queue.append(d)
        elif d["current_price"] < prev.get("price", float("inf")):
            queue.append(d)
        elif prev.get("verdict") not in PUSH_VERDICTS:
            queue.append(d)

    if not queue:
        print("No new deals worth a push this run.")
        return 0

    queue.sort(key=lambda d: -d["delta_pct"])
    queue = queue[:MAX_PER_RUN]

    for d in queue:
        ok, body = send(d)
        if ok:
            recipients = body.get("recipients", "?")
            print(f"pushed {d['sku_id']:<28} {d['verdict']:<7} -> {recipients} people")
            sent[d["sku_id"]] = {"verdict": d["verdict"], "price": d["current_price"]}
        else:
            print(f"FAILED {d['sku_id']}: {body}", file=sys.stderr)

    # remember current state for everything, so a recovery then re-drop re-fires
    for d in data.get("deals", []):
        if d["sku_id"] in sent and d["verdict"] not in PUSH_VERDICTS:
            sent[d["sku_id"]]["verdict"] = d["verdict"]

    save_sent(sent)
    return 0


if __name__ == "__main__":
    sys.exit(main())
