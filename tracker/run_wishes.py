#!/usr/bin/env python3
"""
run_wishes.py  (SINGLE-FILE VERSION)
====================================
Everything the wish watch needs is in THIS ONE FILE. No wishwatch/ folder, no
package, nothing else to upload. Drop this one file in tracker/, overwriting the
old run_wishes.py, and the "No module named 'wishwatch'" error is gone.

What it does (unchanged):
  - reads data/wishes.json  (products users asked to watch)
  - searches noon + Jumia for each wish
  - matches the SAME product across stores, picks the cheapest store
  - notifies "found" the first time, "better price than before" on a drop,
    and stops when a wish is marked purchased
  - writes data/wish_results.json (app reads it) and data/notifications.json
    (your notify.py / OneSignal sends it)

Needs SCRAPER_API_KEY in the environment (already your GitHub secret).
ANTHROPIC_API_KEY is optional (only used to untangle messy titles).
"""

from __future__ import annotations
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Optional
from urllib.parse import quote_plus, urlencode

import requests
from bs4 import BeautifulSoup


# =========================================================================
#  1. NORMALIZE  — turn a messy title into a match key
# =========================================================================
BRANDS = ["apple", "iphone", "samsung", "galaxy", "xiaomi", "redmi", "poco",
          "realme", "oppo", "vivo", "huawei", "honor", "nokia", "infinix",
          "tecno", "oneplus", "google", "pixel", "sony", "lg", "lenovo",
          "hp", "dell", "asus", "acer", "msi", "macbook", "ipad"]
BRAND_ALIAS = {"iphone": "apple", "ipad": "apple", "macbook": "apple",
               "galaxy": "samsung", "redmi": "xiaomi", "poco": "xiaomi",
               "pixel": "google"}
_STORAGE_RE = re.compile(r"(\d+)\s*(tb|gb)\b", re.I)
_RAM_RE = re.compile(r"(\d+)\s*gb\s*ram", re.I)
_MODEL_RE = re.compile(
    r"\b(?:iphone|galaxy|note|redmi|poco|pixel|reno|nova)?\s*"
    r"([a-z]?\d{1,3}[a-z]?(?:\s*(?:pro|max|ultra|plus|lite|fe|mini))*)", re.I)
_COLORS = ["black", "white", "blue", "green", "red", "gold", "silver", "gray",
           "grey", "purple", "pink", "yellow", "titanium", "graphite",
           "midnight", "starlight", "teal", "orange", "cream", "lavender"]


@dataclass
class ProductSig:
    brand: Optional[str] = None
    model: Optional[str] = None
    storage_gb: Optional[int] = None
    ram_gb: Optional[int] = None
    color: Optional[str] = None
    raw: str = ""
    confident: bool = False

    def match_key(self) -> str:
        parts = [(self.brand or "?"),
                 re.sub(r"\s+", "", (self.model or "?")),
                 f"{self.storage_gb}gb" if self.storage_gb else "?"]
        return "|".join(p.lower() for p in parts)


def normalize(title: str) -> ProductSig:
    t = " ".join(title.lower().split())
    sig = ProductSig(raw=title)
    for b in BRANDS:
        if re.search(rf"\b{re.escape(b)}\b", t):
            sig.brand = BRAND_ALIAS.get(b, b)
            break
    ram_m = _RAM_RE.search(t)
    if ram_m:
        sig.ram_gb = int(ram_m.group(1))
    for m in _STORAGE_RE.finditer(t):
        if ram_m and ram_m.start() <= m.start() <= ram_m.end():
            continue
        val, unit = int(m.group(1)), m.group(2)
        sig.storage_gb = val * 1024 if unit.lower() == "tb" else val
        break
    mm = _MODEL_RE.search(t)
    if mm:
        sig.model = " ".join(mm.group(1).split())
    for c in _COLORS:
        if re.search(rf"\b{c}\b", t):
            sig.color = "gray" if c == "grey" else c
            break
    sig.confident = all([sig.brand, sig.model, sig.storage_gb])
    return sig


