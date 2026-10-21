"""Check the Python engine against the desk's JavaScript on the same bars.

python tests/parity.py <deskDir> <bars.pkl> <daily.json> <screens.json> [ndays]
(bars.pkl / daily.json / screens.json are the desk backtest caches built from Robinhood data.)
"""
import json, pickle, subprocess, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bell_alerts.engine import proven_day, spy_ctx, et_date
from bell_alerts.alpaca import regular

desk, pkl, dly, scr = sys.argv[1:5]
nd = int(sys.argv[5]) if len(sys.argv) > 5 else 15
B0, _ = pickle.load(open(pkl, "rb"))
L = {s: {k: sorted(v.values()) for k, v in m.items()} for s, m in B0.items()}
daily = json.load(open(dly)); screens = json.load(open(scr))
from datetime import datetime
from zoneinfo import ZoneInfo
ET = ZoneInfo("America/New_York")
dump, py = {}, []
for D in sorted(screens)[-nd:]:
    now = int(datetime.fromisoformat(D + "T16:00:00").replace(tzinfo=ET).timestamp())
    lo = now - 8 * 86400
    win = lambda s, k, back: [b for b in L.get(s, {}).get(k, []) if now - back <= b[0] < now]
    spy5 = regular(win("SPY", "5minute", 8 * 86400))
    day = dict(now=now, spy5=spy5, syms={})
    X = spy_ctx(spy5, now, D)
    for s in screens[D]:
        d = dict(b5=regular(win(s, "5minute", 8 * 86400)), m10=win(s, "10minute", 8 * 86400), h1=win(s, "hour", 40 * 86400),
                 h4=win(s, "4hour", 130 * 86400), D=[(b["d"], b["o"], b["h"], b["l"], b["c"], b["v"]) for b in daily.get(s, []) if b["d"] < D][-60:])
        day["syms"][s] = d
        rows, _ = proven_day(s, d["b5"], {"10m": d["m10"], "1h": d["h1"], "4h": d["h4"]}, d["D"], X, now, D)
        py += [dict(date=D, sym=s, t=r["t"], g=r["g"], entry=r["entry"], stop=r["stop"], add=bool(r.get("add")), res=r["res"], r=r["r"]) for r in rows]
    dump[D] = day
f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False); json.dump(dump, f); f.close()
js = json.loads(subprocess.check_output(["node", str(Path(__file__).with_name("desk_parity.js")), desk, f.name]))
key = lambda r: (r["date"], r["sym"], r["t"])
P, Jm = {key(r): r for r in py}, {key(r): r for r in js}
bad = [k for k in set(P) | set(Jm) if k not in P or k not in Jm or any(
    (abs(P[k][f] - Jm[k][f]) > 1e-6 if isinstance(P[k][f], float) else P[k][f] != Jm[k][f]) for f in ("g", "entry", "stop", "add", "res", "r"))]
print(f"{len(dump)} days · python {len(py)} alerts · desk {len(js)} alerts · mismatches {len(bad)}")
for k in sorted(bad)[:10]:
    print(k, P.get(k), Jm.get(k))
sys.exit(1 if bad else 0)
