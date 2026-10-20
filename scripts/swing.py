"""崩跌反彈波段模組（swing）。

研究結論（docs/swing_research.md，3 年全市場日K ＋ 官方快照複核）：
「持股 ≥1 週、+15%」的機會集中在**市場急跌後的反彈日**，平時所有動能／突破型態皆無正期望。
故本模組平時待命、只在閘門開啟日產生波段建議單，並以官方日K逐日模擬持倉（悲觀順序）。

  市場壓力計：流動股等權日報酬 EW、近 5 日累計 MKT5（%）
  閘門 T1：MKT5 ≤ −8%（深跌）
  閘門 T2：MKT5 ≤ −5% 且當日 EW ≥ +3%（反彈確認日；config 自 2026-10-20 放寬為 −3%，觀察項——見 README）
  型態 R1 ：收盤漲停（漲幅 ≥9.5%）且收盤離 60 日高 ≥20%（深跌後漲停，優先）
  型態 R2c：5 日跌幅 ≥15% 且今日收紅（急跌後首根紅K）
  排序　　：綜合分＝同日分位數加權「離 60 日高深度（深好）／量比（低好）／20 日均成交值（大好）」
            （config swing.ranking.weights；三項各有獨立實證，法人籌碼與 20 日報酬經檢驗無增益故不用）；
            同日最多取「空槽數」檔，其餘列候補
  進場　　：隔日開盤市價；開盤價 ≥ 漲停價（跳動單位取整）視為買不到
  出場　　：停損 −7%（跳空以開盤價、跌停鎖死延後到次日開盤）；第 5 日起 +15% 停利；
            第 10 日收盤時間出場（前 5 日只停損＝至少持有一週）
  成本　　：手續費 0.1425%×折數 ×2 ＋ 證交稅 0.3%（非當沖）
  規則隨單寫入：每筆訊號記錄當時的出場參數，復盤依單上欄位重放（與當沖模組同模式）

資料流：
  data/swing/ledger.json  逐日壓力計／閘門／訊號（append-only；--rebuild 全量重建）
  data/swing/state.json   由 ledger ＋ 官方日K重算的持倉、已平倉、事件績效（每次重寫）

用法：python scripts/swing.py [--rebuild]
"""
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from indicators import round_tick, market_breadth_ma_series
from review import limit_up_price, limit_down_price

ROOT = Path(__file__).resolve().parent.parent
SWING_DIR = ROOT / "data" / "swing"
LEDGER = SWING_DIR / "ledger.json"
STATE = SWING_DIR / "state.json"
TAIPEI = timezone(timedelta(hours=8))

DEFAULT_CFG = {
    "enabled": True,
    "gate": {"mkt5_le": -8.0, "mkt5_le_rebound": -5.0, "rebound_day_ge": 3.0, "warn_mkt5_le": -5.0},
    "liquidity": {"min_val_ma": 50_000_000, "min_price": 10, "max_price": 1000, "val_window": 20, "min_val_days": 5},
    "setups": {
        "R1": {"enabled": True, "min_chg_pct": 9.5, "min_drawdown_pct": 20, "lookback": 60, "min_lookback": 40},
        "R2c": {"enabled": True, "drop_days": 5, "min_drop_pct": 15},
    },
    "exit": {"stop_pct": 7, "target_pct": 15, "min_hold_days": 5, "max_hold_days": 10},
    "ranking": {"weights": {"drawdown": 1.0, "low_vol_ratio": 0.0, "val_ma": 0.0}},   # 預設＝僅深度（舊行為）
    "max_positions": 20,
    "t2_max_positions": 30,
    "position_pct": 5,
    "lots": 1,
    "dedup_days": 10,
    "breaker": {"enabled": True, "stop_ratio": 0.5, "block_entries": False},
    "swing_tax": 0.003,
    "history_days": 80,
}
SETUP_META = {
    "R1": {"name": "深跌後漲停", "desc": "收盤漲停（漲幅 ≥9.5%）且收盤離 60 日高 ≥20%"},
    "R2c": {"name": "急跌後首根紅K", "desc": "5 日跌幅 ≥15% 且今日收紅（收盤 > 開盤）"},
}


