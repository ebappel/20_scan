#!/usr/bin/env python3
"""
SSS Study 20% plus Bullish -- nightly scan.  (v2)

Replicates the TC2000 pre-scan condition:
    c/c5 >= 1.2  and  minv3 > 100000  and  c >= 5
against a universe of US common stocks + ADRs.

v2 changes:
  - starts the session walk at START_DAYS_BACK (default 1) instead of today,
    because free Polygon plans are not entitled to the current session
  - prints the API's actual error message instead of a bare HTTP status
  - skips a NOT_AUTHORIZED date and keeps walking back rather than dying

Env:
    POLYGON_API_KEY   required
"""

import os
import sys
import json
import time
import datetime as dt

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
VOL_OFFSET = 0           # 0 = include most recent bar (post-close run)
                         # 1 = exclude it (matches TC2000 minv3.1)
UNIVERSE_TYPES = ("CS", "ADRC")

# Free plans aren't entitled to the current session. 1 = start at yesterday.
# If you still get NOT_AUTHORIZED, raise this to 2.
START_DAYS_BACK = 1

SLEEP_BETWEEN_CALLS = 13  # free tier is 5 requests/min


class NotAuthorized(Exception):
    pass


def get(url, params=None):
    """GET with retries. Surfaces Polygon's own error text on failure."""
    params = dict(params or {})
    params["apiKey"] = API_KEY

    for attempt in range(5):
        r = requests.get(url, params=params, timeout=30)

        if r.status_code == 429:
            time.sleep(20 * (attempt + 1))
            continue

        if r.status_code == 403:
            try:
                msg = r.json().get("message", r.text)
            except Exception:
                msg = r.text
            raise NotAuthorized(msg)

        if not r.ok:
            try:
                msg = r.json().get("message", r.text)
            except Exception:
                msg = r.text
            raise RuntimeError(f"HTTP {r.status_code} from {url}\n  {msg}")

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
    Walk backward collecting grouped daily bars, starting START_DAYS_BACK
    days ago. Empty result = weekend or holiday. Returns oldest -> newest.
    """
    sessions = []
    day = dt.date.today() - dt.timedelta(days=START_DAYS_BACK)
    probes = 0
    unauthorized_days = 0

    while len(sessions) < n_needed and probes < 25:
        probes += 1

        if day.weekday() < 5:  # skip weekends without burning a call
            url = f"{BASE}/v2/aggs/grouped/locale/us/market/stocks/{day.isoformat()}"
            try:
                data = get(url, {"adjusted": "true"})
                results = data.get("results") or []
                if results:
                    sessions.append((day, {row["T"]: row for row in results}))
                    sys.stderr.write(f"  {day}: {len(results)} tickers\n")
                else:
                    sys.stderr.write(f"  {day}: no data (holiday?)\n")
            except NotAuthorized as e:
                unauthorized_days += 1
                sys.stderr.write(f"  {day}: NOT AUTHORIZED -- {e}\n")
                if unauthorized_days >= 4:
                    raise SystemExit(
                        "\nPolygon refused 4 dates in a row.\n"
                        "Your plan likely does not include the grouped daily "
                        "aggregates endpoint at all.\n"
                        "Check https://polygon.io/pricing\n"
                    )
            time.sleep(SLEEP_BETWEEN_CALLS)

        day -= dt.timedelta(days=1)

    if len(sessions) < n_needed:
        raise SystemExit(
            f"\nOnly found {len(sessions)} of {n_needed} required sessions.\n"
            "If you see NOT AUTHORIZED above, that's an entitlement problem.\n"
        )

    sessions.reverse()
    return sessions


def run_scan():
    n_needed = LOOKBACK_BARS + 1 + VOL_OFFSET

    sys.stderr.write("Loading universe...\n")
    universe = load_universe()
    sys.stderr.write(f"  {len(universe)} common stocks + ADRs\n")

    sys.stderr.write(f"Loading {n_needed} sessions...\n")
    sessions = load_sessions(n_needed)

    idx_current = len(sessions) - 1
    idx_prior = idx_current - LOOKBACK_BARS

    current_date, current_bars = sessions[idx_current]
    _, prior_bars = sessions[idx_prior]

    vol_end = len(sessions) - VOL_OFFSET
    vol_sessions = sessions[vol_end - MIN_VOL_WINDOW:vol_end]

    hits = []
    for ticker in universe:
        cur = current_bars.get(ticker)
        pri = prior_bars.get(ticker)
        if not cur or not pri:
            continue

        c, c5 = cur.get("c"), pri.get("c")
        if not c or not c5 or c5 <= 0:
            continue
        if c < MIN_PRICE or c / c5 < GAIN_RATIO:
            continue

        vols, ok = [], True
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
