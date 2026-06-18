import React, { useState, useEffect } from 'react'
import { Wallet, TrendingUp, TrendingDown, Activity, BarChart3, Zap, Clock, ArrowUpRight, ArrowDownRight, Bot, ScrollText, DollarSign, XCircle, Loader2, Target } from 'lucide-react'
import { api } from '../services/api'
import { useTranslation } from '../hooks/useTranslation'

function StatCard({ label, value, change, icon: Icon, positive }) {
  return (
    <div className="glass p-5 flex flex-col gap-1">
      <div className="flex items-center justify-between">
        <span className="text-xs text-gray-400 font-medium uppercase tracking-wider">{label}</span>
        <Icon size={16} className={positive ? 'text-neon-green' : 'text-neon-red'} />
      </div>
      <span className="text-2xl font-bold text-white">{value}</span>
      {change != null && (
        <span className={`text-xs flex items-center gap-1 ${positive ? 'text-neon-green' : 'text-neon-red'}`}>
          {positive ? <ArrowUpRight size={12} /> : <ArrowDownRight size={12} />}
          {change}
        </span>
      )}
    </div>
  )
}

function DashboardPlannedTrade({ planned }) {
  if (!planned) return null
  const action = planned.action || 'WAIT'
  const isEntry = action === 'LONG' || action === 'SHORT'
  const isInPosition = action === 'IN_POSITION'

  const actionLabels = {
    LONG: 'Покупка (LONG)',
    SHORT: 'Продажа (SHORT)',
    HOLD: 'Удержание',
    WAIT: 'Ожидание',
    IN_POSITION: `${planned.side === 'LONG' ? 'Длинная' : 'Короткая'} позиция`,
  }

  const trendLabels = { UP: '▲ Восходящий', DOWN: '▼ Нисходящий', NEUTRAL: '— Боковой' }

  return (
    <div className="border-t border-white/5 px-4 py-3 space-y-2.5">
      <div className="flex items-center gap-2 text-xs">
        <Target size={12} className={action === 'LONG' ? 'text-neon-green' : action === 'SHORT' ? 'text-neon-red' : 'text-gray-400'} />
        <span className="font-semibold text-white">План:</span>
        <span className={
          action === 'LONG' ? 'text-neon-green' :
          action === 'SHORT' ? 'text-neon-red' :
          action === 'IN_POSITION' ? 'text-neon-blue' :
          'text-gray-400'
        }>
          {actionLabels[action] || action}
        </span>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-x-4 gap-y-1 text-xs">
        <div className="text-gray-400">Цена: <span className="text-white font-mono">${planned.current_price?.toLocaleString()}</span></div>
        <div className="text-gray-400">EMA200: <span className="text-white font-mono">${planned.ema200?.toLocaleString()}</span></div>
        <div className="text-gray-400">RSI: <span className="text-white font-mono">{planned.rsi}</span></div>
        <div className={`font-semibold ${planned.trend === 'UP' ? 'text-neon-green' : planned.trend === 'DOWN' ? 'text-neon-red' : 'text-gray-400'}`}>
          {trendLabels[planned.trend] || planned.trend}
        </div>
      </div>

      <div className="grid grid-cols-2 gap-x-4 text-xs">
        <div className="text-gray-400">С윙 макс: <span className="text-neon-green font-mono">${planned.swing_high?.toLocaleString()}</span></div>
        <div className="text-gray-400">С윙 мин: <span className="text-neon-red font-mono">${planned.swing_low?.toLocaleString()}</span></div>
      </div>

      {isEntry && planned.entry_zone && (
        <div className="bg-white/5 rounded-lg px-3 py-2.5 space-y-2">
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
          {planned.distance_to_short != null && (
            <div className="text-[11px] text-gray-500">
              Расстояние до входа: ${planned.distance_to_short?.toLocaleString()} ({((planned.distance_to_short / planned.current_price) * 100).toFixed(2)}%)
            </div>
          )}
        </div>
      )}

      {isInPosition && (
        <div className="bg-white/5 rounded-lg px-3 py-2 space-y-1">
          <div className="flex items-center gap-3 text-xs">
            <span className="text-gray-400">Вход: <span className="font-mono text-white">${planned.entry_price?.toLocaleString()}</span></span>
            {planned.unrealized_pnl != null && (
              <span className={`font-bold ${planned.unrealized_pnl >= 0 ? 'text-neon-green' : 'text-neon-red'}`}>
                {planned.unrealized_pnl >= 0 ? '+' : ''}{planned.unrealized_pnl?.toFixed(2)} USDT
              </span>
            )}
          </div>
          {planned.stop_loss && (
            <div className="text-[11px] text-gray-400">
              Трейлинг стоп: {planned.trailing_stop_active ? '🟢 активен' : '⚪ ожидание'} · Стоп: <span className="font-mono">${planned.stop_loss?.toLocaleString()}</span>
            </div>
          )}
          {planned.exit_conditions && (
            <div className="text-[11px] text-gray-500 space-y-0.5">
              {planned.exit_conditions.map((c, i) => <div key={i}>→ {c}</div>)}
            </div>
          )}
        </div>
      )}

      {planned.note && !isEntry && !isInPosition && (
        <div className="text-[11px] text-gray-500 italic">{planned.note}</div>
      )}
    </div>
  )
}