def _merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def swing_cfg(cfg):
    return _merge(DEFAULT_CFG, cfg.get("swing") or {})


def load_json(p, default):
    p = Path(p)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def save_json(p, obj):
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    Path(p).write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


# ---------------------------------------------------------------- 資料
def build_series(snaps):
    """快照 → 每檔對齊日期索引的 OHLC/成交值序列（缺日為 None）；排除 00 開頭 ETF。"""
    n = len(snaps)
    S = {}
    for i, s in enumerate(snaps):
        for code, k in s["stocks"].items():
            if code.startswith("00"):
                continue
            r = S.get(code)
            if r is None:
                r = S[code] = {"n": k["n"], "m": k["m"], "o": [None] * n, "h": [None] * n,
                               "l": [None] * n, "c": [None] * n, "a": [None] * n}
            if k.get("c") and k.get("o") and k.get("h") and k.get("l"):
                r["o"][i], r["h"][i], r["l"][i], r["c"][i], r["a"][i] = k["o"], k["h"], k["l"], k["c"], k.get("a") or 0
            r["n"], r["m"] = k["n"], k["m"]
    for r in S.values():   # 成交值前綴和 → 流動性 O(1)
        cs, cn, acc, cnt = [0.0] * (n + 1), [0] * (n + 1), 0.0, 0
        for i in range(n):
            if r["a"][i] is not None:
                acc += r["a"][i]
                cnt += 1
            cs[i + 1], cn[i + 1] = acc, cnt
        r["cs"], r["cn"] = cs, cn
    return S


def val_ma(r, i, win, min_days):
    lo = max(0, i - win + 1)
    cnt = r["cn"][i + 1] - r["cn"][lo]
    if cnt < min_days:
        return None
    return (r["cs"][i + 1] - r["cs"][lo]) / cnt


def is_liquid(r, i, lq):
    c = r["c"][i]
    if c is None or not (lq["min_price"] <= c <= lq["max_price"]):
        return False
    v = val_ma(r, i, lq["val_window"], lq["min_val_days"])
    return v is not None and v >= lq["min_val_ma"]


# ---------------------------------------------------------------- 壓力計／閘門
def compute_gauge(S, snaps, scfg):
    """回傳 (ew, mkt5, breadth5)：等權日報酬（小數）、近 5 日累計（%）、漲跌家數寬度 5 日均。"""
    n = len(snaps)
    lq = scfg["liquidity"]
    ew = [None] * n
    for i in range(1, n):
        tot, cnt = 0.0, 0
        for r in S.values():
            c0, c1 = r["c"][i - 1], r["c"][i]
            if c0 and c1 and is_liquid(r, i, lq):
                tot += c1 / c0 - 1
                cnt += 1
        ew[i] = tot / cnt if cnt >= 100 else None
    mkt5 = [None] * n
    for i in range(5, n):
        w = ew[i - 4: i + 1]
        if all(x is not None for x in w):
            mkt5[i] = sum(w) * 100
    breadth5 = [None] + market_breadth_ma_series(snaps, 5)
    return ew, mkt5, breadth5


def gate_state(mkt5, ew, g):
    if mkt5 is None or ew is None:
        return "na", None
    if mkt5 <= g["mkt5_le"]:
        return "open", "T1"
    if mkt5 <= g["mkt5_le_rebound"] and ew * 100 >= g["rebound_day_ge"]:
        return "open", "T2"
    if mkt5 <= g["warn_mkt5_le"]:
        return "warn", None
    return "idle", None


