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
function tradeNet(fill, exit, lots) {
  const sh = lots * 1000;
  const disc = settings.discount / 10;
  const feeB = Math.max(Math.floor(fill * sh * FEE_RATE * disc), settings.minFee);
  const feeS = Math.max(Math.floor(exit * sh * FEE_RATE * disc), settings.minFee);
  const tax = Math.floor(exit * sh * DT_TAX);
  const gross = Math.trunc((exit - fill) * sh);
  return { gross, fees: feeB + feeS + tax, net: gross - feeB - feeS - tax };
}

/* ---------- 資料載入 ---------- */
let DB = { picks: null, market: null, reviews: null, strategies: null };
let stratMeta = {}; // id -> {name, desc}

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
    DB = { picks, market, reviews, strategies };
    (market.strategies || []).forEach((s) => (stratMeta[s.id] = s));
    Object.entries(strategies.meta || {}).forEach(([id, m]) => (stratMeta[id] = { id, ...m }));
    $("#dataDate").textContent = `資料日：${market.date} · 更新：${(market.updated_at || "").slice(0, 16).replace("T", " ")}`;
    renderPicks();
    initCustomForm();
    renderReview();
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
function stockCard(p, rank) {
  const tags = (p.strategies || [])
    .map((id) => `<span class="tag" title="${stratMeta[id]?.desc || ""}">${stratMeta[id]?.name || id}</span>`)
    .join("");
  const chg = p.chg_pct;
  return `<div class="card">
    <div class="head">
      ${rank ? `<span class="rank">#${rank}</span>` : ""}
      <span class="code">${p.code}</span><span class="name">${p.name}</span>
      <span class="mkt">${p.market === "tpex" ? "上櫃" : "上市"}</span>
      <span class="score" title="綜合分數（策略權重加總）">${p.score}</span>
    </div>
    <div class="px">收盤 <b>${fmt2(p.close)}</b>
      <span class="${signCls(chg)}">${signTxt(chg)}${fmt2(chg)}%</span></div>
    <div class="tags">${tags || '<span class="muted small">未觸發計分策略</span>'}</div>
    <div class="price-grid">
      <div><div class="lb">建議買進 NL</div><div class="v buy">${fmt2(p.entry ?? p.cdp?.nl)}</div></div>
      <div><div class="lb">停利 NH</div><div class="v tp">${fmt2(p.target ?? p.cdp?.nh)}</div></div>
      <div><div class="lb">停損 AL</div><div class="v sl">${fmt2(p.stop ?? p.cdp?.al)}</div></div>
      <div><div class="lb">順勢突破 AH</div><div class="v ah">${fmt2(p.ah ?? p.cdp?.ah)}</div></div>
    </div>
    <div class="meta">
      <span>月均振幅 ${fmt2(p.amp_avg)}%</span>
      <span>成交 ${fmt(p.vol_lots)} 張</span>
      ${p.dt_ratio != null ? `<span>當沖率 ${fmt2(p.dt_ratio)}%</span>` : ""}
      ${p.breakeven_ticks != null ? `<span>回本約 ${p.breakeven_ticks} 檔</span>` : ""}
    </div>
  </div>`;
}

/* ---------- Tab 1 今日選股 ---------- */
function renderPicks() {
  const d = DB.picks;
  const n = d.picks?.length || 0;
  $("#picksInfo").innerHTML = n
    ? `📅 <b>${d.generated_on}</b> 收盤後產生 · 適用<b>下一交易日</b>盤中 · 共 <b>${n}</b> 檔
       <br>已排除處置股／注意股／非當沖標的／流動性與波動不足者，依策略權重綜合評分排序。`
    : `本日基礎濾網後沒有符合門檻的標的（或歷史資料尚在累積）。可到「自訂選股」放寬條件。`;
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

/* ---------- Tab 3 每日復盤 ---------- */
let pnlChart;
function recomputeDay(day) {
  const lots = settings.lots;
  let net = 0, gross = 0, fees = 0, wins = 0, filled = 0;
  const rows = day.picks.map((p) => {
    if (!p.filled) return { ...p, _net: null };
    const r = tradeNet(p.fill_price, p.exit_price, lots);
    filled++; net += r.net; gross += r.gross; fees += r.fees;
    if (r.net > 0) wins++;
    return { ...p, _net: r.net, _fees: r.fees };
  });
  return { rows, net, gross, fees, wins, filled,
    winRate: filled ? (wins / filled) * 100 : null };
}
function renderReview() {
  const days = [...DB.reviews].sort((a, b) => a.date.localeCompare(b.date));
  if (!days.length) {
    $("#reviewStats").innerHTML = "";
    $("#reviewList").innerHTML = `<div class="empty">尚無復盤紀錄。系統每天收盤後會自動用實際行情驗證前一日的建議單。</div>`;
    return;
  }
  const daily = days.map((d) => ({ date: d.date, ...recomputeDay(d), raw: d }));
  let cum = 0;
  const cumSeries = daily.map((d) => (cum += d.net));
  const totNet = cum, totFilled = daily.reduce((s, d) => s + d.filled, 0);
  const totWins = daily.reduce((s, d) => s + d.wins, 0);

  $("#reviewStats").innerHTML = `
    <div class="stat"><div class="lb">累計淨損益</div><div class="v ${signCls(totNet)}">${signTxt(totNet)}${fmt(totNet)}</div></div>
    <div class="stat"><div class="lb">總勝率</div><div class="v">${totFilled ? ((totWins / totFilled) * 100).toFixed(1) : "–"}%</div></div>
    <div class="stat"><div class="lb">成交筆數</div><div class="v">${fmt(totFilled)}</div></div>
    <div class="stat"><div class="lb">復盤天數</div><div class="v">${daily.length}</div></div>`;

  const css = getComputedStyle(document.documentElement);
  const cUp = css.getPropertyValue("--up").trim(), cDown = css.getPropertyValue("--down").trim();
  chartWhenVisible("review", () => {
  if (pnlChart) pnlChart.destroy();
  pnlChart = new Chart($("#pnlChart"), {
    data: {
      labels: daily.map((d) => d.date.slice(5)),
      datasets: [
        { type: "line", label: "累計淨損益", data: cumSeries, borderColor: css.getPropertyValue("--gold").trim(),
          backgroundColor: "transparent", tension: 0.25, pointRadius: 0, borderWidth: 2, yAxisID: "y1" },
        { type: "bar", label: "單日淨損益", data: daily.map((d) => d.net),
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
    const reasonTxt = { target: "停利", stop: "停損", close: "收盤沖銷", nofill: "未成交" };
    const rows = d.rows.map((p) => {
      if (!p.filled) return `<tr class="dim"><td>${p.code} ${p.name}</td><td>${fmt2(p.entry)}</td>
        <td colspan="2">未成交（最低 ${fmt2(p.day_low)} 未觸價）</td><td>–</td><td>–</td></tr>`;
      return `<tr><td>${p.code} ${p.name}</td><td>${fmt2(p.fill_price)}</td>
        <td>${fmt2(p.exit_price)}</td><td>${reasonTxt[p.exit_reason] || p.exit_reason}</td>
        <td class="${signCls(p.ret_pct)}">${signTxt(p.ret_pct)}${fmt2(p.ret_pct)}%</td>
        <td class="${signCls(p._net)}">${signTxt(p._net)}${fmt(p._net)}</td></tr>`;
    }).join("");
    return `<details class="day-block" ${idx === 0 ? "open" : ""}>
      <summary><span class="d">${d.date}</span>
        <span class="s">${d.filled}/${d.raw.picks.length} 筆成交 · 勝率 ${d.winRate == null ? "–" : d.winRate.toFixed(0) + "%"}</span>
        <span class="pnl ${signCls(d.net)}">${signTxt(d.net)}${fmt(d.net)}</span></summary>
      <div class="tbl-wrap"><table class="trades">
        <tr><th>標的</th><th>進場</th><th>出場</th><th>原因</th><th>報酬</th><th>淨損益</th></tr>
        ${rows}</table></div>
    </details>`;
  }).join("");
}

/* ---------- Tab 4 策略績效 ---------- */
let stratChart;
function renderStrategies() {
  const d = DB.strategies;
  const stats = d.stats || {};
  $("#stratInfo").innerHTML = `評估窗口：最近 <b>${d.window_days}</b> 個復盤日（累計 ${d.review_days} 日） ·
    每日收盤後自動重算：期望值轉負的策略停用（汰弱），績效好的策略加權（留強），直接影響隔日「今日選股」排序。`;
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
      <span class="badge ${s.enabled ? "on" : "off"}">${s.enabled ? "啟用中" : "已停用"}</span>
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
  $("#settingsDlg").showModal();
});
$("#btnSaveSettings").addEventListener("click", () => {
  settings = {
    discount: Math.max(0.1, +$("#sDiscount").value || DEFAULT_SETTINGS.discount),
    minFee: Math.max(0, +$("#sMinFee").value || 0),
    lots: Math.max(1, Math.round(+$("#sLots").value || 1)),
  };
  localStorage.setItem("sp_settings", JSON.stringify(settings));
  $("#settingsDlg").close();
  if (DB.reviews) renderReview();
});
$("#btnCancelSettings").addEventListener("click", () => $("#settingsDlg").close());

boot();
