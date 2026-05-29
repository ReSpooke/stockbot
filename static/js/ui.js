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
    const since = data.scan_started ? ` since ${data.scan_started}` : '';
    status.textContent = `Scanning all NSE stocks${since}…`;
    status.style.color = 'var(--yellow)';
  } else if (data.scan_status === 'error') {
    status.style.color = 'var(--red)';
    status.textContent = 'Scan error — retrying in 5 min';
  } else if (data.last_scan) {
    status.style.color = 'var(--dim)';
    status.textContent = `Last scan ${data.last_scan} · ${items.length} stocks · Bot always active`;
  } else if (data.market_open) {
    status.style.color = 'var(--dim)';
    status.textContent = 'Market open — first scan in progress…';
  } else {
    status.style.color = 'var(--dim)';
    status.textContent = 'Market closed · Bot activates at 9:15 AM IST (Mon–Fri)';
  }

  const tbody = document.getElementById('screener-body');
  if (!items.length) {
    const msg = data.scan_status === 'scanning'
      ? 'Scanning all NSE stocks (~30s for first scan)…'
      : data.market_open
        ? 'Waiting for first scan…'
        : 'Market closed. Bot activates at 9:15 AM IST.';
    tbody.innerHTML = `<tr><td colspan="11" style="color:var(--dim);text-align:center;padding:30px">${msg}</td></tr>`;
    return;
  }

  tbody.innerHTML = items.map((s, i) => {
    // ── Actual market data ──────────────────────────────────────────────
    const pct  = s.pct_chg || 0;
    const pc   = signClass(pct);
    const vwap = s.vwap ? rupee(s.vwap) : '—';
    const rsi  = s.rsi  ? fmt(s.rsi, 1) : '—';
    const orb  = s.or_breakout || '—';
    const abv  = s.above_vwap === true ? ' ↑' : s.above_vwap === false ? ' ↓' : '';
    const abvC = s.above_vwap === true ? 'pos' : s.above_vwap === false ? 'neg' : '';

    // ── Bot analysis (from decision engine, may be absent pre-first cycle) ──
    const botScore = s.bot_score != null ? s.bot_score : null;
    const botSig   = s.bot_signal || s.signal || 'HOLD';
    const botConf  = s.bot_confidence || '—';
    const botScoreHtml = botScore != null
      ? `<span style="color:${botScore >= 5 ? 'var(--green)' : botScore <= -3 ? 'var(--red)' : 'var(--dim)'};font-weight:700">${botScore >= 0 ? '+' : ''}${botScore}</span>`
      : '<span style="color:var(--dim)">—</span>';
    const confCls = botConf === 'HIGH' ? 'pos' : botConf === 'MEDIUM' ? '' : 'neu';

    const rowCls = s.holding ? 'row-held' : '';

    return `<tr class="${rowCls}" onclick="onSymbolClick('${s.symbol}')" style="cursor:pointer">
      <td style="color:var(--dim)">${i + 1}${s.holding ? ' ●' : ''}</td>
      <td><strong>${s.symbol}</strong></td>
      <td class="r">${rupee(s.price)}</td>
      <td class="r ${pc}">${signStr(pct)}${fmt(pct)}%</td>
      <td class="r">${fmt(s.vol_ratio || 0, 1)}×</td>
      <td class="r">${vwap}<span class="${abvC}">${abv}</span></td>
      <td class="r">${rsi}</td>
      <td>${orb}</td>
      <td class="r">${botScoreHtml}</td>
      <td class="r ${confCls}" style="font-size:11px">${botConf}</td>
      <td><span class="badge ${badgeClass(botSig)}">${botSig}</span></td>
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
  _renderPositionRows(pos, tbody);
}

/* Shared renderer used by both full portfolio and live-positions fast-poll */
function _renderPositionRows(pos, tbody) {
  if (!pos || !pos.length) {
    tbody.innerHTML = '<tr><td colspan="7" style="color:var(--dim);text-align:center;padding:20px">No open positions</td></tr>';
    return;
  }
  tbody.innerHTML = pos.map(p => {
    const pn = p.unrealised_pnl || 0;
    const pc = signClass(pn);
    const sg = signStr(pn);
    // Stale = price not yet refreshed from yfinance, show dim
    const priceHtml = p.stale
      ? `<span style="color:var(--dim)">${rupee(p.current_price)} ⟳</span>`
      : `<span class="${pc}">${rupee(p.current_price)}</span>`;
    return `<tr>
      <td><strong>${p.symbol}</strong></td>
      <td class="r">${p.quantity}</td>
      <td class="r" style="color:var(--dim)">${rupee(p.avg_cost)}</td>
      <td class="r">${priceHtml}</td>
      <td class="r">${rupee(p.market_value)}</td>
      <td class="r ${pc}" style="font-weight:700">${sg}${rupee(Math.abs(pn))}</td>
      <td class="r ${pc}">${sg}${fmt(p.pnl_pct)}%</td>
    </tr>`;
  }).join('');
}

/* Fast live-positions update — only refreshes the table rows, not the cards */
function renderPositionsLive(data) {
  const tbody = document.getElementById('positions-body');
  if (tbody) _renderPositionRows(data.positions || [], tbody);

  // Update top bar numbers from live positions
  const positions = data.positions || [];
  const mv  = positions.reduce((s, p) => s + (p.market_value || 0), 0);
  const pnl = positions.reduce((s, p) => s + (p.unrealised_pnl || 0), 0);
  const total = (data.cash || 0) + mv;

  document.getElementById('tb-total').innerHTML = rupee(total);
  document.getElementById('tb-pos').textContent =
    `${positions.length} position${positions.length === 1 ? '' : 's'}`;
  const pnlEl = document.getElementById('tb-pnl');
  pnlEl.className = 'num ' + signClass(pnl);
  pnlEl.innerHTML = `${signStr(pnl)}${rupee(Math.abs(pnl))}`;

  // Also refresh the timestamp hint
  if (data.updated_at) {
    const hint = document.getElementById('prices-ts');
    if (hint) hint.textContent = 'Prices at ' + data.updated_at;
  }
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

/* ── Bot status indicator ────────────────────────────────────────────────── */

function renderBotStatus(state) {
  const el = document.getElementById('bot-status');
  if (!el) return;
  if (state.market_open && state.trading_hours) {
    el.className  = 'bot-active';
    el.textContent = `BOT ACTIVE · ${state.trades_today || 0} trades today · P&L ${state.daily_pnl >= 0 ? '+' : ''}₹${(state.daily_pnl || 0).toFixed(0)}`;
  } else if (state.market_open) {
    el.className  = 'bot-idle';
    el.textContent = 'BOT IDLE · Market open, trading window closed';
  } else {
    el.className  = 'bot-idle';
    el.textContent = 'BOT OFFLINE · Market closed';
  }
}

/* ── Bot decision log ────────────────────────────────────────────────────── */

function renderBotLog(entries) {
  const el = document.getElementById('bot-log-body');
  if (!el) return;
  if (!entries.length) {
    el.innerHTML = '<tr><td colspan="7" style="color:var(--dim);text-align:center;padding:30px">No decisions yet — bot activates at 9:15 AM IST</td></tr>';
    return;
  }
  el.innerHTML = entries.map(e => {
    const ac  = e.action || 'HOLD';
    // Show label: traded = actual action; not traded but signal = SKIP
    const label = e.traded
      ? (ac === 'SELL' && e.exit_type ? ac + ' (' + e.exit_type.replace('_', ' ') + ')' : ac)
      : (ac === 'BUY' || ac === 'SELL') ? ac + ' (skip)' : ac;
    const cls = ac === 'BUY'
      ? (e.traded ? 'side-buy' : 'neu')
      : ac === 'SELL'
        ? (e.traded ? 'side-sell' : 'neu')
        : 'neu';
    const pnlHtml = e.pnl != null
      ? `<span class="${signClass(e.pnl)}">${signStr(e.pnl)}₹${Math.abs(e.pnl).toFixed(0)}</span>`
      : '—';
    const scoreColor = e.score >= 5 ? 'var(--green)' : e.score <= -3 ? 'var(--red)' : 'var(--dim)';
    const reasons = (e.reasons || []).join(' · ');
    const tradedDot = e.traded ? ' <span style="color:var(--green)">✓</span>' : '';
    return `<tr>
      <td style="color:var(--dim);font-size:11px">${e.time || ''}</td>
      <td class="${cls}" style="font-size:12px;font-weight:700">${label}${tradedDot}</td>
      <td><strong>${e.symbol || '—'}</strong></td>
      <td class="r">${e.price ? rupee(e.price) : '—'}</td>
      <td class="r" style="color:${scoreColor};font-weight:700">${e.score >= 0 ? '+' : ''}${e.score}</td>
      <td class="r">${pnlHtml}</td>
      <td style="color:var(--dim);font-size:11px">${reasons}</td>
    </tr>`;
  }).join('');
}
