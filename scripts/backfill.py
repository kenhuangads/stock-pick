"""歷史資料回補：往回抓 N 個交易日的全市場快照（TWSE + TPEx + 當沖統計）。

用法：python scripts/backfill.py [交易日數，預設 45]
已存在的日期會跳過，可安全重跑／續跑。
"""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import twstock


def main():
    target = int(sys.argv[1]) if len(sys.argv) > 1 else 45
    # 以最新可得的交易日為起點
    latest_iso, _ = twstock.fetch_twse_day_all()
    print(f"[backfill] 最新交易日 {latest_iso}，目標回補 {target} 個交易日")
    d = date.fromisoformat(latest_iso)
    have = 0
    probes = 0
    while have < target and probes < target * 2 + 30:
        iso = d.isoformat()
        d -= timedelta(days=1)
        probes += 1
        if d.weekday() >= 5:  # 集中在平日探測（週末直接跳過）
            pass
        if date.fromisoformat(iso).weekday() >= 5:
            continue
        p = twstock.snapshot_path(iso)
        if p.exists():
            have += 1
            print(f"  {iso} 已存在，跳過（{have}/{target}）")
            continue
        snap = twstock.build_snapshot_for_date(iso)
        if snap is None:
            print(f"  {iso} 非交易日")
            continue
        twstock.save_snapshot(snap)
        have += 1
        n_dt = sum(1 for k in snap["stocks"].values() if k.get("dt"))
        print(f"  {iso} 完成：{len(snap['stocks'])} 檔（含當沖統計 {n_dt} 檔）（{have}/{target}）")
    print(f"[backfill] 結束，共 {have} 個交易日可用")


if __name__ == "__main__":
    main()
