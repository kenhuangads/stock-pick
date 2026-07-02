"""每日復盤：用實際 OHLC 檢驗前一交易日收盤後產生的建議單。

模擬假設（保守原則，全部寫進 README）：
- 限價買單掛 entry（CDP NL）：當日最低價 ≤ entry 才視為成交；
  若開盤價低於 entry，以開盤價成交（限價單的實際行為）。
- 出場優先序（悲觀排序）：先檢查停損（最低 ≤ stop → 以 stop 出場），
  再檢查停利（最高 ≥ target → 以 target 出場），皆未觸及 → 收盤價沖銷。
  OHLC 無法得知日內先後順序，一律採最壞情境，避免高估策略績效。
- 現股當沖稅率 0.15%（賣出時課徵）、手續費 0.1425% × 折扣，買賣各一次，
  單邊手續費低於最低手續費時以最低手續費計。
"""


def trade_fees(buy_price, sell_price, lots, fees_cfg, discount=None):
    shares = lots * 1000
    disc = discount if discount is not None else fees_cfg["default_discount"]
    fee_buy = max(int(buy_price * shares * fees_cfg["fee_rate"] * disc), fees_cfg["min_fee"])
    fee_sell = max(int(sell_price * shares * fees_cfg["fee_rate"] * disc), fees_cfg["min_fee"])
    tax = int(sell_price * shares * fees_cfg["daytrade_tax"])
    return fee_buy, fee_sell, tax


def simulate_pick(pick, ohlc, fees_cfg, lots=1):
    """回傳含成交/出場/損益的復盤紀錄。ohlc: 交易日的 {o,h,l,c}。"""
    r = {
        "code": pick["code"], "name": pick["name"], "market": pick.get("market"),
        "score": pick["score"], "strategies": pick["strategies"],
        "entry": pick["entry"], "target": pick["target"], "stop": pick["stop"],
        "cdp_base": pick.get("cdp_base"),  # 原始 CDP 價位與當日振幅，供價格模型重放迭代

        "day_open": ohlc["o"], "day_high": ohlc["h"], "day_low": ohlc["l"], "day_close": ohlc["c"],
        "filled": False, "fill_price": None, "exit_price": None, "exit_reason": None,
        "gross": 0, "fees": 0, "net": 0, "ret_pct": None,
    }
    if ohlc["l"] > pick["entry"]:
        r["exit_reason"] = "nofill"   # 全日未觸及掛買價 → 沒有進場
        return r
    fill = min(pick["entry"], ohlc["o"])
    r["filled"], r["fill_price"] = True, fill

    if ohlc["l"] <= pick["stop"] and fill > pick["stop"]:
        exit_price, reason = pick["stop"], "stop"
    elif ohlc["h"] >= pick["target"]:
        exit_price, reason = pick["target"], "target"
    else:
        exit_price, reason = ohlc["c"], "close"
    r["exit_price"], r["exit_reason"] = exit_price, reason

    shares = lots * 1000
    fee_b, fee_s, tax = trade_fees(fill, exit_price, lots, fees_cfg)
    gross = int((exit_price - fill) * shares)
    r["gross"] = gross
    r["fees"] = fee_b + fee_s + tax
    r["net"] = gross - r["fees"]
    r["ret_pct"] = round((exit_price - fill) / fill * 100, 2)
    return r


def run_review(picks_doc, trade_snapshot, cfg):
    """picks_doc: 前一交易日產生的 picks.json 內容；trade_snapshot: 建議執行日的快照。
    回傳一筆 reviews.json 的日紀錄。"""
    lots = cfg["simulation"]["lots_per_trade"]
    results = []
    for pick in picks_doc.get("picks", []):
        k = trade_snapshot["stocks"].get(pick["code"])
        if not k:
            continue  # 停牌等情況：無資料不計
        results.append(simulate_pick(pick, k, cfg["fees"], lots))
    filled = [r for r in results if r["filled"]]
    wins = [r for r in filled if r["net"] > 0]
    return {
        "date": trade_snapshot["date"],
        "generated_on": picks_doc.get("generated_on"),
        "picks": results,
        "summary": {
            "n_picks": len(results),
            "n_filled": len(filled),
            "n_wins": len(wins),
            "win_rate": round(len(wins) / len(filled) * 100, 1) if filled else None,
            "gross": sum(r["gross"] for r in filled),
            "fees": sum(r["fees"] for r in filled),
            "net": sum(r["net"] for r in filled),
        },
    }
