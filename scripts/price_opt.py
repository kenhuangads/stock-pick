"""價格模型迭代：建議進出場價也跟著復盤結果滾動優化。

建議價 = CDP 基準價 + 偏移 × 當日振幅R：
  entry  = NL + entry_shift·R   （掛買價：往下=更好的價、但更難成交）
  target = NH + target_shift·R  （停利價：往上=賺更多、但更難觸及）
  stop   = AL + stop_shift·R    （停損價：往上=停損更緊、砍得快）

每日收盤後（walk-forward，只用歷史）：
1. 取最近 window_days 個復盤日的建議單，用其記錄的 cdp_base + 當日實際 OHLC，
   對 entry_grid × exit_grid² 的每一組偏移「重放」模擬（同 review.py 的保守規則），
   計算該組偏移下的窗口成交率與總淨損益。
2. 目標優先序：**成交率 ≥ target_fill_rate（預設 67%）為第一目標**——
   達標組合中取淨損益最佳；若無任何組合達標，取成交率最高者逼近目標。
3. 遲滯：同為達標組合時，新偏移需明顯優於目前（improve_margin_pct + 100 元）才切換；
   皆未達標時，成交率需提升 ≥ 2 個百分點才切換。避免每天在雜訊中反覆跳動。
4. 所有切換寫入 log，前端「每日復盤」頁完整呈現診斷與軌跡。
"""
from indicators import round_tick, tick_size
from review import trade_fees, simulate_trade
from intraday import load_intraday

ZERO = {"entry": 0.0, "target": 0.0, "stop": 0.0}


def _sim_one(rec, shifts, fees_cfg, lots, bars=None):
    """用復盤紀錄裡的 cdp_base 與當日行情（5分K優先），以指定偏移重放一筆模擬。
    回傳 (filled, net, exit_reason)。走 review.simulate_trade 共用核心，口徑一致。"""
    base = rec["cdp_base"]
    r = base["r"]
    entry = round_tick(base["nl"] + shifts["entry"] * r, "down")
    target = round_tick(base["nh"] + shifts["target"] * r, "up")
    stop = round_tick(base["al"] + shifts["stop"] * r, "down")
    stop = min(stop, round_tick(entry - tick_size(entry), "down"))
    target = max(target, round_tick(entry + tick_size(entry), "up"))

    ohlc = {"o": rec["day_open"], "h": rec["day_high"], "l": rec["day_low"], "c": rec["day_close"]}
    filled, fill, exit_price, reason, _ = simulate_trade(entry, target, stop, ohlc, bars)
    if not filled:
        # 未成交；若收盤高於掛價代表「掛太低錯過行情」
        return False, 0, ("runaway" if ohlc["c"] > entry else "nofill")
    fee_b, fee_s, tax = trade_fees(fill, exit_price, lots, fees_cfg)
    net = int((exit_price - fill) * lots * 1000) - fee_b - fee_s - tax
    return True, net, reason


def _replay(records, shifts, fees_cfg, lots):
    """整個窗口以指定偏移重放，回傳統計。records: [(rec, bars), ...]"""
    stat = {"n": len(records), "fills": 0, "net": 0, "wins": 0,
            "target": 0, "stop": 0, "close": 0, "runaway": 0}
    for rec, bars in records:
        filled, net, reason = _sim_one(rec, shifts, fees_cfg, lots, bars)
        if filled:
            stat["fills"] += 1
            stat["net"] += net
            stat["wins"] += 1 if net > 0 else 0
            stat[reason] += 1
        elif reason == "runaway":
            stat["runaway"] += 1
    return stat


def _pct(a, b):
    return round(a / b * 100, 1) if b else None


