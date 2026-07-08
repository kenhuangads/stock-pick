"""每日復盤：用實際行情檢驗前一交易日收盤後產生的建議單。

模擬有兩種模式（同一套進出場規則，粒度不同）：

【intraday｜5 分K 順序模擬（優先）】
- 逐根走 5 分K：第一根「最低 ≤ 掛買價」的 bar 視為成交
  （該根開盤已低於掛價 → 以該根開盤價成交，否則以掛價成交）。
- 成交那一根：**不認停利**（同根內的高點可能發生在成交之前，吃不到），
  但認停損（既然殺到掛價，續殺到停損的機率高，保守處理）。
- 成交之後的每一根：先檢查停損（低 ≤ stop）、再檢查停利（高 ≥ target），
  皆未觸及走到收盤 → 以日K收盤價沖銷。
- 這修正了日K模擬的「順序偏誤」——先衝高觸停利、之後才回落到掛買價的單，
  日K會誤記成吃得到的停利，5 分K 能還原真實順序。

【出場引擎擴充（僅 intraday 模式，由價格模型每日 A/B 實證決定開關）】
- 地板式移動停利 trail_dist：觸及停利價後不立刻出場，改追蹤高點回落
  max(原停利, 峰值 − trail_dist) 出場——原停利價為地板、讓大賺的尾巴多跑；
  惟移動停利本質是停損單，5 分K 跳空時以該根開盤價成交、可能略低於地板。
- 時間停損 tstop_bar：至該根（預設 36 ≈ 12:00）尚未觸及停利 → 以該根收盤出場。
  依據實測：拖到收盤沖銷的單午後平均為負漂移（報告的 12:00 法則）。

【跳空穿越停損修正】開盤（或成交當根）價已低於停損 → 以實際可成交價出場，
不得以高於市場的停損價成交（修正舊版會憑空獲利的瑕疵）。

【daily｜日K保守模擬（無 5 分K資料時 fallback）】
- 最低 ≤ 掛價視為成交（開盤更低以開盤價成交）；開盤穿越停損視同開盤即停損。
- 出場悲觀排序：先停損、再停利、皆未觸及以收盤沖銷（無順序資訊，不適用移動停利/時間停損）。

費用：現股當沖稅 0.15%（賣出課徵）、手續費 0.1425% × 折扣，買賣各一次，
單邊低於最低手續費以最低計。
"""


def trade_fees(buy_price, sell_price, lots, fees_cfg, discount=None):
    shares = lots * 1000
    disc = discount if discount is not None else fees_cfg["default_discount"]
    fee_buy = max(int(buy_price * shares * fees_cfg["fee_rate"] * disc), fees_cfg["min_fee"])
    fee_sell = max(int(sell_price * shares * fees_cfg["fee_rate"] * disc), fees_cfg["min_fee"])
    tax = int(sell_price * shares * fees_cfg["daytrade_tax"])
    return fee_buy, fee_sell, tax


def bars_match_ohlc(bars, ohlc, tol=0.005, min_bars=40):
    """5分K 資料品質防線：與官方日K的開/高/低價差超過容忍值、或根數不足
    （缺漏/錯位/分盤異常）→ 判定不可信，模擬退回日K保守規則。
    （曾發現 Yahoo 個別日期整批錯位、高低價差達 20%，不擋會污染復盤。）"""
    if not bars or len(bars) < min_bars:
        return False
    agg = (bars[0][0], max(b[1] for b in bars), min(b[2] for b in bars))
    for a, b in zip(agg, (ohlc["o"], ohlc["h"], ohlc["l"])):
        if not b or abs(a - b) / b > tol:
            return False
    return True


