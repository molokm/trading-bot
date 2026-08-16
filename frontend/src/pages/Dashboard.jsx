import React, { useState, useEffect, useMemo, useRef } from 'react'
import {
  Wallet, TrendingUp, TrendingDown, Activity, XCircle, Loader2, Zap,
  ArrowUpRight, ArrowDownRight, BarChart3, Play, Square, ChevronDown, Filter, ScrollText,
  Clock, Bot, FlaskConical
} from 'lucide-react'
import { api } from '../services/api'
import { MetricCard, Tip, StatusBadge, Chip, PnlBar, EmptyState, Loader } from '../components/ui'
import { useTranslation } from '../hooks/useTranslation'
import { fmtTs } from '../utils/time'

const PAIRS = ['Все', 'BTC', 'ETH', 'BNB', 'XRP', 'SOL', 'DOGE', 'ADA', 'TRX', 'AVAX', 'LTC']

// Coins the bot actively trades — shown as live price cards on the dashboard
const PRICE_COINS = ['BTC', 'ETH', 'BNB', 'XRP', 'SOL', 'DOGE', 'ADA', 'TRX', 'AVAX', 'LTC']

/* ═══════ Animated Value — smooth colour transition ═══════ */
function AnimatedValue({ children, className = '' }) {
  return (
    <span className={`transition-all duration-500 ${className}`}>
      {children}
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
  const [tickers, setTickers] = useState({})
  const [momentumStatus, setMomentumStatus] = useState(null)
  const [momentumTrades, setMomentumTrades] = useState([])
  const [impulseStatus, setImpulseStatus] = useState(null)
  const [validationStatus, setValidationStatus] = useState(null)
  const [tradeLog, setTradeLog] = useState([])
  const [pnl, setPnl] = useState(null)
  const [closing, setClosing] = useState(null)
  const [loading, setLoading] = useState(true)

  // Filters
  const [filterPair, setFilterPair] = useState('Все')
  const [filterResult, setFilterResult] = useState('all') // all | win | loss
  const [filterReason, setFilterReason] = useState('all')

  // Persisted uptime — calculated from server-side started_at
  const [uptimeSec, setUptimeSec] = useState(0)
  const uptimeRef = useRef(null)

  const { t, locale } = useTranslation()

  const REASON_MAP = {
    tp: { label: 'TP', color: 'text-[var(--profit)]' },
    sl: { label: 'SL', color: 'text-[var(--loss)]' },
    trail: { label: t('reason.trail'), color: 'text-[var(--info)]' },
    breakeven: { label: 'BE', color: 'text-[var(--warn)]' },
    manual: { label: t('reason.manual'), color: 'text-[var(--txt-secondary)]' },
    roe_threshold: { label: 'ROE', color: 'text-accent-purple' },
    range_target: { label: t('reason.range_target'), color: 'text-[var(--info)]' },
  }
  const STAGE_MAP = {
    initial:    { label: t('stage.initial'), color: 'text-[var(--txt-muted)]' },
    sl1_trimmed:{ label: 'SL1',    color: 'text-[var(--loss)]' },
    breakeven:  { label: 'BE',     color: 'text-[var(--warn)]' },
    trailing:   { label: 'Trail',  color: 'text-[var(--info)]' },
  }

  useEffect(() => {
    const started = momentumStatus?.started_at
    if (!started) { setUptimeSec(0); return }
    const startedMs = new Date(started).getTime()
    if (isNaN(startedMs)) { setUptimeSec(0); return }
    const tick = () => setUptimeSec(Math.max(0, Math.floor((Date.now() - startedMs) / 1000)))
    tick()
    uptimeRef.current = setInterval(tick, 1000)
    return () => { if (uptimeRef.current) clearInterval(uptimeRef.current) }
  }, [momentumStatus?.started_at])

  useEffect(() => {
    loadData()
    const interval = setInterval(loadData, 10000)
    return () => clearInterval(interval)
  }, [connected])

  // Skip polling while the tab is hidden — frees the (throttled) server
  // instance and avoids piling up requests in the background.
  useEffect(() => {
    const onVis = () => { if (!document.hidden) loadData() }
    document.addEventListener('visibilitychange', onVis)
    return () => document.removeEventListener('visibilitychange', onVis)
  }, [connected])

  async function loadData() {
    if (!connected) { setLoading(false); return }
    if (document.hidden) return
    // Fast tier — renders immediately; the slow tier below fills in when ready.
    try {
      const [pf, pos, tk, momStatus, impStatus, valStatus, priceTickers] = await Promise.all([
        api.getPortfolio().catch(() => null),
        api.getPositions('SWAP').catch(() => null),
        api.getTicker('BTC-USDT-SWAP').catch(() => null),
        api.momentumStatus().catch(() => null),
        api.impulseStatus().catch(() => null),
        api.validationStatus().catch(() => null),
        api.getTickers(PRICE_COINS.map(c => `${c}-USDT-SWAP`)).catch(() => null),
      ])
      if (pf) setPortfolio(pf)
      if (pos) setPositions(pos.positions || [])
      if (tk) setTicker(tk)
      if (momStatus) setMomentumStatus(momStatus)
      if (impStatus) setImpulseStatus(impStatus)
      if (valStatus) setValidationStatus(valStatus)
      if (priceTickers?.tickers) {
        const byCoin = {}
        priceTickers.tickers.forEach(tp => {
          const id = (tp.instId || '').replace('-USDT-SWAP', '')
          if (id) byCoin[id] = tp
        })
        setTickers(byCoin)
      }
      setLoading(false)
    } catch { setLoading(false) }
    // Slow tier — expensive OKX-bills pipelines; served from the server-side
    // 30s cache, so updates arrive a little after the fast tier.
    try {
      const [momTrades, trades, pnlData] = await Promise.all([
        api.momentumTrades(30).catch(() => null),
        api.getPairedTrades(50).catch(() => null),
        api.getPnl().catch(() => null),
      ])
      if (momTrades) setMomentumTrades(momTrades.trades || [])
      if (trades) setTradeLog(trades.trades || [])
      if (pnlData) setPnl(pnlData)
    } catch {}
  }

  // Derived values (declared early — before any useMemo that depends on them)
  const change24hPct = (tk) => {
    if (!tk || !tk.last || !tk.open24h || parseFloat(tk.open24h) <= 0) return 0
    return ((parseFloat(tk.last) - parseFloat(tk.open24h)) / parseFloat(tk.open24h)) * 100
  }
  const btcChange = ticker ? change24hPct(ticker).toFixed(2) : '0.00'
  const totalEquity = portfolio ? portfolio.totalEqUsd || 0 : 0
  // Prefer sum of live OKX position rows (same source as the positions table).
  // /api/pnl.unrealized is a slower cached path and can disagree with the table.
  const unrealizedFromPositions = positions.reduce(
    (s, p) => s + (parseFloat(p.upl) || 0), 0
  )
  const unrealizedPnl = positions.length > 0
    ? unrealizedFromPositions
    : (pnl?.unrealized || 0)
  const pnlTotal = pnl?.total || 0
  const pnlDay = pnl?.['1d'] || 0
  const pnlWeek = pnl?.week || 0
  const pnlMonth = pnl?.['30d'] || 0

  // Per-strategy realized PnL breakdown (from /api/pnl per_bot)
  const botNameMap = {
    rotation_strategy: 'Momentum',
    momentum_strategy: 'Momentum',
    impulse_strategy: 'Impulse 1D',
    validation_strategy: 'MACD+Donchian Validation',
  }
  const pnlByBot = useMemo(() => {
    const per = pnl?.per_bot || {}
    return Object.entries(per)
      .map(([bid, val]) => ({ name: botNameMap[bid] || bid, val }))
      .sort((a, b) => Math.abs(b.val) - Math.abs(a.val))
  }, [pnl])

    // Raw trades from DB + bot log (used as source for activeTrades)
  const allTrades = useMemo(() => {
    const combined = [...tradeLog]
    const pairedKeys = new Set(tradeLog.map(t => `${t.inst_id}_${t.entry_time}_${t.exit_time}`))
    for (const mt of momentumTrades) {
      const key = `${mt.symbol}_${mt.time}_${mt.time}`
      if (pairedKeys.has(key)) continue
      // Full close trade with both prices (in-memory)
      if (mt.entry_price && mt.exit_price) {
        const isLongClose = (mt.pos_side === 'long' && mt.side === 'sell')
                           || (mt.pos_side === 'short' && mt.side === 'buy')
        if (!isLongClose && mt.pos_side) continue
        combined.push({
          entry_time: mt.time, exit_time: mt.time, inst_id: mt.symbol,
          side: mt.pos_side === 'short' ? 'sell' : 'buy',
          entry_px: mt.entry_price, exit_px: mt.exit_price,
          pnl: mt.pnl, reason: mt.reason || '', signal_id: mt.ord_id,
          bot: mt.bot,
        })
      // DB-restored close trade (pnl!=0 but may lack entry_price)
      } else if (mt.pnl != null && parseFloat(mt.pnl) !== 0 && !mt.entry) {
        combined.push({
          entry_time: mt.time, exit_time: mt.time, inst_id: mt.symbol,
          side: (mt.pos_side === 'short' || mt.side === 'sell') ? 'sell' : 'buy',
          entry_px: mt.entry || null, exit_px: mt.exit_price || null,
          pnl: mt.pnl, reason: mt.reason || 'closed', signal_id: mt.ord_id,
          bot: mt.bot,
        })
      // Entry / open trade
      } else if (mt.reason === 'open' || (mt.entry && !mt.exit_price)) {
        combined.push({
          entry_time: mt.time, exit_time: null, inst_id: mt.symbol,
          side: (mt.pos_side === 'short' || mt.side === 'sell') ? 'sell' : 'buy',
          entry_px: mt.entry, exit_px: null, pnl: null, reason: 'open', signal_id: mt.ord_id,
          bot: mt.bot,
        })
      }
    }
    combined.sort((a, b) => {
      const ta = a.exit_time || a.entry_time || ''
      const tb = b.exit_time || b.entry_time || ''
      return tb.localeCompare(ta)
    })
    return combined
  }, [tradeLog, momentumTrades])

  // Active trades — one row per position (open from live status, closed from trade log)
  const activeTrades = useMemo(() => {
    const rows = []
    const tp1Pct = momentumStatus?.config?.tp1_pct || 0.015

    // 1. Open positions — live data from bot status (updates in-place on TP1/SL1)
    const allOpen = [
      ...(momentumStatus?.open_positions || []).map(p => ({ ...p, bot: 'Momentum' })),
      ...(impulseStatus?.open_positions || []).map(p => ({ ...p, bot: 'Impulse 1D' })),
      ...(validationStatus?.open_positions || []).map(p => ({ ...p, bot: 'Validation' })),
    ]
    for (const p of allOpen) {
      const isLong = p.side !== 'short'
      rows.push({
        type: 'open',
        time: p.opened_at || '',
        symbol: p.symbol,
        inst_id: p.inst_id || p.symbol,
        side: isLong ? 'buy' : 'sell',
        pos_side: p.side,
        entry: p.entry_price ?? p.entry,
        stop: p.stop_price ?? p.stop,
        tp1: p.tp1,
        tp2: p.tp2,
        be: p.be_price ?? (isLong ? (p.entry_price ?? p.entry) * 0.999 : (p.entry_price ?? p.entry) * 1.001),
        mark: p.mark_px,
        size: p.size_original ?? p.size,
        size_remaining: p.size_remaining ?? p.size,
        stage: p.stage,
        pos_mode: p.pos_mode,
        breakeven: p.breakeven,
        partial_done: p.partial_done,
        unrealized_pnl: p.unrealized_pnl,
        pnl: null,
        reason: 'open',
        bot: p.bot,
      })
    }

    // 2. Closed trades — from combined trade log, skip 'open' and 'tp1' (partial closes)
    for (const t of allTrades) {
      const r = (t.reason || '').toLowerCase()
      if (r === 'open') continue   // covered by live positions above
      if (r === 'tp1') continue    // partial close — position row updates in-place
      rows.push({
        type: 'closed',
        time: t.exit_time || t.entry_time || '',
        symbol: t.inst_id?.replace('-USDT-SWAP', '') || '',
        inst_id: t.inst_id,
        side: t.side,
        entry: t.entry_px,
        exit: t.exit_px,
        pnl: parseFloat(t.pnl || 0),
        reason: r,
        stage: null,
        bot: t.bot,
      })
    }

    // Open first (sorted by time desc), then closed
    rows.sort((a, b) => {
      if (a.type === 'open' && b.type !== 'open') return -1
      if (a.type !== 'open' && b.type === 'open') return 1
      return (b.time || '').localeCompare(a.time || '')
    })
    return rows
  }, [momentumStatus?.open_positions, impulseStatus?.open_positions, validationStatus?.open_positions, allTrades])

  // Keep allTrades for summary stats (closed only)
  const closedTrades = useMemo(() =>
    allTrades.filter(t => {
      const r = (t.reason || '').toLowerCase()
      return r !== 'open' && r !== 'tp1'
    })
  , [allTrades])

  // Filtered active trades
  const filteredTrades = useMemo(() => {
    return activeTrades.filter(t => {
      if (filterPair !== 'Все') {
        const pair = (t.inst_id || t.symbol || '').toUpperCase()
        if (!pair.includes(filterPair)) return false
      }
      const pnlVal = parseFloat(t.pnl || 0)
      if (filterResult === 'win' && pnlVal < 0) return false
      if (filterResult === 'loss' && pnlVal >= 0) return false
      if (filterReason !== 'all') {
        if (t.type === 'open') {
          if (filterReason !== 'open') return false
        } else {
          if ((t.reason || '').toLowerCase() !== filterReason) return false
        }
      }
      return true
    })
  }, [activeTrades, filterPair, filterResult, filterReason])

  // Sparkline data for golden-zone MetricCards (stable random 10-point trends)
  const sparkData = useMemo(() =>
    Array.from({ length: 10 }, () =>
      Array.from({ length: 10 }, () => Math.random() * 100)
    )
  , [])

  // Summary stats for visible trades (closed only for PnL counts)
  const tradesSummary = useMemo(() => {
    const visible = (filteredTrades.length > 0 ? filteredTrades : activeTrades).slice(0, 5)
    const withPnl = visible.filter(t => t.pnl != null)
    const totalPnl = withPnl.reduce((s, t) => s + parseFloat(t.pnl || 0), 0)
    const wins = withPnl.filter(t => parseFloat(t.pnl || 0) >= 0).length
    const losses = withPnl.filter(t => parseFloat(t.pnl || 0) < 0).length
    return { totalPnl, wins, losses, count: visible.length }
  }, [filteredTrades, activeTrades])

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
    } catch (e) { alert(t('dash.error') + e.message) }
    finally { setClosing(null) }
  }

  const fmt = (v, d = 2) => v != null ? v.toFixed(d) : '---'
  const fmtUsd = (v) => v != null ? `$${Math.abs(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : '---'
  const fmtTime = (ts) => fmtTs(ts, locale)

  return (
    <div className="h-full flex flex-col p-4 gap-3 overflow-hidden">

      {!connected && (
        <div className="flex-shrink-0 flex items-center gap-2 px-3 py-2 rounded-lg border border-[var(--loss)]/30 bg-[var(--loss-dim)] text-2xs text-[var(--loss)]">
          <Activity size={14} />
          <span>{t('dash.offline_banner')}</span>
        </div>
      )}

      {/* ═══ GOLDEN ZONE — Key Metrics ═══ */}
      <div data-tour="metrics" className="flex-shrink-0 grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
        <MetricCard
          label={t('dash.balance')}
          value={<AnimatedValue>{totalEquity ? `$${totalEquity.toLocaleString()}` : '---'}</AnimatedValue>}
          mono
          tip={t('dash.balance_tip')}
          sparkData={sparkData[0]}
        />
        <MetricCard
          label={t('dash.unrealized')}
          value={
            <AnimatedValue className={unrealizedPnl >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}>
              {unrealizedPnl >= 0 ? `+$${fmt(unrealizedPnl)}` : `-$${fmt(Math.abs(unrealizedPnl))}`}
            </AnimatedValue>
          }
          changeType={unrealizedPnl >= 0 ? 'positive' : 'negative'}
          mono
          tip={t('dash.unrealized_tip')}
          sparkData={sparkData[1]}
        />
        <MetricCard
          label={t('dash.pnl_day')}
          value={
            <AnimatedValue className={pnlDay >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}>
              {pnlDay >= 0 ? `+$${fmt(pnlDay)}` : `-$${fmt(Math.abs(pnlDay))}`}
            </AnimatedValue>
          }
          changeType={pnlDay >= 0 ? 'positive' : 'negative'}
          mono
          tip={t('dash.pnl_day_tip')}
          sparkData={sparkData[2]}
        />
        <MetricCard
          label={t('dash.pnl_week')}
          value={
            <AnimatedValue className={pnlWeek >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}>
              {pnlWeek >= 0 ? `+$${fmt(pnlWeek)}` : `-$${fmt(Math.abs(pnlWeek))}`}
            </AnimatedValue>
          }
          changeType={pnlWeek >= 0 ? 'positive' : 'negative'}
          mono
          tip={t('dash.pnl_week_tip')}
          sparkData={sparkData[3]}
        />
        <MetricCard
          label={t('dash.total_pnl')}
          value={
            <div className="flex flex-col gap-0.5">
              <AnimatedValue className={pnlTotal >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}>
                {pnlTotal >= 0 ? `+$${fmt(pnlTotal)}` : `-$${fmt(Math.abs(pnlTotal))}`}
              </AnimatedValue>
              {pnlByBot.length > 0 && (
                <div className="text-[0.6rem] leading-tight text-[var(--txt-muted)]">
                  {pnlByBot.map((b, i) => (
                    <div key={b.name} className="flex items-center gap-1">
                      <span className="text-[var(--txt-secondary)]">{b.name}:</span>
                      <span className={`mono font-medium ${b.val >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>
                        {b.val >= 0 ? '+' : ''}{b.val.toFixed(2)}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          }
          changeType={pnlTotal >= 0 ? 'positive' : 'negative'}
          mono
          tip={t('dash.total_pnl_tip')}
          sparkData={sparkData[4]}
        />
        <MetricCard
          label={t('dash.positions_count')}
          value={<AnimatedValue>{positions.length}</AnimatedValue>}
          mono
          tip={t('dash.positions_count_tip')}
          sparkData={sparkData[5]}
        />
      </div>

      {/* ═══ Цены — компактная панель ═══ */}
      <div className="panel flex-shrink-0">
        <div className="panel-header">
          <span className="text-[var(--txt-muted)]">{t('dash.prices')}</span>
          <Tip text={t('dash.prices_tip')} />
        </div>
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 px-3 py-1.5">
          {PRICE_COINS.map((coin) => {
            const tk = coin === 'BTC' ? ticker : tickers[coin]
            const price = tk ? parseFloat(tk.last) : 0
            const change = tk ? change24hPct(tk) : 0
            const isUp = change >= 0
            const priceStr = price ? `$${price.toLocaleString(undefined, { maximumFractionDigits: price >= 1000 ? 0 : 2 })}` : '---'
            const changeStr = `${isUp ? '▲' : '▼'}${Math.abs(change).toFixed(2)}%`
            return (
              <span key={coin} className="flex items-center gap-1.5 py-1 coin-ticker">
                <span className="text-xs font-semibold text-[var(--txt-secondary)]">{coin}</span>
                <span className="text-xs mono text-[var(--txt)]">{priceStr}</span>
                <span className={`text-2xs mono ${isUp ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>{changeStr}</span>
              </span>
            )
          })}
        </div>
      </div>

      {/* ═══ MAIN GRID 65/35 ═══ */}
      <div className="flex-1 grid grid-cols-1 lg:grid-cols-[1fr_380px] gap-3 min-h-0 main-grid">

        {/* ═══ LEFT — Positions + Trades ═══ */}
        <div className="flex flex-col gap-3 min-h-0 overflow-hidden">

          {/* Open Positions */}
          <div className="panel flex-1 flex flex-col min-h-0">
            <div className="panel-header">
              <Zap size={13} className="text-[var(--profit)]" />
              {t('dash.open_positions')}
              <span className="ml-auto text-[var(--txt-muted)]">{positions.length}</span>
            </div>
            <div className="flex-1 overflow-auto">
              {loading ? (
                <div className="flex items-center justify-center py-12"><Loader /></div>
              ) : positions.length === 0 ? (
                <EmptyState icon={Zap} text={t('dash.no_positions')} sub={t('dash.positions_hint')} />
              ) : (
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>{t('dash.pair')}</th>
                      <th>Bot</th>
                      <th className="text-right">{t('dash.size')}</th>
                      <th className="text-right">{t('dash.entry')}</th>
                      <th className="text-right">{t('dash.mark')}</th>
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
                      const botName = p.bot || ''
                      const botBadge = botName === 'Momentum'
                        ? { label: 'MOM', cls: 'bg-blue-500/20 text-blue-400 border border-blue-500/30' }
                        : botName === 'Impulse'
                        ? { label: 'IMP', cls: 'bg-violet-500/20 text-violet-400 border border-violet-500/30' }
                        : botName
                        ? { label: String(botName).slice(0, 3).toUpperCase(), cls: 'bg-white/10 text-[var(--txt-secondary)] border border-white/10' }
                        : { label: '—', cls: 'text-[var(--txt-muted)]' }
                      const mgnRatio = parseFloat(p.mgnRatio || 0)
                      const riskCls = !mgnRatio ? ''
                        : mgnRatio < 2 ? 'text-[var(--loss)]'
                        : mgnRatio < 5 ? 'text-[var(--warn)]'
                        : 'text-[var(--txt-muted)]'
                      const side = (p.posSide || '').toLowerCase()
                      const lever = p.lever ? `${p.lever}x` : ''
                      return (
                        <tr key={i} style={{
                          background: upl >= 0
                            ? 'linear-gradient(90deg, rgba(0,255,136,0.06) 0%, transparent 50%)'
                            : 'linear-gradient(90deg, rgba(255,51,102,0.06) 0%, transparent 50%)',
                          boxShadow: `inset 2px 0 0 ${upl >= 0 ? 'rgba(0,255,136,0.4)' : 'rgba(255,51,102,0.4)'}`,
                        }}>
                          <td className="text-[var(--txt)] font-medium">
                            <div className="flex flex-col gap-0.5">
                              <span>{p.instId?.replace('-USDT-SWAP', '')}</span>
                              <span className="text-2xs text-[var(--txt-muted)] flex items-center gap-1.5">
                                {side && (
                                  <span className={side === 'long' ? 'text-[var(--profit)]' : side === 'short' ? 'text-[var(--loss)]' : ''}>
                                    {side.toUpperCase()}
                                  </span>
                                )}
                                {lever && <span>{lever}</span>}
                                {mgnRatio > 0 && (
                                  <span className={riskCls} title={t('dash.margin_ratio_tip')}>
                                    MR {mgnRatio >= 100 ? mgnRatio.toFixed(0) : mgnRatio.toFixed(1)}
                                  </span>
                                )}
                              </span>
                            </div>
                          </td>
                          <td><span className={`text-2xs font-bold px-1.5 py-0.5 rounded ${botBadge.cls}`}>{botBadge.label}</span></td>
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
                                {t('dash.close')}
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

          {/* Trades — one row per position */}
          <div className="panel flex-1 flex flex-col min-h-0">
            <div className="panel-header">
              <Activity size={13} className="text-accent-purple" />
              {t('dash.trades')}
              <div className="ml-auto flex gap-1">
                {['all', 'win', 'loss'].map(f => (
                  <Chip key={f} active={filterResult === f} onClick={() => setFilterResult(f)}>
                    {f === 'all' ? t('dash.all') : f === 'win' ? t('dash.profit') : t('dash.loss')}
                  </Chip>
                ))}
              </div>
            </div>
            {activeTrades.length > 0 && (
              <div className="flex items-center gap-4 px-4 py-2 text-2xs bg-[var(--bg)] border-b border-[var(--border)]">
                <span className="text-[var(--txt-muted)]">
                  {t('dash.shown')} <span className="mono text-[var(--txt)] font-medium">{tradesSummary.count}</span>
                </span>
                <span className="text-[var(--txt-muted)]">
                  {t('dash.total_pnl')} <span className={`mono font-bold ${pnlTotal >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>{pnlTotal >= 0 ? '+' : ''}{pnlTotal.toFixed(2)}</span>
                  {pnlByBot.length > 0 && (
                    <span className="ml-1 text-[0.6rem] text-[var(--txt-muted)]">
                      {pnlByBot.map((b, i) => (
                        <span key={b.name} className={i > 0 ? 'ml-1.5' : ''}>
                          <span className="text-[var(--txt-secondary)]">{b.name}:</span>{' '}
                          <span className={`mono font-medium ${b.val >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>
                            {b.val >= 0 ? '+' : ''}{b.val.toFixed(2)}
                          </span>
                        </span>
                      ))}
                    </span>
                  )}
                </span>
                <span className="text-[var(--txt-muted)]">
                  {t('dash.win_count')} <span className="mono text-[var(--profit)] font-medium">{tradesSummary.wins}</span>
                </span>
                <span className="text-[var(--txt-muted)]">
                  {t('dash.loss_count')} <span className="mono text-[var(--loss)] font-medium">{tradesSummary.losses}</span>
                </span>
              </div>
            )}
            <div className="flex-1 overflow-auto">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>{t('dash.time')}</th>
                    <th>{t('dash.pair')}</th>
                    <th>{t('dash.direction')}</th>
                    <th className="text-right">{t('dash.entry')}</th>
                    <th className="text-right">{t('dash.mark')}</th>
                    <th className="text-right">{t('dash.size')}</th>
                    <th className="text-right">TP1</th>
                    <th className="text-right">TP2</th>
                    <th className="text-right">SL</th>
                    <th className="text-right">PnL</th>
                  </tr>
                </thead>
                <tbody>
                  {(filteredTrades.length > 0 ? filteredTrades : activeTrades).slice(0, 5).map((tr, i) => {
                    const isOpen = tr.type === 'open'
                    const pnlVal = parseFloat(tr.pnl || 0)
                    const isLong = tr.side === 'buy'
                    // Level status: SL moved to BE once breakeven hits; TP1/TP2 hit when partial closes
                    const beHit = isOpen && !!tr.breakeven
                    const tp1Hit = isOpen && (tr.stage === 'partial' || tr.stage === 'trailing' || tr.stage === 'tp1_done' || tr.stage === 'tp2_done' || !!tr.partial_done)
                    const tp2Hit = isOpen && (tr.stage === 'trailing' || tr.stage === 'tp2_done')
                    const stageInfo = isOpen
                      ? (STAGE_MAP[tr.stage] || { label: tr.stage, color: 'text-[var(--txt-muted)]' })
                      : (REASON_MAP[tr.reason] || { label: tr.reason || '-', color: 'text-[var(--txt-muted)]' })
                    const botBadge = tr.bot === 'Momentum'
                      ? { label: 'MOM', cls: 'bg-blue-500/20 text-blue-400 border border-blue-500/30' }
                      : tr.bot === 'Impulse 1D'
                        ? { label: 'IMP', cls: 'bg-green-500/20 text-green-400 border border-green-500/30' }
                        : null
                    const mark = parseFloat(tr.mark || 0)
                    const upnl = parseFloat(tr.unrealized_pnl || 0)
                    return (
                      <tr key={`${tr.type}_${tr.inst_id || tr.symbol}_${i}`}
                        style={isOpen ? {
                          background: isLong
                            ? 'linear-gradient(90deg, rgba(0,255,136,0.04) 0%, transparent 40%)'
                            : 'linear-gradient(90deg, rgba(255,51,102,0.04) 0%, transparent 40%)',
                          boxShadow: `inset 2px 0 0 ${isLong ? 'rgba(0,255,136,0.3)' : 'rgba(255,51,102,0.3)'}`,
                        } : undefined}>
                        <td className="text-2xs mono text-[var(--txt-muted)]">{fmtTime(tr.time)}</td>
                        <td className="text-[var(--txt)] font-medium whitespace-nowrap">
                          {tr.symbol || tr.inst_id?.replace('-USDT-SWAP', '') || '-'}
                          {botBadge && (
                            <span className={`ml-1 text-2xs font-bold px-1 py-0.5 rounded ${botBadge.cls}`}>{botBadge.label}</span>
                          )}
                        </td>
                        <td>
                          <span className={`text-2xs font-bold px-1.5 py-0.5 rounded ${isLong ? 'bg-[var(--profit-dim)] text-[var(--profit)]' : 'bg-[var(--loss-dim)] text-[var(--loss)]'}`}>
                            {isLong ? 'L' : 'S'}
                          </span>
                        </td>
                        <td className="text-right mono text-2xs">{tr.entry ? `$${Number(tr.entry).toLocaleString(undefined, {maximumFractionDigits: 2})}` : '—'}</td>
                        <td className="text-right mono text-2xs">
                          {mark > 0 ? (
                            <span className="text-[var(--txt)]">
                              ${mark.toLocaleString(undefined, {maximumFractionDigits: 2})}
                            </span>
                          ) : '—'}
                        </td>
                        <td className="text-right mono text-2xs">
                          {isOpen ? (
                            <span title={`изначально ${tr.size}`}>
                              <span className="text-[var(--txt)]">{Number(tr.size_remaining || tr.size).toFixed(2)}</span>
                              {tr.size && tr.size !== tr.size_remaining ? (
                                <span className="text-[var(--txt-muted)]">/{Number(tr.size).toFixed(2)}</span>
                              ) : null}
                            </span>
                          ) : '—'}
                        </td>
                        <td className="text-right mono text-2xs">
                          {isOpen && tr.tp1 != null ? (
                            <span className={`${tp1Hit ? 'text-[var(--profit)]' : 'text-[var(--txt-muted)]'}`}>
                              ${Number(tr.tp1).toLocaleString(undefined, {maximumFractionDigits: 2})}{tp1Hit ? ' ✓' : ''}
                            </span>
                          ) : '—'}
                        </td>
                        <td className="text-right mono text-2xs">
                          {isOpen && tr.tp2 != null ? (
                            <span className={`${tp2Hit ? 'text-[var(--profit)]' : 'text-[var(--txt-muted)]'}`}>
                              ${Number(tr.tp2).toLocaleString(undefined, {maximumFractionDigits: 2})}{tp2Hit ? ' ✓' : ''}
                            </span>
                          ) : '—'}
                        </td>
                        <td className="text-right mono text-2xs">
                          {isOpen && tr.stop != null ? (
                            <span className={`${beHit ? 'text-[var(--warn)]' : 'text-[var(--loss)]'}`}>
                              ${Number(tr.stop).toLocaleString(undefined, {maximumFractionDigits: 2})}{beHit ? ' ✓' : ''}
                            </span>
                          ) : (tr.type === 'closed' && tr.exit) ? (
                            <span className="text-[var(--txt-muted)]">${parseFloat(tr.exit).toLocaleString()}</span>
                          ) : '—'}
                        </td>
                        <td className="text-right">
                          {!isOpen && tr.pnl != null ? (
                            <span className={`mono text-2xs font-bold ${pnlVal >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>
                              {pnlVal >= 0 ? '+' : ''}{pnlVal.toFixed(2)}
                            </span>
                          ) : isOpen ? (
                            <span className={`text-2xs font-medium ${stageInfo.color}`} title={t('dash.open_no_pnl')}>
                              {stageInfo.label}
                            </span>
                          ) : (
                            <span className="text-2xs text-[var(--txt-muted)]">—</span>
                          )}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
              {activeTrades.length === 0 && <EmptyState icon={ScrollText} text={t('dash.no_trades')} />}
            </div>
          </div>
        </div>

                {/* ═══ RIGHT — Filters + Bots ═══ */}
        <div className="flex flex-col gap-3 min-h-0 right-panel overflow-y-auto">

          {/* Filter Chips */}
          <div className="panel flex-shrink-0">
            <div className="panel-header">
              <Filter size={13} className="text-[var(--info)]" />
              {t('dash.filters')}
            </div>
            <div className="p-3 space-y-2">
              <div className="text-2xs text-[var(--txt-muted)] mb-1">{t('dash.instrument')}</div>
              <div className="flex flex-wrap gap-1">
                {PAIRS.map(p => (
                  <Chip key={p} active={filterPair === p} onClick={() => setFilterPair(p)}>{p === 'Все' ? t('dash.all') : p}</Chip>
                ))}
              </div>
              <div className="text-2xs text-[var(--txt-muted)] mb-1 mt-3">{t('dash.exit_reason')}</div>
              <div className="flex flex-wrap gap-1">
                {[{ k: 'all', l: t('dash.all') }, { k: 'tp', l: 'TP' }, { k: 'sl', l: 'SL' }, { k: 'trail', l: 'Trail' }, { k: 'breakeven', l: 'BE' }, { k: 'manual', l: 'Manual' }].map(r => (
                  <Chip key={r.k} active={filterReason === r.k} onClick={() => setFilterReason(r.k)}>{r.l}</Chip>
                ))}
              </div>
            </div>
          </div>

          {/* ─── Momentum Bot ─── */}
          <div className="panel flex-shrink-0">
            <div className="panel-header">
              <Bot size={13} className="text-[var(--info)]" />
              <span className="flex-1">{t('dash.momentum_bot')}</span>
              {momentumStatus?.version && (
                <span className="text-[0.62rem] font-semibold mono text-[var(--info)] uppercase tracking-wide mr-1">
                  {momentumStatus.version}
                </span>
              )}
              {momentumStatus?.running && <StatusBadge mode="live" label={t('dash.running')} />}
              {!momentumStatus?.running && momentumStatus && <StatusBadge mode="stopped" label={t('dash.stopped')} />}
            </div>
            <div className="p-3 space-y-2">
              {momentumStatus?.running ? (
                <>
                  <div className="grid grid-cols-4 gap-1.5">
                    <div className="p-1.5 rounded-md bg-[var(--bg)]">
                      <div className="text-2xs text-[var(--txt-muted)]">{t('dash.budget')}</div>
                      <div className="mono text-xs font-semibold text-[var(--txt)] mt-0.5">${(momentumStatus.config?.capital || 10000).toLocaleString()}</div>
                    </div>
                    <div className="p-1.5 rounded-md bg-[var(--bg)]">
                      <div className="text-2xs text-[var(--txt-muted)]">PnL</div>
                      <div className={`mono text-xs font-bold mt-0.5 ${momentumStatus.total_pnl >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>
                        ${momentumStatus.total_pnl >= 0 ? '+' : ''}{(momentumStatus.total_pnl || 0).toFixed(2)}
                      </div>
                    </div>
                    <div className="p-1.5 rounded-md bg-[var(--bg)]">
                      <div className="text-2xs text-[var(--txt-muted)]">{t('dash.positions')}</div>
                      <div className="mono text-xs font-semibold text-[var(--txt)] mt-0.5">{momentumStatus.open_positions?.length || 0}/{momentumStatus.config?.max_positions || 2}</div>
                    </div>
                    <div className="p-1.5 rounded-md bg-[var(--bg)]">
                      <div className="text-2xs text-[var(--txt-muted)]">{t('dash.leverage')}</div>
                      <div className="mono text-xs font-semibold text-[var(--info)] mt-0.5">×{momentumStatus.config?.max_leverage || 1}</div>
                    </div>
                  </div>
                  {momentumStatus.open_positions?.length > 0 && (
                    <div className="space-y-1">
                      {momentumStatus.open_positions.map((p, i) => {
                        const isLong = p.side !== 'short'
                        return (
                          <div key={i} className="flex items-center justify-between text-2xs p-1.5 rounded bg-[var(--bg)]">
                            <div className="flex items-center gap-1.5">
                              <span className={`px-1 py-0.5 rounded font-bold ${isLong ? 'bg-[var(--profit-dim)] text-[var(--profit)]' : 'bg-[var(--loss-dim)] text-[var(--loss)]'}`}>{isLong ? 'L' : 'S'}</span>
                              <span className="text-[var(--txt)] font-medium">{p.symbol}</span>
                            </div>
                            <span className="mono text-[var(--txt-muted)]" title={t('dash.open_no_pnl')}>
                              @{p.entry_price != null ? Number(p.entry_price).toFixed(2) : (p.entry != null ? Number(p.entry).toFixed(2) : '—')}
                            </span>
                          </div>
                        )
                      })}
                    </div>
                  )}
                  {!isGuest && (
                    <button className="btn btn-danger btn-sm w-full" onClick={async () => { try { await api.momentumStop(); loadData() } catch (e) { alert(e.message) } }}>
                      <Square size={12} /> {t('dash.stop_bot')}
                    </button>
                  )}
                </>
              ) : (
                <div className="text-center py-4">
                  <p className="text-xs text-[var(--txt-muted)] mb-2">{t('dash.bot_not_running')}</p>
                  {!isGuest && (
                    <button className="btn btn-primary btn-sm" onClick={async () => { try { await api.momentumStart({}); loadData() } catch (e) { alert(e.message) } }}>
                      <Play size={12} /> {t('dash.start')}
                    </button>
                  )}
                </div>
              )}
            </div>
          </div>

          {/* ─── Impulse 1D Bot ─── */}
          <div className="panel flex-shrink-0">
            <div className="panel-header">
              <Zap size={13} className="text-[var(--profit)]" />
              <span className="flex-1">{t('dash.impulse_bot')}</span>
              {impulseStatus?.version && (
                <span className="text-[0.62rem] font-semibold mono text-[var(--profit)] uppercase tracking-wide mr-1">
                  {impulseStatus.version}
                </span>
              )}
              {impulseStatus?.running && <StatusBadge mode="live" label={t('dash.running')} />}
              {!impulseStatus?.running && impulseStatus && <StatusBadge mode="stopped" label={t('dash.stopped')} />}
            </div>
            <div className="p-3 space-y-2">
              <div className="grid grid-cols-4 gap-1.5">
                <div className="p-1.5 rounded-md bg-[var(--bg)]">
                  <div className="text-2xs text-[var(--txt-muted)]">{t('dash.budget')}</div>
                  <div className="mono text-xs font-semibold text-[var(--txt)] mt-0.5">${(impulseStatus?.config?.capital || 10000).toLocaleString?.()}</div>
                </div>
                <div className="p-1.5 rounded-md bg-[var(--bg)]">
                  <div className="text-2xs text-[var(--txt-muted)]">PnL</div>
                  <div className={`mono text-xs font-bold mt-0.5 ${impulseStatus?.total_pnl >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>
                    ${impulseStatus?.total_pnl >= 0 ? '+' : ''}{(impulseStatus?.total_pnl || 0).toFixed(2)}
                  </div>
                </div>
                <div className="p-1.5 rounded-md bg-[var(--bg)]">
                  <div className="text-2xs text-[var(--txt-muted)]">{t('dash.positions')}</div>
                  <div className="mono text-xs font-semibold text-[var(--txt)] mt-0.5">{impulseStatus?.open_positions?.length || 0}/{impulseStatus?.config?.top_k || 4}</div>
                </div>
                <div className="p-1.5 rounded-md bg-[var(--bg)]">
                  <div className="text-2xs text-[var(--txt-muted)]">{t('dash.leverage')}</div>
                  <div className="mono text-xs font-semibold text-[var(--info)] mt-0.5">×{impulseStatus?.config?.max_leverage || 1}</div>
                </div>
              </div>
              {impulseStatus?.open_positions?.length > 0 && (
                <div className="space-y-1">
                  {impulseStatus.open_positions.map((p, i) => {
                    const isLong = p.side !== 'short'
                    return (
                      <div key={i} className="flex items-center justify-between text-2xs p-1.5 rounded bg-[var(--bg)]">
                        <div className="flex items-center gap-1.5">
                          <span className={`px-1 py-0.5 rounded font-bold ${isLong ? 'bg-[var(--profit-dim)] text-[var(--profit)]' : 'bg-[var(--loss-dim)] text-[var(--loss)]'}`}>{isLong ? 'L' : 'S'}</span>
                          <span className="text-[var(--txt)] font-medium">{p.symbol}</span>
                        </div>
                        <span className="mono text-[var(--txt-muted)]" title={t('dash.open_no_pnl')}>
                          @{p.entry_price != null ? Number(p.entry_price).toFixed(2) : (p.entry != null ? Number(p.entry).toFixed(2) : '—')}
                        </span>
                      </div>
                    )
                  })}
                </div>
              )}
              {!isGuest && (
                <button className="btn btn-danger btn-sm w-full" onClick={async () => { try { await api.impulseStop(); loadData() } catch (e) { alert(e.message) } }}>
                  <Square size={12} /> {t('dash.stop_bot')}
                </button>
              )}
            </div>
          </div>

          {/* ─── Validation Bot ─── */}
          <div className="panel flex-shrink-0">
            <div className="panel-header">
              <FlaskConical size={13} className="text-[var(--warn)]" />
              <span className="flex-1">{t('dash.validation_bot')}</span>
              {validationStatus?.version && (
                <span className="text-[0.62rem] font-semibold mono text-[var(--warn)] uppercase tracking-wide mr-1">
                  {validationStatus.version}
                </span>
              )}
              {validationStatus?.running && <StatusBadge mode="live" label={t('dash.running')} />}
              {!validationStatus?.running && validationStatus && <StatusBadge mode="stopped" label={t('dash.stopped')} />}
            </div>
            <div className="p-3 space-y-2">
              <div className="grid grid-cols-4 gap-1.5">
                <div className="p-1.5 rounded-md bg-[var(--bg)]">
                  <div className="text-2xs text-[var(--txt-muted)]">{t('dash.budget')}</div>
                  <div className="mono text-xs font-semibold text-[var(--txt)] mt-0.5">${(validationStatus?.config?.capital || 300).toLocaleString?.()}</div>
                </div>
                <div className="p-1.5 rounded-md bg-[var(--bg)]">
                  <div className="text-2xs text-[var(--txt-muted)]">PnL</div>
                  <div className={`mono text-xs font-bold mt-0.5 ${validationStatus?.total_pnl >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>
                    ${validationStatus?.total_pnl >= 0 ? '+' : ''}{(validationStatus?.total_pnl || 0).toFixed(2)}
                  </div>
                </div>
                <div className="p-1.5 rounded-md bg-[var(--bg)]">
                  <div className="text-2xs text-[var(--txt-muted)]">{t('dash.positions')}</div>
                  <div className="mono text-xs font-semibold text-[var(--txt)] mt-0.5">{validationStatus?.open_positions?.length || 0}/{validationStatus?.config?.top_k || 4}</div>
                </div>
                <div className="p-1.5 rounded-md bg-[var(--bg)]">
                  <div className="text-2xs text-[var(--txt-muted)]">{t('dash.leverage')}</div>
                  <div className="mono text-xs font-semibold text-[var(--info)] mt-0.5">×{validationStatus?.config?.max_leverage || 1}</div>
                </div>
              </div>
              {validationStatus?.open_positions?.length > 0 && (
                <div className="space-y-1">
                  {validationStatus.open_positions.map((p, i) => {
                    const isLong = p.side !== 'short'
                    return (
                      <div key={i} className="flex items-center justify-between text-2xs p-1.5 rounded bg-[var(--bg)]">
                        <div className="flex items-center gap-1.5">
                          <span className={`px-1 py-0.5 rounded font-bold ${isLong ? 'bg-[var(--profit-dim)] text-[var(--profit)]' : 'bg-[var(--loss-dim)] text-[var(--loss)]'}`}>{isLong ? 'L' : 'S'}</span>
                          <span className="text-[var(--txt)] font-medium">{p.symbol}</span>
                        </div>
                        <span className="mono text-[var(--txt-muted)]" title={t('dash.open_no_pnl')}>
                          @{p.entry_price != null ? Number(p.entry_price).toFixed(2) : (p.entry != null ? Number(p.entry).toFixed(2) : '—')}
                        </span>
                      </div>
                    )
                  })}
                </div>
              )}
              {!isGuest && (
                <>
                  {validationStatus?.running ? (
                    <button className="btn btn-danger btn-sm w-full" onClick={async () => { try { await api.validationStop(); loadData() } catch (e) { alert(e.message) } }}>
                      <Square size={12} /> {t('dash.stop_bot')}
                    </button>
                  ) : (
                    <button className="btn btn-primary btn-sm w-full" onClick={async () => { try { await api.validationStart({}); loadData() } catch (e) { alert(e.message) } }}>
                      <Play size={12} /> {t('dash.start')}
                    </button>
                  )}
                </>
              )}
            </div>
          </div>

          {/* ─── Market Data ─── */}
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
                  <span className="text-2xs text-[var(--txt-muted)] uppercase tracking-wide">{t('dash.trend')}</span>
                  <Sparkline data={btcSparkData} />
                </div>
              )}
              <div className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-2xs">
                {ticker ? [
                  { l: t('dash.last'), v: `$${parseFloat(ticker.last).toLocaleString()}`, c: 'text-[var(--txt)]' },
                  { l: t('dash.bid'), v: `$${parseFloat(ticker.bid).toLocaleString()}`, c: 'text-[var(--profit)]' },
                  { l: t('dash.ask'), v: `$${parseFloat(ticker.ask).toLocaleString()}`, c: 'text-[var(--loss)]' },
                  { l: t('dash.high_24h'), v: `$${parseFloat(ticker.high24h).toLocaleString()}`, c: 'text-[var(--profit)]' },
                  { l: t('dash.low_24h'), v: `$${parseFloat(ticker.low24h).toLocaleString()}`, c: 'text-[var(--loss)]' },
                ].map(item => (
                  <div key={item.l} className="flex justify-between">
                    <span className="text-[var(--txt-muted)]">{item.l}</span>
                    <span className={`mono font-medium ${item.c}`}>{item.v}</span>
                  </div>
                )) : (
                  <span className="text-[var(--txt-muted)] col-span-2 text-center py-2">{t('dash.no_data')}</span>
                )}
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}