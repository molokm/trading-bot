import React, { useState, useEffect, useMemo } from 'react'
import {
  Wallet, TrendingUp, TrendingDown, Activity, XCircle, Loader2, Zap,
  ArrowUpRight, ArrowDownRight, BarChart3, Play, Square, ChevronDown, Filter, ScrollText,
  Clock, Bot
} from 'lucide-react'
import { api } from '../services/api'
import { MetricCard, Tip, StatusBadge, Chip, PnlBar, EmptyState, Loader } from '../components/ui'

const PAIRS = ['Все', 'BTC', 'ETH', 'SOL', 'BNB']
const REASON_MAP = {
  tp: { label: 'TP', color: 'text-[var(--profit)]' },
  sl: { label: 'SL', color: 'text-[var(--loss)]' },
  trail: { label: 'Trail', color: 'text-[var(--info)]' },
  breakeven: { label: 'BE', color: 'text-[var(--warn)]' },
  manual: { label: 'Manual', color: 'text-[var(--txt-secondary)]' },
  roe_threshold: { label: 'ROE', color: 'text-accent-purple' },
}

/* ═══════ Animated Value — smooth colour transition ═══════ */
function AnimatedValue({ value, className = '' }) {
  return (
    <span className={`transition-all duration-500 ${className}`}>
      {value}
    </span>
  )
}

