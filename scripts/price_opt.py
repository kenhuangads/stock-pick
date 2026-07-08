"""價格模型迭代：建議進出場價也跟著復盤結果滾動優化。

建議價 = CDP 基準價 + 偏移 × 當日振幅R：
  entry  = NL + entry_shift·R   （掛買價：往下=更好的價、但更難成交）
  target = NH + target_shift·R  （停利價：往上=賺更多、但更難觸及）
  stop   = AL + stop_shift·R    （停損價：往上=停損更緊、砍得快）

每日收盤後（walk-forward，只用歷史）：
1. 取最近 window_days 個復盤日的建議單，用其記錄的 cdp_base + 當日實際 OHLC，
   對 entry_grid × exit_grid² 的每一組偏移「重放」模擬（同 review.py 的保守規則），
   計算該組偏移下的窗口成交率與總淨損益。
2. 搜尋空間除了三個價位偏移，還包含**出場引擎模式**（trail_grid × tstop_grid）：
   - trail：地板式移動停利距離（單位 R，0=關）——觸及停利價後改追蹤高點回落出場，
     原停利價為地板，讓「大賺」的尾巴多跑。
   - tstop：時間停損 bar index（null=關，36 ≈ 12:00）——中午前未觸停利即出場，
     依據實測收盤沖銷單的午後平均負漂移（報告的 12:00 法則）。
   兩者僅作用於 5 分K 可核實的交易；日K fallback 交易維持保守規則。
3. 目標優先序（config: price_optimizer.objective）：
   **成交率 ≥ target_fill_rate（預設 67%）永遠是第一硬門檻**，達標組合中再依目標挑選：
   - objective="payoff"（預設）：正期望值組合中，優先「賺賠比 ≥ min_payoff（預設 1.2）」
     者取淨損益最高；若無達標賺賠比者取淨損益最高——直接編碼「大賺小賠＋高期望值」，
     不會為衝勝率而犧牲賺賠結構。
   - objective="winrate"：正期望值中取勝率最高（同勝率比淨損益）。
   - objective="net"：達標組合中純取淨損益最佳。
   若無任何組合達標成交率，取成交率最高者逼近目標。
4. 遲滯：需明顯優於目前（improve_margin_pct + 100 元 / winrate 模式為勝率 ≥2pp）
   才切換。避免每天在雜訊中反覆跳動。
4. 所有切換寫入 log，前端「每日復盤」頁完整呈現診斷與軌跡。
"""
from indicators import round_tick, tick_size
from review import trade_fees, simulate_trade, bars_match_ohlc
from intraday import load_intraday

ZERO = {"entry": 0.0, "target": 0.0, "stop": 0.0, "trail": 0.0, "tstop": None}


def _sim_one(rec, shifts, fees_cfg, lots, bars=None):
    """用復盤紀錄裡的 cdp_base 與當日行情（5分K優先），以指定偏移＋出場模式重放一筆模擬。
    回傳 (filled, net, exit_reason)。走 review.simulate_trade 共用核心，口徑一致。"""
    base = rec["cdp_base"]
    r = base["r"]
    entry = round_tick(base["nl"] + shifts["entry"] * r, "down")
    target = round_tick(base["nh"] + shifts["target"] * r, "up")
    stop = round_tick(base["al"] + shifts["stop"] * r, "down")
    stop = min(stop, round_tick(entry - tick_size(entry), "down"))
    target = max(target, round_tick(entry + tick_size(entry), "up"))
    trail_mult = shifts.get("trail") or 0
    trail_dist = round(trail_mult * r, 2) if trail_mult else None
    tstop_bar = shifts.get("tstop")

    ohlc = {"o": rec["day_open"], "h": rec["day_high"], "l": rec["day_low"], "c": rec["day_close"]}
    filled, fill, exit_price, reason, _ = simulate_trade(
        entry, target, stop, ohlc, bars, trail_dist, tstop_bar)
    if not filled:
        # 未成交；若收盤高於掛價代表「掛太低錯過行情」
        return False, 0, ("runaway" if ohlc["c"] > entry else "nofill")
    fee_b, fee_s, tax = trade_fees(fill, exit_price, lots, fees_cfg)
    net = int((exit_price - fill) * lots * 1000) - fee_b - fee_s - tax
    return True, net, reason


