"""價格模型迭代：建議進出場價也跟著復盤結果滾動優化（多空各自獨立優化）。

做多建議價 = CDP 基準價 + 偏移 × 當日振幅R：
  entry  = NL + entry_shift·R   （掛買價：往下=更好的價、但更難成交）
  target = NH + target_shift·R  （停利價：往上=賺更多、但更難觸及）
  stop   = AL + stop_shift·R    （停損價：往上=停損更緊、砍得快）

做空為完全鏡像（偏移一律「減」＝往市場方向移動）：
  entry  = NH − entry_shift·R   （掛空價：往下=更容易成交、但空得較差）
  target = NL − target_shift·R  （回補價：往上=更容易回補停利）
  stop   = AH − stop_shift·R    （停損價：往下=停損更緊）
做空樣本不足 min_trades 時，暫借做多側偏移起步（成交物理對稱），
樣本累積夠後即獨立走 walk-forward 網格優化、與做多互不污染。

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
from indicators import price_from_shifts
from review import trade_fees, simulate_trade, bars_match_ohlc, limit_down_price, limit_up_price
from intraday import load_intraday

ZERO = {"entry": 0.0, "target": 0.0, "stop": 0.0, "trail": 0.0, "tstop": None, "mode": "revert"}


def _sim_one(rec, shifts, fees_cfg, lots, bars=None, strict_fill=False, side="long", gap_void=False):
    """用復盤紀錄裡的 cdp_base 與當日行情（5分K優先），以指定偏移＋出場模式重放一筆模擬。
    回傳 (filled, net, exit_reason)。價位建構走 indicators.price_from_shifts、
    模擬走 review.simulate_trade——與選股/復盤共用，口徑保證一致。"""
    base = rec["cdp_base"]
    r = base["r"]
    mode = shifts.get("mode", "revert")
    entry, target, stop = price_from_shifts(base, r, shifts, side)
    trail_mult = shifts.get("trail") or 0
    trail_dist = round(trail_mult * r, 2) if trail_mult else None
    tstop_bar = shifts.get("tstop")

    ohlc = {"o": rec["day_open"], "h": rec["day_high"], "l": rec["day_low"], "c": rec["day_close"]}
    filled, fill, exit_price, reason, _ = simulate_trade(
        entry, target, stop, ohlc, bars, trail_dist, tstop_bar,
        strict_fill=strict_fill, limit_dn=limit_down_price(rec.get("prev_close")),
        side=side, limit_up=limit_up_price(rec.get("prev_close")), entry_mode=mode,
        gap_void=gap_void)
    if not filled:
        if reason == "gapvoid":
            return False, 0, "gapvoid"   # 開盤穿價主動作廢：非「掛太遠」、不計 runaway
        # 未成交且行情往獲利方向跑掉＝「掛太遠錯過行情」（做多看漲過掛價、做空看跌破掛價）
        # 突破模式的未成交＝行情根本沒往方向走（good skip），c 不可能越過觸發價
        runaway = ohlc["c"] > entry if side == "long" else ohlc["c"] < entry
        return False, 0, ("runaway" if runaway else "nofill")
    if side == "short":
        fee_b, fee_s, tax = trade_fees(exit_price, fill, lots, fees_cfg)  # 先賣(fill)後買(exit)，稅課賣出腿
        net = int((fill - exit_price) * lots * 1000) - fee_b - fee_s - tax
    else:
        fee_b, fee_s, tax = trade_fees(fill, exit_price, lots, fees_cfg)
        net = int((exit_price - fill) * lots * 1000) - fee_b - fee_s - tax
    return True, net, reason


def _replay(records, shifts, fees_cfg, lots, strict_fill=False, side="long", gap_void=False):
    """整個窗口以指定偏移＋出場模式重放，回傳統計。records: [(rec, bars), ...]"""
    stat = {"n": len(records), "fills": 0, "net": 0, "wins": 0, "gw": 0, "gl": 0,
            "target": 0, "stop": 0, "close": 0, "trail": 0, "timeout": 0, "runaway": 0,
            "gapvoid": 0}
    for rec, bars in records:
        filled, net, reason = _sim_one(rec, shifts, fees_cfg, lots, bars, strict_fill, side, gap_void)
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
        elif reason == "gapvoid":
            stat["gapvoid"] += 1
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


def _stats_block(cur, base, target_fr):
    return {
        "n_picks": cur["n"],
        "fills": cur["fills"],
        # 成交率分母排除開盤穿價作廢單（主動撤單非掛太遠；與優化器資格門檻同口徑）
        "fill_rate": _pct(cur["fills"], cur["n"] - cur.get("gapvoid", 0)),
        "void_rate": _pct(cur.get("gapvoid", 0), cur["n"]),
        "fill_target": round(target_fr * 100, 1) if target_fr else None,
        "win_rate": _pct(cur["wins"], cur["fills"]),
        "payoff": (round(_payoff(cur), 2) if _payoff(cur) != float("inf") else None),  # 賺賠比
        "target_rate": _pct(cur["target"], cur["fills"]),
        "trail_rate": _pct(cur["trail"], cur["fills"]),
        "stop_rate": _pct(cur["stop"], cur["fills"]),
        "timeout_rate": _pct(cur["timeout"], cur["fills"]),
        "close_rate": _pct(cur["close"], cur["fills"]),
        "runaway_rate": _pct(cur["runaway"], cur["n"]),  # 掛價過遠、行情跑掉的比率
        "net": cur["net"],
        "net_baseline": base["net"],                     # 0 偏移（原始 CDP）對照組
    }


def _nearest_idx(grid, val):
    """網格中最接近 val 的索引（None 只與 None 相等；不在網格上的舊值取最近點）。"""
    best, bd = 0, None
    for i, g in enumerate(grid):
        if g is None or val is None:
            d = 0 if g == val else 1e9
        else:
            d = abs(g - val)
        if bd is None or d < bd:
            best, bd = i, d
    return best


def _step_toward(current, best, grids, max_step):
    """限步採納：每個維度沿網格向 best 至多移動 max_step 格。
    20 日窗口樣本僅 30~50 筆、網格數千組——全域跳躍會追著噪音反覆橫跳
    （實例：8/10 做多一次同時把停損 0.3→0.1、關移動停利、關時間停損，當日 5 筆全拖尾）。
    限步後需連續多日同方向的證據才走得遠＝天然低通濾波。"""
    out = dict(current)
    for dim, grid in grids.items():
        ci, bi = _nearest_idx(grid, current.get(dim)), _nearest_idx(grid, best[dim])
        if ci == bi:
            out[dim] = grid[bi]   # 對齊網格（吸收舊的非網格值）
        else:
            step = max(-max_step, min(max_step, bi - ci))
            out[dim] = grid[ci + step]
    return out


def _fmt_mode(sh):
    t = f"{sh['trail']:.2f}R" if sh.get("trail") else "關"
    ts = sh.get("tstop")
    if ts is not None:
        mins = 9 * 60 + ts * 5
        tss = f"{mins // 60:02d}:{mins % 60:02d}"
    else:
        tss = "關"
    em = "、進場=突破追價" if sh.get("mode") == "breakout" else ""
    return f"移動停利 {t}、時間停損 {tss}{em}"


def _optimize_side(records, pcfg, fees_cfg, lots, strict, current, log, as_of_date, side,
                   gap_void=False):
    """單一方向的網格搜尋＋遲滯切換。回傳 (current_shifts, cur_stat, base_stat)。
    records 為該方向的復盤紀錄；current 為現行偏移（就地不改，回傳新 dict）。

    搜尋空間 = entry×target×stop×trail×tstop×進場模式（revert 逆勢掛單／breakout 突破追價）。
    採納紀律（max_step_per_day > 0 時）：
    - 同模式：向全域最佳「每維最多走一格」，防止單日大跳追噪音；
    - 跨模式：需 mode_switch_margin_mult×margin 的更強證據才切換（切換即整組採納，
      因新模式下無「現行參數」可錨）；證據不足時退回同模式最佳繼續逐步逼近；
    - 同分傾向：偏好出場引擎「開啟」（法醫實證 trail/tstop 開優於關——收盤沖銷單
      午後平均負漂移；舊版偏好「關」導致引擎被窗口噪音關掉）。"""
    label = "做多" if side == "long" else "空方"
    target_fr = pcfg.get("target_fill_rate", 0)
    objective = pcfg.get("objective", "net")
    step_lim = pcfg.get("max_step_per_day", 0)
    current = dict(ZERO, **current)   # 補齊缺漏鍵（舊檔無 mode → revert）

    base = _replay(records, ZERO, fees_cfg, lots, strict, side, gap_void)
    cur = _replay(records, current, fees_cfg, lots, strict, side, gap_void)

    def fill_rate(st):
        denom = st["n"] - st.get("gapvoid", 0)   # 作廢單=主動撤單，不入成交率分母
        return st["fills"] / denom if denom else 0.0

    def win_rate(st):
        return st["wins"] / st["fills"] if st["fills"] else 0.0

    def qualified(st, mode="revert"):
        # 成交率硬門檻是「限價掛單」的可執行性要求（掛太遠=建議做不到）。
        # 突破追價是停損單語意：未觸發=無趨勢不進場（設計目的、非執行失敗），
        # 拿觸發率當門檻會結構性鎖死該模式——改要求絕對成交筆數達統計門檻。
        if mode == "breakout":
            return st["fills"] >= pcfg["min_trades"]
        return fill_rate(st) >= target_fr

    if base["fills"] >= pcfg["min_trades"]:
        grid_entry = pcfg.get("entry_grid") or pcfg.get("shift_grid", [])
        grid_exit = pcfg.get("exit_grid") or pcfg.get("shift_grid", [])
        grid_stop = pcfg.get("stop_grid") or grid_exit   # 停損可用獨立網格（支援更緊的停損）
        grid_trail = pcfg.get("trail_grid", [0])
        grid_tstop = pcfg.get("tstop_grid", [None])
        modes = pcfg.get("entry_modes") or ["revert"]
        min_payoff = pcfg.get("min_payoff", 1.2)
        grids = {"entry": grid_entry, "target": grid_exit, "stop": grid_stop,
                 "trail": grid_trail, "tstop": grid_tstop}
        scored = []
        for mode in modes:
            for a in grid_entry:
                for b in grid_exit:
                    for c in grid_stop:
                        for t in grid_trail:
                            for ts in grid_tstop:
                                sh = {"entry": a, "target": b, "stop": c, "trail": t,
                                      "tstop": ts, "mode": mode}
                                st = _replay(records, sh, fees_cfg, lots, strict, side, gap_void)
                                # 同分傾向：小偏移優先；出場引擎依採納紀律決定偏好方向
                                if step_lim:
                                    eng = (0.01 if not t else 0) + (0.01 if ts is None else 0)
                                else:
                                    eng = (0.01 if t else 0) + (0.01 if ts is not None else 0)
                                scored.append((sh, st, abs(a) + abs(b) + abs(c) + eng))

        def _select(pool):
            """成交率達標為硬門檻；達標組合中依 objective 挑選。回傳 (sh, st)。"""
            pool = pool or scored   # 防護：同模式池為空（config 移除該模式）時退回全池
            qual = [x for x in pool if qualified(x[1], x[0].get("mode", "revert"))]
            if qual:
                positive = [x for x in qual if x[1]["net"] > 0]
                if objective == "payoff" and positive:
                    good = [x for x in positive if _payoff(x[1]) >= min_payoff]
                    picked = sorted(good or positive, key=lambda x: (-x[1]["net"], x[2]))[0]
                elif objective == "winrate" and positive:
                    picked = sorted(positive, key=lambda x: (-win_rate(x[1]), -x[1]["net"], x[2]))[0]
                else:  # objective=net 或無任一正期望值 → 取總淨損益最大（保本）
                    picked = sorted(qual, key=lambda x: (-x[1]["net"], x[2]))[0]
            else:  # 無組合達標成交率 → 逼近目標
                picked = sorted(pool, key=lambda x: (-fill_rate(x[1]), -x[1]["net"], x[2]))[0]
            return picked[0], picked[1]

        best_sh, best_st = _select(scored)
        margin = max(100, abs(cur["net"]) * pcfg["improve_margin_pct"] / 100)
        mode_jump = best_sh["mode"] != current["mode"]
        # 新生模型（現行=ZERO、從未調整過）首次達標可自由跳躍：限步保護的是
        # 「既有參數」不被單日噪音拉走，剛誕生的模型沒有既有可保護——
        # 否則 walk-forward 重建初期要爬行數十日才到位（出場引擎遲遲未開）。
        newborn = current == ZERO
        if step_lim and mode_jump and not newborn:
            mult = pcfg.get("mode_switch_margin_mult", 2)
            if not (qualified(best_st, best_sh["mode"]) and best_st["net"] >= cur["net"] + mult * margin):
                # 跨模式證據不足 → 退回同模式最佳逐步逼近
                best_sh, best_st = _select([x for x in scored if x[0]["mode"] == current["mode"]])
                mode_jump = False

        if best_sh != current:
            cur_q = qualified(cur, current["mode"])
            best_q = qualified(best_st, best_sh["mode"])
            if objective == "winrate":
                cur_ok = cur_q and cur["net"] > 0
                best_ok = best_q and best_st["net"] > 0
                switch = (best_ok and not cur_ok) or \
                         (best_ok and cur_ok and win_rate(best_st) >= win_rate(cur) + 0.02) or \
                         (not best_q and not cur_q and fill_rate(best_st) >= fill_rate(cur) + 0.02)
            else:
                switch = (best_q and not cur_q) or \
                         (best_q and cur_q and best_st["net"] >= cur["net"] + margin) or \
                         (not best_q and not cur_q and fill_rate(best_st) >= fill_rate(cur) + 0.02)
                if objective == "payoff" and not switch and best_q and cur_q:
                    # 賺賠比從不及格→及格且不明顯犧牲期望值 → 換（大賺小賠結構優先）
                    switch = (_payoff(best_st) >= min_payoff > _payoff(cur)
                              and best_st["net"] > 0 and best_st["net"] >= cur["net"] - margin)
            if switch:
                adopt_sh, adopt_st = best_sh, best_st
                if step_lim and not mode_jump and not newborn:
                    cand = _step_toward(current, best_sh, grids, step_lim)
                    cand["mode"] = current["mode"]
                    if cand == current:
                        adopt_sh = None   # 網格對齊後無步可走：本日不動
                    elif cand != best_sh:
                        adopt_sh, adopt_st = cand, _replay(records, cand, fees_cfg, lots, strict, side, gap_void)
                if adopt_sh is not None:
                    def _pf(st):
                        p = _payoff(st)
                        return "∞" if p == float("inf") else f"{p:.2f}"
                    stepped = "（逐步趨近全域最佳）" if adopt_sh != best_sh else ""
                    jumped = "⚡切換進場模式：" if mode_jump else ""
                    log.append({"date": as_of_date,
                                "msg": f"{label}價格模型調整：{jumped}進場 {current['entry']:+.2f}→{adopt_sh['entry']:+.2f}R、"
                                       f"停利 {current['target']:+.2f}→{adopt_sh['target']:+.2f}R、"
                                       f"停損 {current['stop']:+.2f}→{adopt_sh['stop']:+.2f}R、{_fmt_mode(adopt_sh)}"
                                       f"（成交率 {fill_rate(cur)*100:.1f}%→{fill_rate(adopt_st)*100:.1f}%、"
                                       f"勝率 {win_rate(cur)*100:.1f}%→{win_rate(adopt_st)*100:.1f}%、"
                                       f"賺賠比 {_pf(cur)}→{_pf(adopt_st)}、"
                                       f"窗口淨損益 {cur['net']:,} → {adopt_st['net']:,} 元）{stepped}"})
                    current, cur = adopt_sh, adopt_st

    return current, cur, base


def run_price_opt(reviews, cfg, prev_doc, as_of_date):
    """多空各自優化。回傳 (shifts, price_doc)——shifts 頂層為做多偏移（相容舊介面），
    做空偏移在 shifts["short"]。歷史紀錄缺 cdp_base（舊格式）時自動略過該筆。"""
    pcfg = cfg.get("price_optimizer")
    if not pcfg:
        return dict(ZERO), None

    cur_long = dict(ZERO)
    if prev_doc and prev_doc.get("shifts"):
        cur_long.update({k: v for k, v in prev_doc["shifts"].items() if k in ZERO})
    cur_short = dict(ZERO)
    if prev_doc and prev_doc.get("short_shifts"):
        cur_short.update(prev_doc["short_shifts"])
    log = (prev_doc or {}).get("log", [])

    window = reviews[-pcfg["window_days"]:] if reviews else []
    recs = {"long": [], "short": []}
    for day in window:
        bars_by_code = load_intraday(day["date"])
        for r in day["picks"]:
            side = r.get("side", "long")
            base = r.get("cdp_base")
            if not base or (side == "short" and "ah" not in base):
                continue
            b = bars_by_code.get(r["code"])
            ohlc = {"o": r["day_open"], "h": r["day_high"], "l": r["day_low"], "c": r["day_close"]}
            if b and not bars_match_ohlc(b, ohlc):
                b = None  # 資料品質防線：與官方日K不符的 5分K 不得參與重放
            recs[side].append((r, b))
    fees_cfg, lots = cfg["fees"], cfg["simulation"]["lots_per_trade"]
    strict = cfg.get("simulation", {}).get("strict_fill", False)
    gap_void = cfg.get("simulation", {}).get("gap_void", False)
    target_fr = pcfg.get("target_fill_rate", 0)

    cur_long, stat_long, base_long = _optimize_side(
        recs["long"], pcfg, fees_cfg, lots, strict, cur_long, log, as_of_date, "long", gap_void)

    # 做空側：樣本不足時暫借做多偏移起步（掛單成交的物理對稱：偏移一律往市場方向移動）
    base_short_probe = _replay(recs["short"], ZERO, fees_cfg, lots, strict, "short", gap_void)
    if base_short_probe["fills"] < pcfg["min_trades"] and cur_short == ZERO and cur_long != ZERO:
        cur_short = dict(cur_long)
        log.append({"date": as_of_date,
                    "msg": f"空方價格模型樣本不足（窗口成交 {base_short_probe['fills']} 筆 < {pcfg['min_trades']}），"
                           f"暫借做多側偏移起步（進場 {cur_long['entry']:+.2f}R 等），樣本足夠後自動獨立優化"})
    cur_short, stat_short, base_short = _optimize_side(
        recs["short"], pcfg, fees_cfg, lots, strict, cur_short, log, as_of_date, "short", gap_void)

    doc = {
        "updated": as_of_date,
        "window_days": pcfg["window_days"],
        "shifts": cur_long,
        "short_shifts": cur_short,
        "stats": _stats_block(stat_long, base_long, target_fr),
        "short_stats": _stats_block(stat_short, base_short, target_fr),
        "log": log[-100:],
    }
    combined = dict(cur_long)
    combined["short"] = cur_short
    return combined, doc
