const BASE = '/api';

/** Global client backoff after 429/502/503 — stops hammering Render free tier */
let apiBackoffUntil = 0;
let apiBackoffMs = 0;

function getToken() {
  // Prefer httpOnly cookie (credentials: include). Bearer from localStorage is legacy.
  // When COOKIE_ONLY_AUTH=1 server omits token body — localStorage stays empty (more XSS-safe).
  return localStorage.getItem('auth_token') || '';
}

function noteApiFailure(status) {
  if (status === 429 || status === 503 || status === 502) {
    apiBackoffMs = Math.min(120000, Math.max(5000, (apiBackoffMs || 3000) * 2));
    apiBackoffUntil = Date.now() + apiBackoffMs;
  }
}

function noteApiSuccess() {
  apiBackoffMs = 0;
  apiBackoffUntil = 0;
}

async function request(path, options = {}) {
  if (Date.now() < apiBackoffUntil) {
    const wait = Math.ceil((apiBackoffUntil - Date.now()) / 1000);
    const e = new Error(`Сервер перегружен / лимит запросов. Подождите ~${wait}с`);
    e.status = 429;
    e.backoff = true;
    throw e;
  }
  const url = `${BASE}${path}`;
  const token = getToken();
  const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) };
  if (token) headers['Authorization'] = `Bearer ${token}`;
  const config = {
    credentials: 'include',
    ...options,
    headers: { ...headers, ...(options.headers || {}) },
  };
  let resp;
  try {
    resp = await fetch(url, config);
  } catch (netErr) {
    noteApiFailure(503);
    throw netErr;
  }
  if (resp.status === 429 || resp.status === 503 || resp.status === 502) {
    noteApiFailure(resp.status);
  } else if (resp.ok) {
    noteApiSuccess();
  }
  if (resp.status === 401) {
    localStorage.removeItem('auth_token');
    localStorage.removeItem('auth_role');
    if (!window.__MINI_APP__) {
      // Preserve path so user knows session died (e.g. after Render sleep with old tokens)
      window.location.href = '/login?reason=session';
    }
    throw new Error('Unauthorized');
  }
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }));
    let detail = err.detail || err.message || resp.statusText || 'Request failed';
    if (Array.isArray(detail)) {
      detail = detail.map((x) => x.msg || JSON.stringify(x)).join('; ');
    } else if (typeof detail === 'object') {
      detail = JSON.stringify(detail);
    }
    const e = new Error(detail);
    e.status = resp.status;
    throw e;
  }
  return resp.json();
}

