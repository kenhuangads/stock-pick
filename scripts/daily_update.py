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
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import twstock
from indicators import build_market
from strategies import screen, evaluate, STRATEGIES, DEFAULT_WEIGHT
from review import run_review
from optimize import run_optimize

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
    if not doc:
        return {s["id"]: DEFAULT_WEIGHT for s in STRATEGIES}
    return {sid: st.get("weight", DEFAULT_WEIGHT) for sid, st in doc.get("stats", {}).items()}


def generate_outputs(snapshots, cfg, reviews, strat_doc):
    """由（時間排序的）快照序列產生 market/picks/strategies 輸出。"""
    latest_date, market = build_market(snapshots)
    weights, strat_doc = run_optimize(reviews, cfg, strat_doc, latest_date)
    picks = screen(market, cfg, weights)
    for m in market.values():  # 供前端自訂選股使用的個股觸發標記
        score, hits = evaluate(m, weights)
        m["score"], m["strategies"] = score, hits
    picks_doc = {
        "generated_on": latest_date,
        "generated_at": datetime.now(TAIPEI).isoformat(timespec="seconds"),
        "weights_used": weights,
        "note": "建議單適用於 generated_on 之後的下一個交易日",
        "picks": picks,
    }
    market_doc = {
        "date": latest_date,
        "updated_at": datetime.now(TAIPEI).isoformat(timespec="seconds"),
        "count": len(market),
        "strategies": [{"id": s["id"], "name": s["name"], "desc": s["desc"]} for s in STRATEGIES],
        "stocks": sorted(market.values(), key=lambda m: -m["val"]),
    }
    return market_doc, picks_doc, strat_doc


def do_review_if_due(cfg, reviews, snapshots_by_date):
    """把尚未復盤的建議單，對「產生日之後第一個交易日」執行模擬。"""
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
    entry = run_review(picks_doc, snapshots_by_date[trade_date], cfg)
    reviews.append(entry)
    reviews.sort(key=lambda r: r["date"])
    print(f"[review] {gen} 的建議單以 {trade_date} 實際行情復盤："
          f"成交 {entry['summary']['n_filled']} 筆、淨損益 {entry['summary']['net']} 元")
    return reviews, True


def rebuild_walkforward(cfg):
    """由 history 全量重建：每天只用「當天以前」的資料選股，再用隔天實際行情復盤。
    嚴格 walk-forward，權重沿路演化，和真實每日執行的結果一致。"""
    snaps = twstock.load_snapshots()
    min_days = cfg.get("min_history_days", 22)
    if len(snaps) <= min_days:
        print(f"[rebuild] 歷史僅 {len(snaps)} 天，不足 {min_days}+1 天，略過")
        return [], None
    reviews, strat_doc = [], None
    for i in range(min_days, len(snaps)):
        upto = snaps[: i]                      # 只看 i-1 為止的資料
        latest_date, market = build_market(upto)
        weights, strat_doc = run_optimize(reviews, cfg, strat_doc, latest_date)
        picks = screen(market, cfg, weights)
        picks_doc = {"generated_on": latest_date, "picks": picks}
        entry = run_review(picks_doc, snaps[i], cfg)   # 用第 i 天實際行情驗證
        reviews.append(entry)
    print(f"[rebuild] walk-forward 完成：{len(reviews)} 個復盤日")
    return reviews, strat_doc


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

    if "--rebuild" in args:
        reviews, strat_doc = rebuild_walkforward(cfg)
    else:
        reviews = load_json(DATA / "reviews.json", [])
        strat_doc = load_json(LATEST / "strategies.json", None)
        snapshots_by_date = {s["date"]: s for s in snaps}
        reviews, reviewed = do_review_if_due(cfg, reviews, snapshots_by_date)
        new_data = new_data or reviewed
        if not new_data and "--force" not in args and "--offline" not in args:
            print("[skip] 無新交易日資料與新復盤（休市或已更新過），不重寫輸出")
            return

    market_doc, picks_doc, strat_doc = generate_outputs(snaps, cfg, reviews, strat_doc)

    save_json(DATA / "reviews.json", reviews)
    save_json(LATEST / "market.json", market_doc)
    save_json(LATEST / "picks.json", picks_doc)
    save_json(LATEST / "strategies.json", strat_doc)
    save_json(LATEST / "config_snapshot.json", cfg)
    prune_history(cfg.get("history_window", 60))
    print(f"[done] {market_doc['date']}：市場 {market_doc['count']} 檔、"
          f"推薦 {len(picks_doc['picks'])} 檔、復盤累計 {len(reviews)} 日")


if __name__ == "__main__":
    main()
