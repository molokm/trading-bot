import React, { useState, useEffect } from 'react'
import {
  Play, StopCircle, Activity, Bot, Clock, Zap, ZapOff,
  AlertTriangle, CheckCircle, XCircle, Loader2, Plus,
  BarChart3, TrendingUp, Shield, RefreshCw, Radio,
  DollarSign, Target
} from 'lucide-react'
import { api } from '../services/api'
import { useTranslation } from '../hooks/useTranslation'

const STATUS_COLORS = {
  running: 'text-neon-green',
  starting: 'text-neon-yellow',
  stopped: 'text-gray-500',
  error: 'text-neon-red',
}

const STATUS_BG = {
  running: 'bg-neon-green/10 border-neon-green/20',
  starting: 'bg-neon-yellow/10 border-neon-yellow/20',
  stopped: 'bg-gray-500/10 border-gray-500/20',
  error: 'bg-neon-red/10 border-neon-red/20',
}

const STATUS_DOT = {
  running: 'bg-neon-green animate-pulse',
  starting: 'bg-neon-yellow animate-pulse',
  stopped: 'bg-gray-500',
  error: 'bg-neon-red',
}

const ACTION_COLORS = {
  LONG: 'text-neon-green border-neon-green/20 bg-neon-green/5',
  SHORT: 'text-neon-red border-neon-red/20 bg-neon-red/5',
  HOLD: 'text-neon-yellow border-neon-yellow/20 bg-neon-yellow/5',
  WAIT: 'text-gray-400 border-gray-500/20 bg-gray-500/5',
  IN_POSITION: 'text-neon-blue border-neon-blue/20 bg-neon-blue/5',
}

