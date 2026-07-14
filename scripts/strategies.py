"""智能選股策略引擎。

每個策略對應研究報告中的一項當沖優勢因子，各自獨立觸發、
以權重加總成綜合分數；權重由 optimize.py 依復盤績效滾動調整（汰弱留強）。

候選策略池（candidate: True）：新策略先以權重 0 加入「虛擬追蹤」——
觸發紀錄照常寫進復盤、累積勝率/期望值統計，但不影響選股排序；
待樣本數足夠且期望值轉正，optimize.py 會自動賦予權重、正式納入計分。
之後要實驗新想法，只需在 STRATEGIES 加一條 candidate 定義即可。
"""
from indicators import breakeven_ticks, round_tick, tick_size

# 策略定義：id / 名稱 / 說明 / 判斷函式 / 初始權重
STRATEGIES = [
    {
        "id": "vol_surge",
        "name": "量能突增",
        "desc": "今日成交量 ≥ 前5日均量1.5倍且收紅，代表新資金進場、隔日延續機率高",
        "fn": lambda m: (m["vol_ratio"] or 0) >= 1.5 and m["close"] > m["open"],
    },
    {
        "id": "ma_bull",
        "name": "均線多頭",
        "desc": "5MA > 10MA > 20MA 且收盤站上5MA，順大勢做多的結構基礎",
        "fn": lambda m: m["ma5"] > m["ma10"] > m["ma20"] and m["close"] > m["ma5"],
    },
    {
        "id": "breakout20",
        "name": "突破20日高",
        "desc": "收盤突破前20日最高價，動能突破型態、易吸引隔日追價買盤",
        "fn": lambda m: m["close"] > m["high20"],
    },
    {
        "id": "daytrade_heat",
        "name": "當沖熱度",
        "desc": "當沖比率 ≥ 40%，或較前5日均值暴增15個百分點以上，極短線熱錢湧入",
        "fn": lambda m: (m["dt_ratio"] is not None and (
            m["dt_ratio"] >= 40 or
            (m["dt_ratio_ma5"] is not None and m["dt_ratio"] - m["dt_ratio_ma5"] >= 15))),
    },
    {
        "id": "tick_sweet",
        "name": "跳動甜蜜點",
        "desc": "股價剛跨越tick門檻區間（100~130 / 500~650 / 1000~1300元），1~2檔即可回本，資金效率最高",
        "fn": lambda m: (100 <= m["close"] <= 130) or (500 <= m["close"] <= 650) or (1000 <= m["close"] <= 1300),
    },
    {
        "id": "high_amp",
        "name": "高波動體質",
        "desc": "近30日平均振幅 ≥ 3.5%，扣除0.435%摩擦成本後仍有充足價差空間",
        "fn": lambda m: m["amp_avg"] >= 3.5,
    },
    {
        "id": "pullback_ma5",
        "name": "順勢拉回5MA",
        "desc": "多頭排列下回測5MA不破且收在5MA之上，「順大勢、逆小勢」的低風險買點",
        "fn": lambda m: (m["ma5"] > m["ma20"] and m["low"] <= m["ma5"] * 1.01 and m["close"] > m["ma5"]),
    },
    {
        "id": "turnover_hot",
        "name": "高週轉率",
        "desc": "單日週轉率 ≥ 5%，籌碼換手積極、短線資金活躍的明證",
        "fn": lambda m: (m["turnover"] or 0) >= 5,
    },
    # ---- 候選策略池：權重 0 虛擬追蹤，實證有效後由 optimizer 自動啟用 ----
    {
        "id": "strong_close",
        "name": "長紅強勢",
        "desc": "漲幅 ≥ 2%、紅K實體佔振幅 ≥ 60% 且收在當日區間頂部 20%，尾盤買方仍強、隔日慣性延續",
        "fn": lambda m: m["chg_pct"] >= 2 and m["body_ratio"] >= 0.6 and m["close_pos"] >= 0.8,
        "candidate": True,
    },
    {
        "id": "gap_hold",
        "name": "跳空缺口不補",
        "desc": "開盤跳空站上前日最高、全日最低未回補缺口且收紅，多方力道明確的強勢型態",
        "fn": lambda m: m["open"] > m["prev_high"] and m["low"] > m["prev_close"] and m["close"] > m["open"],
        "candidate": True,
    },
    {
        "id": "boll_break",
        "name": "布林上軌突破",
        "desc": "收盤突破布林通道上軌（20MA+2σ），統計意義上的波動擴張突破，常伴隨延續行情",
        "fn": lambda m: m["boll_up"] is not None and m["close"] > m["boll_up"],
        "candidate": True,
    },
    {
        "id": "up3_vol",
        "name": "三日量價齊揚",
        "desc": "連續 3 日收漲且今日量能不縮，趨勢連續性與資金持續進場的組合訊號",
        "fn": lambda m: m["up3"] and m["vol_nofade"],
        "candidate": True,
    },
    # ---- 籌碼面候選策略（用三大法人買賣超、融資融券資料）----
    {
        "id": "inst_buy",
        "name": "法人買超",
        "desc": "三大法人合計淨買超 ≥ 500 張且今日收紅，法人資金認同、隔日慣性偏多（籌碼面）",
        "fn": lambda m: (m.get("inst_net") or 0) >= 500 and m["close"] > m["open"],
        "candidate": True,
    },
    {
        "id": "short_squeeze",
        "name": "軋空題材",
        "desc": "券資比 ≥ 15% 且融券較前日增加、今日收紅，空方回補潛在燃料充足（籌碼面）",
        "fn": lambda m: (m.get("margin_short_ratio") or 0) >= 15 and (m.get("short_increase") or 0) > 0 and m["close"] > m["open"],
        "candidate": True,
    },
    {
        "id": "low_breakeven",
        "name": "低回本檔數",
        "desc": "含稅費回本僅需 ≤ 2 個跳動檔（跳動甜蜜點的直接量化），股價只要動 1-2 檔即覆蓋成本、資金效率最高、暴露風險時間最短",
        "fn": lambda m: (m.get("breakeven") or 99) <= 2,
        "candidate": True,
    },
    # ========== 做空側策略（side=short）：空方環境（大盤寬度弱）時啟用 ==========
    # 與做多鏡像：找「弱勢、破位、出貨」的標的隔日放空（現股當沖先賣後買 / 資券當沖）
    {
        "id": "s_vol_dump",
        "name": "爆量收黑",
        "desc": "今日量 ≥ 前5日均量1.5倍且收黑，量大不漲＝出貨，隔日續弱機率高",
        "fn": lambda m: (m["vol_ratio"] or 0) >= 1.5 and m["close"] < m["open"],
        "side": "short",
    },
    {
        "id": "s_ma_bear",
        "name": "均線空頭",
        "desc": "5MA < 10MA < 20MA 且收盤破5MA，順大勢做空的結構基礎",
        "fn": lambda m: m["ma5"] < m["ma10"] < m["ma20"] and m["close"] < m["ma5"],
        "side": "short",
    },
    {
        "id": "s_breakdown20",
        "name": "跌破20日低",
        "desc": "收盤跌破前20日最低價，動能破底型態、易引發停損追殺",
        "fn": lambda m: m["close"] < m["low20"],
        "side": "short",
    },
    {
        "id": "s_high_amp",
        "name": "高波動體質",
        "desc": "近30日平均振幅 ≥ 3.5%，做空同樣需要足夠價差空間",
        "fn": lambda m: m["amp_avg"] >= 3.5,
        "side": "short",
    },
    {
        "id": "s_weak_close",
        "name": "長黑弱勢",
        "desc": "跌幅 ≥ 2%、黑K實體佔振幅 ≥ 60% 且收在當日區間底部 20%，尾盤賣壓沉重、隔日慣性偏空",
        "fn": lambda m: m["chg_pct"] <= -2 and m["body_ratio"] <= -0.6 and m["close_pos"] <= 0.2,
        "side": "short",
    },
    {
        "id": "s_gap_down",
        "name": "跳空破低不補",
        "desc": "開盤跳空跌破前日最低、全日最高未補缺口且收黑，空方力道明確",
        "fn": lambda m: m["open"] < m["prev_low"] and m["high"] < m["prev_close"] and m["close"] < m["open"],
        "side": "short",
        "candidate": True,
    },
    {
        "id": "s_boll_break",
        "name": "布林下軌破",
        "desc": "收盤跌破布林通道下軌（20MA−2σ），統計意義上的波動擴張破位",
        "fn": lambda m: m.get("boll_dn") is not None and m["close"] < m["boll_dn"],
        "side": "short",
        "candidate": True,
    },
    {
        "id": "s_inst_sell",
        "name": "法人賣超",
        "desc": "三大法人合計淨賣超 ≥ 500 張且今日收黑，法人調節、隔日慣性偏空（籌碼面）",
        "fn": lambda m: (m.get("inst_net") or 0) <= -500 and m["close"] < m["open"],
        "side": "short",
        "candidate": True,
    },
]

