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
let DB = { picks: null, market: null, reviews: null, strategies: null, priceModel: null, config: null, swing: null };
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
    const swing = await loadJSON("data/swing/state.json").catch(() => null);               // 波段模組（崩跌反彈）
    DB = { picks, market, reviews, strategies, priceModel, config, swing };
    (market.strategies || []).forEach((s) => (stratMeta[s.id] = s));
    Object.entries(strategies.meta || {}).forEach(([id, m]) => (stratMeta[id] = { id, ...m }));
    $("#dataDate").textContent = `資料日：${market.date} · 更新：${(market.updated_at || "").slice(0, 16).replace("T", " ")}`;
    renderPicks();
    initCustomForm();
    renderReview();
    renderPriceModel();
    renderStrategies();
    renderSwing();
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
function exitRuleHtml(p) {
  // 出場規則必須用完整句子寫在卡片上：移動停利／時間停損的出場價是盤中才決定的，
  // 復盤會出現卡片四個價位以外的數字（例：9/18 全友停利 55.1 地板、實際追高回落於 57.9 出場）
  if (!p.trail_dist && p.tstop_bar == null) return "";
  const short = p.side === "short";
  const parts = [];
  if (p.trail_dist)
    parts.push(short
      ? `觸及回補價 <b>${fmt2(p.target)}</b> 後<b>不立刻回補</b>，改追蹤盤中低點、反彈 <b>${fmt2(p.trail_dist)}</b> 元時回補（${fmt2(p.target)} 為天花板，最差在此回補）`
      : `觸及停利 <b>${fmt2(p.target)}</b> 後<b>不立刻賣</b>，改追蹤盤中高點、回落 <b>${fmt2(p.trail_dist)}</b> 元時賣出（${fmt2(p.target)} 為地板，最差在此賣）`);
  if (p.tstop_bar != null)
    parts.push(`<b>${tstopTime(p.tstop_bar)}</b> 前仍未觸及${short ? "回補價" : "停利"} → 以當時市價出場`);
  return `<div class="exit-rule">⏱ 出場規則：${parts.join("；")}。<span class="muted">這兩種出場價盤中才決定，復盤會出現卡片以外的數字。</span></div>`;
}
function entryRuleHtml(p) {
  // 進場紀律（執行層規則）：開盤 09:00 與 10:00 各看一眼即可執行。
  // 依據：5 條 walk-forward 路徑類別記帳——10:00 後才成交的單 5/5 路徑負期望；
  // 空單開盤緩衝不足 0.5R 者 4/5 路徑負期望（0.25~0.5R 為 5/5）。
  if (p.min_cushion == null && p.cutoff_bar == null) return "";
  const short = p.side === "short";
  const parts = [];
  if (p.min_cushion) {
    const gate = short ? p.entry - p.min_cushion : p.entry + p.min_cushion;
    parts.push(short
      ? `09:00 開盤價須 <b>≤ ${fmt2(Math.round(gate * 100) / 100)}</b> 才掛空單，高於此價即<b>作廢</b>（開盤離掛價太近＝反彈空間不足）`
      : `09:00 開盤價須 <b>≥ ${fmt2(Math.round(gate * 100) / 100)}</b> 才掛買單，低於此價即<b>作廢</b>（開盤離掛價太近＝回檔空間不足）`);
  }
  if (p.cutoff_bar != null)
    parts.push(`掛單只到 <b>${tstopTime(p.cutoff_bar)}</b>，屆時未成交即<b>撤單</b>（晚成交的單沒有時間發酵）`);
  return `<div class="exit-rule entry-rule">🚦 進場紀律：${parts.join("；")}。</div>`;
}
function stockCard(p, rank) {
  const tags = (p.strategies || [])
    .map((id) => `<span class="tag" title="${stratMeta[id]?.desc || ""}">${stratMeta[id]?.name || id}</span>`)
    .join("");
  const chg = p.chg_pct;
  const short = p.side === "short";
  const sideBadge = `<span class="badge ${short ? "sside" : "lside"}" title="${short ? "做空：放空掛高、回補在低（現股當沖先賣後買）" : "做多：掛低買進、停利在高"}">${short ? "🔻 做空" : "🔺 做多"}</span>`;
  const bo = p.entry_mode === "breakout";
  const boBadge = bo ? `<span class="badge bo" title="突破追價單：價格「穿越」觸發價才進場（非掛單等回檔）——請用券商條件單設定觸發價、觸發後市價追入；若跳空開盤已越過停利價則放棄不追">⚡突破</span>` : "";
  const grid = short
    ? (bo
      ? `<div><div class="lb">跌破觸發 追空</div><div class="v tp">${fmt2(p.entry)}</div></div>
         <div><div class="lb">回補停利</div><div class="v buy">${fmt2(p.target)}</div></div>
         <div><div class="lb">停損</div><div class="v ah">${fmt2(p.stop)}</div></div>
         <div><div class="lb">逆勢賣出 NH</div><div class="v sl">${fmt2(p.cdp_base?.nh ?? "")}</div></div>`
      : `<div><div class="lb" title="開盤若已高於此價（跳空穿價），依紀律作廢此單——「更好的空點」是隔夜動能反向的軋空起點，10 路徑實證均 −326/筆">建議放空 NH</div><div class="v tp">${fmt2(p.entry)}</div></div>
       <div><div class="lb" title="${p.trail_dist ? `這不是出場目標：觸及後啟動移動停利，追蹤盤中低點、反彈 ${fmt2(p.trail_dist)} 元才回補（此價為最差回補價）；實際平均賺賠見頁首「實際賺賠結構」` : "回補停利價"}">${p.trail_dist ? "移停啟動 NL" : "回補停利 NL"}</div><div class="v buy">${fmt2(p.target)}</div></div>
       <div><div class="lb">停損 AH</div><div class="v ah">${fmt2(p.stop)}</div></div>
       <div><div class="lb">破底加碼 AL</div><div class="v sl">${fmt2(p.cdp_base?.al ?? p.target)}</div></div>`)
    : (bo
      ? `<div><div class="lb">漲穿觸發 追買</div><div class="v buy">${fmt2(p.entry)}</div></div>
         <div><div class="lb">停利</div><div class="v tp">${fmt2(p.target)}</div></div>
         <div><div class="lb">停損</div><div class="v sl">${fmt2(p.stop)}</div></div>
         <div><div class="lb">逆勢買進 NL</div><div class="v ah">${fmt2(p.cdp_base?.nl ?? "")}</div></div>`
      : `<div><div class="lb" title="開盤若已低於此價（跳空穿價），依紀律作廢此單——「更便宜」的開盤價是隔夜動能反向的接刀，10 路徑實證均 −326/筆">建議買進 NL</div><div class="v buy">${fmt2(p.entry ?? p.cdp?.nl)}</div></div>
       <div><div class="lb" title="${p.trail_dist ? `這不是出場目標：觸及後啟動移動停利，追蹤盤中高點、回落 ${fmt2(p.trail_dist)} 元才賣出（此價為最差賣價）；實際平均賺賠見頁首「實際賺賠結構」` : "停利價"}">${p.trail_dist ? "移停啟動 NH" : "停利 NH"}</div><div class="v tp">${fmt2(p.target ?? p.cdp?.nh)}</div></div>
       <div><div class="lb">停損 AL</div><div class="v sl">${fmt2(p.stop ?? p.cdp?.al)}</div></div>
       <div><div class="lb">順勢突破 AH</div><div class="v ah">${fmt2(p.ah ?? p.cdp?.ah)}</div></div>`);
  return `<div class="card">
    <div class="head">
      ${rank ? `<span class="rank">#${rank}</span>` : ""}
      <span class="code">${p.code}</span><span class="name">${p.name}</span>
      <span class="mkt">${p.market === "tpex" ? "上櫃" : "上市"}</span>
      ${sideBadge}${boBadge}
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
    ${entryRuleHtml(p)}
    ${exitRuleHtml(p)}
    ${riskBadgeHtml(p)}
  </div>`;
}

/* ---------- Tab 1 今日選股 ---------- */
const shiftTxt = (v) => (v > 0 ? `+${v}` : `${v}`) + "R";
// 時間停損 bar index → 時刻（bar 0 = 09:00 開盤後第一根 5 分K；24=11:00、30=11:30、36=12:00）
const tstopTime = (bar) => {
  const mins = 9 * 60 + bar * 5;
  return `${String(Math.floor(mins / 60)).padStart(2, "0")}:${String(mins % 60).padStart(2, "0")}`;
};
function renderPicks() {
  const d = DB.picks;
  const n = d.picks?.length || 0;
  const sh = d.price_shifts || {};
  const shifted = ["entry", "target", "stop"].some((k) => sh[k]);
  const exitMode = (sh.trail || sh.tstop != null)
    ? `、出場引擎：移動停利 ${sh.trail ? sh.trail + "R" : "關"}／時間停損 ${sh.tstop != null ? tstopTime(sh.tstop) : "關"}（A/B 實證擇優）` : "";
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
  // 翻向日護欄徽章：主方向與前一復盤日不同＝方向可信度最低的日子（10 路徑有 9 條翻向日均為負）
  const flipChip = rg?.flip
    ? `<span class="badge ct" title="主方向與前一交易日相反（寬度 5MA 貼近 50% 的震盪期最常見）——跨 10 條 walk-forward 路徑有 9 條在翻向日平均虧損，故本日主方向名單自動縮編、不遞補；請自行斟酌再縮小部位">⚠️ 環境翻向日｜名單已縮編</span> ` : "";
  // 隔夜訊號徽章（清晨校正）：美股收盤加權（費半50%+S&P30%+NASDAQ20%），突破門檻且與寬度相反時翻轉環境
  const on = rg?.overnight;
  const onChip = on && on.w != null
    ? `<span class="badge ${rg.overnight_flip ? "flip" : "cand"}" title="隔夜訊號＝美股收盤加權（費半 ${on.sox ?? "–"}%／S&P ${on.gspc ?? "–"}%／NASDAQ ${on.ixic ?? "–"}%）；美股價格已消化 Fed、財報、地緣等隔夜消息，與台股當日寬度相關 0.40。清晨 05:10 排程抓取後自動校正環境判定">🌙 隔夜 ${on.w > 0 ? "+" : ""}${on.w}%${rg.overnight_flip ? `｜⚡ 清晨校正：隔夜訊號翻轉環境 → ${rg.book === "long" ? "改做多" : "改做空"}` : ""}</span> ` : "";
  const mixTxt = n
    ? `本日名單：${nLong ? `🔺做多 <b>${nLong}</b> 檔` : ""}${nLong && nShort ? "＋" : ""}${nShort ? `🔻做空 <b class="down">${nShort}</b> 檔（現股當沖先賣後買）` : ""}${nCt
        ? `，其中 <b>${nCt}</b> 檔為<b>逆勢配額</b>（與大盤環境反向、門檻更嚴，實驗性質信心自酌）` : ""}` : "";
  const shS = sh.short || {};
  const priceLine = (shifted || exitMode || nShort)
    ? `<br>📐 價格模型（多空各自迭代）：${nLong ? `做多 進${shiftTxt(sh.entry ?? 0)}/停利${shiftTxt(sh.target ?? 0)}/停損${shiftTxt(sh.stop ?? 0)}` : ""}${nLong && nShort ? "；" : ""}${nShort ? `做空 進−${shS.entry ?? 0}R/回補−${shS.target ?? 0}R/停損−${shS.stop ?? 0}R` : ""}${exitMode}` : "";
  // 系統健康度（誠實透明）：近 20 個復盤日的已實現績效——冷streak時直接警示使用者，
  // 勝過任何假裝有 edge 的優化（連4日虧損的正確第一反應是縮部位，不是加碼調參）
  let healthLine = "";
  if (DB.reviews?.length) {
    const tail = [...DB.reviews].sort((a, b) => a.date.localeCompare(b.date)).slice(-20);
    const hn = tail.reduce((s, r) => s + (r.summary?.net || 0), 0);
    const hf = tail.reduce((s, r) => s + (r.summary?.n_filled || 0), 0);
    const hw = tail.reduce((s, r) => s + (r.summary?.n_wins || 0), 0);
    const wr = hf ? (hw / hf * 100).toFixed(0) : "–";
    healthLine = hn < 0
      ? `<div class="health cold">🥶 <b>系統冷streak警示</b>：近 ${tail.length} 個復盤日合計 <b>−${fmt(-hn)}</b> 元（勝率 ${wr}%）——系統近期沒有優勢。此時的誠實建議是<b>縮小部位或觀望</b>，等汰弱留強與價格模型自我修正回暖，而不是照單全收。</div>`
      : `<div class="health warm">💚 系統健康度：近 ${tail.length} 個復盤日合計 <b>+${fmt(hn)}</b> 元（勝率 ${wr}%）</div>`;
  }
  // 實際賺賠結構（近 40 復盤日、依方向）：卡片上的名目停利／停損距離不代表實際賺賠——
  // 「停利」是移停啟動點、多數輸單由時間停損以小額帶出、停損是災難保險。這裡印真實數字。
  let realLine = "";
  if (DB.reviews?.length) {
    const tail40 = [...DB.reviews].sort((a, b) => a.date.localeCompare(b.date)).slice(-40);
    const seg = ["long", "short"].map((sd) => {
      const tr = tail40.flatMap((r) => r.picks.filter((p) => p.filled && (p.side || "long") === sd));
      if (tr.length < 5) return "";
      const w = tr.filter((p) => p.net > 0), l = tr.filter((p) => p.net <= 0);
      const aw = w.length ? w.reduce((s, p) => s + p.net, 0) / w.length : 0;
      const al = l.length ? l.reduce((s, p) => s + p.net, 0) / l.length : 0;
      const po = al ? aw / -al : 0;
      const ev = tr.reduce((s, p) => s + p.net, 0) / tr.length;
      const cnt = (r) => tr.filter((p) => p.exit_reason === r).length;
      return `${sd === "short" ? "🔻做空" : "🔺做多"} ${tr.length} 筆：勝率 <b>${(w.length / tr.length * 100).toFixed(0)}%</b>・平均賺 <b class="up">+${fmt(Math.round(aw))}</b>／平均賠 <b class="down">${fmt(Math.round(al))}</b>・賺賠比 <b class="${po >= 1.2 ? "up" : po < 1 ? "down" : ""}">${po.toFixed(2)}</b>・每筆期望 <b class="${signCls(ev)}">${signTxt(ev)}${fmt(Math.round(ev))}</b>・出場：移停/停利 ${cnt("trail") + cnt("target")}、時停 ${cnt("timeout")}、停損 ${cnt("stop")}`;
    }).filter(Boolean);
    if (seg.length)
      realLine = `<div class="health stats">📏 <b>實際賺賠結構</b>（近 ${tail40.length} 復盤日）——卡片的名目停利／停損距離不是實際賺賠：「停利」只是移停啟動點、多數輸單由時間停損小額帶出、停損是災難保險。真實數字：<br>${seg.join("<br>")}</div>`;
  }
  const dawnAt = d.dawn_at ? `（${d.dawn_at.slice(11, 16)} 校正）` : "";
  const dawnNote = d.dawn_corrected
    ? `<br>⚡ <b>清晨校正版</b>${dawnAt}：美股收盤後隔夜訊號與台股寬度判定相反，環境已翻轉、建議單已依新方向重出（晚間初版作廢）。${
        d.dawn_late ? `<br>⚠️ <b>此次校正在開盤後才完成</b>（排程被延遲）——若你已依晚間初版進場，請自行判斷是否調整，不要事後拿這份名單回推早盤決策。` : ""}` : "";
  $("#picksInfo").innerHTML = healthLine + realLine + (n
    ? `${regimeChip}${flipChip}${onChip}📅 <b>${d.generated_on}</b> 收盤後產生 · 適用<b>下一交易日</b>盤中 · 共 <b>${n}</b> 檔${nFb
        ? `（含遞補 ${nFb} 檔，訊號較弱）` : ""}${dawnNote}${mixTxt ? `<br>${mixTxt}` : ""}
       <br>已排除處置股／注意股／非當沖標的／不可放空者（做空單）／1張風險超日限者／流動性與波動不足者。${priceLine}${riskSummary}`
    : `${regimeChip}${flipChip}${onChip}本日${book === "short" ? "空方環境下亦" : ""}無符合門檻的標的（可能連續假期後資料待更新，或基礎濾網過嚴）。可到「自訂選股」自行研究。`);
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
    // 出場價若不是卡片上的價位，就把它是怎麼來的寫出來（移動停利=高/低點回撤、時間停損=該時刻市價）
    const exitTxt = (p) => {
      if (p.exit_reason === "trail" && p.trail_dist) {
        const peak = p.side === "short" ? p.exit_price - p.trail_dist : p.exit_price + p.trail_dist;
        return p.side === "short"
          ? `移動停利（低點約 ${fmt2(peak)} 反彈 ${fmt2(p.trail_dist)}）`
          : `移動停利（高點約 ${fmt2(peak)} 回落 ${fmt2(p.trail_dist)}）`;
      }
      if (p.exit_reason === "timeout" && p.tstop_bar != null) return `時間停損（${tstopTime(p.tstop_bar)} 市價）`;
      return reasonTxt[p.exit_reason] || p.exit_reason;
    };
    const rows = d.rows.map((p) => {
      const sm = p.side === "short" ? '<span class="down" title="做空">🔻</span> ' : "";
      // 當時建議的完整價位（做空鏡像：進場為賣價、突破參考為下方 AL 側；突破模式=穿越觸發追價）
      const isBo = p.entry_mode === "breakout";
      const tl = !!p.trail_dist;   // 有移動停利時，「停利價」其實是移停啟動點，不是出場目標
      const lb = p.side === "short"
        ? (isBo ? { e: "⚡跌破追空", t: tl ? "移停啟動" : "回補停利", s: "停損", a: "逆勢賣出" }
                : { e: "放空", t: tl ? "移停啟動" : "回補停利", s: "停損", a: "順勢跌破" })
        : (isBo ? { e: "⚡漲穿追買", t: tl ? "移停啟動" : "停利", s: "停損", a: "逆勢買進" }
                : { e: "買進 NL", t: tl ? "移停啟動 NH" : "停利 NH", s: "停損 AL", a: "突破 AH" });
      const refPx = isBo ? (p.side === "short" ? p.cdp_base?.nh : p.cdp_base?.nl) : p.ah;
      const planLine = `<div class="plan muted small">${lb.e} ${fmt2(p.entry)}・${lb.t} ${fmt2(p.target)}・${lb.s} ${fmt2(p.stop)}${refPx != null ? `・${lb.a} ${fmt2(refPx)}` : ""}${p.trail_dist ? `・移停 ${fmt2(p.trail_dist)}` : ""}${p.tstop_bar != null ? `・時停 ${tstopTime(p.tstop_bar)}` : ""}${p.cutoff_bar != null ? `・掛單至 ${tstopTime(p.cutoff_bar)}` : ""}${p.min_cushion ? `・開盤緩衝 ≥${fmt2(p.min_cushion)}` : ""}</div>`;
      const nameCell = `${sm}${p.code} ${p.name}${planLine}`;
      if (!p.filled) {
        const reasonHypo = { target: "觸及停利", trail: "移動停利", stop: "觸及停損", timeout: "時間停損", close: "收盤沖銷" };
        if (p.touch_only) {
          const hypo = p.hypo_reason
            ? `；若排到隊：${reasonHypo[p.hypo_reason] || p.hypo_reason} ${signTxt(p.hypo_ret_pct)}${fmt2(p.hypo_ret_pct)}%`
            : "";
          return `<tr class="dim"><td>${nameCell}</td><td>${fmt2(p.entry)}</td>
          <td colspan="3">⚠️ 觸價未穿——排隊未必成交（保守記未成交${hypo}）</td><td>–</td></tr>`;
        }
        if (p.exit_reason === "cushionvoid" || p.exit_reason === "expired") {
          const raw = p.raw;
          const rawTxt = raw
            ? `；若仍進場：${reasonHypo[raw.exit_reason] || raw.exit_reason} <span class="${signCls(raw.net)}">${signTxt(raw.net)}${fmt(raw.net)}</span>`
            : "（原本也不會成交）";
          const why = p.exit_reason === "cushionvoid"
            ? `🚫 開盤 ${fmt2(p.day_open)} 離掛價太近（緩衝不足）——依進場紀律作廢`
            : `⏰ ${p.cutoff_bar != null ? tstopTime(p.cutoff_bar) : ""} 前未成交——依進場紀律撤單`;
          return `<tr class="dim"><td>${nameCell}</td><td>${fmt2(p.entry)}</td>
          <td colspan="3">${why}${rawTxt}</td><td>–</td></tr>`;
        }
        if (p.exit_reason === "gapvoid") {
          return `<tr class="dim"><td>${nameCell}</td><td>${fmt2(p.entry)}</td>
          <td colspan="3">🚫 開盤 ${fmt2(p.day_open)} 已穿掛價——依紀律作廢（隔夜動能反向，「更便宜」是接刀；10路徑實證穿價開盤成交均 −326/筆）</td><td>–</td></tr>`;
        }
        const gaveUp = isBo && (p.side === "short" ? p.day_open <= p.target : p.day_open >= p.target);
        const noFillTxt = gaveUp
          ? `未進場（開盤 ${fmt2(p.day_open)} 已越過停利價，不追）`
          : isBo
            ? (p.side === "short" ? `未觸發（最低 ${fmt2(p.day_low)} 未跌破觸發價）` : `未觸發（最高 ${fmt2(p.day_high)} 未漲穿觸發價）`)
            : (p.side === "short" ? `未成交（最高 ${fmt2(p.day_high)} 未觸賣價）` : `未成交（最低 ${fmt2(p.day_low)} 未觸價）`);
        return `<tr class="dim"><td>${nameCell}</td><td>${fmt2(p.entry)}</td>
        <td colspan="3">${noFillTxt}</td><td>–</td></tr>`;
      }
      const m = p._m || {};
      if (!m.taken) {
        const why = m.reason === "consec" ? "🛑 連虧停手" : "🛑 超出當日風險額度";
        return `<tr class="dim"><td>${nameCell}</td><td>${fmt2(p.fill_price)}</td>
        <td>${fmt2(p.exit_price)}</td><td colspan="2">${why}</td>
        <td class="muted">(未取 ${signTxt(p._net)}${fmt(p._net)})</td></tr>`;
      }
      return `<tr><td>${nameCell}</td><td>${fmt2(p.fill_price)}</td>
        <td>${fmt2(p.exit_price)}</td><td>${exitTxt(p)}</td>
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
    `進出場建議價＝CDP 基準價＋偏移 ×「訊號日振幅 R」，偏移與<b>出場引擎</b>（地板式移動停利、多時點時間停損）
     每日依最近 <b>${pm.window_days}</b> 個復盤日重放 A/B 實證、績效明顯改善才切換（防止雜訊抖動）。
     目前：${sh.mode === "breakout" ? "進場模式 <b>⚡突破追價</b>（漲穿 AH−eR 觸發追買）· " : ""}進場 <b>${shiftTxt(sh.entry ?? 0)}</b> · 停利 <b>${shiftTxt(sh.target ?? 0)}</b> ·
     停損 <b>${shiftTxt(sh.stop ?? 0)}</b> · 移動停利 <b>${sh.trail ? sh.trail + "R" : "關"}</b> ·
     時間停損 <b>${sh.tstop != null ? tstopTime(sh.tstop) : "關"}</b>
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
    $("#priceModelInfo").innerHTML += `<br>🔻 <b>空方（獨立迭代）</b>：${shS.mode === "breakout"
      ? `<b>⚡突破追價</b>（跌破 AL+<b>${shS.entry ?? 0}R</b> 觸發追空）· 回補 AL−<b>${shS.target ?? 0}R</b> · 停損 進場+<b>${(0.5 - (shS.stop ?? 0)).toFixed(2)}R</b>`
      : `放空 NH−<b>${shS.entry ?? 0}R</b> ·
      回補 NL−<b>${shS.target ?? 0}R</b> · 停損 AH−<b>${shS.stop ?? 0}R</b>`} · 移動停利 <b>${shS.trail ? shS.trail + "R" : "關"}</b> ·
      時間停損 <b>${shS.tstop != null ? tstopTime(shS.tstop) : "關"}</b>
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


/* ---------- 波段（崩跌反彈波段模組） ---------- */
// 研究依據 docs/swing_research.md：持股 ≥1 週、+15% 的機會集中在「市場急跌後的反彈日」；
// 平時所有動能／突破型態皆無正期望，故模組平時待命、只在閘門開啟日產單。
let swingChart = null;
const pctTxt = (v, d = 2) => (v == null ? "–" : `${v > 0 ? "+" : ""}${Number(v).toFixed(d)}%`);
const SWING_REASON = {
  target: "✅ 停利", stop: "🛑 停損", stop_lock: "🛑 跌停延後停損", time: "⏱ 時間出場",
  limitup: "⛔ 開盤漲停買不到", nobar: "⛔ 無交易", open: "📌 持有中", pending: "🕘 待隔日開盤進場",
};
function swingSetupBadge(s) {
  const R = DB.swing.rules.setups || {};
  return (s.setups || [s.setup]).map((k) => `<span class="badge setup" title="${(R[k] || {}).desc || ""}">${(R[k] || {}).name || k}</span>`).join(" ");
}
function swingSignalCard(s, rules) {
  const ex = s.rules || rules.exit;
  const taken = s.taken;
  const badge = s.blocked ? `<span class="badge off">🚧 斷路器暫停</span>`
    : taken ? `<span class="badge taken">✅ 建議進場</span>` : `<span class="badge wait">候補（槽位已滿）</span>`;
  return `<div class="card${taken ? "" : " dim"}">
    <div class="head"><span class="rank">#${s.rank}</span><span class="code">${s.code}</span><span class="name">${s.name}</span>
      <span class="mkt">${s.market === "tpex" ? "上櫃" : "上市"}</span>${badge}</div>
    <div class="px">收盤 <b>${fmt2(s.close)}</b> <span class="${signCls(s.chg_pct)}">${pctTxt(s.chg_pct)}</span></div>
    <div class="tags">${swingSetupBadge(s)}
      <span class="tag">離 60 日高 ${s.drawdown_pct == null ? "–" : "−" + Number(s.drawdown_pct).toFixed(1) + "%"}${s.hi_days < 60 ? `（僅 ${s.hi_days} 日資料）` : ""}</span>
      <span class="tag">5 日 ${pctTxt(s.drop5_pct, 1)}</span>
      <span class="tag">20 日均成交值 ${(s.val_ma / 1e8).toFixed(1)} 億</span>
      ${s.vol_ratio != null ? `<span class="tag" title="訊號日成交量／前 20 日均量——實證量比低者反彈較佳">量比 ${Number(s.vol_ratio).toFixed(1)}</span>` : ""}
      ${s.score != null ? `<span class="tag" title="綜合分（同日分位數加權）：離高點深度 ${s.score_parts?.drawdown ?? "–"}／低量比 ${s.score_parts?.low_vol_ratio ?? "–"}／成交值 ${s.score_parts?.val_ma ?? "–"}">綜合分 ${s.score}</span>` : ""}</div>
    <div class="price-grid g3">
      <div><div class="lb">開盤價上限（漲停）</div><div class="v ah">${fmt2(s.limit_up_next)}</div></div>
      <div><div class="lb">參考停損（−${ex.stop_pct}%）</div><div class="v sl">${fmt2(s.ref_stop)}</div></div>
      <div><div class="lb">參考停利（+${ex.target_pct}%）</div><div class="v tp">${fmt2(s.ref_target)}</div></div>
    </div>
    <div class="exit-rule">📋 執行：隔日 09:00 <b>開盤市價買進</b>；開盤價 ≥ <b>${fmt2(s.limit_up_next)}</b>（漲停）即<b>放棄</b>。
      成交後停損 = 成交價 −${ex.stop_pct}%（跳空以開盤價出場、跌停鎖死順延次日開盤）；
      <b>第 ${ex.min_hold_days} 日起</b>觸及成交價 +${ex.target_pct}% 停利；第 ${ex.max_hold_days} 日收盤未出場則<b>市價出場</b>。
      <span class="muted">參考價以今日收盤推算，實際以成交價為準。</span></div>
  </div>`;
}
function swingTradesTable(rows) {
  const tr = rows.map((t) => {
    const st = t.status;
    const exitTxt = st === "closed" ? `${fmt2(t.exit)}<div class="plan muted">${t.exit_date}</div>`
      : st === "open" ? `${fmt2(t.last)}<div class="plan muted">${t.last_date} 現價</div>` : "–";
    const ret = st === "closed" || st === "open" ? `<span class="${signCls(t.ret_pct)}">${pctTxt(t.ret_pct)}</span>` : "–";
    const reason = st === "closed" ? SWING_REASON[t.reason] || t.reason
      : st === "unfilled" ? (SWING_REASON[t.reason] || t.reason) + (t.open_next != null ? `<div class="plan muted">開 ${fmt2(t.open_next)} ≥ 漲停 ${fmt2(t.limit_up)}</div>` : "")
      : SWING_REASON[st];
    return `<tr class="${st === "unfilled" ? "dim" : ""}">
      <td class="lf"><b>${t.code}</b> ${t.name}<div class="plan muted">${(t.setups || [t.setup]).map((k) => (DB.swing.rules.setups[k] || {}).name || k).join("＋")}</div></td>
      <td>${t.signal_date}</td>
      <td>${t.fill != null ? fmt2(t.fill) : "–"}${t.fill_date && t.fill != null ? `<div class="plan muted">${t.fill_date}</div>` : ""}</td>
      <td>${exitTxt}</td>
      <td class="lf">${reason}</td>
      <td>${t.days != null ? t.days : "–"}</td>
      <td>${ret}</td>
      <td class="${st === "closed" ? signCls(t.net) : ""}">${st === "closed" ? signTxt(t.net) + fmt(t.net) : "–"}</td>
    </tr>`;
  }).join("");
  return `<div class="tbl-wrap"><table class="trades">
    <thead><tr><th class="lf">股票／型態</th><th>訊號日</th><th>成交</th><th>出場／現價</th><th class="lf">結果</th><th>持有日</th><th>報酬</th><th>1 張淨損益</th></tr></thead>
    <tbody>${tr}</tbody></table></div>`;
}
function swingStatCards(st, extra = "") {
  if (!st || !st.n_closed) return `<div class="stat"><div class="lb">已平倉</div><div class="v">0 筆</div></div>${extra}`;
  return `<div class="stat"><div class="lb">已平倉</div><div class="v">${st.n_closed} 筆</div></div>
    <div class="stat"><div class="lb">勝率 / 達標率</div><div class="v">${st.win_rate}% / ${st.hit_rate}%</div></div>
    <div class="stat"><div class="lb">單筆平均</div><div class="v ${signCls(st.mean_ret_pct)}">${pctTxt(st.mean_ret_pct)}</div></div>
    <div class="stat"><div class="lb">權益貢獻（每檔 ${DB.swing.rules.position_pct}%）</div><div class="v ${signCls(st.contrib_pct)}">${pctTxt(st.contrib_pct)}</div></div>${extra}`;
}
function renderSwing() {
  const sw = DB.swing;
  const gauge = $("#swingGauge"), todayBox = $("#swingToday"), posBox = $("#swingPositions");
  const sumBox = $("#swingSummary"), evBox = $("#swingEvents"), explain = $("#swingExplain");
  if (!sw) {
    gauge.innerHTML = "";
    todayBox.innerHTML = `<div class="empty">尚無波段資料（下一次每日更新後產生）</div>`;
    return;
  }
  const g = sw.gauge, R = sw.rules, G = R.gate, EX = R.exit;
  const meta = { open: ["🟢", "閘門開啟", "open"], warn: ["🟡", "預警", "warn"], idle: ["⚪", "待命", "idle"], na: ["–", "資料不足", "idle"] };
  const [ico, txt, cls] = meta[g.state] || meta.na;
  gauge.innerHTML = `
    <div class="stat"><div class="lb">等權 5 日報酬</div><div class="v ${signCls(g.mkt5)}">${pctTxt(g.mkt5)}</div></div>
    <div class="stat"><div class="lb">當日等權</div><div class="v ${signCls(g.ew)}">${pctTxt(g.ew)}</div></div>
    <div class="stat"><div class="lb">漲跌家數寬度 5MA</div><div class="v">${g.breadth5 == null ? "–" : Number(g.breadth5).toFixed(2)}</div></div>
    <div class="stat gauge-${cls}"><div class="lb">閘門（${g.date}）</div><div class="v">${ico} ${txt}${g.tier ? ` <span class="badge tier">${g.tier}</span>` : ""}</div></div>`;

  // 壓力計走勢圖：等權 5 日報酬 vs 閘門門檻
  chartWhenVisible("swing", () => {
    const ctx = $("#swingChart");
    if (swingChart) swingChart.destroy();
    const H = sw.history.filter((h) => h.mkt5 != null);
    swingChart = new Chart(ctx, {
      type: "line",
      data: {
        labels: H.map((h) => h.date.slice(5)),
        datasets: [
          { label: "等權 5 日報酬 %", data: H.map((h) => h.mkt5), borderColor: "#4da3ff", backgroundColor: "rgba(77,163,255,.15)", fill: true, tension: .25, pointRadius: H.map((h) => (h.state === "open" ? 4 : 0)), pointBackgroundColor: "#2ecc71" },
          { label: `T1 門檻 ${G.mkt5_le}%`, data: H.map(() => G.mkt5_le), borderColor: "rgba(255,93,82,.8)", borderDash: [6, 4], pointRadius: 0, borderWidth: 1 },
          { label: `預警 ${G.warn_mkt5_le}%`, data: H.map(() => G.warn_mkt5_le), borderColor: "rgba(245,192,78,.8)", borderDash: [4, 4], pointRadius: 0, borderWidth: 1 },
        ],
      },
      options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { labels: { color: "#93a4b8", boxWidth: 10 } } },
        scales: { x: { ticks: { color: "#93a4b8", maxTicksLimit: 8 }, grid: { color: "#2a3342" } }, y: { ticks: { color: "#93a4b8" }, grid: { color: "#2a3342" } } } },
    });
  });

  // 今日建議單
  const T = sw.today;
  const takenN = T.signals.filter((s) => s.taken).length;
  const t1Note = G.t1_enabled === false ? "（T1 已關閉，只在反彈確認日出單）"
    : (G.t1_setups?.length || G.t1_max_new) ? `（T1 日限制：${G.t1_setups?.length ? `只出 ${G.t1_setups.join("/")}` : ""}${G.t1_setups?.length && G.t1_max_new ? "、" : ""}${G.t1_max_new ? `新進場最多 ${G.t1_max_new} 檔` : ""}——3 年 A/B：T1 日單勝率 43%、T2 日 94%，容量要留給反彈確認日）` : "";
  const gateRule = `閘門規則：等權 5 日報酬 ≤ ${G.mkt5_le}%（T1 深跌），或 ≤ ${G.mkt5_le_rebound}% 且當日 ≥ +${G.rebound_day_ge}%（T2 反彈確認）${t1Note}。`;
  if (T.state === "open") {
    todayBox.innerHTML = `<div class="notice">🟢 <b>${T.date} 閘門開啟（${T.tier === "T2" ? "T2 反彈確認日" : "T1 深跌"}）</b>：以下為<b>隔日開盤市價買進</b>的波段建議單，
      共 ${T.signals.length} 檔，依<b>綜合分</b>（離 60 日高深度／低量比／成交值，同日分位數加權）排序，取前 ${takenN} 檔（槽位上限 ${g.cap_today}${T.tier === "T2" && g.cap_today > g.max_positions ? `，反彈確認日由 ${g.max_positions} 擴大` : ""}${T.tier === "T1" && T.t1_cap ? `；T1 日新進場上限 ${T.t1_cap} 檔` : ""}，已用 ${g.open_positions}），其餘候補。
      ${T.breaker ? `<br>🚧 斷路器：本事件最近兩批進場多數停損${R.breaker.block_entries ? "，已暫停 T1 新單" : "（僅提醒，未阻擋）"}。` : ""}
      <br><span class="small">${gateRule}</span></div>
      <div class="cards">${T.signals.map((s) => swingSignalCard(s, R)).join("")}</div>`;
  } else {
    const warn = T.state === "warn" ? `🟡 <b>預警</b>：等權 5 日已跌 ${pctTxt(g.mkt5)}，接近閘門。` : "";
    todayBox.innerHTML = `<div class="notice">${warn || "⚪ <b>待命</b>：閘門未開啟，今日沒有波段建議單。"}
      模組只在市場急跌後的反彈日產單（3 年實證平時所有型態皆無正期望）。<br><span class="small">${gateRule}</span></div>`;
  }

  // 持倉
  const P = sw.positions || [];
  posBox.innerHTML = `<h3 class="swing-h3">📌 模擬持倉（${P.length}/${g.max_positions}）</h3>` +
    (P.length ? swingTradesTable(P) +
      `<div class="notice small">持倉規則：停損 ${P.map((p) => `${p.code} ${fmt2(p.stop)}`).join("、")}；
       第 ${EX.min_hold_days} 日起停利 ${P.map((p) => `${p.code} ${fmt2(p.target)}`).join("、")}；最多持有 ${EX.max_hold_days} 日。
       ${P.some((p) => p.lock) ? "⚠️ 有部位跌停鎖死，依規則次日開盤出場。" : ""}</div>`
      : `<div class="notice small">目前無持倉。</div>`);

  // 總覽與事件
  const S = sw.summary || {};
  sumBox.innerHTML = swingStatCards(S, `<div class="stat"><div class="lb">事件 / 閘門日</div><div class="v">${S.n_events || 0} / ${S.n_gate_days || 0}</div></div>
    <div class="stat"><div class="lb">最高同時持倉（資金需求）</div><div class="v">${S.peak_open || 0} 檔 ≈ ${((S.peak_open || 0) * R.position_pct)}%</div></div>`);
  const EV = sw.events || [];
  evBox.innerHTML = EV.length ? EV.map((e, i) => {
    const st = e.stats || {};
    const line = st.n_closed ? `勝率 ${st.win_rate}%・達標 ${st.hit_rate}%・單筆均 <span class="${signCls(st.mean_ret_pct)}">${pctTxt(st.mean_ret_pct)}</span>・權益貢獻 <span class="${signCls(st.contrib_pct)}">${pctTxt(st.contrib_pct)}</span>` : "尚未平倉";
    const dayRows = e.days.map((d) => `<tr><td class="lf">${d.date}</td><td>${d.tier}</td><td class="${signCls(d.mkt5)}">${pctTxt(d.mkt5)}</td><td class="${signCls(d.ew)}">${pctTxt(d.ew)}</td><td>${d.n_signals}</td><td>${d.n_taken}${d.cap != null ? `<div class="plan muted">上限 ${d.cap}・既有 ${d.open_before ?? 0}</div>` : ""}</td><td class="lf">${d.breaker ? "🚧" : ""}</td></tr>`).join("");
    return `<details class="day-block"${i === 0 ? " open" : ""}>
      <summary><span class="d">事件 #${e.id} ${e.start}${e.end !== e.start ? ` ～ ${e.end}` : ""}</span>
        <span class="s">閘門 ${e.days.length} 日・訊號 ${e.n_signals}・進場 ${e.n_taken}（買不到 ${e.n_unfilled}）</span>
        <span class="pnl ${signCls(st.contrib_pct)}">${st.n_closed ? pctTxt(st.contrib_pct) : ""}</span></summary>
      <div class="notice small" style="margin:0 10px 10px">${line}${st.n_closed ? `・持有 ≥5 日 ${st.hold5_rate}%・1 張合計 <span class="${signCls(st.net_ntd)}">${signTxt(st.net_ntd)}${fmt(st.net_ntd)}</span>` : ""}</div>
      <div class="tbl-wrap"><table class="trades"><thead><tr><th class="lf">閘門日</th><th>層級</th><th>等權 5 日</th><th>當日</th><th>訊號</th><th>取用</th><th class="lf"></th></tr></thead><tbody>${dayRows}</tbody></table></div>
      ${swingTradesTable(e.trades)}
    </details>`;
  }).join("") : `<div class="empty">尚無事件（資料期間內市場未出現急跌）。</div>`;

  explain.innerHTML = `
    <div class="s-item"><b>為什麼平時沒有建議單？</b><p>3 年全市場日K事件研究：市場 5 日報酬 ≥0 時，追漲停、突破 60／120 日新高、急跌反彈的 10 日期望值都是負的；
      優勢幾乎全在市場急跌後的反彈日（閘門內「深跌後漲停」勝率 95%、單筆均 +13%、+15% 達標 62%，2024／2025／2026 每年皆正）。</p></div>
    <div class="s-item"><b>閘門</b><p>流動股（20 日均成交值 ≥5,000 萬、股價 10～1,000）等權日報酬近 5 日累計 ≤ ${G.mkt5_le}%（T1 深跌），或 ≤ ${G.mkt5_le_rebound}% 且當日 ≥ +${G.rebound_day_ge}%（T2 反彈確認）。
      <b>T1 與 T2 不對等</b>：3 年引擎 A/B（4 次事件、槽位實際取用）T2 日的單 +17%／勝率 94%／回撤 −0.7%，T1 日的單 +2.6%／勝率 43% 且佔走槽位，反彈確認日只剩 1～2 槽——
      故 T1 日${G.t1_setups?.length ? `只出 ${G.t1_setups.join("/")}` : "不限型態"}${G.t1_max_new ? `、新進場最多 ${G.t1_max_new} 檔` : ""}（三年 +51%→+55%、回撤 −10.7%→−5.3%）。${G.t1_enabled === false ? "目前 T1 已關閉：只在反彈確認日出單（三年 +61.6%／回撤 −0.7%，但會錯過沒有大反彈日的事件，如 2026-03-09）。" : "若改 `swing.gate.t1_enabled=false` 只留反彈確認日：三年 +61.6%／回撤 −0.7%，代價是錯過沒有大反彈日的事件（如 2026-03-09）。"}</p></div>
    <div class="s-item"><b>型態與排序</b><p>${Object.values(R.setups).map((s) => `${s.name}：${s.desc}`).join("；")}。同股同日兩者皆符合以前者計。<b>排序＝綜合分</b>：同日分位數加權「離 60 日高深度（被殺最深的先反彈，ρ +0.38）／量比低（量能失控的反彈日反而弱，ρ −0.17）／20 日均成交值大（閘門內 ρ +0.25）」；
      法人買賣超經檢驗無排序力（ρ ≈ 0）、20 日報酬與深度重疊、融資增減偏噪音，皆不採用。日內排序力本就有限（ρ 0.1～0.2）——閘門對不對遠比挑哪幾檔重要。
      排除處置股、ETF、持倉中或 ${R.dedup_days} 日內剛交易過的股票。</p></div>
    <div class="s-item"><b>進出場與模擬</b><p>隔日開盤市價進場，開盤即漲停視為買不到；停損 −${EX.stop_pct}%（跳空以開盤價、跌停鎖死順延）；第 ${EX.min_hold_days} 日起 +${EX.target_pct}% 停利；第 ${EX.max_hold_days} 日收盤時間出場。
      日K悲觀順序（同日先停損）。成本：手續費 ×2 ＋ 證交稅 0.3%（非當沖）。最多 ${R.max_positions} 檔、每檔權益 ${R.position_pct}%（${R.max_positions} 檔滿倉＝${R.max_positions * R.position_pct}%，可於 \`swing.position_pct\` 調整）${R.t2_max_positions > R.max_positions ? `；反彈確認日（T2）槽位可擴大到 ${R.t2_max_positions} 檔，需預留這部分資金` : ""}。</p></div>
    <div class="s-item"><b>限制</b><p>樣本只有 4 次 V 型反彈（2024-08、2025-04、2026-03、2026-07），沒有長空頭；反彈日隔天部分股票鎖漲停買不到，實際容量比表格小。
      完整研究：<a href="https://github.com/kenhuangads/stock-pick/blob/main/docs/swing_research.md" target="_blank" rel="noopener">docs/swing_research.md</a>。</p></div>`;
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
