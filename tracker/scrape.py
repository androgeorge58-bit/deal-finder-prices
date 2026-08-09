"""
Deal Finder price recorder.

Reads tracker/products.csv, fetches each product page, extracts the current
price, and appends one row per product to data/history.jsonl.

Extraction is layered: it tries four independent strategies per page and takes
the first one that returns a plausible number. This is deliberate -- e-commerce
sites change their HTML constantly, and a single CSS selector will break within
weeks. Whichever strategy succeeded is recorded in the log so you can see which
ones are still working.

Run locally:   python tracker/scrape.py
Run one SKU:   python tracker/scrape.py --only sony-wh1000xm5
Debug a page:  python tracker/scrape.py --only sony-wh1000xm5 --debug
"""

import argparse
import csv
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

# ---- fetch routes (inlined so there is no second file to install) ----

TIMEOUT = 20          # a hung request costs 20s, not 25s x 3 = 75s
ATTEMPTS = 2          # total failure across 12 products: ~3 min, not 19

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
    "Connection": "keep-alive",
}


# --------------------------------------------------------------------------
# individual routes -- each returns page text or raises
# --------------------------------------------------------------------------

def _direct(session, url):
    r = session.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text


def _reader(session, url):
    # r.jina.ai returns the rendered page as clean text, prices included.
    r = session.get(
        "https://r.jina.ai/" + url,
        headers={"User-Agent": HEADERS["User-Agent"], "Accept": "text/plain"},
        timeout=TIMEOUT + 20,   # rendering takes longer than a raw fetch
    )
    r.raise_for_status()
    return r.text


def _scraperapi(session, url):
    key = os.environ.get("SCRAPER_API_KEY", "").strip()
    if not key:
        raise RuntimeError("no_api_key")
    r = session.get(
        "https://api.scraperapi.com/",
        params={"api_key": key, "url": url, "country_code": "eg"},
        timeout=TIMEOUT + 40,
    )
    r.raise_for_status()
    return r.text


def _allorigins(session, url):
    r = session.get(
        "https://api.allorigins.win/raw?url=" + quote(url, safe=""),
        headers={"User-Agent": HEADERS["User-Agent"]},
        timeout=TIMEOUT + 20,
    )
    r.raise_for_status()
    return r.text


ROUTES = [
    ("direct", _direct),
    ("reader", _reader),
    ("scraperapi", _scraperapi),
    ("allorigins", _allorigins),
]


# --------------------------------------------------------------------------
# probing
# --------------------------------------------------------------------------

def looks_like_a_product_page(text):
    """Cheap sanity check -- did we get the page, or a block page?"""
    if not text or len(text) < 500:
        return False
    low = text.lower()
    if "captcha" in low or "access denied" in low or "are you a robot" in low:
        return False
    return any(m in low for m in ("egp", "ج.م", "__next_data__", "application/ld+json"))


def choose_route(session, probe_url, log=print):
    """
    Try each route against one real product page. Return (name, fn) for the
    first that comes back with something usable, or (None, None).
    """
    log("Probing routes to noon...")
    for name, fn in ROUTES:
        started = time.time()
        try:
            text = fn(session, probe_url)
        except Exception as exc:
            reason = type(exc).__name__
            if str(exc) == "no_api_key":
                reason = "skipped (no SCRAPER_API_KEY set)"
            log(f"  {name:<12} {reason}")
            continue
        took = time.time() - started
        if looks_like_a_product_page(text):
            log(f"  {name:<12} OK  ({took:.1f}s, {len(text):,} chars)  <-- using this")
            return name, fn
        log(f"  {name:<12} responded but no price content ({len(text):,} chars)")
    log("  no route reached noon")
    return None, None


def fetch_with(fn, session, url):
    """Run one route with a couple of retries. Returns (text, error)."""
    last = None
    for i in range(ATTEMPTS):
        try:
            return fn(session, url), None
        except requests.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else "?"
            last = f"http_{code}"
            if code in (403, 429):
                time.sleep(4 * (i + 1) + random.uniform(0, 2))
                continue
        except Exception as exc:
            last = type(exc).__name__
        time.sleep(1.5 * (i + 1) + random.uniform(0, 1))
    return None, last or "unknown"


