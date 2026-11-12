"""Write draft/kalshi.json from Kalshi's markets for event KXPRESNOMD-28.

Usage: python3 .github/kalshi/kalshi_odds.py RAW_JSON OUT_JSON

Each candidate's chance is the middle of Kalshi's best yes bid and ask (in dollars,
so 0.27 means 27%). When one side is missing or the spread is wider than
MAX_SPREAD, the last traded price is used instead. Kalshi's chances are not
rescaled, so across all candidates they add up to a little over 100%.
"""
import json
import sys
from datetime import datetime, timezone

EVENT = "KXPRESNOMD-28"
URL = "https://kalshi.com/markets/kxpresnomd"
MAX_SPREAD = 0.05


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def chance(m):
    bid, ask, last = num(m.get("yes_bid_dollars")), num(m.get("yes_ask_dollars")), num(m.get("last_price_dollars"))
    if bid and ask and ask >= bid and ask - bid <= MAX_SPREAD:
        return round((bid + ask) / 2, 4)
    return last


def build(raw):
    prices = {}
    for m in raw.get("markets", []):
        name = (m.get("yes_sub_title") or (m.get("custom_strike") or {}).get("Candidate") or "").strip()
        p = chance(m)
        if name and p is not None and m.get("status") in ("active", "open"):
            prices[name] = p
    return {"event": EVENT, "url": URL,
            "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "prices": dict(sorted(prices.items(), key=lambda kv: (-kv[1], kv[0])))}


def main(raw_path, out_path):
    with open(raw_path, encoding="utf-8") as f:
        raw = json.load(f)
    data = build(raw)
    if len(data["prices"]) < 5:
        sys.exit(f"Only {len(data['prices'])} markets with prices; not writing {out_path}")
    old = None
    try:
        with open(out_path, encoding="utf-8") as f:
            old = json.load(f)
    except (OSError, ValueError):
        pass
    if old and old.get("prices") == data["prices"]:
        print("Prices unchanged; file left as is")
        return
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.write("\n")
    print(f"Wrote {len(data['prices'])} prices to {out_path}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
