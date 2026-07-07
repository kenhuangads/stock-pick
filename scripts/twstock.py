"""台股資料抓取層：TWSE / TPEx 公開 API。

資料來源（全部為官方免費公開端點）：
- TWSE openapi STOCK_DAY_ALL      上市全部個股當日收盤行情（最新一日）
- TWSE rwd MI_INDEX               上市全部個股收盤行情（指定歷史日期，回補用）
- TWSE legacy TWTB4U              上市個股當日沖銷交易統計（指定日期）
- TWSE openapi TWTB4U             上市可現股當沖標的與暫停註記（最新）
- TWSE openapi announcement/punish, announcement/notice  處置股 / 注意股
- TWSE openapi t187ap03_L         上市公司基本資料（已發行股數，週轉率用）
- TPEx www dailyQuotes            上櫃全部個股收盤行情（含歷史日期、發行股數）
- TPEx openapi tpex_securities    上櫃可現股當沖標的
- TPEx openapi tpex_disposal_information / tpex_trading_warning_information  處置 / 注意
"""
import json
import re
import ssl
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

TAIPEI = timezone(timedelta(hours=8))

import requests
from requests.adapters import HTTPAdapter


class _NonStrictTLSAdapter(HTTPAdapter):
    """Python 3.13+ 預設啟用 X509 嚴格驗證，TPEx 憑證缺 SKI 欄位會被拒；
    這裡僅關閉 strict 旗標，鏈驗證與主機名驗證照常。"""

    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 stock-pick/1.0")
TIMEOUT = 45
SLEEP_BETWEEN = 3.0   # 對官方站台保持禮貌的請求間隔（秒），過快會觸發 TWSE 軟性限流

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
HISTORY_DIR = DATA_DIR / "history"

_session = requests.Session()
_session.mount("https://", _NonStrictTLSAdapter())
_session.headers.update({"User-Agent": UA, "Accept": "application/json"})


def http_get_json(url, params=None, retries=3, sleep=SLEEP_BETWEEN, headers=None):
    last_err = None
    for i in range(retries):
        try:
            r = _session.get(url, params=params, timeout=TIMEOUT, headers=headers)
            if r.status_code == 200 and r.text.strip():
                time.sleep(sleep)
                return r.json()
            last_err = f"HTTP {r.status_code} len={len(r.text)}"
        except Exception as e:  # noqa: BLE001
            last_err = repr(e)
        time.sleep(2 + i * 2)
    raise RuntimeError(f"GET {url} failed after {retries} tries: {last_err}")


def safe_get_json(url, params=None, retries=2, headers=None):
    """失敗時回傳 None（用於非必要的增強資料，避免整條管線中斷）。"""
    try:
        return http_get_json(url, params, retries=retries, headers=headers)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] optional fetch failed: {url} ({e})")
        return None


# ---------- 日期工具 ----------

def roc_to_iso(roc: str) -> str:
    """'1150701' 或 '115/07/01' -> '2026-07-01'"""
    s = roc.strip().replace("/", "")
    y, m, d = int(s[:-4]) + 1911, int(s[-4:-2]), int(s[-2:])
    return f"{y:04d}-{m:02d}-{d:02d}"


def iso_to_roc_slash(iso: str) -> str:
    y, m, d = iso.split("-")
    return f"{int(y) - 1911}/{m}/{d}"


def iso_to_ymd(iso: str) -> str:
    return iso.replace("-", "")


def parse_num(s):
    """'1,234.56' / '--' / '' -> float 或 None"""
    if s is None:
        return None
    s = str(s).replace(",", "").strip()
    if s in ("", "--", "---", "----", "-----", "X", "N/A", "0.0000") and s != "0.0000":
        return None
    try:
        return float(s)
    except ValueError:
        return None


CODE_RE = re.compile(r"^(\d{4}|00\d{2,4}[A-Z]?)$")


def is_tradeable_code(code: str) -> bool:
    """保留 4 碼普通股/TDR 與 00 開頭 ETF；排除特別股、權證等。"""
    return bool(CODE_RE.match(code.strip()))