function PlannedTradePanel({ planned }) {
  if (!planned) return null
  const action = planned.action || 'WAIT'
  const isEntry = action === 'LONG' || action === 'SHORT'
  const isInPosition = action === 'IN_POSITION'

  return (
    <div className={`mt-3 rounded-lg border px-3 py-2.5 text-xs space-y-2 ${ACTION_COLORS[action] || ACTION_COLORS.WAIT}`}>
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2 font-bold">
          <Target size={12} />
          <span>План: {action === 'LONG' ? 'LONG' : action === 'SHORT' ? 'SHORT' : action === 'IN_POSITION' ? `${planned.side} (в позиции)` : 'Ожидание'}</span>
        </div>
        <div className="flex items-center gap-3 text-[10px] opacity-70">
          <span>EMA200: ${planned.ema200?.toLocaleString()}</span>
          <span>RSI: {planned.rsi}</span>
        </div>
      </div>

      {planned.trend && (
        <div className="flex items-center gap-2 text-[10px]">
          <span className={`font-semibold ${planned.trend === 'UP' ? 'text-neon-green' : planned.trend === 'DOWN' ? 'text-neon-red' : 'text-gray-400'}`}>
            {planned.trend === 'UP' ? '▲' : planned.trend === 'DOWN' ? '▼' : '—'} {planned.trend}
          </span>
          <span>Swing H: ${planned.swing_high?.toLocaleString()}</span>
          <span>Swing L: ${planned.swing_low?.toLocaleString()}</span>
        </div>
      )}

      {isEntry && planned.entry_zone && (
        <div className="space-y-2">
          <div className={`rounded-lg px-4 py-3 text-center ${
            action === 'LONG' ? 'bg-neon-green/10 border border-neon-green/30' : 'bg-neon-red/10 border border-neon-red/30'
          }`}>
            <div className="text-[10px] text-gray-400 uppercase tracking-wider mb-1">Цена входа в сделку</div>
            <div className={`text-xl font-bold font-mono tracking-wide ${
              action === 'LONG' ? 'text-neon-green' : 'text-neon-red'
            }`}>
              ${planned.entry_zone[0]?.toLocaleString()} — ${planned.entry_zone[1]?.toLocaleString()}
            </div>
          </div>
          {planned.stop_loss && (
            <div className="flex items-center justify-between text-xs px-1">
              <span className="text-gray-400">Стоп-лосс:</span>
              <span className="font-mono font-bold text-neon-red">${planned.stop_loss?.toLocaleString()}</span>
            </div>
          )}
          {planned.conditions && (
            <div className="grid grid-cols-2 gap-x-4 gap-y-1">
              {Object.entries(planned.conditions).map(([key, cond]) => {
                const labels = {
                  uptrend: 'Тренд вверх',
                  downtrend: 'Тренд вниз',
                  pulled_back: 'Откат от макс.',
                  near_support: 'У поддержки',
                  bounce: 'Отскок',
                  climbed: 'Рост к сопрот.',
                  near_resistance: 'У сопротивл.',
                  reject: 'Отклонение',
                }
                return (
                  <div key={key} className="flex items-center gap-1.5 text-[11px]">
                    <span className={cond.met ? 'text-neon-green' : 'text-neon-red'}>{cond.met ? '✓' : '✗'}</span>
                    <span className="text-gray-300">{labels[key] || key}</span>
                  </div>
                )
              })}
            </div>
          )}
          {planned.distance_to_long != null && (
            <div className="text-[11px] text-gray-500">
              Расстояние до входа: ${planned.distance_to_long?.toLocaleString()} ({((planned.distance_to_long / planned.current_price) * 100).toFixed(2)}%)
            </div>
          )}
        </div>
      )}

      {isInPosition && (
        <div className="space-y-1">
          <div className="flex items-center gap-3">
            <span>Вход: <b className="font-mono">${planned.entry_price?.toLocaleString()}</b></span>
            {planned.unrealized_pnl != null && (
              <span className={planned.unrealized_pnl >= 0 ? 'text-neon-green font-bold' : 'text-neon-red font-bold'}>
                {planned.unrealized_pnl >= 0 ? '+' : ''}{planned.unrealized_pnl?.toFixed(2)} USDT
              </span>
            )}
          </div>
          {planned.stop_loss && (
            <div className="text-[10px]">
              Trailing stop: {planned.trailing_stop_active ? '🟢 активен' : '⚪ ожидание'} | Стоп: ${planned.stop_loss?.toLocaleString()}
            </div>
          )}
          {planned.exit_conditions && (
            <div className="text-[10px] opacity-60 space-y-0.5">
              {planned.exit_conditions.map((c, i) => <div key={i}>→ {c}</div>)}
            </div>
          )}
        </div>
      )}

      {planned.note && !isEntry && !isInPosition && (
        <div className="text-[10px] opacity-60">{planned.note}</div>
      )}
    </div>
  )
}

