# -*- coding: utf-8 -*-
"""波段出場研究（3 年 Yahoo 日K × swing 引擎原樣的閘門／訊號／排序／槽位）：
Q1 第 5 日前不能停利的代價；Q2 固定 +15% 的必要性；Q3 高點是否可由進場時特徵推估（ATR／回撤深度／距前高）；
並比較「固定停利」「波動縮放停利」「回撤比例停利」「+15% 鎖利後移停」「純移停」等出場規則。"""
import json, sys, math
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import swing
from indicators import round_tick

OLD = Path(__file__).parent   # 需 yahoo3y/*.json（3 年 Yahoo 日K，約 400MB，不在 repo；由 fetch_yahoo3y.py 產生）
HERE = Path(__file__).parent
cfg = json.load(open(str(Path(__file__).resolve().parents[2] / "config.json"), encoding="utf-8"))
scfg = swing.swing_cfg(cfg)
fees, lots, tax, CAP = cfg["fees"], scfg["lots"], scfg["swing_tax"], scfg["max_positions"]
DEDUP = scfg["dedup_days"]

# ---------- 資料：yahoo3y → 引擎結構（同 gate_variants_3y） ----------
raw = {f.stem: json.loads(f.read_text(encoding="utf-8")) for f in sorted((OLD / "yahoo3y").glob("*.json"))}
cal = sorted({d for r in raw.values() for d in r["d"] if d >= "2023-10-01"}); di = {d: i for i, d in enumerate(cal)}; N = len(cal)
S, V = {}, {}
for code, r in raw.items():
    o = [None] * N; h = [None] * N; l = [None] * N; c = [None] * N; a = [None] * N; v = [None] * N
    for k, d in enumerate(r["d"]):
        i = di.get(d)
        if i is None:
            continue
        o[i], h[i], l[i], c[i], v[i] = r["o"][k], r["h"][k], r["l"][k], r["c"][k], r["v"][k]
        a[i] = r["c"][k] * r["v"][k]
    cs, cn, acc, cnt = [0.0] * (N + 1), [0] * (N + 1), 0.0, 0
    for i in range(N):
        if a[i] is not None:
            acc += a[i]; cnt += 1
        cs[i + 1], cn[i + 1] = acc, cnt
    S[code] = {"n": r.get("n", code), "m": r.get("m"), "o": o, "h": h, "l": l, "c": c, "a": a, "cs": cs, "cn": cn}
    V[code] = v


class DayStocks:
    def __init__(self, i): self.i = i
    def get(self, code, default=None):
        s = S.get(code)
        if not s or s["c"][self.i] is None: return default
        return {"c": s["c"][self.i], "v": V[code][self.i]}
    def items(self):
        for code, s in S.items():
            if s["c"][self.i] is not None:
                yield code, {"c": s["c"][self.i], "v": V[code][self.i]}
    def __contains__(self, code): return code in S


snaps = [{"date": d, "stocks": DayStocks(i), "punish": []} for i, d in enumerate(cal)]
ew, mkt5, _ = swing.compute_gauge(S, snaps, scfg)
print(f"三年資料：{len(S)} 檔、{N} 日（{cal[0]}~{cal[-1]}）")

gate_days = []
for i in range(60, N - 1):
    st, tier = swing.gate_state(mkt5[i], ew[i], scfg["gate"])
    if st != "open":
        continue
    sigs = swing.detect_signals(S, snaps, i, scfg)
    if tier == "T1" and scfg["gate"].get("t1_setups"):
        sigs = [s for s in sigs if s["setup"] in scfg["gate"]["t1_setups"]]
    gate_days.append((i, tier, sigs))
print(f"閘門日 {len(gate_days)}（T2 {sum(t=='T2' for _, t, _ in gate_days)}）、訊號 {sum(len(s) for *_, s in gate_days)} 筆")