# ---------- TWSE ----------

def fetch_twse_day_all():
    """最新一個交易日的上市全部個股行情。回傳 (iso_date, {code: row})"""
    data = http_get_json("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL")
    stocks, iso = {}, None
    for r in data:
        code = r.get("Code", "").strip()
        if not is_tradeable_code(code):
            continue
        iso = iso or roc_to_iso(r["Date"])
        o, h, l, c = (parse_num(r.get(k)) for k in
                      ("OpeningPrice", "HighestPrice", "LowestPrice", "ClosingPrice"))
        v, a, t = parse_num(r.get("TradeVolume")), parse_num(r.get("TradeValue")), parse_num(r.get("Transaction"))
        if None in (o, h, l, c) or not v:
            continue
        stocks[code] = {"n": r.get("Name", "").strip(), "m": "twse",
                        "o": o, "h": h, "l": l, "c": c,
                        "v": int(v), "a": int(a or 0), "t": int(t or 0)}
    return iso, stocks


def fetch_twse_mi_index(iso_date, soft_retries=4):
    """指定日期的上市全部個股行情（歷史回補）。非交易日回傳 {}。

    TWSE rwd 端點有軟性限流：過於頻繁時對合法日期也回「很抱歉，沒有符合條件的資料!」，
    與真正的休市日無法區分，因此遇到時以遞增等待重試。
    """
    data = None
    for i in range(soft_retries + 1):
        data = safe_get_json(
            "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX",
            {"date": iso_to_ymd(iso_date), "type": "ALLBUT0999", "response": "json"},
            headers={"Connection": "close"})
        if data and data.get("stat") == "OK":
            break
        if i < soft_retries:
            wait = 20 + i * 25
            print(f"[throttle] MI_INDEX {iso_date} 未回資料，{wait}s 後重試（{i + 1}/{soft_retries}）")
            time.sleep(wait)
    if not data or data.get("stat") != "OK":
        return {}
    table = None
    for t in data.get("tables", []):
        f = t.get("fields") or []
        if "證券代號" in f and any("收盤價" in x for x in f):
            table = t
            break
    if not table:
        return {}
    idx = {name: i for i, name in enumerate(table["fields"])}

    def col(row, key):
        return row[idx[key]] if key in idx else None

    stocks = {}
    for row in table.get("data", []):
        code = str(col(row, "證券代號") or "").strip()
        if not is_tradeable_code(code):
            continue
        o, h, l, c = (parse_num(col(row, k)) for k in ("開盤價", "最高價", "最低價", "收盤價"))
        v = parse_num(col(row, "成交股數"))
        a = parse_num(col(row, "成交金額"))
        t = parse_num(col(row, "成交筆數"))
        if None in (o, h, l, c) or not v:
            continue
        stocks[code] = {"n": str(col(row, "證券名稱") or "").strip(), "m": "twse",
                        "o": o, "h": h, "l": l, "c": c,
                        "v": int(v), "a": int(a or 0), "t": int(t or 0)}
    return stocks


def fetch_twse_daytrade_stats(iso_date):
    """指定日期上市個股當沖成交股數 {code: dt_shares}；拿不到回 {}。"""
    # 這個舊版端點對 gzip 與連線重用的處理有問題：必須要求未壓縮內容，
    # 且每次都用全新連線（重用 MI_INDEX 用過的 keep-alive 連線會逾時）。
    data = None
    for attempt in range(3):
        try:
            r = requests.get(
                "https://www.twse.com.tw/exchangeReport/TWTB4U",
                params={"response": "json", "date": iso_to_ymd(iso_date), "selectType": "All"},
                headers={"User-Agent": UA, "Accept-Encoding": "identity", "Connection": "close"},
                timeout=60)
            if r.status_code == 200 and r.text.strip():
                data = r.json()
                time.sleep(SLEEP_BETWEEN)
                break
        except Exception as e:  # noqa: BLE001
            print(f"[warn] TWTB4U attempt {attempt + 1} failed: {e!r}")
        time.sleep(2 + attempt * 2)
    if not data or data.get("stat") != "OK":
        return {}
    out = {}
    for t in data.get("tables", []):
        f = t.get("fields") or []
        if "證券代號" not in f:
            continue
        idx = {name: i for i, name in enumerate(f)}
        vol_key = next((k for k in f if "成交股數" in k), None)
        if not vol_key:
            continue
        for row in t.get("data", []):
            code = str(row[idx["證券代號"]]).strip()
            v = parse_num(row[idx[vol_key]])
            if is_tradeable_code(code) and v:
                out[code] = int(v)
    return out


