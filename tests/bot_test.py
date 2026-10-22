"""Bell Bot against a fake broker (no network, no account). python tests/bot_test.py"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bell_alerts.broker import OrderRejected
from bell_alerts.trader import Trader


class FakeBroker:
    live = False

    def __init__(self, equity=100000.0, last=100000.0):
        self.equity, self.last, self.book, self.calls, self.n = equity, last, [], [], 0

    def account(self):
        return dict(equity=str(self.equity), last_equity=str(self.last))

    def orders(self, after, status="all"):
        return [o for o in self.book if status == "all" or o["status"] in ("new", "held", "accepted")]

    def positions(self):
        return {o["symbol"]: dict(qty=o["qty"], unrealized_pl="12.5") for o in self.book if o["status"] == "filled"}

    def bracket(self, sym, qty, limit, stop, target, cid):
        if any(o["client_order_id"] == cid for o in self.book):
            raise OrderRejected("422: client_order_id must be unique")
        self.n += 1
        o = dict(id=f"o{self.n}", client_order_id=cid, symbol=sym, qty=str(qty), filled_qty="0", status="new",
                 submitted_at="2026-10-22T13:45:10Z", filled_avg_price=None,
                 legs=[dict(id=f"o{self.n}s", type="stop", status="held", stop_price=f"{stop:.2f}"),
                       dict(id=f"o{self.n}t", type="limit", status="held", limit_price=f"{target:.2f}")])
        self.book.append(o)
        self.calls.append(("bracket", sym, qty, round(limit, 2), round(stop, 2), round(target, 2)))
        return o

    def replace_stop(self, oid, stop):
        self.calls.append(("replace_stop", oid, round(stop, 2)))

    def cancel(self, oid):
        self.calls.append(("cancel", oid))

    def close(self, sym):
        self.calls.append(("close", sym))


def alert(sym, hm, entry, stop, **kw):
    from main import et_at
    h, m = hm
    return dict(sym=sym, t=et_at("2026-10-22", h, m), g="A+", entry=entry, stop=stop, tgt=entry + 1.5 * (entry - stop), first=not kw.get("repeat"), **kw)


def check(name, ok):
    print(("ok   " if ok else "FAIL ") + name)
    if not ok:
        check.failed = True


check.failed = False
logs = []

# 1. TRADING off: nothing is sent
os.environ["TRADING"] = "off"
fb = FakeBroker()
bot = Trader("2026-10-22", 100, fb, logs.append)
line = bot.on_alert(alert("NVDA", (9, 45), 100.0, 98.0), late=False)
check("dry run sends nothing", fb.calls == [] and line.startswith("would"))

# 2. armed: bracket with the right size, then a duplicate is refused
os.environ["TRADING"] = "on"
fb = FakeBroker()
bot = Trader("2026-10-22", 100, fb, logs.append)
line = bot.on_alert(alert("NVDA", (9, 45), 100.0, 98.0), late=False)
# limit 100.20, R = 2.20 -> 45 shares
check("bracket order sized for $100 risk", fb.calls == [("bracket", "NVDA", 45, 100.2, 98.0, 103.0)])
check("same alert twice is not re-sent", "already placed" in bot.on_alert(alert("NVDA", (9, 45), 100.0, 98.0), late=False))

# 3. restart: a new Trader finds today's orders and does not double them
bot2 = Trader("2026-10-22", 100, fb, logs.append)
check("restart rebuilds today's orders", "already placed" in bot2.on_alert(alert("NVDA", (9, 45), 100.0, 98.0), late=False))

# 4. ADD raises the first lot's stop, then places its own bracket
line = bot.on_alert(alert("NVDA", (10, 10), 102.0, 100.5, add=True, repeat=True, move_from=98.0, move_to=100.5), late=False)
check("ADD raises the first lot's stop", ("replace_stop", "o1s", 100.5) in fb.calls)
check("ADD places its own lot", fb.calls[-1][0] == "bracket" and fb.calls[-1][1] == "NVDA")

# 5. late alerts are skipped
check("late alert skipped", "late" in bot.on_alert(alert("AMD", (9, 50), 50.0, 49.0), late=True))

# 6. MAX_TRADES
os.environ["MAX_TRADES"] = "2"
fb = FakeBroker()
bot = Trader("2026-10-22", 100, fb, logs.append)
for i, s in enumerate(["A", "B", "C"]):
    last = bot.on_alert(alert(s, (9, 45 + i * 5), 50.0, 49.0), late=False)
check("MAX_TRADES stops the 3rd entry", "MAX_TRADES" in last and sum(c[0] == "bracket" for c in fb.calls) == 2)
os.environ.pop("MAX_TRADES")

# 7. daily loss stop
fb = FakeBroker(equity=99650.0, last=100000.0)  # -3.5R
bot = Trader("2026-10-22", 100, fb, logs.append)
check("daily -3R stop", "MAX_LOSS_R" in bot.on_alert(alert("TSLA", (9, 45), 200.0, 196.0), late=False) and not fb.calls)

# 8. position cap: tight stop on a pricey stock is capped at $25k
fb = FakeBroker()
bot = Trader("2026-10-22", 100, fb, logs.append)
bot.on_alert(alert("AVGO", (9, 45), 400.0, 399.5), late=False)
check("position capped at MAX_POS_USD", fb.calls and fb.calls[0][2] * fb.calls[0][3] <= 25000)

# 9. unfilled entry is cancelled after ENTRY_TTL
bot.expire_entries()
check("stale entry cancelled", ("cancel", "o1") in fb.calls)

# 10. flatten only touches the bot's own symbols
fb = FakeBroker()
bot = Trader("2026-10-22", 100, fb, logs.append)
bot.on_alert(alert("NVDA", (9, 45), 100.0, 98.0), late=False)
fb.book[0]["status"] = "filled"
fb.book.append(dict(id="mine", client_order_id="manual-1", symbol="AAPL", qty="10", status="filled"))
out = bot.flatten()
check("flatten closes the bot's NVDA only", ("close", "NVDA") in fb.calls and ("close", "AAPL") not in fb.calls)

sys.exit(1 if check.failed else 0)