/* ═══════ Sparkline SVG — 60×20 trend line ═══════ */
function Sparkline({ data, width = 60, height = 20 }) {
  if (!data || data.length < 2) return null
  const min = Math.min(...data)
  const max = Math.max(...data)
  const range = max - min || 1
  const stepX = width / (data.length - 1)
  const pathD = data.map((v, i) => {
    const x = i * stepX
    const y = height - ((v - min) / range) * height
    return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`
  }).join(' ')
  const isPositive = data[data.length - 1] >= data[0]
  const color = isPositive ? 'var(--profit)' : 'var(--loss)'
  return (
    <svg width={width} height={height} className='inline-block'>
      <path d={pathD} fill='none' stroke={color} strokeWidth='1.5' strokeLinecap='round' strokeLinejoin='round' />
    </svg>
  )
}

/* ═══════ Helper: check admin access ═══════ */
function isAdmin(isGuest) {
  return !isGuest
}

/* ═══════ Dashboard ═══════ */
export default function Dashboard({ health, connected, isGuest, demoMode }) {
  const [portfolio, setPortfolio] = useState(null)
  const [positions, setPositions] = useState([])
  const [ticker, setTicker] = useState(null)
  const [momentumStatus, setMomentumStatus] = useState(null)
  const [momentumTrades, setMomentumTrades] = useState([])
  const [tradeLog, setTradeLog] = useState([])
  const [pnl, setPnl] = useState(null)
  const [closing, setClosing] = useState(null)
  const [loading, setLoading] = useState(true)
  const [botUptime, setBotUptime] = useState(0)

  // Filters
  const [filterPair, setFilterPair] = useState('Все')
  const [filterResult, setFilterResult] = useState('all') // all | win | loss
  const [filterReason, setFilterReason] = useState('all')

  // Uptime counter
  useEffect(() => {
    if (!momentumStatus?.running) { setBotUptime(0); return }
    const id = setInterval(() => setBotUptime(s => s + 1), 1000)
    return () => clearInterval(id)
  }, [momentumStatus?.running])

  useEffect(() => {
    loadData()
    const interval = setInterval(loadData, 10000)
    return () => clearInterval(interval)
  }, [connected])

  async function loadData() {
    if (!connected) { setLoading(false); return }
    try {
      const [pf, pos, tk, momStatus, momTrades, trades, pnlData] = await Promise.all([
        api.getPortfolio().catch(() => null),
        api.getPositions('SWAP').catch(() => null),
        api.getTicker('BTC-USDT-SWAP').catch(() => null),
        api.momentumStatus().catch(() => null),
        api.momentumTrades(30).catch(() => null),
        api.getPairedTrades(50).catch(() => null),
        api.getPnl().catch(() => null),
      ])
      if (pf) setPortfolio(pf)
      if (pos) setPositions(pos.positions || [])
      if (tk) setTicker(tk)
      if (momStatus) setMomentumStatus(momStatus)
      if (momTrades) setMomentumTrades(momTrades.trades || [])
      if (trades) setTradeLog(trades.trades || [])
      if (pnlData) setPnl(pnlData)
    } catch {}
    setLoading(false)
  }

  // Derived values (declared early — before any useMemo that depends on them)
  const btcUsd = ticker ? parseFloat(ticker.last) : 0
  const btcChange = ticker ? parseFloat(ticker.change24h || 0).toFixed(2) : '0.00'
  const totalEquity = portfolio ? portfolio.totalEqUsd || 0 : 0
  const unrealizedPnl = pnl?.unrealized || 0
  const pnlDay = pnl?.['1d'] || 0
  const pnlWeek = pnl?.['7d'] || 0
  const pnlMonth = pnl?.['30d'] || 0

  // Filtered trades
  const filteredTrades = useMemo(() => {
    return tradeLog.filter(t => {
      if (filterPair !== 'Все') {
        const pair = (t.inst_id || '').toUpperCase()
        if (!pair.includes(filterPair)) return false
      }
      const pnlVal = parseFloat(t.pnl || 0)
      if (filterResult === 'win' && pnlVal < 0) return false
      if (filterResult === 'loss' && pnlVal >= 0) return false
      if (filterReason !== 'all') {
        if ((t.reason || '').toLowerCase() !== filterReason) return false
      }
      return true
    })
  }, [tradeLog, filterPair, filterResult, filterReason])

  // Sparkline data for golden-zone MetricCards (stable random 10-point trends)
  const sparkData = useMemo(() =>
    Array.from({ length: 6 }, () =>
      Array.from({ length: 10 }, () => Math.random() * 100)
    )
  , [])

  // Summary stats for visible trades
  const tradesSummary = useMemo(() => {
    const visible = (filteredTrades.length > 0 ? filteredTrades : tradeLog).slice(0, 30)
    const totalPnl = visible.reduce((s, t) => s + parseFloat(t.pnl || 0), 0)
    const wins = visible.filter(t => parseFloat(t.pnl || 0) >= 0).length
    const losses = visible.filter(t => parseFloat(t.pnl || 0) < 0).length
    return { totalPnl, wins, losses, count: visible.length }
  }, [filteredTrades, tradeLog])

  // Synthetic BTC sparkline (visual only) — btcChange is now declared above
  const btcSparkData = useMemo(() => {
    const isUp = parseFloat(btcChange) >= 0
    return Array.from({ length: 10 }, (_, i) =>
      isUp ? 50 - i * 2 + Math.random() * 4 : 50 + i * 2 + Math.random() * 4
    )
  }, [btcChange])

  const formatUptime = (s) => {
    const h = Math.floor(s / 3600)
    const m = Math.floor((s % 3600) / 60)
    const sec = s % 60
    return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`
  }

  const handleClosePosition = async (p) => {
    const posId = `${p.instId}_${p.posSide}`
    setClosing(posId)
    try {
      await api.closePosition(p.instId, p.posSide, p.pos, p.mgnMode || 'cross')
      loadData()
    } catch (e) { alert('Ошибка: ' + e.message) }
    finally { setClosing(null) }
  }

  const fmt = (v, d = 2) => v != null ? v.toFixed(d) : '---'
  const fmtUsd = (v) => v != null ? `$${Math.abs(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : '---'
  const fmtTime = (ts) => ts ? new Date(ts).toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }) : '---'

  return (
    <div className="h-full flex flex-col p-4 gap-3 overflow-hidden">

      {/* ═══ GOLDEN ZONE — Key Metrics ═══ */}
      <div data-tour="metrics" className="flex-shrink-0 grid grid-cols-2 lg:grid-cols-6 gap-3">
        <MetricCard
          label="Баланс"
          value={<AnimatedValue>{totalEquity ? `$${totalEquity.toLocaleString()}` : '---'}</AnimatedValue>}
          mono
          tip="Общая стоимость портфеля по рыночным ценам"
          sparkData={sparkData[0]}
        />
        <MetricCard
          label="Unrealized PnL"
          value={
            <AnimatedValue className={unrealizedPnl >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}>
              {unrealizedPnl >= 0 ? `+$${fmt(unrealizedPnl)}` : `-$${fmt(Math.abs(unrealizedPnl))}`}
            </AnimatedValue>
          }
          changeType={unrealizedPnl >= 0 ? 'positive' : 'negative'}
          mono
          tip="Нереализованная прибыль/убыток по открытым позициям"
          sparkData={sparkData[1]}
        />
        <MetricCard
          label="PnL сегодня"
          value={
            <AnimatedValue className={pnlDay >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}>
              {pnlDay >= 0 ? `+$${fmt(pnlDay)}` : `-$${fmt(Math.abs(pnlDay))}`}
            </AnimatedValue>
          }
          changeType={pnlDay >= 0 ? 'positive' : 'negative'}
          mono
          tip="Реализованная прибыль/убыток за последние 24 часа"
          sparkData={sparkData[2]}
        />
        <MetricCard
          label="PnL неделя"
          value={
            <AnimatedValue className={pnlWeek >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}>
              {pnlWeek >= 0 ? `+$${fmt(pnlWeek)}` : `-$${fmt(Math.abs(pnlWeek))}`}
            </AnimatedValue>
          }
          changeType={pnlWeek >= 0 ? 'positive' : 'negative'}
          mono
          tip="Реализованная прибыль/убыток за 7 дней"
          sparkData={sparkData[3]}
        />
        <MetricCard
          label="Позиций"
          value={<AnimatedValue>{positions.length}</AnimatedValue>}
          mono
          tip="Количество открытых позиций"
          sparkData={sparkData[4]}
        />
        <MetricCard
          label="BTC"
          value={<AnimatedValue>{btcUsd ? `$${btcUsd.toLocaleString()}` : '---'}</AnimatedValue>}
          change={`${btcChange}%`}
          changeType={parseFloat(btcChange) >= 0 ? 'positive' : 'negative'}
          mono
          tip="Текущая цена Bitcoin-USDT Perpetual Swap"
          sparkData={sparkData[5]}
        />
      </div>

      {/* ═══ MAIN GRID 65/35 ═══ */}
      <div className="flex-1 grid grid-cols-1 lg:grid-cols-[1fr_380px] gap-3 min-h-0 main-grid">

        {/* ═══ LEFT — Positions + Trades ═══ */}
        <div className="flex flex-col gap-3 min-h-0 overflow-hidden">

          {/* Open Positions */}
          <div className="panel flex-1 flex flex-col min-h-0">
            <div className="panel-header">
              <Zap size={13} className="text-[var(--profit)]" />
              Открытые позиции
              <span className="ml-auto text-[var(--txt-muted)]">{positions.length}</span>
            </div>
            <div className="flex-1 overflow-auto">
              {loading ? (
                <div className="flex items-center justify-center py-12"><Loader /></div>
              ) : positions.length === 0 ? (
                <EmptyState icon={Zap} text="Нет открытых позиций" sub="Позиции появятся после запуска бота" />
              ) : (
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Пара</th>
                      <th className="text-right">Размер</th>
                      <th className="text-right">Entry</th>
                      <th className="text-right">Mark</th>
                      <th className="text-right">PnL</th>
                      <th className="text-right">ROE</th>
                      {isAdmin(isGuest) ? null : <th className="text-right"></th>}
                    </tr>
                  </thead>
                  <tbody>
                    {positions.map((p, i) => {
                      const upl = parseFloat(p.upl || 0)
                      const roe = parseFloat(p.uplRatio || 0) * 100
                      const posId = `${p.instId}_${p.posSide}`
                      return (
                        <tr key={i} style={{
                          background: upl >= 0
                            ? 'linear-gradient(90deg, rgba(0,255,136,0.06) 0%, transparent 50%)'
                            : 'linear-gradient(90deg, rgba(255,51,102,0.06) 0%, transparent 50%)',
                          boxShadow: `inset 2px 0 0 ${upl >= 0 ? 'rgba(0,255,136,0.4)' : 'rgba(255,51,102,0.4)'}`,
                        }}>
                          <td className="text-[var(--txt)] font-medium">{p.instId?.replace('-USDT-SWAP', '')}</td>
                          <td className="text-right mono">{parseFloat(p.pos).toFixed(3)}</td>
                          <td className="text-right mono">${parseFloat(p.avgPx).toLocaleString()}</td>
                          <td className="text-right mono">${parseFloat(p.markPx).toLocaleString()}</td>
                          <td className={`text-right mono font-semibold ${upl >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>
                            {upl >= 0 ? '+' : ''}{upl.toFixed(2)}
                          </td>
                          <td className={`text-right mono ${roe >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>
                            {roe.toFixed(2)}%
                          </td>
                          {!isGuest && (
                            <td className="text-right">
                              <button
                                className="btn btn-danger btn-sm"
                                onClick={() => handleClosePosition(p)}
                                disabled={closing === posId}
                              >
                                {closing === posId ? <Loader /> : <XCircle size={11} />}
                                Закрыть
                              </button>
                            </td>
                          )}
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              )}
            </div>
          </div>

          {/* Bot Status + Recent Trades */}
          <div className="panel flex-1 flex flex-col min-h-0">
            <div className="panel-header">
              <Activity size={13} className="text-accent-purple" />
              Последние сделки
              <div className="ml-auto flex gap-1">
                {['all', 'win', 'loss'].map(f => (
                  <Chip key={f} active={filterResult === f} onClick={() => setFilterResult(f)}>
                    {f === 'all' ? 'Все' : f === 'win' ? 'Прибыль' : 'Убыток'}
                  </Chip>
                ))}
              </div>
            </div>
            {tradeLog.length > 0 && (
              <div className="flex items-center gap-4 px-4 py-2 text-2xs bg-[var(--bg)] border-b border-[var(--border)]">
                <span className="text-[var(--txt-muted)]">
                  Показано: <span className="mono text-[var(--txt)] font-medium">{tradesSummary.count}</span>
                </span>
                <span className="text-[var(--txt-muted)]">
                  Сумма PnL: <span className={`mono font-bold ${tradesSummary.totalPnl >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>{tradesSummary.totalPnl >= 0 ? '+' : ''}{tradesSummary.totalPnl.toFixed(2)}</span>
                </span>
                <span className="text-[var(--txt-muted)]">
                  Прибыльных: <span className="mono text-[var(--profit)] font-medium">{tradesSummary.wins}</span>
                </span>
                <span className="text-[var(--txt-muted)]">
                  Убыточных: <span className="mono text-[var(--loss)] font-medium">{tradesSummary.losses}</span>
                </span>
              </div>
            )}
            <div className="flex-1 overflow-auto">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Вход</th>
                    <th>Выход</th>
                    <th>Пара</th>
                    <th>Direction</th>
                    <th className="text-right">Entry</th>
                    <th className="text-right">Exit</th>
                    <th className="text-right">PnL</th>
                    <th className="text-right">Причина</th>
                  </tr>
                </thead>
                <tbody>
                  {(filteredTrades.length > 0 ? filteredTrades : tradeLog).slice(0, 30).map((t, i) => {
                    const pnlVal = parseFloat(t.pnl || 0)
                    const reason = (t.reason || '').toLowerCase()
                    const reasonInfo = REASON_MAP[reason] || { label: t.reason || '-', color: 'text-[var(--txt-muted)]' }
                    return (
                      <tr key={t.signal_id || i}>
                        <td className="text-2xs mono text-[var(--txt-muted)]">{fmtTime(t.entry_time)}</td>
                        <td className="text-2xs mono text-[var(--txt-muted)]">{t.exit_time ? fmtTime(t.exit_time) : '—'}</td>
                        <td className="text-[var(--txt)] font-medium">{t.inst_id?.replace('-USDT-SWAP', '') || '-'}</td>
                        <td>
                          <span className={`text-2xs font-bold px-1.5 py-0.5 rounded ${t.side === 'buy' ? 'bg-[var(--profit-dim)] text-[var(--profit)]' : 'bg-[var(--loss-dim)] text-[var(--loss)]'}`}>
                            {t.side === 'buy' ? 'LONG' : 'SHORT'}
                          </span>
                        </td>
                        <td className="text-right mono">{t.entry_px ? `$${parseFloat(t.entry_px).toLocaleString()}` : '—'}</td>
                        <td className="text-right mono">{t.exit_px ? `$${parseFloat(t.exit_px).toLocaleString()}` : '—'}</td>
                        <td className="text-right">
                          <div className="flex items-center justify-end gap-2">
                            <PnlBar value={pnlVal} maxAbs={200} />
                            <span className={`mono text-2xs font-bold ${pnlVal >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>
                              {t.pnl != null ? `${pnlVal >= 0 ? '+' : ''}${pnlVal.toFixed(2)}` : '—'}
                            </span>
                          </div>
                        </td>
                        <td className="text-right">
                          <span className={`text-2xs font-medium ${reasonInfo.color}`}>{reasonInfo.label}</span>
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
              {tradeLog.length === 0 && <EmptyState icon={ScrollText} text="Сделок пока нет" />}
            </div>
          </div>
        </div>

        {/* ═══ RIGHT — Filters + Bot Log ═══ */}
        <div className="flex flex-col gap-3 min-h-0 right-panel">

          {/* Filter Chips */}
          <div className="panel flex-shrink-0">
            <div className="panel-header">
              <Filter size={13} className="text-[var(--info)]" />
              Фильтры
            </div>
            <div className="p-3 space-y-2">
              <div className="text-2xs text-[var(--txt-muted)] mb-1">Инструмент</div>
              <div className="flex flex-wrap gap-1">
                {PAIRS.map(p => (
                  <Chip key={p} active={filterPair === p} onClick={() => setFilterPair(p)}>{p}</Chip>
                ))}
              </div>
              <div className="text-2xs text-[var(--txt-muted)] mb-1 mt-3">Причина выхода</div>
              <div className="flex flex-wrap gap-1">
                {[{ k: 'all', l: 'Все' }, { k: 'tp', l: 'TP' }, { k: 'sl', l: 'SL' }, { k: 'trail', l: 'Trail' }, { k: 'breakeven', l: 'BE' }, { k: 'manual', l: 'Manual' }].map(r => (
                  <Chip key={r.k} active={filterReason === r.k} onClick={() => setFilterReason(r.k)}>{r.l}</Chip>
                ))}
              </div>
            </div>
          </div>

          {/* Bot Status Card */}
          <div className="panel flex-1 flex flex-col min-h-0">
            <div className="panel-header">
              <Bot size={13} className="text-[var(--warn)]" />
              Momentum Bot
              {momentumStatus?.running && <StatusBadge mode="live" label="Running" />}
              {!momentumStatus?.running && momentumStatus && <StatusBadge mode="stopped" label="Stopped" />}
            </div>
            <div className="flex-1 overflow-auto p-3 space-y-3">
              {momentumStatus?.running ? (
                <>
                  {/* Uptime */}
                  <div className="flex items-center justify-between px-3 py-2 rounded-md bg-[var(--bg)] border border-[var(--border)]">
                    <div className="flex items-center gap-1.5">
                      <Clock size={12} className="text-[var(--profit)]" />
                      <span className="text-2xs text-[var(--txt-muted)] uppercase tracking-wide">Время работы</span>
                    </div>
                    <span className="mono text-sm font-bold text-[var(--profit)]">{formatUptime(botUptime)}</span>
                  </div>

                  <div className="grid grid-cols-2 gap-2">
                    <div className="p-2 rounded-md bg-[var(--bg)]">
                      <div className="text-2xs text-[var(--txt-muted)]">Капитал</div>
                      <div className="mono text-sm font-semibold text-[var(--txt)] mt-0.5">${momentumStatus.equity?.toLocaleString() || '---'}</div>
                    </div>
                    <div className="p-2 rounded-md bg-[var(--bg)]">
                      <div className="text-2xs text-[var(--txt-muted)]">Позиций</div>
                      <div className="mono text-sm font-semibold text-[var(--txt)] mt-0.5">{momentumStatus.open_positions?.length || 0} / {momentumStatus.config?.max_positions || 4}</div>
                    </div>
                    <div className="p-2 rounded-md bg-[var(--bg)]">
                      <div className="text-2xs text-[var(--txt-muted)]">Сделок</div>
                      <div className="mono text-sm font-semibold text-[var(--txt)] mt-0.5">{momentumStatus.total_trades || 0}</div>
                    </div>
                    <div className="p-2 rounded-md bg-[var(--bg)]">
                      <div className="text-2xs text-[var(--txt-muted)]">Риск</div>
                      <div className="mono text-sm font-semibold text-[var(--txt)] mt-0.5">{((momentumStatus.config?.risk_per_trade || 0.03) * 100).toFixed(0)}%</div>
                    </div>
                  </div>

                  {/* Open bot positions */}
                  {momentumStatus.open_positions?.length > 0 && (
                    <div>
                      <div className="text-2xs text-[var(--txt-muted)] font-medium mb-1.5">Активные позиции бота</div>
                      <div className="space-y-1">
                        {momentumStatus.open_positions.map((p, i) => (
                          <div key={i} className="flex items-center justify-between text-2xs p-2 rounded-md bg-[var(--bg)]">
                            <div className="flex items-center gap-2">
                              <span className="px-1.5 py-0.5 rounded font-bold bg-[var(--profit-dim)] text-[var(--profit)]">LONG</span>
                              <span className="text-[var(--txt)] font-medium">{p.symbol}</span>
                            </div>
                            <div className="flex items-center gap-2 text-[var(--txt-muted)]">
                              <span>${p.entry?.toFixed(0)}</span>
                              <span className="text-[var(--loss)]">SL ${p.stop?.toFixed(0)}</span>
                            </div>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}

                  {/* Recent bot trades */}
                  {momentumTrades.length > 0 && (
                    <div>
                      <div className="text-2xs text-[var(--txt-muted)] font-medium mb-1.5">Лог бота</div>
                      <div className="space-y-1">
                        {momentumTrades.slice(0, 15).map((tr, i) => {
                          const isBuy = tr.side === 'buy'
                          return (
                            <div key={i} className="flex items-center justify-between text-2xs p-1.5 rounded bg-[var(--bg)]">
                              <div className="flex items-center gap-1.5">
                                <span className={`w-1.5 h-1.5 rounded-full ${isBuy ? 'bg-[var(--profit)]' : 'bg-[var(--loss)]'}`} />
                                <span className="text-[var(--txt)]">{tr.symbol}</span>
                              </div>
                              {tr.pnl != null && (
                                <span className={`mono font-semibold ${tr.pnl >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>
                                  {tr.pnl >= 0 ? '+' : ''}{tr.pnl.toFixed(2)}
                                </span>
                              )}
                              <span className="text-[var(--txt-muted)]">{tr.time ? new Date(tr.time).toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }) : ''}</span>
                            </div>
                          )
                        })}
                      </div>
                    </div>
                  )}

                  {!isGuest && (
                    <button
                      className="btn btn-danger btn-sm w-full"
                      onClick={async () => { try { await api.momentumStop(); loadData() } catch (e) { alert(e.message) } }}
                    >
                      <Square size={12} /> Остановить бота
                    </button>
                  )}
                </>
              ) : (
                <div className="text-center py-6">
                  <p className="text-xs text-[var(--txt-muted)] mb-3">Бот не запущен</p>
                  {!isGuest && (
                    <button
                      className="btn btn-primary btn-sm"
                      onClick={async () => { try { await api.momentumStart({}); loadData() } catch (e) { alert(e.message) } }}
                    >
                      <Play size={12} /> Запустить
                    </button>
                  )}
                </div>
              )}
            </div>
          </div>

          {/* Market Data */}
          <div className="panel flex-shrink-0">
            <div className="panel-header">
              <BarChart3 size={13} className="text-[var(--info)]" />
              BTC-USDT
              {ticker && (
                <span className={`ml-auto inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-2xs font-bold ${
                  parseFloat(btcChange) >= 0
                    ? 'bg-[var(--profit-dim)] text-[var(--profit)]'
                    : 'bg-[var(--loss-dim)] text-[var(--loss)]'
                }`}>
                  {parseFloat(btcChange) >= 0 ? '▲' : '▼'} {parseFloat(btcChange) >= 0 ? '+' : '-'}{Math.abs(parseFloat(btcChange)).toFixed(2)}%
                </span>
              )}
            </div>
            <div className="p-3">
              {ticker && (
                <div className="flex items-center justify-between mb-2.5 pb-2 border-b border-[var(--border)]">
                  <span className="text-2xs text-[var(--txt-muted)] uppercase tracking-wide">Тренд</span>
                  <Sparkline data={btcSparkData} />
                </div>
              )}
              <div className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-2xs">
                {ticker ? [
                  { l: 'Last', v: `$${parseFloat(ticker.last).toLocaleString()}`, c: 'text-[var(--txt)]' },
                  { l: 'Bid', v: `$${parseFloat(ticker.bid).toLocaleString()}`, c: 'text-[var(--profit)]' },
                  { l: 'Ask', v: `$${parseFloat(ticker.ask).toLocaleString()}`, c: 'text-[var(--loss)]' },
                  { l: '24h High', v: `$${parseFloat(ticker.high24h).toLocaleString()}`, c: 'text-[var(--profit)]' },
                  { l: '24h Low', v: `$${parseFloat(ticker.low24h).toLocaleString()}`, c: 'text-[var(--loss)]' },
                ].map(item => (
                  <div key={item.l} className="flex justify-between">
                    <span className="text-[var(--txt-muted)]">{item.l}</span>
                    <span className={`mono font-medium ${item.c}`}>{item.v}</span>
                  </div>
                )) : (
                  <span className="text-[var(--txt-muted)] col-span-2 text-center py-2">Нет данных</span>
                )}
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