export const api = {
  // ── Auth ──
  login: (password) =>
    request('/auth/login', { method: 'POST', body: JSON.stringify({ password }) }),

  guest: () =>
    request('/auth/guest', { method: 'POST' }),

  logout: () =>
    request('/auth/logout', { method: 'POST' }),

  authStatus: () => request('/auth/status'),

  health: () => request('/health'),
  positionsClaimsHealth: () => request('/health/positions-claims'),

  riskStatus: () => request('/risk/status'),
  riskKill: (enabled) =>
    request('/risk/kill', { method: 'POST', body: JSON.stringify({ enabled }) }),
  getMode: () => request('/mode'),
  setMode: (demo, confirm) =>
    request('/mode', { method: 'POST', body: JSON.stringify({ demo, confirm }) }),
  getAudit: (limit = 50) => request(`/audit?limit=${limit}`),
  reportSummary: () => request('/reports/summary'),

  // ── Public equity tracker (no auth) ──
  getTracker: () => request('/tracker'),

  // ── Credentials ──
  testCredentials: (creds) =>
    request('/credentials/test', { method: 'POST', body: JSON.stringify(creds) }),

  initCredentials: (creds) =>
    request('/credentials/init', { method: 'POST', body: JSON.stringify(creds) }),

  credentialsStatus: () => request('/credentials/status'),

  // ── Portfolio / Positions ──
  getPortfolio: () => request('/portfolio'),

  getPositions: (instType = 'SWAP') =>
    request(`/positions?inst_type=${instType}`),

  closePosition: (instId, posSide, sz, mgnMode = 'cross') =>
    request('/positions/close', {
      method: 'POST',
      body: JSON.stringify({ instId, posSide, sz, mgnMode }),
    }),

  // ── Market ──
  getTicker: (instId = 'BTC-USDT') =>
    request(`/market/ticker?inst_id=${instId}`),

  getTickers: (instIds = []) =>
    request(`/market/tickers?inst_id=${instIds.join(',')}`),

  getCandles: (instId = 'BTC-USDT', bar = '1H', limit = 200) =>
    request(`/market/candles?inst_id=${instId}&bar=${bar}&limit=${limit}`),

  // ── Trade ──
  placeOrder: (order) =>
    request('/trade/order', { method: 'POST', body: JSON.stringify(order) }),

  getOrders: (instType = 'SWAP') => request(`/trade/orders?inst_type=${instType}`),

  getTradeLog: () => request('/trade/log'),

  // ── Trades & PnL ──
  getAllTrades: (limit = 50) => request(`/trades?limit=${limit}`),

  getPairedTrades: (limit = 15, begin = '', end = '') => {
    let url = `/trades/paired?limit=${limit}`
    if (begin) url += `&begin=${encodeURIComponent(begin)}`
    if (end) url += `&end=${encodeURIComponent(end)}`
    return request(url)
  },

  pnlReconcile: () => request('/pnl/reconcile'),
  getPnl: () => request('/pnl'),

  // ── Momentum Strategy ──
  momentumStatus: () => request('/momentum/status'),

  momentumStart: (config = {}) =>
    request('/momentum/start', { method: 'POST', body: JSON.stringify(config) }),

  momentumStop: () =>
    request('/momentum/stop', { method: 'POST' }),

  momentumTrades: (limit = 20) => request(`/momentum/trades?limit=${limit}`),

  rotationStatus: () => request('/rotation/status'),

  // ── Impulse 1D Strategy ──
  impulseStatus: () => request('/impulse/status'),

  impulseStart: (config = {}) =>
    request('/impulse/start', { method: 'POST', body: JSON.stringify(config) }),

  impulseStop: () =>
    request('/impulse/stop', { method: 'POST' }),

  impulseTrades: (limit = 50) => request(`/impulse/trades?limit=${limit}`),

  impulseConfig: (config = {}) =>
    request('/impulse/config', { method: 'POST', body: JSON.stringify(config) }),

  impulseReset: () =>
    request('/impulse/reset', { method: 'POST' }),

  // ── Validation Strategy ──
  validationStatus: () => request('/validation/status'),

  validationStart: (config = {}) =>
    request('/validation/start', { method: 'POST', body: JSON.stringify(config) }),

  validationStop: () =>
    request('/validation/stop', { method: 'POST' }),

  validationReset: () =>
    request('/validation/reset', { method: 'POST' }),

  validationTrades: (limit = 50) =>
    request(`/validation/trades?limit=${limit}`),

  // ── AI Discretionary ──
  aiStatus: () => request('/ai/status'),
  aiStart: (config = {}) =>
    request('/ai/start', { method: 'POST', body: JSON.stringify(config) }),
  aiStop: () =>
    request('/ai/stop', { method: 'POST' }),
  aiDecide: () =>
    request('/ai/decide', { method: 'POST', body: JSON.stringify({}) }),
  aiLogs: (limit = 200) => request(`/ai/logs?limit=${limit}`),

  // ── Order Book Scalp ──
  scalpStatus: () => request('/scalp/status'),
  scalpBook: (coin = 'BTC', levels = 12) =>
    request(`/scalp/book?coin=${encodeURIComponent(coin)}&levels=${levels}`),
  scalpStart: (config = {}) =>
    request('/scalp/start', { method: 'POST', body: JSON.stringify(config) }),
  scalpStop: () =>
    request('/scalp/stop', { method: 'POST' }),

  // ── VWAP Mean Reversion ──
  vwapRevStatus: () => request('/vwap_rev/status'),
  vwapRevStart: (config = {}) =>
    request('/vwap_rev/start', { method: 'POST', body: JSON.stringify(config) }),
  vwapRevStop: () =>
    request('/vwap_rev/stop', { method: 'POST' }),

  // ── Smart Money Tracker ──
  smartMoneyStatus: () => request('/smart-money/status'),
  smartMoneyDiscover: (page = 1, limit = 20, opts = {}) => {
    const q = new URLSearchParams({
      page: String(page),
      limit: String(limit),
      sort: opts.sort || 'roi',
      min_roi: String(opts.min_roi ?? 0),
      verified_only: opts.verified_only ? 'true' : 'false',
      sources: opts.sources || 'okx,hyperliquid,social',
    })
    return request(`/smart-money/discover?${q}`)
  },
  smartMoneyTrader: (code) => request(`/smart-money/trader/${code}`),
  smartMoneyTracked: () => request('/smart-money/tracked'),
  smartMoneyTrack: (code) =>
    request('/smart-money/track', { method: 'POST', body: JSON.stringify({ unique_code: code }) }),
  smartMoneyUntrack: (code) =>
    request('/smart-money/untrack', { method: 'POST', body: JSON.stringify({ unique_code: code }) }),
  smartMoneyCopy: (code, amt) =>
    request('/smart-money/copy', { method: 'POST', body: JSON.stringify({ unique_code: code, copy_amt: amt }) }),
  smartMoneyStopCopy: (code) =>
    request('/smart-money/stop-copy', { method: 'POST', body: JSON.stringify({ unique_code: code }) }),
  smartMoneyMyCopies: () => request('/smart-money/my-copies'),
  smartMoneyStart: (config = {}) =>
    request('/smart-money/start', { method: 'POST', body: JSON.stringify(config) }),
  smartMoneyStop: () =>
    request('/smart-money/stop', { method: 'POST' }),
  smartMoneyUpdateConfig: (config = {}) =>
    request('/smart-money/config', { method: 'POST', body: JSON.stringify(config) }),
  smartMoneyPnl: () => request('/smart-money/pnl'),
  smartMoneyTrades: (limit = 100) => request(`/smart-money/trades?limit=${limit}`),
  smartMoneyMirrorStatus: () => request('/smart-money/mirror/status'),
  smartMoneyMirrorStart: (body) =>
    request('/smart-money/mirror/start', { method: 'POST', body: JSON.stringify(body || {}) }),
  smartMoneyMirrorStop: (body) =>
    request('/smart-money/mirror/stop', { method: 'POST', body: JSON.stringify(body || {}) }),
  smartMoneyTraderHistory: (code, limit = 50) =>
    request(`/smart-money/trader/${code}/history?limit=${limit}`),

  // ── Chart ──
  chartTrades: (instId) => request(`/chart/trades?inst_id=${instId}`),

  // ── Backtest (real OKX data) ──
  runBacktest: (config) =>
    request('/backtest/run', { method: 'POST', body: JSON.stringify(config || {}) }),

  getLastBacktest: () => request('/backtest/last'),

  // ── Backtest (freqtrade engine) ──
  runFreqtradeBacktest: (config) =>
    request('/backtest/freqtrade', { method: 'POST', body: JSON.stringify(config || {}) }),

  // ── Telegram notifications ──
  telegramStatus: () => request('/telegram/status'),

  telegramConfig: (config) =>
    request('/telegram/config', { method: 'POST', body: JSON.stringify(config) }),

  telegramTest: (config) =>
    request('/telegram/test', { method: 'POST', body: JSON.stringify(config || {}) }),

  telegramSimulate: (config) =>
    request('/telegram/simulate', { method: 'POST', body: JSON.stringify(config || {}) }),

  telegramMenu: (config) =>
    request('/telegram/menu', { method: 'POST', body: JSON.stringify(config || {}) }),

  // ── Paid signal subscriptions (Telegram Stars) ──
  subsList: () => request('/subs'),
  subsConfig: () => request('/subs/config'),
  subsActivate: (payload) =>
    request('/subs/activate', { method: 'POST', body: JSON.stringify(payload || {}) }),
  subsDeactivate: (payload) =>
    request('/subs/deactivate', { method: 'POST', body: JSON.stringify(payload || {}) }),

  // ── Multi-tenant /api/me/* (mini-app user accounts) ──
  me: () => request('/me'),
  meCredentials: (creds) =>
    request('/me/credentials', { method: 'POST', body: JSON.stringify(creds || {}) }),
  meCredentialsTest: (creds) =>
    request('/me/credentials/test', { method: 'POST', body: JSON.stringify(creds || {}) }),
  mePortfolio: () => request('/me/portfolio'),
  mePositions: () => request('/me/positions?inst_type=SWAP'),
  meClosePosition: (instId, posSide, mgnMode = 'cross') =>
    request('/me/positions/close', {
      method: 'POST',
      body: JSON.stringify({ instId, posSide, mgnMode }),
    }),
  meStatus: () => request('/me/status'),
  meRotationStart: (config = {}) =>
    request('/me/rotation/start', { method: 'POST', body: JSON.stringify(config) }),
  meRotationStop: () => request('/me/rotation/stop', { method: 'POST' }),
  meImpulseStart: (config = {}) =>
    request('/me/impulse/start', { method: 'POST', body: JSON.stringify(config) }),
  meImpulseStop: () => request('/me/impulse/stop', { method: 'POST' }),
  meTrades: (limit = 30) => request(`/me/trades?limit=${limit}`),
  mePnl: () => request('/me/pnl'),

  telegramAuth: (initData) =>
    request('/auth/telegram', { method: 'POST', body: JSON.stringify({ initData }) }),

  debugMiniLog: (logs) =>
    request('/debug/mini-log', { method: 'POST', body: JSON.stringify({ logs }) }),

  debugServerHits: () => request('/debug/server-hits'),

  // ── Analysis log ──
  downloadAnalysisLog: async () => {
    const url = `${BASE}/analysis/log`;
    const token = getToken();
    const headers = {};
    if (token) headers['Authorization'] = `Bearer ${token}`;
    const resp = await fetch(url, { headers });
    if (resp.status === 401) {
      localStorage.removeItem('auth_token');
      localStorage.removeItem('auth_role');
      if (!window.__MINI_APP__) window.location.href = '/login';
      throw new Error('Unauthorized');
    }
    if (!resp.ok) throw new Error('Failed to download log');
    return resp.blob();
  },
};
