# -*- coding: utf-8 -*-
"""同路徑進場規則掃描：對所有 5分K 核實過的建議單（同一批股票、同一天、同一組 K 線），
只換「進場規則」、沿用原出場引擎（停利/移停/時停/停損），比較成交率與損益。"""
import json, sys, collections
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from review import simulate_trade, bars_match_ohlc, limit_down_price, limit_up_price, _pnl
from indicators import tick_size, round_tick
from intraday import load_intraday

ROOT = Path(__file__).resolve().parents[2]
cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
fees = cfg["fees"]; strict = cfg["simulation"]["strict_fill"]
reviews = json.loads((ROOT / "data" / "reviews.json").read_text(encoding="utf-8"))
WIN = ("2026-10-16", "2026-10-30")

recs = []
for day in reviews:
    bars_by = load_intraday(day["date"])
    for p in day["picks"]:
        if p["sim_mode"] != "intraday":
            continue
        bars = bars_by.get(p["code"])
        ohlc = {"o": p["day_open"], "h": p["day_high"], "l": p["day_low"], "c": p["day_close"]}
        if not bars or not bars_match_ohlc(bars, ohlc):
            continue
        recs.append(dict(date=day["date"], p=p, bars=bars, ohlc=ohlc, side=p.get("side", "long"),
                         R=p["cdp_base"]["r"], shadow=bool(p.get("shadow"))))
print(f"樣本：{len(recs)} 筆（多 {sum(r['side']=='long' for r in recs)}／空 {sum(r['side']=='short' for r in recs)}；shadow {sum(r['shadow'] for r in recs)}）")


def sim(r, entry, target, stop, *, mode="revert", gap_void=True, cushion=None, cutoff=12, bars=None, tstop=None, side=None):
    p = r["p"]; side = side or r["side"]; bars = bars if bars is not None else r["bars"]
    f, fill, ex, reason, _ = simulate_trade(entry, target, stop, r["ohlc"], bars, p.get("trail_dist"),
        p["tstop_bar"] if tstop is None else tstop, strict_fill=strict,
        limit_dn=limit_down_price(p["prev_close"]), side=side, limit_up=limit_up_price(p["prev_close"]),
        entry_mode=mode, gap_void=gap_void, min_cushion=cushion, entry_cutoff_bar=cutoff,
        tstop_cond=bool(p.get("tstop_cond")))
    if not f:
        return None, reason
    return _pnl(side, fill, ex, 1, fees)[2], reason


def cushion_of(r, k):   # 現行：空單 0.5R、多單 0
    return k * r["R"] if (r["side"] == "short" and k) else None


def translated(r, new_entry):
    p = r["p"]; d = new_entry - p["entry"]
    return new_entry, p["target"] + d, p["stop"] + d


def gapped(r):
    p = r["p"]; o = r["ohlc"]["o"]; s = r["side"]
    return (s == "long" and o < p["entry"]) or (s == "short" and o > p["entry"])


def rule_current_recorded(r):
    p = r["p"]
    return sim(r, p["entry"], p["target"], p["stop"], gap_void=True, cushion=p.get("min_cushion"), cutoff=p.get("cutoff_bar"))


def rule_uniform(r, cushion_k=0.5, cutoff=12, gap_void=True):
    p = r["p"]
    return sim(r, p["entry"], p["target"], p["stop"], gap_void=gap_void, cushion=cushion_of(r, cushion_k), cutoff=cutoff)


def rule_open_market(r, fixed=False, gap_void=False):
    p = r["p"]; o = r["ohlc"]["o"]; s = r["side"]
    if gap_void and gapped(r):
        return None, "gapvoid"
    if fixed:
        t, st = p["target"], p["stop"]
        if (s == "long" and (o >= t or o <= st)) or (s == "short" and (o <= t or o >= st)):
            return None, "noroom"
        return sim(r, o, t, st, gap_void=False, cushion=None, cutoff=1)
    e, t, st = translated(r, o)
    return sim(r, e, t, st, gap_void=False, cushion=None, cutoff=1)


