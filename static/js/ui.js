/**
 * ui.js — pure DOM rendering functions.
 * No fetch calls here; each function receives data and updates the DOM.
 */

/* ── Helpers ─────────────────────────────────────────────────────────────── */

function fmt(n, decimals = 2) {
  return (n ?? 0).toFixed(decimals);
}

function rupee(n) {
  return '₹' + fmt(n);
}

function signClass(n) {
  return n >= 0 ? 'pos' : 'neg';
}

function signStr(n) {
  return n >= 0 ? '+' : '';
}

function badgeClass(signal) {
  if (signal === 'BUY')  return 'badge-buy';
  if (signal === 'SELL') return 'badge-sell';
  return 'badge-hold';
}

/* ── Clock ───────────────────────────────────────────────────────────────── */

function renderClock() {
  const ist = new Date(new Date().toLocaleString('en-US', { timeZone: 'Asia/Kolkata' }));
  const pad = n => String(n).padStart(2, '0');
  document.getElementById('ist-clock').textContent =
    'IST ' + pad(ist.getHours()) + ':' + pad(ist.getMinutes()) + ':' + pad(ist.getSeconds());
}

/* ── Toast ───────────────────────────────────────────────────────────────── */

function toast(msg, type = '') {
  const el = document.createElement('div');
  el.className = 'toast ' + type;
  el.innerHTML = msg;
  document.getElementById('toast-container').appendChild(el);
  setTimeout(() => el.remove(), 4000);
}

/* ── Market badges ───────────────────────────────────────────────────────── */

function renderMarketBadges(data) {
  const container = document.getElementById('market-badges');
  container.innerHTML = Object.entries(data.markets || {}).map(([mkt, info]) => {
    const cls = info.open ? 'mkt-open' : 'mkt-closed';
    return `<div class="mkt-badge ${cls}"><span class="dot"></span>${mkt} ${info.local_time || ''}</div>`;
  }).join('');
}

/* ── Watchlist ───────────────────────────────────────────────────────────── */

function renderWatchlist(items, activeSymbol) {
  document.getElementById('watchlist').innerHTML = items.map(item => {
    const price = item.price ? item.price.toFixed(2) : '—';
    const cur   = item.currency === '₹' ? '₹' : (item.currency || '₹');
    const active = activeSymbol === item.symbol ? ' active' : '';
    return `<div class="wl-item${active}" onclick="onSymbolClick('${item.symbol}')">
      <div class="wl-sym">${item.symbol}<span class="wl-exch">${item.market || ''}</span></div>
      <div class="wl-price">${cur}${price}</div>
    </div>`;
  }).join('');
}

/* ── Search results ──────────────────────────────────────────────────────── */

function renderSearchResults(items) {
  const el = document.getElementById('search-results');
  if (!items.length) { el.style.display = 'none'; return; }
  el.innerHTML = items.map(it =>
    `<div class="sr-item" onmousedown="onSymbolClick('${it.symbol}')">
       <div class="sr-sym">${it.symbol}</div>
       <div class="sr-name">${it.name}</div>
     </div>`
  ).join('');
  el.style.display = 'block';
}

function hideSearchResults() {
  document.getElementById('search-results').style.display = 'none';
}

/* ── Screener ────────────────────────────────────────────────────────────── */