for _s in STRATEGIES:
    _s.setdefault("side", "long")   # 未標註者預設為做多策略

STRAT_BY_ID = {s["id"]: s for s in STRATEGIES}
DEFAULT_WEIGHT = 1.0


def passes_base_filters(m, cfg, side="long"):
    """基礎濾網：流動性、價格帶、波動下限、排除處置/注意/非當沖標的。
    動能下限依方向：做多要求漲幅 ≥ min_chg_pct，做空要求跌幅 ≥ min_chg_pct（收黑夠深）。"""
    f = cfg["base_filters"]
    if not (f["price_min"] <= m["close"] <= f["price_max"]):
        return False
    # 動能下限：70 日實證顯示訊號日動能不足（漲幅<5%）的建議單平均為負期望值
    # （美股 stocks-in-play 理論同理：當沖標的需有事件級動能）。做空鏡像＝需夠深的跌幅。
    mom = f.get("min_chg_pct", -100)
    if side == "short":
        if -m["chg_pct"] < mom:
            return False
    elif m["chg_pct"] < mom:
        return False
    if m["val_ma5"] < f["min_value_5d_avg"]:
        return False
    if m["vol_ma5_lots"] < f["min_volume_5d_avg_lots"]:
        return False
    if m["amp_avg"] < f["min_amp20_pct"]:
        return False
    if f.get("exclude_punish", True) and m["flags"]["punish"]:
        return False
    if f.get("exclude_notice", True) and m["flags"]["notice"]:
        return False
    if f.get("require_daytrade_eligible", True) and m["flags"]["dt_ok"] is False:
        return False
    return True


