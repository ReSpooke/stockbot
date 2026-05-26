/**
 * api.js — thin wrappers around every backend endpoint.
 * All functions return a Promise<data> and throw on non-2xx responses.
 */

const API = (() => {
  async function _get(url) {
    const r = await fetch(url);
    if (!r.ok) throw new Error(`GET ${url} → ${r.status}`);
    return r.json();
  }

  async function _post(url, body) {
    const r = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error(`POST ${url} → ${r.status}`);
    return r.json();
  }

  return {
    marketStatus:  ()      => _get('/api/market_status'),
    watchlist:     ()      => _get('/api/watchlist'),
    screener:      ()      => _get('/api/screener'),
    portfolio:     ()      => _get('/api/portfolio'),
    trades:        ()      => _get('/api/trades'),
    liveNews:      ()      => _get('/api/live_news'),
    intradayState: ()      => _get('/api/intraday_state'),
    intraday:      (sym)   => _get('/api/intraday/' + encodeURIComponent(sym)),
    search:        (q)     => _get('/api/search?q=' + encodeURIComponent(q)),

    setAutoTrade:  (enabled) => _post('/api/auto_trade', { enabled }),
    squareOff:     ()        => _post('/api/squareoff', {}),
    trade:         (symbol, side, price) => _post('/api/trade', { symbol, side, price }),
  };
})();