function renderScreener(data) {
  const items  = data.results || [];
  const status = document.getElementById('screener-status');

  if (data.scan_status === 'scanning') {
    const since = data.scan_started ? ` (started ${data.scan_started})` : '';
    status.textContent = `Scanning Nifty 50…${since}`;
  } else if (data.scan_status === 'error') {
    status.style.color = 'var(--red)';
    status.textContent = 'Scan error — will retry in 5 min';
  } else if (data.last_scan) {
    status.style.color = '';
    status.textContent = `Last scan: ${data.last_scan} | ${items.length} stocks`;
  } else if (data.market_open) {
    status.textContent = 'Market open — first scan starting…';
  } else {
    status.textContent = 'Market closed (opens 9:15 AM IST Mon–Fri)';
  }

  const tbody = document.getElementById('screener-body');
  if (!items.length) {
    const msg = data.scan_status === 'scanning'
      ? `First scan in progress (scanning 50 stocks, ~20s)…`
      : data.market_open
        ? 'Waiting for scan results…'
        : 'Market closed. Opens 9:15 AM IST (Mon–Fri).';
    tbody.innerHTML = `<tr><td colspan="10" style="color:var(--dim);text-align:center;padding:30px">${msg}</td></tr>`;
    return;
  }

  tbody.innerHTML = items.map((s, i) => {
    const pct  = s.pct_chg || 0;
    const pc   = signClass(pct);
    const sig  = s.signal || 'HOLD';
    const vwap = s.vwap ? rupee(s.vwap) : '—';
    const rsi  = s.rsi  ? fmt(s.rsi, 1) : '—';
    const orb  = s.or_breakout || '—';
    return `<tr onclick="onSymbolClick('${s.symbol}')" style="cursor:pointer">
      <td style="color:var(--dim)">${i + 1}</td>
      <td><strong>${s.symbol}</strong></td>
      <td style="color:var(--dim);font-size:12px">${s.name || ''}</td>
      <td class="r">${rupee(s.price)}</td>
      <td class="r ${pc}">${signStr(pct)}${fmt(pct)}%</td>
      <td class="r">${fmt(s.vol_ratio || 0, 1)}x</td>
      <td class="r">${vwap}</td>
      <td class="r">${rsi}</td>
      <td>${orb}</td>
      <td><span class="badge ${badgeClass(sig)}">${sig}</span></td>
    </tr>`;
  }).join('');
}

/* ── Intraday detail card ────────────────────────────────────────────────── */

function renderIntradayDetail(sym, data) {
  document.getElementById('id-sym-title').textContent = sym + ' — Intraday';
  const pct = data.pct_chg || 0;
  const or  = data.or_breakout ? data.or_breakout.toUpperCase() : '—';
  const sig = data.signal || 'HOLD';

  document.getElementById('id-grid').innerHTML = `
    <div class="id-cell"><div class="lbl">LTP</div>
      <div class="val">${rupee(data.price)}</div></div>
    <div class="id-cell"><div class="lbl">Change %</div>
      <div class="val ${signClass(pct)}">${signStr(pct)}${fmt(pct)}%</div></div>
    <div class="id-cell"><div class="lbl">VWAP</div>
      <div class="val">${rupee(data.vwap)}</div></div>
    <div class="id-cell"><div class="lbl">Vol Ratio</div>
      <div class="val">${data.vol_ratio || '—'}x</div></div>
    <div class="id-cell"><div class="lbl">RSI (5m)</div>
      <div class="val">${fmt(data.rsi || 50, 1)}</div></div>
    <div class="id-cell"><div class="lbl">MACD Hist</div>
      <div class="val">${fmt(data.macd_hist || 0, 4)}</div></div>
    <div class="id-cell"><div class="lbl">OR Breakout</div>
      <div class="val">${or}</div></div>
    <div class="id-cell"><div class="lbl">Signal</div>
      <div class="val"><span class="badge ${badgeClass(sig)}">${sig}</span></div></div>
  `;
  document.getElementById('intraday-detail').style.display = 'block';
}

function showIntradayError(sym) {
  document.getElementById('id-sym-title').textContent = sym + ' — Intraday';
  document.getElementById('id-grid').innerHTML =
    '<div style="color:var(--red)">No intraday data (market may be closed)</div>';
  document.getElementById('intraday-detail').style.display = 'block';
}

/* ── Portfolio ───────────────────────────────────────────────────────────── */

function renderPortfolioBar(d) {
  const pnl = d.total_pnl || 0;
  const pc  = pnl >= 0 ? 'pnl-pos' : 'pnl-neg';
  const sg  = signStr(pnl);

  document.getElementById('tb-total').innerHTML = rupee(d.total_value);
  const pnlEl = document.getElementById('tb-pnl');
  pnlEl.className = 'num ' + pc;
  pnlEl.innerHTML = `${sg}${rupee(Math.abs(pnl))} (${sg}${fmt(d.total_pnl_pct)}%)`;
  document.getElementById('tb-pos').textContent =
    `${d.n_positions || 0} position${d.n_positions === 1 ? '' : 's'}`;
}

