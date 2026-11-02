# -*- coding: utf-8 -*-
"""跨路徑複現：對各條 walk-forward 擾動路徑（ab_*.json 的 reviews）套同一組進場規則，
看「做空側：現行 vs 取消緩衝 vs 開盤回檔 k·R」的類別效果是否跨路徑複現。只用有 5分K 的筆。"""
import json, sys, glob
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from review import simulate_trade, bars_match_ohlc, limit_down_price, limit_up_price, _pnl
from indicators import round_tick
from intraday import load_intraday

ROOT = Path(__file__).resolve().parents[2]; HERE = Path(__file__).parent
cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
fees = cfg["fees"]; strict = cfg["simulation"]["strict_fill"]
_bars_cache = {}


def bars_for(date):
    if date not in _bars_cache:
        _bars_cache[date] = load_intraday(date)
    return _bars_cache[date]


def load_recs(reviews, since="2026-05-01"):
    recs = []
    for day in reviews:
        if day["date"] < since:
            continue
        bb = bars_for(day["date"])
        for p in day["picks"]:
            bars = bb.get(p["code"])
            ohlc = {"o": p["day_open"], "h": p["day_high"], "l": p["day_low"], "c": p["day_close"]}
            if not bars or not bars_match_ohlc(bars, ohlc) or not p.get("cdp_base"):
                continue
            recs.append(dict(date=day["date"], p=p, bars=bars, ohlc=ohlc, side=p.get("side", "long"), R=p["cdp_base"]["r"]))
    return recs


def sim(r, entry, target, stop, *, gap_void=True, cushion=None, cutoff=12):
    p = r["p"]
    f, fill, ex, reason, _ = simulate_trade(entry, target, stop, r["ohlc"], r["bars"], p.get("trail_dist"), p.get("tstop_bar"),
        strict_fill=strict, limit_dn=limit_down_price(p["prev_close"]), side=r["side"], limit_up=limit_up_price(p["prev_close"]),
        entry_mode="revert", gap_void=gap_void, min_cushion=cushion, entry_cutoff_bar=cutoff, tstop_cond=bool(p.get("tstop_cond")))
    return (_pnl(r["side"], fill, ex, 1, fees)[2] if f else None), reason


def gapped(r):
    o = r["ohlc"]["o"]; e = r["p"]["entry"]
    return (r["side"] == "long" and o < e) or (r["side"] == "short" and o > e)


def rule_uniform(r, cushion_k=0.5, cutoff=12, gap_void=True):
    p = r["p"]; cush = cushion_k * r["R"] if (r["side"] == "short" and cushion_k) else None
    return sim(r, p["entry"], p["target"], p["stop"], gap_void=gap_void, cushion=cush, cutoff=cutoff)


def rule_pullback(r, k, gap_void=True, cutoff=12):
    p = r["p"]; o = r["ohlc"]["o"]; s = r["side"]
    if gap_void and gapped(r):
        return None, "gapvoid"
    ne = round_tick(o - k * r["R"] if s == "long" else o + k * r["R"], "down" if s == "long" else "up")
    d = ne - p["entry"]
    return sim(r, ne, p["target"] + d, p["stop"] + d, gap_void=False, cushion=None, cutoff=cutoff)


def rule_open(r, gap_void=True):
    p = r["p"]; o = r["ohlc"]["o"]
    if gap_void and gapped(r):
        return None, "gapvoid"
    d = o - p["entry"]
    return sim(r, o, p["target"] + d, p["stop"] + d, gap_void=False, cushion=None, cutoff=1)


RULES = [
    ("現行(緩衝0.5R)", lambda r: rule_uniform(r)),
    ("取消緩衝", lambda r: rule_uniform(r, cushion_k=None)),
    ("緩衝0.25R", lambda r: rule_uniform(r, cushion_k=0.25)),
    ("開盤市價+gv", lambda r: rule_open(r)),
    ("回檔0.15R", lambda r: rule_pullback(r, 0.15)),
    ("回檔0.25R", lambda r: rule_pullback(r, 0.25)),
    ("回檔0.35R", lambda r: rule_pullback(r, 0.35)),
    ("回檔0.5R", lambda r: rule_pullback(r, 0.5)),
    ("回檔0.25R無gv", lambda r: rule_pullback(r, 0.25, gap_void=False)),
]


def stats(vals, n):
    f = [v for v in vals if v is not None]; w = [v for v in f if v > 0]; l = [v for v in f if v <= 0]
    return dict(n=n, f=len(f), net=sum(f), ev=sum(f) / n if n else 0, wr=len(w) / len(f) * 100 if f else 0,
                pf=(sum(w) / -sum(l)) if l and sum(l) < 0 else float("inf"))


paths = [("live", json.loads((ROOT / "data" / "reviews.json").read_text(encoding="utf-8")))]
for fp in sorted(glob.glob(str(HERE / "ab_*.json"))):
    try:
        d = json.loads(Path(fp).read_text(encoding="utf-8"))
        if isinstance(d, dict) and d.get("reviews"):
            paths.append((Path(fp).stem[3:], d["reviews"]))
    except Exception as e:  # noqa
        print("skip", fp, e)

out = {}
for side in ("short", "long"):
    print(f"\n==================== {side} ====================")
    print(f"{'path':12}{'n':>5} | " + " | ".join(f"{nm:>14}" for nm, _ in RULES))
    print(f"{'':12}{'':>5} | " + " | ".join(f"{'成交/淨/EV':>14}" for _ in RULES))
    agg = {nm: [] for nm, _ in RULES}
    for label, reviews in paths:
        recs = [r for r in load_recs(reviews) if r["side"] == side]
        if len(recs) < 20:
            continue
        row = f"{label:12}{len(recs):5} | "
        cells = []
        for nm, fn in RULES:
            st = stats([fn(r)[0] for r in recs], len(recs))
            agg[nm].append(st)
            cells.append(f"{st['f']:3}/{st['net']:+8,}/{st['ev']:+5.0f}")
        print(row + " | ".join(f"{c:>14}" for c in cells))
        out[(side, label)] = {nm: agg[nm][-1] for nm, _ in RULES}
    # 跨路徑：每條路徑「規則 − 現行」的淨損益差，正的路徑數
    base = agg[RULES[0][0]]
    print("  vs 現行 — 改善路徑數 / 總路徑數（淨損益差中位數）：")
    for nm, _ in RULES[1:]:
        diffs = [a["net"] - b["net"] for a, b in zip(agg[nm], base)]
        diffs_sorted = sorted(diffs)
        med = diffs_sorted[len(diffs) // 2] if diffs else 0
        print(f"    {nm:14} {sum(d > 0 for d in diffs)}/{len(diffs)}  中位數 {med:+,}  各路徑 {[f'{d:+,}' for d in diffs]}")
json.dump({f"{k[0]}|{k[1]}": v for k, v in out.items()}, open(HERE / "entry_scan_paths.json", "w", encoding="utf-8"), ensure_ascii=False)