# ---------- 進場時特徵 ----------
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
    fill = r["o"][fi]
    c0 = r["c"][i]; cm1 = r["c"][i - 1] if i >= 1 else None
    return {"dd60": sig.get("drawdown_pct"), "drop5": sig.get("drop5_pct"), "atr": atr_pct(code, i),
            "ret_sig": (c0 / cm1 - 1) * 100 if cm1 else None, "mkt5": mkt5[i], "ew": ew[i] * 100 if ew[i] is not None else None,
            "gap": (fill / c0 - 1) * 100 if fill and c0 else None,
            "room": (hi60(code, i) / fill - 1) * 100 if fill and hi60(code, i) else None,
            "volr": sig.get("vol_ratio"), "setup": sig.get("setup")}


# ---------- 通用出場模擬（悲觀日K；F 規則與 swing.simulate_signal 完全同口徑） ----------
def clamp(x, lo, hi): return max(lo, min(hi, x))


def sim(code, t, rule, feat=None):
    r = S[code]; fi = t + 1
    if fi >= N or r["o"][fi] is None or r["c"][fi] is None:
        return None
    lu = swing.limit_up_price(r["c"][t])
    if r["o"][fi] >= lu - 1e-9:
        return {"status": "unfilled"}
    fill = r["o"][fi]
    hard = round_tick(fill * (1 - rule["stop_pct"] / 100), "down")
    stop = hard
    tgt_pct = rule.get("target_pct")
    mode = rule.get("target_mode")
    if mode == "atr":
        a = (feat or {}).get("atr") or atr_pct(code, t)
        tgt_pct = clamp(rule["k"] * a, rule["tmin"], rule["tmax"]) if a else rule["tmin"]
    elif mode == "retrace":
        hi = hi60(code, t)
        tgt_pct = clamp(rule["r"] * (hi / fill - 1) * 100, rule["tmin"], rule["tmax"]) if hi else rule["tmin"]
    target = round_tick(fill * (1 + tgt_pct / 100), "up") if tgt_pct else None
    trail = (rule.get("trail_pct") or 0) / 100
    lock_trail = (rule.get("lock_trail_pct") or 0) / 100
    min_hold = rule.get("min_hold_days", 0); max_hold = rule["max_hold_days"]
    max_hold_locked = rule.get("max_hold_locked", max_hold)
    peak = fill; prevc = r["c"][t]; pend = False; locked_mode = False; floor = None
    mfe = 0.0; mae = 0.0; first15 = None; path = []
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
                    locked_mode, floor, peak = True, target, c   # 鎖利：目標價成地板，之後追蹤收盤高點回落出場
                else:
                    exit_px, reason = (o if o >= target else target), "target"
            if exit_px is None and not pend and d >= (max_hold_locked if locked_mode else max_hold):
                exit_px, reason = c, "time"
        mfe = max(mfe, h / fill - 1); mae = min(mae, l / fill - 1)
        path.append((h / fill - 1, l / fill - 1, c / fill - 1))
        if exit_px is not None:
            net = swing.swing_net(fill, exit_px, lots, fees, tax)[0]
            return {"status": "closed", "fill": fill, "fill_idx": fi, "exit": exit_px, "exit_idx": j, "reason": reason, "days": d,
                    "ret_pct": round(net / (fill * lots * 1000) * 100, 2), "mfe": mfe * 100, "mae": mae * 100,
                    "first15": first15, "path": path, "tgt_pct": tgt_pct}
        if trail and not pend:
            peak = max(peak, c); stop = max(stop, round_tick(peak * (1 - trail), "down"))
        elif locked_mode and not pend:
            peak = max(peak, c)
        prevc = c
    return {"status": "open", "fill": fill, "fill_idx": fi, "mfe": mfe * 100, "mae": mae * 100, "first15": first15, "path": path}


