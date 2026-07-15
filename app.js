/* 台股當沖選股工具 — 前端邏輯
 * 資料由 GitHub Actions 每個交易日更新（data/ 下的靜態 JSON），
 * 手續費折數等個人設定存於 localStorage，復盤損益於前端即時重算。 */
"use strict";

const $ = (s) => document.querySelector(s);
const fmt = (n) => (n == null ? "–" : Number(n).toLocaleString("zh-TW"));
const fmt2 = (n) => (n == null ? "–" : Number(n).toLocaleString("zh-TW", { maximumFractionDigits: 2 }));
const signCls = (n) => (n > 0 ? "up" : n < 0 ? "down" : "flat");
const signTxt = (n) => (n > 0 ? "+" : "");

/* ---------- 個人設定 ---------- */
const DEFAULT_SETTINGS = { discount: 2.8, minFee: 20, lots: 1 };
let settings = { ...DEFAULT_SETTINGS, ...JSON.parse(localStorage.getItem("sp_settings") || "{}") };

/* ---------- 費用模型（與後端 review.py 一致） ---------- */
const FEE_RATE = 0.001425, DT_TAX = 0.0015;
function tradeNet(buy, sell, lots) {
  const sh = lots * 1000;
  const disc = settings.discount / 10;
  const feeB = Math.max(Math.floor(buy * sh * FEE_RATE * disc), settings.minFee);
  const feeS = Math.max(Math.floor(sell * sh * FEE_RATE * disc), settings.minFee);
  const tax = Math.floor(sell * sh * DT_TAX);
  const gross = Math.trunc((sell - buy) * sh);
  return { gross, fees: feeB + feeS + tax, net: gross - feeB - feeS - tax };
}
// side-aware：做多＝買 fill 賣 exit；做空＝賣 fill 買 exit（先賣後買，稅課在賣出=fill）
function sideNet(side, fill, exit, lots) {
  return side === "short" ? tradeNet(exit, fill, lots) : tradeNet(fill, exit, lots);
}

/* ---------- 資料載入 ---------- */
let DB = { picks: null, market: null, reviews: null, strategies: null, priceModel: null, config: null };
let stratMeta = {}; // id -> {name, desc, candidate}

async function loadJSON(path) {
  const r = await fetch(`${path}?t=${Date.now()}`);
  if (!r.ok) throw new Error(`${path} ${r.status}`);
  return r.json();
}

async function boot() {
  try {
    const [picks, market, reviews, strategies] = await Promise.all([
      loadJSON("data/latest/picks.json"),
      loadJSON("data/latest/market.json"),
      loadJSON("data/reviews.json"),
      loadJSON("data/latest/strategies.json"),
    ]);
    const priceModel = await loadJSON("data/latest/price_model.json").catch(() => null); // 舊部署可能還沒有
    const config = await loadJSON("data/latest/config_snapshot.json").catch(() => null);   // 風控預設值
    DB = { picks, market, reviews, strategies, priceModel, config };
    (market.strategies || []).forEach((s) => (stratMeta[s.id] = s));
    Object.entries(strategies.meta || {}).forEach(([id, m]) => (stratMeta[id] = { id, ...m }));
    $("#dataDate").textContent = `資料日：${market.date} · 更新：${(market.updated_at || "").slice(0, 16).replace("T", " ")}`;
    renderPicks();
    initCustomForm();
    renderReview();
    renderPriceModel();
    renderStrategies();
  } catch (e) {
    $("#dataDate").textContent = "資料載入失敗";
    $("#picksInfo").innerHTML = `⚠️ 尚無資料或載入失敗（${e.message}）。首次部署請先執行資料回補 Workflow。`;
  }
}

/* ---------- 分頁 ---------- */
const pendingCharts = {}; // 圖表必須等分頁可見才建立（隱藏容器中 Chart.js 會得到 0 尺寸）
function chartWhenVisible(tab, draw) {
  if (document.querySelector(`#panel-${tab}`).classList.contains("active")) draw();
  else pendingCharts[tab] = draw;
}
document.querySelectorAll(".tab").forEach((btn) =>
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b === btn));
    document.querySelectorAll(".panel").forEach((p) => p.classList.toggle("active", p.id === `panel-${btn.dataset.tab}`));
    const tab = btn.dataset.tab;
    if (pendingCharts[tab]) { pendingCharts[tab](); delete pendingCharts[tab]; }
    window.scrollTo({ top: 0 });
  })
);