def fetch_twse_daytrade_eligible():
    """上市可現股當沖名單。回傳 (eligible_codes, suspended_codes)。"""
    data = safe_get_json("https://openapi.twse.com.tw/v1/exchangeReport/TWTB4U")
    ok, sus = set(), set()
    for r in data or []:
        code = r.get("Code", "").strip()
        if not code:
            continue
        if str(r.get("Suspension", "")).strip():
            sus.add(code)
        else:
            ok.add(code)
    return ok, sus


def fetch_tpex_daytrade_eligible():
    data = safe_get_json("https://www.tpex.org.tw/openapi/v1/tpex_securities")
    ok, sus = set(), set()
    for r in data or []:
        code = str(r.get("證券代號", "")).strip()
        if not code:
            continue
        if str(r.get("暫停現股賣出後現款買進當沖註記", "")).strip():
            sus.add(code)   # 暫停先賣後買，先買後賣仍可；標記供參考
        ok.add(code)
    return ok, sus


def _parse_period_end(period: str):
    """'115/06/29～115/07/10' 或 '1150701~1150714' -> 迄日 ISO"""
    dates = re.findall(r"\d{3}/\d{2}/\d{2}|\d{7}", str(period))
    if not dates:
        return None
    try:
        return roc_to_iso(dates[-1])
    except Exception:  # noqa: BLE001
        return None


def fetch_punish(today_iso):
    """處置中的股票代號集合（TWSE + TPEx，處置期間涵蓋今日以後者）。"""
    codes = set()
    for url, code_key, period_key in [
        ("https://openapi.twse.com.tw/v1/announcement/punish", "Code", "DispositionPeriod"),
        ("https://www.tpex.org.tw/openapi/v1/tpex_disposal_information",
         "SecuritiesCompanyCode", "DispositionPeriod"),
    ]:
        for r in safe_get_json(url) or []:
            code = str(r.get(code_key, "")).strip()
            end = _parse_period_end(r.get(period_key, ""))
            if code and end and end >= today_iso:
                codes.add(code)
    return codes


def fetch_notice(today_iso):
    """最近 4 天內公告的注意股代號集合（TWSE + TPEx）。"""
    cutoff = (date.fromisoformat(today_iso) - timedelta(days=4)).isoformat()
    codes = set()
    for url, code_key, date_key in [
        ("https://openapi.twse.com.tw/v1/announcement/notice", "Code", "Date"),
        ("https://www.tpex.org.tw/openapi/v1/tpex_trading_warning_information",
         "SecuritiesCompanyCode", "Date"),
    ]:
        for r in safe_get_json(url) or []:
            code = str(r.get(code_key, "")).strip()
            d = str(r.get(date_key, "")).strip()
            if not code or not d:
                continue
            try:
                if roc_to_iso(d) >= cutoff:
                    codes.add(code)
            except Exception:  # noqa: BLE001
                continue
    return codes


def fetch_twse_shares_issued():
    """{code: 已發行普通股股數}（週轉率計算用）。"""
    data = safe_get_json("https://openapi.twse.com.tw/v1/opendata/t187ap03_L")
    out = {}
    for r in data or []:
        code = str(r.get("公司代號", "")).strip()
        sh = parse_num(r.get("已發行普通股股數（股）") or r.get("已發行普通股股數"))
        if code and sh:
            out[code] = int(sh)
    return out


# ---------- 籌碼面（三大法人買賣超、融資融券）----------

