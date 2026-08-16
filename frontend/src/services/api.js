const BASE = '/api';

function getToken() {
  return localStorage.getItem('auth_token') || '';
}

async function request(path, options = {}) {
  const url = `${BASE}${path}`;
  const token = getToken();
  const headers = { 'Content-Type': 'application/json' };
  if (token) headers['Authorization'] = `Bearer ${token}`;
  const config = { headers, ...options };
  const resp = await fetch(url, config);
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
    const e = new Error(err.detail || 'Request failed');
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

  riskStatus: () => request('/risk/status'),
  riskKill: (enabled) =>
    request('/risk/kill', { method: 'POST', body: JSON.stringify({ enabled }) }),
  getMode: () => request('/mode'),
  setMode: (demo, confirm) =>
    request('/mode', { method: 'POST', body: JSON.stringify({ demo, confirm }) }),
  getAudit: (limit = 50) => request(`/audit?limit=${limit}`),

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