def rule_pullback(r, k, gap_void=True, cutoff=12, floor_limit=False):
    """開盤回檔 k·R 掛單（多：open-kR；空：open+kR），計畫整體平移；gap_void 沿用（開盤已穿舊掛價＝作廢）。
    floor_limit：掛價取 max(舊掛價, open-kR)（多）——開盤離舊掛價不遠時仍掛舊價。"""
    p = r["p"]; o = r["ohlc"]["o"]; s = r["side"]
    if gap_void and gapped(r):
        return None, "gapvoid"
    ne = o - k * r["R"] if s == "long" else o + k * r["R"]
    if floor_limit:
        ne = max(ne, p["entry"]) if s == "long" else min(ne, p["entry"])
    ne = round_tick(ne, "down" if s == "long" else "up")
    e, t, st = translated(r, ne)
    return sim(r, e, t, st, gap_void=False, cushion=None, cutoff=cutoff)


def rule_firstbar(r, gap_void=True):
    p = r["p"]; s = r["side"]; b = r["bars"]
    if gap_void and gapped(r):
        return None, "gapvoid"
    if len(b) < 3:
        return None, "nobars"
    ok = b[0][3] > b[0][0] if s == "long" else b[0][3] < b[0][0]
    if not ok:
        return None, "noconfirm"
    ne = b[1][0]; e, t, st = translated(r, ne)
    return sim(r, e, t, st, gap_void=False, cushion=None, cutoff=1, bars=b[1:],
               tstop=(p["tstop_bar"] - 1) if p["tstop_bar"] else None)


def rule_orb(r, n, gap_void=True, cutoff=12):
    p = r["p"]; s = r["side"]; b = r["bars"]
    if gap_void and gapped(r):
        return None, "gapvoid"
    if len(b) <= n:
        return None, "nobars"
    if s == "long":
        hi = max(x[1] for x in b[:n]); ne = round_tick(hi + tick_size(hi), "up")
    else:
        lo = min(x[2] for x in b[:n]); ne = round_tick(lo - tick_size(lo), "down")
    e, t, st = translated(r, ne)
    return sim(r, e, t, st, mode="breakout", gap_void=False, cushion=None, cutoff=cutoff, bars=b[n:],
               tstop=(p["tstop_bar"] - n) if p["tstop_bar"] else None)


RULES = collections.OrderedDict([
    ("R0 現行(紀錄參數)", rule_current_recorded),
    ("R0u 現行規則統一套用", lambda r: rule_uniform(r)),
    ("R1 取消空單緩衝", lambda r: rule_uniform(r, cushion_k=None)),
    ("R8 緩衝降為0.25R", lambda r: rule_uniform(r, cushion_k=0.25)),
    ("R9 撤單延至11:00", lambda r: rule_uniform(r, cutoff=24)),
    ("R2 純限價+10:00撤單(無gap/緩衝)", lambda r: rule_uniform(r, cushion_k=None, gap_void=False)),
    ("R3 純限價整天", lambda r: rule_uniform(r, cushion_k=None, gap_void=False, cutoff=None)),
    ("R4 開盤市價(計畫平移)", lambda r: rule_open_market(r)),
    ("R4g 開盤市價+gap_void", lambda r: rule_open_market(r, gap_void=True)),
    ("R4f 開盤市價(原價位)", lambda r: rule_open_market(r, fixed=True)),
    ("R5 開盤回檔0.15R", lambda r: rule_pullback(r, 0.15)),
    ("R5 開盤回檔0.25R", lambda r: rule_pullback(r, 0.25)),
    ("R5 開盤回檔0.35R", lambda r: rule_pullback(r, 0.35)),
    ("R5 開盤回檔0.5R", lambda r: rule_pullback(r, 0.5)),
    ("R5n 開盤回檔0.25R(無gap_void)", lambda r: rule_pullback(r, 0.25, gap_void=False)),
    ("R5n 開盤回檔0.5R(無gap_void)", lambda r: rule_pullback(r, 0.5, gap_void=False)),
    ("R5f 回檔0.25R但不低於舊掛價", lambda r: rule_pullback(r, 0.25, floor_limit=True)),
    ("R5f 回檔0.5R但不低於舊掛價", lambda r: rule_pullback(r, 0.5, floor_limit=True)),
    ("R6 首根確認後市價", rule_firstbar),
    ("R6n 首根確認(無gap_void)", lambda r: rule_firstbar(r, gap_void=False)),
    ("R7 ORB-5分", lambda r: rule_orb(r, 1)),
    ("R7 ORB-15分", lambda r: rule_orb(r, 3)),
    ("R7n ORB-15分(無gap_void)", lambda r: rule_orb(r, 3, gap_void=False)),
])


