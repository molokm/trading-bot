import React, { useState, useEffect, useCallback } from 'react';
import { Brain, Play, Pause, RefreshCw, Target, Activity, TrendingUp, AlertTriangle, Zap, BarChart3 } from 'lucide-react';
import { api } from '../services/api';

function RegimeBadge({ regime }) {
  const colors = {
    bull: 'bg-green-500/20 text-green-400 border-green-500/30',
    bear: 'bg-red-500/20 text-red-400 border-red-500/30',
    sideways: 'bg-yellow-500/20 text-yellow-400 border-yellow-500/30',
    unknown: 'bg-gray-500/20 text-gray-400 border-gray-500/30',
  };
  return (
    <span className={`text-xs px-2 py-0.5 rounded-full border ${colors[regime] || colors.unknown}`}>
      {regime?.toUpperCase() || '?'}
    </span>
  );
}

function ActionBadge({ action }) {
  const colors = {
    LONG: 'bg-green-500/20 text-green-400 border-green-500/30',
    SHORT: 'bg-red-500/20 text-red-400 border-red-500/30',
    BLOCKED: 'bg-orange-500/20 text-orange-400 border-orange-500/30',
    WAIT: 'bg-gray-500/20 text-gray-400 border-gray-500/30',
  };
  return (
    <span className={`text-xs px-2 py-0.5 rounded-full border font-bold ${colors[action] || colors.WAIT}`}>
      {action}
    </span>
  );
}

function RuleStatus({ details }) {
  if (!details) return null;
  return (
    <div className="space-y-1 mt-2">
      {Object.entries(details).map(([id, d]) => (
        <div key={id} className="flex items-center gap-2 text-xs">
          <span>{d.passed ? '✅' : '❌'}</span>
          <span className={d.passed ? 'text-green-400' : 'text-red-400'}>{d.description}</span>
        </div>
      ))}
    </div>
  );
}

function EvalCard({ eval_ }) {
  const [expanded, setExpanded] = useState(false);
  const regime = eval_.regime || 'unknown';

  return (
    <div className={`glass rounded-xl p-4 border cursor-pointer transition-all ${
      eval_.rules_passed
        ? 'border-green-500/30 shadow-lg shadow-green-500/5'
        : 'border-white/5 hover:border-white/10'
    }`} onClick={() => setExpanded(!expanded)}>
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className={`w-10 h-10 rounded-lg flex items-center justify-center ${
            eval_.rules_passed ? 'bg-green-500/20' : 'bg-white/5'
          }`}>
            {eval_.rules_passed ? <Target size={18} className="text-green-400" /> : <Activity size={18} className="text-gray-500" />}
          </div>
          <div>
            <div className="text-white font-bold text-sm">{eval_.symbol}</div>
            <div className="text-gray-500 text-xs">${eval_.entry_price?.toLocaleString()}</div>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <RegimeBadge regime={regime} />
          <ActionBadge action={eval_.action} />
        </div>
      </div>

      {expanded && (
        <div className="mt-3 pt-3 border-t border-white/5" onClick={e => e.stopPropagation()}>
          <div className="grid grid-cols-3 gap-3 mb-3">
            <div className="text-center">
              <div className="text-gray-500 text-xs">Entry</div>
              <div className="text-white text-sm font-mono">${eval_.entry_price?.toLocaleString()}</div>
            </div>
            <div className="text-center">
              <div className="text-gray-500 text-xs">Stop</div>
              <div className="text-red-400 text-sm font-mono">${eval_.stop_loss?.toLocaleString()}</div>
            </div>
            <div className="text-center">
              <div className="text-gray-500 text-xs">Target</div>
              <div className="text-green-400 text-sm font-mono">${eval_.target_price?.toLocaleString()}</div>
            </div>
          </div>
          <div className="text-center mb-3">
            <span className="text-xs text-gray-500">R:R = 1:{eval_.r_value ? (4.5 / 1.8).toFixed(1) : '?'}</span>
          </div>
          <RuleStatus details={eval_.details} />
        </div>
      )}
    </div>
  );
}

