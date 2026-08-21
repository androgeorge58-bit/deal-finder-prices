"""
Deal Finder catalogue harvester.

The old scraper fetched one product page per product: 33 requests for 33
products. This fetches CATEGORY and SEARCH pages instead. A single noon
category page already contains 40-60 products with their prices, names,
images and links -- so one request yields 40-60 products instead of one.

That is the whole trick. Same proxy budget, ~50x the catalogue.

    tracker/sources.csv     the list of pages to harvest
    data/catalog.json       every product ever seen: name, url, image, category
    data/history.jsonl      one price row per product per run (same as before)

verdict.py then runs unchanged on top of it.

Run:
    python tracker/harvest.py
    python tracker/harvest.py --only noon-electronics --debug
    python tracker/harvest.py --dry-run
"""

import argparse
import csv
import hashlib
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
SOURCES = ROOT / "tracker" / "sources.csv"
HISTORY = ROOT / "data" / "history.jsonl"
CATALOG = ROOT / "data" / "catalog.json"
DEBUG_DIR = ROOT / "data" / "debug"

MIN_PLAUSIBLE_EGP = 20
MAX_PLAUSIBLE_EGP = 5_000_000
TIMEOUT = 30

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
}


# ==========================================================================
# fetch routes  (same layered idea as scrape.py -- try until one answers)
# ==========================================================================

def _direct(session, url):
    r = session.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text


def _scraperapi(session, url, render=False):
    key = os.environ.get("SCRAPER_API_KEY", "").strip()
    if not key:
        raise RuntimeError("no_api_key")
    params = {"api_key": key, "url": url, "country_code": "eg"}
    if render:
        params["render"] = "true"
    r = session.get("https://api.scraperapi.com/", params=params, timeout=TIMEOUT + 60)
    r.raise_for_status()
    return r.text


def _scraperapi_rendered(session, url):
    # Some listing pages build their grid in the browser. render=true runs a
    # real browser on ScraperAPI's side. Costs more credits, so it is a
    # fallback, never the first choice.
    return _scraperapi(session, url, render=True)


def _reader(session, url):
    r = session.get("https://r.jina.ai/" + url,
                    headers={"User-Agent": HEADERS["User-Agent"]}, timeout=TIMEOUT + 30)
    r.raise_for_status()
    return r.text


ROUTES = [
    ("direct", _direct),
    ("scraperapi", _scraperapi),
    ("scraperapi_js", _scraperapi_rendered),
    ("reader", _reader),
]


def usable(text):
    if not text or len(text) < 800:
        return False
    low = text.lower()
    if "captcha" in low or "access denied" in low or "are you a robot" in low:
        return False
    return True


def choose_route(session, probe_url, log=print):
    log("Probing routes...")
    for name, fn in ROUTES:
        t0 = time.time()
        try:
            text = fn(session, probe_url)
        except Exception as exc:
            reason = "skipped (no SCRAPER_API_KEY)" if str(exc) == "no_api_key" else type(exc).__name__
            log(f"  {name:<15} {reason}")
            continue
        if usable(text):
            log(f"  {name:<15} OK ({time.time()-t0:.1f}s, {len(text):,} chars)  <-- using this")
            return name, fn
        log(f"  {name:<15} responded but looked empty ({len(text):,} chars)")
    return None, None


# ==========================================================================
# helpers
# ==========================================================================

