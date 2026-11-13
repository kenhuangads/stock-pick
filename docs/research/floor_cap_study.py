# -*- coding: utf-8 -*-
"""鎖利地板上限敏感度：現行（回撤 50%、夾 8~30%、第 1 日起鎖利、移停 8%、鎖利後 20 日）只換上限 15/20/25/30/40/無上限，
另附固定地板 15%／20%。3 年 Yahoo（現行閘門：T1 −13%、T1 只 R2c、≤3）＋官方快照＋模組實際 64 筆反事實。"""
import json, sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import swing, twstock
from exitsim import make, layer2, summ

OLD = Path(__file__).parent   # 需 yahoo3y/*.json（3 年 Yahoo 日K，不在 repo）
cfg = json.load(open(str(Path(__file__).resolve().parents[2] / "config.json"), encoding="utf-8"))
scfg = swing.swing_cfg(cfg)
fees, lots, tax, CAP = cfg["fees"], scfg["lots"], scfg["swing_tax"], scfg["max_positions"]
BASE = {"stop_pct": 7, "target_pct": None, "target_mode": "retrace", "r": 0.5, "tmin": 8, "tmax": 30,
        "min_hold_days": 1, "max_hold_days": 10, "lock_trail_pct": 8, "max_hold_locked": 20}
RULES = {
    "上限15%": {**BASE, "tmax": 15},
    "上限20%": {**BASE, "tmax": 20},
    "上限25%": {**BASE, "tmax": 25},
    "上限30%（現行）": BASE,
    "上限40%": {**BASE, "tmax": 40},
    "無上限": {**BASE, "tmax": 999},
    "固定地板15%": {"stop_pct": 7, "target_pct": 15, "min_hold_days": 1, "max_hold_days": 10, "lock_trail_pct": 8, "max_hold_locked": 20},
    "固定地板20%": {"stop_pct": 7, "target_pct": 20, "min_hold_days": 1, "max_hold_days": 10, "lock_trail_pct": 8, "max_hold_locked": 20},
}
CUR = "上限30%（現行）"


