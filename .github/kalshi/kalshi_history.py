"""Write draft/kalshi-history.json: each candidate's daily Kalshi price for event KXPRESNOMD-28.

Usage: python3 .github/kalshi/kalshi_history.py RAW_JSON OUT_JSON

RAW_JSON is the markets response the "Kalshi odds" workflow already fetches. For each
active market this asks Kalshi for daily candlesticks and keeps one price per day, worked
out the same way as kalshi_odds.py: the middle of the closing yes bid and ask, or the
closing trade price when the spread is wider than MAX_SPREAD. Days with no price repeat
the day before. The file is rebuilt at most once per UTC day, so the 30-minute workflow
only calls Kalshi's history API once a day.
"""
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone

EVENT = "KXPRESNOMD-28"
SERIES = "KXPRESNOMD"
API = "https://api.elections.kalshi.com/trade-api/v2"
MAX_SPREAD = 0.05
DAY = 86400


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def day_price(c):
    bid = num((c.get("yes_bid") or {}).get("close_dollars"))
    ask = num((c.get("yes_ask") or {}).get("close_dollars"))
    close = num((c.get("price") or {}).get("close_dollars"))
    if bid and ask and ask >= bid and ask - bid <= MAX_SPREAD:
        return (bid + ask) / 2
    return close


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "bkkelly.com draft odds"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))


def series_for(market, now):
    opened = datetime.fromisoformat(market["open_time"].replace("Z", "+00:00"))
    start = int(opened.timestamp())
    url = f"{API}/series/{SERIES}/markets/{market['ticker']}/candlesticks?start_ts={start}&end_ts={now}&period_interval=1440"
    days = {}
    for c in fetch(url).get("candlesticks") or []:
        p = day_price(c)
        if p is not None:
            # end_period_ts closes the day; key it by the UTC date it ends.
            days[(c["end_period_ts"] - 1) // DAY] = p
    return days


def main(raw_path, out_path):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        with open(out_path, encoding="utf-8") as f:
            if json.load(f).get("updated", "")[:10] == today:
                print("History already updated today; file left as is")
                return
    except (OSError, ValueError):
        pass
    with open(raw_path, encoding="utf-8") as f:
        markets = [m for m in json.load(f).get("markets", []) if m.get("status") in ("active", "open")]
    now = int(time.time())
    all_days, names = {}, {}
    for m in markets:
        name = (m.get("yes_sub_title") or (m.get("custom_strike") or {}).get("Candidate") or "").strip()
        if not name or not m.get("open_time"):
            continue
        try:
            all_days[name] = series_for(m, now)
        except Exception as e:  # one bad market shouldn't stop the rest
            print(f"Skipped {name}: {e}")
        time.sleep(0.2)
    all_days = {n: d for n, d in all_days.items() if d}
    if len(all_days) < 5:
        sys.exit(f"Only {len(all_days)} candidates with history; not writing {out_path}")
    first = min(min(d) for d in all_days.values())
    last = max(max(d) for d in all_days.values())
    series = {}
    for name, d in all_days.items():
        lo = min(d)
        vals, prev = [], None
        for day in range(lo, last + 1):
            prev = d.get(day, prev)
            vals.append(round(prev * 10000))  # basis points: 1050 means 10.5%
        series[name] = {"start": lo - first, "bp": vals}
    out = {
        "event": EVENT,
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "first_day": datetime.fromtimestamp(first * DAY, timezone.utc).strftime("%Y-%m-%d"),
        "note": "Daily price in basis points per candidate. Index 0 of bp is first_day plus start days.",
        "series": dict(sorted(series.items())),
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")
    print(f"Wrote history for {len(series)} candidates, {last - first + 1} days, to {out_path}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
