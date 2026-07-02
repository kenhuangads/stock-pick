"""價格模型迭代：建議進出場價也跟著復盤結果滾動優化。

建議價 = CDP 基準價 + 偏移 × 當日振幅R：
  entry  = NL + entry_shift·R   （掛買價：往下=更好的價、但更難成交）
  target = NH + target_shift·R  （停利價：往上=賺更多、但更難觸及）
  stop   = AL + stop_shift·R    （停損價：往上=停損更緊、砍得快）

每日收盤後（walk-forward，只用歷史）：
1. 取最近 window_days 個復盤日的建議單，用其記錄的 cdp_base + 當日實際 OHLC，
   對 shift_grid 的每一組偏移「重放」模擬（同 review.py 的保守規則），
   計算該組偏移下的窗口總淨損益。
2. 約束：成交筆數不得低於 0 偏移基準的 min_fill_ratio（防止「都不成交淨損益 0」勝出）。
3. 遲滯：新偏移的窗口淨損益需明顯優於目前偏移（improve_margin_pct + 100 元）才切換，
   避免每天在雜訊中反覆跳動；並列時偏好偏移絕對值小（貼近原始 CDP）。
4. 所有切換寫入 log，前端「每日復盤」頁完整呈現診斷與軌跡。
"""
from indicators import round_tick, tick_size
from review import trade_fees

ZERO = {"entry": 0.0, "target": 0.0, "stop": 0.0}


def _sim_one(rec, shifts, fees_cfg, lots):
    """用復盤紀錄裡的 cdp_base 與當日 OHLC，以指定偏移重放一筆模擬。
    回傳 (filled, net, exit_reason)。規則與 review.simulate_pick 一致（保守假設）。"""
    base = rec["cdp_base"]
    r = base["r"]
    entry = round_tick(base["nl"] + shifts["entry"] * r, "down")
    target = round_tick(base["nh"] + shifts["target"] * r, "up")
    stop = round_tick(base["al"] + shifts["stop"] * r, "down")
    stop = min(stop, round_tick(entry - tick_size(entry), "down"))
    target = max(target, round_tick(entry + tick_size(entry), "up"))

    o, h, l, c = rec["day_open"], rec["day_high"], rec["day_low"], rec["day_close"]
    if l > entry:
        # 未成交；若收盤高於掛價代表「掛太低錯過行情」
        return False, 0, ("runaway" if c > entry else "nofill")
    fill = min(entry, o)
    if l <= stop and fill > stop:
        exit_price, reason = stop, "stop"
    elif h >= target:
        exit_price, reason = target, "target"
    else:
        exit_price, reason = c, "close"
    fee_b, fee_s, tax = trade_fees(fill, exit_price, lots, fees_cfg)
    net = int((exit_price - fill) * lots * 1000) - fee_b - fee_s - tax
    return True, net, reason


def _replay(records, shifts, fees_cfg, lots):
    """整個窗口以指定偏移重放，回傳統計。"""
    stat = {"n": len(records), "fills": 0, "net": 0, "wins": 0,
            "target": 0, "stop": 0, "close": 0, "runaway": 0}
    for rec in records:
        filled, net, reason = _sim_one(rec, shifts, fees_cfg, lots)
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
    records = [r for day in window for r in day["picks"] if r.get("cdp_base")]
    fees_cfg, lots = cfg["fees"], cfg["simulation"]["lots_per_trade"]

    base = _replay(records, ZERO, fees_cfg, lots)
    cur = _replay(records, current, fees_cfg, lots)

    if base["fills"] >= pcfg["min_trades"]:
        grid = pcfg["shift_grid"]
        min_fills = max(1, round(base["fills"] * pcfg["min_fill_ratio"]))
        candidates = []
        for a in grid:
            for b in grid:
                for c in grid:
                    sh = {"entry": a, "target": b, "stop": c}
                    st = _replay(records, sh, fees_cfg, lots)
                    if st["fills"] < min_fills:
                        continue
                    dist_cur = abs(a - current["entry"]) + abs(b - current["target"]) + abs(c - current["stop"])
                    candidates.append((st["net"], -(abs(a) + abs(b) + abs(c)), -dist_cur, sh, st))
        if candidates:
            candidates.sort(key=lambda x: (-x[0], x[1], x[2]))
            best_net, _, _, best_sh, best_st = candidates[0]
            margin = max(100, abs(cur["net"]) * pcfg["improve_margin_pct"] / 100)
            if best_sh != current and best_net >= cur["net"] + margin:
                log.append({"date": as_of_date,
                            "msg": f"價格模型調整：進場 {current['entry']:+.2f}→{best_sh['entry']:+.2f}R、"
                                   f"停利 {current['target']:+.2f}→{best_sh['target']:+.2f}R、"
                                   f"停損 {current['stop']:+.2f}→{best_sh['stop']:+.2f}R"
                                   f"（窗口淨損益 {cur['net']:,} → {best_net:,} 元）"})
                current, cur = best_sh, best_st

    doc = {
        "updated": as_of_date,
        "window_days": pcfg["window_days"],
        "shifts": current,
        "stats": {
            "n_picks": cur["n"],
            "fills": cur["fills"],
            "fill_rate": _pct(cur["fills"], cur["n"]),
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
