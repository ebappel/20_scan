#!/usr/bin/env python3
"""
SSS Study -- nightly breadth scan.  (v3)

Runs TWO scans against the same six sessions of data:

    UP:    c/c5 >= 1.20   (up 20% or more over 5 trading days)
    DOWN:  c/c5 <= 0.80   (down 20% or more over 5 trading days)

Both share the same filters:
    min(volume over last 3 sessions) > 100,000
    close >= $5

Universe: US common stocks + ADRs.
Data: Polygon.io grouped daily aggregates, previous trading day.

Outputs to output/:
    latest_up.csv     the up 20% names
    latest_down.csv   the down 20% names
    latest.json       both lists plus the breadth summary
    history.csv       one row per night: date, up count, down count, ratio

Env:
    POLYGON_API_KEY   required
"""

import os
import sys
import csv
import json
import time
import datetime as dt

import requests

API_KEY = os.environ.get("POLYGON_API_KEY")
if not API_KEY:
    sys.exit("POLYGON_API_KEY not set")

BASE = "https://api.polygon.io"

# --- scan parameters -------------------------------------------------
UP_RATIO = 1.20          # up 20% or more
DOWN_RATIO = 0.80        # down 20% or more
LOOKBACK_BARS = 5        # c5 = close 5 bars ago
MIN_VOL_WINDOW = 3       # minimum volume over last N sessions
MIN_VOL = 100_000
MIN_PRICE = 5.00
VOL_OFFSET = 0           # 0 = include most recent bar
                         # 1 = exclude it (matches TC2000 minv3.1)
UNIVERSE_TYPES = ("CS", "ADRC")

START_DAYS_BACK = 1      # start at previous session, not today
SLEEP_BETWEEN_CALLS = 13  # free tier is 5 requests/min


class NotAuthorized(Exception):
    pass


def get(url, params=None):
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
    sessions = []
    day = dt.date.today() - dt.timedelta(days=START_DAYS_BACK)
    probes = 0
    unauthorized_days = 0

    while len(sessions) < n_needed and probes < 25:
        probes += 1

        if day.weekday() < 5:
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
                        "\nPolygon refused 4 dates in a row -- likely an "
                        "entitlement problem. See https://polygon.io/pricing\n"
                    )
            time.sleep(SLEEP_BETWEEN_CALLS)

        day -= dt.timedelta(days=1)

    if len(sessions) < n_needed:
        raise SystemExit(
            f"\nOnly found {len(sessions)} of {n_needed} required sessions.\n"
        )

    sessions.reverse()
    return sessions


def run_scans():
    n_needed = LOOKBACK_BARS + 1 + VOL_OFFSET

    sys.stderr.write("Loading universe...\n")
    universe = load_universe()
    sys.stderr.write(f"  {len(universe)} common stocks + ADRs\n")

    sys.stderr.write(f"Loading {n_needed} sessions...\n")
    sessions = load_sessions(n_needed)

    idx_current = len(sessions) - 1
    current_date, current_bars = sessions[idx_current]
    _, prior_bars = sessions[idx_current - LOOKBACK_BARS]

    vol_end = len(sessions) - VOL_OFFSET
    vol_sessions = sessions[vol_end - MIN_VOL_WINDOW:vol_end]

    up_hits, down_hits = [], []

    for ticker in universe:
        cur = current_bars.get(ticker)
        pri = prior_bars.get(ticker)
        if not cur or not pri:
            continue

        c, c5 = cur.get("c"), pri.get("c")
        if not c or not c5 or c5 <= 0:
            continue

        # price and volume filters apply to BOTH directions
        if c < MIN_PRICE:
            continue

        ratio = c / c5
        if UP_RATIO > ratio > DOWN_RATIO:
            continue  # neither scan

        vols, ok = [], True
        for _, bars in vol_sessions:
            b = bars.get(ticker)
            if not b:
                ok = False
                break
            vols.append(b.get("v") or 0)
        if not ok or min(vols) <= MIN_VOL:
            continue

        row = {
            "ticker": ticker,
            "close": round(c, 2),
            "close_5d_ago": round(c5, 2),
            "pct_change_5d": round((ratio - 1) * 100, 2),
            "volume": int(cur.get("v") or 0),
            "min_vol_3d": int(min(vols)),
        }

        if ratio >= UP_RATIO:
            up_hits.append(row)
        else:
            down_hits.append(row)

    up_hits.sort(key=lambda h: h["pct_change_5d"], reverse=True)
    down_hits.sort(key=lambda h: h["pct_change_5d"])  # most negative first

    up_n, down_n = len(up_hits), len(down_hits)
    total = up_n + down_n

    return {
        "scan": "SSS Study 20% Breadth",
        "as_of": current_date.isoformat(),
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "criteria": {
            "up_ratio": UP_RATIO,
            "down_ratio": DOWN_RATIO,
            "lookback_bars": LOOKBACK_BARS,
            "min_vol": MIN_VOL,
            "min_vol_window": MIN_VOL_WINDOW,
            "vol_offset": VOL_OFFSET,
            "min_price": MIN_PRICE,
        },
        "breadth": {
            "up_count": up_n,
            "down_count": down_n,
            "net": up_n - down_n,
            # share of the 20% movers that are to the upside, 0.0 - 1.0
            "up_share": round(up_n / total, 4) if total else None,
            # classic ratio; None when there are no down names
            "up_down_ratio": round(up_n / down_n, 2) if down_n else None,
        },
        "up_count": up_n,
        "down_count": down_n,
        "up": up_hits,
        "down": down_hits,
    }


FIELDS = ["ticker", "close", "close_5d_ago", "pct_change_5d",
          "volume", "min_vol_3d"]


def write_csv(path, rows):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)


def append_history(out):
    """One row per trading day. Skips dates already recorded."""
    path = "output/history.csv"
    seen = set()
    if os.path.exists(path):
        with open(path) as f:
            for row in csv.DictReader(f):
                seen.add(row["as_of"])

    if out["as_of"] in seen:
        sys.stderr.write(f"history already has {out['as_of']}, skipping\n")
        return

    b = out["breadth"]
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["as_of", "up_count", "down_count", "net",
                        "up_share", "up_down_ratio"])
        w.writerow([out["as_of"], b["up_count"], b["down_count"],
                    b["net"], b["up_share"], b["up_down_ratio"]])


def main():
    out = run_scans()
    os.makedirs("output", exist_ok=True)

    with open("output/latest.json", "w") as f:
        json.dump(out, f, indent=2)

    write_csv("output/latest_up.csv", out["up"])
    write_csv("output/latest_down.csv", out["down"])
    append_history(out)

    b = out["breadth"]
    sys.stderr.write(
        f"\nAs of {out['as_of']}:\n"
        f"  up 20%+   : {b['up_count']}\n"
        f"  down 20%+ : {b['down_count']}\n"
        f"  net       : {b['net']:+d}\n"
    )


if __name__ == "__main__":
    main()