def default_weight(s):
    """候選策略未經實證前預設權重 0（只追蹤不計分）。"""
    return 0.0 if s.get("candidate") else DEFAULT_WEIGHT


def evaluate(m, weights, side="long"):
    """回傳 (score, [觸發的策略id])，只評估指定方向（long/short）的策略。
    所有觸發（含權重0的候選/停用策略）都記錄供復盤追蹤，但只有權重 > 0 的策略貢獻分數。"""
    hits, score = [], 0.0
    for s in STRATEGIES:
        if s.get("side", "long") != side:
            continue
        try:
            if s["fn"](m):
                hits.append(s["id"])
                score += weights.get(s["id"], default_weight(s))
        except (TypeError, KeyError):
            continue
    return round(score, 2), hits


def shifted_prices(c, day_range, shifts):
    """CDP 基準價 + 價格模型偏移（單位：當日振幅 R）。
    偏移由 price_opt.py 依復盤績效滾動迭代；0 偏移＝原始 CDP 價位。"""
    s = shifts or {}
    entry = round_tick(c["nl"] + s.get("entry", 0) * day_range, "down")
    target = round_tick(c["nh"] + s.get("target", 0) * day_range, "up")
    stop = round_tick(c["al"] + s.get("stop", 0) * day_range, "down")
    stop = min(stop, round_tick(entry - tick_size(entry), "down"))   # 停損必須低於進場
    target = max(target, round_tick(entry + tick_size(entry), "up"))  # 停利必須高於進場
    return entry, target, stop


