import React, { useState, useEffect, useCallback } from 'react'
import { useDropzone } from 'react-dropzone'
import {
  Upload, Play, StopCircle, BarChart3, Trash2, FileCode,
  TrendingUp, AlertTriangle, CheckCircle, XCircle, Loader2, Eye,
  LineChart, Bot, Radio
} from 'lucide-react'
import {
  LineChart as RechartsLine, Line, XAxis, YAxis, Tooltip,
  ResponsiveContainer, Area, AreaChart, CartesianGrid
} from 'recharts'
import { api } from '../services/api'
import { useTranslation } from '../hooks/useTranslation'

export default function StrategiesPage() {
  const { t } = useTranslation()
  const [strategies, setStrategies] = useState([])
  const [allBots, setAllBots] = useState([])
  const [selected, setSelected] = useState(null)

  const [backtestParams, setBacktestParams] = useState({
    symbol: 'BTC-USDT',
    timeframe: '1H',
    start_date: '',
    end_date: '',
    initial_capital: 10000,
  })
  const [btResult, setBtResult] = useState(null)
  const [btRunning, setBtRunning] = useState(false)
  const [btProgress, setBtProgress] = useState('')
  const [showDeploy, setShowDeploy] = useState(false)
  const [deploying, setDeploying] = useState(false)
  const [deployResult, setDeployResult] = useState(null)
  const [previewCode, setPreviewCode] = useState(null)

  useEffect(() => { loadStrategies(); loadBots() }, [])

  async function loadStrategies() {
    try {
      const res = await api.getStrategies()
      setStrategies(res.strategies || [])
    } catch {}
  }

  async function loadBots() {
    try {
      const res = await api.listAllBots()
      setAllBots(res.bots || [])
    } catch {}
  }

  async function handleBacktest() {
    if (!selected) return
    setBtRunning(true)
    setBtResult(null)
    setBtProgress('Старт...')
    try {
      const { job_id } = await api.runBacktest({
        strategy_id: selected.id,
        symbol: backtestParams.symbol,
        timeframe: backtestParams.timeframe,
        start_date: backtestParams.start_date || undefined,
        end_date: backtestParams.end_date || undefined,
        initial_capital: backtestParams.initial_capital,
        params: {},
      })
      while (true) {
        await new Promise(r => setTimeout(r, 1000))
        const st = await api.getBacktestStatus(job_id)
        setBtProgress(st.progress || '')
        if (st.status === 'done') {
          setBtResult(st.result)
          break
        }
        if (st.status === 'error') {
          setBtResult({ error: st.error || 'Ошибка' })
          break
        }
      }
    } catch (err) {
      setBtResult({ error: err.message })
    }
    setBtRunning(false)
  }

  async function handleDeploy() {
    if (!selected) return
    setDeploying(true)
    try {
      const result = await api.deployLive({
        strategy_id: selected.id,
        symbol: backtestParams.symbol,
        timeframe: backtestParams.timeframe,
        capital: backtestParams.initial_capital,
      })
      setDeployResult(result)
    } catch (err) {
      setDeployResult({ error: err.message })
    }
    setDeploying(false)
    setShowDeploy(false)
  }

  const onDrop = useCallback(async (acceptedFiles) => {
    for (const file of acceptedFiles) {
      const content = await file.text()
      try {
        await api.uploadStrategy(file.name, content)
      } catch {}
    }
    loadStrategies()
  }, [])

  const { getRootProps, getInputProps, isDragActive } = useDropzone({
    onDrop,
    accept: { 'text/x-python': ['.py'], 'application/json': ['.json'] },
  })

  async function handleDelete(id) {
    try {
      await api.deleteStrategy(id)
      loadStrategies()
      if (selected?.id === id) {
        setSelected(null)
        setBtResult(null)
      }
    } catch {}
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold text-white">{t('strategies.title')}</h2>
          <p className="text-sm text-gray-400 mt-1">{t('strategies.subtitle')}</p>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Strategy List */}
        <div className="lg:col-span-1 space-y-4">
          <div {...getRootProps()} className={`dropzone ${isDragActive ? 'active' : ''}`}>
            <input {...getInputProps()} />
            <Upload size={24} className="mx-auto text-gray-400 mb-2" />
            <p className="text-sm text-gray-400">
              {isDragActive ? t('strategies.drop_active') : t('strategies.drop_inactive')}
            </p>
            <p className="text-xs text-gray-500 mt-1">{t('strategies.drop_hint')}</p>
          </div>

          <div className="glass p-4 space-y-2">
            <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-wider mb-3">
              {t('strategies.my_strategies', { count: strategies.length })}
            </h3>
            {strategies.length === 0 ? (
              <p className="text-sm text-gray-500 text-center py-4">
                {t('strategies.no_strategies')}
              </p>
            ) : (
              strategies.map(s => (
                <div
                  key={s.id}
                  className={`p-3 rounded-xl cursor-pointer transition-all ${
                    selected?.id === s.id
                      ? 'bg-neon-green/5 border border-neon-green/20 neon-glow-green'
                      : 'bg-white/5 hover:bg-white/10 border border-transparent'
                  }`}
                  onClick={() => { setSelected(s); setBtResult(null); setPreviewCode(null) }}
                >
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-2">
                      <FileCode size={14} className="text-neon-blue" />
                      <span className="text-sm font-medium text-white flex items-center gap-2">{s.name}{s.id === 'trend_momentum_pro' && <span className="px-1.5 py-0.5 rounded text-[10px] font-bold bg-gradient-to-r from-purple-500 to-pink-500 text-white">AI</span>}</span>
                    </div>
                    <button
                      className="text-gray-500 hover:text-neon-red transition-colors"
                      onClick={e => { e.stopPropagation(); handleDelete(s.id) }}
                    >
                      <Trash2 size={14} />
                    </button>
                  </div>
                  <div className="flex items-center gap-3 mt-1.5">
                    <span className="text-xs text-gray-500">{s.symbol}</span>
                    <span className="text-xs text-gray-500">{s.timeframe}</span>
                  </div>
                </div>
              ))
            )}
          </div>
        </div>

        {/* Strategy Detail + Backtest */}
        <div className="lg:col-span-2 space-y-4">
          {!selected ? (
            <div className="glass p-12 text-center">
              <BarChart3 size={40} className="mx-auto text-gray-500 mb-3" />
              <p className="text-gray-400">{t('strategies.select_prompt')}</p>
            </div>
          ) : (
            <>
              {/* Strategy Info */}
              <div className="glass p-5">
                <div className="flex items-center justify-between mb-3">
                  <div>
                    <h3 className="text-lg font-bold text-white">{selected.name}</h3>
                    <p className="text-sm text-gray-400 mt-1">{selected.description || t('strategies.no_description')}</p>
                  </div>
                  <button
                    className="text-xs text-gray-400 hover:text-white flex items-center gap-1 px-3 py-1.5 rounded-lg bg-white/5"
                    onClick={() => setPreviewCode(selected)}
                  >
                    <Eye size={12} /> {t('strategies.view_code')}
                  </button>
                </div>
                <div className="flex gap-4 text-xs">
                  <span className="text-gray-400">{t('strategies.symbol')}: <span className="text-white">{selected.symbol}</span></span>
                  <span className="text-gray-400">{t('strategies.timeframe')}: <span className="text-white">{selected.timeframe}</span></span>
                  <span className="text-gray-400">{t('strategies.file')}: <span className="text-white">{selected.filename}</span></span>
                </div>
              </div>

              {/* Deployed Bots */}
              {(() => {
                const stratBots = allBots.filter(b => b.strategy_id === selected.id)
                if (stratBots.length === 0) return null
                return (
                  <div className="glass p-5">
                    <h4 className="text-sm font-semibold text-white mb-4 flex items-center gap-2">
                      <Bot size={16} className="text-neon-blue" />
                      Запущенные боты ({stratBots.length})
                    </h4>
                    <div className="space-y-2">
                      {stratBots.map(b => (
                        <div key={b.id} className="flex items-center justify-between bg-white/5 rounded-xl px-4 py-3">
                          <div className="flex items-center gap-3">
                            <div className={`w-2 h-2 rounded-full ${b.status === 'running' ? 'bg-neon-green animate-pulse' : 'bg-gray-500'}`} />
                            <div>
                              <div className="text-sm font-medium text-white">{b.name || b.id}</div>
                              <div className="text-xs text-gray-400">{b.symbol} · {b.timeframe} · ${b.capital}</div>
                            </div>
                          </div>
                          <div className="flex items-center gap-4 text-xs">
                            <span className={`${b.status === 'running' ? 'text-neon-green' : 'text-gray-400'}`}>
                              {b.status === 'running' ? 'Активен' : 'Остановлен'}
                            </span>
                            <span className={`mono font-medium ${(b.total_pnl || 0) >= 0 ? 'text-neon-green' : 'text-neon-red'}`}>
                              {(b.total_pnl || 0) >= 0 ? '+' : ''}{(b.total_pnl || 0).toFixed(2)} USDT
                            </span>
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                )
              })()}

              {/* Backtest Controls */}
              <div className="glass p-5">
                <h4 className="text-sm font-semibold text-white mb-4 flex items-center gap-2">
                  <LineChart size={16} className="text-neon-blue" />
                  {t('strategies.backtest_config')}
                </h4>
                <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
                  <div>
                    <label className="text-xs text-gray-400">{t('strategies.symbol_label')}</label>
                    <input
                      type="text"
                      className="w-full mt-1 text-xs"
                      value={backtestParams.symbol}
                      onChange={e => setBacktestParams({ ...backtestParams, symbol: e.target.value })}
                    />
                  </div>
                  <div>
                    <label className="text-xs text-gray-400">{t('strategies.timeframe_label')}</label>
                    <select
                      className="w-full mt-1 text-xs"
                      value={backtestParams.timeframe}
                      onChange={e => setBacktestParams({ ...backtestParams, timeframe: e.target.value })}
                    >
                      {['1m','5m','15m','30m','1H','4H','1D'].map(tf => (
                        <option key={tf} value={tf}>{tf}</option>
                      ))}
                    </select>
                  </div>
                  <div>
                    <label className="text-xs text-gray-400">{t('strategies.capital_label')}</label>
                    <input
                      type="number"
                      className="w-full mt-1 text-xs"
                      value={backtestParams.initial_capital}
                      onChange={e => setBacktestParams({ ...backtestParams, initial_capital: +e.target.value })}
                    />
                  </div>
                  <div>
                    <label className="text-xs text-gray-400">Начало</label>
                    <input
                      type="date"
                      className="w-full mt-1 text-xs"
                      value={backtestParams.start_date}
                      onChange={e => setBacktestParams({ ...backtestParams, start_date: e.target.value })}
                    />
                  </div>
                  <div>
                    <label className="text-xs text-gray-400">Конец</label>
                    <input
                      type="date"
                      className="w-full mt-1 text-xs"
                      value={backtestParams.end_date}
                      onChange={e => setBacktestParams({ ...backtestParams, end_date: e.target.value })}
                    />
                  </div>
                </div>
                <div className="flex gap-3 mt-4">
                  <button
                    className="btn-neon px-5 py-2 rounded-xl text-sm font-semibold flex items-center gap-2"
                    onClick={handleBacktest}
                    disabled={btRunning}
                  >
                    {btRunning ? <Loader2 size={14} className="animate-spin" /> : <BarChart3 size={14} />}
                    {btRunning ? btProgress : t('strategies.run_backtest')}
                  </button>
                  <button
                    className="btn-danger px-5 py-2 rounded-xl text-sm font-semibold flex items-center gap-2"
                    onClick={() => setShowDeploy(true)}
                  >
                    <Play size={14} />
                    {t('strategies.deploy_live')}
                  </button>
                </div>
              </div>

              {/* Backtest Results */}
              {btResult && (
                <div className="glass p-5">
                  {btResult.error ? (
                    <div className="flex items-center gap-2 text-neon-red">
                      <XCircle size={16} />
                      {btResult.error}
                    </div>
                  ) : (
                    <>
                      <h4 className="text-sm font-semibold text-white mb-4">{t('strategies.results')}</h4>

                      {/* Period + candles */}
                      <div className="text-xs text-gray-400 mb-4">
                        Свечей загружено: <span className="text-white">{btResult.candles_loaded ?? '?'}</span>
                        {btResult.period && <>, период: <span className="text-white">{btResult.period}</span></>}
                      </div>

                      {/* Metrics */}
                      <div className="grid grid-cols-2 md:grid-cols-5 gap-3 mb-6">
                        {[
                          { label: t('strategies.total_return'), value: `${btResult.total_return_pct?.toFixed(2) ?? 0}%`, color: (btResult.total_return_pct ?? 0) >= 0 ? 'text-neon-green' : 'text-neon-red' },
                          { label: t('strategies.final_capital'), value: `$${(btResult.final_capital ?? 0).toLocaleString()}`, color: 'text-white' },
                          { label: t('strategies.sharpe_ratio'), value: (btResult.sharpe_ratio ?? 0).toFixed(3), color: (btResult.sharpe_ratio ?? 0) >= 1 ? 'text-neon-green' : 'text-neon-yellow' },
                          { label: t('strategies.max_dd'), value: `${(btResult.max_drawdown ?? 0).toFixed(2)}%`, color: 'text-neon-red' },
                          { label: t('strategies.win_rate'), value: `${(btResult.win_rate ?? 0).toFixed(1)}%`, color: (btResult.win_rate ?? 0) >= 50 ? 'text-neon-green' : 'text-neon-red' },
                          { label: t('strategies.total_trades'), value: btResult.total_trades ?? 0, color: 'text-white' },
                          { label: t('strategies.wins'), value: btResult.winning_trades ?? 0, color: 'text-neon-green' },
                          { label: t('strategies.losses'), value: btResult.losing_trades ?? 0, color: 'text-neon-red' },
                        ].map(m => (
                          <div key={m.label} className="glass p-3 text-center">
                            <div className="text-xs text-gray-400">{m.label}</div>
                            <div className={`mono text-lg font-bold ${m.color}`}>{m.value}</div>
                          </div>
                        ))}
                      </div>

                      {/* Equity Curve */}
                      <div className="h-64">
                        <ResponsiveContainer width="100%" height="100%">
                          <AreaChart data={btResult.equity_curve || []}>
                            <defs>
                              <linearGradient id="equityGrad" x1="0" y1="0" x2="0" y2="1">
                                <stop offset="5%" stopColor="#00ff88" stopOpacity={0.3} />
                                <stop offset="95%" stopColor="#00ff88" stopOpacity={0} />
                              </linearGradient>
                            </defs>
                            <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" />
                            <XAxis
                              dataKey="time"
                              tick={{ fontSize: 10, fill: '#666' }}
                              tickFormatter={v => new Date(v).toLocaleDateString()}
                            />
                            <YAxis
                              domain={['auto', 'auto']}
                              tick={{ fontSize: 10, fill: '#666' }}
                              tickFormatter={v => `$${v.toLocaleString()}`}
                            />
                            <Tooltip
                              contentStyle={{
                                background: 'rgba(10,10,30,0.95)',
                                border: '1px solid rgba(255,255,255,0.1)',
                                borderRadius: 8,
                                color: '#fff',
                              }}
                              formatter={v => [`$${v.toLocaleString()}`, t('strategies.final_capital')]}
                            />
                            <Area
                              type="monotone"
                              dataKey="equity"
                              stroke="#00ff88"
                              strokeWidth={2}
                              fill="url(#equityGrad)"
                            />
                          </AreaChart>
                        </ResponsiveContainer>
                      </div>
                    </>
                  )}
                </div>
              )}
            </>
          )}
        </div>
      </div>

      {/* Preview Code Modal */}
      {previewCode && (
        <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50" onClick={() => setPreviewCode(null)}>
          <div className="glass max-w-2xl w-full mx-4 max-h-[70vh] overflow-auto" onClick={e => e.stopPropagation()}>
            <div className="p-4 border-b border-white/5 flex items-center justify-between">
              <h3 className="font-semibold text-white">{previewCode.filename}</h3>
              <button className="text-gray-400 hover:text-white" onClick={() => setPreviewCode(null)}>
                <XCircle size={16} />
              </button>
            </div>
            <pre className="p-4 text-xs text-gray-300 mono overflow-x-auto whitespace-pre-wrap">{previewCode.code}</pre>
          </div>
        </div>
      )}

      {/* Deploy Confirmation Modal */}
      {showDeploy && (
        <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50">
          <div className="glass max-w-md w-full mx-4" onClick={e => e.stopPropagation()}>
            <div className="p-5 space-y-4">
              <div className="flex items-center gap-3">
                <AlertTriangle size={24} className="text-neon-red" />
                <h3 className="text-lg font-bold text-white">{t('strategies.deploy_title')}</h3>
              </div>
              <p className="text-sm text-gray-300">
                {t('strategies.deploy_confirm')} <strong className="text-white">{selected?.name}</strong> {t('strategies.deploy_on')} {backtestParams.symbol}.
              </p>
              <div className="bg-neon-red/5 border border-neon-red/20 rounded-xl p-3">
                <p className="text-xs text-neon-red">
                  <strong>{t('strategies.deploy_risk_warning')}</strong>
                </p>
              </div>
              <div className="text-xs text-gray-400 space-y-1">
                <p>{t('strategies.deploy_strategy')}: {selected?.name}</p>
                <p>{t('strategies.deploy_symbol')}: {backtestParams.symbol}</p>
                <p>{t('strategies.deploy_capital')}: ${backtestParams.initial_capital}</p>
              </div>
              <div className="flex gap-3">
                <button
                  className="btn-danger flex-1 py-2.5 rounded-xl text-sm font-semibold flex items-center justify-center gap-2"
                  onClick={handleDeploy}
                  disabled={deploying}
                >
                  {deploying ? <Loader2 size={14} className="animate-spin" /> : <Play size={14} />}
                  {deploying ? t('strategies.deploying') : t('strategies.confirm_deploy')}
                </button>
                <button
                  className="bg-white/5 text-white flex-1 py-2.5 rounded-xl text-sm font-semibold hover:bg-white/10"
                  onClick={() => setShowDeploy(false)}
                >
                  {t('strategies.cancel')}
                </button>
              </div>
              {deployResult && (
                <div className={`flex items-center gap-2 p-3 rounded-lg text-sm ${
                  deployResult.error ? 'bg-neon-red/5 text-neon-red' : 'bg-neon-green/5 text-neon-green'
                }`}>
                  {deployResult.error ? <XCircle size={14} /> : <CheckCircle size={14} />}
                  {deployResult.error || `${t('strategies.deploy_success')} ${deployResult.bot_id}`}
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
