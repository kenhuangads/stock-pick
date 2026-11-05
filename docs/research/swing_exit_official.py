# -*- coding: utf-8 -*-
"""官方快照（data/history）上的波段出場 A/B：同一組閘門日／訊號／槽位，只換出場規則；含逐事件拆解與實際 ledger 單的 +15% 首觸日。"""
import json, sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import twstock, swing
from exitsim import make, CANDIDATES, layer2, summ

cfg = json.load(open(str(Path(__file__).resolve().parents[2] / "config.json"), encoding="utf-8"))
scfg = swing.swing_cfg(cfg)
snaps = twstock.load_snapshots(); dates = [s["date"] for s in snaps]; N = len(snaps)
S = swing.build_series(snaps)
ew, mkt5, _ = swing.compute_gauge(S, snaps, scfg)
fees, lots, tax, CAP = cfg["fees"], scfg["lots"], scfg["swing_tax"], scfg["max_positions"]
sim, features = make(S, N, fees, lots, tax, mkt5, ew)

gate_days = []
for i in range(60, N - 1):
    st, tier = swing.gate_state(mkt5[i], ew[i], scfg["gate"])
    if st != "open":
        continue
    sigs = swing.detect_signals(S, snaps, i, scfg)
    if tier == "T1" and scfg["gate"].get("t1_setups"):
        sigs = [s for s in sigs if s["setup"] in scfg["gate"]["t1_setups"]]
    gate_days.append((i, tier, sigs))
print(f"官方快照 {N} 日（{dates[0]}~{dates[-1]}）；閘門日 {len(gate_days)}：" + ", ".join(f"{dates[i]}({t},{len(sg)})" for i, t, sg in gate_days))
feats = {(i, s["code"]): features(s, i) for i, _, sigs in gate_days for s in sigs}

events = []
for i, _, _ in gate_days:
    if events and i - events[-1][1] <= 10: events[-1][1] = i
    else: events.append([i, i])

print("\n=== 層一：閘門日全部訊號 ===")
L1 = {}
for name, rule in CANDIDATES.items():
    tr = []
    for i, tier, sigs in gate_days:
        for s in sigs:
            res = sim(s["code"], i, rule, feats[(i, s["code"])])
            if res and res["status"] == "closed":
                tr.append({"i": i, "tier": tier, "sig": s, "res": res})
    L1[name] = tr
    print(summ(tr, name, dates))

print("\n=== 層二：槽位實際取用（上限 10、T1 新進場 ≤3、10 日去重）===")
L2 = {}
for name, rule in CANDIDATES.items():
    tr = layer2(gate_days, lambda c, i, f, rule=rule: sim(c, i, rule, f), feats, CAP, scfg["dedup_days"], scfg["gate"].get("t1_max_new"))
    L2[name] = tr
    print(summ(tr, name, dates))

print("\n=== 逐事件（層一 單筆均；事件＝相鄰 ≤10 日的閘門日群）===")
hdr = f"  {'事件':22}" + "".join(f"{n[:12]:>14}" for n in CANDIDATES)
print(hdr)
for s0, e0 in events:
    line = f"  {dates[s0]}~{dates[e0][5:]:<10}"
    for name in CANDIDATES:
        rr = [t["res"]["ret_pct"] for t in L1[name] if s0 <= t["i"] <= e0]
        line += f"{(sum(rr)/len(rr)):>+9.2f}%({len(rr):>3})" if rr else f"{'—':>14}"
    print(line)
print("  （層二）")
for s0, e0 in events:
    line = f"  {dates[s0]}~{dates[e0][5:]:<10}"
    for name in CANDIDATES:
        rr = [t["res"]["ret_pct"] for t in L2[name] if s0 <= t["i"] <= e0 and t["res"]["status"] == "closed"]
        line += f"{(sum(rr)/len(rr)):>+9.2f}%({len(rr):>3})" if rr else f"{'—':>14}"
    print(line)

# 實際 ledger 取用單：+15% 首觸日與最終結果（F）
print("\n=== 模組實際取用單（ledger taken）：+15% 首次觸及日 ===")
L = json.load(open(str(Path(__file__).resolve().parents[2] / "data" / "swing" / "ledger.json"), encoding="utf-8"))
di = {d: i for i, d in enumerate(dates)}
cnt = defaultdict(int); early = []
for d, rec in L["days"].items():
    i = di.get(d)
    if i is None: continue
    for s in rec.get("signals", []):
        if not s.get("taken"): continue
        res = sim(s["code"], i, s.get("rules") or scfg["exit"])
        if not res or res["status"] != "closed": continue
        f15 = res["first15"]
        cnt["never" if (f15 is None or f15 > 10) else f"d{f15}"] += 1
        if f15 is not None and f15 < 5: early.append(res)
tot = sum(cnt.values())
print("  " + "  ".join(f"{k}:{v}" for k, v in sorted(cnt.items())) + f"  （共 {tot} 筆已平倉）")
if early:
    er = [r["ret_pct"] for r in early]
    print(f"  第 5 日前已觸及 +15% 的 {len(early)} 筆：F 最終均 {sum(er)/len(er):+.2f}%、<15% 者 {sum(x<15 for x in er)}、<0 者 {sum(x<=0 for x in er)}；出場 {dict((k, sum(1 for r in early if r['reason']==k)) for k in set(r['reason'] for r in early))}")