F = {"stop_pct": 7, "target_pct": 15, "min_hold_days": 5, "max_hold_days": 10}
RULES = {
    "F 現行 −7/+15@5d/10d": F,
    "F0 +15 從第1日起": {**F, "min_hold_days": 1},
    "F3 +15 從第3日起": {**F, "min_hold_days": 3},
    "F 停利+10": {**F, "target_pct": 10},
    "F 停利+20": {**F, "target_pct": 20},
    "F 停利+25": {**F, "target_pct": 25},
    "F 時限15日": {**F, "max_hold_days": 15},
    "F 時限20日": {**F, "max_hold_days": 20},
    "ATR×2 (8~30%)": {**F, "target_pct": None, "target_mode": "atr", "k": 2, "tmin": 8, "tmax": 30},
    "ATR×3 (8~30%)": {**F, "target_pct": None, "target_mode": "atr", "k": 3, "tmin": 8, "tmax": 30},
    "ATR×4 (8~30%)": {**F, "target_pct": None, "target_mode": "atr", "k": 4, "tmin": 8, "tmax": 30},
    "回撤38% (8~30%)": {**F, "target_pct": None, "target_mode": "retrace", "r": 0.382, "tmin": 8, "tmax": 30},
    "回撤50% (8~30%)": {**F, "target_pct": None, "target_mode": "retrace", "r": 0.5, "tmin": 8, "tmax": 30},
    "回撤62% (8~30%)": {**F, "target_pct": None, "target_mode": "retrace", "r": 0.618, "tmin": 8, "tmax": 30},
    "鎖利15%→移停5%/20d": {**F, "lock_trail_pct": 5, "max_hold_locked": 20},
    "鎖利15%→移停8%/20d": {**F, "lock_trail_pct": 8, "max_hold_locked": 20},
    "鎖利15%→移停8%/30d": {**F, "lock_trail_pct": 8, "max_hold_locked": 30},
    "鎖利15%@1d→移停8%/20d": {**F, "min_hold_days": 1, "lock_trail_pct": 8, "max_hold_locked": 20},
    "T20 純移停10%/20d(−8)": {"stop_pct": 8, "trail_pct": 10, "max_hold_days": 20},
    "T40 純移停12%/40d(−8)": {"stop_pct": 8, "trail_pct": 12, "max_hold_days": 40},
}


def summ(trades, label, show_years=True):
    cl = [t for t in trades if t["res"]["status"] == "closed"]
    if not cl:
        return f"  {label:28} n=0"
    rets = [t["res"]["ret_pct"] for t in cl]; w = [x for x in rets if x > 0]; lo = [x for x in rets if x <= 0]
    pf = sum(w) / -sum(lo) if lo and sum(lo) else 99
    hold = sum(t["res"]["days"] for t in cl) / len(cl)
    reasons = defaultdict(int)
    for t in cl: reasons[t["res"]["reason"]] += 1
    eq = 1.0; peak = 1.0; dd = 0.0
    for t in sorted(cl, key=lambda t: t["res"]["exit_idx"]):
        eq *= 1 + 0.10 * t["res"]["ret_pct"] / 100; peak = max(peak, eq); dd = min(dd, eq / peak - 1)
    yrs = defaultdict(list)
    for t in cl: yrs[cal[t["i"]][:4]].append(t["res"]["ret_pct"])
    ytxt = " ".join(f"{y}:{sum(v)/len(v):+.1f}%" for y, v in sorted(yrs.items())) if show_years else ""
    rtxt = "/".join(f"{k[:5]}{v}" for k, v in sorted(reasons.items()))
    return (f"  {label:28} n={len(cl):>4} 均{sum(rets)/len(rets):>+6.2f}% 勝{len(w)/len(cl)*100:>3.0f}% PF{min(pf,99):>5.2f} "
            f"持{hold:>4.1f}d 日均{sum(rets)/len(cl)/hold:>+5.2f}% 權益{(eq-1)*100:>+7.1f}% 回撤{dd*100:>+5.1f}% | {ytxt} | {rtxt}")


# ---------- 驗證：F 與引擎一致 ----------
mis = 0; tot = 0
for i, tier, sigs in gate_days:
    for s in sigs:
        a = sim(s["code"], i, F); b = swing.simulate_signal(S, s["code"], i, scfg["exit"], fees, lots, tax)
        if a is None or a["status"] != "closed" or b["status"] != "closed":
            continue
        tot += 1
        if abs(a["ret_pct"] - b["ret_pct"]) > 0.011 or a["reason"] != b["reason"]:
            mis += 1
print(f"[check] 通用模擬 vs 引擎 F：{tot} 筆比對、不一致 {mis} 筆")

