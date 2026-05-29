/**
 * app.js — application state, event handlers, boot & polling.
 * Depends on: api.js, ui.js (loaded first via <script> tags).
 *
 * INITIAL_CAP is injected by the Flask template into the global scope.
 * The bot is ALWAYS active during market hours — no toggle needed.
 */

/* ── State ───────────────────────────────────────────────────────────────── */

let activeSymbol = null;
let activeSnap   = null;   // last intraday snapshot (used by manual trade)

/* ── Clock ───────────────────────────────────────────────────────────────── */

setInterval(renderClock, 1000);
renderClock();

/* ── Tabs ────────────────────────────────────────────────────────────────── */

const TAB_NAMES = ['screener', 'botlog', 'portfolio', 'trades', 'news'];

function switchTab(name) {
  document.querySelectorAll('.tab').forEach((el, i) =>
    el.classList.toggle('active', TAB_NAMES[i] === name)
  );
  document.querySelectorAll('.tab-pane').forEach(el =>
    el.classList.remove('active')
  );
  document.getElementById('tab-' + name).classList.add('active');

  if (name === 'portfolio') loadPortfolio();
  if (name === 'trades')    loadTrades();
  if (name === 'news')      loadNews();
  if (name === 'botlog')    loadBotLog();
}

/* ── Market status ───────────────────────────────────────────────────────── */

async function loadMarketStatus() {
  try { renderMarketBadges(await API.marketStatus()); } catch (e) {}
}

/* ── Watchlist ───────────────────────────────────────────────────────────── */

async function loadWatchlist() {
  try { renderWatchlist(await API.watchlist(), activeSymbol); } catch (e) {}
}

/* ── Search ──────────────────────────────────────────────────────────────── */

let _searchDebounce = null;

function onSearchInput(q) {
  clearTimeout(_searchDebounce);
  if (!q.trim()) { hideSearchResults(); return; }
  _searchDebounce = setTimeout(async () => {
    try { renderSearchResults(await API.search(q)); } catch (e) { hideSearchResults(); }
  }, 220);
}

function onSearchBlur() {
  setTimeout(hideSearchResults, 150);
}

/* ── Symbol selection ────────────────────────────────────────────────────── */

function onSymbolClick(sym) {
  activeSymbol = sym;
  document.getElementById('search-input').value = '';
  hideSearchResults();
  switchTab('screener');
  loadIntradayDetail(sym);
}

async function loadIntradayDetail(sym) {
  document.getElementById('intraday-detail').style.display = 'block';
  document.getElementById('id-sym-title').textContent = sym + ' — Intraday';
  document.getElementById('id-grid').innerHTML =
    '<div style="color:var(--dim);font-size:12px">Loading…</div>';
  try {
    const data = await API.intraday(sym);
    activeSnap = data;
    renderIntradayDetail(sym, data);
    loadWatchlist();
  } catch (e) {
    activeSnap = null;
    showIntradayError(sym);
  }
}

/* ── Screener ────────────────────────────────────────────────────────────── */

async function loadScreener() {
  try { renderScreener(await API.screener()); } catch (e) {}
}

async function triggerScan() {
  const btn = document.getElementById('scan-btn');
  btn.textContent = 'Scanning…';
  btn.disabled = true;
  await new Promise(r => setTimeout(r, 3000));
  await loadScreener();
  btn.textContent = 'Refresh Scan';
  btn.disabled = false;
}

/* ── Portfolio ───────────────────────────────────────────────────────────── */

async function loadPortfolio() {
  try {
    const data = await API.portfolio();
    renderPortfolioBar(data);
    renderPortfolioTab(data, INITIAL_CAP);
  } catch (e) {}
}

/* Fast 5-second poll: refreshes position rows + top bar from cached prices */
async function refreshPositions() {
  try {
    renderPositionsLive(await API.positionsLive());
  } catch (e) {}
}

/* ── Trades ──────────────────────────────────────────────────────────────── */

async function loadTrades() {
  try { renderTrades(await API.trades()); } catch (e) {}
}

/* ── News ────────────────────────────────────────────────────────────────── */

async function loadNews() {
  try {
    const data = await API.liveNews();
    renderNews(data.articles || []);
  } catch (e) {}
}

/* ── Bot log ─────────────────────────────────────────────────────────────── */

async function loadBotLog() {
  try { renderBotLog(await API.botLog(200)); } catch (e) {}
}

/* ── Bot status (top bar) ────────────────────────────────────────────────── */

async function syncBotStatus() {
  try {
    const data = await API.intradayState();
    renderBotStatus(data);
    // Also keep portfolio bar fresh from the state
  } catch (e) {}
}

/* ── Manual trade ────────────────────────────────────────────────────────── */

async function manualTrade(side) {
  if (!activeSymbol) { toast('Select a stock first', 'err'); return; }
  if (!activeSnap || !activeSnap.price) { toast('No price data — try again', 'err'); return; }

  const price = activeSnap.price;
  if (!confirm(`${side.toUpperCase()} ${activeSymbol} @ ₹${price.toFixed(2)}?`)) return;

  try {
    const result = await API.trade(activeSymbol, side, price);
    toast(result.message || (result.ok ? 'Trade done' : 'Trade failed'), result.ok ? 'ok' : 'err');
    if (result.ok) { loadPortfolio(); loadTrades(); }
  } catch (e) {
    toast('Trade error: ' + e.message, 'err');
  }
}

/* ── Square-off ──────────────────────────────────────────────────────────── */

async function squareOff() {
  if (!confirm('Close ALL open positions now?')) return;
  try {
    const result = await API.squareOff();
    toast(`Square-off done. Daily P&L: ₹${(result.daily_pnl || 0).toFixed(2)}`, 'ok');
    loadPortfolio();
    loadTrades();
  } catch (e) {
    toast('Square-off error', 'err');
  }
}

/* ── Boot & polling ──────────────────────────────────────────────────────── */

async function init() {
  await Promise.all([
    loadMarketStatus(),
    loadWatchlist(),
    loadScreener(),
    loadPortfolio(),
    refreshPositions(),
    syncBotStatus(),
  ]);
}

init();

setInterval(loadMarketStatus,  30_000);
setInterval(loadWatchlist,     15_000);
setInterval(loadPortfolio,     30_000);   // full portfolio (with fresh yfinance fetch)
setInterval(refreshPositions,   5_000);   // live P&L from cache every 5s
setInterval(syncBotStatus,     30_000);

// Poll screener every 10s (background scan is ~30s; this catches updates quickly)
setInterval(loadScreener, 10_000);

// Bot log auto-refreshes when the tab is visible
setInterval(() => {
  if (document.getElementById('tab-botlog').classList.contains('active')) loadBotLog();
  if (document.getElementById('tab-news').classList.contains('active'))   loadNews();
}, 15_000);