# ---------------------------------------------------------------- 訊號
def detect_signals(S, snaps, i, scfg):
    lq, st, ex = scfg["liquidity"], scfg["setups"], scfg["exit"]
    r1, r2 = st["R1"], st["R2c"]
    punish = set(snaps[i].get("punish") or [])
    out = []
    for code, r in S.items():
        c, o = r["c"][i], r["o"][i]
        if c is None or o is None or i < 1 or not r["c"][i - 1] or code in punish:
            continue
        if not is_liquid(r, i, lq):
            continue
        chg = (c / r["c"][i - 1] - 1) * 100
        highs = [h for h in r["h"][max(0, i - r1["lookback"]): i] if h is not None]
        hi = max(highs) if highs else None
        dd = (1 - c / hi) * 100 if hi else None
        d5 = None
        if i >= r2["drop_days"] and r["c"][i - r2["drop_days"]]:
            d5 = (c / r["c"][i - r2["drop_days"]] - 1) * 100
        setups = []
        if (r1["enabled"] and chg >= r1["min_chg_pct"] and len(highs) >= r1["min_lookback"]
                and dd is not None and dd >= r1["min_drawdown_pct"]):
            setups.append("R1")
        if r2["enabled"] and d5 is not None and d5 <= -r2["min_drop_pct"] and c > o:
            setups.append("R2c")
        if not setups:
            continue
        # 量比：訊號日成交量 / 前 20 日均量（實證：量比低者反彈較佳——量能失控的反彈日反而弱）
        vols = []
        for j in range(max(0, i - 20), i):
            kj = snaps[j]["stocks"].get(code)
            if kj and kj.get("v"):
                vols.append(kj["v"])
        v_today = (snaps[i]["stocks"].get(code) or {}).get("v")
        vol_ratio = round(v_today / (sum(vols) / len(vols)), 2) if (vols and v_today) else None
        out.append({
            "code": code, "name": r["n"], "market": r["m"], "setup": setups[0], "setups": setups,
            "close": c, "chg_pct": round(chg, 2),
            "drop5_pct": round(d5, 2) if d5 is not None else None,
            "drawdown_pct": round(dd, 2) if dd is not None else None, "hi_days": len(highs),
            "val_ma": int(val_ma(r, i, lq["val_window"], lq["min_val_days"]) or 0),
            "vol_ratio": vol_ratio,
            "limit_up_next": limit_up_price(c),
            "ref_stop": round_tick(c * (1 - ex["stop_pct"] / 100), "down"),
            "ref_target": round_tick(c * (1 + ex["target_pct"] / 100), "up"),
            "rules": dict(ex),
        })
    _rank_signals(out, scfg.get("ranking") or {})
    return out


def _pct_rank(vals):
    """同日分位數 0~1（大=高）；None 視為中位（不加分不扣分）。"""
    idx = [k for k, v in enumerate(vals) if v is not None]
    out = [0.5] * len(vals)
    if len(idx) == 1:
        out[idx[0]] = 1.0
    elif idx:
        order = sorted(idx, key=lambda k: vals[k])
        for pos, k in enumerate(order):
            out[k] = pos / (len(order) - 1)
    return out


def _rank_signals(out, rcfg):
    """綜合分排序：深度（深好）／量比（低好）／成交值（大好）依權重加總同日分位數 → 0~100。
    檢驗（2026 官方資料 346 筆閘門內/深桶已平倉訊號）：深度 ρ=+0.38 最強、量比 ρ=−0.17（低者 +10.4% vs
    高者 +4.9%）、閘門內成交值 ρ=+0.25；20 日報酬與深度重疊、融資偏噪音、法人買賣超 ρ≈0 無排序力。
    日內排序力本就有限（ρ 0.1~0.2）——閘門對不對遠比挑哪幾檔重要。"""
    w = (rcfg.get("weights") or {})
    wd, wv, wa = w.get("drawdown", 1.0) or 0, w.get("low_vol_ratio", 0) or 0, w.get("val_ma", 0) or 0
    tot = (wd + wv + wa) or 1.0
    pd = _pct_rank([x["drawdown_pct"] for x in out])
    pv = _pct_rank([(-x["vol_ratio"]) if x.get("vol_ratio") is not None else None for x in out])
    pa = _pct_rank([x["val_ma"] for x in out])
    for k, x in enumerate(out):
        x["score"] = round((wd * pd[k] + wv * pv[k] + wa * pa[k]) / tot * 100, 1)
        x["score_parts"] = {"drawdown": round(pd[k] * 100), "low_vol_ratio": round(pv[k] * 100), "val_ma": round(pa[k] * 100)}
    out.sort(key=lambda x: (-x["score"], -(x["drawdown_pct"] or 0), x["drop5_pct"] if x["drop5_pct"] is not None else 0))
    for k, x in enumerate(out, 1):
        x["rank"] = k