def to_number(raw):
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        v = float(raw)
        return v if MIN_PLAUSIBLE_EGP <= v <= MAX_PLAUSIBLE_EGP else None
    text = str(raw).translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
    text = text.replace("\u066b", ".").replace("\u066c", ",")
    # kill per-unit fragments so "EGP 27.78/kg" doesn't become a candidate
    text = re.sub(r"\d[\d.,]*\s*(?:/|per\s+)\s*(?:kg|g|ml|l|piece|pc|unit|item|lb|oz)\b",
                  " ", text, flags=re.I)
    runs = re.findall(r"\d[\d.,]*\d|\d", text)
    if not runs:
        return None
    def _norm(t):
        if "," in t and "." in t:
            if t.rfind(",") > t.rfind("."):
                t = t.replace(".", "").replace(",", ".")
            else:
                t = t.replace(",", "")
        elif "," in t:
            parts = t.split(",")
            t = t.replace(",", "") if len(parts[-1]) == 3 else t.replace(",", ".")
        return t
    # parse every candidate; keep only plausible prices, then take the LARGEST.
    # rationale: on a retail card the main sale/list price is the biggest number
    # in the plausible EGP range. Per-unit prices, ratings, review counts are smaller
    # or out of range. If a bigger 'was' price is also present, we discover it
    # elsewhere via a dedicated selector.
    vals = []
    for r in runs:
        try:
            v = float(_norm(r))
        except ValueError:
            continue
        if MIN_PLAUSIBLE_EGP <= v <= MAX_PLAUSIBLE_EGP:
            vals.append(v)
    return max(vals) if vals else None


def slugify(text, fallback="item"):
    text = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    return text[:44] or fallback


def sku_from_url(url, retailer, name=""):
    """
    A stable id that survives the retailer renaming the product. Prefer the
    retailer's own product code out of the URL; fall back to a hash so the
    same product always lands on the same row of history.
    """
    m = re.search(r"/([A-Z0-9]{8,})/p/", url or "", re.I)          # noon
    if m:
        return f"{retailer}-{m.group(1).lower()}"
    m = re.search(r"/dp/([A-Z0-9]{10})", url or "", re.I)           # amazon
    if m:
        return f"{retailer}-{m.group(1).lower()}"
    m = re.search(r"-(\d{6,})\.html", url or "")                    # jumia
    if m:
        return f"{retailer}-{m.group(1)}"
    basis = (url or name or "").split("?")[0]
    if not basis:
        return None
    return f"{retailer}-{hashlib.sha1(basis.encode()).hexdigest()[:12]}"


def looks_like_product(node):
    """A dict from embedded JSON that plausibly describes a sellable item."""
    if not isinstance(node, dict):
        return False
    keys = {k.lower() for k in node.keys()}
    has_name = bool(keys & {"name", "title", "product_title", "displayname"})
    has_price = bool(keys & {"price", "sale_price", "saleprice", "offer_price",
                             "final_price", "finalprice", "amount"})
    has_id = bool(keys & {"sku", "id", "url", "productid", "product_id", "asin", "code"})
    return has_name and has_price and has_id


def resolve_image(node, retailer, base_url=""):
    """
    Find a usable photo URL inside a product record.

    The listing JSON sometimes holds a full URL, sometimes only a CDN key like
    "v1683266334/N53393168A_1". Guessing the key format is how the first
    version ended up with no photos at all, so: take any real URL we can see
    first, and only build one from a key as a last resort.
    """
    def clean(u):
        u = str(u).strip()
        if u.startswith("//"):
            u = "https:" + u
        if u.startswith("http") and len(u) > 12:
            return u.split()[0]
        return ""

    # 1. an explicit image field that already holds a URL
    for key in ("image", "image_url", "imageUrl", "thumbnail", "thumb",
                "main_image", "mainImage", "images", "media"):
        for k, v in node.items():
            if k.lower() != key.lower():
                continue
            if isinstance(v, list) and v:
                v = v[0]
            if isinstance(v, dict):
                v = v.get("url") or v.get("src") or v.get("image_key") or ""
            got = clean(v)
            if got:
                return got

    # 2. any string anywhere in the record that looks like an image URL
    for v in node.values():
        if isinstance(v, str) and v.startswith("http") and re.search(r"\.(jpe?g|png|webp)", v, re.I):
            return clean(v)

    # 3. last resort: build one from a CDN key
    for k, v in node.items():
        if "image" in k.lower() and isinstance(v, str) and v and not v.startswith("http"):
            key = v.strip("/")
            if retailer == "noon":
                if not re.search(r"\.(jpe?g|png|webp)$", key, re.I):
                    key += ".jpg"
                return f"https://f.nooncdn.com/p/{key}"
    return ""


