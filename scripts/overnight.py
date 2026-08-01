"""隔夜訊號：美股收盤對台股「今日」的環境修正。

為什麼需要：選股在台股收盤後產生，但到隔日開盤前還有美股整場（台灣時間
21:30 ～ 夏令 04:00／冬令 05:00）的巨量資訊——2026-07-31 台股創最大漲點
前夜，費半 +8.19%，而系統依前日台股寬度判空方、出了 8 檔空單（實測案例）。
清晨排程抓到美股收盤後，用本模組把隔夜訊號融進環境判定做「清晨校正」。

訊號定義：overnight_pct = 0.5·SOX + 0.3·GSPC + 0.2·IXIC（費半對台股電子
權重最高；美股價格已消化 Fed／財報／地緣等全部消息，是「最新事件」的
總和量化，比解析新聞可靠）。

對齊規則：台股交易日 D 的隔夜訊號 = 「D 前最近一個美股交易日」的漲跌%
（美東 D-1 收盤＝台灣 D 日清晨）。walk-forward 重放與清晨即時判定同一口徑。

資料存 data/overnight.json：{"美東日期": {"sox":%, "gspc":%, "ixic":%, "w":加權%}}，
每日清晨排程增量更新，歷史一次回補（Yahoo 日線可回溯多年）。
"""
import json
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OVERNIGHT_PATH = ROOT / "data" / "overnight.json"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
US_EAST_APPROX = timezone(timedelta(hours=-5))  # 只用於把時間戳歸到美東「日期」，DST 誤差不影響日界

SYMBOLS = {"sox": "^SOX", "gspc": "^GSPC", "ixic": "^IXIC"}
WEIGHTS = {"sox": 0.5, "gspc": 0.3, "ixic": 0.2}


def _fetch_closes(symbol, d1_iso, d2_iso, retries=2):
    """Yahoo 日線收盤 {美東日期: close}。"""
    p1 = int(datetime.fromisoformat(d1_iso).timestamp()) - 86400
    p2 = int(datetime.fromisoformat(d2_iso).timestamp()) + 2 * 86400
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?interval=1d&period1={p1}&period2={p2}")
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
                c = q["close"][i]
                if c:
                    day = datetime.fromtimestamp(t, US_EAST_APPROX).strftime("%Y-%m-%d")
                    out[day] = c
            return out
        except Exception as e:  # noqa: BLE001
            if attempt == retries:
                print(f"[overnight] {symbol} 抓取失敗：{e}")
                return {}
            time.sleep(1.5)
    return {}


def load_overnight():
    if OVERNIGHT_PATH.exists():
        return json.loads(OVERNIGHT_PATH.read_text(encoding="utf-8"))
    return {}


def save_overnight(doc):
    OVERNIGHT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OVERNIGHT_PATH.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
                              encoding="utf-8")


def update_overnight(d1_iso, d2_iso):
    """抓 [d1, d2] 期間三大指數，計算逐日漲跌% 併入 overnight.json。回傳 doc。"""
    closes = {k: _fetch_closes(sym, d1_iso, d2_iso) for k, sym in SYMBOLS.items()}
    doc = load_overnight()
    all_days = sorted(set().union(*[set(v) for v in closes.values()]))
    for i in range(1, len(all_days)):
        d, d0 = all_days[i], all_days[i - 1]
        row = doc.get(d, {})
        for k in SYMBOLS:
            c, c0 = closes[k].get(d), closes[k].get(d0)
            if c and c0:
                row[k] = round((c / c0 - 1) * 100, 2)
        if row:
            row["w"] = round(sum(WEIGHTS[k] * row.get(k, 0) for k in WEIGHTS), 2)
            doc[d] = row
    save_overnight(doc)
    return doc


def overnight_for(tw_date, doc=None):
    """台股交易日 tw_date 的隔夜訊號＝「該日之前最近一個美股交易日」的漲跌row；
    無資料（假日斷檔/未回補）回 None。超過 5 天找不到視為斷檔。"""
    doc = doc if doc is not None else load_overnight()
    d = datetime.fromisoformat(tw_date).date()
    for back in range(1, 6):
        key = (d - timedelta(days=back)).isoformat()
        if key in doc:
            return doc[key]
    return None
