"""每日更新協調器。

流程（GitHub Actions 平日 21:00 台北時間執行，或本機手動執行）：
  1. 抓最新收盤快照 → 存入 data/history/（已存在則略過）
  2. 復盤：把前一次產生的建議單，用「產生日之後第一個交易日」的實際 OHLC 模擬 → 累積到 reviews.json
  3. 迭代優化：滾動窗口重算各策略勝率/期望值 → 更新權重（汰弱留強）→ strategies.json
  4. 智能選股：用最新資料 + 最新權重掃描全市場 → picks.json（隔日建議單）
  5. 輸出 market.json 供前端自訂條件即時重新選股

用法：
  python scripts/daily_update.py            # 完整每日流程（抓網路資料）
  python scripts/daily_update.py --offline  # 不抓資料，用既有 history 重建輸出
  python scripts/daily_update.py --rebuild  # 由 history 全量 walk-forward 重建復盤/權重（回測種子）
"""
import json
import sys
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import twstock
from indicators import build_market, market_breadth, market_breadth_ma_series
from strategies import screen, evaluate, STRATEGIES, default_weight
from review import run_review
from optimize import run_optimize
from price_opt import run_price_opt
from intraday import ensure_intraday, load_intraday
from overnight import load_overnight, overnight_for, update_overnight

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
LATEST = DATA / "latest"
TAIPEI = timezone(timedelta(hours=8))


def load_json(p, default):
    p = Path(p)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return default


def save_json(p, obj):
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    Path(p).write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def load_config():
    return json.loads((ROOT / "config.json").read_text(encoding="utf-8"))


def weights_from_doc(doc):
    """由 strategies.json 還原權重；缺漏的策略（例如剛加入的候選）補預設值。"""
    weights = {s["id"]: default_weight(s) for s in STRATEGIES}
    for sid, st in (doc or {}).get("stats", {}).items():
        if sid in weights:
            weights[sid] = st.get("weight", weights[sid])
    return weights


def regime_state(snapshots, cfg, overnight_row=None):
    """大盤環境：市場寬度均值 ≥ 門檻為多方、< 門檻為空方。
    enabled 時據此決定「主方向」：多方環境以做多為主、空方環境以做空為主
    （walk-forward 實證：空方環境做多平均負期望值——主力不做多、改做空）。
    hysteresis > 0 時啟用遲滯帶抗抖動：翻多需 ≥ 門檻+帶寬、翻空需 < 門檻−帶寬，
    帶內維持前一狀態；以寬度序列無狀態重放推得當前狀態（walk-forward 安全）。
    book：當日主方向 long/short（逆勢配額另由 screen_book 處理）。

    overnight_row（清晨校正）：美股收盤的隔夜訊號 {sox,gspc,ixic,w}。台股寬度只看得到
    「昨天以前」，看不到隔夜的美股整場——2026-07-31 台股創最大漲點前夜費半 +8.19%，
    寬度卻判空方出了 8 檔空單（實測案例）。隔夜加權訊號與台股當日寬度相關 0.40：
    訊號突破門檻且與寬度判定相反時，以隔夜訊號覆蓋（flip）。"""
    rcfg = cfg.get("regime_filter") or {}
    ma = rcfg.get("breadth_ma", 5)
    thr = rcfg.get("min_breadth", 0.5)
    hys = rcfg.get("hysteresis", 0) or 0
    b, bma = market_breadth(snapshots, ma)
    if bma is None:
        bull = True
    elif hys > 0:
        # 只取「完整 ma 窗口」的均值序列重放（暖身期樣本少、噪音大，不參與翻轉判定）
        series = market_breadth_ma_series(snapshots, ma)[max(0, ma - 1):]
        if not series:
            bull = bma >= thr
        else:
            bull = True                 # 起始視為多方（與無資料預設一致）
            for v in series:
                if bull and v < thr - hys:
                    bull = False
                elif not bull and v >= thr + hys:
                    bull = True
    else:
        bull = bma >= thr

    # 隔夜訊號覆蓋：只在「與寬度判定相反」時翻轉（同向無需動作）
    on_cfg = cfg.get("overnight") or {}
    on_flip = None
    if on_cfg.get("enabled") and overnight_row and overnight_row.get("w") is not None:
        w = overnight_row["w"]
        fu, fd = on_cfg.get("flip_up"), on_cfg.get("flip_down")
        if fu is not None and w >= fu and not bull:
            bull, on_flip = True, "bull"
        elif fd is not None and w <= fd and bull:
            bull, on_flip = False, "bear"

    book = "long" if (bull or not rcfg.get("enabled")) else "short"
    return {"breadth": round(b, 3) if b is not None else None,
            "breadth_ma": round(bma, 3) if bma is not None else None,
            "bull": bull, "enabled": bool(rcfg.get("enabled")), "book": book,
            "overnight": overnight_row, "overnight_flip": on_flip}


