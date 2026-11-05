# -*- coding: utf-8 -*-
"""模組實際取用的 64 筆已平倉波段單：同一批進場，只換出場規則的反事實（無槽位效應，逐筆對照）。"""
import json, sys
from collections import defaultdict
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import twstock, swing
from exitsim import make, CANDIDATES

cfg = json.load(open(str(Path(__file__).resolve().parents[2] / "config.json"), encoding="utf-8"))
scfg = swing.swing_cfg(cfg)
snaps = twstock.load_snapshots(); dates = [s["date"] for s in snaps]; N = len(snaps)
S = swing.build_series(snaps)
ew, mkt5, _ = swing.compute_gauge(S, snaps, scfg)
sim, features = make(S, N, cfg["fees"], scfg["lots"], scfg["swing_tax"], mkt5, ew)
L = json.load(open(str(Path(__file__).resolve().parents[2] / "data" / "swing" / "ledger.json"), encoding="utf-8"))
di = {d: i for i, d in enumerate(dates)}
taken = []
for d, rec in L["days"].items():
    i = di.get(d)
    if i is None: continue
    for s in rec.get("signals", []):
        if s.get("taken"):
            taken.append((i, s))
print(f"ledger 取用訊號 {len(taken)} 筆")
rows = {}
for name, rule in CANDIDATES.items():
    res = []
    for i, s in taken:
        r = sim(s["code"], i, rule, features(s, i))
        if r and r["status"] == "closed":
            res.append(r)
    rows[name] = res
base = rows["F 現行 −7/+15@5d/10d"]
print(f"F 已平倉 {len(base)} 筆、淨 {sum(r['ret_pct'] for r in base):+.1f}%（單筆均 {sum(r['ret_pct'] for r in base)/len(base):+.2f}%）")
print(f"\n{'規則':30}{'平倉':>5}{'單筆均':>8}{'合計':>9}{'勝率':>6}{'停損':>5}{'持有':>6}{'日均':>7}{'vs F 合計':>10}")
for name, res in rows.items():
    if not res: continue
    tot = sum(r["ret_pct"] for r in res); hold = sum(r["days"] for r in res) / len(res)
    stops = sum(r["reason"].startswith("stop") for r in res)
    print(f"{name:30}{len(res):>5}{tot/len(res):>+8.2f}{tot:>+9.1f}{sum(r['ret_pct']>0 for r in res)/len(res)*100:>5.0f}%{stops:>5}{hold:>6.1f}{tot/len(res)/hold:>+7.2f}{tot - sum(r['ret_pct'] for r in base):>+10.1f}")
# 逐筆：F vs 鎖利15%@1d→移停8%（哪些單變好／變差）
cand = "鎖利15%@1d→移停8%/20d"
better = worse = same = 0
for a, b in zip(base, rows[cand]):
    if b["ret_pct"] > a["ret_pct"] + 0.05: better += 1
    elif b["ret_pct"] < a["ret_pct"] - 0.05: worse += 1
    else: same += 1
print(f"\n逐筆（F → {cand}）：變好 {better}、變差 {worse}、相同 {same}")
