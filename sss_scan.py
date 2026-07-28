#!/usr/bin/env python3
"""
SSS Study -- nightly breadth scan.  (v4)

Runs TWO scans against the same sessions of data:

    UP:    c/c5 >= 1.20   (up 20% or more over 5 trading days)
    DOWN:  c/c5 <= 0.80   (down 20% or more over 5 trading days)

Both share the same filters:
    min(volume over last 3 sessions) > 100,000
    close >= $5

Universe: US common stocks + ADRs.
Data: Polygon.io grouped daily aggregates, previous trading day.

v4 changes:
  - records SPY's close in history.csv so the chart has a price line
  - BACKFILL_DAYS env var computes breadth for past dates in one run

Outputs to output/:
    latest_up.csv     the up 20% names
    latest_down.csv   the down 20% names
    latest.json       both lists plus the breadth summary
    history.csv       one row per session: date, SPY close, up, down, net

Env:
    POLYGON_API_KEY   required
    BACKFILL_DAYS     optional. Set to e.g. 120 to rebuild history from
                      scratch across the last 120 trading days, then unset.
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
UP_RATIO = 1.20
DOWN_RATIO = 0.80
LOOKBACK_BARS = 5
MIN_VOL_WINDOW = 3
MIN_VOL = 100_000
MIN_PRICE = 5.00
VOL_OFFSET = 0
UNIVERSE_TYPES = ("CS", "ADRC")
BENCHMARK = "SPY"

START_DAYS_BACK = 1
SLEEP_BETWEEN_CALLS = 13

BACKFILL_DAYS = int(os.environ.get("BACKFILL_DAYS", "0"))


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
    """Walk backward collecting grouped daily bars. Returns oldest -> newest."""
    sessions = []
    day = dt.date.today() - dt.timedelta(days=START_DAYS_BACK)
    probes = 0
    unauthorized_days = 0
    max_probes = n_needed * 2 + 20

    while len(sessions) < n_needed and probes < max_probes:
        probes += 1

        if day.weekday() < 5:
            url = f"{BASE}/v2/aggs/grouped/locale/us/market/stocks/{day.isoformat()}"
            try:
                data = get(url, {"adjusted": "true"})
                results = data.get("results") or []
                if results:
                    sessions.append((day, {row["T"]: row for row in results}))
                    sys.stderr.write(
                        f"  {day}: {len(results)} tickers "
                        f"({len(sessions)}/{n_needed})\n")
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


def scan_one_day(universe, sessions, idx):
    """Compute breadth for sessions[idx]. Needs LOOKBACK_BARS prior sessions."""
    current_date, current_bars = sessions[idx]
    _, prior_bars = sessions[idx - LOOKBACK_BARS]

    vol_end = idx + 1 - VOL_OFFSET
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
        if c < MIN_PRICE:
            continue

        ratio = c / c5
        if UP_RATIO > ratio > DOWN_RATIO:
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
    down_hits.sort(key=lambda h: h["pct_change_5d"])

    spy = current_bars.get(BENCHMARK)
    spy_close = round(spy["c"], 2) if spy else None

    up_n, down_n = len(up_hits), len(down_hits)
    total = up_n + down_n

    return {
        "scan": "SSS Study 20% Breadth",
        "as_of": current_date.isoformat(),
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "spy_close": spy_close,
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
            "up_share": round(up_n / total, 4) if total else None,
            "up_down_ratio": round(up_n / down_n, 2) if down_n else None,
        },
        "up_count": up_n,
        "down_count": down_n,
        "up": up_hits,
        "down": down_hits,
    }


FIELDS = ["ticker", "close", "close_5d_ago", "pct_change_5d",
          "volume", "min_vol_3d"]
HIST_FIELDS = ["as_of", "spy_close", "up_count", "down_count",
               "net", "up_share", "up_down_ratio"]


def write_csv(path, rows):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)


def hist_row(out):
    b = out["breadth"]
    return {
        "as_of": out["as_of"],
        "spy_close": out["spy_close"],
        "up_count": b["up_count"],
        "down_count": b["down_count"],
        "net": b["net"],
        "up_share": b["up_share"],
        "up_down_ratio": b["up_down_ratio"],
    }


def merge_history(new_rows):
    """Merge rows into history.csv, keyed by date. Newest date wins."""
    path = "output/history.csv"
    by_date = {}

    if os.path.exists(path):
        with open(path) as f:
            for row in csv.DictReader(f):
                by_date[row["as_of"]] = {k: row.get(k, "") for k in HIST_FIELDS}

    for row in new_rows:
        by_date[row["as_of"]] = row

    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HIST_FIELDS)
        w.writeheader()
        for d in sorted(by_date):
            w.writerow(by_date[d])

    return len(by_date)


def main():
    os.makedirs("output", exist_ok=True)

    n_days = max(BACKFILL_DAYS, 1)
    n_needed = n_days + LOOKBACK_BARS + VOL_OFFSET

    sys.stderr.write("Loading universe...\n")
    universe = load_universe()
    sys.stderr.write(f"  {len(universe)} common stocks + ADRs\n")

    if BACKFILL_DAYS:
        sys.stderr.write(
            f"BACKFILL MODE: {BACKFILL_DAYS} days "
            f"({n_needed} sessions, roughly {n_needed * SLEEP_BETWEEN_CALLS // 60} min)\n")

    sys.stderr.write(f"Loading {n_needed} sessions...\n")
    sessions = load_sessions(n_needed)

    # scan every day that has enough history behind it
    first = LOOKBACK_BARS + VOL_OFFSET
    results = [scan_one_day(universe, sessions, i)
               for i in range(first, len(sessions))]

    latest = results[-1]

    with open("output/latest.json", "w") as f:
        json.dump(latest, f, indent=2)

    write_csv("output/latest_up.csv", latest["up"])
    write_csv("output/latest_down.csv", latest["down"])

    total = merge_history([hist_row(r) for r in results])

    b = latest["breadth"]
    sys.stderr.write(
        f"\nAs of {latest['as_of']} (SPY {latest['spy_close']}):\n"
        f"  up 20%+   : {b['up_count']}\n"
        f"  down 20%+ : {b['down_count']}\n"
        f"  net       : {b['net']:+d}\n"
        f"  history   : {total} sessions on file\n"
    )


if __name__ == "__main__":
    main()
