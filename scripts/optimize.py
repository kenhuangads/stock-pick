"""迭代優化：依復盤紀錄滾動評估各策略績效，汰弱留強調整權重。

規則（全部參數在 config.json 的 optimizer 區塊）：
- 以最近 window_days 個復盤日為評估窗口，統計每個策略「有背書的成交單」
  之勝率、平均報酬與期望值（每筆淨損益 / 進場成本）。
- 樣本數 ≥ min_trades 且期望值 < disable_expectancy_below → 停用（權重 0），
  但持續虛擬追蹤，窗口內表現回升會自動重新啟用。
- 其餘依 勝率、期望值 映射權重： weight = 0.5 + (win_rate-50)*0.03 + expectancy*40，
  夾在 [weight_min, weight_max] 之間。表現越好，該策略在綜合評分中話語權越大。
- 候選策略（candidate）：預設權重 0 只虛擬追蹤；樣本 ≥ min_trades 且期望值
  ≥ enable_expectancy_above 才「實證轉正」自動啟用，之後與核心策略同規則汰弱留強。
- 每次啟用/停用變化都寫進 evolution log，前端「策略績效」頁完整呈現。
"""
from strategies import STRATEGIES, default_weight


def strategy_stats(reviews, window_days):
    window = reviews[-window_days:] if window_days else reviews
    stats = {s["id"]: {"trades": 0, "wins": 0, "net_sum": 0, "cost_sum": 0.0} for s in STRATEGIES}
    for day in window:
        for r in day["picks"]:
            if not r["filled"]:
                continue
            cost = r["fill_price"] * 1000
            for sid in r["strategies"]:
                if sid not in stats:
                    continue
                st = stats[sid]
                st["trades"] += 1
                st["wins"] += 1 if r["net"] > 0 else 0
                st["net_sum"] += r["net"]
                st["cost_sum"] += cost
    out = {}
    for sid, st in stats.items():
        trades = st["trades"]
        win_rate = st["wins"] / trades * 100 if trades else None
        expectancy = (st["net_sum"] / st["cost_sum"]) if st["cost_sum"] else None  # 每元成本的淨報酬
        out[sid] = {
            "trades": trades,
            "wins": st["wins"],
            "win_rate": round(win_rate, 1) if win_rate is not None else None,
            "net_sum": st["net_sum"],
            "avg_net": round(st["net_sum"] / trades) if trades else None,
            "expectancy": round(expectancy * 100, 3) if expectancy is not None else None,  # %
        }
    return out


def compute_weights(stats, opt_cfg, prev_doc=None):
    """回傳 (weights{sid:w}, stats_with_weight, log_entries)"""
    prev_enabled = {}
    if prev_doc:
        for sid, s in prev_doc.get("stats", {}).items():
            prev_enabled[sid] = s.get("enabled", True)
    weights, log = {}, []
    for s in STRATEGIES:
        sid = s["id"]
        st = stats[sid]
        is_cand = s.get("candidate", False)
        enabled = not is_cand          # 候選策略未經實證前不啟用
        w = default_weight(s)
        if st["trades"] >= opt_cfg["min_trades"] and st["expectancy"] is not None:
            exp_frac = st["expectancy"] / 100
            threshold = opt_cfg.get("enable_expectancy_above", 0) if is_cand and not prev_enabled.get(sid, False) \
                else opt_cfg["disable_expectancy_below"]
            if exp_frac < threshold:
                enabled = False
                w = 0.0
            else:
                enabled = True
                w = 0.5 + (st["win_rate"] - 50) * 0.03 + exp_frac * 40
                w = max(opt_cfg["weight_min"], min(opt_cfg["weight_max"], round(w, 2)))
        st["weight"] = w
        st["enabled"] = enabled
        st["candidate"] = is_cand
        weights[sid] = w
        # 候選策略以「未啟用」為初始狀態，首次轉正也要留下紀錄
        was = prev_enabled.get(sid, False if is_cand else None)
        if was is not None and was != enabled:
            if is_cand and enabled:
                msg = f"候選策略「{s['name']}」實證有效（期望值轉正），自動納入計分"
            elif is_cand:
                msg = f"候選策略「{s['name']}」期望值轉負，退回觀察區（持續虛擬追蹤）"
            elif not enabled:
                msg = f"策略「{s['name']}」停用（期望值轉負，汰弱）"
            else:
                msg = f"策略「{s['name']}」重新啟用（績效回升，留強）"
            log.append(msg)
    return weights, stats, log


def run_optimize(reviews, cfg, prev_doc, as_of_date):
    stats = strategy_stats(reviews, cfg["optimizer"]["window_days"])
    weights, stats, changes = compute_weights(stats, cfg["optimizer"], prev_doc)
    log = (prev_doc or {}).get("log", [])
    for msg in changes:
        log.append({"date": as_of_date, "msg": msg})
    doc = {
        "updated": as_of_date,
        "window_days": cfg["optimizer"]["window_days"],
        "review_days": len(reviews),
        "stats": stats,
        "log": log[-200:],
        "meta": {s["id"]: {"name": s["name"], "desc": s["desc"], "candidate": s.get("candidate", False)}
                 for s in STRATEGIES},
    }
    return weights, doc