function renderPortfolioTab(d, initialCap) {
  const pnl = d.total_pnl || 0;
  const pc  = pnl >= 0 ? 'pnl-pos' : 'pnl-neg';
  const sg  = signStr(pnl);

  document.getElementById('pf-cards').innerHTML = `
    <div class="pf-card">
      <div class="label">Total Value</div>
      <div class="value">${rupee(d.total_value)}</div>
      <div class="sub ${pc}">${sg}${rupee(Math.abs(pnl))} (${sg}${fmt(d.total_pnl_pct)}%)</div>
    </div>
    <div class="pf-card">
      <div class="label">Available Cash</div>
      <div class="value">${rupee(d.cash)}</div>
    </div>
    <div class="pf-card">
      <div class="label">Market Value</div>
      <div class="value">${rupee(d.market_value)}</div>
      <div class="sub">${d.n_positions || 0} position(s)</div>
    </div>
    <div class="pf-card">
      <div class="label">Unrealised P&amp;L</div>
      <div class="value ${pc}">${sg}${rupee(Math.abs(pnl))}</div>
      <div class="sub">Starting: ${rupee(initialCap)}</div>
    </div>`;

  const pos   = d.positions || [];
  const tbody = document.getElementById('positions-body');
  if (!pos.length) {
    tbody.innerHTML = '<tr><td colspan="7" style="color:var(--dim);text-align:center;padding:20px">No open positions</td></tr>';
    return;
  }
  tbody.innerHTML = pos.map(p => {
    const pn = p.unrealised_pnl || 0;
    return `<tr>
      <td><strong>${p.symbol}</strong></td>
      <td class="r">${p.quantity}</td>
      <td class="r">${rupee(p.avg_cost)}</td>
      <td class="r">${rupee(p.current_price)}</td>
      <td class="r">${rupee(p.market_value)}</td>
      <td class="r ${signClass(pn)}">${signStr(pn)}${rupee(Math.abs(pn))}</td>
      <td class="r ${signClass(pn)}">${signStr(pn)}${fmt(p.pnl_pct)}%</td>
    </tr>`;
  }).join('');
}

/* ── Trades ──────────────────────────────────────────────────────────────── */

function renderTrades(trades) {
  const tbody = document.getElementById('trades-body');
  if (!trades.length) {
    tbody.innerHTML = '<tr><td colspan="8" style="color:var(--dim);text-align:center;padding:20px">No trades yet</td></tr>';
    return;
  }
  tbody.innerHTML = trades.map(t => {
    // backend field is 'pnl' (only for sells)
    const pnl     = t.pnl ?? null;
    const pnlHtml = pnl != null
      ? `<span class="${signClass(pnl)}">${signStr(pnl)}${rupee(Math.abs(pnl))}</span>`
      : '—';
    const side    = (t.side || '').toUpperCase();
    const qty     = t.quantity || 0;
    return `<tr>
      <td><strong>${t.symbol}</strong></td>
      <td class="${side === 'BUY' ? 'side-buy' : 'side-sell'}">${side}</td>
      <td class="r">${qty}</td>
      <td class="r">${rupee(t.price)}</td>
      <td class="r">${rupee(qty * t.price)}</td>
      <td class="r">${pnlHtml}</td>
      <td style="color:var(--dim);font-size:11px;max-width:130px;overflow:hidden;white-space:nowrap">${t.reason || '—'}</td>
      <td style="color:var(--dim);font-size:11px">${(t.executed_at || '').slice(0, 16)}</td>
    </tr>`;
  }).join('');
}

/* ── News ────────────────────────────────────────────────────────────────── */

function renderNews(articles) {
  const el = document.getElementById('news-list');
  if (!articles.length) {
    el.innerHTML = '<div style="color:var(--dim);padding:20px">No news available</div>';
    return;
  }
  el.innerHTML = articles.slice(0, 40).map(a => {
    const s    = a.sentiment || 0;
    const sc   = s > 0.1 ? 'sent-pos' : s < -0.1 ? 'sent-neg' : '';
    const time = (a.published_at || '').slice(0, 16);
    const sentHtml = s
      ? ` &bull; <span class="${sc}">${s > 0 ? '&#9650;' : '&#9660;'} ${Math.abs(s).toFixed(2)}</span>`
      : '';
    return `<div class="news-item">
      <a href="${a.url || '#'}" target="_blank">
        <div class="nh">${a.title || 'Untitled'}</div>
      </a>
      <div class="nm">
        <span style="font-weight:600;color:var(--accent)">${a.symbol || ''}</span>
        ${time ? ` &bull; ${time}` : ''}${sentHtml}
      </div>
    </div>`;
  }).join('');
}

/* ── Auto-trade button ───────────────────────────────────────────────────── */

function renderAutoBtn(enabled) {
  const btn = document.getElementById('auto-btn');
  btn.className = enabled ? 'on' : 'off';
  btn.textContent = enabled ? 'AUTO ON' : 'AUTO OFF';
}