# ---------- 層一：全部訊號 ----------
feats = {}
print("\n=== 層一：閘門日全部訊號（統計力；年別＝單筆均；日均＝單筆均/持有日）===")
L1 = {}
for name, rule in RULES.items():
    trades = []
    for i, tier, sigs in gate_days:
        for s in sigs:
            key = (i, s["code"])
            if key not in feats:
                feats[key] = features(s, i)
            res = sim(s["code"], i, rule, feats[key])
            if res and res["status"] == "closed":
                trades.append({"i": i, "tier": tier, "sig": s, "res": res, "f": feats[key]})
    L1[name] = trades
    print(summ(trades, name))

# ---------- 層二：槽位取用（每個規則自己的持倉動態；cap 10、T1 新進場 ≤3、10 日去重）----------
print("\n=== 層二：槽位實際取用（上限 10、持倉佔槽、T1 日新進場 ≤3；年別＝單筆均）===")
for name, rule in RULES.items():
    trades, open_until, last_traded = [], {}, {}
    for i, tier, sigs in gate_days:
        open_until = {c: e for c, e in open_until.items() if e > i}
        free = max(0, CAP - len(open_until))
        if tier == "T1" and scfg["gate"].get("t1_max_new"):
            free = min(free, int(scfg["gate"]["t1_max_new"]))
        taken = 0
        for s in sigs:
            if taken >= free:
                break
            if s["code"] in open_until or (s["code"] in last_traded and i - last_traded[s["code"]] < DEDUP):
                continue
            res = sim(s["code"], i, rule, feats.get((i, s["code"])))
            if not res or res["status"] != "closed":
                continue
            taken += 1; last_traded[s["code"]] = i; open_until[s["code"]] = res["exit_idx"]
            trades.append({"i": i, "tier": tier, "sig": s, "res": res})
    print(summ(trades, name))

# ---------- Q1：+15% 何時被觸及、第 5 日前觸及的單後來怎樣 ----------
base = L1["F 現行 −7/+15@5d/10d"]
print("\n=== Q1：+15% 首次觸及日分佈（F 層一）===")
cnt = defaultdict(int)
for t in base:
    f15 = t["res"]["first15"]
    cnt["never(10d內)" if (f15 is None or f15 > 10) else f"d{f15}"] += 1
n = len(base)
print("  " + "  ".join(f"{k}:{v}({v/n*100:.0f}%)" for k, v in sorted(cnt.items(), key=lambda x: (x[0] == "never(10d內)", x[0]))))
early = [t for t in base if t["res"]["first15"] is not None and t["res"]["first15"] < 5]
if early:
    er = [t["res"]["ret_pct"] for t in early]; reasons = defaultdict(int)
    for t in early: reasons[t["res"]["reason"]] += 1
    print(f"  第 5 日前已觸及 +15% 的 {len(early)} 筆，在 F 規則下最終：均 {sum(er)/len(er):+.2f}%、勝 {sum(x>0 for x in er)/len(er)*100:.0f}%、出場 {dict(reasons)}")
    print(f"    其中最終報酬 <15% 者 {sum(x < 15 for x in er)} 筆（{sum(x<15 for x in er)/len(er)*100:.0f}%）、<0 者 {sum(x<=0 for x in er)} 筆——這就是「第 5 日前不能賣」的代價")

# ---------- Q2：高點分佈（10 日內 MFE）----------
mfes = sorted(t["res"]["mfe"] for t in base)
q = lambda p: mfes[int(p * (len(mfes) - 1))]
print(f"\n=== Q2：10 日內最高價（MFE）分佈（F 層一 n={len(mfes)}）===")
print(f"  P10 {q(.1):+.1f}%  P25 {q(.25):+.1f}%  中位 {q(.5):+.1f}%  P75 {q(.75):+.1f}%  P90 {q(.9):+.1f}%  均 {sum(mfes)/len(mfes):+.1f}%")
print(f"  MFE ≥10%: {sum(m>=10 for m in mfes)/len(mfes)*100:.0f}%  ≥15%: {sum(m>=15 for m in mfes)/len(mfes)*100:.0f}%  ≥20%: {sum(m>=20 for m in mfes)/len(mfes)*100:.0f}%  ≥30%: {sum(m>=30 for m in mfes)/len(mfes)*100:.0f}%")


