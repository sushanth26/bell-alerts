"""Bell Alerts: the Opening Bell Desk's Proven alerts, sent to Telegram on every 5-minute close, 9:40-11:00 ET.

  python main.py run                 live morning loop (what GitHub Actions runs)
  python main.py replay 2026-10-20   print what it would have sent that day
  python main.py record [date]       after the close: save the day's alerts and results to data/
  python main.py test                send a test message
  python main.py chat-id             show your Telegram chat id (message the bot first)

Research only, not investment advice. It never places, reviews or cancels orders.
"""
import os
import sys
import time
from datetime import datetime, timedelta
from html import escape
from pathlib import Path

from bell_alerts import telegram
from bell_alerts.alpaca import Alpaca, daily_from_intraday, days_ago, regular, to_4h
from bell_alerts.engine import ET, FIRST_MIN, LAST_MIN, proven_day, spy_ctx

ROOT = Path(__file__).parent
RISK = float(os.environ.get("RISK", "100"))
MAX_NAMES = int(os.environ.get("MAX_NAMES", "30"))
FOOT = "<i>Research only, not investment advice.</i>"


def read_list(name):
    p = ROOT / name
    if not p.exists():
        return []
    return [s.strip().upper() for line in p.read_text().splitlines() for s in line.split("#")[0].split() if s.strip()]


def et_at(date, h, m):
    return int(datetime.fromisoformat(f"{date}T{h:02d}:{m:02d}:00").replace(tzinfo=ET).timestamp())


def hhmm(t):
    return datetime.fromtimestamp(t, ET).strftime("%-I:%M")


def px(x):
    return f"${x:,.2f}"


class Day:
    """All bars for one session, refreshed bar by bar."""

    def __init__(self, api, date):
        self.api, self.date = api, date
        self.open_t = et_at(date, 9, 30)

    def screen(self, now):
        """Desk Backtest screen: pool names > $15, prior-day vol > 2M, 10-day avg vol > 5M, prior day +1.5%,
        gap >= 2% at the open; top MAX_NAMES by gap, plus everything in watchlist.txt."""
        pool = read_list("pool.txt")
        d = self.api.bars(pool, "1Day", days_ago(self.open_t, 40), et_at(self.date, 0, 0), feed="sip")
        cand = {}
        for s, arr in d.items():
            arr = [b for b in arr if datetime.fromtimestamp(b[0], ET).date().isoformat() < self.date]
            if len(arr) < 12:
                continue
            prev, prev2 = arr[-1], arr[-2]
            avg10 = sum(b[5] for b in arr[-10:]) / 10
            if prev[4] > 15 and prev[5] > 2e6 and avg10 > 5e6 and prev[4] / prev2[4] - 1 > 0.015:
                cand[s] = prev[4]
        first = self.api.bars(list(cand), "5Min", self.open_t, self.open_t + 300, feed=self._feed5(now))
        gaps = []
        for s, pc in cand.items():
            b = next((b for b in first.get(s, []) if b[0] == self.open_t), None)
            if b and b[1] / pc - 1 >= 0.02:
                gaps.append((b[1] / pc - 1, s))
        gaps.sort(reverse=True)
        self.gaps = {s: g for g, s in gaps[:MAX_NAMES]}
        extra = [s for s in read_list("watchlist.txt") if s not in self.gaps]
        self.uni = [s for _, s in gaps[:MAX_NAMES]] + extra
        return self.uni

    def _feed5(self, now):
        # VWAP and rel vol always come from one feed, the configured one
        return self.api.feed

    def load(self, now):
        syms = self.uni + ["SPY"]
        self.b5 = {s: regular(v) for s, v in self.api.bars(syms, "5Min", days_ago(self.open_t, 7), now, feed=self._feed5(now)).items()}
        self.m10 = self.api.bars_mixed(syms, "10Min", days_ago(self.open_t, 7), now)
        self.h1 = self.api.bars_mixed(syms, "1Hour", days_ago(self.open_t, 90), now)
        d30 = self.api.bars(self.uni, "30Min", days_ago(self.open_t, 80), et_at(self.date, 0, 0), feed="sip")
        self.daily = {s: [b for b in daily_from_intraday(v) if b[0] < self.date] for s, v in d30.items()}

    def refresh(self, now):
        """Pull today's newest bars and merge them in."""
        syms = self.uni + ["SPY"]
        pre = et_at(self.date, 4, 0)
        new5 = self.api.bars(syms, "5Min", self.open_t, now, feed=self._feed5(now))
        for s, v in new5.items():
            self.b5[s] = _merge(self.b5.get(s, []), regular(v))
        for store, tf in ((self.m10, "10Min"), (self.h1, "1Hour")):
            for s, v in self.api.bars_mixed(syms, tf, pre, now).items():
                store[s] = _merge(store.get(s, []), v)

    def has_bar(self, sym, bar_start):
        return any(b[0] == bar_start for b in reversed(self.b5.get(sym, [])))

    def alerts(self, now):
        X = spy_ctx(self.b5.get("SPY"), now, self.date)
        rows = []
        for s in self.uni:
            tf = {"10m": self.m10.get(s, []), "1h": self.h1.get(s, []), "4h": to_4h(self.h1.get(s, []))}
            r, _ = proven_day(s, self.b5.get(s), tf, self.daily.get(s, []), X, now, self.date, RISK)
            rows += r
        return sorted(rows, key=lambda r: r["t"])