def fill_with_llm(title: str, client, model: str = "claude-sonnet-4-6") -> ProductSig:
    base = normalize(title)
    if client is None or base.confident:
        return base
    prompt = ("Extract product attributes from this retail title. Reply ONLY "
              'with JSON: {"brand":..,"model":..,"storage_gb":..,"color":..}. '
              "null for anything absent. Storage in GB (1TB=1024).\n\nTitle: " + title)
    try:
        resp = client.messages.create(model=model, max_tokens=200,
                                      messages=[{"role": "user", "content": prompt}])
        txt = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        data = json.loads(txt.replace("```json", "").replace("```", "").strip())
        for k in ("brand", "model", "color"):
            if data.get(k):
                setattr(base, k, str(data[k]).lower())
        if data.get("storage_gb"):
            base.storage_gb = int(data["storage_gb"])
        base.confident = all([base.brand, base.model, base.storage_gb])
    except Exception:
        pass
    return base


# =========================================================================
#  2. PLATFORMS  — search URLs + HTML parsers (the only fragile part)
# =========================================================================
@dataclass
class Listing:
    platform: str
    title: str
    price: Optional[float]
    url: str
    image: Optional[str] = None
    sku: Optional[str] = None


def _to_float(s: str) -> Optional[float]:
    if not s:
        return None
    m = re.search(r"\d+(?:\.\d+)?", s.replace(",", ""))
    return float(m.group()) if m else None


class NoonAdapter:
    name = "noon"

    def search_url(self, q):
        return f"https://www.noon.com/egypt-en/search/?q={quote_plus(q)}"

    def parse(self, html):
        out, soup = [], BeautifulSoup(html, "html.parser")
        blob = soup.find("script", id="__NEXT_DATA__")
        if blob and blob.string:
            try:
                out = self._from_next(json.loads(blob.string))
                if out:
                    return out
            except Exception:
                pass
        for card in soup.select('[data-qa="product-block"], div.productContainer'):
            a = card.find("a", href=True)
            title = card.select_one('[data-qa="product-name"], .name')
            price = card.select_one('[data-qa="product-price"], .price, .sellingPrice')
            img = card.find("img")
            if not (a and title):
                continue
            href = a["href"]
            out.append(Listing(self.name, title.get_text(" ", strip=True),
                               _to_float(price.get_text() if price else ""),
                               href if href.startswith("http") else f"https://www.noon.com{href}",
                               (img.get("src") or img.get("data-src")) if img else None))
        return out

    def _from_next(self, data):
        out = []
        def walk(n):
            if isinstance(n, dict):
                if "sku" in n and ("name" in n or "title" in n):
                    p = n.get("sale_price") or n.get("price")
                    if isinstance(p, dict):
                        p = p.get("value")
                    out.append(Listing(self.name, n.get("name") or n.get("title") or "",
                                       _to_float(str(p)) if p is not None else None,
                                       f"https://www.noon.com/egypt-en/{n.get('url_key', n['sku'])}/p/",
                                       (n.get("image_key") and f"https://f.nooncdn.com/p/{n['image_key']}.jpg"),
                                       n.get("sku")))
                for v in n.values():
                    walk(v)
            elif isinstance(n, list):
                for v in n:
                    walk(v)
        walk(data)
        seen, uniq = set(), []
        for l in out:
            if l.sku in seen:
                continue
            seen.add(l.sku)
            uniq.append(l)
        return uniq


class JumiaAdapter:
    name = "jumia"

    def search_url(self, q):
        return f"https://www.jumia.com.eg/catalog/?q={quote_plus(q)}"

    def parse(self, html):
        out, soup = [], BeautifulSoup(html, "html.parser")
        for card in soup.select("article.prd, a.core"):
            a = card if card.name == "a" else card.find("a", href=True)
            name = card.select_one(".name")
            price = card.select_one(".prc")
            img = card.find("img")
            if not (a and name):
                continue
            href = a.get("href", "")
            out.append(Listing(self.name, name.get_text(" ", strip=True),
                               _to_float(price.get_text() if price else ""),
                               href if href.startswith("http") else f"https://www.jumia.com.eg{href}",
                               (img.get("data-src") or img.get("src")) if img else None))
        return out


