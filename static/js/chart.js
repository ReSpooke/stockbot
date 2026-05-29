/**
 * chart.js — stock chart rendering using TradingView Lightweight Charts v4.
 *
 * Exports: initChart(containerId, symbol)  — create/update the chart
 *          destroyChart()                  — clean up
 *
 * Chart layout:
 *   Top panel    : Candlestick + VWAP line + Bot BUY/SELL markers
 *   Bottom panel : Volume histogram
 *
 * Bot vs Actual overlay:
 *   BUY  markers (green ▲) — where the bot entered
 *   SELL markers (red  ▼) — where the bot exited
 *   Hovering a marker shows the score and reason in a tooltip.
 */

'use strict';

const ChartModule = (() => {
  let _chart      = null;
  let _candleSeries= null;
  let _vwapSeries  = null;
  let _volChart    = null;
  let _volSeries   = null;
  let _container   = null;

  const CHART_H    = 280;
  const VOL_H      = 80;

  const DARK = {
    layout:     { background: { color: '#16181d' }, textColor: '#8b8f9a' },
    grid:       { vertLines: { color: '#2a2d35' }, horzLines: { color: '#2a2d35' } },
    crosshair:  { mode: 1 },
    rightPriceScale: { borderColor: '#2a2d35' },
    timeScale:  { borderColor: '#2a2d35', timeVisible: true, secondsVisible: false },
  };

  function _destroy() {
    if (_chart)    { _chart.remove();    _chart    = null; }
    if (_volChart) { _volChart.remove(); _volChart = null; }
    _candleSeries = null;
    _vwapSeries   = null;
    _volSeries    = null;
  }

  function _formatTs(unix) {
    const d = new Date(unix * 1000);
    const hh = String(d.getHours()).padStart(2,'0');
    const mm = String(d.getMinutes()).padStart(2,'0');
    return `${hh}:${mm}`;
  }

  async function init(containerId, symbol) {
    _container = document.getElementById(containerId);
    if (!_container) return;

    // Show loading state
    _container.innerHTML = `<div style="display:flex;align-items:center;justify-content:center;
      height:${CHART_H + VOL_H + 4}px;color:var(--dim);font-size:13px">
      Loading chart for ${symbol}…</div>`;

    // Fetch chart data
    let data;
    try {
      const r = await fetch('/api/chart/' + encodeURIComponent(symbol));
      if (!r.ok) throw new Error(await r.text());
      data = await r.json();
    } catch (e) {
      _container.innerHTML = `<div style="display:flex;align-items:center;justify-content:center;
        height:${CHART_H + VOL_H}px;color:var(--red);font-size:12px">
        Chart unavailable: ${e.message}</div>`;
      return;
    }

    if (!data.candles || !data.candles.length) {
      _container.innerHTML = `<div style="display:flex;align-items:center;justify-content:center;
        height:${CHART_H + VOL_H}px;color:var(--dim);font-size:12px">
        No candle data (market may be closed)</div>`;
      return;
    }

    // Build chart DOM
    _container.innerHTML = '';
    const priceDiv = document.createElement('div');
    priceDiv.style.cssText = `height:${CHART_H}px;`;
    const volDiv = document.createElement('div');
    volDiv.style.cssText = `height:${VOL_H}px;margin-top:2px;`;
    _container.appendChild(priceDiv);
    _container.appendChild(volDiv);

    _destroy(); // clean up previous if any

    // ── Price chart ─────────────────────────────────────────────────────
    _chart = LightweightCharts.createChart(priceDiv, {
      ...DARK,
      width:  priceDiv.clientWidth || _container.clientWidth,
      height: CHART_H,
    });

    _candleSeries = _chart.addCandlestickSeries({
      upColor:          '#26a69a',
      downColor:        '#ef5350',
      borderUpColor:    '#26a69a',
      borderDownColor:  '#ef5350',
      wickUpColor:      '#26a69a',
      wickDownColor:    '#ef5350',
    });
    _candleSeries.setData(data.candles);

    // VWAP line
    if (data.vwap && data.vwap.length) {
      _vwapSeries = _chart.addLineSeries({
        color:     '#ffb74d',
        lineWidth: 1,
        lineStyle: 2,   // dashed
        title:     'VWAP',
        priceLineVisible: false,
        lastValueVisible: true,
      });
      _vwapSeries.setData(data.vwap);
    }

    // Bot BUY/SELL markers
    if (data.markers && data.markers.length) {
      _candleSeries.setMarkers(data.markers);
    }

    // ── Volume chart ─────────────────────────────────────────────────────
    _volChart = LightweightCharts.createChart(volDiv, {
      ...DARK,
      width:  volDiv.clientWidth || _container.clientWidth,
      height: VOL_H,
      timeScale: { visible: false },
      rightPriceScale: { scaleMargins: { top: 0.05, bottom: 0 } },
    });

    _volSeries = _volChart.addHistogramSeries({
      priceFormat: { type: 'volume' },
      priceScaleId: '',
    });
    _volSeries.priceScale().applyOptions({ scaleMargins: { top: 0.1, bottom: 0 } });
    _volSeries.setData(data.volume || []);

    // Keep time scales in sync
    _chart.timeScale().subscribeVisibleLogicalRangeChange(range => {
      if (range && _volChart) _volChart.timeScale().setVisibleLogicalRange(range);
    });
    _volChart.timeScale().subscribeVisibleLogicalRangeChange(range => {
      if (range && _chart) _chart.timeScale().setVisibleLogicalRange(range);
    });

    _chart.timeScale().fitContent();
    _volChart.timeScale().fitContent();

    // Responsive resize
    const ro = new ResizeObserver(() => {
      const w = _container.clientWidth;
      if (_chart)    _chart.applyOptions({ width: w });
      if (_volChart) _volChart.applyOptions({ width: w });
    });
    ro.observe(_container);
  }

  return { init, destroy: _destroy };
})();
