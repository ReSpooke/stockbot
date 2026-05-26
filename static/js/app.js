/**
 * app.js — application state, event handlers, boot & polling.
 * Depends on: api.js, ui.js (loaded first via <script> tags).
 *
 * INITIAL_CAP is injected by the Flask template into the global scope.
 */

/* ── State ───────────────────────────────────────────────────────────────── */

let autoEnabled  = false;
let activeSymbol = null;
let activeSnap   = null;   // last intraday snapshot (used by manual trade)

/* ── Clock ───────────────────────────────────────────────────────────────── */

setInterval(renderClock, 1000);
renderClock();

/* ── Tabs ────────────────────────────────────────────────────────────────── */

const TAB_NAMES = ['screener', 'portfolio', 'trades', 'news'];

function switchTab(name) {
  document.querySelectorAll('.tab').forEach((el, i) =>
    el.classList.toggle('active', TAB_NAMES[i] === name)
  );
  document.querySelectorAll('.tab-pane').forEach(el =>
    el.classList.remove('active')
  );
  document.getElementById('tab-' + name).classList.add('active');

  // Lazy-load data for each tab
  if (name === 'portfolio') loadPortfolio();
  if (name === 'trades')    loadTrades();
  if (name === 'news')      loadNews();
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
  // Show placeholder while loading
  document.getElementById('intraday-detail').style.display = 'block';
  document.getElementById('id-sym-title').textContent = sym + ' — Intraday';
  document.getElementById('id-grid').innerHTML =
    '<div style="color:var(--dim);font-size:12px">Loading…</div>';
  try {
    const data = await API.intraday(sym);
    activeSnap = data;
    renderIntradayDetail(sym, data);
    // Refresh watchlist to highlight active symbol
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
  // The background thread does the actual work; just wait then refresh
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

/* ── Auto-trade toggle ───────────────────────────────────────────────────── */

async function toggleAutoTrade() {
  const next = !autoEnabled;
  try {
    await API.setAutoTrade(next);
    autoEnabled = next;
    renderAutoBtn(autoEnabled);
    toast(
      autoEnabled
        ? 'Auto-trading ENABLED — bot buys/sells every 5 min during market hours'
        : 'Auto-trading DISABLED',
      autoEnabled ? 'ok' : ''
    );
  } catch (e) {
    toast('Failed to toggle auto-trade', 'err');
  }
}

async function syncAutoState() {
  try {
    const data = await API.intradayState();
    autoEnabled = data.enabled;
    renderAutoBtn(autoEnabled);
  } catch (e) {}
}

/* ── Manual trade ────────────────────────────────────────────────────────── */

async function manualTrade(side) {
  if (!activeSymbol) { toast('Select a stock first', 'err'); return; }
  if (!activeSnap || !activeSnap.price) { toast('No price data — try again', 'err'); return; }

  const price = activeSnap.price;
  const label = side.toUpperCase();
  if (!confirm(`${label} ${activeSymbol} @ &#8377;${price.toFixed(2)}?`)) return;

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
    toast(`Square-off done. Daily P&L: &#8377;${(result.daily_pnl || 0).toFixed(2)}`, 'ok');
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
    syncAutoState(),
  ]);
}

init();

// Polling intervals
setInterval(loadMarketStatus, 30_000);
setInterval(loadWatchlist,    15_000);
setInterval(loadScreener,     30_000);
setInterval(loadPortfolio,    20_000);
setInterval(syncAutoState,    60_000);
setInterval(() => {
  if (document.getElementById('tab-news').classList.contains('active')) loadNews();
}, 60_000);
