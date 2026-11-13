# -*- coding: utf-8 -*-
"""通用波段出場模擬（悲觀日K，與 swing.simulate_signal 同口徑）＋進場特徵。
rule 鍵：stop_pct, target_pct | target_mode in {None,'atr','retrace'} (k/r, tmin, tmax), min_hold_days, max_hold_days,
         trail_pct（純移停）, lock_trail_pct（目標價成地板後移停）, max_hold_locked。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import swing
from indicators import round_tick


def clamp(x, lo, hi): return max(lo, min(hi, x))


def make(S, N, fees, lots, tax, mkt5=None, ew=None):
    def hi60(code, t):
        hs = [x for x in S[code]["h"][max(0, t - 59):t + 1] if x is not None]
        return max(hs) if hs else None

    def atr_pct(code, t, win=20):
        r = S[code]; vals = []
        for j in range(max(1, t - win + 1), t + 1):
            if r["h"][j] is None or r["l"][j] is None or r["c"][j] is None or r["c"][j - 1] is None:
                continue
            tr = max(r["h"][j] - r["l"][j], abs(r["h"][j] - r["c"][j - 1]), abs(r["l"][j] - r["c"][j - 1]))
            vals.append(tr / r["c"][j] * 100)
        return sum(vals) / len(vals) if vals else None

    def features(sig, i):
        code = sig["code"]; r = S[code]; fi = i + 1
        fill = r["o"][fi] if fi < N else None
        c0 = r["c"][i]; cm1 = r["c"][i - 1] if i >= 1 else None
        h60 = hi60(code, i)
        return {"dd60": sig.get("drawdown_pct"), "drop5": sig.get("drop5_pct"), "atr": atr_pct(code, i),
                "ret_sig": (c0 / cm1 - 1) * 100 if (c0 and cm1) else None,
                "mkt5": mkt5[i] if mkt5 else None, "ew": ew[i] * 100 if (ew and ew[i] is not None) else None,
                "gap": (fill / c0 - 1) * 100 if (fill and c0) else None,
                "room": (h60 / fill - 1) * 100 if (fill and h60) else None,
                "volr": sig.get("vol_ratio"), "setup": sig.get("setup")}

    def target_pct_of(rule, code, t, fill, feat=None):
        mode = rule.get("target_mode")
        if mode == "atr":
            a = (feat or {}).get("atr") or atr_pct(code, t)
            return clamp(rule["k"] * a, rule["tmin"], rule["tmax"]) if a else rule["tmin"]
        if mode == "retrace":
            hi = hi60(code, t)
            return clamp(rule["r"] * (hi / fill - 1) * 100, rule["tmin"], rule["tmax"]) if hi else rule["tmin"]
        return rule.get("target_pct")

    def sim(code, t, rule, feat=None):
        r = S[code]; fi = t + 1
        if fi >= N or r["o"][fi] is None or r["c"][fi] is None:
            return None
        lu = swing.limit_up_price(r["c"][t])
        if r["o"][fi] >= lu - 1e-9:
            return {"status": "unfilled"}
        fill = r["o"][fi]
        hard = round_tick(fill * (1 - rule["stop_pct"] / 100), "down"); stop = hard
        tgt_pct = target_pct_of(rule, code, t, fill, feat)
        target = round_tick(fill * (1 + tgt_pct / 100), "up") if tgt_pct else None
        trail = (rule.get("trail_pct") or 0) / 100
        lock_trail = (rule.get("lock_trail_pct") or 0) / 100
        min_hold = rule.get("min_hold_days", 0); max_hold = rule["max_hold_days"]
        max_hold_locked = rule.get("max_hold_locked", max_hold)
        peak = fill; prevc = r["c"][t]; pend = False; locked_mode = False; floor = None
        mfe = 0.0; mae = 0.0; first15 = None
        for j in range(fi, N):
            o, h, l, c = r["o"][j], r["h"][j], r["l"][j], r["c"][j]
            if o is None or c is None:
                continue
            d = j - fi
            exit_px = reason = None
            if first15 is None and h >= fill * 1.15 - 1e-9:
                first15 = d
            if pend:
                exit_px, reason = o, "stop_lock"
            else:
                locked = prevc is not None and c <= swing.limit_down_price(prevc) + 1e-9 and l == c
                cur_stop = max(floor, round_tick(peak * (1 - lock_trail), "down")) if locked_mode else stop
                stop_reason = "trail" if (locked_mode or (trail and stop > hard + 1e-9)) else "stop"
                if d >= 1 and o <= cur_stop:
                    if locked and o == c:
                        pend = True
                    else:
                        exit_px, reason = o, stop_reason
                elif l <= cur_stop:
                    if locked:
                        pend = True
                    else:
                        exit_px, reason = cur_stop, stop_reason
                if exit_px is None and not pend and target is not None and not locked_mode and d >= min_hold and (o >= target or h >= target):
                    if lock_trail and c >= target:
                        locked_mode, floor, peak = True, target, c
                    else:
                        exit_px, reason = (o if o >= target else target), "target"
                if exit_px is None and not pend and d >= (max_hold_locked if locked_mode else max_hold):
                    exit_px, reason = c, "time"
            mfe = max(mfe, h / fill - 1); mae = min(mae, l / fill - 1)
            if exit_px is not None:
                net = swing.swing_net(fill, exit_px, lots, fees, tax)[0]
                return {"status": "closed", "fill": fill, "fill_idx": fi, "exit": exit_px, "exit_idx": j, "reason": reason, "days": d,
                        "ret_pct": round(net / (fill * lots * 1000) * 100, 2), "mfe": mfe * 100, "mae": mae * 100,
                        "first15": first15, "tgt_pct": tgt_pct}
            if trail and not pend:
                peak = max(peak, c); stop = max(stop, round_tick(peak * (1 - trail), "down"))
            elif locked_mode and not pend:
                peak = max(peak, c)
            prevc = c
        return {"status": "open", "fill": fill, "fill_idx": fi, "mfe": mfe * 100, "mae": mae * 100, "first15": first15,
                "tgt_pct": tgt_pct, "locked": locked_mode}

    return sim, features


F = {"stop_pct": 7, "target_pct": 15, "min_hold_days": 5, "max_hold_days": 10}
CANDIDATES = {
    "F 現行 −7/+15@5d/10d": F,
    "F0 +15 從第1日起": {**F, "min_hold_days": 1},
    "F 停利+20": {**F, "target_pct": 20},
    "ATR×4 (8~30%)": {**F, "target_pct": None, "target_mode": "atr", "k": 4, "tmin": 8, "tmax": 30},
    "回撤50% (8~30%)": {**F, "target_pct": None, "target_mode": "retrace", "r": 0.5, "tmin": 8, "tmax": 30},
    "鎖利15%→移停5%/20d": {**F, "lock_trail_pct": 5, "max_hold_locked": 20},
    "鎖利15%@1d→移停5%/20d": {**F, "min_hold_days": 1, "lock_trail_pct": 5, "max_hold_locked": 20},
    "鎖利15%@1d→移停8%/20d": {**F, "min_hold_days": 1, "lock_trail_pct": 8, "max_hold_locked": 20},
    "鎖利回撤50%@1d→移停5%/20d": {**F, "min_hold_days": 1, "target_pct": None, "target_mode": "retrace", "r": 0.5, "tmin": 8, "tmax": 30, "lock_trail_pct": 5, "max_hold_locked": 20},
    "鎖利ATR×4@1d→移停5%/20d": {**F, "min_hold_days": 1, "target_pct": None, "target_mode": "atr", "k": 4, "tmin": 8, "tmax": 30, "lock_trail_pct": 5, "max_hold_locked": 20},
    "T40 純移停12%/40d(−8)": {"stop_pct": 8, "trail_pct": 12, "max_hold_days": 40},
}


def layer2(gate_days, sim, feats, cap, dedup, t1_max_new):
    trades, open_until, last_traded = [], {}, {}
    for i, tier, sigs in gate_days:
        open_until = {c: e for c, e in open_until.items() if e > i}
        free = max(0, cap - len(open_until))
        if tier == "T1" and t1_max_new:
            free = min(free, int(t1_max_new))
        taken = 0
        for s in sigs:
            if taken >= free:
                break
            if s["code"] in open_until or (s["code"] in last_traded and i - last_traded[s["code"]] < dedup):
                continue
            res = sim(s["code"], i, feats.get((i, s["code"])))
            if not res or res["status"] == "unfilled":
                continue
            taken += 1; last_traded[s["code"]] = i
            open_until[s["code"]] = res["exit_idx"] if res["status"] == "closed" else 10 ** 9
            trades.append({"i": i, "tier": tier, "sig": s, "res": res})
    return trades


def summ(trades, label, cal=None):
    from collections import defaultdict
    cl = [t for t in trades if t["res"]["status"] == "closed"]
    if not cl:
        return f"  {label:30} n=0"
    rets = [t["res"]["ret_pct"] for t in cl]; w = [x for x in rets if x > 0]; lo = [x for x in rets if x <= 0]
    pf = sum(w) / -sum(lo) if lo and sum(lo) else 99
    hold = sum(t["res"]["days"] for t in cl) / len(cl)
    reasons = defaultdict(int)
    for t in cl: reasons[t["res"]["reason"]] += 1
    eq = 1.0; peak = 1.0; dd = 0.0
    for t in sorted(cl, key=lambda t: t["res"]["exit_idx"]):
        eq *= 1 + 0.10 * t["res"]["ret_pct"] / 100; peak = max(peak, eq); dd = min(dd, eq / peak - 1)
    ytxt = ""
    if cal:
        yrs = defaultdict(list)
        for t in cl: yrs[cal[t["i"]][:4]].append(t["res"]["ret_pct"])
        ytxt = " ".join(f"{y}:{sum(v)/len(v):+.1f}%" for y, v in sorted(yrs.items()))
    rtxt = "/".join(f"{k[:5]}{v}" for k, v in sorted(reasons.items()))
    return (f"  {label:30} n={len(cl):>4} 均{sum(rets)/len(rets):>+6.2f}% 勝{len(w)/len(cl)*100:>3.0f}% PF{min(pf,99):>5.2f} "
            f"持{hold:>4.1f}d 權益{(eq-1)*100:>+6.1f}% 回撤{dd*100:>+5.1f}% | {ytxt} | {rtxt}")
