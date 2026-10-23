"""Bell Alerts: the Opening Bell Desk's Proven alerts, sent to Telegram on every 5-minute close, 9:40-11:00 ET.

  python main.py run                 live morning loop (what GitHub Actions runs)
  python main.py replay 2026-10-20   print what it would have sent that day
  python main.py record [date]       after the close: add the day's alerts to data/trades.csv
  python main.py test                send a test message
  python main.py chat-id             show your Telegram chat id (message the bot first)
  python main.py trade               Bell Bot: the same alerts as bracket orders in your Alpaca account
                                     (9:20-11:00 ET trades, 15:30-16:00 ET closes the bot's positions)
  python main.py trade report        today's bot orders and P&L

Research only, not investment advice. The alerts never place orders; only `trade` does, in the account
whose keys you set, and only when you set TRADING=on (see bell_alerts/trader.py).
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
MAX_WATCH = 60          # the desk watches at most 60 names a day
RESCAN_EVERY = 5  # minutes: the scanner reruns on every 5-minute close from 9:35, like the desk
FOOT = "<i>Research only, not investment advice.</i>"


def read_list(name):
    p = ROOT / name
    if not p.exists():
        return []
    return [s.strip().upper() for line in p.read_text().splitlines() for s in line.split("#")[0].split() if s.strip()]


def desk_list915(date):
    """The desk's 9:15 shortlist, which the 9:05 ET Claude task writes to data/list915.txt on GitHub.
    Read live from GitHub (the checkout happens at 9:20, possibly before it lands); used only if dated today."""
    import urllib.request
    repo = os.environ.get("GITHUB_REPOSITORY", "sushanth26/bell-alerts")
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/contents/data/list915.txt",
                                 headers={"Accept": "application/vnd.github.raw", "Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            lines = r.read().decode().splitlines()
    except Exception as e:
        print(f"9:15 list not read: {e}")
        return []
    if not lines or date not in lines[0]:
        print(f"9:15 list is not from {date}: {lines[0] if lines else 'empty'}")
        return []
    return [s.strip().upper() for line in lines[1:] for s in line.split("#")[0].split() if s.strip()]


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
        """Desk screen: pool names > $15, prior-day vol > 2M, 10-day avg vol > 2M, gap >= 2% at the open
        (prior day +1.5% and the 5M average were dropped on 2026-10-23); top MAX_NAMES by gap, plus the desk's 9:15 list and watchlist.txt.
        rescan() then adds names as they move, like the desk's scanner."""
        pool = read_list("pool.txt")
        d = self.api.bars(pool, "1Day", days_ago(self.open_t, 40), et_at(self.date, 0, 0), feed="sip")
        cand = {}
        for s, arr in d.items():
            arr = [b for b in arr if datetime.fromtimestamp(b[0], ET).date().isoformat() < self.date]
            if len(arr) < 12:
                continue
            prev = arr[-1]
            avg10 = sum(b[5] for b in arr[-10:]) / 10
            if prev[4] > 15 and prev[5] > 2e6 and avg10 > 2e6:
                cand[s] = prev[4]
        first = self.api.bars(list(cand), "5Min", self.open_t, self.open_t + 300, feed=self._feed5(now))
        gaps = []
        for s, pc in cand.items():
            b = next((b for b in first.get(s, []) if b[0] == self.open_t), None)
            if b and b[1] / pc - 1 >= 0.02:
                gaps.append((b[1] / pc - 1, s))
        gaps.sort(reverse=True)
        self.cand = cand
        self.scanned = set()
        self.joined = {}  # name -> when the rescan added it (names from the opening screen have none)
        self.gaps = {s: g for g, s in gaps[:MAX_NAMES]}
        self.list915 = desk_list915(self.date)
        extra = [s for s in dict.fromkeys(self.list915 + read_list("watchlist.txt")) if s not in self.gaps]
        self.uni = [s for _, s in gaps[:MAX_NAMES]] + extra
        return self.uni

    def _feed5(self, now):
        # VWAP and rel vol always come from one feed, the configured one
        return self.api.feed

    def load(self, now, syms=None):
        """Bars for `syms` (default: the whole universe plus SPY), added to what is already loaded."""
        if syms is None:
            syms = self.uni + ["SPY"]
            self.b5, self.m10, self.h1, self.daily = {}, {}, {}, {}
        if not syms:
            return
        self.b5.update({s: regular(v) for s, v in self.api.bars(syms, "5Min", days_ago(self.open_t, 7), now, feed=self._feed5(now)).items()})
        self.m10.update(self.api.bars_mixed(syms, "10Min", days_ago(self.open_t, 7), now))
        self.h1.update(self.api.bars_mixed(syms, "1Hour", days_ago(self.open_t, 90), now))
        names = [s for s in syms if s != "SPY"]
        d30 = self.api.bars(names, "30Min", days_ago(self.open_t, 80), et_at(self.date, 0, 0), feed="sip") if names else {}
        self.daily.update({s: [b for b in daily_from_intraday(v) if b[0] < self.date] for s, v in d30.items()})

    def rescan(self, now):
        """The desk's scanner: on every 5-minute close from 9:35 to 11:00, the top MAX_NAMES screen names up > 2% on
        the prior close join the watch list for the rest of the day (at most MAX_WATCH). Returns the new names."""
        checks = [t for t in range(et_at(self.date, 9, 35), min(now, et_at(self.date, 11, 0)) + 1, RESCAN_EVERY * 60)
                  if t not in self.scanned]
        if not checks or not self.cand:
            return []
        b = self.api.bars(list(self.cand), "5Min", self.open_t, now, feed=self._feed5(now))
        new = []
        for t in checks:
            self.scanned.add(t)
            movers = []
            for s, pc in self.cand.items():
                last = [x for x in regular(b.get(s, [])) if x[0] + 300 <= t]
                if last and last[-1][4] / pc - 1 > 0.02:
                    movers.append((last[-1][4] / pc - 1, s))
            for g, s in sorted(movers, reverse=True)[:MAX_NAMES]:
                if s not in self.uni and len(self.uni) < MAX_WATCH:
                    self.uni.append(s)
                    self.joined[s] = t
                    self.gaps[s] = g
                    new.append(s)
        self.load(now, new)
        return new

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
            rows += [x for x in r if x["t"] >= self.joined.get(s, 0)]  # nothing from before the name was watched
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
            new = day.rescan(time.time())
            if new:
                telegram.send(f"🔎 Now watching +{len(new)}: {escape(', '.join(f'{s} {day.gaps[s] * 100:+.1f}%' for s in new))}\n{FOOT}")
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
    day.rescan(close)
    print(f"{date}: {len(uni)} names ({api.feed}): {' '.join(uni)}\n")
    rows = day.alerts(close)
    for r in rows:
        print(message(r).replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", "").split("\n<a")[0], "\n")
    print(summary(rows, uni).replace("<i>", "").replace("</i>", ""))
    if send:
        telegram.send(f"📋 Replay of {date} ({len(uni)} names, {api.feed.upper()} data)\n" + summary(rows, uni).replace("Morning done", "Alerts"))


def record(date=None):
    """After the close: add that day's alerts with final results to data/trades.csv (the workflow commits it)."""
    import csv
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
    day.rescan(close)
    if not day.has_bar("SPY", day.open_t):
        print(f"{date}: no SPY bars, market closed. Nothing recorded.")
        return
    rows = day.alerts(close)
    # one table: data/trades.csv, one row per alert; re-running a day replaces that day's rows
    cols = ["date", "time", "ticker", "type", "grade", "spy_vs_vwap", "entry", "stop", "target", "qty", "result", "r", "usd"]
    path = ROOT / "data" / "trades.csv"
    path.parent.mkdir(exist_ok=True)
    keep = []
    if path.exists():
        with open(path, newline="") as f:
            keep = [x for x in csv.DictReader(f) if x["date"] != date]
    new = [dict(date=date, time=datetime.fromtimestamp(r["t"], ET).strftime("%H:%M"), ticker=r["sym"], type="ADD" if r.get("add") else "repeat" if r.get("repeat") else "first",
                grade=r["g"], spy_vs_vwap="above" if r["spy_up"] else "below", entry=f"{r['entry']:.2f}", stop=f"{r['stop']:.2f}",
                target=f"{r['tgt']:.2f}", qty=r["qty"], result=r["res"], r=f"{r['r']:.2f}", usd=f"{r['r'] * RISK:.2f}") for r in rows]
    allrows = sorted(keep + new, key=lambda x: (x["date"], x["time"]))
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(allrows)
    print(f"{date}: saved {len(new)} alerts · {len(allrows)} rows in data/trades.csv")


def trade(cmd=None):
    from bell_alerts.trader import Trader
    now = time.time()
    e = datetime.fromtimestamp(now, ET)
    date, mins = e.date().isoformat(), e.hour * 60 + e.minute
    log = load_log(date)

    def say(msg):
        line = f"{datetime.now(ET):%H:%M} {msg}"
        print(line)
        log.append(line)

    bot = Trader(date, RISK, log=say)
    print(f"Bell Bot {date} · {bot.mode} · risk {px(RISK)} · max {bot.max_trades} trades · stop at -{bot.max_loss_r:g}R")
    if cmd == "report" or e.weekday() >= 5 or not (9 * 60 <= mins < LAST_MIN or 15 * 60 + 30 <= mins < 16 * 60):
        print("\n".join(bot.report()) or "No bot orders today.")
        publish(bot, log)
        return
    if 15 * 60 + 30 <= mins < 16 * 60:
        for line in bot.flatten() or ["Nothing to close at the end of the day."]:
            say(line)
        publish(bot, log)
        return
    api = Alpaca()
    day = Day(api, date)
    wait_until(et_at(date, 9, 35) + 8)
    uni = day.screen(time.time())
    day.load(time.time())
    if not day.has_bar("SPY", day.open_t):
        print("No SPY bar at 9:30: market closed today.")
        return
    say(f"Watching {len(uni)} names: {' '.join(uni)}" + (f" (9:15 list: {' '.join(day.list915)})" if day.list915 else ""))
    publish(bot, log)
    seen = set()
    bar_end = max(et_at(date, 9, 40), (int(time.time()) // 300) * 300)
    while et_min_of(bar_end) < LAST_MIN:
        wait_until(bar_end + 6)
        try:
            for _ in range(10):  # a bar can land a few seconds late
                day.refresh(time.time())
                if day.has_bar("SPY", bar_end - 300):
                    break
                time.sleep(6)
            new = day.rescan(time.time())
            if new:
                say(f"Now watching +{len(new)}: {' '.join(new)}")
            now = time.time()
            for r in day.alerts(now):
                k = f"{r['sym']}|{r['t']}"
                if k not in seen:
                    seen.add(k)
                    say(f"{hhmm(r['t'])} alert · {bot.on_alert(r, late=now - r['t'] > 6 * 60)}")
            bot.expire_entries()
        except Exception as err:
            say(f"{hhmm(bar_end)} error: {err}")
        publish(bot, log)
        bar_end += 300
    wait_until(bar_end + 6)
    bot.expire_entries()
    print("\n".join(bot.report()))
    publish(bot, log)


def load_log(date):
    import json
    p = ROOT / "data" / "bot" / f"{date}.json"
    try:
        return json.loads(p.read_text()).get("log", [])
    except Exception:
        return []


def publish(bot, log):
    """Write the dashboard's data (data/bot/<date>.json + status.json) and push it, when running on GitHub."""
    import json
    import subprocess
    try:
        snap = json.dumps(bot.snapshot(log), indent=1)
    except Exception as err:
        print(f"dashboard snapshot failed: {err}")
        return
    d = ROOT / "data" / "bot"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{bot.date}.json").write_text(snap)
    (d / "status.json").write_text(snap)
    days = sorted(p.stem for p in d.glob("20*.json"))
    (d / "days.json").write_text(json.dumps(days))
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    git = lambda *a: subprocess.run(["git", "-C", str(ROOT), *a], capture_output=True, text=True)
    git("add", "data/bot")
    if git("diff", "--cached", "--quiet").returncode == 0:
        return
    git("commit", "-m", f"Bot status {datetime.now(ET):%Y-%m-%d %H:%M} ET")
    for _ in range(3):
        if git("push").returncode == 0:
            return
        git("pull", "--rebase", "-X", "theirs")
    print("dashboard push failed")


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
    elif cmd == "trade":
        trade(sys.argv[2] if len(sys.argv) > 2 else None)
    elif cmd == "test":
        telegram.send(f"✅ Bell Alerts test message. Alerts arrive here on 5-minute closes, 9:40–11:00 ET.\n{FOOT}")
    elif cmd == "chat-id":
        for cid, name in telegram.chat_ids().items():
            print(cid, name)
    else:
        print(__doc__)