def run_price_opt(reviews, cfg, prev_doc, as_of_date):
    """回傳 (shifts, price_doc)。歷史紀錄缺 cdp_base（舊格式）時自動略過該筆。"""
    pcfg = cfg.get("price_optimizer")
    if not pcfg:
        return dict(ZERO), None

    current = dict(ZERO)
    if prev_doc and prev_doc.get("shifts"):
        current.update(prev_doc["shifts"])
    log = (prev_doc or {}).get("log", [])

    window = reviews[-pcfg["window_days"]:] if reviews else []
    records = []
    for day in window:
        bars_by_code = load_intraday(day["date"])
        for r in day["picks"]:
            if r.get("cdp_base"):
                records.append((r, bars_by_code.get(r["code"])))
    fees_cfg, lots = cfg["fees"], cfg["simulation"]["lots_per_trade"]

    base = _replay(records, ZERO, fees_cfg, lots)
    cur = _replay(records, current, fees_cfg, lots)
    target_fr = pcfg.get("target_fill_rate", 0)

    def fill_rate(st):
        return st["fills"] / st["n"] if st["n"] else 0.0

    def qualified(st):
        return fill_rate(st) >= target_fr

    if base["fills"] >= pcfg["min_trades"]:
        grid_entry = pcfg.get("entry_grid") or pcfg.get("shift_grid", [])
        grid_exit = pcfg.get("exit_grid") or pcfg.get("shift_grid", [])
        scored = []
        for a in grid_entry:
            for b in grid_exit:
                for c in grid_exit:
                    sh = {"entry": a, "target": b, "stop": c}
                    st = _replay(records, sh, fees_cfg, lots)
                    dist0 = abs(a) + abs(b) + abs(c)
                    scored.append((sh, st, dist0))
        # 成交率達標優先；達標者比淨損益，未達標者比成交率（越接近目標越好）
        qual = sorted([x for x in scored if qualified(x[1])], key=lambda x: (-x[1]["net"], x[2]))
        if qual:
            best_sh, best_st, _ = qual[0]
        else:
            near = sorted(scored, key=lambda x: (-fill_rate(x[1]), -x[1]["net"], x[2]))
            best_sh, best_st, _ = near[0]

        if best_sh != current:
            margin = max(100, abs(cur["net"]) * pcfg["improve_margin_pct"] / 100)
            cur_q, best_q = qualified(cur), qualified(best_st)
            switch = (best_q and not cur_q) or \
                     (best_q and cur_q and best_st["net"] >= cur["net"] + margin) or \
                     (not best_q and not cur_q and fill_rate(best_st) >= fill_rate(cur) + 0.02)
            if switch:
                log.append({"date": as_of_date,
                            "msg": f"價格模型調整：進場 {current['entry']:+.2f}→{best_sh['entry']:+.2f}R、"
                                   f"停利 {current['target']:+.2f}→{best_sh['target']:+.2f}R、"
                                   f"停損 {current['stop']:+.2f}→{best_sh['stop']:+.2f}R"
                                   f"（成交率 {fill_rate(cur)*100:.1f}%→{fill_rate(best_st)*100:.1f}%、"
                                   f"窗口淨損益 {cur['net']:,} → {best_st['net']:,} 元）"})
                current, cur = best_sh, best_st

    doc = {
        "updated": as_of_date,
        "window_days": pcfg["window_days"],
        "shifts": current,
        "stats": {
            "n_picks": cur["n"],
            "fills": cur["fills"],
            "fill_rate": _pct(cur["fills"], cur["n"]),
            "fill_target": round(target_fr * 100, 1) if target_fr else None,
            "win_rate": _pct(cur["wins"], cur["fills"]),
            "target_rate": _pct(cur["target"], cur["fills"]),
            "stop_rate": _pct(cur["stop"], cur["fills"]),
            "close_rate": _pct(cur["close"], cur["fills"]),
            "runaway_rate": _pct(cur["runaway"], cur["n"]),  # 掛價過低、行情跑掉的比率
            "net": cur["net"],
            "net_baseline": base["net"],                     # 0 偏移（原始 CDP）對照組
        },
        "log": log[-100:],
    }
    return current, doc
