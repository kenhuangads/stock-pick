# -*- coding: utf-8 -*-
"""單一崩跌事件的「鎖利地板上限」比較：同一批實際取用的單（ledger taken），只換地板上限重算出場；
另附該事件所有閘門日的全部訊號（層一）。用來在事件結束後，以同口徑檢查現行上限 30% 是否合適。

用法：python docs/research/floor_cap_event.py [事件起始日，預設 2026-11-04]
輸出：Markdown（表格＋逐筆），印到 stdout。資料只用 repo 內的官方快照與 ledger，不需 3 年資料集。
仍在持有的單以最新收盤計「未實現」並另列，不混入已實現統計；全部出場前的比較會偏向低上限（低上限較早實現贏家）。"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import swing  # noqa: E402
import twstock  # noqa: E402
from exitsim import make  # noqa: E402

START = next((a for a in sys.argv[1:] if not a.startswith("--")), "2026-11-04")
cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
scfg = swing.swing_cfg(cfg)
fees, lots, tax = cfg["fees"], scfg["lots"], scfg["swing_tax"]
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

snaps = twstock.load_snapshots()
dates = [s["date"] for s in snaps]
di = {d: i for i, d in enumerate(dates)}
N = len(dates)
S = swing.build_series(snaps)
ew, mkt5, _ = swing.compute_gauge(S, snaps, scfg)
sim, features = make(S, N, fees, lots, tax, mkt5, ew)
state = json.loads((ROOT / "data" / "swing" / "state.json").read_text(encoding="utf-8"))
ledger = json.loads((ROOT / "data" / "swing" / "ledger.json").read_text(encoding="utf-8"))
ev = next((e for e in state["events"] if e["start"] == START), None)
if not ev:
    print(f"找不到起始日 {START} 的事件；現有事件：{[e['start'] for e in state['events']]}")
    sys.exit(1)
gate_dates = [d["date"] for d in ev["days"]]


def last_close(code):
    c = S[code]["c"]
    for j in range(N - 1, -1, -1):
        if c[j] is not None:
            return c[j]
    return None


def run(sigs, rule):
    out = []
    for d, s in sigs:
        i = di[d]
        res = sim(s["code"], i, rule, features(s, i))
        if not res or res["status"] == "unfilled":
            continue
        if res["status"] == "open":
            lc = last_close(s["code"])
            res["unreal"] = round((lc / res["fill"] - 1) * 100, 2) if lc else None
        out.append((d, s, res))
    return out


def stats(rows):
    cl = [r for _, _, r in rows if r["status"] == "closed"]
    op = [r for _, _, r in rows if r["status"] == "open"]
    rets = [r["ret_pct"] for r in cl]
    reached = sum(1 for _, _, r in rows if r.get("tgt_pct") and r["mfe"] >= r["tgt_pct"] - 1e-9)
    return {"n": len(rows), "closed": len(cl), "open": len(op), "sum": sum(rets), "mean": (sum(rets) / len(rets)) if rets else None,
            "win": (sum(x > 0 for x in rets) / len(rets) * 100) if rets else None,
            "hold": (sum(r["days"] for r in cl) / len(cl)) if cl else None, "reached": reached,
            "unreal": [r.get("unreal") for r in op]}


def fmt(x, d=2, sign=True):
    return "–" if x is None else (f"{x:+.{d}f}" if sign else f"{x:.{d}f}")


taken = [(d, s) for d in gate_dates for s in (ledger["days"].get(d) or {}).get("signals", []) if s.get("taken")]
allsig = [(d, s) for d in gate_dates for s in (ledger["days"].get(d) or {}).get("signals", [])]
newrule = [(d, s) for d, s in taken if (s.get("rules") or {}).get("lock_trail_pct")]

print(f"# 鎖利地板上限比較：事件 {ev['start']}～{ev['end']}（資料至 {dates[-1]}）\n")
print(f"閘門日：{'、'.join(gate_dates)}；實際取用 {len(taken)} 筆（其中新出場規則 {len(newrule)} 筆）；當日全部訊號 {len(allsig)} 筆。\n")
cur_rows = run(taken, BASE)
still = [(d, s, r) for d, s, r in cur_rows if r["status"] == "open"]
print(f"**現行規則下仍持有：{len(still)} 筆**" + (f"（{'、'.join(s['code'] + ' ' + s['name'] for _, s, _ in still)}）——比較尚未完全同口徑，見文末說明" if still else "——已全部出場，可同口徑比較") + "\n")

for label, sigs in (("實際取用（新出場規則的單）", newrule), ("實際取用（事件全部，含 11/4 舊規則單）", taken), ("當日全部訊號（層一）", allsig)):
    print(f"## {label}\n")
    print("| 規則 | 已出場 | 仍持有 | 已實現合計 | 單筆均 | 勝率 | 平均持有日 | 摸到地板 | 仍持有者未實現（依最新收盤） |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for name, rule in RULES.items():
        st = stats(run(sigs, rule))
        us = [u for u in st["unreal"] if u is not None]
        un = f"{len(us)} 筆、平均 {sum(us) / len(us):+.1f}%" if us else "–"
        print(f"| {name} | {st['closed']} | {st['open']} | {fmt(st['sum'], 1)}% | {fmt(st['mean'])}% | {fmt(st['win'], 0, False)}% | "
              f"{fmt(st['hold'], 1, False)} | {st['reached']} | {un} |")
    print()

print("## 逐筆（實際取用；報酬為各規則下的已實現 %，持有中標「持」）\n")
names = list(RULES)
print("| 股票 | 層級 | 訊號日 | 成交 | 地板%（現行） | 最高 | " + " | ".join(names) + " |")
print("| --- | --- | --- | --- | --- | --- | " + " | ".join("---" for _ in names) + " |")
for d, s in taken:
    i = di[d]
    cells, base_res = [], None
    for name in names:
        res = sim(s["code"], i, RULES[name], features(s, i))
        if name == CUR:
            base_res = res
        if not res or res["status"] == "unfilled":
            cells.append("買不到")
        elif res["status"] == "open":
            cells.append("持")
        else:
            cells.append(f"{res['ret_pct']:+.1f}（{res['reason']}）")
    tier = (ledger["days"][d] or {}).get("tier")
    fill = base_res.get("fill") if base_res else None
    tgt = base_res.get("tgt_pct") if base_res else None
    mfe = base_res.get("mfe") if base_res else None
    print(f"| {s['code']} {s['name']} | {tier} | {d} | {fmt(fill, 2, False)} | {fmt(tgt, 1, False)} | {fmt(mfe, 1)}% | " + " | ".join(cells) + " |")

print("\n**說明**：同一批單、同一組日K（悲觀順序：同日先停損）只換地板上限；『摸到地板』＝持有期間最高價曾達地板。"
      "仍持有的單不計入已實現；在全部出場前，低上限因較早實現贏家而看起來較好（偏差），結論以全部出場後的數字為準。"
      f"未鎖利者最多持有 {BASE['max_hold_days']} 日、鎖利後最多 {BASE['max_hold_locked']} 日（自成交日起算）。")