ADAPTERS = [NoonAdapter(), JumiaAdapter()]


# =========================================================================
#  3. FETCHER  — ScraperAPI wrapper
# =========================================================================
class Fetcher:
    def __init__(self, api_key=None, timeout=40, max_retries=2, throttle_s=1.0):
        self.api_key = api_key or os.getenv("SCRAPER_API_KEY") or os.getenv("SCRAPERAPI_KEY")
        self.timeout, self.max_retries, self.throttle_s = timeout, max_retries, throttle_s
        self._last = 0.0

    def _wrap(self, url):
        if not self.api_key:
            return url
        return "https://api.scraperapi.com/?" + urlencode(
            {"api_key": self.api_key, "url": url, "country_code": "eg"})

    def fetch(self, url):
        gap = time.time() - self._last
        if gap < self.throttle_s:
            time.sleep(self.throttle_s - gap)
        target = self._wrap(url)
        for attempt in range(self.max_retries + 1):
            try:
                r = requests.get(target, timeout=self.timeout,
                                 headers={"User-Agent": "Mozilla/5.0"})
                self._last = time.time()
                if r.status_code == 200 and r.text:
                    return r.text
            except requests.RequestException:
                pass
            time.sleep(1.5 * (attempt + 1))
        return None


# =========================================================================
#  4. STORE  — wishes.json + wish_history.json
# =========================================================================
@dataclass
class Wish:
    query: str
    user_id: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    category: Optional[str] = None
    active: bool = True
    purchased: bool = False
    created_at: float = field(default_factory=time.time)
    last_notified_price: Optional[float] = None
    last_best_price: Optional[float] = None
    last_supplier: Optional[str] = None
    last_url: Optional[str] = None
    last_checked: Optional[float] = None

    def is_watching(self):
        return self.active and not self.purchased


class Store:
    def __init__(self, data_dir="data"):
        self.dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.wishes_path = os.path.join(data_dir, "wishes.json")
        self.history_path = os.path.join(data_dir, "wish_history.json")

    def load_wishes(self):
        if not os.path.exists(self.wishes_path):
            return []
        with open(self.wishes_path, encoding="utf-8") as f:
            return [Wish(**w) for w in json.load(f)]

    def save_wishes(self, wishes):
        with open(self.wishes_path, "w", encoding="utf-8") as f:
            json.dump([asdict(w) for w in wishes], f, ensure_ascii=False, indent=2)

    def mark_purchased(self, wish_id):
        wishes = self.load_wishes()
        for w in wishes:
            if w.id == wish_id:
                w.purchased, w.active = True, False
        self.save_wishes(wishes)

    def append_history(self, wish_id, price, supplier):
        hist = {}
        if os.path.exists(self.history_path):
            with open(self.history_path, encoding="utf-8") as f:
                hist = json.load(f)
        hist.setdefault(wish_id, []).append(
            {"t": int(time.time()), "price": price, "supplier": supplier})
        with open(self.history_path, "w", encoding="utf-8") as f:
            json.dump(hist, f, ensure_ascii=False, indent=2)


# =========================================================================
#  5. ENGINE  — search -> match -> cheapest -> notify decision
# =========================================================================
_TOKEN = re.compile(r"[a-z0-9]+")


def _tokens(s):
    return set(_TOKEN.findall(s.lower()))


def _match_score(query, sig):
    q = _tokens(query)
    if not q:
        return 0.0
    overlap = len(q & _tokens(sig.raw)) / len(q)
    qs = re.search(r"(\d+)\s*(tb|gb)", query.lower())
    if qs and sig.storage_gb:
        want = int(qs.group(1)) * (1024 if qs.group(2) == "tb" else 1)
        overlap += 0.25 if want == sig.storage_gb else -0.35
    return overlap