def pick(node, names):
    for n in names:
        for k, v in node.items():
            if k.lower() == n and v not in (None, "", []):
                return v
    return None


# ==========================================================================
# extraction strategies
# ==========================================================================

def from_embedded_json(soup, html, base_url, retailer):
    """
    noon is a Next.js site: the whole product grid sits in __NEXT_DATA__ as
    JSON. Reading that is far more stable than CSS selectors, which the site
    rewrites constantly. Also catches any other site that embeds its listing.
    """
    out = []
    scripts = soup.find_all("script", id="__NEXT_DATA__")
    scripts += soup.find_all("script", type="application/json")
    for tag in scripts:
        try:
            blob = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue

        found = []

        def walk(node):
            if isinstance(node, dict):
                if looks_like_product(node):
                    found.append(node)
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(blob)

        for p in found:
            price = to_number(pick(p, ["sale_price", "saleprice", "final_price",
                                       "finalprice", "offer_price", "price", "amount"]))
            if not price:
                continue
            name = pick(p, ["name", "title", "product_title", "displayname"])
            url = pick(p, ["url", "link", "product_url"])
            sku = pick(p, ["sku", "productid", "product_id", "id", "code", "asin"])
            if url and not str(url).startswith("http"):
                url = urljoin(base_url, str(url))
            if not url and sku:
                url = f"https://www.noon.com/egypt-en/{slugify(name)}/{sku}/p/"
            image = resolve_image(p, retailer, base_url)
            was = to_number(pick(p, ["was_price", "wasprice", "original_price",
                                     "list_price", "regular_price", "price"]))
            out.append({
                "name": str(name)[:160] if name else "",
                "price": price,
                "was": was if was and was > price else None,
                "url": str(url) if url else "",
                "image": str(image) if image else "",
            })
        if out:
            return out
    return out


def from_jsonld_list(soup, html, base_url, retailer):
    """schema.org ItemList -- common on Jumia category pages."""
    out = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            blob = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for node in blob if isinstance(blob, list) else [blob]:
            if not isinstance(node, dict):
                continue
            items = node.get("itemListElement") or []
            for it in items if isinstance(items, list) else []:
                item = it.get("item") if isinstance(it, dict) and "item" in it else it
                if not isinstance(item, dict):
                    continue
                offers = item.get("offers") or {}
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                price = to_number(offers.get("price") if isinstance(offers, dict) else None)
                if not price:
                    continue
                url = item.get("url") or ""
                if url and not url.startswith("http"):
                    url = urljoin(base_url, url)
                image = item.get("image") or ""
                if isinstance(image, list) and image:
                    image = image[0]
                out.append({
                    "name": str(item.get("name", ""))[:160],
                    "price": price,
                    "was": None,
                    "url": url,
                    "image": image if isinstance(image, str) else "",
                })
    return out



def _is_real_product(item, retailer):
    """
    Reject rows that look like filter chips / brand tiles / category cards
    instead of real product listings. Real product cards:
      - have a name of at least 3 tokens (brand + word or two)
      - have a URL that points to a product detail page, not a filter/search
      - name isn't a generic label the harvester picked up from the sidebar
    """
    name = (item.get("name") or "").strip()
    url  = (item.get("url")  or "").strip().lower()
    if not name or not url:
        return False
    # generic labels the harvester wrongly captures from filter chips / sidebars
    BAD_NAMES = {
        "brand", "price", "size", "color", "colour", "category", "categories",
        "rating", "delivery", "sort", "filter", "shop", "shop by", "all",
        "featured", "sponsored", "best sellers", "new arrivals", "sale",
        "official store", "flash sale", "menu", "back to top",
    }
    if name.lower() in BAD_NAMES:
        return False
    if len(name) < 6 and " " not in name:  # single short word -> almost never a real title
        return False
    # URL must look like a product detail page (heuristics per retailer)
    if retailer == "noon":
        # noon product URLs contain "/p/" or a SKU-looking path segment
        if "/p/" not in url and not re.search(r"/[a-z0-9-]{6,}/n\d+", url):
            return False
    elif retailer == "jumia":
        # jumia product URLs end in .html (category/filter URLs don't)
        if not url.endswith(".html"):
            return False
        # and reject brand/category/filter query pages that slipped through
        if any(seg in url for seg in ("/mlp-", "/catalog", "?q=", "brand=", "category=")):
            return False
    elif retailer == "amazon":
        if "/dp/" not in url and "/gp/product" not in url:
            return False
    return True


