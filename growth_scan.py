#!/usr/bin/env python3
"""
Growth Screen -- fundamental scan.  (v1)

Finds stocks where ALL of the following are true:

    1. Average sales growth over the last 2 reported quarters >= 39%
       (each quarter measured against the SAME quarter one year earlier,
        then the two figures averaged)
    2. Market cap < $10 billion
    3. First listed within the last 10 years

Data sources:
    SEC EDGAR XBRL "frames" API  -- revenue and share counts.
        Free, no API key, no meaningful rate limit. One call returns
        every filer's number for a given quarter.
    Polygon.io  -- previous close (for market cap) and listing date.
        Only used for names that already passed filters 1 and 2, because
        the free tier allows just 5 calls per minute.

Listing dates are cached in output/list_dates.csv so each ticker is only
ever looked up once. The first few runs will leave some names marked
"pending" while the cache fills; after that it is essentially instant.

Outputs to output/:
    growth_screen.csv    the qualifying names
    growth_screen.json   same data plus run metadata, read by growth.html
    list_dates.csv       the listing-date cache (do not delete)

Env:
    POLYGON_API_KEY   required
    SEC_USER_AGENT    required by the SEC. Format: "Your Name your@email.com"
"""

import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, timedelta

# ---------------------------------------------------------------- settings

MIN_AVG_GROWTH = 0.39           # 39%
MAX_MARKET_CAP = 10_000_000_000  # $10B
MAX_LISTING_AGE_YEARS = 10
MIN_PRICE = 0.0                  # raise to 5.0 to match the breadth scan

MAX_POLYGON_LOOKUPS = 150        # per run; ~33 min at the free-tier limit
POLYGON_SLEEP = 13.0             # seconds between calls (safely under 5/min)
MIN_FRAME_COVERAGE = 1200        # a quarter with fewer filers is too fresh

OUT_DIR = "output"
CACHE_PATH = os.path.join(OUT_DIR, "list_dates.csv")

# Revenue is tagged inconsistently across filers. Tried in order; the first
# tag that has a value for a given company wins.
REVENUE_TAGS = [
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "SalesRevenueNet",
]

# (taxonomy, tag, unit, is_instantaneous)
SHARE_TAGS = [
    ("dei", "EntityCommonStockSharesOutstanding", "shares", True),
    ("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding", "shares", False),
    ("us-gaap", "WeightedAverageNumberOfSharesOutstandingBasic", "shares", False),
]

POLYGON_KEY = os.environ.get("POLYGON_API_KEY", "").strip()
SEC_UA = os.environ.get("SEC_USER_AGENT", "").strip()


def log(msg):
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


# ------------------------------------------------------------ http helpers