def fetch_twse_institutional(iso_date):
    """指定日期上市個股三大法人合計買賣超（張）。{code: net_lots}；拿不到回 {}。
    T86 為扁平 fields/data 結構，最後一欄「三大法人買賣超股數」單位為股，÷1000 轉張。"""
    data = safe_get_json(
        "https://www.twse.com.tw/rwd/zh/fund/T86",
        {"date": iso_to_ymd(iso_date), "selectType": "ALL", "response": "json"},
        headers={"Connection": "close"})
    if not data or data.get("stat") != "OK":
        return {}
    fields = data.get("fields", [])
    i_code = next((i for i, f in enumerate(fields) if "證券代號" in str(f)), 0)
    i_net = next((i for i, f in enumerate(fields) if "三大法人買賣超股數" in str(f)), None)
    if i_net is None:
        return {}
    out = {}
    for row in data.get("data", []):
        if i_net >= len(row) or i_code >= len(row):
            continue   # 備註/合計等短列，跳過
        code = str(row[i_code]).strip()
        net = parse_num(row[i_net])
        if is_tradeable_code(code) and net is not None:
            out[code] = round(net / 1000)   # 股 → 張
    return out


def fetch_twse_margin(iso_date):
    """指定日期上市個股融資融券餘額（張）。{code: {mgn, sht, mgn0, sht0}}；拿不到回 {}。
    個股表欄位名稱重複（融資/融券群組各有「今日餘額」），故以出現順序定位。"""
    data = safe_get_json(
        "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN",
        {"date": iso_to_ymd(iso_date), "selectType": "ALL", "response": "json"},
        headers={"Connection": "close"})
    if not data or data.get("stat") != "OK":
        return {}
    table = None
    for t in data.get("tables", []):
        f = [str(x).strip() for x in (t.get("fields") or [])]
        if any(x.startswith("代號") for x in f) and f.count("今日餘額") >= 2:
            table = t
            table["_f"] = f
            break
    if not table:
        return {}
    f = table["_f"]
    i_code = next(i for i, x in enumerate(f) if x.startswith("代號"))
    today = [i for i, x in enumerate(f) if x == "今日餘額"]      # [融資, 融券]
    prev = [i for i, x in enumerate(f) if x == "前日餘額"]       # [融資, 融券]
    if len(today) < 2 or len(prev) < 2:
        return {}
    out = {}
    need = max(i_code, today[0], today[1], prev[0], prev[1])
    for row in table.get("data", []):
        if need >= len(row):
            continue   # 短列（備註/合計）跳過
        code = str(row[i_code]).strip()
        if not is_tradeable_code(code):
            continue
        mgn, sht = parse_num(row[today[0]]), parse_num(row[today[1]])
        mgn0, sht0 = parse_num(row[prev[0]]), parse_num(row[prev[1]])
        if mgn is None and sht is None:
            continue
        out[code] = {"mgn": int(mgn or 0), "sht": int(sht or 0),
                     "mgn0": int(mgn0 or 0), "sht0": int(sht0 or 0)}
    return out


def fetch_tpex_institutional(iso_date):
    """上櫃個股三大法人合計買賣超（張）；openapi 僅最新日，日期不符回 {}。
    tpex_3insti_daily_trading 的 TotalDifference 單位為股，÷1000 轉張。"""
    data = safe_get_json("https://www.tpex.org.tw/openapi/v1/tpex_3insti_daily_trading")
    out = {}
    for r in data or []:
        d = str(r.get("Date", "")).strip()
        if d and roc_to_iso(d) != iso_date:
            return {}
        code = str(r.get("SecuritiesCompanyCode", "")).strip()
        net = parse_num(r.get("TotalDifference"))
        if is_tradeable_code(code) and net is not None:
            out[code] = round(net / 1000)   # 股 → 張
    return out


