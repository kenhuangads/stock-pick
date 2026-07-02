"""5 分 K 取得與儲存：用來核實復盤的進出場「順序」。

日 K 只有 OHLC、看不出日內先後——若股價先觸停利價、之後才跌到掛買價，
日K模擬會誤記一筆吃不到的停利單。5 分 K 能還原順序，讓復盤貼近真實。

資料源：Yahoo Finance chart API（上市 {code}.TW／上櫃 {code}.TWO），
5 分線約可回溯 55~60 天。抓到的 bars 存 data/intraday/{date}.json
（僅存當日建議單的個股，永久保留供 walk-forward 重放），格式：
  {"2472": [[o,h,l,c], ...], ...}   # 依時間排序
"""
import json
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INTRADAY_DIR = ROOT / "data" / "intraday"
TAIPEI = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}


def _symbol(code, market):
    return f"{code}.{'TWO' if market == 'tpex' else 'TW'}"


def fetch_5m_series(code, market, d1, d2, retries=2):
    """抓單一個股 [d1, d2]（含）期間的 5 分K，回傳 {date: [[o,h,l,c],...]}；失敗回 {}。"""
    p1 = int(datetime.strptime(d1, "%Y-%m-%d").replace(tzinfo=TAIPEI).timestamp())
    p2 = int(datetime.strptime(d2, "%Y-%m-%d").replace(tzinfo=TAIPEI).timestamp()) + 86400
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{_symbol(code, market)}"
           f"?interval=5m&period1={p1}&period2={p2}")
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read())
            res = (data.get("chart", {}).get("result") or [None])[0]
            if not res or not res.get("timestamp"):
                return {}
            q = res["indicators"]["quote"][0]
            out = {}
            for i, t in enumerate(res["timestamp"]):
                o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
                if o is None or h is None or l is None or c is None:
                    continue
                day = datetime.fromtimestamp(t, TAIPEI).strftime("%Y-%m-%d")
                out.setdefault(day, []).append([round(o, 2), round(h, 2), round(l, 2), round(c, 2)])
            return out
        except Exception as e:
            if attempt == retries:
                print(f"[intraday] {code} 抓取失敗：{e}")
                return {}
            time.sleep(1.5)
    return {}


def intraday_path(date):
    return INTRADAY_DIR / f"{date}.json"


def load_intraday(date):
    """回傳該日 {code: bars}；無檔案回 {}。"""
    p = intraday_path(date)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {}


def save_intraday(date, by_code):
    INTRADAY_DIR.mkdir(parents=True, exist_ok=True)
    intraday_path(date).write_text(
        json.dumps(by_code, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def ensure_intraday(date, picks, sleep_s=0.4):
    """確保 date 當日、picks 清單個股的 5 分K已存檔（缺的才抓）。回傳 {code: bars}。"""
    have = load_intraday(date)
    missing = [p for p in picks if p["code"] not in have]
    for p in missing:
        series = fetch_5m_series(p["code"], p.get("market"), date, date)
        bars = series.get(date)
        if bars:
            have[p["code"]] = bars
        time.sleep(sleep_s)  # 對 Yahoo 客氣一點
    if missing:
        save_intraday(date, have)
        got = sum(1 for p in missing if p["code"] in have)
        print(f"[intraday] {date} 補抓 {got}/{len(missing)} 檔 5 分K")
    return have


def backfill_reviews(reviews):
    """為既有復盤紀錄回補 5 分K：依個股分組、一次抓整段區間再按日拆檔。
    Yahoo 5 分線僅回溯約 60 天，更早的日期抓不到會自動略過（模擬 fallback 日K）。"""
    need = {}   # code -> {"market": m, "dates": set()}
    for day in reviews:
        have = load_intraday(day["date"])
        for r in day["picks"]:
            if r["code"] in have:
                continue
            info = need.setdefault(r["code"], {"market": r.get("market"), "dates": set()})
            info["dates"].add(day["date"])
    if not need:
        print("[intraday] 歷史 5 分K已齊全")
        return
    all_dates = sorted({d for v in need.values() for d in v["dates"]})
    d1, d2 = all_dates[0], all_dates[-1]
    print(f"[intraday] 回補 {len(need)} 檔個股、{d1} ~ {d2}")
    per_date = {d: load_intraday(d) for d in all_dates}
    for i, (code, info) in enumerate(sorted(need.items()), 1):
        series = fetch_5m_series(code, info["market"], d1, d2)
        for d in info["dates"]:
            if d in series:
                per_date[d][code] = series[d]
        time.sleep(0.4)
        if i % 20 == 0:
            print(f"[intraday] 進度 {i}/{len(need)}")
    for d, by_code in per_date.items():
        if by_code:
            save_intraday(d, by_code)
    covered = sum(1 for d in all_dates for c in per_date[d])
    print(f"[intraday] 回補完成：{len(all_dates)} 日、共 {covered} 檔次")