# ---------------------------------------------------------------- 模擬
def swing_net(buy, sell, lots, fees_cfg, tax_rate, discount=None):
    shares = lots * 1000
    disc = discount if discount is not None else fees_cfg["default_discount"]
    fee_b = max(int(buy * shares * fees_cfg["fee_rate"] * disc), fees_cfg["min_fee"])
    fee_s = max(int(sell * shares * fees_cfg["fee_rate"] * disc), fees_cfg["min_fee"])
    tax = int(sell * shares * tax_rate)
    gross = int(round((sell - buy) * shares))
    return gross - fee_b - fee_s - tax, fee_b + fee_s + tax


def simulate_signal(S, code, t, rules, fees_cfg, lots, tax_rate):
    """訊號日 t（索引）→ 隔日開盤進場、日K悲觀順序模擬到資料末端。"""
    r = S.get(code)
    n = len(r["c"])
    fi = t + 1
    if fi >= n:
        return {"status": "pending"}
    if r["o"][fi] is None or r["c"][fi] is None:
        return {"status": "unfilled", "reason": "nobar"}
    lu = limit_up_price(r["c"][t])
    if r["o"][fi] >= lu - 1e-9:
        return {"status": "unfilled", "reason": "limitup", "open_next": r["o"][fi], "limit_up": lu}
    fill = r["o"][fi]
    stop = round_tick(fill * (1 - rules["stop_pct"] / 100), "down")
    target = round_tick(fill * (1 + rules["target_pct"] / 100), "up")
    res = {"status": "open", "fill": fill, "fill_idx": fi, "stop": stop, "target": target,
           "mfe_pct": 0.0, "mae_pct": 0.0, "lock": False}
    prevc, pend, last_j = r["c"][t], False, fi
    for j in range(fi, n):
        o, h, l, c = r["o"][j], r["h"][j], r["l"][j], r["c"][j]
        if o is None or c is None:
            continue   # 停牌日不計
        d = j - fi
        last_j = j
        exit_px = reason = None
        if pend:
            exit_px, reason = o, "stop_lock"
        else:
            locked = prevc is not None and c <= limit_down_price(prevc) + 1e-9 and l == c
            if d >= 1 and o <= stop:
                if locked and o == c:
                    pend = True
                else:
                    exit_px, reason = o, "stop"
            elif l <= stop:
                if locked:
                    pend = True
                else:
                    exit_px, reason = stop, "stop"
            if exit_px is None and not pend and d >= rules["min_hold_days"]:
                if o >= target:
                    exit_px, reason = o, "target"
                elif h >= target:
                    exit_px, reason = target, "target"
            if exit_px is None and not pend and d >= rules["max_hold_days"]:
                exit_px, reason = c, "time"
        res["mfe_pct"] = round(max(res["mfe_pct"], (h / fill - 1) * 100), 2)
        res["mae_pct"] = round(min(res["mae_pct"], (l / fill - 1) * 100), 2)
        if exit_px is not None:
            net, fees = swing_net(fill, exit_px, lots, fees_cfg, tax_rate)
            res.update({"status": "closed", "exit": exit_px, "exit_idx": j, "reason": reason, "days": d,
                        "net": net, "fees": fees, "ret_pct": round(net / (fill * lots * 1000) * 100, 2)})
            return res
        prevc = c
    res.update({"days": last_j - fi, "last": r["c"][last_j], "last_idx": last_j, "lock": pend,
                "ret_pct": round((r["c"][last_j] / fill - 1) * 100, 2)})   # 未實現（未扣費）
    return res


