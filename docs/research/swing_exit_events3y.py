# -*- coding: utf-8 -*-
"""3 年資料：候選出場規則的逐事件拆解（層一與層二），看改善是否跨事件一致。"""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import swing
from exitsim import make, CANDIDATES, layer2, summ

OLD = Path(__file__).parent   # 需 yahoo3y/*.json（3 年 Yahoo 日K，約 400MB，不在 repo；由 fetch_yahoo3y.py 產生）
cfg = json.load(open(str(Path(__file__).resolve().parents[2] / "config.json"), encoding="utf-8"))
scfg = swing.swing_cfg(cfg)
fees, lots, tax, CAP = cfg["fees"], scfg["lots"], scfg["swing_tax"], scfg["max_positions"]
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


snaps = [{"date": d, "stocks": DayStocks(i), "punish": []} for i, d in enumerate(cal)]
ew, mkt5, _ = swing.compute_gauge(S, snaps, scfg)
sim, features = make(S, N, fees, lots, tax, mkt5, ew)
gate_days = []
for i in range(60, N - 1):
    st, tier = swing.gate_state(mkt5[i], ew[i], scfg["gate"])
    if st != "open": continue
    sigs = swing.detect_signals(S, snaps, i, scfg)
    if tier == "T1" and scfg["gate"].get("t1_setups"): sigs = [s for s in sigs if s["setup"] in scfg["gate"]["t1_setups"]]
    gate_days.append((i, tier, sigs))
feats = {(i, s["code"]): features(s, i) for i, _, sigs in gate_days for s in sigs}
events = []
for i, _, _ in gate_days:
    if events and i - events[-1][1] <= 10: events[-1][1] = i
    else: events.append([i, i])
print(f"3 年：閘門日 {len(gate_days)}、事件 {len(events)}")

L1, L2 = {}, {}
for name, rule in CANDIDATES.items():
    tr = []
    for i, tier, sigs in gate_days:
        for s in sigs:
            res = sim(s["code"], i, rule, feats[(i, s["code"])])
            if res and res["status"] == "closed": tr.append({"i": i, "tier": tier, "sig": s, "res": res})
    L1[name] = tr
    L2[name] = layer2(gate_days, lambda c, i, f, rule=rule: sim(c, i, rule, f), feats, CAP, scfg["dedup_days"], scfg["gate"].get("t1_max_new"))

print("\n=== 層二 總表 ===")
for name in CANDIDATES: print(summ(L2[name], name, cal))

base = "F 現行 −7/+15@5d/10d"
for layer, L in (("層一", L1), ("層二", L2)):
    print(f"\n=== 逐事件（{layer}）：各規則 − F 的單筆均差（百分點）；最後一欄＝改善事件數 ===")
    print(f"  {'事件':22}" + "".join(f"{n[:11]:>13}" for n in CANDIDATES if n != base))
    wins = {n: 0 for n in CANDIDATES}; tot = 0
    for s0, e0 in events:
        rb = [t["res"]["ret_pct"] for t in L[base] if s0 <= t["i"] <= e0]
        if not rb: continue
        tot += 1
        line = f"  {cal[s0]}~{cal[e0][5:]:<10}"
        for name in CANDIDATES:
            if name == base: continue
            rr = [t["res"]["ret_pct"] for t in L[name] if s0 <= t["i"] <= e0]
            if rr:
                dlt = sum(rr) / len(rr) - sum(rb) / len(rb); wins[name] += dlt > 0
                line += f"{dlt:>+10.2f}({len(rr):>2})"
            else: line += f"{'—':>13}"
        print(line + f"   [F n={len(rb)}]")
    print("  改善事件數/總事件：" + "  ".join(f"{n[:11]}={wins[n]}/{tot}" for n in CANDIDATES if n != base))