def from_cards(soup, html, base_url, retailer):
    """
    CSS fallback. Selectors per retailer, several each -- listing markup
    changes often, so we try a few and take whichever finds the most.
    """
    selectors = {
        "noon":   ["a[class*=productBoxLink]", "[data-qa*=product]", "div[class*=productContainer] a"],
        "jumia":  ["article.prd", "article[class*=prd]", "a.core"],
        "amazon": ["div[data-asin]:not([data-asin=''])", "div.s-result-item[data-asin]"],
    }
    tries = selectors.get(retailer, []) + ["article", "li[class*=product]", "div[class*=product]"]

    best = []
    for sel in tries:
        cards = soup.select(sel)
        if len(cards) < 3:
            continue
        got = []
        for c in cards:
            # price: prefer a marked-up price element, else any currency text
            price = None
            for psel in ["span.a-offscreen", "[class*=price]", "[data-ga4-price]",
                         "div.prc", "[class*=amount]"]:
                el = c.select_one(psel)
                if el:
                    price = to_number(el.get("data-ga4-price") or el.get_text(" ", strip=True))
                    if price:
                        break
            # NOTE: no full-card-text fallback anymore. If none of the price
            # selectors above found a price, skip the card rather than guessing
            # from arbitrary text (which used to grab per-kg prices, ratings, etc).
            if not price:
                continue

            link = c if c.name == "a" else (c.select_one("a[href]") or {})
            href = link.get("href") if hasattr(link, "get") else ""
            if href and not str(href).startswith("http"):
                href = urljoin(base_url, str(href))

            name = ""
            for nsel in ["h2", "h3", "[class*=name]", "[class*=title]", "[data-ga4-item_name]"]:
                el = c.select_one(nsel)
                if el:
                    name = el.get("data-ga4-item_name") or el.get_text(" ", strip=True)
                    if name:
                        break
            if not name and hasattr(link, "get"):
                name = link.get("title") or link.get("aria-label") or ""

            image = ""
            for img in c.select("img"):
                for attr in ("src", "data-src", "data-lazy-src", "data-original", "srcset", "data-srcset"):
                    cand = (img.get(attr) or "").strip()
                    if not cand:
                        continue
                    cand = cand.split(",")[0].split()[0]
                    if cand.startswith("//"):
                        cand = "https:" + cand
                    if cand.startswith("http") and not any(
                            x in cand.lower() for x in ("logo", "sprite", "placeholder", "1x1", "blank")):
                        image = cand
                        break
                if image:
                    break

            got.append({
                "name": str(name)[:160], "price": price, "was": None,
                "url": str(href or ""), "image": image if image.startswith("http") else "",
            })
        got = [g for g in got if _is_real_product(g, retailer)]
        if len(got) > len(best):
            best = got
    return best


STRATEGIES = [
    ("embedded_json", from_embedded_json),
    ("jsonld_list", from_jsonld_list),
    ("cards", from_cards),
]


def harvest_page(html, base_url, retailer, debug=False):
    soup = BeautifulSoup(html, "html.parser")
    for name, fn in STRATEGIES:
        try:
            items = fn(soup, html, base_url, retailer)
        except Exception as exc:
            if debug:
                print(f"      strategy {name} raised {type(exc).__name__}: {exc}")
            continue
        # defence in depth: reject non-product rows regardless of which strategy found them
        items = [i for i in items if i["price"] and (i["url"] or i["name"])
                                     and _is_real_product(i, retailer)]
        if len(items) >= 3:
            return items, name
    return [], None