def _main_side(review_entry):
    """一個復盤日的主方向＝非逆勢單的多數方向（無主向單回 None）。"""
    mains = [p for p in (review_entry or {}).get("picks", []) if not p.get("counter")]
    if not mains:
        return None
    n_long = sum(1 for p in mains if p.get("side", "long") == "long")
    return "long" if n_long * 2 >= len(mains) else "short"


def screen_book(market, cfg, weights, shifts, regime, prev_book=None):
    """組當日建議單：主方向（順大盤環境）為主，保留少量「逆勢配額」給另一方向——
    當沖多空皆有機會，環境判定也可能錯；逆勢單門檻更嚴（counter_min_hits）、
    無遞補、數量受 counter_quota 限制，並標記 counter=True 供前端明示與後續實證。
    總檔數維持 max_picks：主方向 = max_picks − 實際逆勢檔數。
    counter_sides（選配）：限制逆勢配額只開放給指定方向——94 日同路徑歸因：
    逆勢多（空頭環境買強勢）+18,354、逆勢空（多頭環境空弱勢）6 月起連三月失血
    （台股結構性偏多、軋空頻繁，多頭環境空弱勢＝接刀），故預設只留 ["long"]。
    翻向日護欄（flip_max_picks，prev_book=前一復盤日主方向）：主方向與前一日不同的
    「翻向日」跨 10 條 walk-forward 路徑有 9 條日均為負（−87～−1,779/日；寬度 5MA 在
    震盪市貼著 50% 來回＝方向可信度最低的日子），故翻向日主方向名單縮編、不遞補；
    不改方向判定本身（遲滯帶改方向曾複驗失敗）、逆勢配額照常（它是對沖）。"""
    rcfg = cfg.get("regime_filter") or {}
    main = regime["book"]
    counter = "short" if main == "long" else "long"
    max_picks = cfg.get("max_picks", 8)
    flip_cap = int(rcfg.get("flip_max_picks", 0) or 0)
    flip = bool(regime["enabled"] and flip_cap and prev_book and main != prev_book)
    regime["flip"] = flip   # 供前端明示「環境翻向日」與復盤歸因
    if flip:
        max_picks = min(max_picks, flip_cap)
    counter_picks = []
    quota = int(rcfg.get("counter_quota", 0) or 0)
    allowed = rcfg.get("counter_sides")   # None/缺省＝雙向皆可
    if allowed and counter not in allowed:
        quota = 0
    if regime["enabled"] and quota > 0:
        c2 = dict(cfg)
        c2["max_picks"] = quota
        c2["min_strategies_triggered"] = rcfg.get("counter_min_hits", 3)
        counter_picks = screen(market, c2, weights, shifts, side=counter, allow_fallback=False)
        for p in counter_picks:
            p["counter"] = True
    c1 = dict(cfg)
    c1["max_picks"] = max(0, max_picks - len(counter_picks))
    main_picks = screen(market, c1, weights, shifts, side=main, allow_fallback=not flip)
    return main_picks + counter_picks