def get_json(url, headers=None, tries=3, quiet_404=False):
    """GET a URL and parse JSON. Returns None on a clean miss."""
    for attempt in range(tries):
        req = urllib.request.Request(url, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                if not quiet_404:
                    log(f"    404 {url}")
                return None
            if e.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                wait = 5 * (attempt + 1)
                log(f"    {e.code}, retrying in {wait}s")
                time.sleep(wait)
                continue
            log(f"    HTTP {e.code} on {url}")
            return None
        except Exception as e:
            if attempt < tries - 1:
                time.sleep(5)
                continue
            log(f"    failed: {e}")
            return None
    return None


def sec_get(url):
    if not SEC_UA:
        log("SEC_USER_AGENT is not set. The SEC rejects anonymous requests.")
        sys.exit(1)
    time.sleep(0.15)  # SEC asks for under 10 requests/second
    return get_json(url, headers={
        "User-Agent": SEC_UA,
        "Accept-Encoding": "gzip, deflate",
        "Host": "data.sec.gov",
    })


# ------------------------------------------------------------ quarter math

def quarter_of(d):
    return (d.month - 1) // 3 + 1


def shift_quarter(year, q, n):
    """Move n quarters from (year, q). n may be negative."""
    idx = year * 4 + (q - 1) + n
    return idx // 4, idx % 4 + 1


def frame_label(year, q, instantaneous=False):
    return f"CY{year}Q{q}{'I' if instantaneous else ''}"


# ----------------------------------------------------------- EDGAR loading

def load_ticker_map():
    """CIK -> ticker/name. The SEC publishes this as a flat file."""
    url = "https://www.sec.gov/files/company_tickers.json"
    data = get_json(url, headers={"User-Agent": SEC_UA,
                                  "Accept-Encoding": "gzip, deflate"})
    if not data:
        log("Could not load the SEC ticker map. Cannot continue.")
        sys.exit(1)
    out = {}
    for row in data.values():
        cik = int(row["cik_str"])
        # Keep the first ticker seen for a CIK (share classes list A, B, ...)
        if cik not in out:
            out[cik] = {"ticker": row["ticker"].upper(), "name": row["title"]}
    log(f"Ticker map: {len(out)} companies")
    return out


def load_revenue_frame(year, q):
    """Merged revenue for one calendar quarter: {cik: (value, tag)}."""
    merged = {}
    for tag in REVENUE_TAGS:
        url = (f"https://data.sec.gov/api/xbrl/frames/us-gaap/{tag}/USD/"
               f"{frame_label(year, q)}.json")
        data = sec_get(url)
        if not data or "data" not in data:
            continue
        added = 0
        for row in data["data"]:
            cik = row.get("cik")
            val = row.get("val")
            if cik is None or val is None:
                continue
            if cik not in merged:
                merged[cik] = (float(val), tag)
                added += 1
        log(f"    {tag}: +{added} (running total {len(merged)})")
    return merged


def pick_latest_quarter():
    """Walk back until a quarter has enough filers to be usable."""
    probe = date.today() - timedelta(days=120)
    year, q = probe.year, quarter_of(probe)
    for _ in range(5):
        log(f"  probing {frame_label(year, q)}")
        frame = load_revenue_frame(year, q)
        if len(frame) >= MIN_FRAME_COVERAGE:
            log(f"  using {frame_label(year, q)} as the latest quarter "
                f"({len(frame)} filers)")
            return year, q, frame
        log(f"  {frame_label(year, q)} has only {len(frame)} filers, "
            f"stepping back")
        year, q = shift_quarter(year, q, -1)
    log("No quarter had enough coverage. Aborting.")
    sys.exit(1)


def load_shares(year, q):
    """Share counts for one quarter: {cik: shares}."""
    merged = {}
    for taxonomy, tag, unit, inst in SHARE_TAGS:
        url = (f"https://data.sec.gov/api/xbrl/frames/{taxonomy}/{tag}/{unit}/"
               f"{frame_label(year, q, inst)}.json")
        data = sec_get(url)
        if not data or "data" not in data:
            log(f"    {tag}: unavailable")
            continue
        added = 0
        for row in data["data"]:
            cik, val = row.get("cik"), row.get("val")
            if cik is None or not val:
                continue
            if cik not in merged:
                merged[cik] = float(val)
                added += 1
        log(f"    {tag}: +{added} (running total {len(merged)})")
    return merged


# --------------------------------------------------------- Polygon helpers

def polygon_prices():
    """Previous session's closes for the whole market in one call."""
    for back in range(1, 8):
        d = (date.today() - timedelta(days=back)).isoformat()
        url = (f"https://api.polygon.io/v2/aggs/grouped/locale/us/market/"
               f"stocks/{d}?adjusted=true&apiKey={POLYGON_KEY}")
        data = get_json(url, quiet_404=True)
        if data and data.get("resultsCount", 0) > 0:
            prices = {r["T"].upper(): r["c"] for r in data["results"]
                      if r.get("c")}
            log(f"Prices as of {d}: {len(prices)} tickers")
            return prices, d
        time.sleep(POLYGON_SLEEP)
    log("Could not retrieve prices from Polygon.")
    sys.exit(1)


def read_cache():
    cache = {}
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH, newline="") as f:
            for row in csv.DictReader(f):
                cache[row["ticker"]] = row["list_date"]
    return cache


