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

  testCredentials: (creds) =>
    request('/credentials/test', {
      method: 'POST',
      body: JSON.stringify(creds),
    }),

  initCredentials: (creds) =>
    request('/credentials/init', {
      method: 'POST',
      body: JSON.stringify(creds),
    }),

  getPortfolio: () => request('/portfolio'),

  getPositions: (instType = 'SWAP') =>
    request(`/positions?inst_type=${instType}`),

  getTicker: (instId = 'BTC-USDT') =>
    request(`/market/ticker?inst_id=${instId}`),

  getCandles: (instId = 'BTC-USDT', bar = '1H', limit = 200) =>
    request(`/market/candles?inst_id=${instId}&bar=${bar}&limit=${limit}`),

  placeOrder: (order) =>
    request('/trade/order', { method: 'POST', body: JSON.stringify(order) }),

  getOrders: (instType = 'SWAP') => request(`/trade/orders?inst_type=${instType}`),

  getTradeLog: () => request('/trade/log'),

  getStrategies: () => request('/strategies'),

  uploadStrategy: (filename, content) =>
    request('/strategies/upload', {
      method: 'POST',
      body: JSON.stringify({ filename, content }),
    }),

  deleteStrategy: (id) =>
    request(`/strategies/${id}`, { method: 'DELETE' }),

  runBacktest: (data) =>
    request('/backtest/run', { method: 'POST', body: JSON.stringify(data) }),

  getBacktestStatus: (jobId) =>
    request(`/backtest/status/${jobId}`),

  getBacktestHistory: () => request('/backtest/history'),

  deployLive: (data) =>
    request('/live/deploy', { method: 'POST', body: JSON.stringify(data) }),

  stopBot: (botId) =>
    request(`/live/stop/${botId}`, { method: 'POST' }),

  startBot: (botId) =>
    request(`/live/start/${botId}`, { method: 'POST' }),

  restartBot: (botId) =>
    request(`/live/restart/${botId}`, { method: 'POST' }),

  getBotDetail: (botId) => request(`/live/bots/${botId}`),

  listBots: () => request('/live/bots'),

  // ── Database-backed ──
  listAllBots: () => request('/bots'),

  getBotSignals: (botId, limit = 100) =>
    request(`/bots/${botId}/signals?limit=${limit}`),

  getBotTrades: (botId, limit = 100) =>
    request(`/bots/${botId}/trades?limit=${limit}`),

  getBotMetrics: (botId, limit = 100) =>
    request(`/bots/${botId}/metrics?limit=${limit}`),

  deleteBot: (botId) =>
    request(`/bots/${botId}`, { method: 'DELETE' }),

  getAllSignals: (limit = 100) => request(`/signals?limit=${limit}`),

  getAllTrades: (limit = 50) => request(`/trades?limit=${limit}`),

  getBotChart: (botId, params = '') => request(`/bots/${botId}/chart${params}`),

  getPnl: () => request('/pnl'),

  getPairedTrades: (limit = 15, begin = '', end = '') => {
    let url = `/trades/paired?limit=${limit}`
    if (begin) url += `&begin=${encodeURIComponent(begin)}`
    if (end) url += `&end=${encodeURIComponent(end)}`
    return request(url)
  },

  closePosition: (instId, posSide, sz, mgnMode = 'cross') =>
    request('/positions/close', {
      method: 'POST',
      body: JSON.stringify({ instId, posSide, sz, mgnMode }),
    }),

  getDbPositions: () => request('/db/positions'),

  getBotPlanned: (botId) => request(`/bots/${botId}/planned`),

  wsStatus: () => request('/ws/status'),

  credentialsStatus: () => request('/credentials/status'),

  // ── AI Orchestrator ──
  orchStatus: () => request('/orch/status'),
  orchEvaluate: (symbol) => request(`/orch/evaluate?symbol=${symbol}`, { method: 'POST' }),
  orchScan: () => request('/orch/scan', { method: 'POST' }),
  orchCycle: () => request('/orch/cycle', { method: 'POST' }),
  orchCycleRun: () => request('/orch/cycle-run', { method: 'POST' }),
  orchGetRules: () => request('/orch/rules'),
  orchUpdateRules: (rules) => request('/orch/rules', { method: 'POST', body: JSON.stringify(rules) }),

  // ── R-Multiple Tracking ──
  rStats: () => request('/r-stats'),
  rTrades: () => request('/r-trades'),
  rDaily: () => request('/r-daily'),
  rSimulate: (data) => request('/r-simulate', { method: 'POST', body: JSON.stringify(data) }),
};
