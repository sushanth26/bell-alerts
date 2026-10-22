"""Alpaca trading API (https://docs.alpaca.markets/reference/postorder). Standard library only.

Paper account by default. Live trading needs the repository variable ALPACA_LIVE=yes *and* live keys in
TRADE_KEY_ID / TRADE_SECRET_KEY: both are settings you change yourself.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"


class OrderRejected(RuntimeError):
    pass


class Broker:
    def __init__(self, key=None, secret=None, live=None):
        self.key = key or os.environ["TRADE_KEY_ID"]
        self.secret = secret or os.environ["TRADE_SECRET_KEY"]
        self.live = (os.environ.get("ALPACA_LIVE", "").lower() == "yes") if live is None else live
        self.base = LIVE_URL if self.live else PAPER_URL

    def _req(self, method, path, body=None, params=None):
        url = f"{self.base}{path}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "APCA-API-KEY-ID": self.key, "APCA-API-SECRET-KEY": self.secret, "Content-Type": "application/json"})
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    txt = r.read().decode()
                    return json.loads(txt) if txt else None
            except urllib.error.HTTPError as e:
                msg = e.read().decode()[:300]
                if e.code == 429 or e.code >= 500:
                    time.sleep(1 + attempt * 2)
                    continue
                if e.code in (403, 422):  # rejected order: duplicate id, buying power, bad price ...
                    raise OrderRejected(f"{e.code}: {msg}") from None
                raise RuntimeError(f"Alpaca trading {e.code}: {msg}") from None
            except urllib.error.URLError:
                time.sleep(1 + attempt * 2)
        raise RuntimeError(f"Alpaca trading: no response for {method} {path}")

    def account(self):
        return self._req("GET", "/v2/account")

    def orders(self, after_iso, status="all"):
        return self._req("GET", "/v2/orders", params=dict(status=status, after=after_iso, limit=500, nested="true", direction="asc")) or []

    def positions(self):
        return {p["symbol"]: p for p in self._req("GET", "/v2/positions") or []}

    def bracket(self, sym, qty, limit, stop, target, client_id):
        """Buy `qty` with a limit, a stop-loss and a take-profit attached. Alpaca holds the exits server-side."""
        return self._req("POST", "/v2/orders", dict(
            symbol=sym, qty=str(qty), side="buy", type="limit", limit_price=price(limit), time_in_force="day",
            order_class="bracket", client_order_id=client_id,
            take_profit=dict(limit_price=price(target)), stop_loss=dict(stop_price=price(stop))))

    def replace_stop(self, order_id, stop):
        return self._req("PATCH", f"/v2/orders/{order_id}", dict(stop_price=price(stop)))

    def cancel(self, order_id):
        return self._req("DELETE", f"/v2/orders/{order_id}")

    def close(self, sym):
        """Cancel the symbol's open orders, then sell the position at market."""
        return self._req("DELETE", f"/v2/positions/{urllib.parse.quote(sym)}", params=dict(percentage="100"))


def price(x):
    """Alpaca takes pennies at or above $1 and up to 4 decimals below."""
    return f"{x:.2f}" if x >= 1 else f"{x:.4f}"
