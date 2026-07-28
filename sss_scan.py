#!/usr/bin/env python3
"""
SSS Study 20% plus Bullish -- nightly scan.

Replicates the TC2000 pre-scan condition:
    c/c5 >= 1.2  and  minv3 > 100000  and  c >= 5
against a universe of US common stocks + ADRs.

Data source: Polygon.io grouped daily aggregates (one call returns every
US ticker's OHLCV for a single date), plus the reference tickers endpoint
to restrict the universe to CS (common stock) and ADRC (ADR) types.

Env:
    POLYGON_API_KEY   required
"""

import os
import sys
import json
import time
import datetime as dt
from collections import defaultdict

import requests

API_KEY = os.environ.get("POLYGON_API_KEY")
if not API_KEY:
    sys.exit("POLYGON_API_KEY not set")

BASE = "https://api.polygon.io"

# --- scan parameters -------------------------------------------------
GAIN_RATIO = 1.20        # c / c5 >= 1.20   (up 20% over 5 trading days)
LOOKBACK_BARS = 5        # c5 = close 5 bars ago
MIN_VOL_WINDOW = 3       # minimum volume over last N sessions
MIN_VOL = 100_000
MIN_PRICE = 5.00
VOL_OFFSET = 0           # 0 = include today's bar (post-close run)
                         # 1 = exclude today's bar (matches TC2000 minv3.1)
UNIVERSE_TYPES = ("CS", "ADRC")

# Free tier is rate limited. Bump this down if you're on a paid plan.
SLEEP_BETWEEN_CALLS = 13


def get(url, params=None):
    params = dict(params or {})
    params["apiKey"] = API_KEY
    for attempt in range(5):
        r = requests.get(url, params=params, timeout=30)
        if r.status_code == 429:
            time.sleep(20 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"repeated rate limiting on {url}")


def load_universe():
    """Return the set of tickers that are common stock or ADR."""
    tickers = set()
    for t in UNIVERSE_TYPES:
        url = f"{BASE}/v3/reference/tickers"
        params = {"market": "stocks", "type": t, "active": "true", "limit": 1000}
        while True:
            data = get(url, params)
            for row in data.get("results", []):
                tickers.add(row["ticker"])
            nxt = data.get("next_url")
            if not nxt:
                break
            url, params = nxt, {}
            time.sleep(SLEEP_BETWEEN_CALLS)
        time.sleep(SLEEP_BETWEEN_CALLS)
    return tickers


def load_sessions(n_needed):
    """
    Walk backward from today collecting grouped daily bars.
    Empty result = weekend or market holiday; skip it.
    Returns list of (date, {ticker: bar}) ordered oldest -> newest.
    """
    sessions = []
    day = dt.date.today()
    probes = 0
    while len(sessions) < n_needed and probes < 20:
        probes += 1
        if day.weekday() < 5:  # skip obvious weekends without burning a call
            url = f"{BASE}/v2/aggs/grouped/locale/us/market/stocks/{day.isoformat()}"
            data = get(url, {"adjusted": "true"})
            results = data.get("results") or []
            if results:
                bars = {row["T"]: row for row in results}
                sessions.append((day, bars))
                sys.stderr.write(f"  {day}: {len(bars)} tickers\n")
            time.sleep(SLEEP_BETWEEN_CALLS)
        day -= dt.timedelta(days=1)

    if len(sessions) < n_needed:
        raise RuntimeError(f"only found {len(sessions)} of {n_needed} sessions")

    sessions.reverse()
    return sessions


def run_scan():
    n_needed = LOOKBACK_BARS + 1 + VOL_OFFSET

    sys.stderr.write("Loading universe...\n")
    universe = load_universe()
    sys.stderr.write(f"  {len(universe)} common stocks + ADRs\n")

    sys.stderr.write(f"Loading {n_needed} sessions...\n")
    sessions = load_sessions(n_needed)

    # newest bar we evaluate against
    idx_current = len(sessions) - 1
    idx_prior = idx_current - LOOKBACK_BARS

    current_date, current_bars = sessions[idx_current]
    _, prior_bars = sessions[idx_prior]

    # volume window
    vol_end = len(sessions) - VOL_OFFSET
    vol_start = vol_end - MIN_VOL_WINDOW
    vol_sessions = sessions[vol_start:vol_end]

    hits = []
    for ticker in universe:
        cur = current_bars.get(ticker)
        pri = prior_bars.get(ticker)
        if not cur or not pri:
            continue

        c = cur.get("c")
        c5 = pri.get("c")
        if not c or not c5 or c5 <= 0:
            continue

        if c < MIN_PRICE:
            continue
        if c / c5 < GAIN_RATIO:
            continue

        vols = []
        ok = True
        for _, bars in vol_sessions:
            b = bars.get(ticker)
            if not b:
                ok = False
                break
            vols.append(b.get("v") or 0)
        if not ok or min(vols) <= MIN_VOL:
            continue

        hits.append({
            "ticker": ticker,
            "close": round(c, 2),
            "close_5d_ago": round(c5, 2),
            "pct_change_5d": round((c / c5 - 1) * 100, 2),
            "volume": int(cur.get("v") or 0),
            "min_vol_3d": int(min(vols)),
        })

    hits.sort(key=lambda h: h["pct_change_5d"], reverse=True)

    return {
        "scan": "SSS Study 20% plus Bullish",
        "as_of": current_date.isoformat(),
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "criteria": {
            "gain_ratio": GAIN_RATIO,
            "lookback_bars": LOOKBACK_BARS,
            "min_vol": MIN_VOL,
            "min_vol_window": MIN_VOL_WINDOW,
            "vol_offset": VOL_OFFSET,
            "min_price": MIN_PRICE,
        },
        "count": len(hits),
        "results": hits,
    }


def main():
    out = run_scan()

    os.makedirs("output", exist_ok=True)

    with open("output/latest.json", "w") as f:
        json.dump(out, f, indent=2)

    with open("output/latest.csv", "w") as f:
        f.write("ticker,close,close_5d_ago,pct_change_5d,volume,min_vol_3d\n")
        for h in out["results"]:
            f.write("{ticker},{close},{close_5d_ago},{pct_change_5d},"
                    "{volume},{min_vol_3d}\n".format(**h))

    sys.stderr.write(f"\n{out['count']} hits as of {out['as_of']}\n")


if __name__ == "__main__":
    main()
