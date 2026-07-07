"""為既有歷史快照補上籌碼面資料（上市三大法人買賣超 T86、融資融券 MI_MARGN）。

additive、冪等：只補尚未有籌碼欄位的日期，已補過者略過，可安全重跑。
上櫃個股籌碼 openapi 僅提供最新日，歷史無法回補，故僅補上市。

用法：python scripts/backfill_chip.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import twstock


def main():
    files = sorted(twstock.HISTORY_DIR.glob("????-??-??.json"))
    print(f"[chip] 掃描 {len(files)} 個歷史快照")
    done = 0
    for p in files:
        snap = json.loads(p.read_text(encoding="utf-8"))
        stocks = snap["stocks"]
        if any(("inst" in k) or ("mgn" in k) for k in stocks.values()):
            print(f"  {snap['date']} 已有籌碼，略過")
            continue
        iso = snap["date"]
        inst = twstock.fetch_twse_institutional(iso)
        margin = twstock.fetch_twse_margin(iso)
        if not inst and not margin:
            print(f"  {iso} 籌碼抓取皆空（可能限流），保留原狀，稍後可重跑")
            continue
        twstock.attach_chip(stocks, inst, margin)
        twstock.save_snapshot(snap)
        n_i = sum(1 for k in stocks.values() if "inst" in k)
        n_m = sum(1 for k in stocks.values() if "mgn" in k)
        done += 1
        print(f"  {iso}: 法人 {n_i} 檔、融資券 {n_m} 檔")
    print(f"[chip] 完成，補了 {done} 天")


if __name__ == "__main__":
    main()