@dataclass
class Notification:
    wish_id: str
    user_id: str
    kind: str
    query: str
    price: float
    prev_price: Optional[float]
    supplier: str
    url: str
    title: str
    ts: int


@dataclass
class WishResult:
    wish_id: str
    query: str
    best_price: Optional[float]
    best_supplier: Optional[str]
    best_url: Optional[str]
    best_title: Optional[str]
    options: list
    checked_at: int


def _search_all(query, fetcher, adapters):
    out = []
    for ad in adapters:
        html = fetcher.fetch(ad.search_url(query))
        if not html:
            continue
        try:
            out.extend(ad.parse(html))
        except Exception:
            continue
    return out


def _best_group(query, listings, llm_client, min_match):
    groups, scores = {}, {}
    for l in listings:
        sig = normalize(l.title)
        if not sig.confident and llm_client is not None:
            sig = fill_with_llm(l.title, llm_client)
        key = sig.match_key()
        groups.setdefault(key, []).append(l)
        scores[key] = max(scores.get(key, 0.0), _match_score(query, sig))
    if not groups:
        return []
    best = max(scores, key=scores.get)
    return groups[best] if scores[best] >= min_match else []


def run_cycle(store, fetcher, adapters=ADAPTERS, llm_client=None, min_match=0.5):
    wishes = store.load_wishes()
    notifs, results = [], []
    for w in wishes:
        if not w.is_watching():
            continue
        listings = _search_all(w.query, fetcher, adapters)
        group = _best_group(w.query, listings, llm_client, min_match)
        w.last_checked = time.time()
        if not group:
            results.append(WishResult(w.id, w.query, None, None, None, None, [],
                                      int(w.last_checked)))
            continue
        group.sort(key=lambda l: l.price if l.price is not None else 1e12)
        cheapest = group[0]
        price = cheapest.price
        if price is None:
            continue
        store.append_history(w.id, price, cheapest.platform)
        prev = w.last_notified_price
        if prev is None:
            notifs.append(Notification(w.id, w.user_id, "found", w.query, price,
                                       prev, cheapest.platform, cheapest.url,
                                       cheapest.title, int(time.time())))
            w.last_notified_price = price
        elif price < prev:
            notifs.append(Notification(w.id, w.user_id, "better_price", w.query,
                                       price, prev, cheapest.platform, cheapest.url,
                                       cheapest.title, int(time.time())))
            w.last_notified_price = price
        w.last_best_price = price
        w.last_supplier = cheapest.platform
        w.last_url = cheapest.url
        results.append(WishResult(w.id, w.query, price, cheapest.platform,
                                  cheapest.url, cheapest.title,
                                  [{"platform": l.platform, "title": l.title,
                                    "price": l.price, "url": l.url} for l in group],
                                  int(w.last_checked)))
    store.save_wishes(wishes)
    return notifs, results


# =========================================================================
#  6. ENTRYPOINT
# =========================================================================
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
    fetcher = Fetcher()
    if not fetcher.api_key:
        print("WARNING: no SCRAPER_API_KEY found; searches will likely fail.")
    notifs, results = run_cycle(store, fetcher, llm_client=_llm_client())

    with open(os.path.join(DATA_DIR, "wish_results.json"), "w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in results], f, ensure_ascii=False, indent=2)

    q_path = os.path.join(DATA_DIR, "notifications.json")
    queue = []
    if os.path.exists(q_path):
        with open(q_path, encoding="utf-8") as f:
            queue = json.load(f)
    queue.extend(asdict(n) for n in notifs)
    with open(q_path, "w", encoding="utf-8") as f:
        json.dump(queue, f, ensure_ascii=False, indent=2)

    print(f"wishes checked -> {len(results)} results, {len(notifs)} new notifications")
    for n in notifs:
        tag = "NEW" if n.kind == "found" else f"was {n.prev_price}"
        print(f"  [{n.kind}] {n.query}: {n.price} @ {n.supplier} ({tag})")


if __name__ == "__main__":
    main()