/* ---------- 共用：個股卡片 ---------- */
function riskBadgeHtml(p) {
  // 今日選股的事前風險標註：以建議張數計「觸停損最壞虧損」，比照復盤的風險閘門口徑
  const rk = riskParams();
  if (!rk.enabled || p.entry == null || p.stop == null || p.entry === p.stop) return "";
  const side = p.side || "long";
  const lots = sizeLots({ fill_price: p.entry, stop: p.stop, side }, rk);
  const worst = -sideNet(side, p.entry, p.stop, lots).net;
  const worst1 = lots === 1 ? worst : -sideNet(side, p.entry, p.stop, 1).net;
  if (worst1 > rk.daily_loss_limit)
    return `<div class="risk-line block">🛑 一張最壞 −${fmt(worst1)}，超過日限 −${fmt(rk.daily_loss_limit)}（風控會跳過此單）</div>`;
  if (worst > rk.daily_loss_limit * 0.5)
    return `<div class="risk-line warn">⚠️ 建議 ${lots} 張・最壞 −${fmt(worst)}（吃掉大半日限額度）</div>`;
  return `<div class="risk-line ok">🛡️ 建議 ${lots} 張・最壞 −${fmt(worst)}</div>`;
}
function sparkline(closes, w = 96, h = 30) {
  if (!closes || closes.length < 2) return "";
  const min = Math.min(...closes), max = Math.max(...closes), rng = max - min || 1;
  const pts = closes.map((c, i) =>
    `${(i / (closes.length - 1) * w).toFixed(1)},${(h - 2 - (c - min) / rng * (h - 4)).toFixed(1)}`).join(" ");
  const up = closes[closes.length - 1] >= closes[0];
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true"
    title="近 ${closes.length} 日收盤走勢"><polyline points="${pts}" fill="none"
    stroke="${up ? "var(--up)" : "var(--down)"}" stroke-width="1.5" stroke-linejoin="round"/></svg>`;
}
function stockCard(p, rank) {
  const tags = (p.strategies || [])
    .map((id) => `<span class="tag" title="${stratMeta[id]?.desc || ""}">${stratMeta[id]?.name || id}</span>`)
    .join("");
  const chg = p.chg_pct;
  const short = p.side === "short";
  const sideBadge = `<span class="badge ${short ? "sside" : "lside"}" title="${short ? "做空：放空掛高、回補在低（現股當沖先賣後買）" : "做多：掛低買進、停利在高"}">${short ? "🔻 做空" : "🔺 做多"}</span>`;
  const grid = short
    ? `<div><div class="lb">建議放空 NH</div><div class="v tp">${fmt2(p.entry)}</div></div>
       <div><div class="lb">回補停利 NL</div><div class="v buy">${fmt2(p.target)}</div></div>
       <div><div class="lb">停損 AH</div><div class="v ah">${fmt2(p.stop)}</div></div>
       <div><div class="lb">破底加碼 AL</div><div class="v sl">${fmt2(p.cdp_base?.al ?? p.target)}</div></div>`
    : `<div><div class="lb">建議買進 NL</div><div class="v buy">${fmt2(p.entry ?? p.cdp?.nl)}</div></div>
       <div><div class="lb">停利 NH</div><div class="v tp">${fmt2(p.target ?? p.cdp?.nh)}</div></div>
       <div><div class="lb">停損 AL</div><div class="v sl">${fmt2(p.stop ?? p.cdp?.al)}</div></div>
       <div><div class="lb">順勢突破 AH</div><div class="v ah">${fmt2(p.ah ?? p.cdp?.ah)}</div></div>`;
  return `<div class="card">
    <div class="head">
      ${rank ? `<span class="rank">#${rank}</span>` : ""}
      <span class="code">${p.code}</span><span class="name">${p.name}</span>
      <span class="mkt">${p.market === "tpex" ? "上櫃" : "上市"}</span>
      ${sideBadge}
      ${p.counter ? `<span class="badge ct" title="逆勢配額：與大盤環境主方向相反、以更嚴門檻（活躍命中 ≥3）選出的實驗性標的——環境判定可能錯、強勢股也可能逆勢走，信心自酌">逆勢</span>` : ""}
      ${p.fallback ? `<span class="badge fb" title="未達完整門檻（活躍策略命中不足），為湊滿觀察名單的遞補標的，信心較低">遞補</span>` : ""}
      <span class="score" title="綜合分數（策略權重加總）">${p.score}</span>
    </div>
    <div class="px">收盤 <b>${fmt2(p.close)}</b>
      <span class="${signCls(chg)}">${signTxt(chg)}${fmt2(chg)}%</span>${sparkline(p.spark)}</div>
    <div class="tags">${tags || '<span class="muted small">未觸發計分策略</span>'}</div>
    <div class="price-grid">${grid}</div>
    <div class="meta">
      <span>月均振幅 ${fmt2(p.amp_avg)}%</span>
      <span>成交 ${fmt(p.vol_lots)} 張</span>
      ${p.dt_ratio != null ? `<span>當沖率 ${fmt2(p.dt_ratio)}%</span>` : ""}
      ${p.breakeven_ticks != null ? `<span>回本約 ${p.breakeven_ticks} 檔</span>` : ""}
    </div>
    ${riskBadgeHtml(p)}
  </div>`;
}

/* ---------- Tab 1 今日選股 ---------- */
const shiftTxt = (v) => (v > 0 ? `+${v}` : `${v}`) + "R";
function renderPicks() {
  const d = DB.picks;
  const n = d.picks?.length || 0;
  const sh = d.price_shifts || {};
  const shifted = ["entry", "target", "stop"].some((k) => sh[k]);
  const exitMode = (sh.trail || sh.tstop != null)
    ? `、出場引擎：移動停利 ${sh.trail ? sh.trail + "R" : "關"}／時間停損 ${sh.tstop != null ? "12:00" : "關"}（A/B 實證擇優）` : "";
  const rk = riskParams();
  const rg = d.regime || DB.market?.breadth || null;
  const book = d.regime?.book || (d.picks?.[0]?.side) || "long";   // 今日建議單方向
  // 事前風險總覽：全下（依建議張數）觸停損的最壞合計（side-aware）
  let riskSummary = "";
  if (rk.enabled && n) {
    const worstSum = d.picks.reduce((s, p) => {
      if (p.entry == null || p.stop == null || p.entry === p.stop) return s;
      const lots = sizeLots({ fill_price: p.entry, stop: p.stop, side: p.side }, rk);
      return s + -sideNet(p.side, p.entry, p.stop, lots).net;
    }, 0);
    riskSummary = `<br>🛡️ 依建議張數全下、全部觸停損的最壞合計約 <b>−${fmt(worstSum)}</b>；你的日限 <b>−${fmt(rk.daily_loss_limit)}</b>——盤中實際請依風險閘門順序進場（超額即停，卡片有逐檔標註）。`;
  }
  const nFb = (d.picks || []).filter((p) => p.fallback).length;
  const nCt = (d.picks || []).filter((p) => p.counter).length;
  const nShort = (d.picks || []).filter((p) => p.side === "short").length;
  const nLong = n - nShort;
  // 大盤環境徽章：市場寬度（上漲家數比）5 日均，≥0.5 多方主做多、<0.5 空方主做空
  const regimeChip = rg && rg.breadth_ma != null
    ? `<span class="badge ${rg.bull ? "on" : "off"}" title="市場寬度＝全市場上漲家數比的 5 日均；≥50% 多方環境以做多為主、<50% 空方環境以做空為主，另保留少量更嚴門檻的逆勢配額">${rg.bull ? "🌤 多方環境 → 做多為主" : "⛈ 空方環境 → 做空為主"}｜寬度5MA ${(rg.breadth_ma * 100).toFixed(0)}%</span> ` : "";
  const mixTxt = n
    ? `本日名單：${nLong ? `🔺做多 <b>${nLong}</b> 檔` : ""}${nLong && nShort ? "＋" : ""}${nShort ? `🔻做空 <b class="down">${nShort}</b> 檔（現股當沖先賣後買）` : ""}${nCt
        ? `，其中 <b>${nCt}</b> 檔為<b>逆勢配額</b>（與大盤環境反向、門檻更嚴，實驗性質信心自酌）` : ""}` : "";
  const shS = sh.short || {};
  const priceLine = (shifted || exitMode || nShort)
    ? `<br>📐 價格模型（多空各自迭代）：${nLong ? `做多 進${shiftTxt(sh.entry ?? 0)}/停利${shiftTxt(sh.target ?? 0)}/停損${shiftTxt(sh.stop ?? 0)}` : ""}${nLong && nShort ? "；" : ""}${nShort ? `做空 進−${shS.entry ?? 0}R/回補−${shS.target ?? 0}R/停損−${shS.stop ?? 0}R` : ""}${exitMode}` : "";
  $("#picksInfo").innerHTML = n
    ? `${regimeChip}📅 <b>${d.generated_on}</b> 收盤後產生 · 適用<b>下一交易日</b>盤中 · 共 <b>${n}</b> 檔${nFb
        ? `（含遞補 ${nFb} 檔，訊號較弱）` : ""}${mixTxt ? `<br>${mixTxt}` : ""}
       <br>已排除處置股／注意股／非當沖標的／不可放空者（做空單）／1張風險超日限者／流動性與波動不足者。${priceLine}${riskSummary}`
    : `${regimeChip}本日${book === "short" ? "空方環境下亦" : ""}無符合門檻的標的（可能連續假期後資料待更新，或基礎濾網過嚴）。可到「自訂選股」自行研究。`;
  $("#picksList").innerHTML = (d.picks || []).map((p, i) => stockCard(p, i + 1)).join("") ||
    `<div class="empty">今日無推薦標的</div>`;

  const w = d.weights_used || {};
  $("#strategyExplain").innerHTML = Object.values(stratMeta)
    .map((s) => `<div class="s-item"><b>${s.name}</b>
      <span class="muted small">（目前權重 ${w[s.id] ?? 1}${w[s.id] === 0 ? "，已停用" : ""}）</span>
      <p>${s.desc}</p></div>`).join("");
}

/* ---------- Tab 2 自訂選股 ---------- */
const FILTER_KEY = "sp_filter";
function initCustomForm() {
  $("#stratChecks").innerHTML = Object.values(stratMeta)
    .map((s) => `<label class="chk" title="${s.desc}"><input type="checkbox" value="${s.id}" checked>${s.name}</label>`)
    .join("");
  const saved = JSON.parse(localStorage.getItem(FILTER_KEY) || "null");
  if (saved) applyFilterValues(saved);
  $("#btnScreen").addEventListener("click", runCustomScreen);
  $("#btnSaveFilter").addEventListener("click", () => {
    localStorage.setItem(FILTER_KEY, JSON.stringify(readFilterValues()));
    $("#customInfo").innerHTML = "💾 已儲存為此裝置的預設條件（下次開啟自動套用）";
  });
  $("#btnResetFilter").addEventListener("click", () => {
    localStorage.removeItem(FILTER_KEY);
    applyFilterValues({ priceMin: 10, priceMax: 1000, volWin: "5", volMin: 2000, ampMin: 2.5, dtMin: 0,
      twse: true, tpex: true, excl: true, dtOk: true, minHits: "1", strategies: Object.keys(stratMeta) });
    runCustomScreen();
  });
  runCustomScreen();
}
function readFilterValues() {
  return {
    priceMin: +$("#fPriceMin").value || 0, priceMax: +$("#fPriceMax").value || 99999,
    volWin: $("#fVolWin").value, volMin: +$("#fVolMin").value || 0,
    ampMin: +$("#fAmpMin").value || 0, dtMin: +$("#fDtMin").value || 0,
    twse: $("#fTwse").checked, tpex: $("#fTpex").checked,
    excl: $("#fExcl").checked, dtOk: $("#fDtOk").checked,
    minHits: $("#fMinHits").value,
    strategies: [...document.querySelectorAll("#stratChecks input:checked")].map((i) => i.value),
  };
}
function applyFilterValues(f) {
  $("#fPriceMin").value = f.priceMin; $("#fPriceMax").value = f.priceMax;
  $("#fVolWin").value = f.volWin; $("#fVolMin").value = f.volMin;
  $("#fAmpMin").value = f.ampMin; $("#fDtMin").value = f.dtMin;
  $("#fTwse").checked = f.twse; $("#fTpex").checked = f.tpex;
  $("#fExcl").checked = f.excl; $("#fDtOk").checked = f.dtOk;
  $("#fMinHits").value = f.minHits;
  document.querySelectorAll("#stratChecks input").forEach((i) => (i.checked = f.strategies.includes(i.value)));
}
function runCustomScreen() {
  const f = readFilterValues();
  const rows = DB.market.stocks.filter((m) => {
    if (m.close < f.priceMin || m.close > f.priceMax) return false;
    const vol = f.volWin === "3" ? m.vol_ma3_incl_lots : m.vol_ma5_incl_lots;
    if ((vol || 0) < f.volMin) return false;
    if ((m.amp_avg || 0) < f.ampMin) return false;
    if (f.dtMin > 0 && !(m.dt_ratio >= f.dtMin)) return false;
    if (!f.twse && m.market === "twse") return false;
    if (!f.tpex && m.market === "tpex") return false;
    if (f.excl && (m.flags?.punish || m.flags?.notice)) return false;
    if (f.dtOk && m.flags?.dt_ok === false) return false;
    const hits = (m.strategies || []).filter((id) => f.strategies.includes(id));
    m._hits = hits;
    m._score = m.score || 0;
    return hits.length >= +f.minHits;
  });
  rows.sort((a, b) => b._score - a._score || b.amp_avg - a.amp_avg);
  const top = rows.slice(0, 30);
  $("#customInfo").innerHTML = `符合條件 <b>${rows.length}</b> 檔${rows.length > 30 ? "，顯示綜合分數前 30 檔" : ""} ·
    條件：${f.priceMin}–${f.priceMax} 元、近${f.volWin}日均量 ≥ ${fmt(f.volMin)} 張、月均振幅 ≥ ${f.ampMin}%${f.dtMin ? `、當沖率 ≥ ${f.dtMin}%` : ""}`;
  $("#customList").innerHTML = top.map((m) =>
    stockCard({ ...m, strategies: m._hits, score: m._score }, null)).join("") ||
    `<div class="empty">沒有符合條件的標的，試著放寬條件</div>`;
}

/* ---------- Tab 3 每日復盤（含每日虧損風控層）---------- */
let pnlChart;
const RISK_DEFAULTS = { enabled: true, daily_loss_limit: 10000, max_consecutive_losses: 3,
  position_sizing: "risk_parity", per_trade_risk: 2500, max_lots: 4, lots: 1 };
function riskParams() {
  return { ...RISK_DEFAULTS, ...(DB.config?.risk || {}), ...(settings.risk || {}) };
}
function sizeLots(p, rk) {
  // 部位風險預算：每筆風險 ≈ per_trade_risk，張數 = 預算 / |進場−停損| /1000，夾在 [1, max_lots]
  const dist = Math.abs((p.fill_price ?? p.entry) - p.stop);   // 做多停損在下、做空在上，取絕對距離
  if (rk.position_sizing === "risk_parity" && dist > 0) {
    return Math.max(1, Math.min(rk.max_lots, Math.round(rk.per_trade_risk / (dist * 1000))));
  }
  return Math.max(1, Math.round(rk.lots || settings.lots || 1));
}
/* 同時算 raw（每筆固定張數全開）與 managed（部位風險預算＋每日虧損斷路器）。
   斷路器：依綜合分數（信心）順序進場，當日累計實現虧損 ≤ −上限、或連虧 N 筆 → 停開新倉。 */
function recomputeDay(day, limitOverride) {
  const rk = riskParams();
  const limit = limitOverride != null ? limitOverride : rk.daily_loss_limit;
  const lots0 = settings.lots;
  let rawNet = 0, rawWins = 0, filled = 0;
  const rawByCode = {};
  day.picks.forEach((p) => {
    if (!p.filled) return;
    const r = sideNet(p.side, p.fill_price, p.exit_price, lots0);
    rawNet += r.net; filled++; if (r.net > 0) rawWins++;
    rawByCode[p.code] = r.net;
  });
  const order = day.picks.filter((p) => p.filled).slice()
    .sort((a, b) => (b.score || 0) - (a.score || 0));
  let mNet = 0, taken = 0, skipped = 0, mWins = 0, consec = 0, halted = false;
  const ann = {};
  order.forEach((p) => {
    const lots = rk.enabled ? sizeLots(p, rk) : lots0;
    const r = sideNet(p.side, p.fill_price, p.exit_price, lots);
    if (rk.enabled) {
      if (halted) { skipped++; ann[p.code] = { taken: false, lots, reason: "consec" }; return; }
      // 進場前風險閘門：這筆觸停損的最大淨虧損必須 ≤ 當日剩餘額度，否則不進場。
      // 因實際出場價不優於停損價，故此規則在數學上保證單日淨虧損永不超過上限。
      const worstLoss = -sideNet(p.side, p.fill_price, p.stop, lots).net;   // 觸停損的最大虧損（含費用，正值）
      const remaining = limit + mNet;                                // 剩餘可承受虧損（limit 可為 Infinity）
      if (worstLoss > remaining) { skipped++; ann[p.code] = { taken: false, lots, reason: "budget" }; return; }
    }
    mNet += r.net; taken++;
    if (r.net > 0) { mWins++; consec = 0; }
    else { consec++; if (consec >= rk.max_consecutive_losses) halted = true; }
    ann[p.code] = { taken: true, lots, net: r.net };
  });
  const rows = day.picks.map((p) => ({ ...p, _net: p.filled ? rawByCode[p.code] : null, _m: ann[p.code] }));
  return { rows, net: mNet, rawNet, filled, wins: mWins, taken, skipped, halted,
    winRate: taken ? (mWins / taken) * 100 : null,
    rawWinRate: filled ? (rawWins / filled) * 100 : null };
}

/* 敏感度：在指定每日上限下，跑完整個復盤期並匯總（累計損益、勝率、最慘單日、最大回撤、報酬÷回撤）。
   其餘風控參數（部位大小、連虧停手）沿用目前設定，只變動「每日上限」以隔離其效果。 */
function aggregateAtLimit(days, limit) {
  let cum = 0, peak = 0, maxDD = 0, taken = 0, wins = 0, worst = 0;
  days.forEach((d) => {
    const r = recomputeDay(d, limit);
    cum += r.net;
    peak = Math.max(peak, cum);
    maxDD = Math.max(maxDD, peak - cum);
    taken += r.taken; wins += r.wins;
    worst = Math.min(worst, r.net);
  });
  const score = maxDD > 0 ? cum / maxDD : (cum > 0 ? Infinity : 0);   // Calmar 式：報酬÷最大回撤
  return { limit, net: cum, winRate: taken ? (wins / taken) * 100 : null, worst, maxDD, score };
}
function renderRiskSensitivity(days) {
  const host = $("#riskSensitivity");
  const rk = riskParams();
  if (!rk.enabled || !days.length) { host.innerHTML = ""; return; }
  const limits = [10000, 15000, 20000, 30000, Infinity];
  const rows = limits.map((L) => aggregateAtLimit(days, L));
  // 風險調整最佳只在「有上限」的組合中挑（不推薦「無上限」＝等於拿掉風控）
  const capped = rows.filter((r) => isFinite(r.limit));
  const best = capped.reduce((a, b) => (b.score > a.score ? b : a));
  const fmtL = (L) => (isFinite(L) ? "$" + fmt(L) : "無上限");
  const scoreTxt = (s) => (isFinite(s) ? s.toFixed(2) : "∞");
  const trs = rows.map((r) => {
    const cls = [r.limit === best.limit ? "best" : "", r.limit === rk.daily_loss_limit ? "cur" : ""].join(" ");
    return `<tr class="${cls}">
      <td>${fmtL(r.limit)}${r.limit === rk.daily_loss_limit ? " ◀目前" : ""}${r.limit === best.limit ? " ⭐" : ""}</td>
      <td class="${signCls(r.net)}">${signTxt(r.net)}${fmt(r.net)}</td>
      <td>${r.winRate == null ? "–" : r.winRate.toFixed(0) + "%"}</td>
      <td class="down">${fmt(r.worst)}</td>
      <td class="down">-${fmt(r.maxDD)}</td>
      <td><b>${scoreTxt(r.score)}</b></td>
    </tr>`;
  }).join("");
  const canApply = best.limit !== rk.daily_loss_limit;
  host.innerHTML = `<details class="explain">
    <summary>📊 每日上限敏感度分析（風險調整後最佳：<b>${fmtL(best.limit)}</b>）</summary>
    <div class="tbl-wrap"><table class="trades sens">
      <tr><th>每日虧損上限</th><th>累計損益</th><th>勝率</th><th>最慘單日</th><th>最大回撤</th><th>報酬÷回撤</th></tr>
      ${trs}</table></div>
    <p class="muted small">「報酬÷回撤」＝累計淨損益 ÷ 最大回撤（Calmar 式，越高越好）。純看累計損益幾乎必然「上限越高越賺」
    ——因為放行更多正期望值的單。⭐ 為<b>有上限</b>組合中風險調整最佳者（刻意不推薦「無上限」＝等於拿掉風控）。
    ${canApply ? `<button class="btn" id="btnApplyBestLimit" style="margin-top:8px">一鍵套用 ${fmtL(best.limit)} 為每日上限</button>` : "（⭐ 已是你目前的設定）"}</p>
    <p class="muted small">⚠️ <b>這正是「別把每日上限交給優化器」的活教材</b>：目前僅 ${days.length} 天樣本，少數幾筆尾部大賺（被低上限跳過的贏家）
    會主導結果，常讓「無上限」連風險調整比都偏高——但那不代表真的該拿掉風控，只代表樣本太少、不可盡信。每日上限是你能承受多少單日虧損的<b>風險偏好</b>，
    此表供你看清取捨，數字由你拍板；待復盤天數累積夠多，這裡的結論才會穩定。</p>
  </details>`;
  const btn = $("#btnApplyBestLimit");
  if (btn) btn.addEventListener("click", () => {
    settings.risk = { ...rk, daily_loss_limit: best.limit };
    localStorage.setItem("sp_settings", JSON.stringify(settings));
    renderReview();
  });
}
function renderReview() {
  const days = [...DB.reviews].sort((a, b) => a.date.localeCompare(b.date));
  if (!days.length) {
    $("#reviewStats").innerHTML = "";
    $("#reviewList").innerHTML = `<div class="empty">尚無復盤紀錄。系統每天收盤後會自動用實際行情驗證前一日的建議單。</div>`;
    return;
  }
  const rk = riskParams();
  const daily = days.map((d) => ({ date: d.date, ...recomputeDay(d), raw: d }));
  let cumM = 0, cumR = 0;
  const cumManaged = [], cumRaw = [];
  daily.forEach((d) => { cumManaged.push(cumM += d.net); cumRaw.push(cumR += d.rawNet); });
  const totNet = cumM, totRaw = cumR;
  const totTaken = daily.reduce((s, d) => s + d.taken, 0);
  const totWins = daily.reduce((s, d) => s + d.wins, 0);
  const haltDays = daily.filter((d) => d.halted).length;
  const worstM = Math.min(...daily.map((d) => d.net));
  const rawBreach = daily.filter((d) => d.rawNet < -rk.daily_loss_limit).length;
  const mBreach = daily.filter((d) => d.net < -rk.daily_loss_limit).length;

  // 大賺小賠儀表板：以風控後實際進場的每筆淨損益計
  const mNets = [];
  const reasonAgg = {};   // 出場原因分佈（風控後實際進場的單）
  daily.forEach((d) => d.rows.forEach((p) => {
    if (!p._m?.taken) return;
    mNets.push(p._m.net);
    const r = reasonAgg[p.exit_reason] || (reasonAgg[p.exit_reason] = { n: 0, net: 0 });
    r.n++; r.net += p._m.net;
  }));
  const mW = mNets.filter((x) => x > 0), mL = mNets.filter((x) => x <= 0);
  const avgW = mW.length ? mW.reduce((a, b) => a + b, 0) / mW.length : null;
  const avgL = mL.length ? -mL.reduce((a, b) => a + b, 0) / mL.length : null;
  const payoff = avgW != null && avgL ? avgW / avgL : null;
  const pf = mL.length ? mW.reduce((a, b) => a + b, 0) / -mL.reduce((a, b) => a + b, 0) : null;
  const expectPer = mNets.length ? Math.round(mNets.reduce((a, b) => a + b, 0) / mNets.length) : null;
  let ddPeak = 0, mdd = 0;   // 最大回撤（風控後累計曲線）
  cumManaged.forEach((c) => { ddPeak = Math.max(ddPeak, c); mdd = Math.min(mdd, c - ddPeak); });

  $("#reviewStats").innerHTML = `
    <div class="stat"><div class="lb">累計淨損益<span class="muted">（風控後）</span></div><div class="v ${signCls(totNet)}">${signTxt(totNet)}${fmt(totNet)}</div></div>
    <div class="stat"><div class="lb">總勝率<span class="muted">（風控後）</span></div><div class="v">${totTaken ? ((totWins / totTaken) * 100).toFixed(1) : "–"}%</div></div>
    <div class="stat"><div class="lb">賺賠比<span class="muted">（均賺/均賠）</span></div><div class="v ${payoff != null ? (payoff >= 1.2 ? "up" : payoff < 1 ? "down" : "") : ""}">${payoff != null ? payoff.toFixed(2) : "–"}</div></div>
    <div class="stat"><div class="lb">獲利因子<span class="muted">（總賺/總賠）</span></div><div class="v ${pf != null ? (pf >= 1 ? "up" : "down") : ""}">${pf != null ? pf.toFixed(2) : "–"}</div></div>
    <div class="stat"><div class="lb">每筆期望值</div><div class="v ${signCls(expectPer)}">${expectPer != null ? signTxt(expectPer) + fmt(expectPer) : "–"}</div></div>
    <div class="stat"><div class="lb">最大回撤<span class="muted">（風控後）</span></div><div class="v ${mdd < 0 ? "down" : ""}">${fmt(mdd)}</div></div>
    <div class="stat"><div class="lb">最慘單日<span class="muted">（風控後）</span></div><div class="v ${signCls(worstM)}">${signTxt(worstM)}${fmt(worstM)}</div></div>
    <div class="stat"><div class="lb">觸發斷路器</div><div class="v">${haltDays} 天</div></div>
    <div class="stat"><div class="lb">5分K核實</div><div class="v">${(() => { const a = daily.reduce((s, d) => s + (d.raw.summary?.n_intraday || 0), 0), b = daily.reduce((s, d) => s + d.raw.summary.n_picks, 0); return b ? Math.round(a / b * 100) + "%" : "–"; })()}</div></div>
    <div class="stat"><div class="lb">復盤天數</div><div class="v">${daily.length}</div></div>`;

  // 出場原因分佈條：寬度=筆數占比、顏色=該原因總損益方向（一眼看出錢從哪賺、往哪虧）
  const reasonLabel = { target: "停利", trail: "移動停利", stop: "停損", timeout: "時間停損", close: "收盤沖銷" };
  const totalTakenN = Object.values(reasonAgg).reduce((s, r) => s + r.n, 0);
  $("#exitDist").innerHTML = totalTakenN ? `<div class="dist-title muted small">出場原因分佈（風控後 ${totalTakenN} 筆・寬度=筆數占比・紅=該類合計為賺、綠=賠）</div>
    <div class="dist-bar">${Object.entries(reasonAgg).sort((a, b) => b[1].n - a[1].n).map(([k, r]) =>
      `<i class="${r.net >= 0 ? "gain" : "loss"}" style="flex:${r.n}" title="${reasonLabel[k] || k}：${r.n} 筆、合計 ${signTxt(r.net)}${fmt(r.net)}"></i>`).join("")}</div>
    <div class="dist-legend small">${Object.entries(reasonAgg).sort((a, b) => b[1].n - a[1].n).map(([k, r]) =>
      `<span><i class="${r.net >= 0 ? "gain" : "loss"}"></i>${reasonLabel[k] || k} ${Math.round(r.n / totalTakenN * 100)}%・${signTxt(r.net)}${fmt(r.net)}</span>`).join("")}</div>` : "";

  $("#riskNote").innerHTML = rk.enabled
    ? `🛡️ 已套用每日虧損風控：部位<b>${rk.position_sizing === "risk_parity" ? `風險預算 ${fmt(rk.per_trade_risk)} 元/筆` : `固定 ${rk.lots} 張`}</b>、
       單日累計虧損觸及 <b>−${fmt(rk.daily_loss_limit)}</b> 或連虧 <b>${rk.max_consecutive_losses}</b> 筆即停開新倉。
       單日虧損破 ${fmt(rk.daily_loss_limit)} 的天數：<b class="down">原始 ${rawBreach} 天 → 風控後 ${mBreach} 天</b>；
       未套風控的累計損益為 ${signTxt(totRaw)}${fmt(totRaw)}（每筆固定 ${settings.lots} 張）。可在 ⚙️ 設定調整。`
    : `⚠️ 每日虧損風控已關閉，顯示為每筆固定 ${settings.lots} 張全開的原始結果。可在 ⚙️ 設定開啟。`;

  renderRiskSensitivity(days);

  const css = getComputedStyle(document.documentElement);
  const cUp = css.getPropertyValue("--up").trim(), cDown = css.getPropertyValue("--down").trim();
  chartWhenVisible("review", () => {
  if (pnlChart) pnlChart.destroy();
  pnlChart = new Chart($("#pnlChart"), {
    data: {
      labels: daily.map((d) => d.date.slice(5)),
      datasets: [
        { type: "line", label: "累計（風控後）", data: cumManaged, borderColor: css.getPropertyValue("--gold").trim(),
          backgroundColor: "transparent", tension: 0.25, pointRadius: 0, borderWidth: 2, yAxisID: "y1" },
        { type: "line", label: "累計（未風控）", data: cumRaw, borderColor: "#5b6675",
          borderDash: [4, 4], backgroundColor: "transparent", tension: 0.25, pointRadius: 0, borderWidth: 1.5, yAxisID: "y1" },
        { type: "bar", label: "單日（風控後）", data: daily.map((d) => d.net),
          backgroundColor: daily.map((d) => (d.net >= 0 ? cUp + "cc" : cDown + "cc")), yAxisID: "y" },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: { legend: { labels: { color: "#93a4b8", boxWidth: 12, font: { size: 11 } } } },
      scales: {
        x: { ticks: { color: "#5b6675", font: { size: 10 }, maxTicksLimit: 10 }, grid: { color: "#1c2330" } },
        y: { ticks: { color: "#5b6675", font: { size: 10 } }, grid: { color: "#1c2330" } },
        y1: { position: "right", ticks: { color: "#8a7040", font: { size: 10 } }, grid: { display: false } },
      },
    },
  });
  });

  $("#reviewList").innerHTML = [...daily].reverse().map((d, idx) => {
    const reasonTxt = { target: "停利", trail: "移動停利", stop: "停損", timeout: "時間停損", close: "收盤沖銷", nofill: "未成交" };
    const rows = d.rows.map((p) => {
      const sm = p.side === "short" ? '<span class="down" title="做空">🔻</span> ' : "";
      const noFillTxt = p.side === "short" ? `未成交（最高 ${fmt2(p.day_high)} 未觸賣價）` : `未成交（最低 ${fmt2(p.day_low)} 未觸價）`;
      if (!p.filled) return `<tr class="dim"><td>${sm}${p.code} ${p.name}</td><td>${fmt2(p.entry)}</td>
        <td colspan="3">${noFillTxt}</td><td>–</td></tr>`;
      const m = p._m || {};
      if (!m.taken) {
        const why = m.reason === "consec" ? "🛑 連虧停手" : "🛑 超出當日風險額度";
        return `<tr class="dim"><td>${sm}${p.code} ${p.name}</td><td>${fmt2(p.fill_price)}</td>
        <td>${fmt2(p.exit_price)}</td><td colspan="2">${why}</td>
        <td class="muted">(未取 ${signTxt(p._net)}${fmt(p._net)})</td></tr>`;
      }
      return `<tr><td>${sm}${p.code} ${p.name}</td><td>${fmt2(p.fill_price)}</td>
        <td>${fmt2(p.exit_price)}</td><td>${reasonTxt[p.exit_reason] || p.exit_reason}</td>
        <td>${m.lots} 張</td>
        <td class="${signCls(m.net)}">${signTxt(m.net)}${fmt(m.net)}</td></tr>`;
    }).join("");
    const nIntra = d.raw.summary?.n_intraday;
    return `<details class="day-block" ${idx === 0 ? "open" : ""}>
      <summary><span class="d">${d.date}</span>
        <span class="s">${d.taken}/${d.filled} 筆進場${d.skipped ? `（斷路器跳過 ${d.skipped}）` : ""} · 勝率 ${d.winRate == null ? "–" : d.winRate.toFixed(0) + "%"}${nIntra ? ` · 5分K核實 ${nIntra}` : ""}</span>
        <span class="pnl ${signCls(d.net)}">${signTxt(d.net)}${fmt(d.net)}</span></summary>
      <div class="tbl-wrap"><table class="trades">
        <tr><th>標的</th><th>進場</th><th>出場</th><th>原因</th><th>張數</th><th>淨損益</th></tr>
        ${rows}</table></div>
    </details>`;
  }).join("");
}

/* ---------- 價格模型（每日復盤頁）---------- */
function renderPriceModel() {
  const pm = DB.priceModel;
  if (!pm || !pm.stats) return; // 尚未產生價格模型 → 區塊保持隱藏
  const s = pm.stats, sh = pm.shifts || {};
  $("#priceModelCard").hidden = false;
  $("#priceModelInfo").innerHTML =
    `進出場建議價＝CDP 基準價＋偏移 ×「訊號日振幅 R」，偏移與<b>出場引擎</b>（地板式移動停利、12:00 時間停損）
     每日依最近 <b>${pm.window_days}</b> 個復盤日重放 A/B 實證、績效明顯改善才切換（防止雜訊抖動）。
     目前：進場 <b>${shiftTxt(sh.entry ?? 0)}</b> · 停利 <b>${shiftTxt(sh.target ?? 0)}</b> ·
     停損 <b>${shiftTxt(sh.stop ?? 0)}</b> · 移動停利 <b>${sh.trail ? sh.trail + "R" : "關"}</b> ·
     時間停損 <b>${sh.tstop != null ? "12:00" : "關"}</b>
     ${s.net != null && s.net_baseline != null ? `｜窗口淨損益 <b>${fmt(s.net)}</b> vs 原始 CDP <b>${fmt(s.net_baseline)}</b> 元` : ""}`;
  const frCls = s.fill_target == null ? "" : (s.fill_rate ?? 0) >= s.fill_target ? "up" : "down";
  const tRate = (s.target_rate ?? 0) + (s.trail_rate ?? 0);
  $("#priceModelStats").innerHTML = `
    <div class="stat"><div class="lb">掛單成交率${s.fill_target != null ? `（目標 ≥${s.fill_target}%）` : ""}</div>
      <div class="v ${frCls}">${s.fill_rate ?? "–"}%</div></div>
    <div class="stat"><div class="lb">賺賠比（窗口）</div><div class="v ${s.payoff != null ? (s.payoff >= 1.2 ? "up" : s.payoff < 1 ? "down" : "") : ""}">${s.payoff ?? "–"}</div></div>
    <div class="stat"><div class="lb">停利出場占比${s.trail_rate ? "（含移動）" : ""}</div><div class="v up">${s.fill_rate != null ? tRate.toFixed(1) : "–"}%</div></div>
    <div class="stat"><div class="lb">停損出場占比</div><div class="v down">${s.stop_rate ?? "–"}%</div></div>
    ${s.timeout_rate ? `<div class="stat"><div class="lb">時間停損占比</div><div class="v">${s.timeout_rate}%</div></div>` : ""}
    <div class="stat"><div class="lb">掛價過低錯失率</div><div class="v">${s.runaway_rate ?? "–"}%</div></div>`;
  // 空方價格模型（獨立偏移與統計）
  const ss = pm.short_stats, shS = pm.short_shifts || {};
  if (ss && ss.n_picks) {
    const sfrCls = ss.fill_target == null ? "" : (ss.fill_rate ?? 0) >= ss.fill_target ? "up" : "down";
    $("#priceModelInfo").innerHTML += `<br>🔻 <b>空方（獨立迭代）</b>：放空 NH−<b>${shS.entry ?? 0}R</b> ·
      回補 NL−<b>${shS.target ?? 0}R</b> · 停損 AH−<b>${shS.stop ?? 0}R</b> · 移動停利 <b>${shS.trail ? shS.trail + "R" : "關"}</b> ·
      時間停損 <b>${shS.tstop != null ? "12:00" : "關"}</b>
      ｜窗口：成交率 <b class="${sfrCls}">${ss.fill_rate ?? "–"}%</b> · 賺賠比 <b>${ss.payoff ?? "–"}</b> ·
      淨損益 <b>${fmt(ss.net)}</b> vs 原始 CDP <b>${fmt(ss.net_baseline)}</b> 元`;
  }
  const log = pm.log || [];
  $("#priceModelLog").innerHTML = log.length
    ? [...log].reverse().slice(0, 10).map((l) => `<div class="log-item"><span class="d">${l.date}</span>${l.msg}</div>`).join("")
    : "尚無調整紀錄（樣本累積中，窗口成交滿門檻後開始搜尋）";
}

/* ---------- Tab 4 策略績效 ---------- */
let stratChart;
function renderStrategies() {
  const d = DB.strategies;
  const stats = d.stats || {};
  $("#stratInfo").innerHTML = `評估窗口：最近 <b>${d.window_days}</b> 個復盤日（累計 ${d.review_days} 日） ·
    每日收盤後自動重算：期望值轉負的策略停用（汰弱），績效好的策略加權（留強），直接影響隔日「今日選股」排序。
    <br>🧪 <b>候選策略池</b>：新策略以權重 0 虛擬追蹤（觸發照記、不計分），樣本足夠且期望值實證轉正才自動納入計分；轉負會退回觀察區。`;
  const list = Object.entries(stats)
    .map(([id, s]) => ({ id, name: stratMeta[id]?.name || id, desc: stratMeta[id]?.desc || "", ...s }))
    .sort((a, b) => (b.weight || 0) - (a.weight || 0) || (b.win_rate || 0) - (a.win_rate || 0));

  chartWhenVisible("strategy", () => {
    if (stratChart) stratChart.destroy();
    stratChart = new Chart($("#stratChart"), {
      type: "bar",
      data: {
        labels: list.map((s) => s.name),
        datasets: [{ label: "勝率 %", data: list.map((s) => s.win_rate ?? 0),
          backgroundColor: list.map((s) => (s.enabled ? "#f5c04ecc" : "#4a5468cc")) }],
      },
      options: {
        indexAxis: "y", responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { max: 100, ticks: { color: "#5b6675" }, grid: { color: "#1c2330" } },
          y: { ticks: { color: "#93a4b8", font: { size: 11 } }, grid: { display: false } },
        },
      },
    });
  });

  $("#stratList").innerHTML = list.map((s) => `<div class="card s-card">
    <div class="s-head"><span class="s-name">${s.name}</span>
      ${s.candidate ? `<span class="badge cand" title="候選策略：虛擬追蹤，實證有效自動轉正">候選</span>` : ""}
      <span class="badge ${s.enabled ? "on" : "off"}">${s.enabled ? "啟用中" : s.candidate ? "觀察中" : "已停用"}</span>
      <span class="badge w">權重 ${s.weight}</span></div>
    <div class="s-desc">${s.desc}</div>
    <div class="bar"><i style="width:${Math.min(s.win_rate || 0, 100)}%"></i></div>
    <div class="s-meta">
      <span>勝率 <b>${s.win_rate == null ? "樣本不足" : s.win_rate + "%"}</b></span>
      <span>交易 ${s.trades} 筆</span>
      <span>單筆均損益 ${s.avg_net == null ? "–" : signTxt(s.avg_net) + fmt(s.avg_net) + " 元"}</span>
      <span>期望值 ${s.expectancy == null ? "–" : signTxt(s.expectancy) + s.expectancy + "%"}</span>
    </div>
  </div>`).join("");

  const log = d.log || [];
  $("#evolveLog").innerHTML = log.length
    ? [...log].reverse().slice(0, 30).map((l) => `<div class="log-item"><span class="d">${l.date}</span>${l.msg}</div>`).join("")
    : "尚無調整紀錄（樣本累積中，策略達 8 筆成交後開始評估）";
}

/* ---------- 設定 ---------- */
$("#btnSettings").addEventListener("click", () => {
  $("#sDiscount").value = settings.discount;
  $("#sMinFee").value = settings.minFee;
  $("#sLots").value = settings.lots;
  const rk = riskParams();
  $("#sRiskEnabled").checked = rk.enabled;
  $("#sDailyLimit").value = rk.daily_loss_limit;
  $("#sMaxConsec").value = rk.max_consecutive_losses;
  $("#sSizing").value = rk.position_sizing;
  $("#sPerTradeRisk").value = rk.per_trade_risk;
  $("#sMaxLots").value = rk.max_lots;
  $("#settingsDlg").showModal();
});
$("#btnSaveSettings").addEventListener("click", () => {
  settings = {
    ...settings,
    discount: Math.max(0.1, +$("#sDiscount").value || DEFAULT_SETTINGS.discount),
    minFee: Math.max(0, +$("#sMinFee").value || 0),
    lots: Math.max(1, Math.round(+$("#sLots").value || 1)),
    risk: {
      enabled: $("#sRiskEnabled").checked,
      daily_loss_limit: Math.max(0, +$("#sDailyLimit").value || 10000),
      max_consecutive_losses: Math.max(1, Math.round(+$("#sMaxConsec").value || 3)),
      position_sizing: $("#sSizing").value === "fixed" ? "fixed" : "risk_parity",
      per_trade_risk: Math.max(0, +$("#sPerTradeRisk").value || 2500),
      max_lots: Math.max(1, Math.round(+$("#sMaxLots").value || 4)),
    },
  };
  localStorage.setItem("sp_settings", JSON.stringify(settings));
  $("#settingsDlg").close();
  if (DB.reviews) renderReview();
});
$("#btnCancelSettings").addEventListener("click", () => $("#settingsDlg").close());

boot();
