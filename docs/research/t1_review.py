# -*- coding: utf-8 -*-
"""T1 開關複審（2026-11-06，納入 11 月事件）：
3 年 Yahoo（4 事件）＋官方快照（含 2026-11 事件，11/6 出場價以盤中 5 分K 補上）。
變體：BC（現行：T1 只出 R2c、新進場 ≤3）／T2only（關 T1）／A_BC（T1 門檻 −8%→−13%＋BC）。
出場：新規則（鎖利移停＋自適應地板）與舊 F。槽位邏輯複製 swing.process（上限 10、持倉佔槽、10 日去重、T1 新進場上限）。"""
import json, sys, copy
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import swing, twstock
from exitsim import make

OLD = Path(__file__).parent   # 需 yahoo3y/*.json（3 年 Yahoo 日K，不在 repo）
cfg = json.load(open(str(Path(__file__).resolve().parents[2] / "config.json"), encoding="utf-8"))
scfg = swing.swing_cfg(cfg)
fees, lots, tax = cfg["fees"], scfg["lots"], scfg["swing_tax"]
NEW = {"stop_pct": 7, "target_pct": None, "target_mode": "retrace", "r": 0.5, "tmin": 8, "tmax": 30,
       "min_hold_days": 1, "max_hold_days": 10, "lock_trail_pct": 8, "max_hold_locked": 20}
OLDF = {"stop_pct": 7, "target_pct": 15, "min_hold_days": 5, "max_hold_days": 10}
VARIANTS = {
    "BC 現行(T1 只R2c、≤3)": dict(scfg["gate"]),
    "T2only 關 T1": {**scfg["gate"], "t1_enabled": False},
    "A_BC T1 門檻 −13%": {**scfg["gate"], "mkt5_le": -13.0},
}


def run(S, snaps, N, ew, mkt5, rule, gate_cfg, last_i):
    sim, _ = make(S, N, fees, lots, tax, mkt5, ew)
    taken = []
    for i in range(60, min(last_i, N - 2) + 1):
        st, tier = swing.gate_state(mkt5[i], ew[i], gate_cfg)
        if st != "open":
            continue

        def recently(code):
            for x in taken:
                if x["code"] != code:
                    continue
                r = x["res"]
                if r["status"] == "open" and r["fill_idx"] <= i:
                    return True
                if r["status"] == "closed" and (r["exit_idx"] > i or i - x["i"] <= scfg["dedup_days"]):
                    return True
            return False
        sigs = [s for s in swing.detect_signals(S, snaps, i, scfg) if not recently(s["code"])]
        if tier == "T1" and gate_cfg.get("t1_setups"):
            sigs = [s for s in sigs if s["setup"] in gate_cfg["t1_setups"]]
        open_n = sum(1 for x in taken if x["res"]["status"] in ("open", "closed") and x["res"]["fill_idx"] <= i
                     and (x["res"]["status"] == "open" or x["res"]["exit_idx"] > i))
        cap = scfg["max_positions"]
        free = max(0, cap - open_n)
        if tier == "T1" and gate_cfg.get("t1_max_new"):
            free = min(free, int(gate_cfg["t1_max_new"]))
        k = 0
        for s in sigs:
            if k >= free:
                break
            res = sim(s["code"], i, rule)
            if res is None:
                continue
            k += 1
            if res["status"] == "unfilled":
                continue
            taken.append({"i": i, "tier": tier, "code": s["code"], "setup": s["setup"], "res": res})
    return taken


def events_of(trades):
    ev = []
    for t in sorted(trades, key=lambda t: t["i"]):
        if ev and t["i"] - ev[-1][1] <= 15:
            ev[-1][1] = t["i"]; ev[-1][2].append(t)
        else:
            ev.append([t["i"], t["i"], [t]])
    return ev


def stats(trades):
    cl = [t for t in trades if t["res"]["status"] == "closed"]
    if not cl:
        return dict(n=0, mean=0, win=0, eq=0, dd=0, worst=0, tail=0)
    rets = [t["res"]["ret_pct"] for t in cl]
    eq = peak = 1.0; dd = 0.0
    for t in sorted(cl, key=lambda t: t["res"]["exit_idx"]):
        eq *= 1 + 0.10 * t["res"]["ret_pct"] / 100; peak = max(peak, eq); dd = min(dd, eq / peak - 1)
    return dict(n=len(cl), mean=sum(rets) / len(rets), win=sum(r > 0 for r in rets) / len(rets) * 100,
                eq=(eq - 1) * 100, dd=dd * 100, worst=min(rets), tail=sum(r <= -10 for r in rets))