def generate_outputs(snapshots, cfg, reviews, strat_doc, price_doc, overnight_row=None):
    """由（時間排序的）快照序列產生 market/picks/strategies/price_model 輸出。
    overnight_row：清晨校正跑（--dawn）時傳入剛收盤的美股隔夜訊號；晚間初版為 None。"""
    latest_date, market = build_market(snapshots)
    weights, strat_doc = run_optimize(reviews, cfg, strat_doc, latest_date)
    shifts, price_doc = run_price_opt(reviews, cfg, price_doc, latest_date)
    regime = regime_state(snapshots, cfg, overnight_row)
    picks = screen_book(market, cfg, weights, shifts, regime,
                        prev_book=_main_side(reviews[-1]) if reviews else None)
    for m in market.values():  # 供前端自訂選股使用的個股觸發標記（多空皆計）
        sl, hl = evaluate(m, weights, "long")
        ss, hs = evaluate(m, weights, "short")
        m["score"], m["strategies"] = round(sl + ss, 2), hl + hs
    picks_doc = {
        "generated_on": latest_date,
        "generated_at": datetime.now(TAIPEI).isoformat(timespec="seconds"),
        "weights_used": weights,
        "price_shifts": shifts,
        "regime": regime,
        "note": "建議單適用於 generated_on 之後的下一個交易日",
        "picks": picks,
    }
    for m in market.values():
        m.pop("closes20", None)   # sparkline 序列只留在 picks（8檔），全市場輸出剔除以控制檔案大小
    market_doc = {
        "date": latest_date,
        "updated_at": datetime.now(TAIPEI).isoformat(timespec="seconds"),
        "count": len(market),
        "breadth": regime,
        "strategies": [{"id": s["id"], "name": s["name"], "desc": s["desc"],
                        "candidate": s.get("candidate", False)} for s in STRATEGIES],
        "stocks": sorted(market.values(), key=lambda m: -m["val"]),
    }
    return market_doc, picks_doc, strat_doc, price_doc


def do_review_if_due(cfg, reviews, snapshots_by_date, allow_fetch=True):
    """把尚未復盤的建議單，對「產生日之後第一個交易日」執行模擬（5分K核實順序）。"""
    picks_doc = load_json(LATEST / "picks.json", None)
    if not picks_doc or not picks_doc.get("picks"):
        return reviews, False
    gen = picks_doc["generated_on"]
    reviewed_dates = {r["date"] for r in reviews}
    later = sorted(d for d in snapshots_by_date if d > gen)
    if not later:
        return reviews, False
    trade_date = later[0]
    if trade_date in reviewed_dates:
        return reviews, False
    bars = (ensure_intraday(trade_date, picks_doc["picks"]) if allow_fetch
            else load_intraday(trade_date))
    entry = run_review(picks_doc, snapshots_by_date[trade_date], cfg, bars)
    reviews.append(entry)
    reviews.sort(key=lambda r: r["date"])
    s = entry["summary"]
    print(f"[review] {gen} 的建議單以 {trade_date} 實際行情復盤："
          f"成交 {s['n_filled']} 筆、淨損益 {s['net']} 元（5分K核實 {s['n_intraday']}/{s['n_picks']}）")
    return reviews, True


