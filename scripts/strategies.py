"""智能選股策略引擎。

每個策略對應研究報告中的一項當沖優勢因子，各自獨立觸發、
以權重加總成綜合分數；權重由 optimize.py 依復盤績效滾動調整（汰弱留強）。
"""
from indicators import breakeven_ticks

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
]

STRAT_BY_ID = {s["id"]: s for s in STRATEGIES}
DEFAULT_WEIGHT = 1.0


def passes_base_filters(m, cfg):
    """基礎濾網：流動性、價格帶、波動下限、排除處置/注意/非當沖標的。"""
    f = cfg["base_filters"]
    if not (f["price_min"] <= m["close"] <= f["price_max"]):
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


def evaluate(m, weights):
    """回傳 (score, [觸發的策略id])，只計入 enabled（權重>0）的策略。"""
    hits, score = [], 0.0
    for s in STRATEGIES:
        try:
            if s["fn"](m):
                hits.append(s["id"])
                score += weights.get(s["id"], DEFAULT_WEIGHT)
        except (TypeError, KeyError):
            continue
    return round(score, 2), hits


def make_pick(m, score, hits, discount):
    """由 CDP 產生隔日建議買賣價（逆勢：NL 掛買、NH 停利、AL 停損）。"""
    c = m["cdp"]
    return {
        "code": m["code"], "name": m["name"], "market": m["market"],
        "close": m["close"], "chg_pct": m["chg_pct"],
        "score": score, "strategies": hits,
        "entry": c["nl"], "target": c["nh"], "stop": c["al"], "ah": c["ah"],
        "breakeven_ticks": breakeven_ticks(c["nl"], discount),
        "amp_avg": m["amp_avg"], "vol_lots": m["vol_lots"],
        "dt_ratio": m["dt_ratio"],
    }


def screen(market, cfg, weights):
    """全市場掃描 → 依綜合分數排序的推薦清單。"""
    picks = []
    discount = cfg["fees"]["default_discount"]
    for m in market.values():
        if not passes_base_filters(m, cfg):
            continue
        score, hits = evaluate(m, weights)
        if len(hits) < cfg.get("min_strategies_triggered", 2) or score <= 0:
            continue
        picks.append(make_pick(m, score, hits, discount))
    picks.sort(key=lambda p: (-p["score"], -p["amp_avg"]))
    return picks[: cfg.get("max_picks", 8)]