def simulate_trade(entry, target, stop, ohlc, bars=None, trail_dist=None, tstop_bar=None):
    """共用模擬核心（review 與 price_opt 都走這裡，確保口徑一致）。
    回傳 (filled, fill_price, exit_price, exit_reason, sim_mode)。
    ohlc: {o,h,l,c} 日K；bars: [[o,h,l,c],...] 5分K（可 None）。
    trail_dist: 地板式移動停利距離（絕對價差，None/0=關）；tstop_bar: 時間停損 bar index（None=關）。
    出場理由：target 停利｜trail 移動停利｜stop 停損｜timeout 時間停損｜close 收盤沖銷｜nofill 未成交。"""
    if bars:
        fill = None
        armed, peak = False, None   # 觸及停利價後啟動移動停利追蹤
        for i, (bo, bh, bl, bc) in enumerate(bars):
            if fill is None:
                if bl <= entry:
                    fill = min(entry, bo)
                    if fill <= stop:
                        # 跳空直接穿越停損：停損單即刻觸發，以成交價出場（僅損費用）
                        return True, fill, fill, "stop", "intraday"
                    if bl <= stop:
                        return True, fill, stop, "stop", "intraday"
                continue
            if not armed:
                if bl <= stop:
                    return True, fill, min(stop, bo), "stop", "intraday"
                if bh >= target:
                    if trail_dist:
                        armed, peak = True, bh   # 啟動移動停利，讓獲利多跑
                        level = max(target, peak - trail_dist)
                        if bc < level:
                            # 觸頂後同一根即回落穿越追蹤價（收盤在其下，可確認先後）→ 以追蹤價出場
                            return True, fill, level, "trail", "intraday"
                    else:
                        return True, fill, target, "target", "intraday"
                elif tstop_bar is not None and i >= tstop_bar:
                    return True, fill, bc, "timeout", "intraday"
            else:
                level = max(target, peak - trail_dist)   # 原停利價為地板
                if bo <= level:
                    return True, fill, bo, "trail", "intraday"   # 跳空：以開盤價成交（可能略低於地板）
                if bl <= level:
                    return True, fill, level, "trail", "intraday"
                peak = max(peak, bh)
        if fill is None:
            return False, None, None, "nofill", "intraday"
        return True, fill, ohlc["c"], "close", "intraday"

    # fallback：日K保守規則（順序未知，悲觀假設；無法模擬移動停利/時間停損）
    if ohlc["l"] > entry:
        return False, None, None, "nofill", "daily"
    fill = min(entry, ohlc["o"])
    if fill <= stop:
        return True, fill, fill, "stop", "daily"   # 開盤跳空穿越停損：視同開盤即停損
    if ohlc["l"] <= stop:
        return True, fill, stop, "stop", "daily"
    if ohlc["h"] >= target:
        return True, fill, target, "target", "daily"
    return True, fill, ohlc["c"], "close", "daily"


def simulate_pick(pick, ohlc, fees_cfg, lots=1, bars=None):
    """回傳含成交/出場/損益的復盤紀錄。ohlc: 交易日的 {o,h,l,c}；bars: 當日 5 分K。"""
    r = {
        "code": pick["code"], "name": pick["name"], "market": pick.get("market"),
        "score": pick["score"], "strategies": pick["strategies"],
        "entry": pick["entry"], "target": pick["target"], "stop": pick["stop"],
        "trail_dist": pick.get("trail_dist"), "tstop_bar": pick.get("tstop_bar"),
        "cdp_base": pick.get("cdp_base"),  # 原始 CDP 價位與當日振幅，供價格模型重放迭代
        "day_open": ohlc["o"], "day_high": ohlc["h"], "day_low": ohlc["l"], "day_close": ohlc["c"],
        "filled": False, "fill_price": None, "exit_price": None, "exit_reason": None,
        "sim_mode": None,
        "gross": 0, "fees": 0, "net": 0, "ret_pct": None,
    }
    if bars and not bars_match_ohlc(bars, ohlc):
        bars = None  # 5分K與官方日K不符（錯位/缺漏），退回日K保守模擬
    filled, fill, exit_price, reason, mode = simulate_trade(
        pick["entry"], pick["target"], pick["stop"], ohlc, bars,
        pick.get("trail_dist"), pick.get("tstop_bar"))
    r["sim_mode"], r["exit_reason"] = mode, reason
    if not filled:
        return r
    r["filled"], r["fill_price"], r["exit_price"] = True, fill, exit_price

    shares = lots * 1000
    fee_b, fee_s, tax = trade_fees(fill, exit_price, lots, fees_cfg)
    gross = int((exit_price - fill) * shares)
    r["gross"] = gross
    r["fees"] = fee_b + fee_s + tax
    r["net"] = gross - r["fees"]
    r["ret_pct"] = round((exit_price - fill) / fill * 100, 2)
    return r


def run_review(picks_doc, trade_snapshot, cfg, intraday=None):
    """picks_doc: 前一交易日產生的 picks.json 內容；trade_snapshot: 建議執行日的快照；
    intraday: {code: bars} 當日 5 分K（可 None）。回傳一筆 reviews.json 的日紀錄。"""
    lots = cfg["simulation"]["lots_per_trade"]
    intraday = intraday or {}
    results = []
    for pick in picks_doc.get("picks", []):
        k = trade_snapshot["stocks"].get(pick["code"])
        if not k:
            continue  # 停牌等情況：無資料不計
        results.append(simulate_pick(pick, k, cfg["fees"], lots, intraday.get(pick["code"])))
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
            "n_intraday": sum(1 for r in results if r["sim_mode"] == "intraday"),
            "win_rate": round(len(wins) / len(filled) * 100, 1) if filled else None,
            "gross": sum(r["gross"] for r in filled),
            "fees": sum(r["fees"] for r in filled),
            "net": sum(r["net"] for r in filled),
        },
    }
