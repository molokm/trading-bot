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
    window.location.href = '/login';
    throw new Error('Unauthorized');
  }
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }));
    throw new Error(err.detail || 'Request failed');
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

  momentumChartData: () => request('/momentum/chart-data'),
};