def _merge(old, new):
    m = {b[0]: b for b in old}
    m.update({b[0]: b for b in new})
    return [m[t] for t in sorted(m)]


def message(r, late=False):
    R = r["entry"] - r["stop"]
    kind = "ADD" if r.get("add") else ("Repeat" if r.get("repeat") else "Proven buy")
    lines = [
        f"{'🟢' if not r.get('repeat') else '➕' if r.get('add') else '🔁'} <b>{escape(r['sym'])}</b> · {kind} · <b>{r['g']}</b> · SPY {'above' if r['spy_up'] else 'below'} VWAP",
        f"Entry {px(r['entry'])} ({hhmm(r['t'])} close) · Stop {px(r['stop'])} · Target {px(r['tgt'])} (1.5R)",
        f"Qty {r['qty']} for {px(RISK)} risk (R = {px(R)}) · {r['rvx']:.1f}× SPY rel vol",
    ]
    if r.get("add"):
        lines.append(f"Move first lot's stop {px(r['move_from'])} → {px(r['move_to'])}")
    if late:
        lines.append(f"<i>Late: found at {hhmm(time.time())} ET, after its bar closed.</i>")
    lines.append(f'<a href="https://www.tradingview.com/chart/?symbol={escape(r["sym"])}">Chart</a> · {FOOT}')
    return "\n".join(lines)


def summary(rows, uni):
    if not rows:
        return f"🔔 Morning done: no Proven alerts on {len(uni)} names.\n{FOOT}"
    first = [r for r in rows if r["first"]]
    tot = sum(r["r"] for r in first)
    tag = {"stop": "stop −1R", "target": "target +1.5R", "raised": "raised stop"}
    out = [f"🔔 Morning done: {len(rows)} alerts on {len(first)} tickers · first-per-ticker so far {tot:+.1f}R ({px(tot * RISK)})"]
    for r in rows:
        out.append(f"{hhmm(r['t'])} {escape(r['sym'])} {'ADD' if r.get('add') else 'rep' if r.get('repeat') else ''} {r['g']} @ {px(r['entry'])} → {tag.get(r['res'], 'open')} {r['r']:+.2f}R".replace("  ", " "))
    out.append(FOOT)
    return "\n".join(out)


def wait_until(t):
    while True:
        d = t - time.time()
        if d <= 0:
            return
        time.sleep(min(d, 30))