def short_prices(c, day_range):
    """做空建議價（CDP 對稱）：放空掛 NH（逆勢賣出區）、回補停利 NL、停損 AH（順勢突破＝看錯）。
    做空 v1 用原始 CDP 價（價格模型目前只優化做多側，避免把多方最佳偏移錯套到空方）。
    保證 停損 > 進場 > 停利。"""
    entry = round_tick(c["nh"], "up")
    target = round_tick(c["nl"], "down")
    stop = round_tick(c["ah"], "up")
    stop = max(stop, round_tick(entry + tick_size(entry), "up"))     # 停損必須高於進場
    target = min(target, round_tick(entry - tick_size(entry), "down"))  # 停利必須低於進場
    return entry, target, stop


def make_pick(m, score, hits, discount, price_shifts=None, side="long"):
    """由 CDP＋價格模型偏移產生隔日建議買賣價。
    做多：NL 掛買、NH 停利、AL 停損（＋價格模型偏移）；做空：NH 放空、NL 回補、AH 停損。
    出場引擎（移動停利/時間停損）兩側共用（R 為單位、方向無關）。"""
    c = m["cdp"]
    day_range = m["high"] - m["low"]
    if side == "short":
        entry, target, stop = short_prices(c, day_range)
    else:
        entry, target, stop = shifted_prices(c, day_range, price_shifts)
    trail_mult = (price_shifts or {}).get("trail") or 0
    return {
        "code": m["code"], "name": m["name"], "market": m["market"], "side": side,
        "close": m["close"], "chg_pct": m["chg_pct"],
        "score": score, "strategies": hits,
        "entry": entry, "target": target, "stop": stop, "ah": c["ah"],
        "trail_dist": round(trail_mult * day_range, 2) if trail_mult else None,
        "tstop_bar": (price_shifts or {}).get("tstop"),
        "cdp_base": {"nl": c["nl"], "nh": c["nh"], "al": c["al"], "ah": c["ah"], "r": round(day_range, 2)},
        "breakeven_ticks": breakeven_ticks(entry, discount),
        "amp_avg": m["amp_avg"], "vol_lots": m["vol_lots"],
        "dt_ratio": m["dt_ratio"],
        "spark": m.get("closes20"),   # 近20日收盤走勢（卡片 sparkline）
    }


def screen(market, cfg, weights, price_shifts=None, side="long"):
    """全市場掃描 → 依綜合分數排序的推薦清單（指定方向 long/short）。
    門檻只計「有權重的策略」命中數，候選/停用策略純追蹤、不影響入選。

    遞補機制：弱勢窗口下活躍策略減少、完整門檻可能選不滿 max_picks——
    此時從「通過基礎濾網且有觸發訊號（含候選/停用追蹤）」者依
    (活躍命中數, 綜合分數, 總命中數, 振幅) 遞補湊滿，並標記 fallback=True
    讓前端明示信心等級較低。遞補單照常復盤，維持樣本累積讓停用策略有機會翻身。"""
    picks, bench = [], []
    discount = cfg["fees"]["default_discount"]
    min_trig = cfg.get("min_strategies_triggered", 2)
    max_picks = cfg.get("max_picks", 8)
    for m in market.values():
        if not passes_base_filters(m, cfg, side):
            continue
        score, hits = evaluate(m, weights, side)
        if not hits:
            continue
        active_hits = [h for h in hits if weights.get(h, 0) > 0]
        p = make_pick(m, score, hits, discount, price_shifts, side)
        if len(active_hits) >= min_trig and score > 0:
            picks.append(p)
        else:
            bench.append((len(active_hits), score, len(hits), p))
    picks.sort(key=lambda p: (-p["score"], -p["amp_avg"]))
    picks = picks[:max_picks]
    if len(picks) < max_picks and bench:
        bench.sort(key=lambda x: (-x[0], -x[1], -x[2], -x[3]["amp_avg"]))
        for _, _, _, p in bench[: max_picks - len(picks)]:
            p["fallback"] = True
            picks.append(p)
    return picks
