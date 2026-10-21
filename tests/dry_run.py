"""Exercise main.py end to end with a fake Alpaca that serves cached bars. python tests/dry_run.py <bars.pkl> <daily.json> <date>"""
import json, pickle, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main
from bell_alerts import alpaca
from bell_alerts.engine import et_date

pkl, dly, D = sys.argv[1:4]
B0, _ = pickle.load(open(pkl, "rb"))
L = {s: {k: sorted(v.values()) for k, v in m.items()} for s, m in B0.items()}
daily = json.load(open(dly))
TF = {"5Min": "5minute", "10Min": "10minute", "1Hour": "hour"}
calls = []

class Fake(alpaca.Alpaca):
    def __init__(self): self.feed = "iex"
    def bars(self, symbols, timeframe, start, end=None, feed=None):
        calls.append((timeframe, len(symbols), feed))
        s0, e0 = alpaca._epoch(start), alpaca._epoch(end) if end is not None else 1e12
        out = {}
        for s in symbols:
            if timeframe == "1Day":
                arr = [(int(__import__("datetime").datetime.fromisoformat(b["d"] + "T12:00:00").timestamp()), b["o"], b["h"], b["l"], b["c"], b["v"]) for b in daily.get(s, [])]
            elif timeframe == "30Min":  # build from 5-min bars
                arr = L.get(s, {}).get("5minute", [])
            else:
                arr = L.get(s, {}).get(TF[timeframe], [])
            arr = [b for b in arr if s0 <= b[0] < e0]
            if arr: out[s] = arr
        return out

main.Alpaca = Fake
main.replay(D)
print("\ncalls:", calls)