# ---------- Q3：高點可預測性 ----------
def spearman(xs, ys):
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pairs) < 10:
        return None, len(pairs)
    def ranks(v):
        order = sorted(range(len(v)), key=lambda k: v[k]); rk = [0] * len(v)
        k = 0
        while k < len(v):
            j = k
            while j + 1 < len(v) and v[order[j + 1]] == v[order[k]]: j += 1
            for m in range(k, j + 1): rk[order[m]] = (k + j) / 2
            k = j + 1
        return rk
    rx, ry = ranks([p[0] for p in pairs]), ranks([p[1] for p in pairs])
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry)); vx = sum((a - mx) ** 2 for a in rx); vy = sum((b - my) ** 2 for b in ry)
    return (cov / math.sqrt(vx * vy) if vx and vy else 0.0), len(pairs)


print("\n=== Q3：進場時特徵 vs 10 日內高點（Spearman ρ；F 層一）===")
FEATS = [("dd60", "60日回撤深度%"), ("drop5", "5日跌幅%"), ("atr", "ATR20%(波動)"), ("ret_sig", "訊號日漲幅%"),
         ("mkt5", "市場5日%"), ("ew", "市場當日%"), ("gap", "進場跳空%"), ("room", "距60日高(空間)%"), ("volr", "量比")]
mfe_all = [t["res"]["mfe"] for t in base]
for key, label in FEATS:
    xs = [t["f"].get(key) for t in base]
    rho, m = spearman(xs, mfe_all)
    rho_ret, _ = spearman(xs, [t["res"]["ret_pct"] for t in base])
    rho_hit, _ = spearman(xs, [1 if t["res"]["mfe"] >= 15 else 0 for t in base])
    if rho is None:
        continue
    print(f"  {label:16} ρ(MFE)={rho:+.2f}  ρ(F報酬)={rho_ret:+.2f}  ρ(達15%)={rho_hit:+.2f}  n={m}")

print("\n=== Q3：依特徵三分位看 MFE 與「哪個固定停利最好」（F 層一，停利 10/15/20/25 的單筆均）===")
alts = {k: {t["i"] * 100000 + int(t["sig"]["code"].ljust(6, "0")[:6], 36) if False else (t["i"], t["sig"]["code"]): t["res"]["ret_pct"] for t in L1[k]}
        for k in ("F 停利+10", "F 現行 −7/+15@5d/10d", "F 停利+20", "F 停利+25", "鎖利15%→移停8%/20d")}
for key, label in (("atr", "ATR20%"), ("dd60", "60日回撤%"), ("room", "距60日高%"), ("mkt5", "市場5日%")):
    rows = [t for t in base if t["f"].get(key) is not None]
    rows.sort(key=lambda t: t["f"][key])
    k3 = len(rows) // 3
    print(f"  [{label}]")
    for name, part in (("低", rows[:k3]), ("中", rows[k3:2 * k3]), ("高", rows[2 * k3:])):
        if not part: continue
        vals = [t["f"][key] for t in part]; m = [t["res"]["mfe"] for t in part]
        line = f"    {name}（{min(vals):+.1f}~{max(vals):+.1f}）n={len(part):>4} MFE中位{sorted(m)[len(m)//2]:>+5.1f}% 達15%{sum(x>=15 for x in m)/len(m)*100:>3.0f}% |"
        for an, amap in alts.items():
            rr = [amap[(t["i"], t["sig"]["code"])] for t in part if (t["i"], t["sig"]["code"]) in amap]
            line += f" {an.split()[1] if an.startswith('F ') else '鎖利8%'}:{sum(rr)/len(rr):>+5.2f}"
        print(line)

json.dump({name: [{"i": t["i"], "code": t["sig"]["code"], "ret": t["res"]["ret_pct"], "days": t["res"]["days"], "reason": t["res"]["reason"]} for t in tr]
           for name, tr in L1.items()}, open(HERE / "swing_exit_study_l1.json", "w", encoding="utf-8"))