def fetch_tpex_margin(iso_date):
    """上櫃個股融資融券餘額（張）；openapi 僅最新日，日期不符回 {}。
    注意：TPEx 融資融券資料常較三大法人落後一個交易日，故各自獨立判斷日期。"""
    data = safe_get_json("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_margin_balance")
    out = {}
    for r in data or []:
        d = str(r.get("Date", "")).strip()
        if d and roc_to_iso(d) != iso_date:
            return {}
        code = str(r.get("SecuritiesCompanyCode", "")).strip()
        mgn = parse_num(r.get("MarginPurchaseBalance"))
        sht = parse_num(r.get("ShortSaleBalance"))
        mgn0 = parse_num(r.get("MarginPurchaseBalancePreviousDay"))
        sht0 = parse_num(r.get("ShortSaleBalancePreviousDay"))
        if is_tradeable_code(code) and (mgn is not None or sht is not None):
            out[code] = {"mgn": int(mgn or 0), "sht": int(sht or 0),
                         "mgn0": int(mgn0 or 0), "sht0": int(sht0 or 0)}
    return out


def attach_chip(stocks, inst, margin):
    """把三大法人淨買超與融資融券餘額併入個股 dict（就地）。"""
    for code, net in (inst or {}).items():
        if code in stocks:
            stocks[code]["inst"] = net
    for code, mg in (margin or {}).items():
        if code in stocks:
            stocks[code].update(mg)


# ---------- TPEx ----------

def fetch_tpex_daily(iso_date):
    """指定日期的上櫃全部個股行情（含發行股數）。非交易日回傳 {}。"""
    data = safe_get_json(
        "https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes",
        {"date": iso_to_roc_slash(iso_date), "type": "EW", "response": "json"})
    if not data:
        return {}
    table = None
    for t in data.get("tables", []):
        f = [str(x).strip() for x in (t.get("fields") or [])]
        if any(x.startswith("代號") for x in f) and any(x.startswith("收盤") for x in f):
            table = t
            table["_fields"] = f
            break
    if not table:
        return {}
    f = table["_fields"]

    def find(*prefixes):
        for i, name in enumerate(f):
            clean = re.sub(r"<[^>]+>|\s", "", name)
            if any(clean.startswith(p) for p in prefixes):
                return i
        return None

    i_code, i_name = find("代號"), find("名稱")
    i_c, i_o, i_h, i_l = find("收盤"), find("開盤"), find("最高"), find("最低")
    i_v, i_a, i_t = find("成交股數"), find("成交金額"), find("成交筆數")
    i_sh = find("發行股數")
    stocks = {}
    for row in table.get("data", []):
        code = str(row[i_code]).strip()
        if not is_tradeable_code(code):
            continue
        o, h, l, c = (parse_num(row[i]) if i is not None else None for i in (i_o, i_h, i_l, i_c))
        v = parse_num(row[i_v]) if i_v is not None else None
        if None in (o, h, l, c) or not v:
            continue
        stocks[code] = {"n": str(row[i_name]).strip(), "m": "tpex",
                        "o": o, "h": h, "l": l, "c": c,
                        "v": int(v),
                        "a": int(parse_num(row[i_a]) or 0) if i_a is not None else 0,
                        "t": int(parse_num(row[i_t]) or 0) if i_t is not None else 0,
                        "sh": int(parse_num(row[i_sh]) or 0) if i_sh is not None else None}
    return stocks


# ---------- Snapshot 組裝 ----------

def snapshot_path(iso_date):
    return HISTORY_DIR / f"{iso_date}.json"


