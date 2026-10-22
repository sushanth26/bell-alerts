"""Bell Bot: places the Proven alerts as bracket orders in your Alpaca account (paper unless you switch it).

Every alert -> one limit buy with its stop and 1.5R target attached (Alpaca holds both exits).
ADD alerts also raise the first lot's stop. Unfilled entries are cancelled after ENTRY_TTL seconds.
At 15:50 ET `flatten` closes whatever the bot still holds.

Safety settings (GitHub repository variables):
  TRADING      on = send orders · anything else = log what it would do (default)
  MAX_TRADES   new entries per day (default 6)
  MAX_LOSS_R   stop new entries once the day is down this many R (default 3)
  MAX_POS_USD  largest position, in dollars (default 25000)
  SLIP         entry limit above the alert price, as a fraction (default 0.002 = 0.2%)
  ENTRY_TTL    seconds an entry may sit unfilled (default 240)
"""
import os
import time
from datetime import datetime

from .broker import Broker, OrderRejected
from .engine import ET

PREFIX = "bell"
OPEN = {"new", "accepted", "pending_new", "partially_filled", "held", "accepted_for_bidding", "pending_replace"}


def env(name, default):
    v = os.environ.get(name, "")
    return type(default)(v) if v.strip() else default


class Trader:
    def __init__(self, date, risk, broker=None, log=print):
        self.date, self.risk, self.log = date, risk, log
        self.armed = os.environ.get("TRADING", "").lower() == "on"
        self.max_trades = env("MAX_TRADES", 6)
        self.max_loss_r = env("MAX_LOSS_R", 3.0)
        self.max_pos = env("MAX_POS_USD", 25000.0)
        self.slip = env("SLIP", 0.002)
        self.ttl = env("ENTRY_TTL", 240)
        self.b = broker or Broker()
        self.lots = {}      # client order id -> {'sym', 'order'}
        self.halted = None
        self.sync()

    @property
    def mode(self):
        return ("LIVE" if self.b.live else "PAPER") + ("" if self.armed else " · dry run, TRADING is off")

    def cid(self, r):
        return f"{PREFIX}-{self.date}-{r['sym']}-{datetime.fromtimestamp(r['t'], ET):%H%M}"

    def sync(self):
        """Rebuild today's lots from Alpaca, so a restarted run never doubles an order."""
        mine = f"{PREFIX}-{self.date}-"
        for o in self.b.orders(f"{self.date}T04:00:00-04:00"):
            cid = o.get("client_order_id") or ""
            if cid.startswith(mine):
                self.lots[cid] = dict(sym=o["symbol"], order=o)
        return self.lots

    def day_r(self):
        a = self.b.account()
        return (float(a["equity"]) - float(a["last_equity"])) / self.risk

    def on_alert(self, r, late):
        """Act on one Proven alert. Returns a short line for the log."""
        cid = self.cid(r)
        tag = f"{r['sym']} {'ADD' if r.get('add') else 'repeat' if r.get('repeat') else 'first'} {r['g']}"
        if cid in self.lots:
            return f"{tag}: already placed"
        if late:
            return f"{tag}: skipped, alert arrived late"
        if r.get("add"):
            self.raise_stop(r)
        if self.halted:
            return f"{tag}: skipped, {self.halted}"
        if len(self.lots) >= self.max_trades:
            self.halted = f"{self.max_trades} trades today (MAX_TRADES)"
            return f"{tag}: skipped, {self.halted}"
        dr = self.day_r()
        if dr <= -self.max_loss_r:
            self.halted = f"day is {dr:+.1f}R (MAX_LOSS_R {self.max_loss_r:g})"
            return f"{tag}: skipped, {self.halted}"
        limit = r["entry"] * (1 + self.slip)
        R = limit - r["stop"]
        qty = int(min(self.risk // R if R > 0 else 0, self.max_pos // limit))
        if qty < 1:
            return f"{tag}: skipped, size rounds to 0 shares"
        line = f"{tag}: buy {qty} @ ≤{limit:.2f} · stop {r['stop']:.2f} · target {r['tgt']:.2f}"
        if not self.armed:
            self.lots[cid] = dict(sym=r["sym"], order=None)
            return "would " + line
        try:
            o = self.b.bracket(r["sym"], qty, limit, r["stop"], r["tgt"], cid)
        except OrderRejected as e:
            return f"{tag}: rejected by Alpaca, {e}"
        self.lots[cid] = dict(sym=r["sym"], order=o)
        return line

    def raise_stop(self, r):
        """ADD: move the first lot's stop up to r['move_to'] (the desk's rule)."""
        if not self.armed:
            self.log(f"{r['sym']}: would raise first lot's stop {r['move_from']:.2f} -> {r['move_to']:.2f}")
            return
        for o in self.refresh_lots(r["sym"]):
            for leg in o.get("legs") or []:
                if leg.get("type") == "stop" and leg.get("status") in OPEN and float(leg["stop_price"]) < r["move_to"]:
                    try:
                        self.b.replace_stop(leg["id"], r["move_to"])
                        self.log(f"{r['sym']}: first lot's stop raised {leg['stop_price']} -> {r['move_to']:.2f}")
                    except OrderRejected as e:
                        self.log(f"{r['sym']}: stop raise rejected, {e}")
                    return

    def refresh_lots(self, sym=None):
        """Today's bot orders for `sym` (oldest first), fresh from Alpaca."""
        self.sync()
        return [l["order"] for l in self.lots.values() if l["order"] and (sym is None or l["sym"] == sym)]

    def expire_entries(self):
        """Cancel entries still unfilled after ENTRY_TTL; Alpaca drops their exit legs with them."""
        if not self.armed:
            return
        now = time.time()
        for o in self.refresh_lots():
            if o["status"] in ("new", "accepted", "pending_new"):
                placed = datetime.fromisoformat(o["submitted_at"].replace("Z", "+00:00")).timestamp()
                if now - placed > self.ttl:
                    try:
                        self.b.cancel(o["id"])
                        self.log(f"{o['symbol']}: entry not filled in {self.ttl}s, cancelled")
                    except Exception as e:
                        self.log(f"{o['symbol']}: cancel failed, {e}")

    def flatten(self):
        """Close everything the bot opened today; positions you hold yourself are left alone."""
        syms = {l["sym"] for l in self.lots.values()}
        held = self.b.positions()
        if self.armed:
            for o in self.b.orders(f"{self.date}T04:00:00-04:00", status="open"):
                if o["symbol"] in syms:
                    self.b.cancel(o["id"])
            if syms:
                time.sleep(2)  # let the cancels free the shares before selling
        out = []
        for s in sorted(syms & set(held)):
            p = held[s]
            if self.armed:
                self.b.close(s)
            out.append(f"{s}: {'closed' if self.armed else 'would close'} {p['qty']} sh, P&L ${float(p['unrealized_pl']):+,.2f}")
        return out

    def report(self):
        """Today's bot orders and the day's P&L in R."""
        lines = []
        for o in self.refresh_lots():
            legs = {l["type"]: l for l in o.get("legs") or []}
            exit_ = next((f"{k} {l['filled_avg_price']}" for k, l in (("target", legs.get("limit")), ("stop", legs.get("stop")))
                          if l and l.get("status") == "filled"), "")
            lines.append(f"{o['symbol']:6} {o['status']:9} qty {o['filled_qty']}/{o['qty']} @ {o.get('filled_avg_price') or '-'}"
                         + (f" → {exit_}" if exit_ else ""))
        try:
            lines.append(f"Day P&L {self.day_r():+.2f}R")
        except Exception as e:
            lines.append(f"Day P&L unavailable: {e}")
        return lines