def write_cache(cache):
    with open(CACHE_PATH, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ticker", "list_date"])
        for t in sorted(cache):
            w.writerow([t, cache[t]])


def polygon_list_date(ticker):
    """Listing date for one ticker. Returns '' if Polygon has none."""
    url = (f"https://api.polygon.io/v3/reference/tickers/{ticker}"
           f"?apiKey={POLYGON_KEY}")
    data = get_json(url, quiet_404=True)
    if not data:
        return "none"
    return (data.get("results") or {}).get("list_date") or "none"


# ------------------------------------------------------------------ screen

def main():
    if not POLYGON_KEY:
        log("POLYGON_API_KEY is not set.")
        sys.exit(1)

    os.makedirs(OUT_DIR, exist_ok=True)

    log("Loading SEC ticker map")
    tickers = load_ticker_map()

    log("Finding the most recent quarter with full coverage")
    y_latest, q_latest, rev_latest = pick_latest_quarter()

    y_prev, q_prev = shift_quarter(y_latest, q_latest, -1)
    y_l4, q_l4 = shift_quarter(y_latest, q_latest, -4)
    y_p4, q_p4 = shift_quarter(y_prev, q_prev, -4)

    log(f"Loading {frame_label(y_prev, q_prev)}")
    rev_prev = load_revenue_frame(y_prev, q_prev)
    log(f"Loading {frame_label(y_l4, q_l4)}")
    rev_l4 = load_revenue_frame(y_l4, q_l4)
    log(f"Loading {frame_label(y_p4, q_p4)}")
    rev_p4 = load_revenue_frame(y_p4, q_p4)

    log("Loading share counts")
    shares = load_shares(y_latest, q_latest)

    # -- filter 1: growth -------------------------------------------------
    candidates = []
    for cik, (rev_a, _) in rev_latest.items():
        if cik not in tickers:
            continue
        base_a = rev_l4.get(cik)
        rev_b = rev_prev.get(cik)
        base_b = rev_p4.get(cik)
        if not (base_a and rev_b and base_b):
            continue
        base_a_val, base_b_val = base_a[0], base_b[0]
        rev_b_val = rev_b[0]
        # A negative or zero year-ago base makes the percentage meaningless
        if base_a_val <= 0 or base_b_val <= 0:
            continue
        g1 = rev_a / base_a_val - 1.0
        g2 = rev_b_val / base_b_val - 1.0
        avg = (g1 + g2) / 2.0
        if avg < MIN_AVG_GROWTH:
            continue
        candidates.append({
            "cik": cik,
            "ticker": tickers[cik]["ticker"],
            "name": tickers[cik]["name"],
            "rev_latest": rev_a,
            "rev_latest_yago": base_a_val,
            "growth_latest": g1,
            "rev_prev": rev_b_val,
            "rev_prev_yago": base_b_val,
            "growth_prev": g2,
            "avg_growth": avg,
        })
    log(f"Passed the growth filter: {len(candidates)}")

    # -- filter 2: market cap ---------------------------------------------
    prices, price_date = polygon_prices()
    sized = []
    for c in candidates:
        px = prices.get(c["ticker"])
        sh = shares.get(c["cik"])
        if not px or not sh or px < MIN_PRICE:
            continue
        cap = px * sh
        if cap >= MAX_MARKET_CAP:
            continue
        c["price"] = px
        c["shares"] = sh
        c["market_cap"] = cap
        sized.append(c)
    sized.sort(key=lambda r: -r["avg_growth"])
    log(f"Passed the market cap filter: {len(sized)}")

    # -- filter 3: listing age --------------------------------------------
    cache = read_cache()
    cutoff = date.today() - timedelta(days=365.25 * MAX_LISTING_AGE_YEARS)
    lookups = 0

    for c in sized:
        t = c["ticker"]
        if t not in cache:
            if lookups >= MAX_POLYGON_LOOKUPS:
                continue
            if lookups:
                time.sleep(POLYGON_SLEEP)
            cache[t] = polygon_list_date(t)
            lookups += 1
            log(f"  [{lookups}] {t} -> {cache[t]}")
    write_cache(cache)
    log(f"Looked up {lookups} listing dates this run; "
        f"cache holds {len(cache)}")

    passing, pending = [], []
    for c in sized:
        ld = cache.get(c["ticker"])
        if ld is None:
            c["list_date"] = ""
            pending.append(c)
            continue
        c["list_date"] = "" if ld == "none" else ld
        if not c["list_date"]:
            pending.append(c)
            continue
        try:
            listed = date.fromisoformat(c["list_date"])
        except ValueError:
            pending.append(c)
            continue
        c["years_listed"] = round((date.today() - listed).days / 365.25, 1)
        if listed >= cutoff:
            passing.append(c)

    log(f"Passed all three filters: {len(passing)}  "
        f"(awaiting a listing date: {len(pending)})")

    # -- write ------------------------------------------------------------
    meta = {
        "price_date": price_date,
        "quarter_latest": frame_label(y_latest, q_latest),
        "quarter_prev": frame_label(y_prev, q_prev),
        "criteria": {
            "min_avg_growth": MIN_AVG_GROWTH,
            "max_market_cap": MAX_MARKET_CAP,
            "max_listing_age_years": MAX_LISTING_AGE_YEARS,
        },
        "counts": {
            "growth_pass": len(candidates),
            "cap_pass": len(sized),
            "final": len(passing),
            "pending": len(pending),
        },
    }

    with open(os.path.join(OUT_DIR, "growth_screen.json"), "w") as f:
        json.dump({"meta": meta, "passing": passing, "pending": pending},
                  f, indent=2)

    cols = ["ticker", "name", "price", "market_cap", "avg_growth",
            "growth_latest", "growth_prev", "rev_latest", "rev_latest_yago",
            "rev_prev", "rev_prev_yago", "list_date", "years_listed"]
    with open(os.path.join(OUT_DIR, "growth_screen.csv"), "w",
              newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for c in passing:
            w.writerow([c.get(k, "") for k in cols])

    log("\nDone.")
    log(f"  quarters compared : {meta['quarter_latest']} and "
        f"{meta['quarter_prev']} (each vs. one year earlier)")
    log(f"  growth >= {int(MIN_AVG_GROWTH * 100)}%     : "
        f"{len(candidates)}")
    log(f"  cap < $10B        : {len(sized)}")
    log(f"  listed < 10y ago  : {len(passing)}")
    log(f"  pending lookup    : {len(pending)}")


if __name__ == "__main__":
    main()