def stats(rows):
    n = len(rows); fills = [x for x in rows if x[0] is not None]
    nets = [x[0] for x in fills]; w = [x for x in nets if x > 0]; l = [x for x in nets if x <= 0]
    return dict(n=n, f=len(fills), fr=len(fills) / n * 100 if n else 0, net=sum(nets), avg=sum(nets) / len(nets) if nets else 0,
                wr=len(w) / len(nets) * 100 if nets else 0, aw=sum(w) / len(w) if w else 0, al=sum(l) / len(l) if l else 0,
                pf=(sum(w) / -sum(l)) if l and sum(l) < 0 else float("inf"))


def fmt(s):
    return (f"n={s['n']:4} 成交{s['f']:4}({s['fr']:5.1f}%) 淨{s['net']:+9,} 均{s['avg']:+6.0f} 勝{s['wr']:4.0f}% "
            f"賺{s['aw']:+6.0f}/賠{s['al']:+6.0f} PF{s['pf']:4.2f}")


results = {}
for name, fn in RULES.items():
    rows = [(fn(r), r) for r in recs]
    results[name] = rows
    allr = [(x[0][0],) for x in rows]
    L = [(x[0][0],) for x in rows if x[1]["side"] == "long"]; Sh = [(x[0][0],) for x in rows if x[1]["side"] == "short"]
    W = [(x[0][0],) for x in rows if WIN[0] <= x[1]["date"] <= WIN[1]]
    print(f"\n{name}\n  全部 {fmt(stats(allr))}\n  做多 {fmt(stats(L))}\n  做空 {fmt(stats(Sh))}\n  10/16-30 {fmt(stats(W))}")

mis = 0
for (res, r) in results["R0 現行(紀錄參數)"]:
    p = r["p"]
    if r["shadow"]:
        continue
    rec_net = p["net"] if p["filled"] else None
    if res[0] != rec_net:
        mis += 1
print(f"\n[check] R0 與紀錄不一致：{mis} 筆")

names = ["R0u 現行規則統一套用", "R2 純限價+10:00撤單(無gap/緩衝)", "R4 開盤市價(計畫平移)", "R4g 開盤市價+gap_void",
         "R5 開盤回檔0.25R", "R5 開盤回檔0.5R", "R5f 回檔0.5R但不低於舊掛價", "R6 首根確認後市價", "R7 ORB-15分"]
months = sorted({r["date"][:7] for r in recs})
for label, side in (("全部側", None), ("做空", "short"), ("做多", "long")):
    print(f"\n=== 月別淨損益/成交數（{label}）===")
    print(f"{'rule':34}" + "".join(f"{m[2:]:>14}" for m in months))
    for nm in names:
        row = f"{nm:34}"
        for m in months:
            st = stats([(x[0][0],) for x in results[nm] if x[1]["date"][:7] == m and (side is None or x[1]["side"] == side)])
            row += f"{st['net']:+9,}/{st['f']:3}"
        print(row)

json.dump({nm: [(x[0][0], x[0][1], x[1]["date"], x[1]["p"]["code"], x[1]["side"], x[1]["shadow"]) for x in rows]
           for nm, rows in results.items()},
          open(Path(__file__).parent / "entry_scan.json", "w", encoding="utf-8"), ensure_ascii=False)
