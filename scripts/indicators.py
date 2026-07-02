"""指標計算：tick 跳動規則、CDP 逆勢系統、均線、波動度、個股綜合指標。"""

TICK_LADDER = [(10, 0.01), (50, 0.05), (100, 0.10), (500, 0.50), (1000, 1.00), (float("inf"), 5.00)]


def tick_size(price: float) -> float:
    for bound, tick in TICK_LADDER:
        if price < bound:
            return tick
    return 5.0


def round_tick(price: float, direction: str = "nearest") -> float:
    """將價格對齊到合法跳動檔位。direction: down(買)/up(賣)/nearest"""
    t = tick_size(price)
    n = price / t
    if direction == "down":
        n = int(n + 1e-9)
    elif direction == "up":
        n = -int(-(n - 1e-9) // 1)
    else:
        n = round(n)
    return round(n * t, 2)


def cdp(h: float, l: float, c: float) -> dict:
    """CDP 逆勢操作系統：由前一日 H/L/C 推算今日五個關鍵價位。"""
    val = (h + l + 2 * c) / 4
    return {
        "cdp": round_tick(val),
        "ah": round_tick(val + (h - l), "up"),      # 順勢突破區
        "nh": round_tick(2 * val - l, "up"),        # 逆勢賣出區
        "nl": round_tick(2 * val - h, "down"),      # 逆勢買進區
        "al": round_tick(val - (h - l), "down"),    # 順勢跌破區（停損參考）
    }


def sma(values, n):
    if len(values) < n:
        return None
    window = values[-n:]
    return sum(window) / n


def compute_stock_metrics(series):
    """series: 依日期排序的該股 K 線 list，每筆 {o,h,l,c,v,a,dt?,sh?}。
    回傳市場快照用的綜合指標 dict（資料不足回 None）。"""
    if len(series) < 21:
        return None
    today, prev = series[-1], series[-2]
    closes = [k["c"] for k in series]
    vols = [k["v"] for k in series]
    vals = [k["a"] for k in series]

    ma5, ma10, ma20 = sma(closes, 5), sma(closes, 10), sma(closes, 20)
    vol_ma5 = sma(vols[:-1], 5)            # 不含今日的前 5 日均量（判斷今日是否爆量）
    vol_ma3_incl = sma(vols, 3)            # 含今日的近 3 日均量（自訂條件用）
    vol_ma5_incl = sma(vols, 5)
    val_ma5 = sma(vals, 5)

    amp_win = series[-30:] if len(series) >= 30 else series[-20:]
    amps = [(k["h"] - k["l"]) / k["o"] * 100 for k in amp_win if k["o"]]
    amp_avg = sum(amps) / len(amps) if amps else 0.0

    high20 = max(k["h"] for k in series[-21:-1])   # 不含今日的前 20 日最高

    dt_ratio = None
    dt_hist = [(k.get("dt") or 0) / k["v"] * 100 for k in series[-6:] if k.get("dt") and k["v"]]
    if today.get("dt") and today["v"]:
        dt_ratio = today["dt"] / today["v"] * 100
    dt_ratio_ma5 = sum(dt_hist[:-1]) / len(dt_hist[:-1]) if len(dt_hist) > 1 else None

    turnover = None
    if today.get("sh"):
        turnover = today["v"] / today["sh"] * 100

    return {
        "close": today["c"], "open": today["o"], "high": today["h"], "low": today["l"],
        "chg_pct": round((today["c"] - prev["c"]) / prev["c"] * 100, 2) if prev["c"] else 0,
        "vol_lots": today["v"] // 1000,
        "val": today["a"],
        "val_ma5": int(val_ma5 or 0),
        "vol_ma5_lots": int((vol_ma5 or 0) // 1000),
        "vol_ma3_incl_lots": int((vol_ma3_incl or 0) // 1000),
        "vol_ma5_incl_lots": int((vol_ma5_incl or 0) // 1000),
        "vol_ratio": round(today["v"] / vol_ma5, 2) if vol_ma5 else None,
        "amp_today": round((today["h"] - today["l"]) / today["o"] * 100, 2) if today["o"] else 0,
        "amp_avg": round(amp_avg, 2),
        "ma5": round(ma5, 2), "ma10": round(ma10, 2), "ma20": round(ma20, 2),
        "high20": high20,
        "dt_ratio": round(dt_ratio, 1) if dt_ratio is not None else None,
        "dt_ratio_ma5": round(dt_ratio_ma5, 1) if dt_ratio_ma5 is not None else None,
        "turnover": round(turnover, 2) if turnover is not None else None,
        "cdp": cdp(today["h"], today["l"], today["c"]),
        "days": len(series),
    }


def build_market(snapshots):
    """由歷史快照組出最新交易日的全市場指標表。
    回傳 (latest_date, {code: {name, market, metrics...}})"""
    latest = snapshots[-1]
    per_stock = {}
    for snap in snapshots:
        for code, k in snap["stocks"].items():
            per_stock.setdefault(code, []).append(k)
    market = {}
    for code, row in latest["stocks"].items():
        m = compute_stock_metrics(per_stock[code])
        if m is None:
            continue
        m["code"], m["name"], m["market"] = code, row["n"], row["m"]
        m["flags"] = {
            "punish": code in set(latest.get("punish", [])),
            "notice": code in set(latest.get("notice", [])),
            "dt_ok": code in set(latest.get("dt_ok", [])) if latest.get("dt_ok") else None,
            "dt_sell_first_suspended": code in set(latest.get("dt_sus", [])),
        }
        market[code] = m
    return latest["date"], market


def breakeven_ticks(price: float, discount: float = 0.28) -> int:
    """現股當沖回本所需最少跳動檔數（手續費打折後）。"""
    cost_pct = 0.001425 * 2 * discount + 0.0015
    t = tick_size(price)
    need = price * cost_pct
    n = 1
    while n * t < need:
        n += 1
    return n