export default function Dashboard({ health, connected, isGuest }) {
  const { t } = useTranslation()
  const [portfolio, setPortfolio] = useState(null)
  const [positions, setPositions] = useState([])
  const [ticker, setTicker] = useState(null)
  const [liveBots, setLiveBots] = useState([])
  const [tradeLog, setTradeLog] = useState([])
  const [pnl, setPnl] = useState(null)
  const [closing, setClosing] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    loadData()
    const interval = setInterval(loadData, 10000)
    return () => clearInterval(interval)
  }, [connected])

  async function loadData() {
    if (!connected) { setLoading(false); return }
    try {
      const [pf, pos, tk, bots, trades, pnlData] = await Promise.all([
        api.getPortfolio().catch(() => null),
        api.getPositions('SWAP').catch(() => null),
        api.getTicker('BTC-USDT-SWAP').catch(() => null),
        api.listBots().catch(() => null),
        api.getPairedTrades(15).catch(() => null),
        api.getPnl().catch(() => null),
      ])
      if (pf) setPortfolio(pf)
      if (pos) setPositions(pos.positions || [])
      if (tk) setTicker(tk)
      if (bots) setLiveBots(bots.bots || [])
      if (trades) setTradeLog(trades.trades || [])
      if (pnlData) setPnl(pnlData)
    } catch {}
    setLoading(false)
  }

  const btcUsd = ticker ? parseFloat(ticker.last) : 0
  const btcChange = ticker ? parseFloat(ticker.change24h || 0).toFixed(2) : '0.00'
  const btcPositive = parseFloat(btcChange) >= 0

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold text-white">{t('dashboard.title')}</h2>
          <p className="text-sm text-gray-400 mt-1">{t('dashboard.subtitle')}</p>
        </div>
        <div className="flex items-center gap-2">
          <Activity size={14} className="text-gray-400" />
          <span className="text-xs text-gray-500">
            {connected ? t('dashboard.websocket') : t('dashboard.websocket_offline')}
          </span>
          <span className={`status-dot ${connected ? 'online' : 'offline'}`} />
        </div>
      </div>

      {/* Stats Grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard
          label={t('dashboard.portfolio_value')}
          value={portfolio ? `$${(portfolio.totalEqUsd || 0).toLocaleString()}` : '---'}
          icon={Wallet}
          positive
        />
        <StatCard
          label={t('dashboard.btc_price')}
          value={btcUsd ? `$${btcUsd.toLocaleString()}` : '---'}
          change={`${btcChange}%`}
          icon={TrendingUp}
          positive={btcPositive}
        />
        <StatCard
          label={t('dashboard.open_positions')}
          value={positions.length}
          icon={BarChart3}
          positive={positions.length > 0}
        />
        <StatCard
          label={t('dashboard.status')}
          value={connected ? t('dashboard.online') : t('dashboard.offline')}
          icon={Zap}
          positive={connected}
        />
      </div>

      {/* PNL Row */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <div className="glass p-5 flex items-center gap-4">
          <div className="w-10 h-10 rounded-lg bg-neon-green/10 flex items-center justify-center">
            <DollarSign size={20} className="text-neon-green" />
          </div>
          <div>
            <div className="text-xs text-gray-400 font-medium uppercase tracking-wider">{t('dashboard.pnl_day')}</div>
            <div className={`text-xl font-bold ${pnl && pnl['1d'] >= 0 ? 'text-neon-green' : 'text-neon-red'}`}>
              {pnl ? `${pnl['1d'] >= 0 ? '+' : ''}$${pnl['1d'].toFixed(2)}` : t('dashboard.no_pnl')}
            </div>
          </div>
        </div>
        <div className="glass p-5 flex items-center gap-4">
          <div className="w-10 h-10 rounded-lg bg-neon-purple/10 flex items-center justify-center">
            <DollarSign size={20} className="text-neon-purple" />
          </div>
          <div>
            <div className="text-xs text-gray-400 font-medium uppercase tracking-wider">{t('dashboard.pnl_week')}</div>
            <div className={`text-xl font-bold ${pnl && pnl['7d'] >= 0 ? 'text-neon-green' : 'text-neon-red'}`}>
              {pnl ? `${pnl['7d'] >= 0 ? '+' : ''}$${pnl['7d'].toFixed(2)}` : t('dashboard.no_pnl')}
            </div>
          </div>
        </div>
        <div className="glass p-5 flex items-center gap-4">
          <div className="w-10 h-10 rounded-lg bg-neon-blue/10 flex items-center justify-center">
            <DollarSign size={20} className="text-neon-blue" />
          </div>
          <div>
            <div className="text-xs text-gray-400 font-medium uppercase tracking-wider">{t('dashboard.pnl_month')}</div>
            <div className={`text-xl font-bold ${pnl && pnl['30d'] >= 0 ? 'text-neon-green' : 'text-neon-red'}`}>
              {pnl ? `${pnl['30d'] >= 0 ? '+' : ''}$${pnl['30d'].toFixed(2)}` : t('dashboard.no_pnl')}
            </div>
          </div>
        </div>
      </div>

      {/* Portfolio Details */}
      <div className="glass p-5">
        <h3 className="text-sm font-semibold text-white mb-4 flex items-center gap-2">
          <Wallet size={16} className="text-neon-green" />
          {t('dashboard.portfolio_balance')}
        </h3>
        {portfolio ? (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-gray-400 border-b border-white/5">
                  <th className="text-left py-3 px-2 font-medium">{t('dashboard.asset')}</th>
                  <th className="text-right py-3 px-2 font-medium">{t('dashboard.balance')}</th>
                  <th className="text-right py-3 px-2 font-medium">{t('dashboard.usd_value')}</th>
                  <th className="text-right py-3 px-2 font-medium">{t('dashboard.available')}</th>
                  <th className="text-right py-3 px-2 font-medium">{t('dashboard.frozen')}</th>
                </tr>
              </thead>
              <tbody>
                {(portfolio.details || []).map(d => (
                  <tr key={d.ccy} className="border-b border-white/5 hover:bg-white/5 transition-colors">
                    <td className="py-3 px-2 font-medium text-white">{d.ccy}</td>
                    <td className="py-3 px-2 text-right mono">{parseFloat(d.eq).toFixed(4)}</td>
                    <td className="py-3 px-2 text-right mono text-neon-green">
                      ${parseFloat(d.eqUsd).toLocaleString()}
                    </td>
                    <td className="py-3 px-2 text-right mono">{parseFloat(d.availBal).toFixed(4)}</td>
                    <td className="py-3 px-2 text-right mono text-gray-400">{parseFloat(d.frozenBal).toFixed(4)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="text-center py-8 text-gray-500">
            <p>{t('dashboard.no_portfolio')}</p>
          </div>
        )}
      </div>

      {/* Live Bots */}
      <div className="glass p-5">
        <h3 className="text-sm font-semibold text-white mb-4 flex items-center gap-2">
          <Bot size={16} className="text-neon-green" />
          {t('dashboard.active_bots') || 'Активные боты'}
          {liveBots.length > 0 && (
            <span className="text-xs text-neon-green bg-neon-green/10 px-2 py-0.5 rounded-full">
              {liveBots.filter(b => b.status === 'running').length}/{liveBots.length}
            </span>
          )}
        </h3>
        {liveBots.length > 0 ? (
          <div className="space-y-4">
            {liveBots.map(bot => (
              <div key={bot.id} className="bg-white/5 rounded-xl overflow-hidden">
                <div className="flex items-center justify-between px-4 py-3">
                  <div>
                    <div className="text-sm font-medium text-white flex items-center gap-2">
                      {bot.strategy_id} — {bot.symbol}
                      {bot.strategy_id === 'trend_momentum_pro' && <span className="px-1.5 py-0.5 rounded text-[10px] font-bold bg-gradient-to-r from-purple-500 to-pink-500 text-white">AI</span>}
                      <span className={`px-1.5 py-0.5 rounded text-[10px] font-bold ${
                        bot.status === 'running' ? 'bg-neon-green/20 text-neon-green' :
                        bot.status === 'error' ? 'bg-red-500/20 text-red-400' :
                        'bg-gray-500/20 text-gray-400'
                      }`}>{bot.status}</span>
                    </div>
                    <div className="text-xs text-gray-400">{bot.timeframe} · циклов: {bot.cycle_count}</div>
                  </div>
                  <div className="flex items-center gap-2">
                    <div className="text-right">
                      {bot.position !== 0 ? (
                        <>
                          <div className={`text-xs font-bold ${bot.position > 0 ? 'text-neon-green' : 'text-neon-red'}`}>
                            {bot.position > 0 ? '▲ LONG' : '▼ SHORT'} {Math.abs(bot.position).toFixed(6)} BTC
                          </div>
                          {bot.pnl !== 0 && (
                            <div className={`text-xs ${bot.pnl >= 0 ? 'text-neon-green' : 'text-neon-red'}`}>
                              {bot.pnl >= 0 ? '+' : ''}{bot.pnl.toFixed(2)} USDT
                            </div>
                          )}
                        </>
                      ) : (
                        <span className="text-xs text-gray-500">Нет позиции</span>
                      )}
                    </div>
                    {bot.status === 'running' ? (
                      <button
                        onClick={async () => {
                          try {
                            await api.stopBot(bot.id)
                            loadData()
                          } catch (e) {
                            alert('Ошибка: ' + e.message)
                          }
                        }}
                        className="px-3 py-1.5 rounded-lg text-xs font-medium bg-red-500/10 text-red-400 hover:bg-red-500/20 transition-colors"
                      >
                        Стоп
                      </button>
                    ) : (
                      <button
                        onClick={async () => {
                          try {
                            await api.startBot(bot.id)
                            loadData()
                          } catch (e) {
                            alert('Ошибка: ' + e.message)
                          }
                        }}
                        className="px-3 py-1.5 rounded-lg text-xs font-medium bg-neon-green/10 text-neon-green hover:bg-neon-green/20 transition-colors"
                      >
                        Старт
                      </button>
                    )}
                  </div>
                </div>
                {bot.planned_trade && <DashboardPlannedTrade planned={bot.planned_trade} />}
              </div>
            ))}
          </div>
        ) : (
          <p className="text-sm text-gray-500">Нет ботов. Задеплой стратегию на вкладке Live Trading</p>
        )}
      </div>

      {/* Recent Trades */}
      <div className="glass p-5">
        <h3 className="text-sm font-semibold text-white mb-4 flex items-center gap-2">
          <ScrollText size={16} className="text-neon-blue" />
          {t('dashboard.recent_trades') || 'Последние сделки'}
        </h3>
        {tradeLog.length > 0 ? (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-gray-400 border-b border-white/5">
                  <th className="text-left py-2 px-1 font-medium">Время входа</th>
                  <th className="text-left py-2 px-1 font-medium">Выход</th>
                  <th className="text-left py-2 px-1 font-medium">Пара</th>
                  <th className="text-center py-2 px-1 font-medium">Направление</th>
                  <th className="text-right py-2 px-1 font-medium">Цена входа</th>
                  <th className="text-right py-2 px-1 font-medium">Цена выхода</th>
                  <th className="text-right py-2 px-1 font-medium">Объём</th>
                  <th className="text-right py-2 px-1 font-medium">P&amp;L</th>
                </tr>
              </thead>
              <tbody>
                {tradeLog.slice(0, 20).map((t, i) => {
                  const pnl = t.pnl != null ? parseFloat(t.pnl) : null
                  return (
                  <tr key={t.signal_id || i} className="border-b border-white/5 hover:bg-white/5 transition-colors">
                    <td className="py-2 px-1 text-xs text-gray-400">
                      {t.entry_time ? new Date(t.entry_time).toLocaleString() : '-'}
                    </td>
                    <td className="py-2 px-1 text-xs text-gray-400">
                      {t.exit_time ? new Date(t.exit_time).toLocaleString() : 'открыта'}
                    </td>
                    <td className="py-2 px-1 text-white font-medium">{t.inst_id || '-'}</td>
                    <td className="py-2 px-1 text-center">
                      <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${
                        t.side === 'buy' ? 'bg-neon-green/10 text-neon-green' : 'bg-neon-red/10 text-neon-red'
                      }`}>
                        {t.side === 'buy' ? 'LONG' : 'SHORT'}
                      </span>
                    </td>
                    <td className="py-2 px-1 text-right mono">
                      {t.entry_px ? `$${parseFloat(t.entry_px).toLocaleString()}` : '-'}
                    </td>
                    <td className="py-2 px-1 text-right mono">
                      {t.exit_px ? `$${parseFloat(t.exit_px).toLocaleString()}` : '-'}
                    </td>
                    <td className="py-2 px-1 text-right mono">{t.entry_sz || '-'}</td>
                    <td className="py-2 px-1 text-right">
                      {pnl !== null ? (
                        <span className={`mono text-xs font-bold ${pnl >= 0 ? 'text-neon-green' : 'text-neon-red'}`}>
                          {pnl >= 0 ? '+' : ''}{pnl.toFixed(2)} USDT
                        </span>
                      ) : (
                        <span className="text-xs text-gray-500">открыта</span>
                      )}
                    </td>
                  </tr>
                )})}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="text-sm text-gray-500">Сделок пока нет</p>
        )}
      </div>

      {/* Positions & Market Info */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div className="glass p-5">
          <h3 className="text-sm font-semibold text-white mb-4 flex items-center gap-2">
            <TrendingUp size={16} className="text-neon-blue" />
            {t('dashboard.open_positions_title')}
          </h3>
          {positions.length > 0 ? (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-gray-400 border-b border-white/5">
                    <th className="text-left py-2 px-1 font-medium">{t('dashboard.pair')}</th>
                    <th className="text-right py-2 px-1 font-medium">{t('dashboard.size')}</th>
                    <th className="text-right py-2 px-1 font-medium">{t('dashboard.entry')}</th>
                    <th className="text-right py-2 px-1 font-medium">{t('dashboard.mark')}</th>
                    <th className="text-right py-2 px-1 font-medium">{t('dashboard.pnl')}</th>
                    <th className="text-right py-2 px-1 font-medium">{t('dashboard.actions')}</th>
                  </tr>
                </thead>
                <tbody>
                  {positions.slice(0, 10).map((p, i) => {
                    const pnl = parseFloat(p.upl || 0)
                    const positive = pnl >= 0
                    const posId = `${p.instId}_${p.posSide}`
                    return (
                      <tr key={i} className="border-b border-white/5 hover:bg-white/5 transition-colors">
                        <td className="py-2 px-1 text-white font-medium">{p.instId}</td>
                        <td className="py-2 px-1 text-right mono">{parseFloat(p.pos).toFixed(3)}</td>
                        <td className="py-2 px-1 text-right mono">${parseFloat(p.avgPx).toLocaleString()}</td>
                        <td className="py-2 px-1 text-right mono">${parseFloat(p.markPx).toLocaleString()}</td>
                        <td className={`py-2 px-1 text-right mono ${positive ? 'text-neon-green' : 'text-neon-red'}`}>
                          ${pnl.toLocaleString()}
                        </td>
                        <td className="py-2 px-1 text-right">
                          {isGuest ? (
                            <span className="text-xs text-gray-500">—</span>
                          ) : (
                          <button
                            onClick={async () => {
                              setClosing(posId)
                              try {
                                await api.closePosition(p.instId, p.posSide, p.pos, p.mgnMode || 'cross')
                                loadData()
                              } catch (e) {
                                alert('Ошибка закрытия: ' + e.message)
                              } finally {
                                setClosing(null)
                              }
                            }}
                            disabled={closing === posId}
                            className="flex items-center gap-1 px-2.5 py-1.5 rounded-lg text-xs font-medium bg-neon-red/10 border border-neon-red/20 text-neon-red hover:bg-neon-red/20 transition-colors disabled:opacity-50"
                            title="Закрыть позицию"
                          >
                            {closing === posId ? <Loader2 size={12} className="animate-spin" /> : <XCircle size={12} />}
                            {closing === posId ? '...' : 'Закрыть'}
                          </button>
                          )}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="text-center py-8 text-gray-500">
              <p>{t('dashboard.no_positions')}</p>
            </div>
          )}
        </div>

        <div className="glass p-5">
          <h3 className="text-sm font-semibold text-white mb-4 flex items-center gap-2">
            <BarChart3 size={16} className="text-neon-purple" />
            {t('dashboard.market_data')}
          </h3>
          {ticker ? (
            <div className="space-y-3">
              {[
                { label: t('dashboard.last_price'), value: `$${parseFloat(ticker.last).toLocaleString()}`, color: 'text-white' },
                { label: t('dashboard.bid'), value: `$${parseFloat(ticker.bid).toLocaleString()}`, color: 'text-neon-green' },
                { label: t('dashboard.ask'), value: `$${parseFloat(ticker.ask).toLocaleString()}`, color: 'text-neon-red' },
                { label: t('dashboard.high_24h'), value: `$${parseFloat(ticker.high24h).toLocaleString()}`, color: 'text-neon-green' },
                { label: t('dashboard.low_24h'), value: `$${parseFloat(ticker.low24h).toLocaleString()}`, color: 'text-neon-red' },
                { label: t('dashboard.change_24h'), value: `${btcChange}%`, color: btcPositive ? 'text-neon-green' : 'text-neon-red' },
                { label: t('dashboard.volume_24h'), value: ticker.vol24h ? `${parseFloat(ticker.vol24h).toFixed(2)} BTC` : '---', color: 'text-gray-300' },
              ].map(item => (
                <div key={item.label} className="flex justify-between items-center">
                  <span className="text-sm text-gray-400">{item.label}</span>
                  <span className={`mono text-sm font-medium ${item.color}`}>{item.value}</span>
                </div>
              ))}
            </div>
          ) : (
            <div className="text-center py-8 text-gray-500">
              <p>{t('dashboard.no_market_data')}</p>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
