"""波段紀錄審計：以 ledger（規則隨單寫入）＋官方快照重算整個 state，須與存檔逐筆一致。

檢查項目：
1. 重算一致——trades / positions / unfilled / pending 的成交、出場、原因、損益與 state.json 完全相同
2. 結構——閘門未開啟不得有訊號；同日取用數不得超過空槽；標記「漲停買不到」者開盤價必須 ≥ 漲停價
3. 時效——ledger 最後處理日落後最新快照時只警告（不阻斷當沖資料 commit）

用法：python scripts/verify_swing.py（有問題列明細並以非零碼結束）
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import twstock
import swing

ROOT = Path(__file__).resolve().parent.parent
FIELDS = ("status", "fill", "fill_date", "stop", "target", "exit", "exit_date", "reason", "days", "ret_pct", "net",
          "open_next", "limit_up", "mfe_pct", "mae_pct", "lock")


def audit():
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    ledger = swing.load_json(swing.LEDGER, None)
    state = swing.load_json(swing.STATE, None)
    if not ledger or not state:
        print("[verify-swing] 尚無波段資料，略過")
        return 0
    snaps = twstock.load_snapshots()
    days = dict(ledger.get("days") or {})
    _, recomputed = swing.process(cfg, snaps, days, allow_new=False)
    issues = []
    for key in ("trades", "positions", "unfilled", "pending"):
        a = {(r["signal_date"], r["code"]): r for r in state.get(key, [])}
        b = {(r["signal_date"], r["code"]): r for r in recomputed.get(key, [])}
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                issues.append(f"{key} {k[0]} {k[1]} 只存在於{'存檔' if k in a else '重算'}")
                continue
            diff = [f for f in FIELDS if a[k].get(f) != b[k].get(f)]
            if diff:
                issues.append(f"{key} {k[0]} {k[1]} 重算不一致：" + "、".join(f"{f} {a[k].get(f)}≠{b[k].get(f)}" for f in diff))
    scfg = swing.swing_cfg(cfg)
    n_sig = n_taken = 0
    for d, rec in sorted(days.items()):
        sigs = rec.get("signals") or []
        n_sig += len(sigs)
        taken = sum(1 for s in sigs if s.get("taken"))
        n_taken += taken
        if rec.get("state") != "open" and sigs:
            issues.append(f"{d} 閘門未開啟（{rec.get('state')}）卻有 {len(sigs)} 筆訊號")
        cap = rec.get("cap") or scfg["max_positions"]
        if rec.get("open_before") is not None and taken + rec["open_before"] > cap:
            issues.append(f"{d} 取用 {taken} 筆＋既有持倉 {rec['open_before']} 超過上限 {cap}（{rec.get('tier')}）")
    for r in state.get("unfilled", []):
        if r.get("reason") == "limitup" and not (r["open_next"] >= r["limit_up"] - 1e-9):
            issues.append(f"{r['signal_date']} {r['code']} 標記漲停買不到但開盤 {r['open_next']} < 漲停 {r['limit_up']}")
    for r in state.get("trades", []):
        rules = r.get("rules") or scfg["exit"]
        if r["reason"] == "target" and r["days"] < rules["min_hold_days"]:
            issues.append(f"{r['signal_date']} {r['code']} 第 {r['days']} 日停利，早於最少持有 {rules['min_hold_days']} 日")
        if r["reason"] == "time" and r["days"] < rules["max_hold_days"]:
            issues.append(f"{r['signal_date']} {r['code']} 第 {r['days']} 日時間出場，早於 {rules['max_hold_days']} 日")
    print(f"[verify-swing] 掃描 {len(days)} 日、訊號 {n_sig} 筆（取用 {n_taken}）、已平倉 {len(state.get('trades', []))}、"
          f"持倉 {len(state.get('positions', []))}、買不到 {len(state.get('unfilled', []))}")
    if ledger.get("last_date") != snaps[-1]["date"]:
        print(f"[verify-swing] ⚠️ ledger 最後處理日 {ledger.get('last_date')} 落後最新快照 {snaps[-1]['date']}（波段流程可能失敗，請查 log）")
    if issues:
        print(f"[verify-swing] ⚠️ 發現 {len(issues)} 筆問題：")
        for i in issues:
            print("  -", i)
        return 1
    print("[verify-swing] ✅ 全部通過：重算一致、結構合法")
    return 0


if __name__ == "__main__":
    sys.exit(audit())