def lockrate(trades):
    cl = [t for t in trades if t["res"]["status"] == "closed"]
    if not cl: return 0, 0, 0
    reached = sum(1 for t in cl if t["res"]["mfe"] >= t["res"]["tgt_pct"] - 1e-9) if all(t["res"].get("tgt_pct") for t in cl) else 0
    floors = sorted(t["res"]["tgt_pct"] for t in cl if t["res"].get("tgt_pct"))
    at_cap = sum(1 for t in cl if t["res"].get("tgt_pct") and t["res"]["tgt_pct"] >= 29.99)
    return reached / len(cl) * 100, floors[len(floors) // 2] if floors else 0, at_cap / len(cl) * 100


def gate_days_of(S, snaps, N, ew, mkt5):
    out = []
    for i in range(60, N - 1):
        st, tier = swing.gate_state(mkt5[i], ew[i], scfg["gate"])
        if st != "open": continue
        sigs = swing.detect_signals(S, snaps, i, scfg)
        if tier == "T1" and scfg["gate"].get("t1_setups"):
            sigs = [s for s in sigs if s["setup"] in scfg["gate"]["t1_setups"]]
        out.append((i, tier, sigs))
    return out


def run_all(label, S, snaps, cal, N, ew, mkt5):
    sim, features = make(S, N, fees, lots, tax, mkt5, ew)
    gd = gate_days_of(S, snaps, N, ew, mkt5)
    feats = {(i, s["code"]): features(s, i) for i, _, sigs in gd for s in sigs}
    events = []
    for i, _, _ in gd:
        if events and i - events[-1][1] <= 10: events[-1][1] = i
        else: events.append([i, i])
    print(f"\n==================== {label}：閘門日 {len(gd)}、事件 {len(events)} ====================")
    L1, L2 = {}, {}
    for name, rule in RULES.items():
        tr = []
        for i, tier, sigs in gd:
            for s in sigs:
                res = sim(s["code"], i, rule, feats[(i, s["code"])])
                if res and res["status"] == "closed": tr.append({"i": i, "tier": tier, "sig": s, "res": res})
        L1[name] = tr
        L2[name] = layer2(gd, lambda c, i, f, rule=rule: sim(c, i, rule, f), feats, CAP, scfg["dedup_days"], scfg["gate"].get("t1_max_new"))
    print("--- 層一（閘門日全部訊號）---")
    for name in RULES:
        lr, med, cap = lockrate(L1[name])
        print(summ(L1[name], name, cal) + f" | 摸到地板 {lr:.0f}%・地板中位 {med:.1f}%・夾到上限 {cap:.0f}%")
    print("--- 層二（槽位實際取用）---")
    for name in RULES:
        lr, med, cap = lockrate(L2[name])
        print(summ(L2[name], name, cal) + f" | 摸到地板 {lr:.0f}%")
    for lay, L in (("層一", L1), ("層二", L2)):
        print(f"--- 逐事件（{lay}）：各規則 − 現行 的單筆均差（百分點）---")
        wins = defaultdict(int); tot = 0
        for s0, e0 in events:
            rb = [t["res"]["ret_pct"] for t in L[CUR] if s0 <= t["i"] <= e0 and t["res"]["status"] == "closed"]
            if not rb: continue
            tot += 1
            line = f"  {cal[s0]}~{cal[e0][5:]:<6}"
            for name in RULES:
                if name == CUR: continue
                rr = [t["res"]["ret_pct"] for t in L[name] if s0 <= t["i"] <= e0 and t["res"]["status"] == "closed"]
                if rr:
                    d = sum(rr) / len(rr) - sum(rb) / len(rb); wins[name] += d > 0
                    line += f" {name[:6]}{d:>+6.2f}"
            print(line + f"  [現行 n={len(rb)} 均{sum(rb)/len(rb):+.2f}%]")
        print("  改善事件數：" + "  ".join(f"{n}={wins[n]}/{tot}" for n in RULES if n != CUR))
    return sim, features


# ---------- 3 年 ----------
raw = {f.stem: json.loads(f.read_text(encoding="utf-8")) for f in sorted((OLD / "yahoo3y").glob("*.json"))}
cal = sorted({d for r in raw.values() for d in r["d"] if d >= "2023-10-01"}); di = {d: i for i, d in enumerate(cal)}; N = len(cal)
S, V = {}, {}
for code, r in raw.items():
    o = [None] * N; h = [None] * N; l = [None] * N; c = [None] * N; a = [None] * N; v = [None] * N
    for k, d in enumerate(r["d"]):
        i = di.get(d)
        if i is None: continue
        o[i], h[i], l[i], c[i], v[i] = r["o"][k], r["h"][k], r["l"][k], r["c"][k], r["v"][k]; a[i] = r["c"][k] * r["v"][k]
    cs, cn, acc, cnt = [0.0] * (N + 1), [0] * (N + 1), 0.0, 0
    for i in range(N):
        if a[i] is not None: acc += a[i]; cnt += 1
        cs[i + 1], cn[i + 1] = acc, cnt
    S[code] = {"n": r.get("n", code), "m": r.get("m"), "o": o, "h": h, "l": l, "c": c, "a": a, "cs": cs, "cn": cn}; V[code] = v


class DayStocks:
    def __init__(self, i): self.i = i
    def get(self, code, default=None):
        s = S.get(code)
        if not s or s["c"][self.i] is None: return default
        return {"c": s["c"][self.i], "v": V[code][self.i]}
    def items(self):
        for code, s in S.items():
            if s["c"][self.i] is not None: yield code, {"c": s["c"][self.i], "v": V[code][self.i]}
    def __contains__(self, code): return code in S


snaps3 = [{"date": d, "stocks": DayStocks(i), "punish": []} for i, d in enumerate(cal)]
ew3, mkt53, _ = swing.compute_gauge(S, snaps3, scfg)
run_all("3 年 Yahoo（2023-10～2026-10-21）", S, snaps3, cal, N, ew3, mkt53)

# ---------- 官方 ----------
snaps = twstock.load_snapshots(); dates = [s["date"] for s in snaps]
So = swing.build_series(snaps)
ewo, mkt5o, _ = swing.compute_gauge(So, snaps, scfg)
sim_o, feat_o = run_all("官方快照（2026-02～最新；尚未出場者不計）", So, snaps, dates, len(dates), ewo, mkt5o)

# ---------- 模組實際取用、已平倉者：逐筆反事實 ----------
L = json.load(open(str(Path(__file__).resolve().parents[2] / "data" / "swing" / "ledger.json"), encoding="utf-8"))
dix = {d: i for i, d in enumerate(dates)}
taken = [(dix[d], s) for d, rec in L["days"].items() if d in dix for s in rec.get("signals", []) if s.get("taken")]
print(f"\n==================== 模組實際取用 {len(taken)} 筆（只算各規則下已平倉者）====================")
for name, rule in RULES.items():
    rs = [sim_o(s["code"], i, rule, feat_o(s, i)) for i, s in taken]
    cl = [r for r in rs if r and r["status"] == "closed"]
    if not cl: continue
    tot = sum(r["ret_pct"] for r in cl); hold = sum(r["days"] for r in cl) / len(cl)
    reached = sum(1 for r in cl if r.get("tgt_pct") and r["mfe"] >= r["tgt_pct"] - 1e-9)
    print(f"  {name:12} 平倉 {len(cl):>3} 合計 {tot:>+7.1f}% 單筆均 {tot/len(cl):>+6.2f}% 勝率 {sum(r['ret_pct']>0 for r in cl)/len(cl)*100:>3.0f}% 持有 {hold:>4.1f} 日 摸到地板 {reached}")