def _replay(records, shifts, fees_cfg, lots):
    """整個窗口以指定偏移＋出場模式重放，回傳統計。records: [(rec, bars), ...]"""
    stat = {"n": len(records), "fills": 0, "net": 0, "wins": 0, "gw": 0, "gl": 0,
            "target": 0, "stop": 0, "close": 0, "trail": 0, "timeout": 0, "runaway": 0}
    for rec, bars in records:
        filled, net, reason = _sim_one(rec, shifts, fees_cfg, lots, bars)
        if filled:
            stat["fills"] += 1
            stat["net"] += net
            if net > 0:
                stat["wins"] += 1
                stat["gw"] += net
            else:
                stat["gl"] -= net
            stat[reason] += 1
        elif reason == "runaway":
            stat["runaway"] += 1
    return stat


def _payoff(st):
    """賺賠比 = 平均賺 / 平均賠；無虧損視為極大。"""
    losses = st["fills"] - st["wins"]
    if st["wins"] == 0:
        return 0.0
    if losses == 0 or st["gl"] == 0:
        return float("inf")
    return (st["gw"] / st["wins"]) / (st["gl"] / losses)


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
            if not r.get("cdp_base"):
                continue
            b = bars_by_code.get(r["code"])
            ohlc = {"o": r["day_open"], "h": r["day_high"], "l": r["day_low"], "c": r["day_close"]}
            if b and not bars_match_ohlc(b, ohlc):
                b = None  # 資料品質防線：與官方日K不符的 5分K 不得參與重放
            records.append((r, b))
    fees_cfg, lots = cfg["fees"], cfg["simulation"]["lots_per_trade"]

    base = _replay(records, ZERO, fees_cfg, lots)
    cur = _replay(records, current, fees_cfg, lots)
    target_fr = pcfg.get("target_fill_rate", 0)

    def fill_rate(st):
        return st["fills"] / st["n"] if st["n"] else 0.0

    def win_rate(st):
        return st["wins"] / st["fills"] if st["fills"] else 0.0

    def qualified(st):
        return fill_rate(st) >= target_fr

    objective = pcfg.get("objective", "net")

    if base["fills"] >= pcfg["min_trades"]:
        grid_entry = pcfg.get("entry_grid") or pcfg.get("shift_grid", [])
        grid_exit = pcfg.get("exit_grid") or pcfg.get("shift_grid", [])
        grid_trail = pcfg.get("trail_grid", [0])
        grid_tstop = pcfg.get("tstop_grid", [None])
        min_payoff = pcfg.get("min_payoff", 1.2)
        scored = []
        for a in grid_entry:
            for b in grid_exit:
                for c in grid_exit:
                    for t in grid_trail:
                        for ts in grid_tstop:
                            sh = {"entry": a, "target": b, "stop": c, "trail": t, "tstop": ts}
                            st = _replay(records, sh, fees_cfg, lots)
                            # 同分時偏好較小偏移與較簡單的出場模式（防過擬合傾向）
                            dist0 = abs(a) + abs(b) + abs(c) + (0.01 if t else 0) + (0.01 if ts is not None else 0)
                            scored.append((sh, st, dist0))
        # 成交率達標為硬門檻；達標組合中依 objective 挑選
        qual = [x for x in scored if qualified(x[1])]
        if qual:
            positive = [x for x in qual if x[1]["net"] > 0]
            if objective == "payoff" and positive:
                # 大賺小賠優先：賺賠比達標者中取淨損益最高；無達標者退回正期望值最高
                good = [x for x in positive if _payoff(x[1]) >= min_payoff]
                pool = good or positive
                best_sh, best_st, _ = sorted(pool, key=lambda x: (-x[1]["net"], x[2]))[0]
            elif objective == "winrate" and positive:
                best_sh, best_st, _ = sorted(positive, key=lambda x: (-win_rate(x[1]), -x[1]["net"], x[2]))[0]
            else:  # objective=net 或無任一正期望值 → 取總淨損益最大（保本）
                best_sh, best_st, _ = sorted(qual, key=lambda x: (-x[1]["net"], x[2]))[0]
        else:  # 無組合達標成交率 → 逼近目標
            best_sh, best_st, _ = sorted(scored, key=lambda x: (-fill_rate(x[1]), -x[1]["net"], x[2]))[0]

        if best_sh != current:
            cur_q, best_q = qualified(cur), qualified(best_st)
            if objective == "winrate":
                # 達標且正期望值下，勝率明顯提升(≥2pp)才換；或從「未達標/負期望值」進步到「達標且正期望值」
                cur_ok = cur_q and cur["net"] > 0
                best_ok = best_q and best_st["net"] > 0
                switch = (best_ok and not cur_ok) or \
                         (best_ok and cur_ok and win_rate(best_st) >= win_rate(cur) + 0.02) or \
                         (not best_q and not cur_q and fill_rate(best_st) >= fill_rate(cur) + 0.02)
            else:
                margin = max(100, abs(cur["net"]) * pcfg["improve_margin_pct"] / 100)
                switch = (best_q and not cur_q) or \
                         (best_q and cur_q and best_st["net"] >= cur["net"] + margin) or \
                         (not best_q and not cur_q and fill_rate(best_st) >= fill_rate(cur) + 0.02)
                if objective == "payoff" and not switch and best_q and cur_q:
                    # 賺賠比從不及格→及格且不明顯犧牲期望值 → 換（大賺小賠結構優先）
                    switch = (_payoff(best_st) >= min_payoff > _payoff(cur)
                              and best_st["net"] > 0 and best_st["net"] >= cur["net"] - margin)
            if switch:
                def _mode(sh):
                    t = f"{sh['trail']:.2f}R" if sh.get("trail") else "關"
                    ts = "12:00" if sh.get("tstop") is not None else "關"
                    return f"移動停利 {t}、時間停損 {ts}"
                def _pf(st):
                    p = _payoff(st)
                    return "∞" if p == float("inf") else f"{p:.2f}"
                log.append({"date": as_of_date,
                            "msg": f"價格模型調整：進場 {current['entry']:+.2f}→{best_sh['entry']:+.2f}R、"
                                   f"停利 {current['target']:+.2f}→{best_sh['target']:+.2f}R、"
                                   f"停損 {current['stop']:+.2f}→{best_sh['stop']:+.2f}R、{_mode(best_sh)}"
                                   f"（成交率 {fill_rate(cur)*100:.1f}%→{fill_rate(best_st)*100:.1f}%、"
                                   f"勝率 {win_rate(cur)*100:.1f}%→{win_rate(best_st)*100:.1f}%、"
                                   f"賺賠比 {_pf(cur)}→{_pf(best_st)}、"
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
            "payoff": (round(_payoff(cur), 2) if _payoff(cur) != float("inf") else None),  # 賺賠比
            "target_rate": _pct(cur["target"], cur["fills"]),
            "trail_rate": _pct(cur["trail"], cur["fills"]),
            "stop_rate": _pct(cur["stop"], cur["fills"]),
            "timeout_rate": _pct(cur["timeout"], cur["fills"]),
            "close_rate": _pct(cur["close"], cur["fills"]),
            "runaway_rate": _pct(cur["runaway"], cur["n"]),  # 掛價過低、行情跑掉的比率
            "net": cur["net"],
            "net_baseline": base["net"],                     # 0 偏移（原始 CDP）對照組
        },
        "log": log[-100:],
    }
    return current, doc