def rebuild_walkforward(cfg, allow_fetch=True):
    """由 history 全量重建：每天只用「當天以前」的資料選股，再用隔天實際行情復盤。
    嚴格 walk-forward，策略權重與價格偏移沿路演化，和真實每日執行的結果一致。
    復盤優先用 5 分K核實順序（缺檔時線上補抓；Yahoo 約可回溯 60 天）。"""
    snaps = twstock.load_snapshots()
    min_days = cfg.get("min_history_days", 22)
    if len(snaps) <= min_days:
        print(f"[rebuild] 歷史僅 {len(snaps)} 天，不足 {min_days}+1 天，略過")
        return [], None, None
    reviews, strat_doc, price_doc = [], None, None
    # Yahoo 5 分K只回溯約 60 天：更早的日期抓了必空，直接跳過抓取（避免整輪失敗重試拖慢 rebuild）
    intraday_cutoff = (date.today() - timedelta(days=55)).isoformat()
    on_doc = load_overnight() if (cfg.get("overnight") or {}).get("enabled") else {}
    for i in range(min_days, len(snaps)):
        upto = snaps[: i]                      # 只看 i-1 為止的資料
        latest_date, market = build_market(upto)
        weights, strat_doc = run_optimize(reviews, cfg, strat_doc, latest_date)
        shifts, price_doc = run_price_opt(reviews, cfg, price_doc, latest_date)
        trade_date = snaps[i]["date"]
        # 隔夜訊號＝美東 trade_date-1 收盤（台灣 trade_date 清晨可得）→ 模擬「清晨校正」視角
        on_row = overnight_for(trade_date, on_doc) if on_doc else None
        regime = regime_state(upto, cfg, on_row)   # 環境閘門同樣只看歷史（walk-forward 誠實）
        picks = screen_book(market, cfg, weights, shifts, regime,
                            prev_book=_main_side(reviews[-1]) if reviews else None)
        picks_doc = {"generated_on": latest_date, "picks": picks}
        bars = (ensure_intraday(trade_date, picks) if allow_fetch and trade_date >= intraday_cutoff
                else load_intraday(trade_date))
        entry = run_review(picks_doc, snaps[i], cfg, bars)   # 用第 i 天實際行情驗證
        reviews.append(entry)
    n_intra = sum(r["summary"].get("n_intraday", 0) for r in reviews)
    n_all = sum(r["summary"]["n_picks"] for r in reviews)
    print(f"[rebuild] walk-forward 完成:{len(reviews)} 個復盤日（5分K核實 {n_intra}/{n_all} 筆）")
    return reviews, strat_doc, price_doc


def prune_history(keep):
    if not keep:
        return
    files = sorted((DATA / "history").glob("????-??-??.json"))
    for p in files[:-keep]:
        p.unlink()
        print(f"[prune] 移除過舊快照 {p.name}")


def dawn_correction(cfg):
    """清晨校正（台北 ~05:10，美股剛收盤）：抓隔夜訊號 → 重判環境 → 重出當日建議單。

    晚間初版只看得到台股寬度；清晨此刻補上美股整場的資訊（費半/S&P/NASDAQ 已消化
    Fed、財報、地緣等全部隔夜事件）。方向被翻轉時（如 2026-07-31 前夜費半 +8.19%
    而寬度判空）重選股救回整天；未翻轉時也更新 regime 附帶的隔夜數值供前端徽章顯示。"""
    today = datetime.now(TAIPEI).date()
    if today.weekday() >= 5:
        print("[dawn] 週末休市，跳過")
        return
    update_overnight((today - timedelta(days=10)).isoformat(), today.isoformat())
    on_row = overnight_for(today.isoformat())
    if not on_row:
        print("[dawn] 無隔夜資料（美股假日或抓取失敗），維持晚間版")
        return
    prev = load_json(LATEST / "picks.json", None)
    snaps = twstock.load_snapshots()
    reviews = load_json(DATA / "reviews.json", [])
    strat_doc = load_json(LATEST / "strategies.json", None)
    price_doc = load_json(LATEST / "price_model.json", None)
    market_doc, picks_doc, strat_doc, price_doc = generate_outputs(
        snaps, cfg, reviews, strat_doc, price_doc, overnight_row=on_row)
    old_book = ((prev or {}).get("regime") or {}).get("book")
    new_book = picks_doc["regime"]["book"]
    now = datetime.now(TAIPEI)
    picks_doc["dawn_corrected"] = bool(picks_doc["regime"].get("overnight_flip"))
    # 校正時刻：排程可能被 GitHub 延遲數小時，開盤（09:00）後才到的校正要讓使用者看得見，
    # 才不會拿盤中才變動的方向去回推早上的決策。
    picks_doc["dawn_at"] = now.isoformat(timespec="minutes")
    picks_doc["dawn_late"] = now.hour >= 9
    save_json(LATEST / "picks.json", picks_doc)
    save_json(LATEST / "market.json", market_doc)
    flip = picks_doc["regime"].get("overnight_flip")
    late = "（⚠️ 已開盤後才校正）" if picks_doc["dawn_late"] else ""
    print(f"[dawn] {now:%H:%M} 隔夜加權 {on_row.get('w'):+}%（費半 {on_row.get('sox'):+}%）→ "
          f"{'⚡ 環境翻轉 ' + str(old_book) + '→' + str(new_book) + '，已重出建議單' if flip else f'方向維持 {new_book}，僅更新隔夜資訊'}{late}")


