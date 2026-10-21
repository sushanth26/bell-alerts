"""Proven rules, ported line for line from the Opening Bell Desk (core.js / desk.js provenDay).

Bars are tuples (t, o, h, l, c, v) with t = bar start in epoch seconds.
Research only, not investment advice.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

EXT = dict(off_low=1.0, target_day=1.6, vwap=1.0, ema12=0.8)
MIN_ATR_PCT = 0.03
FIRST_MIN, LAST_MIN = 9 * 60 + 40, 11 * 60   # alerts on 5-min closes from 9:40 up to (not incl.) 11:00 ET
COOLDOWN = 20 * 60
TARGET_R = 1.5


def et_date(t):
    return datetime.fromtimestamp(t, ET).date().isoformat()


def et_min(t):
    d = datetime.fromtimestamp(t, ET)
    return d.hour * 60 + d.minute


def finished(bars, sec, now):
    return [b for b in bars or [] if b[0] + sec <= now]


def ema_last(closes, n):
    if len(closes) < n:
        return None
    e = sum(closes[:n]) / n
    k = 2 / (n + 1)
    for c in closes[n:]:
        e += k * (c - e)
    return e


def cloud(bars, f, s, live):
    """EMA cloud on finished bars plus one live step at the current price (desk: cloud())."""
    if not bars or len(bars) < s:
        return None
    cl = [b[4] for b in bars]
    ef, es = ema_last(cl, f), ema_last(cl, s)
    if ef is None or es is None:
        return None
    if live is not None:
        ef += 2 / (f + 1) * (live - ef)
        es += 2 / (s + 1) * (live - es)
    return dict(f=ef, s=es, top=max(ef, es), bot=min(ef, es), bull=ef > es)


def atr14(daily):
    """Wilder ATR 14 from daily bars [(d, o, h, l, c, v)] (desk: atr14())."""
    if not daily or len(daily) < 15:
        return None
    tr = [max(daily[i][2] - daily[i][3], abs(daily[i][2] - daily[i - 1][4]), abs(daily[i][3] - daily[i - 1][4]))
          for i in range(1, len(daily))]
    a = sum(tr[:14]) / 14
    for x in tr[14:]:
        a = (a * 13 + x) / 14
    return a


def proven_stop(px, vwap, ema12, atr):
    """Higher of VWAP and 10m EMA12 below price, never closer than 0.5 ATR."""
    c = [x for x in (vwap, ema12) if x is not None and x < px]
    s = max(c) if c else px - 0.5 * atr
    return min(s, px - 0.5 * atr)


def low_vol(atr, px):
    return bool(atr and px and atr / px < MIN_ATR_PCT)


def ext_check(px, lo, vwap, e12, atr):
    r = []
    if not px or not atr:
        return r
    if lo is not None and (px - lo) / atr >= EXT["off_low"]:
        r.append(f"{(px - lo) / atr:.1f} ATR off the day low")
    st = proven_stop(px, vwap, e12, atr)
    tgt = px + 1.5 * (px - st)
    if lo is not None and (tgt - lo) / atr > EXT["target_day"]:
        r.append(f"1.5R target needs a {(tgt - lo) / atr:.1f} ATR day")
    if vwap and (px - vwap) / atr > EXT["vwap"]:
        r.append(f"{(px - vwap) / atr:.1f} ATR above VWAP")
    if e12 and (px - e12) / atr > EXT["ema12"]:
        r.append(f"{(px - e12) / atr:.1f} ATR above 10m EMA12")
    return r


def walk_lot(bars, start_t, entry, stop0, tgt, raises):
    """Walk one lot through later bars; a raised stop applies to bars starting at/after its time."""
    R = entry - stop0
    st, last = stop0, None
    for b in bars:
        if b[0] < start_t:
            continue
        for x in raises or []:
            if b[0] >= x["t"] and x["stop"] > st:
                st = x["stop"]
        if b[3] <= st:
            return ("raised" if st > stop0 else "stop"), (st - entry) / R
        if b[2] >= tgt:
            return "target", (tgt - entry) / R
        last = b
    return None, ((last[4] - entry) / R if last else 0)


def rel_vol(bars, k):
    w = bars[max(0, k - 19):k + 1]
    a = sum(b[5] for b in w) / len(w)
    return bars[k][5] / a if a else None


def spy_ctx(spy5, now, date):
    spy_all = finished(spy5, 300, now)
    idx = {b[0]: i for i, b in enumerate(spy_all)}
    vw, pv, vv = {}, 0.0, 0.0
    for b in spy_all:
        if et_date(b[0]) != date:
            continue
        pv += (b[2] + b[3] + b[4]) / 3 * b[5]
        vv += b[5]
        vw[b[0]] = (pv / vv if vv else b[4], b[4])
    return dict(all=spy_all, idx=idx, vw=vw)


def grade_ok(g):
    return g in ("A+", "A")


def proven_day(sym, bars5, tf, daily, X, now, date, risk=100.0):
    """Every Proven alert `sym` gave on `date` up to `now`.

    bars5: regular-session 5-min bars (today plus a few prior days, for rel vol)
    tf:    {'10m': [...], '1h': [...], '4h': [...]} extended-hours bars
    daily: finished regular-session daily bars before `date`: [(date, o, h, l, c, v)]
    """
    alls = finished(bars5, 300, now)
    td = [b for b in alls if et_date(b[0]) == date]
    off = len(alls) - len(td)
    atr = atr14(daily)
    pdh = daily[-1][2] if daily else None
    rows, blocked = [], []
    if not td or not atr or not pdh:
        return rows, blocked
    pv = vv = 0.0
    lo = float("inf")
    cool = cool_b = -1e15
    first, fs = None, "none"
    for i, b in enumerate(td):
        pv += (b[2] + b[3] + b[4]) / 3 * b[5]
        vv += b[5]
        lo = min(lo, b[3])
        if first and fs == "open" and b[0] >= first["t"]:
            if b[3] <= first.get("cur_stop", first["stop"]):
                fs = "stopped"
            elif b[2] >= first["tgt"]:
                fs = "target"
        t = b[0] + 300
        m = et_min(t)
        if m < FIRST_MIN or m >= LAST_MIN:
            continue
        px = b[4]
        vwap = pv / vv if vv else px
        k, sv = X["idx"].get(b[0]), X["vw"].get(b[0])
        c10 = cloud(finished(tf.get("10m"), 600, t), 5, 12, px)
        c60 = cloud(finished(tf.get("1h"), 3600, t), 34, 50, px)
        c240 = cloud(finished(tf.get("4h"), 14400, t), 34, 50, px)
        if k is None or not sv or not c10 or not c60:
            continue
        r1, r2 = rel_vol(alls, off + i), rel_vol(X["all"], k)
        rvx = r1 / r2 if r1 and r2 else None
        if not (px > vwap and c10["f"] > c10["s"] and px > c60["top"] and rvx is not None and rvx >= 1.5 and px > pdh):
            continue
        dist = (px - c60["top"]) / atr
        g = "B" if dist > 2.5 else ("A+" if c60["bull"] and c240 and c240["bull"] else "A")
        ext = ext_check(px, lo, vwap, c10["s"], atr)
        if low_vol(atr, px):
            ext.append(f"ATR only {atr / px * 100:.1f}% of price (min {MIN_ATR_PCT * 100:.0f}%)")
        stop = proven_stop(px, vwap, c10["s"], atr)
        R = px - stop
        tgt = px + TARGET_R * R
        res, r = "open", None
        later = td[i + 1:]
        for b2 in later:
            if b2[3] <= stop:
                res, r = "stop", -1.0
                break
            if b2[2] >= tgt:
                res, r = "target", TARGET_R
                break
        if r is None:
            r = (later[-1][4] - px) / R if later else 0.0
        row = dict(sym=sym, t=t, g=g, entry=px, stop=stop, tgt=tgt, qty=int(risk // R) if R > 0 else 0,
                   spy_up=sv[1] > sv[0], res=res, r=r, ext=ext, rvx=rvx, atr=atr, vwap=vwap, ema12=c10["s"])
        if grade_ok(g) and not ext:
            if t - cool >= COOLDOWN:
                row["add"] = bool(first) and fs == "open" and px > first["entry"]
                row["repeat"] = bool(first)
                if row["add"]:
                    row["move_from"] = first.get("cur_stop", first["stop"])
                    first["cur_stop"] = max(row["move_from"], row["stop"])
                    row["move_to"] = first["cur_stop"]
                    first.setdefault("raises", []).append(dict(t=t, stop=first["cur_stop"]))
                if not first:
                    first, fs = row, "open"
                rows.append(row)
                cool = t
        elif t - cool_b >= COOLDOWN:
            blocked.append(row)
            cool_b = t
    if first and first.get("raises"):
        how, r = walk_lot(td, first["t"], first["entry"], first["stop"], first["tgt"], first["raises"])
        first["res"], first["r"] = how or "open", r
    for i, r in enumerate(rows):
        r["first"] = i == 0
        r.pop("cur_stop", None)
    return rows, blocked