def fmt(s):
    return (f"n={s['n']:>3} 均{s['mean']:>+6.2f}% 勝{s['win']:>3.0f}% 權益{s['eq']:>+6.1f}% 回撤{s['dd']:>+5.1f}% "
            f"最差{s['worst']:>+6.1f}% ≤−10%:{s['tail']}")


def report(label, S, snaps, cal, N, ew, mkt5, last_i, event_names):
    print(f"\n==================== {label} ====================")
    out = {}
    for rname, rule in (("新出場（鎖利移停＋自適應地板）", NEW), ("舊出場 F", OLDF)):
        print(f"\n--- {rname} ---")
        for vname, g in VARIANTS.items():
            tr = run(S, snaps, N, ew, mkt5, rule, g, last_i)
            out[(rname, vname)] = tr
            t1 = [t for t in tr if t["tier"] == "T1"]; t2 = [t for t in tr if t["tier"] == "T2"]
            print(f"  {vname:22} 全部 {fmt(stats(tr))}")
            print(f"  {'':22} T1日 {fmt(stats(t1))}")
            print(f"  {'':22} T2日 {fmt(stats(t2))}")
        # 逐事件：權益貢獻（Σ 10%×報酬）
        base = out[(rname, "BC 現行(T1 只R2c、≤3)")]
        evs = events_of(base + out[(rname, "T2only 關 T1")])
        print(f"  逐事件權益貢獻（Σ10%×報酬；括號＝筆數，T1 筆數）：")
        for s0, e0, _ in evs:
            line = f"    {cal[s0]}~{cal[e0][5:]}"
            for vname in VARIANTS:
                tr = [t for t in out[(rname, vname)] if s0 - 15 <= t["i"] <= e0 + 15 and t["res"]["status"] == "closed"]
                c = sum(0.10 * t["res"]["ret_pct"] for t in tr)
                n1 = sum(t["tier"] == "T1" for t in tr)
                line += f" | {vname.split()[0]} {c:>+6.2f}%({len(tr)},T1:{n1})"
            print(line)
    return out


# ---------- 3 年 Yahoo ----------
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
R3 = report("3 年 Yahoo（2023-10～2026-10-21，4 事件）", S, snaps3, cal, N, ew3, mkt53, N - 2, None)

# ---------- 官方快照＋11/6 盤中出場價 ----------
snaps = twstock.load_snapshots(); dates = [s["date"] for s in snaps]
So = swing.build_series(snaps)
last_i = len(snaps) - 1                      # 11/5：閘門判定只到最新快照日
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from intraday import fetch_5m_series
extra = "2026-11-06"
for code, s in So.items():
    for k in ("o", "h", "l", "c", "a"):
        if k in s and isinstance(s[k], list):
            s[k].append(None)
patched = []
for code in ("3624", "4919", "4908"):
    if code not in So: continue
    bars = fetch_5m_series(code, So[code].get("m") or "twse", extra, extra).get(extra) or []
    if bars:
        So[code]["o"][-1] = bars[0][0]; So[code]["h"][-1] = max(b[1] for b in bars)
        So[code]["l"][-1] = min(b[2] for b in bars); So[code]["c"][-1] = bars[-1][3]
        patched.append(code)
print(f"\n官方快照 {dates[0]}~{dates[-1]}，另以 5 分K 補 {extra} 的 {patched}")
ewo, mkt5o, _ = swing.compute_gauge(So, snaps, scfg)
ewo = list(ewo) + [None]; mkt5o = list(mkt5o) + [None]
dates_x = dates + [extra]
RO = report("官方快照（2026-02～11-05，含 11 月事件）", So, snaps, dates_x, len(dates_x), ewo, mkt5o, last_i, None)
for (rname, vname), tr in RO.items():
    if vname.startswith("BC") and rname.startswith("新"):
        nov = [t for t in tr if dates_x[t["i"]] >= "2026-11-01"]
        print("\n  11 月事件 BC 取用：", [(dates_x[t["i"]], t["tier"], t["code"], t["res"]["status"], t["res"].get("reason"), t["res"].get("ret_pct")) for t in nov])