def main():
    args = set(sys.argv[1:])
    cfg = load_config()
    LATEST.mkdir(parents=True, exist_ok=True)
    new_data = False

    if "--dawn" in args:
        dawn_correction(cfg)
        return

    if "--rebuild" not in args and "--offline" not in args:
        snap = twstock.build_latest_snapshot()
        if snap:
            p = twstock.snapshot_path(snap["date"])
            if p.exists():
                existing = json.loads(p.read_text(encoding="utf-8"))
                if "punish" not in existing:
                    # 回補產生的快照缺處置/注意/可當沖名單 → 以完整版本升級
                    twstock.save_snapshot(snap)
                    new_data = True
                    print(f"[fetch] 已升級 {snap['date']} 快照（補上排除名單）")
                else:
                    print(f"[fetch] {snap['date']} 快照已存在（今日已更新或休市）")
            else:
                twstock.save_snapshot(snap)
                new_data = True
                print(f"[fetch] 已儲存 {snap['date']} 快照（{len(snap['stocks'])} 檔）")
        else:
            print("[fetch] 無法取得最新行情，改用既有歷史資料")

    snaps = twstock.load_snapshots()
    if len(snaps) < cfg.get("min_history_days", 22):
        print(f"[error] 歷史資料僅 {len(snaps)} 天，請先執行 scripts/backfill.py")
        sys.exit(1)

    allow_fetch = "--offline" not in args
    if "--rebuild" in args:
        reviews, strat_doc, price_doc = rebuild_walkforward(cfg, allow_fetch)
    else:
        reviews = load_json(DATA / "reviews.json", [])
        strat_doc = load_json(LATEST / "strategies.json", None)
        price_doc = load_json(LATEST / "price_model.json", None)
        snapshots_by_date = {s["date"]: s for s in snaps}
        reviews, reviewed = do_review_if_due(cfg, reviews, snapshots_by_date, allow_fetch)
        new_data = new_data or reviewed
        if not new_data and "--force" not in args and "--offline" not in args:
            print("[skip] 無新交易日資料與新復盤（休市或已更新過），不重寫輸出")
            return

    # 若「訊號日當晚的美股」已收盤（清晨校正已抓過），重新產生輸出時要沿用該隔夜訊號，
    # 否則盤中重跑（rebuild／手動）會把當日清晨校正的方向靜默改回未校正版。
    # 精確比對訊號日 key（不用 overnight_for 的回溯查找）：晚間跑產生的是「隔天」建議，
    # 隔天的隔夜訊號尚未發生，回溯查找會誤用前一天的過期訊號。
    on_row = None
    if (cfg.get("overnight") or {}).get("enabled"):
        on_row = load_overnight().get(snaps[-1]["date"])
        if on_row:
            print(f"[fetch] 沿用 {snaps[-1]['date']} 已知隔夜訊號 {on_row.get('w'):+}%（保留清晨校正）")
    market_doc, picks_doc, strat_doc, price_doc = generate_outputs(
        snaps, cfg, reviews, strat_doc, price_doc, overnight_row=on_row)
    if on_row:   # 與清晨校正同口徑標記，前端徽章才不會因盤中重跑而消失
        picks_doc["dawn_corrected"] = bool(picks_doc["regime"].get("overnight_flip"))

    save_json(DATA / "reviews.json", reviews)
    save_json(LATEST / "market.json", market_doc)
    save_json(LATEST / "picks.json", picks_doc)
    save_json(LATEST / "strategies.json", strat_doc)
    if price_doc:
        save_json(LATEST / "price_model.json", price_doc)
    save_json(LATEST / "config_snapshot.json", cfg)
    prune_history(cfg.get("history_window", 60))
    print(f"[done] {market_doc['date']}：市場 {market_doc['count']} 檔、"
          f"推薦 {len(picks_doc['picks'])} 檔、復盤累計 {len(reviews)} 日")


if __name__ == "__main__":
    main()
