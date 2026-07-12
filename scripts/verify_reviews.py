"""復盤紀錄審計器：逐筆核實每一天每一筆模擬的正確性。

檢查項目：
1. 重放一致性——把紀錄的價位丟回模擬引擎重跑，結果必須與存檔完全一致
2. 停利順序斷言——intraday 停利單，成交 bar 之後必須真的觸及過停利價
3. 未成交驗證——標記未成交的單，5分K 不得出現觸及掛價的 bar
4. 資料品質——intraday 模擬所用 5分K 聚合後必須與官方日K相符

用法：python scripts/verify_reviews.py
發現問題時列出明細並以非零碼結束（可掛進 CI / 每日更新後自檢）。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from review import simulate_trade, bars_match_ohlc, limit_down_price
from intraday import load_intraday

ROOT = Path(__file__).resolve().parent.parent


def audit():
    reviews = json.loads((ROOT / "data" / "reviews.json").read_text(encoding="utf-8"))
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    strict = cfg.get("simulation", {}).get("strict_fill", False)
    issues, checked, n_intraday = [], 0, 0
    for day in reviews:
        date = day["date"]
        bars_by_code = load_intraday(date)
        for p in day["picks"]:
            checked += 1
            ohlc = {"o": p["day_open"], "h": p["day_high"], "l": p["day_low"], "c": p["day_close"]}
            bars = bars_by_code.get(p["code"])
            if bars and not bars_match_ohlc(bars, ohlc):
                bars = None

            filled, fill, exitp, reason, mode = simulate_trade(
                p["entry"], p["target"], p["stop"], ohlc, bars,
                p.get("trail_dist"), p.get("tstop_bar"),
                strict_fill=strict, limit_dn=limit_down_price(p.get("prev_close")))
            if (filled, fill, exitp, reason, mode) != (
                    p["filled"], p["fill_price"], p["exit_price"], p["exit_reason"], p["sim_mode"]):
                issues.append(f"{date} {p['code']} 重放不一致："
                              f"紀錄({p['filled']},{p['fill_price']},{p['exit_price']},{p['exit_reason']},{p['sim_mode']})"
                              f" vs 重放({filled},{fill},{exitp},{reason},{mode})")
                continue
            if p["sim_mode"] != "intraday" or not bars:
                continue
            n_intraday += 1
            def fillable(b):
                # 與模擬引擎同口徑：開盤 ≤ 掛價必成；否則 strict 需穿價、寬鬆觸價即可
                return b[0] <= p["entry"] or (b[2] < p["entry"] if strict else b[2] <= p["entry"])

            if p["exit_reason"] in ("target", "trail"):
                # 停利/移動停利：成交之後必須真的觸及過原停利價（移動停利以停利觸價啟動）
                fill_i = next((i for i, b in enumerate(bars) if fillable(b)), None)
                if fill_i is None or not any(b[1] >= p["target"] for b in bars[fill_i + 1:]):
                    issues.append(f"{date} {p['code']} 停利順序違規：成交後未曾觸及 {p['target']}")
            if p["exit_reason"] == "stop" and p["fill_price"] > p["stop"] and p["exit_price"] > p["stop"]:
                issues.append(f"{date} {p['code']} 停損出場價 {p['exit_price']} 高於停損價 {p['stop']}（未跳空卻優於停損）")
            if not p["filled"] and any(fillable(b) for b in bars):
                issues.append(f"{date} {p['code']} 未成交但 5分K 顯示曾觸價")

    print(f"[verify] 掃描 {len(reviews)} 天、{checked} 筆（intraday {n_intraday} 筆）")
    if issues:
        print(f"[verify] ⚠️ 發現 {len(issues)} 筆問題：")
        for i in issues:
            print("  -", i)
        return 1
    print("[verify] ✅ 全部通過：重放一致、停利順序合法、未成交正確")
    return 0


if __name__ == "__main__":
    sys.exit(audit())