# ---------------------------------------------------------------- 主流程
def process(cfg, snaps, days, allow_new=True):
    """逐日處理：既有 ledger 日期照紀錄重放；新日期偵測訊號並寫入 days（append-only）。
    回傳 (days, state)。allow_new=False 供審計重算用（不新增日期）。"""
    scfg = swing_cfg(cfg)
    fees_cfg, lots, tax = cfg["fees"], scfg["lots"], scfg["swing_tax"]
    n = len(snaps)
    dates = [s["date"] for s in snaps]
    S = build_series(snaps)
    ew, mkt5, breadth5 = compute_gauge(S, snaps, scfg)
    taken = []       # {date, idx, sig, res}
    unfilled = []
    gate_days = []   # 閘門開啟日索引
    events = []      # [{start, end, idxs}]

    def open_at(i):
        return [x for x in taken if x["res"]["status"] in ("open", "closed")
                and x["res"]["fill_idx"] <= i and (x["res"]["status"] == "open" or x["res"]["exit_idx"] > i)]

    def recently_traded(code, i):
        for x in taken:
            if x["sig"]["code"] != code:
                continue
            st = x["res"]["status"]
            if st == "pending" or (st == "open" and x["res"]["fill_idx"] <= i):
                return True
            if st == "closed" and (x["res"]["exit_idx"] > i or i - x["idx"] <= scfg["dedup_days"]):
                return True
        return False

    def breaker_active(i, tier):
        """同一事件內最近兩批（已全部平倉）停損比例皆 ≥ 門檻 → 斷路器；反彈確認日（T2）解除。"""
        bk = scfg["breaker"]
        if not bk["enabled"] or tier == "T2" or not events:
            return False
        settled = []
        for idx in events[-1]["idxs"]:
            batch = [x for x in taken if x["idx"] == idx and x["res"]["status"] in ("open", "closed")]
            if not batch:
                continue
            if any(x["res"]["status"] == "open" or x["res"]["exit_idx"] > i for x in batch):
                continue
            settled.append(sum(x["res"]["reason"].startswith("stop") for x in batch) / len(batch))
        return len(settled) >= 2 and all(r >= bk["stop_ratio"] for r in settled[-2:])

    for i in range(n):
        d = dates[i]
        state, tier = gate_state(mkt5[i], ew[i], scfg["gate"])
        rec = days.get(d)
        if rec is None:
            if not allow_new:
                continue
            rec = {"date": d, "mkt5": round(mkt5[i], 2) if mkt5[i] is not None else None,
                   "ew": round(ew[i] * 100, 2) if ew[i] is not None else None,
                   "breadth5": round(breadth5[i], 3) if breadth5[i] is not None else None,
                   "state": state, "tier": tier, "signals": [], "breaker": False}
            if state == "open":
                if events and i - events[-1]["end"] <= scfg["dedup_days"]:
                    events[-1]["end"] = i
                    events[-1]["idxs"].append(i)
                else:
                    events.append({"start": i, "end": i, "idxs": [i]})
                sigs = [s for s in detect_signals(S, snaps, i, scfg) if not recently_traded(s["code"], i)]
                for k, s in enumerate(sigs, 1):
                    s["rank"] = k
                brk = breaker_active(i, tier)
                rec["breaker"] = brk
                # 槽位上限：一般日 max_positions；反彈確認日（T2）可擴到 t2_max_positions（不低於一般上限）
                cap = max(scfg["max_positions"], scfg["t2_max_positions"]) if tier == "T2" else scfg["max_positions"]
                rec["cap"] = cap
                free = max(0, cap - len(open_at(i)))
                slot = 0
                for s in sigs:
                    s["blocked"] = bool(brk and scfg["breaker"]["block_entries"])
                    s["taken"] = (not s["blocked"]) and slot < free
                    slot += s["taken"]
                rec["signals"] = sigs
                rec["open_before"] = len(open_at(i))
            days[d] = rec
        else:
            if rec.get("state") == "open":
                if events and i - events[-1]["end"] <= scfg["dedup_days"]:
                    events[-1]["end"] = i
                    events[-1]["idxs"].append(i)
                else:
                    events.append({"start": i, "end": i, "idxs": [i]})
        if rec.get("state") == "open":
            gate_days.append(i)
        for s in rec.get("signals", []):
            if not s.get("taken"):
                continue
            res = simulate_signal(S, s["code"], i, s.get("rules") or scfg["exit"], fees_cfg, lots, tax)
            x = {"date": d, "idx": i, "sig": s, "res": res}
            taken.append(x)
            if res["status"] == "unfilled":
                unfilled.append(x)

    # ---------- 輸出 state ----------
    def row(x):
        s, r = x["sig"], x["res"]
        base = {"signal_date": x["date"], "code": s["code"], "name": s["name"], "market": s["market"],
                "setup": s["setup"], "setups": s["setups"], "rank": s.get("rank"), "close": s["close"],
                "drawdown_pct": s["drawdown_pct"], "drop5_pct": s["drop5_pct"], "status": r["status"]}
        if r["status"] == "unfilled":
            base.update({"reason": r["reason"], "open_next": r.get("open_next"), "limit_up": r.get("limit_up"),
                         "fill_date": dates[x["idx"] + 1] if x["idx"] + 1 < n else None})
        elif r["status"] in ("open", "closed"):
            base.update({"fill": r["fill"], "fill_date": dates[r["fill_idx"]], "stop": r["stop"], "target": r["target"],
                         "mfe_pct": r["mfe_pct"], "mae_pct": r["mae_pct"], "days": r["days"], "ret_pct": r["ret_pct"],
                         "rules": s.get("rules") or scfg["exit"]})
            if r["status"] == "closed":
                base.update({"exit": r["exit"], "exit_date": dates[r["exit_idx"]], "reason": r["reason"],
                             "net": r["net"], "fees": r["fees"]})
            else:
                base.update({"last": r["last"], "last_date": dates[r["last_idx"]], "lock": r["lock"],
                             "remaining_days": max(0, (s.get("rules") or scfg["exit"])["max_hold_days"] - r["days"])})
        return base

    peak_open = max((len(open_at(i)) for i in range(n)), default=0)   # 最高同時持倉（資金需求）
    closed = [row(x) for x in taken if x["res"]["status"] == "closed"]
    positions = [row(x) for x in taken if x["res"]["status"] == "open"]
    pending = [row(x) for x in taken if x["res"]["status"] == "pending"]
    unf = [row(x) for x in unfilled]
    pp = scfg["position_pct"] / 100

    def stats(rows):
        c = [r for r in rows if r["status"] == "closed"]
        if not c:
            return {"n_closed": 0}
        rets = [r["ret_pct"] for r in c]
        return {"n_closed": len(c), "win_rate": round(sum(x > 0 for x in rets) / len(c) * 100, 1),
                "hit_rate": round(sum(r["reason"] == "target" for r in c) / len(c) * 100, 1),
                "stop_rate": round(sum(r["reason"].startswith("stop") for r in c) / len(c) * 100, 1),
                "mean_ret_pct": round(sum(rets) / len(c), 2),
                "median_days": sorted(r["days"] for r in c)[len(c) // 2],
                "hold5_rate": round(sum(r["days"] >= 5 for r in c) / len(c) * 100, 1),
                "contrib_pct": round(sum(x * pp for x in rets), 2),
                "net_ntd": sum(r["net"] for r in c)}

    ev_out = []
    for k, e in enumerate(events, 1):
        idxs = set(e["idxs"])
        rows_e = [row(x) for x in taken if x["idx"] in idxs]
        n_sig = sum(len(days[dates[i]]["signals"]) for i in e["idxs"])
        ev_out.append({"id": k, "start": dates[e["start"]], "end": dates[e["end"]],
                       "days": [{"date": dates[i], "tier": days[dates[i]]["tier"], "mkt5": days[dates[i]]["mkt5"],
                                 "ew": days[dates[i]]["ew"], "n_signals": len(days[dates[i]]["signals"]),
                                 "n_taken": sum(1 for s in days[dates[i]]["signals"] if s.get("taken")),
                                 "cap": days[dates[i]].get("cap"), "open_before": days[dates[i]].get("open_before"),
                                 "breaker": days[dates[i]].get("breaker", False)} for i in e["idxs"]],
                       "n_signals": n_sig, "n_taken": len(rows_e),
                       "n_unfilled": sum(r["status"] == "unfilled" for r in rows_e),
                       "n_open": sum(r["status"] == "open" for r in rows_e),
                       "n_pending": sum(r["status"] == "pending" for r in rows_e),
                       "stats": stats(rows_e), "trades": rows_e})
    today = days.get(dates[-1]) or {"date": dates[-1], "state": state, "tier": tier, "signals": []}
    hist = [{"date": dates[i], "mkt5": round(mkt5[i], 2) if mkt5[i] is not None else None,
             "ew": round(ew[i] * 100, 2) if ew[i] is not None else None,
             "breadth5": round(breadth5[i], 3) if breadth5[i] is not None else None,
             "state": gate_state(mkt5[i], ew[i], scfg["gate"])[0]}
            for i in range(max(0, n - scfg["history_days"]), n)]
    g_state, g_tier = gate_state(mkt5[-1], ew[-1], scfg["gate"])
    state_doc = {
        "generated_on": dates[-1], "updated_at": datetime.now(TAIPEI).isoformat(timespec="seconds"),
        "gauge": {"date": dates[-1], "mkt5": hist[-1]["mkt5"], "ew": hist[-1]["ew"], "breadth5": hist[-1]["breadth5"],
                  "state": g_state, "tier": g_tier, "breaker": today.get("breaker", False),
                  "open_positions": len(positions), "max_positions": scfg["max_positions"],
                  "cap_today": today.get("cap") or scfg["max_positions"]},
        "history": hist,
        "today": {"date": today["date"], "state": today.get("state"), "tier": today.get("tier"),
                  "breaker": today.get("breaker", False), "signals": today.get("signals", [])},
        "positions": sorted(positions, key=lambda r: r["fill_date"]),
        "pending": pending,
        "trades": sorted(closed, key=lambda r: (r["exit_date"], r["code"]), reverse=True),
        "unfilled": sorted(unf, key=lambda r: r["signal_date"], reverse=True)[:60],
        "events": ev_out[::-1],
        "summary": {**stats(closed), "n_events": len(ev_out), "n_gate_days": len(gate_days),
                    "n_taken": len(taken), "n_unfilled": len(unf), "n_open": len(positions), "peak_open": peak_open},
        "rules": {"gate": scfg["gate"], "exit": scfg["exit"], "max_positions": scfg["max_positions"],
                  "t2_max_positions": max(scfg["max_positions"], scfg["t2_max_positions"]),
                  "position_pct": scfg["position_pct"], "lots": lots, "setups": SETUP_META,
                  "ranking": scfg.get("ranking") or {},
                  "breaker": scfg["breaker"], "dedup_days": scfg["dedup_days"]},
    }
    return days, state_doc


def run_swing(cfg, snaps, rebuild=False):
    scfg = swing_cfg(cfg)
    if not scfg["enabled"]:
        print("[swing] 已停用")
        return None
    ledger = {} if rebuild else load_json(LEDGER, {})
    days = {} if rebuild else dict(ledger.get("days") or {})
    days, state = process(cfg, snaps, days, allow_new=True)
    save_json(LEDGER, {"version": 1, "last_date": snaps[-1]["date"], "days": days})
    save_json(STATE, state)
    g, sm = state["gauge"], state["summary"]
    tier = f"（{g['tier']}）" if g["tier"] else ""
    print(f"[swing] {g['date']} 等權5日 {g['mkt5']:+.2f}% 當日 {g['ew']:+.2f}% → 閘門 {g['state']}{tier}；"
          f"今日訊號 {len(state['today']['signals'])}（取 {sum(1 for s in state['today']['signals'] if s.get('taken'))}）、"
          f"持倉 {sm['n_open']}、事件 {sm['n_events']}、已平倉 {sm.get('n_closed', 0)} 筆"
          + (f"（勝率 {sm['win_rate']}%、達標 {sm['hit_rate']}%、單筆均 {sm['mean_ret_pct']:+.2f}%）" if sm.get("n_closed") else ""))
    return state


if __name__ == "__main__":
    import twstock
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    run_swing(cfg, twstock.load_snapshots(), rebuild="--rebuild" in sys.argv)