def run():
    now = time.time()
    e = datetime.fromtimestamp(now, ET)
    date, mins = e.date().isoformat(), e.hour * 60 + e.minute
    if e.weekday() >= 5 or mins < 9 * 60 or mins >= LAST_MIN:
        print(f"Outside 9:00-11:00 ET on a weekday ({e:%a %H:%M} ET): nothing to do.")
        return
    api = Alpaca()
    day = Day(api, date)
    wait_until(et_at(date, 9, 35) + 8)
    try:
        uni = day.screen(time.time())
        day.load(time.time())
    except Exception as err:
        telegram.send(f"⚠️ Bell Alerts could not start: {escape(str(err))[:500]}")
        raise
    if not day.has_bar("SPY", day.open_t):
        print("No SPY bar at 9:30: market closed today.")
        return
    names = ", ".join(f"{s} {day.gaps[s] * 100:+.1f}%" if s in day.gaps else s for s in uni) or "none"
    telegram.send(f"🔔 Bell Alerts on · {len(uni)} names ({api.feed.upper()} data): {escape(names)}\n{FOOT}")
    sent, errors = set(), 0
    bar_end = max(et_at(date, 9, 40), (int(time.time()) // 300) * 300)
    while et_min_of(bar_end) < LAST_MIN:
        wait_until(bar_end + 6)
        try:
            for _ in range(10):  # a bar can land a few seconds late
                day.refresh(time.time())
                if day.has_bar("SPY", bar_end - 300):
                    break
                time.sleep(6)
            now = time.time()
            for r in day.alerts(now):
                k = f"{r['sym']}|{r['t']}"
                if k in sent:
                    continue
                sent.add(k)
                telegram.send(message(r, late=now - r["t"] > 6 * 60))
            print(f"{hhmm(bar_end)} checked {len(uni)} names, {len(sent)} alerts so far")
        except Exception as err:
            errors += 1
            print(f"{hhmm(bar_end)} error: {err}")
            if errors in (1, 5):
                telegram.send(f"⚠️ Bell Alerts error at {hhmm(bar_end)}: {escape(str(err))[:400]}")
        bar_end += 300
    try:
        telegram.send(summary(day.alerts(time.time()), uni))
    except Exception as err:
        print(f"summary failed: {err}")


def et_min_of(t):
    d = datetime.fromtimestamp(t, ET)
    return d.hour * 60 + d.minute


def replay(date, send=False):
    api = Alpaca()
    day = Day(api, date)
    close = min(et_at(date, 16, 0), int(time.time()))
    uni = day.screen(close)
    day.load(close)
    print(f"{date}: {len(uni)} names ({api.feed}): {' '.join(uni)}\n")
    rows = day.alerts(close)
    for r in rows:
        print(message(r).replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", "").split("\n<a")[0], "\n")
    print(summary(rows, uni).replace("<i>", "").replace("</i>", ""))
    if send:
        telegram.send(f"📋 Replay of {date} ({len(uni)} names, {api.feed.upper()} data)\n" + summary(rows, uni).replace("Morning done", "Alerts"))


RULES = "v5 · A+/A (1H pivot) · ext 1.0/1.6/1.0/0.8 · ATR ≥ 3% · 5 of 5 · 9:40–11:00 · 20m cooldown · ADD raises first stop"


def record(date=None):
    """After the close: save that day's alerts and final results to data/ (the workflow commits them)."""
    import csv
    import json
    now = time.time()
    e = datetime.fromtimestamp(now, ET)
    date = date or e.date().isoformat()
    if date == e.date().isoformat() and (e.weekday() >= 5 or e.hour * 60 + e.minute < 16 * 60):
        print(f"{date}: market not closed yet ({e:%a %H:%M} ET), nothing recorded.")
        return
    api = Alpaca()
    day = Day(api, date)
    close = min(et_at(date, 16, 0), int(now))
    uni = day.screen(close)
    day.load(close)
    if not day.has_bar("SPY", day.open_t):
        print(f"{date}: no SPY bars, market closed. Nothing recorded.")
        return
    rows = day.alerts(close)
    first = [r for r in rows if r["first"]]
    rec = dict(date=date, feed=api.feed, risk=RISK, rules=RULES, recorded_at=datetime.fromtimestamp(now, ET).isoformat(timespec="seconds"),
               names=[dict(sym=s, gap=round(day.gaps[s], 4) if s in day.gaps else None) for s in uni],
               first_R=round(sum(r["r"] for r in first), 3), all_R=round(sum(r["r"] for r in rows), 3),
               alerts=[dict(time=hhmm(r["t"]), t=r["t"], sym=r["sym"], type="ADD" if r.get("add") else "repeat" if r.get("repeat") else "first",
                            grade=r["g"], spy_up=r["spy_up"], entry=round(r["entry"], 4), stop=round(r["stop"], 4), target=round(r["tgt"], 4),
                            qty=r["qty"], result=r["res"], r=round(r["r"], 3), usd=round(r["r"] * RISK, 2), rvx=round(r["rvx"], 2),
                            move_from=r.get("move_from"), move_to=r.get("move_to")) for r in rows])
    out = ROOT / "data" / "days"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{date}.json").write_text(json.dumps(rec, indent=1) + "\n")
    # rebuild the two CSVs from every saved day, so a re-run never duplicates rows
    days = [json.loads(p.read_text()) for p in sorted(out.glob("*.json"))]
    cols = ["date", "time", "sym", "type", "grade", "spy_up", "entry", "stop", "target", "qty", "result", "r", "usd"]
    with open(ROOT / "data" / "trades.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for d in days:
            for a in d["alerts"]:
                w.writerow([d["date"]] + [a[c] for c in cols[1:]])
    with open(ROOT / "data" / "days.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "names", "alerts", "tickers", "first_R", "first_usd", "all_R", "feed"])
        for d in days:
            n1 = sum(1 for a in d["alerts"] if a["type"] == "first")
            w.writerow([d["date"], len(d["names"]), len(d["alerts"]), n1, d["first_R"], round(d["first_R"] * d["risk"], 2), d["all_R"], d["feed"]])
    tot = sum(d["first_R"] for d in days)
    print(f"{date}: saved {len(rows)} alerts on {len(first)} tickers, first-per-ticker {rec['first_R']:+.2f}R · {len(days)} days on file, {tot:+.2f}R total")
    if os.environ.get("RECORD_NOTIFY"):
        telegram.send(f"📒 Saved {date}: {len(rows)} alerts on {len(first)} tickers, {rec['first_R']:+.2f}R ({px(rec['first_R'] * RISK)}). "
                      f"{len(days)} days on file, {tot:+.2f}R total.\n{FOOT}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "run":
        run()
    elif cmd == "replay":
        os.environ.pop("TELEGRAM_BOT_TOKEN", None)  # replays only print
        replay(sys.argv[2])
    elif cmd == "send-day":  # one Telegram message with that day's alerts
        replay(sys.argv[2], send=True)
    elif cmd == "record":  # after the close; optional date to backfill
        record(sys.argv[2] if len(sys.argv) > 2 else None)
    elif cmd == "test":
        telegram.send(f"✅ Bell Alerts test message. Alerts arrive here on 5-minute closes, 9:40–11:00 ET.\n{FOOT}")
    elif cmd == "chat-id":
        for cid, name in telegram.chat_ids().items():
            print(cid, name)
    else:
        print(__doc__)
