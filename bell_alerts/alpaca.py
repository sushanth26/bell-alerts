"""Alpaca market data (https://docs.alpaca.markets/reference/stockbars). Standard library only.

FEED=iex  (free plan): real-time bars come from IEX only. SIP history is used where the free plan allows
          (older than 15 minutes) for prices; today's 5-min bars stay on IEX so VWAP and rel vol are
          computed from one consistent feed.
FEED=sip  (paid plan): everything from the consolidated tape, closest to Robinhood / TradingView.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from .engine import ET, et_date, et_min

URL = "https://data.alpaca.markets/v2/stocks/bars"
SIP_DELAY = 16 * 60  # free plan: no SIP data from the last 15 minutes


class Alpaca:
    def __init__(self, key=None, secret=None, feed=None):
        self.key = key or os.environ["ALPACA_KEY_ID"]
        self.secret = secret or os.environ["ALPACA_SECRET_KEY"]
        self.feed = (feed or os.environ.get("FEED") or "iex").lower()

    def _get(self, params):
        q = urllib.parse.urlencode(params)
        req = urllib.request.Request(f"{URL}?{q}", headers={"APCA-API-KEY-ID": self.key, "APCA-API-SECRET-KEY": self.secret})
        for attempt in range(5):
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                if e.code == 429 or e.code >= 500:
                    time.sleep(2 + attempt * 3)
                    continue
                raise RuntimeError(f"Alpaca {e.code}: {e.read().decode()[:300]}") from None
            except urllib.error.URLError:
                time.sleep(2 + attempt * 3)
        raise RuntimeError("Alpaca: no response after 5 tries")

    def bars(self, symbols, timeframe, start, end=None, feed=None):
        """{sym: [(t, o, h, l, c, v)]} with t = bar start, epoch seconds."""
        out = {}
        syms = sorted(set(symbols))
        for i in range(0, len(syms), 50):
            params = dict(symbols=",".join(syms[i:i + 50]), timeframe=timeframe, start=_iso(start), limit=10000,
                          adjustment="split", feed=feed or self.feed, sort="asc")
            if end is not None:
                params["end"] = _iso(end)
            while True:
                d = self._get(params)
                for s, arr in (d.get("bars") or {}).items():
                    out.setdefault(s, []).extend(
                        (_ts(b["t"]), float(b["o"]), float(b["h"]), float(b["l"]), float(b["c"]), float(b["v"])) for b in arr)
                tok = d.get("next_page_token")
                if not tok:
                    break
                params["page_token"] = tok
        for s in out:
            out[s] = _dedupe(out[s])
        return out

    def bars_mixed(self, symbols, timeframe, start, now):
        """Prices for clouds: SIP up to the free plan's 15-minute line, IEX for the last stretch."""
        if self.feed == "sip":
            return self.bars(symbols, timeframe, start, now)
        cut = now - SIP_DELAY
        old = self.bars(symbols, timeframe, start, cut, feed="sip") if cut > _epoch(start) else {}
        new = self.bars(symbols, timeframe, max(_epoch(start), cut - 3600), now, feed="iex")
        for s, arr in new.items():
            have = {b[0] for b in old.get(s, [])}
            old[s] = _dedupe(old.get(s, []) + [b for b in arr if b[0] not in have])
        return old


def regular(bars):
    return [b for b in bars if 570 <= et_min(b[0]) < 960]


def to_4h(hour_bars):
    """4-hour extended bars from 1-hour bars, buckets 4:00 / 8:00 / 12:00 / 16:00 ET."""
    out, cur, key = [], None, None
    for b in hour_bars:
        d = datetime.fromtimestamp(b[0], ET)
        start = d.replace(hour=d.hour - d.hour % 4, minute=0, second=0, microsecond=0)
        k = int(start.timestamp())
        if k != key:
            if cur:
                out.append(tuple(cur))
            key, cur = k, [k, b[1], b[2], b[3], b[4], b[5]]
        else:
            cur[2] = max(cur[2], b[2]); cur[3] = min(cur[3], b[3]); cur[4] = b[4]; cur[5] += b[5]
    if cur:
        out.append(tuple(cur))
    return out


def daily_from_intraday(bars):
    """Regular-session daily bars [(date, o, h, l, c, v)] from 30-min (or 5-min) bars."""
    days = {}
    for b in regular(bars):
        d = et_date(b[0])
        if d not in days:
            days[d] = [d, b[1], b[2], b[3], b[4], b[5]]
        else:
            x = days[d]; x[2] = max(x[2], b[2]); x[3] = min(x[3], b[3]); x[4] = b[4]; x[5] += b[5]
    return [tuple(days[d]) for d in sorted(days)]


def _dedupe(arr):
    m = {b[0]: b for b in arr}
    return [m[t] for t in sorted(m)]


def _ts(s):
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def _epoch(x):
    return x if isinstance(x, (int, float)) else int(x.timestamp())


def _iso(x):
    if isinstance(x, (int, float)):
        x = datetime.fromtimestamp(x, timezone.utc)
    return x.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def days_ago(now, n):
    return datetime.fromtimestamp(now, timezone.utc) - timedelta(days=n)