def save_snapshot(snap):
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    p = snapshot_path(snap["date"])
    p.write_text(json.dumps(snap, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return p


def load_snapshots(limit=None):
    """依日期排序載入全部（或最後 limit 個）歷史快照。"""
    files = sorted(HISTORY_DIR.glob("????-??-??.json"))
    if limit:
        files = files[-limit:]
    return [json.loads(p.read_text(encoding="utf-8")) for p in files]


def build_snapshot_for_date(iso_date, with_extras=False):
    """回補模式：抓指定歷史日期的 TWSE+TPEx 行情與當沖統計。

    先以 TPEx（無限流）判斷是否為交易日，避免對 TWSE 的休市日查詢
    觸發軟性限流重試。TPEx 確認是交易日但 TWSE 仍拿不到 → 拋錯（避免留下缺日）。
    """
    tpex = fetch_tpex_daily(iso_date)
    if not tpex:
        return None  # 非交易日
    twse = fetch_twse_mi_index(iso_date)
    if not twse:
        raise RuntimeError(f"TWSE MI_INDEX {iso_date} 在確認為交易日的情況下仍無資料（疑似限流），請稍後續跑")
    dt = fetch_twse_daytrade_stats(iso_date)
    stocks = {**tpex, **twse}
    for code, sh in (dt or {}).items():
        if code in stocks:
            stocks[code]["dt"] = sh
    # 籌碼面：上市個股三大法人買賣超與融資融券（可帶歷史日期）。
    # 上櫃個股籌碼 openapi 僅有最新日，歷史回補時不套用（缺 = 候選策略當日不觸發）。
    attach_chip(stocks, fetch_twse_institutional(iso_date), fetch_twse_margin(iso_date))
    snap = {"date": iso_date, "stocks": stocks}
    if with_extras:
        _attach_extras(snap)
    return snap


def build_latest_snapshot(max_lookback=6):
    """每日更新模式：從「台北今日」往回探，找到最近一個有行情的交易日並組出快照。

    以 date-specific 的 MI_INDEX + TPEx dailyQuotes 為準（可靠、當日 15:00 即發布），
    而非 STOCK_DAY_ALL——後者常拖到深夜才更新、且無法指定日期。

    往回探是關鍵：GitHub 排程可能延遲數小時、跨過午夜才觸發，此時「台北今日」
    已是隔天（尚未開盤、無資料）。逐日回退能在這種情況下正確抓到真正的最新交易日，
    週末、國定假日也自然跳過。已入庫且完整的交易日 → 回傳既有快照（交由呼叫端判斷略過）。
    """
    today = datetime.now(TAIPEI).date()
    for back in range(max_lookback):
        d = today - timedelta(days=back)
        if d.weekday() >= 5:      # 週六、週日直接跳過，省一次無謂查詢
            continue
        iso = d.isoformat()
        p = snapshot_path(iso)
        if p.exists():
            existing = json.loads(p.read_text(encoding="utf-8"))
            if "punish" in existing:          # 已有完整快照 → 這就是最新交易日，不需重抓
                print(f"[fetch] 最新交易日 {iso} 快照已完整")
                return existing
        try:
            snap = build_snapshot_for_date(iso, with_extras=True)
        except RuntimeError as e:
            print(f"[warn] {iso} 疑似限流：{e}")
            continue
        if snap:
            print(f"[fetch] 探得最新交易日 {iso}（回退 {back} 天）")
            return snap
        # snap is None → 該日 TPEx 無資料（非交易日），繼續往前一天探
    print(f"[warn] 回退 {max_lookback} 天仍無交易日資料")
    return None


def _attach_extras(snap):
    iso = snap["date"]
    ok_t, sus_t = fetch_twse_daytrade_eligible()
    ok_o, sus_o = fetch_tpex_daytrade_eligible()
    shares = fetch_twse_shares_issued()
    for code, sh in (shares or {}).items():
        if code in snap["stocks"] and not snap["stocks"][code].get("sh"):
            snap["stocks"][code]["sh"] = sh
    snap["dt_ok"] = sorted((ok_t | ok_o) & set(snap["stocks"]))
    snap["dt_sus"] = sorted((sus_t | sus_o) & set(snap["stocks"]))
    snap["punish"] = sorted(fetch_punish(iso) & set(snap["stocks"]))
    snap["notice"] = sorted(fetch_notice(iso) & set(snap["stocks"]))
    # 籌碼面：最新日 TWSE 帶日期版 + TPEx openapi（僅最新日，日期相符才套用）
    attach_chip(snap["stocks"], fetch_twse_institutional(iso), fetch_twse_margin(iso))
    attach_chip(snap["stocks"], fetch_tpex_institutional(iso), fetch_tpex_margin(iso))
