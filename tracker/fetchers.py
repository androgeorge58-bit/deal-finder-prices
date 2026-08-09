"""
Fetch routes to noon.

noon accepts TCP connections from GitHub's datacenter IPs and then never
answers -- every request dies on ReadTimeout. A plain requests.get() will
never work from a GitHub Actions runner, no matter what headers you send.

So instead of one route, there are four. At the start of every run the
scraper probes them against a real product page and uses whichever one
actually returns a price. If noon changes its blocking, or a proxy dies,
the next route takes over without you touching anything.

Routes, in the order they are tried:

  direct      no proxy. Free, fastest. Works when run from a home
              connection; expected to fail on GitHub Actions.
  reader      r.jina.ai text extraction service. Free, no signup.
  scraperapi  api.scraperapi.com. Free tier is 1,000 requests/month.
              Only attempted if you set the SCRAPER_API_KEY secret.
  allorigins  api.allorigins.win open proxy. Free, no signup, slower
              and less reliable -- the last resort.
"""

import os
import time
import random
from urllib.parse import quote

import requests

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
