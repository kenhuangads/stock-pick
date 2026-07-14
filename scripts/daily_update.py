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
from indicators import build_market, market_breadth
from strategies import screen, evaluate, STRATEGIES, default_weight
from review import run_review
from optimize import run_optimize
from price_opt import run_price_opt
from intraday import ensure_intraday, load_intraday

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


def regime_state(snapshots, cfg):
    """大盤環境：市場寬度均值 ≥ 門檻為多方、< 門檻為空方。
    enabled 時據此切換選股方向：多方環境找做多、空方環境找做空
    （walk-forward 實證：空方環境做多平均負期望值——不做多、改做空才對）。
    book：當日建議單方向 long/short。"""
    rcfg = cfg.get("regime_filter") or {}
    b, bma = market_breadth(snapshots, rcfg.get("breadth_ma", 5))
    bull = (bma is None) or (bma >= rcfg.get("min_breadth", 0.5))
    book = "long" if (bull or not rcfg.get("enabled")) else "short"
    return {"breadth": round(b, 3) if b is not None else None,
            "breadth_ma": round(bma, 3) if bma is not None else None,
            "bull": bull, "enabled": bool(rcfg.get("enabled")), "book": book}


def generate_outputs(snapshots, cfg, reviews, strat_doc, price_doc):
    """由（時間排序的）快照序列產生 market/picks/strategies/price_model 輸出。"""
    latest_date, market = build_market(snapshots)
    weights, strat_doc = run_optimize(reviews, cfg, strat_doc, latest_date)
    shifts, price_doc = run_price_opt(reviews, cfg, price_doc, latest_date)
    regime = regime_state(snapshots, cfg)
    picks = screen(market, cfg, weights, shifts, side=regime["book"])
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
    for i in range(min_days, len(snaps)):
        upto = snaps[: i]                      # 只看 i-1 為止的資料
        latest_date, market = build_market(upto)
        weights, strat_doc = run_optimize(reviews, cfg, strat_doc, latest_date)
        shifts, price_doc = run_price_opt(reviews, cfg, price_doc, latest_date)
        regime = regime_state(upto, cfg)       # 環境閘門同樣只看歷史（walk-forward 誠實）
        picks = screen(market, cfg, weights, shifts, side=regime["book"])
        picks_doc = {"generated_on": latest_date, "picks": picks}
        trade_date = snaps[i]["date"]
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


def main():
    args = set(sys.argv[1:])
    cfg = load_config()
    LATEST.mkdir(parents=True, exist_ok=True)
    new_data = False

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

    market_doc, picks_doc, strat_doc, price_doc = generate_outputs(snaps, cfg, reviews, strat_doc, price_doc)

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
