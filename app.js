/* 台股槓桿監測 — 前端（不需要建置工具） */
(function () {
  "use strict";

  const $ = (s, el) => (el || document).querySelector(s);
  const view = $("#view");
  const WK = "日一二三四五六";
  const st = {
    market: null, stocks: null, stocksPromise: null,
    mkt: localStorage.getItem("mkt") || "all", range: localStorage.getItem("range") || "90",
    watch: readWatch(), query: "",
  };

  // ---------- 小工具
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
  function fmt(n, d = 1) { return n == null || isNaN(n) ? "—" : Number(n).toLocaleString("zh-TW", { minimumFractionDigits: d, maximumFractionDigits: d }); }
  function int(n) { return n == null ? "—" : Math.round(n).toLocaleString("zh-TW"); }
  function sgn(n, d = 1) { if (n == null || isNaN(n)) return "—"; return (n > 0 ? "▲ " : n < 0 ? "▼ " : "") + fmt(Math.abs(n), d); }
  function cls(n) { return n > 0 ? "up" : n < 0 ? "down" : ""; }
  function parseD(s) { return new Date(+s.slice(0, 4), +s.slice(4, 6) - 1, +s.slice(6, 8)); }
  function md(s) { return `${+s.slice(4, 6)}/${+s.slice(6, 8)}`; }
  function wd(s) { return "週" + WK[parseD(s).getDay()]; }
  function zone(r) { return r == null ? null : r < 130 ? ["danger", "危險"] : r < 150 ? ["warn", "警戒"] : ["safe", "安全"]; }
  function fearLabel(x) { return x == null ? "—" : x < 20 ? "樂觀" : x < 40 ? "平穩" : x < 60 ? "謹慎" : x < 80 ? "緊張" : "恐慌"; }
  function readWatch() { try { return JSON.parse(localStorage.getItem("watch") || "[]"); } catch (e) { return []; } }
  function saveWatch() { try { localStorage.setItem("watch", JSON.stringify(st.watch)); } catch (e) { /* 無痕模式 */ } }
  function remember(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* ignore */ } }

  // ---------- 資料
  async function getJSON(url) {
    const r = await fetch(url + "?t=" + Math.floor(Date.now() / 300000), { cache: "no-cache" });
    if (!r.ok) throw new Error(url + " " + r.status);
    return r.json();
  }
  async function loadMarket() { if (!st.market) st.market = await getJSON("data/market.json"); return st.market; }
  function loadStocks() {
    if (!st.stocksPromise) st.stocksPromise = getJSON("data/stocks.json").then(j => (st.stocks = j));
    return st.stocksPromise;
  }

  const MK = { all: ["allRatio", "allAmt", "全市場"], tw: ["twRatio", "twAmt", "上市"], otc: ["otcRatio", "otcAmt", "上櫃"] };

  // ---------- 圖形
  function gaugeSVG(v) {
    const lo = 120, hi = 200, cx = 150, cy = 150, R = 120;
    const pt = x => { const a = Math.PI * (1 - (Math.min(hi, Math.max(lo, x)) - lo) / (hi - lo)); return [cx + R * Math.cos(a), cy - R * Math.sin(a)]; };
    const arc = (a, b, color) => { const p = pt(a), q = pt(b); return `<path d="M${p[0].toFixed(1)} ${p[1].toFixed(1)} A${R} ${R} 0 0 1 ${q[0].toFixed(1)} ${q[1].toFixed(1)}" fill="none" stroke="${color}" stroke-width="6" stroke-linecap="round"/>`; };
    const m = pt(v == null ? lo : v);
    return `<svg viewBox="0 0 300 160" role="img" aria-label="維持率儀表 ${v == null ? "無資料" : fmt(v) + "%"}">
      ${arc(120, 129, "var(--z-danger)")}${arc(131, 149, "var(--z-warn)")}${arc(151, 200, "var(--z-safe)")}
      ${v == null ? "" : `<circle cx="${m[0].toFixed(1)}" cy="${m[1].toFixed(1)}" r="9" fill="var(--card)" stroke="var(--ink)" stroke-width="3"/>`}
    </svg>`;
  }

  function spark(vals, h = 48) {
    const v = vals.filter(x => x != null);
    if (v.length < 2) return `<p class="tiny">資料累積中</p>`;
    const mn = Math.min(...v), mx = Math.max(...v), sp = (mx - mn) || 1;
    const pts = v.map((x, i) => `${(i / (v.length - 1) * 300).toFixed(1)},${(4 + (1 - (x - mn) / sp) * (h - 8)).toFixed(1)}`).join(" ");
    return `<svg viewBox="0 0 300 ${h}" preserveAspectRatio="none" width="100%" height="${h}" aria-hidden="true"><polyline fill="none" stroke="var(--accent)" stroke-width="1.8" stroke-linejoin="round" vector-effect="non-scaling-stroke" points="${pts}"/></svg>`;
  }

  const RATIO_CHART = {
    id: "r", unit: "%", label: "維持率",
    domain: vals => [Math.min(128, ...vals) - 2, Math.max(170, ...vals) + 2],
    lines: [[166, "166 原始", "var(--line)"], [150, "150 警戒", "var(--z-warn)"], [130, "130 追繳", "var(--z-danger)"]],
    read: (r, v) => `${fmt(v)}%`,
  };
  const FEAR_CHART = {
    id: "f", unit: "", label: "恐慌溫度",
    domain: () => [0, 100],
    lines: [[80, "80 恐慌", "var(--z-danger)"], [60, "60 緊張", "var(--z-warn)"], [40, "40 謹慎", "var(--line)"], [20, "20 平穩", "var(--line)"]],
    read: (r, v) => `${Math.round(v)} ${fearLabel(v)}${r.vix ? "・VIX " + fmt(r.vix, 2) : ""}`,
  };

  function chartHTML(rows, key, o) {
    const data = rows.filter(r => r[key] != null);
    if (data.length < 2) return `<p class="tiny">資料累積中</p>`;
    const vals = data.map(r => r[key]);
    const [mn, mx] = o.domain(vals);
    const W = 600, H = 200, y = v => (1 - (v - mn) / (mx - mn)) * H, x = i => i / (data.length - 1) * W;
    const pts = data.map((r, i) => `${x(i).toFixed(1)},${y(r[key]).toFixed(1)}`).join(" ");
    const lines = o.lines.map(([v, t, c]) => ({ v, t, c, top: y(v) / H * 100 }));
    return `<div class="chart" id="chart-${o.id}">
      <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="${esc(md(data[0].d))} 到 ${esc(md(data[data.length - 1].d))} ${o.label}走勢，最新 ${fmt(vals[vals.length - 1])}${o.unit}">
        ${lines.map(l => `<line x1="0" x2="${W}" y1="${y(l.v).toFixed(1)}" y2="${y(l.v).toFixed(1)}" stroke="${l.c}" stroke-dasharray="4 5" vector-effect="non-scaling-stroke"/>`).join("")}
        <polyline fill="none" stroke="var(--accent)" stroke-width="2" stroke-linejoin="round" vector-effect="non-scaling-stroke" points="${pts}"/>
        <line class="hl" x1="0" x2="0" y1="0" y2="${H}" stroke="var(--muted)" stroke-width="1" vector-effect="non-scaling-stroke" opacity="0"/>
      </svg>
      <div class="gutter">${lines.map(l => `<span class="tiny" style="top:${l.top.toFixed(1)}%">${l.t}</span>`).join("")}</div>
    </div>
    <div class="card-head tiny" style="padding-right:72px"><span>${md(data[0].d)}</span><span id="read-${o.id}" aria-live="polite"></span><span>${md(data[data.length - 1].d)}</span></div>`;
  }

  function bindChartHover(rows, key, o) {
    const wrap = $("#chart-" + o.id); if (!wrap) return;
    const box = $("svg", wrap), line = $(".hl", wrap), read = $("#read-" + o.id);
    const data = rows.filter(r => r[key] != null);
    const move = e => {
      const rect = box.getBoundingClientRect();
      const t = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width));
      const i = Math.round(t * (data.length - 1));
      const xx = i / (data.length - 1) * 600;
      line.setAttribute("x1", xx); line.setAttribute("x2", xx); line.setAttribute("opacity", ".5");
      read.textContent = `${md(data[i].d)}　${o.read(data[i], data[i][key])}`;
    };
    box.addEventListener("pointermove", move);
    box.addEventListener("pointerdown", move);
    box.addEventListener("pointerleave", () => { line.setAttribute("opacity", "0"); read.textContent = ""; });
  }

  // ---------- 畫面：總覽
  async function renderOverview() {
    const m = await loadMarket();
    const days = m.days;
    if (!days.length) { view.innerHTML = `<div class="empty">還沒有資料，第一次抓取完成後就會出現。</div>`; return; }
    const last = days[days.length - 1], prev = days[days.length - 2] || {};
    const [rk, ak, mname] = MK[st.mkt];
    const r = last[rk], rPrev = prev[rk], amt = last[ak], amtPrev = prev[ak];
    const off = last.official || [...days].reverse().find(d => d.official)?.official;
    const offDay = last.official ? last.d : [...days].reverse().find(d => d.official)?.d;
    const offPrev = (() => { const ds = days.filter(d => d.official); return ds.length > 1 ? ds[ds.length - 2].official.keepRate : null; })();
    const fear = last.fear, fp = last.fearParts || {};
    const rangeN = st.range === "all" ? days.length : +st.range;
    const rows = days.slice(-rangeN);
    const vixPrev = prev.vix;
    const rangeSeg = `<div class="seg small" role="group" aria-label="期間">
            ${[["30", "30 日"], ["90", "90 日"], ["all", "全部"]].map(([k, t]) => `<button data-range="${k}" aria-pressed="${st.range === k}">${t}</button>`).join("")}
          </div>`;

    view.innerHTML = `
      <div class="seg" role="group" aria-label="市場">
        ${Object.entries(MK).map(([k, v]) => `<button data-mkt="${k}" aria-pressed="${st.mkt === k}">${v[2]}</button>`).join("")}
      </div>
      <div class="grid cols3">
        <section class="card">
          <div class="card-head"><span class="label">大盤融資維持率</span><span class="tiny">${mname} · 民間算法</span></div>
          <div class="gauge">
            ${gaugeSVG(r)}
            <div class="readout">
              <span class="big">${fmt(r)}<span class="unit">%</span></span>
              <span class="tiny ${cls(r - rPrev)}">${rPrev != null ? sgn(r - rPrev) + " 較前日" : ""}</span>
            </div>
            <div class="legend"><span><i style="background:var(--z-danger)"></i>130 以下 追繳</span><span><i style="background:var(--z-warn)"></i>130–150 警戒</span><span><i style="background:var(--z-safe)"></i>150 以上</span></div>
          </div>
        </section>

        <section class="card">
          <div class="card-head"><span class="label">官方全市場擔保維持率</span><span class="tiny">${offDay ? md(offDay) + " · " : ""}臺股儀表板</span></div>
          <span class="big">${fmt(off && off.keepRate, 2)}<span class="unit">%</span></span>
          <span class="tiny ${off && offPrev != null ? cls(off.keepRate - offPrev) : ""}">${off && offPrev != null ? sgn(off.keepRate - offPrev, 2) + " 較前日" : "整戶計算（融資＋融券，上市＋上櫃）"}</span>
          <div class="facts">
            <div><span class="tiny">${mname}融資餘額</span><b>${fmt(amt / 1e8, 0)} 億</b></div>
            <div><span class="tiny">餘額增減</span><b class="${cls(amt - amtPrev)}">${amtPrev ? sgn((amt - amtPrev) / 1e8, 0) + " 億" : "—"}</b></div>
            <div><span class="tiny">追繳戶數</span><b>${off ? int(off.callAcc) : "—"}</b></div>
            <div><span class="tiny">處分戶數</span><b>${off ? int(off.exeAcc) : "—"}</b></div>
          </div>
        </section>

        <section class="card">
          <div class="card-head"><span class="label">恐慌溫度</span><span class="tiny">台指 VIX <b style="color:var(--ink);font-weight:500">${fmt(last.vix, 2)}</b> <span class="${cls(last.vix - vixPrev)}">${vixPrev ? sgn(last.vix - vixPrev, 2) : ""}</span></span></div>
          <div style="display:flex;align-items:baseline;gap:10px"><span class="big">${fear == null ? "—" : Math.round(fear)}</span><span style="font-size:15px;font-weight:500">${fearLabel(fear)}</span></div>
          <div class="fearbar" aria-hidden="true">
            <i style="background:var(--f1)"></i><i style="background:var(--f2)"></i><i style="background:var(--f3)"></i><i style="background:var(--f4)"></i><i style="background:var(--f5)"></i>
            ${fear == null ? "" : `<span class="marker" style="left:${Math.min(99, Math.max(1, fear))}%"></span>`}
          </div>
          <div class="scale" style="margin-top:2px"><span>樂觀</span><span>謹慎</span><span>恐慌</span></div>
          <div class="facts">
            <div><span class="tiny">加權指數</span><b>${fmt(last.taiex, 0)} <span class="tiny ${cls(last.taiexChg)}">${last.taiexPct != null ? (last.taiexPct > 0 ? "+" : "") + fmt(last.taiexPct, 2) + "%" : ""}</span></b></div>
            <div><span class="tiny">組成</span><b class="tiny" style="font-size:12px;line-height:1.6">VIX ${fp.vix ?? "—"}・維持率 ${fp.ratio ?? "—"}<br>融資 ${fp.margin ?? "—"}・趨勢 ${fp.trend ?? "—"}</b></div>
          </div>
        </section>
      </div>

      <div class="grid cols2">
      <section class="card">
        <div class="card-head" style="flex-wrap:wrap">
          <h2 class="section">恐慌溫度走勢</h2>
          ${rangeSeg}
        </div>
        ${chartHTML(rows, "fear", FEAR_CHART)}
      </section>

      <section class="card">
        <div class="card-head" style="flex-wrap:wrap">
          <h2 class="section">${mname}維持率走勢</h2>
          ${rangeSeg}
        </div>
        ${chartHTML(rows, rk, RATIO_CHART)}
      </section>
      </div>
      <p class="note">民間算法＝融資股票市值 ÷ 融資金額，反映單純做多的融資戶壓力；官方數字以整戶計算，兩者定義不同。資料為盤後公布，非即時。</p>`;

    view.querySelectorAll("[data-mkt]").forEach(b => b.onclick = () => { st.mkt = b.dataset.mkt; remember("mkt", st.mkt); renderOverview(); });
    view.querySelectorAll("[data-range]").forEach(b => b.onclick = () => { st.range = b.dataset.range; remember("range", st.range); renderOverview(); });
    bindChartHover(rows, "fear", FEAR_CHART);
    bindChartHover(rows, rk, RATIO_CHART);
  }

  // ---------- 畫面：個股
  function stockRow(code, it) {
    const z = zone(it[7]);
    return `<a href="#/stock/${encodeURIComponent(code)}">
      <div style="display:flex;flex-direction:column;gap:2px;min-width:0"><span class="name">${esc(it[0])}</span><span class="tiny">${esc(code)} · ${it[1] === "TW" ? "上市" : "上櫃"}</span></div>
      <div style="display:flex;align-items:center;gap:10px"><span style="font-size:16px">${it[7] == null ? "—" : fmt(it[7]) + "%"}</span>${z ? `<span class="pill ${z[0]}">${z[1]}</span>` : ""}</div>
    </a>`;
  }

  function searchStocks(q) {
    const items = st.stocks.items; q = q.trim().toUpperCase();
    if (!q) return [];
    const out = [];
    for (const [c, it] of Object.entries(items)) {
      if (c.startsWith(q)) out.push([0, c.length, c, it]);
      else if (it[0].toUpperCase().includes(q)) out.push([1, it[0].length, c, it]);
    }
    return out.sort((a, b) => a[0] - b[0] || a[1] - b[1] || a[2].localeCompare(b[2])).slice(0, 8).map(x => [x[2], x[3]]);
  }

  async function renderStock(code) {
    view.innerHTML = `<div class="empty">載入個股資料…</div>`;
    const s = await loadStocks();
    if (code) st.query = "";
    const it = code ? s.items[code] : null;
    view.innerHTML = `
      <div style="display:flex;flex-direction:column;gap:6px">
        <label for="q" class="tiny">股票代號或名稱</label>
        <div class="search">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="var(--muted)" stroke-width="1.8" aria-hidden="true"><circle cx="11" cy="11" r="6"></circle><path d="M20 20l-4.5-4.5"></path></svg>
          <input id="q" type="search" inputmode="search" autocomplete="off" enterkeyhint="search" placeholder="例如 2330 或 台積電" value="${esc(code || st.query || "")}">
        </div>
      </div>
      <div id="sugg"></div>
      <div id="detail">${code ? (it ? detailHTML(code, it, s.d) : `<div class="empty">找不到 ${esc(code)}，它可能不是融資標的。</div>`) : `<div class="empty">輸入代號或名稱查詢估算融資維持率。<br>只有可以融資的股票與 ETF 才有資料。</div>`}</div>`;
    const q = $("#q"), sugg = $("#sugg");
    const show = () => {
      st.query = q.value;
      const res = searchStocks(q.value);
      const same = res.length === 1 && res[0][0] === code;
      sugg.innerHTML = res.length && !same ? `<div class="sugg">${res.map(([c, i]) => stockRow(c, i)).join("")}</div>` : (q.value.trim() && !res.length ? `<div class="empty">沒有符合的融資標的</div>` : "");
    };
    q.addEventListener("input", show);
    q.addEventListener("keydown", e => { if (e.key === "Enter") { const r = searchStocks(q.value); if (r.length) { st.query = ""; location.hash = "#/stock/" + r[0][0]; } } });
    if (!code) { q.focus(); if (st.query) show(); }
    bindStar(code);
  }

  function detailHTML(code, it, d) {
    const [name, mkt, close, chg, bal, balChg, avg, ratio, hist] = it;
    const z = zone(ratio);
    const pos = ratio == null ? null : Math.min(99, Math.max(1, (ratio - 120) / 130 * 100));
    const pct = close && chg != null && close - chg ? chg / (close - chg) * 100 : null;
    const on = st.watch.includes(code);
    return `<section class="card">
      <div class="card-head" style="align-items:flex-start">
        <div style="display:flex;flex-direction:column;gap:2px"><span style="font-size:18px;font-weight:600">${esc(name)}</span><span class="tiny">${esc(code)} · ${mkt === "TW" ? "上市" : "上櫃"} · ${md(d)} 收盤</span></div>
        <button class="star" id="star" aria-pressed="${on}" aria-label="${on ? "從自選移除" : "加入自選"}"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4l2.4 5 5.4.6-4 3.7 1.1 5.3L12 16l-4.9 2.6 1.1-5.3-4-3.7 5.4-.6z"/></svg></button>
      </div>
      <div class="card-head" style="align-items:flex-end">
        <div style="display:flex;flex-direction:column;gap:4px"><span class="tiny">估算融資維持率</span><span class="big" style="font-size:40px">${fmt(ratio)}<span class="unit">%</span></span></div>
        <div style="display:flex;flex-direction:column;align-items:flex-end;gap:4px"><span style="font-size:20px">${fmt(close, close >= 100 ? 1 : 2)}</span><span class="tiny ${cls(chg)}">${sgn(chg, 2)}${pct != null ? `（${pct > 0 ? "+" : ""}${fmt(pct, 2)}%）` : ""}</span>${z ? `<span class="pill ${z[0]}">${z[1]}</span>` : ""}</div>
      </div>
      <div style="display:flex;flex-direction:column;gap:6px">
        <div class="linebar" aria-hidden="true">
          <i style="width:7.7%;background:var(--z-danger);border-radius:3px 0 0 3px"></i><i style="width:15.4%;background:var(--z-warn)"></i><i style="flex:1;background:var(--z-safe);border-radius:0 3px 3px 0"></i>
          ${pos == null ? "" : `<span class="marker" style="left:${pos}%"></span>`}
        </div>
        <div class="scale" style="margin-top:0"><span>120</span><span>130</span><span>150</span><span>250</span></div>
      </div>
      <div class="facts">
        <div><span class="tiny">融資餘額</span><b>${int(bal)} 張</b></div>
        <div><span class="tiny">今日增減</span><b>${balChg > 0 ? "+" : ""}${int(balChg)} 張</b></div>
        <div><span class="tiny">估算融資均價</span><b>${fmt(avg, avg >= 100 ? 1 : 2)}</b></div>
        <div><span class="tiny">融資市值</span><b>${fmt(bal * 1000 * close / 1e8, 2)} 億</b></div>
      </div>
      <div style="display:flex;flex-direction:column;gap:6px"><span class="tiny">近 ${hist.length} 日估算維持率</span>${spark(hist)}</div>
      <p class="note">估算值：以每日融資增減張數與收盤價推算融資均價，再用官方融資總額校正，不是券商實際帳務。券商追繳是看整戶維持率，單一個股低於 130% 不代表會被追繳。</p>
    </section>`;
  }

  function bindStar(code) {
    const b = $("#star"); if (!b) return;
    b.onclick = () => {
      const i = st.watch.indexOf(code);
      if (i >= 0) st.watch.splice(i, 1); else st.watch.push(code);
      saveWatch();
      const on = st.watch.includes(code);
      b.setAttribute("aria-pressed", on); b.setAttribute("aria-label", on ? "從自選移除" : "加入自選");
    };
  }

  // ---------- 畫面：自選
  async function renderWatch() {
    view.innerHTML = `<div class="empty">載入中…</div>`;
    const s = await loadStocks();
    const list = st.watch.filter(c => s.items[c]);
    const sorted = [...list].sort((a, b) => (s.items[a][7] ?? 999) - (s.items[b][7] ?? 999));
    view.innerHTML = `
      <div class="card-head"><h2 class="section">自選清單</h2><span class="tiny">${md(s.d)} · 依維持率由低到高</span></div>
      ${sorted.length ? `<div class="list">${sorted.map(c => stockRow(c, s.items[c])).join("")}</div>`
        : `<div class="list"><div class="empty">還沒有自選股。<br>到「個股」查詢後按右上角的星號加入。</div></div>`}
      <p class="note">自選清單只存在這台裝置的瀏覽器裡。</p>`;
  }

  // ---------- 畫面：說明
  async function renderAbout() {
    const m = await loadMarket().catch(() => null);
    view.innerHTML = `<section class="card prose">
      <h2 class="section">指標怎麼算</h2>
      <p><b>大盤融資維持率（民間算法）</b>：所有融資股票的市值 ÷ 融資金額。上市、上櫃用證交所與櫃買中心每日公布的融資餘額與收盤價計算。這個數字反映單純做多的融資戶壓力，和過去財經網站公布的算法相同。</p>
      <p><b>官方全市場擔保維持率</b>：來自證交所「臺股儀表板」，用券商實際帳務以整戶（融資＋融券）計算，涵蓋上市與上櫃，通常比民間算法高。</p>
      <p><b>個股估算維持率</b>：交易所只公布融資張數，沒有公布成本。這裡用每日新增融資張數 × 當天收盤價推算平均成本，再用官方融資總額校正，所以是估計值。</p>
      <p><b>恐慌溫度（0–100）</b>：台指 VIX 占 40%、全市場融資維持率 25%、加權指數偏離 20 日均線 20%、融資餘額 5 日變化 15%。越高代表市場越恐慌。期交所只提供近 4 個月的 VIX 檔案，所以恐慌溫度從 8 月開始才有歷史。</p>
      <p><b>門檻</b>：166% 約為原始維持率（融資 6 成），150% 是券商常見的預警線，130% 是追繳線。</p>
    </section>
    <section class="card prose">
      <h2 class="section">資料來源與更新</h2>
      <p>臺灣證券交易所、證券櫃檯買賣中心、臺灣期貨交易所公開資料。每天盤後自動更新（約晚上 9 點半後與隔天早上各一次）。</p>
      <p class="tiny">最後更新：${m ? esc(m.updated.replace("T", " ")) : "—"}・共 ${m ? m.days.length : 0} 個交易日</p>
      <p class="tiny">本工具僅供個人參考，不構成投資建議。</p>
    </section>`;
  }

  // ---------- 路由
  function setNav(name) {
    document.querySelectorAll("[data-nav]").forEach(a => a.dataset.nav === name ? a.setAttribute("aria-current", "page") : a.removeAttribute("aria-current"));
  }
  async function route() {
    const h = location.hash.replace(/^#\/?/, "");
    const [p, arg] = h.split("/");
    const name = p === "stock" ? "stock" : p === "watch" ? "watch" : p === "about" ? "about" : "overview";
    setNav(name);
    $("#title").textContent = { overview: "台股槓桿監測", stock: "個股查詢", watch: "自選清單", about: "說明" }[name];
    try {
      if (name === "stock") await renderStock(arg ? decodeURIComponent(arg).toUpperCase() : "");
      else if (name === "watch") await renderWatch();
      else if (name === "about") await renderAbout();
      else await renderOverview();
    } catch (e) {
      console.error(e);
      view.innerHTML = `<div class="err">資料讀取失敗，請稍後重新整理。（${esc(e.message)}）</div>`;
    }
    window.scrollTo(0, 0);
  }

  async function header() {
    try {
      const m = await loadMarket();
      const d = m.days.length ? m.days[m.days.length - 1].d : null;
      $("#asof").textContent = d ? `${md(d)} ${wd(d)} · 盤後資料` : "尚無資料";
    } catch (e) { $("#asof").textContent = "離線"; }
  }

  // ---------- 主題
  function applyTheme(t) { if (t) document.documentElement.dataset.theme = t; else delete document.documentElement.dataset.theme; }
  applyTheme(localStorage.getItem("theme"));
  $("#themeBtn").onclick = () => {
    const dark = getComputedStyle(document.documentElement).colorScheme.includes("dark");
    const t = dark ? "light" : "dark";
    applyTheme(t); remember("theme", t);
  };

  window.addEventListener("hashchange", route);
  header(); route();
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(() => {});
})();