ROOT = Path(__file__).resolve().parent.parent
PRODUCTS = ROOT / "tracker" / "products.csv"
HISTORY = ROOT / "data" / "history.jsonl"
DEBUG_DIR = ROOT / "data" / "debug"

# A price below this is almost certainly a shipping fee, a monthly instalment,
# or a "starting from" teaser -- not the product price.
MIN_PLAUSIBLE_EGP = 20
MAX_PLAUSIBLE_EGP = 2_000_000

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


# --------------------------------------------------------------------------
# price parsing helpers
# --------------------------------------------------------------------------

def to_number(raw):
    """Turn '19,955.00 EGP' or 'ج.م ١٩٩٥٥' into a float, or None."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        val = float(raw)
        return val if MIN_PLAUSIBLE_EGP <= val <= MAX_PLAUSIBLE_EGP else None

    text = str(raw)
    # Arabic-Indic digits -> ASCII
    text = text.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
    text = text.replace("\u066b", ".").replace("\u066c", ",")
    # Grab the longest run that starts and ends with a digit. Stripping
    # characters instead would let the dot in "ج.م" glue itself to the number.
    runs = re.findall(r"\d[\d.,]*\d|\d", text)
    if not runs:
        return None
    text = max(runs, key=len)

    # Decide which separator is decimal. If both appear, the last one wins.
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        # 19,955 -> thousands ; 19,95 -> decimal
        parts = text.split(",")
        if len(parts[-1]) == 3:
            text = text.replace(",", "")
        else:
            text = text.replace(",", ".")

    try:
        val = float(text)
    except ValueError:
        return None
    return val if MIN_PLAUSIBLE_EGP <= val <= MAX_PLAUSIBLE_EGP else None


# --------------------------------------------------------------------------
# extraction strategies -- each returns a float or None
# --------------------------------------------------------------------------

def from_jsonld(soup, html):
    """schema.org Product markup. The most stable source when present."""
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            blob = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for node in blob if isinstance(blob, list) else [blob]:
            if not isinstance(node, dict):
                continue
            graph = node.get("@graph")
            candidates = graph if isinstance(graph, list) else [node]
            for item in candidates:
                if not isinstance(item, dict):
                    continue
                offers = item.get("offers")
                if not offers:
                    continue
                for offer in offers if isinstance(offers, list) else [offers]:
                    if not isinstance(offer, dict):
                        continue
                    price = to_number(offer.get("price") or offer.get("lowPrice"))
                    if price:
                        return price
    return None


def from_next_data(soup, html):
    """noon and many Next.js shops embed state in __NEXT_DATA__."""
    tag = soup.find("script", id="__NEXT_DATA__")
    if not tag or not tag.string:
        return None
    try:
        blob = json.loads(tag.string)
    except json.JSONDecodeError:
        return None

    hits = []
    keys = {"sale_price", "salePrice", "price", "offer_price", "final_price"}

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k in keys and isinstance(v, (int, float, str)):
                    n = to_number(v)
                    if n:
                        hits.append((k, n))
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(blob)
    if not hits:
        return None
    # prefer an explicit sale/final price over a generic "price"
    for preferred in ("sale_price", "salePrice", "final_price", "offer_price"):
        for k, n in hits:
            if k == preferred:
                return n
    return hits[0][1]


def from_meta(soup, html):
    """Open Graph / product meta tags."""
    selectors = [
        ("meta", {"property": "product:price:amount"}),
        ("meta", {"property": "og:price:amount"}),
        ("meta", {"itemprop": "price"}),
        ("meta", {"name": "twitter:data1"}),
    ]
    for name, attrs in selectors:
        tag = soup.find(name, attrs=attrs)
        if tag:
            price = to_number(tag.get("content"))
            if price:
                return price
    return None


def from_visible_text(soup, html):
    """Last resort: currency-marked numbers in the rendered text."""
    text = soup.get_text(" ", strip=True)
    patterns = [
        r"(?:EGP|ج\.?م\.?|جنيه)\s*([\d\u0660-\u0669.,]+)",
        r"([\d\u0660-\u0669.,]+)\s*(?:EGP|ج\.?م\.?|جنيه)",
    ]
    found = []
    for pat in patterns:
        for m in re.finditer(pat, text):
            price = to_number(m.group(1))
            if price:
                found.append(price)
    # The page shows old price, new price, instalments. The product price is
    # rarely the smallest; take the median of what we found.
    if not found:
        return None
    found.sort()
    return found[len(found) // 2]


STRATEGIES = [
    ("jsonld", from_jsonld),
    ("next_data", from_next_data),
    ("meta", from_meta),
    ("visible_text", from_visible_text),
]


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------

def scrape_one(session, route_fn, row, debug=False):
    url = row["url"].strip()
    html, error = fetch_with(route_fn, session, url)

    record = {
        "sku_id": row["sku_id"].strip(),
        "retailer": row["retailer"].strip().lower(),
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "price": None,
        "currency": "EGP",
        "method": None,
        "status": "error",
        "note": error,
    }

    if html is None:
        return record

    if debug:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        (DEBUG_DIR / f"{record['sku_id']}.html").write_text(html, encoding="utf-8")

    soup = BeautifulSoup(html, "html.parser")

    # In-stock detection saves you from recording a stale price on a dead page.
    lowered = html.lower()
    out_of_stock = any(
        marker in lowered
        for marker in ("outofstock", "out of stock", "sold out", "غير متوفر", "نفذت الكمية")
    )

    for name, fn in STRATEGIES:
        try:
            price = fn(soup, html)
        except Exception as exc:  # a broken page must not kill the whole run
            if debug:
                print(f"    strategy {name} raised {type(exc).__name__}: {exc}")
            continue
        if price:
            record.update(
                price=round(price, 2),
                method=name,
                status="out_of_stock" if out_of_stock else "ok",
                note=None,
            )
            return record

    record["note"] = "no_price_found"
    return record


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def load_products():
    if not PRODUCTS.exists():
        sys.exit(f"Missing {PRODUCTS}. Add your product list first.")
    with PRODUCTS.open(encoding="utf-8-sig", newline="") as fh:
        rows = [r for r in csv.DictReader(fh) if r.get("url", "").strip()]
    if not rows:
        sys.exit("products.csv has no rows with a url. Nothing to track.")
    return rows


def main():
    print('Deal Finder price recorder starting', flush=True)
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", help="scrape a single sku_id")
    parser.add_argument("--debug", action="store_true", help="save fetched HTML to data/debug/")
    parser.add_argument("--dry-run", action="store_true", help="do not write history")
    args = parser.parse_args()

    rows = load_products()
    if args.only:
        rows = [r for r in rows if r["sku_id"].strip() == args.only]
        if not rows:
            sys.exit(f"No product with sku_id '{args.only}'")

    session = requests.Session()

    route_name, route_fn = choose_route(session, rows[0]["url"].strip())
    if route_fn is None:
        print(
            "\nNo route reached noon. Nothing was recorded.\n"
            "Fix: add a SCRAPER_API_KEY secret in GitHub\n"
            "  (repo Settings > Secrets and variables > Actions > New repository secret).\n"
            "Get a free key at scraperapi.com -- the free tier covers this workload.",
            file=sys.stderr,
        )
        return 1
    print()

    records, ok, failed = [], 0, 0

    for i, row in enumerate(rows, 1):
        rec = scrape_one(session, route_fn, row, debug=args.debug)
        rec["route"] = route_name
        records.append(rec)
        if rec["status"] == "ok":
            ok += 1
            flag = "ok"
        else:
            failed += 1
            flag = rec["status"] + (f" ({rec['note']})" if rec["note"] else "")
        print(f"[{i:>3}/{len(rows)}] {rec['sku_id']:<28} {str(rec['price'] or '-'):>12}  {rec['method'] or '-':<12} {flag}")
        if i < len(rows):
            time.sleep(random.uniform(1.5, 3.5))  # be a polite visitor

    print(f"\nCaptured {ok} / {len(rows)} via {route_name}  ({failed} failed)")

    if args.dry_run:
        print("dry run -- nothing written")
        return 0

    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY.open("a", encoding="utf-8") as fh:
        for rec in records:
            if rec["price"] is not None:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # A run that captures almost nothing usually means you got blocked.
    # Fail loudly so the GitHub Actions email tells you.
    if ok == 0:
        print(f"ERROR: route '{route_name}' probed OK but captured nothing.", file=sys.stderr)
        return 1
    if failed > ok:
        print("WARNING: more failures than successes.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