export default function OrchestratorPage() {
  const [status, setStatus] = useState(null);
  const [evaluations, setEvaluations] = useState({});
  const [scanResults, setScanResults] = useState([]);
  const [rStats, setRStats] = useState(null);
  const [loading, setLoading] = useState(false);
  const [cycleRunning, setCycleRunning] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const [s, r] = await Promise.all([
        api.request('/orch/status'),
        api.request('/r-stats'),
      ]);
      setStatus(s);
      setRStats(r);
    } catch (e) {
      console.error('Refresh error:', e);
    }
  }, []);

  useEffect(() => {
    refresh();
    const interval = setInterval(refresh, 30000);
    return () => clearInterval(interval);
  }, [refresh]);

  const runCycle = async () => {
    setCycleRunning(true);
    try {
      const result = await api.request('/orch/cycle-run', { method: 'POST' });
      // Fetch updated evaluations
      const status = await api.request('/orch/status');
      setStatus(status);
      // The evaluations come from the cycle
      if (result.all_results) {
        const evals = {};
        result.all_results.forEach(e => { evals[e.symbol] = e; });
        setEvaluations(evals);
      }
    } catch (e) {
      console.error('Cycle error:', e);
    }
    setCycleRunning(false);
  };

  const runScan = async () => {
    setLoading(true);
    try {
      const result = await api.request('/orch/scan', { method: 'POST' });
      setScanResults(result.results || []);
    } catch (e) {
      console.error('Scan error:', e);
    }
    setLoading(false);
  };

  const evalSymbol = async (symbol) => {
    try {
      const result = await api.request(`/orch/evaluate?symbol=${symbol}`, { method: 'POST' });
      setEvaluations(prev => ({ ...prev, [symbol]: result }));
    } catch (e) {
      console.error('Eval error:', e);
    }
  };

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <Brain className="text-neon-green" size={28} />
          <div>
            <h1 className="text-2xl font-bold text-white">AI Orchestrator</h1>
            <p className="text-gray-400 text-sm">{status?.strategy || 'Loading...'}</p>
          </div>
        </div>
        <div className="flex gap-2">
          <button
            onClick={runScan}
            disabled={loading}
            className="flex items-center gap-2 px-4 py-2 rounded-lg bg-white/5 text-gray-300 hover:bg-white/10 text-sm disabled:opacity-50"
          >
            <RefreshCw size={14} className={loading ? 'animate-spin' : ''} />
            Scan
          </button>
          <button
            onClick={runCycle}
            disabled={cycleRunning}
            className="flex items-center gap-2 px-4 py-2 rounded-lg bg-neon-green/20 text-neon-green hover:bg-neon-green/30 text-sm font-bold disabled:opacity-50"
          >
            {cycleRunning ? <Pause size={14} /> : <Play size={14} />}
            {cycleRunning ? 'Running...' : 'Run Cycle'}
          </button>
        </div>
      </div>

      {/* Stats Row */}
      <div className="grid grid-cols-4 gap-3">
        <div className="glass rounded-xl p-4">
          <div className="text-gray-500 text-xs mb-1">Cycles Run</div>
          <div className="text-white text-xl font-bold">{status?.cycle_count || 0}</div>
        </div>
        <div className="glass rounded-xl p-4">
          <div className="text-gray-500 text-xs mb-1">Symbols</div>
          <div className="text-white text-xl font-bold">{status?.symbols?.length || 0}</div>
        </div>
        <div className="glass rounded-xl p-4">
          <div className="text-gray-500 text-xs mb-1">Active Signals</div>
          <div className={`text-xl font-bold ${status?.active_signals > 0 ? 'text-green-400' : 'text-gray-500'}`}>
            {status?.active_signals || 0}
          </div>
        </div>
        <div className="glass rounded-xl p-4">
          <div className="text-gray-500 text-xs mb-1">Total R</div>
          <div className={`text-xl font-bold ${(rStats?.total_r || 0) > 0 ? 'text-green-400' : 'text-red-400'}`}>
            {rStats?.total_r?.toFixed(2) || '0.00'}
          </div>
        </div>
      </div>

      {/* R-Multiple Stats */}
      {rStats && rStats.total_trades > 0 && (
        <div className="glass rounded-xl p-4">
          <h3 className="text-white font-bold text-sm mb-3 flex items-center gap-2">
            <BarChart3 size={14} /> R-Multiple Performance
          </h3>
          <div className="grid grid-cols-6 gap-3 text-center">
            <div>
              <div className="text-gray-500 text-xs">Trades</div>
              <div className="text-white text-sm font-bold">{rStats.total_trades}</div>
            </div>
            <div>
              <div className="text-gray-500 text-xs">Win Rate</div>
              <div className="text-white text-sm font-bold">{rStats.win_rate}%</div>
            </div>
            <div>
              <div className="text-gray-500 text-xs">Avg R</div>
              <div className={`text-sm font-bold ${(rStats.avg_r || 0) > 0 ? 'text-green-400' : 'text-red-400'}`}>
                {rStats.avg_r?.toFixed(3)}
              </div>
            </div>
            <div>
              <div className="text-gray-500 text-xs">Profit Factor</div>
              <div className="text-white text-sm font-bold">{rStats.profit_factor?.toFixed(2)}</div>
            </div>
            <div>
              <div className="text-gray-500 text-xs">Max Win</div>
              <div className="text-green-400 text-sm font-bold">{rStats.max_win_r?.toFixed(2)}R</div>
            </div>
            <div>
              <div className="text-gray-500 text-xs">Max Loss</div>
              <div className="text-red-400 text-sm font-bold">{rStats.max_loss_r?.toFixed(2)}R</div>
            </div>
          </div>
        </div>
      )}

      {/* Scanner Results */}
      {scanResults.length > 0 && (
        <div className="glass rounded-xl p-4">
          <h3 className="text-white font-bold text-sm mb-3 flex items-center gap-2">
            <Zap size={14} /> Scanner Results
          </h3>
          <div className="space-y-2">
            {scanResults.map((r, i) => (
              <div key={i} className="flex items-center justify-between px-3 py-2 rounded-lg bg-white/3 hover:bg-white/5 cursor-pointer"
                   onClick={() => evalSymbol(r.instId)}>
                <div className="flex items-center gap-3">
                  <span className={r.change_pct > 0 ? 'text-green-400' : 'text-red-400'}>
                    {r.change_pct > 0 ? '▲' : '▼'}
                  </span>
                  <span className="text-white text-sm font-mono">{r.instId}</span>
                  <span className={`text-xs ${r.change_pct > 0 ? 'text-green-400' : 'text-red-400'}`}>
                    {r.change_pct > 0 ? '+' : ''}{r.change_pct}%
                  </span>
                </div>
                <div className="flex items-center gap-3 text-xs text-gray-500">
                  <span>${(r.vol_24h_usd / 1e6).toFixed(0)}M</span>
                  <span>Score: {r.score}</span>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Evaluations */}
      <div>
        <h3 className="text-white font-bold text-sm mb-3 flex items-center gap-2">
          <Target size={14} /> Evaluations
        </h3>
        {Object.keys(evaluations).length > 0 ? (
          <div className="space-y-2">
            {Object.values(evaluations).map((e, i) => (
              <EvalCard key={i} eval_={e} />
            ))}
          </div>
        ) : (
          <div className="glass rounded-xl p-8 text-center">
            <Brain size={40} className="text-gray-600 mx-auto mb-3" />
            <p className="text-gray-500">No evaluations yet. Click "Run Cycle" to start.</p>
          </div>
        )}
      </div>
    </div>
  );
}