# ==========================================================================
# main
# ==========================================================================

def load_sources():
    if not SOURCES.exists():
        sys.exit(f"Missing {SOURCES}")
    with SOURCES.open(encoding="utf-8-sig", newline="") as fh:
        rows = [r for r in csv.DictReader(fh)
                if r.get("url", "").strip() and not r["url"].lstrip().startswith("#")]
    if not rows:
        sys.exit("sources.csv has no usable rows.")
    return rows


def load_catalog():
    if not CATALOG.exists():
        return {}
    try:
        return json.loads(CATALOG.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="harvest a single source_id")
    ap.add_argument("--debug", action="store_true", help="save fetched HTML to data/debug/")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-per-source", type=int, default=120)
    args = ap.parse_args()

    print("Deal Finder harvester starting", flush=True)
    rows = load_sources()
    if args.only:
        rows = [r for r in rows if r["source_id"].strip() == args.only]
        if not rows:
            sys.exit(f"No source with source_id '{args.only}'")

    session = requests.Session()
    route_name, route_fn = choose_route(session, rows[0]["url"].strip())
    if route_fn is None:
        print("\nNo route reached the retailer. Nothing recorded.\n"
              "Add a SCRAPER_API_KEY secret in GitHub if you have not already.",
              file=sys.stderr)
        return 1
    print()

    catalog = load_catalog()
    now = datetime.now(timezone.utc)
    stamp, day = now.isoformat(timespec="seconds"), now.strftime("%Y-%m-%d")
    price_rows, seen_now = [], set()

    for i, row in enumerate(rows, 1):
        sid = row["source_id"].strip()
        retailer = row["retailer"].strip().lower()
        category = row.get("category", "other").strip() or "other"
        url = row["url"].strip()

        try:
            html = route_fn(session, url)
        except Exception as exc:
            print(f"[{i:>2}/{len(rows)}] {sid:<26} FAILED ({type(exc).__name__})")
            continue

        if args.debug:
            DEBUG_DIR.mkdir(parents=True, exist_ok=True)
            (DEBUG_DIR / f"{sid}.html").write_text(html, encoding="utf-8")

        items, how = harvest_page(html, url, retailer, debug=args.debug)
        kept = 0
        for it in items[: args.max_per_source]:
            sku = sku_from_url(it["url"], retailer, it["name"])
            if not sku or sku in seen_now:
                continue
            seen_now.add(sku)

            entry = catalog.get(sku, {})
            catalog[sku] = {
                "name_en": it["name"] or entry.get("name_en", ""),
                "name_ar": entry.get("name_ar", ""),
                "category": category,
                "retailer": retailer,
                "url": it["url"] or entry.get("url", ""),
                "image": it["image"] or entry.get("image", ""),
                "affiliate_url": entry.get("affiliate_url", ""),
                "claimed_was": it.get("was") or None,
                "first_seen": entry.get("first_seen", day),
                "last_seen": day,
            }
            price_rows.append({
                "sku_id": sku, "retailer": retailer,
                "captured_at": stamp, "date": day,
                "price": round(it["price"], 2), "currency": "EGP",
                "method": how, "status": "ok", "note": None,
            })
            kept += 1

        print(f"[{i:>2}/{len(rows)}] {sid:<26} {kept:>4} products  via {how or '-'}")
        if i < len(rows):
            time.sleep(random.uniform(1.5, 3.5))

    print(f"\nHarvested {len(price_rows)} price points across {len(rows)} pages "
          f"via {route_name}")
    print(f"Catalogue now holds {len(catalog)} distinct products")

    if args.dry_run:
        print("dry run -- nothing written")
        return 0

    if not price_rows:
        print("ERROR: nothing harvested.", file=sys.stderr)
        return 1

    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY.open("a", encoding="utf-8") as fh:
        for r in price_rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    CATALOG.write_text(json.dumps(catalog, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Wrote {HISTORY.name} (+{len(price_rows)} rows) and {CATALOG.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