export default function LiveTrading() {
  const { t } = useTranslation()
  const [bots, setBots] = useState([])
  const [strategies, setStrategies] = useState([])
  const [loading, setLoading] = useState(true)
  const [showDeploy, setShowDeploy] = useState(false)
  const [deploying, setDeploying] = useState(false)
  const [deployResult, setDeployResult] = useState(null)
  const [stopping, setStopping] = useState(null)
  const [restarting, setRestarting] = useState(null)

  const [deployForm, setDeployForm] = useState({
    strategy_id: '',
    symbol: 'BTC-USDT',
    timeframe: '15m',
    capital: 100,
  })

  const baseFromSymbol = (sym) => sym?.split('-')[0] || ''
  const formatVol = (pos, sym) => `${Math.abs(pos).toFixed(6)} ${baseFromSymbol(sym)}`

  const fetchBots = async () => {
    try {
      const res = await api.listBots()
      setBots(res.bots || [])
    } catch (e) { console.error('fetchBots error:', e) }
  }

  const fetchStrategies = async () => {
    try {
      const res = await api.getStrategies()
      setStrategies(res.strategies || [])
      if (res.strategies?.length > 0 && !deployForm.strategy_id) {
        setDeployForm(f => ({ ...f, strategy_id: res.strategies[0].id }))
      }
    } catch (e) { console.error('fetchStrategies error:', e) }
  }

  useEffect(() => {
    const init = async () => {
      setLoading(true)
      await Promise.all([fetchBots(), fetchStrategies()])
      setLoading(false)
    }
    init()
  }, [])

  useEffect(() => {
    const interval = setInterval(fetchBots, 5000)
    return () => clearInterval(interval)
  }, [])

  const handleDeploy = async () => {
    if (!deployForm.strategy_id) return
    setDeploying(true)
    setDeployResult(null)
    try {
      const result = await api.deployLive(deployForm)
      setDeployResult(result)
      await fetchBots()
      setDeploying(false)
      setTimeout(() => { setShowDeploy(false); setDeployResult(null) }, 1500)
    } catch (err) {
      setDeployResult({ error: err.message })
      setDeploying(false)
    }
  }

  const handleStop = async (botId) => {
    setStopping(botId)
    try {
      await api.stopBot(botId)
      await fetchBots()
    } catch {}
    setStopping(null)
  }

  const handleRestart = async (botId) => {
    setRestarting(botId)
    try {
      await api.restartBot(botId)
      await fetchBots()
    } catch {}
    setRestarting(null)
  }

  const timeframeOptions = ['1m', '5m', '15m', '30m', '1H', '4H', '1D']
  const symbols = ['BTC-USDT', 'ETH-USDT', 'SOL-USDT', 'DOGE-USDT', 'XRP-USDT']

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold text-white">Живые боты</h2>
          <p className="text-sm text-gray-400 mt-1">
            {loading ? 'Загрузка...' : `${bots.length} ботов, ${bots.filter(b => b.status === 'running').length} активных`}
          </p>
        </div>
        <button
          className="btn-neon px-5 py-2.5 rounded-xl text-sm font-semibold flex items-center gap-2"
          onClick={() => { setDeployResult(null); setShowDeploy(true); fetchStrategies() }}
        >
          <Plus size={16} /> Запустить бота
        </button>
      </div>

      {/* Live Status Banner */}
      <div className="glass p-4 flex items-center gap-4">
        <div className="flex items-center gap-2">
          <Radio size={16} className={bots.some(b => b.status === 'running') ? 'text-neon-green animate-pulse' : 'text-gray-500'} />
          <span className="text-sm text-gray-300">
            {bots.some(b => b.status === 'running') ? 'Торговля активна' : 'Нет активных ботов'}
          </span>
        </div>
        <div className="flex-1" />
        <button
          className="flex items-center gap-1.5 text-xs text-gray-400 hover:text-white transition-colors"
          onClick={fetchBots}
        >
          <RefreshCw size={12} /> Обновить
        </button>
        <div className="flex items-center gap-2 text-xs text-gray-500">
          <Clock size={12} /> Авто-обновление каждые 5с
        </div>
      </div>

      {/* PnL Panel */}
      {bots.length > 0 && bots.some(b => b.status === 'running') && (
        <div className="glass p-5 border border-white/5">
          <div className="flex items-center gap-3 mb-4">
            <DollarSign size={20} className="text-neon-yellow" />
            <h3 className="text-base font-bold text-white">Текущий P&L</h3>
          </div>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            {(() => {
              const totalPnL = bots.reduce((s, b) => s + (b.total_pnl || b.pnl || 0), 0)
              const totalCapital = bots.reduce((s, b) => s + (b.capital || 0), 0)
              const pnlPct = totalCapital > 0 ? (totalPnL / totalCapital) * 100 : 0
              const hasPosition = bots.some(b => b.position !== 0)
              return (
                <>
                  <div className="space-y-1">
                    <div className="text-xs text-gray-400">Суммарный P&L</div>
                    <div className={`mono text-2xl font-bold ${totalPnL >= 0 ? 'text-neon-green' : 'text-neon-red'}`}>
                      {totalPnL >= 0 ? '+' : ''}{totalPnL.toFixed(2)} USDT
                    </div>
                    <div className={`mono text-xs font-medium ${pnlPct >= 0 ? 'text-neon-green/70' : 'text-neon-red/70'}`}>
                      {pnlPct >= 0 ? '+' : ''}{pnlPct.toFixed(2)}%
                    </div>
                  </div>
                  <div className="space-y-1">
                    <div className="text-xs text-gray-400">Реализованный</div>
                    <div className={`mono text-lg font-bold ${totalPnL >= 0 ? 'text-neon-green' : 'text-neon-red'}`}>
                      {totalPnL >= 0 ? '+' : ''}{totalPnL.toFixed(2)} USDT
                    </div>
                  </div>
                  <div className="space-y-1">
                    <div className="text-xs text-gray-400">Открытых позиций</div>
                    <div className="text-lg font-bold text-white">
                      {bots.filter(b => b.position !== 0).length}
                    </div>
                    {hasPosition && (
                      <div className="text-xs text-gray-500">
                        {bots.filter(b => b.position > 0).length} LONG / {bots.filter(b => b.position < 0).length} SHORT
                      </div>
                    )}
                  </div>
                  <div className="space-y-1">
                    <div className="text-xs text-gray-400">Сделок</div>
                    <div className="text-lg font-bold text-white">
                      {bots.reduce((s, b) => s + (b.trade_count || 0), 0)}
                    </div>
                    <div className="text-xs text-gray-500">
                      {bots.reduce((s, b) => s + (b.win_count || 0), 0)} побед / {bots.reduce((s, b) => s + (b.loss_count || 0), 0)} поражений
                    </div>
                  </div>
                </>
              )
            })()}
          </div>
        </div>
      )}

      {/* Bots Grid */}
      {bots.length === 0 ? (
        <div className="glass p-12 text-center">
          <Bot size={48} className="mx-auto text-gray-500 mb-4" />
          <p className="text-gray-400 text-lg mb-2">Нет запущенных ботов</p>
          <p className="text-gray-500 text-sm mb-6">Запустите бота чтобы начать автоматическую торговлю</p>
          <button
            className="btn-neon px-6 py-2.5 rounded-xl text-sm font-semibold flex items-center gap-2 mx-auto"
            onClick={() => { setDeployResult(null); setShowDeploy(true); fetchStrategies() }}
          >
            <Play size={16} /> Запустить первого бота
          </button>
        </div>
      ) : (
        <div className="grid gap-4">
          {bots.map(bot => {
            const isRunning = bot.status === 'running'
            return (
              <div key={bot.id} className={`glass p-5 border ${STATUS_BG[bot.status] || 'border-white/5'}`}>
                <div className="flex items-start justify-between">
                  <div className="space-y-1.5">
                    <div className="flex items-center gap-3">
                      <div className={`w-2.5 h-2.5 rounded-full ${STATUS_DOT[bot.status] || 'bg-gray-500'}`} />
                      <span className="text-sm font-bold text-white flex items-center gap-2">
                        {strategies.find(s => s.id === bot.strategy_id)?.name || bot.strategy_id}
                        {bot.strategy_id === 'trend_momentum_pro' && <span className="px-1.5 py-0.5 rounded text-[10px] font-bold bg-gradient-to-r from-purple-500 to-pink-500 text-white">AI</span>}
                      </span>
                      <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${STATUS_COLORS[bot.status] || 'text-gray-400'}`}>
                        {bot.status}
                      </span>
                    </div>
                    <div className="flex items-center gap-4 text-xs text-gray-400">
                      <span className="flex items-center gap-1">
                        <BarChart3 size={12} /> {bot.symbol}
                      </span>
                      <span className="flex items-center gap-1">
                        <Clock size={12} /> {bot.timeframe}
                      </span>
                      <span>Капитал: ${bot.capital}</span>
                      <span>Циклов: {bot.cycle_count}</span>
                      {bot.signal_type && <span className="text-xs text-gray-500">{bot.signal_type === 'diff' ? 'дифф' : 'позиц'}</span>}
                    </div>
                  </div>
                  <div className="flex items-center gap-4">
                    <div className="text-right space-y-1">
                      <div className={`mono text-base font-bold ${(bot.total_pnl || bot.pnl) >= 0 ? 'text-neon-green' : 'text-neon-red'}`}>
                        {(bot.total_pnl || bot.pnl) >= 0 ? '+' : ''}{(bot.total_pnl || bot.pnl).toFixed(2)} USDT
                      </div>
                      {bot.position !== 0 && bot.unrealized_pnl != null && (
                        <div className={`mono text-xs ${bot.unrealized_pnl >= 0 ? 'text-neon-green/70' : 'text-neon-red/70'}`}>
                          unreal: {bot.unrealized_pnl >= 0 ? '+' : ''}{bot.unrealized_pnl.toFixed(2)} USDT
                        </div>
                      )}
                      {bot.position !== 0 && (
                        <div className="text-xs space-y-0.5">
                          <div className={bot.position > 0 ? 'text-neon-green' : 'text-neon-red'}>
                            {bot.position > 0 ? '▲ LONG' : '▼ SHORT'} {formatVol(bot.position, bot.symbol)}
                          </div>
                          {bot.entry_price > 0 && (
                            <div className="text-gray-400">Вход: ${bot.entry_price.toFixed(2)}</div>
                          )}
                          <div className="text-gray-500">
                            Текущая: ${(bot.current_price || 0).toFixed(2)}
                          </div>
                        </div>
                      )}
                      {bot.orders?.length > 0 && bot.position === 0 && (
                        <div className="text-xs space-y-0.5">
                          {bot.orders.filter(o => o.pnl != null).slice(-3).reverse().map((o, i) => (
                            <div key={i} className={`${o.pnl >= 0 ? 'text-neon-green' : 'text-neon-red'}`}>
                              {o.side === 'sell' ? '▼' : '▲'} {o.pnl >= 0 ? '+' : ''}{o.pnl.toFixed(2)} USDT
                            </div>
                          ))}
                        </div>
                      )}
                      {bot.last_cycle && (
                        <div className="text-xs text-gray-500">
                          {new Date(bot.last_cycle).toLocaleTimeString()}
                        </div>
                      )}
                    </div>
                    {isRunning && (
                      <button
                        className="flex items-center gap-1.5 px-4 py-2 rounded-lg bg-neon-red/10 border border-neon-red/20 text-neon-red text-xs font-medium hover:bg-neon-red/20 transition-colors"
                        onClick={() => handleStop(bot.id)}
                        disabled={stopping === bot.id}
                      >
                        {stopping === bot.id ? (
                          <Loader2 size={12} className="animate-spin" />
                        ) : (
                          <StopCircle size={12} />
                        )}
                        {stopping === bot.id ? 'Остановка...' : 'Стоп'}
                      </button>
                    )}
                    {bot.status === 'stopped' && (
                      <button
                        className="flex items-center gap-1.5 px-4 py-2 rounded-lg bg-neon-green/10 border border-neon-green/20 text-neon-green text-xs font-medium hover:bg-neon-green/20 transition-colors"
                        onClick={() => handleRestart(bot.id)}
                        disabled={restarting === bot.id}
                      >
                        {restarting === bot.id ? (
                          <Loader2 size={12} className="animate-spin" />
                        ) : (
                          <Play size={12} />
                        )}
                        {restarting === bot.id ? 'Перезапуск...' : 'Перезапустить'}
                      </button>
                    )}
                  </div>
                </div>
                {bot.error && (
                  <div className="mt-3 flex items-center gap-2 text-xs text-neon-red bg-neon-red/5 rounded-lg px-3 py-2">
                    <AlertTriangle size={12} />
                    {bot.error}
                  </div>
                )}
                {bot.planned_trade && isRunning && (
                  <PlannedTradePanel planned={bot.planned_trade} />
                )}
                {bot.status === 'stopped' && (
                  <div className="mt-3 flex items-center gap-2 text-xs text-gray-500 bg-gray-500/5 rounded-lg px-3 py-2">
                    <XCircle size={12} />
                    Бот остановлен
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}

      {/* Deploy Modal */}
      {showDeploy && (
        <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50" onClick={() => setShowDeploy(false)}>
          <div className="glass max-w-md w-full mx-4 max-h-[90vh] overflow-auto" onClick={e => e.stopPropagation()}>
            <div className="p-5 space-y-4">
              <div className="flex items-center gap-3">
                <Bot size={24} className="text-neon-blue" />
                <h3 className="text-lg font-bold text-white">Запуск торгового бота</h3>
              </div>

              <div className="space-y-3">
                <div>
                  <label className="text-xs text-gray-400 mb-1 block">Стратегия</label>
                  <select
                    className="w-full text-sm"
                    value={deployForm.strategy_id}
                    onChange={e => setDeployForm({ ...deployForm, strategy_id: e.target.value })}
                  >
                    {strategies.map(s => (
                      <option key={s.id} value={s.id}>{s.name} ({s.timeframe})</option>
                    ))}
                  </select>
                </div>

                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label className="text-xs text-gray-400 mb-1 block">Торговая пара</label>
                    <select
                      className="w-full text-sm"
                      value={deployForm.symbol}
                      onChange={e => setDeployForm({ ...deployForm, symbol: e.target.value })}
                    >
                      {symbols.map(s => (
                        <option key={s} value={s}>{s}</option>
                      ))}
                    </select>
                  </div>
                  <div>
                    <label className="text-xs text-gray-400 mb-1 block">Таймфрейм</label>
                    <select
                      className="w-full text-sm"
                      value={deployForm.timeframe}
                      onChange={e => setDeployForm({ ...deployForm, timeframe: e.target.value })}
                    >
                      {timeframeOptions.map(tf => (
                        <option key={tf} value={tf}>{tf}</option>
                      ))}
                    </select>
                  </div>
                </div>

                <div>
                  <label className="text-xs text-gray-400 mb-1 block">Капитал (USDT)</label>
                  <input
                    type="number"
                    className="w-full text-sm"
                    min="10"
                    value={deployForm.capital}
                    onChange={e => setDeployForm({ ...deployForm, capital: +e.target.value })}
                  />
                </div>
              </div>

              <div className="bg-neon-yellow/5 border border-neon-yellow/20 rounded-xl p-3">
                <p className="text-xs text-neon-yellow">
                  Бот будет проверять сигнал каждые {deployForm.timeframe === '1m' ? '60' : deployForm.timeframe === '5m' ? '240' : deployForm.timeframe === '15m' ? '600' : '1200'} сек и исполнять сделки автоматически.
                </p>
              </div>

              <div className="flex gap-3">
                <button
                  className="btn-neon flex-1 py-2.5 rounded-xl text-sm font-semibold flex items-center justify-center gap-2"
                  onClick={handleDeploy}
                  disabled={deploying || !deployForm.strategy_id}
                >
                  {deploying ? <Loader2 size={14} className="animate-spin" /> : <Play size={14} />}
                  {deploying ? 'Запуск...' : 'Запустить бота'}
                </button>
                <button
                  className="bg-white/5 text-white flex-1 py-2.5 rounded-xl text-sm font-semibold hover:bg-white/10"
                  onClick={() => setShowDeploy(false)}
                >
                  Отмена
                </button>
              </div>

              {deployResult && (
                <div className={`flex items-center gap-2 p-3 rounded-lg text-sm ${
                  deployResult.error ? 'bg-neon-red/5 text-neon-red' : 'bg-neon-green/5 text-neon-green'
                }`}>
                  {deployResult.error ? <XCircle size={14} /> : <CheckCircle size={14} />}
                  {deployResult.error || `Бот ${deployResult.bot_id} запущен! Цикл: ${deployResult.cycle_interval_sec}с`}
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
