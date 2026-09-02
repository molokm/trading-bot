import React, { useState, useEffect, useMemo, useRef } from 'react'
import {
  Wallet, TrendingUp, TrendingDown, Activity, XCircle, Loader2, Zap,
  ArrowUpRight, ArrowDownRight, BarChart3, Play, Square, ChevronDown, Filter, ScrollText,
  Clock, Bot, FlaskConical, AlertTriangle, RefreshCw, ShieldAlert
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
  const [aiStatus, setAiStatus] = useState(null)
  const [smartMoneyStatus, setSmartMoneyStatus] = useState(null)
  const [vwapRevStatus, setVwapRevStatus] = useState(null)
  const [aiBusy, setAiBusy] = useState(false)
  const [tradeLog, setTradeLog] = useState([])
  const [pnl, setPnl] = useState(null)
  const [closing, setClosing] = useState(null)
  const [loading, setLoading] = useState(true)
  const [dataFreshAt, setDataFreshAt] = useState(null)
  const [reconcileOpen, setReconcileOpen] = useState(false)
  const [reconcileLoading, setReconcileLoading] = useState(false)
  const [reconcileResult, setReconcileResult] = useState(null)
  const [reconcileError, setReconcileError] = useState('')


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
    const interval = setInterval(() => {
      if (typeof document !== 'undefined' && document.hidden) return
      loadData()
    }, 20000)
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
      const [
        pf,
        pos,
        tk,
        momStatus,
        impStatus,
        valStatus,
        aiSt,
        smartMoneySt,
        vwapRevSt,
        priceTickers,
      ] = await Promise.all([
        api.getPortfolio().catch(() => null),
        api.getPositions('SWAP').catch(() => null),
        api.getTicker('BTC-USDT-SWAP').catch(() => null),
        api.momentumStatus().catch(() => null),
        api.impulseStatus().catch(() => null),
        api.validationStatus().catch(() => null),
        api.aiStatus().catch(() => null),
        api.smartMoneyStatus().catch(() => null),
        api.vwapRevStatus().catch(() => null),
        api.getTickers(PRICE_COINS.map(c => `${c}-USDT-SWAP`)).catch(() => null),
      ])
      if (pf) setPortfolio(pf)
      if (pos) setPositions(pos.positions || [])
      if (tk) setTicker(tk)
      if (momStatus) setMomentumStatus(momStatus)
      if (impStatus) setImpulseStatus(impStatus)
      if (valStatus) setValidationStatus(valStatus)
      setAiStatus(aiSt)
      setSmartMoneyStatus(smartMoneySt)
      setVwapRevStatus(vwapRevSt)
      // Seed PnL early from AI status so cards are not stuck at 0 while /api/pnl loads
      if (aiSt && (aiSt.total_pnl != null || aiSt.lifetime_pnl != null)) {
        const aiP = Number(aiSt.lifetime_pnl ?? aiSt.total_pnl ?? 0)
        setPnl(prev => {
          if (prev && Number(prev.total) !== 0) return prev
          return {
            total: aiP,
            '1d': Number(prev?.['1d'] ?? 0),
            week: Number(prev?.week ?? 0),
            '7d': Number(prev?.['7d'] ?? 0),
            '30d': aiP,
            unrealized: Number(prev?.unrealized ?? 0),
            per_bot: { 'AI Discretionary 1H': aiP, ...(prev?.per_bot || {}) },
            per_bot_all: { 'AI Discretionary 1H': aiP, ...(prev?.per_bot_all || {}) },
            source: 'ai_status_seed',
          }
        })
      }

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
        api.getPairedTrades(200).catch(() => null),
        api.getPnlSummary().catch(() => api.getPnl()).catch(() => null),
      ])
      if (momTrades) setMomentumTrades(momTrades.trades || [])
      if (trades) setTradeLog(trades.trades || [])
      if (pnlData && !pnlData.detail && (pnlData.total != null || pnlData['1d'] != null || pnlData.per_bot)) {
        setPnl(pnlData)
      } else if (health?.sm_diag && (health.sm_diag.pnl_total != null || health.sm_diag.pnl_per_bot)) {
        const sd = health.sm_diag
        setPnl({
          total: Number(sd.pnl_total ?? 0),
          '1d': Number(sd.pnl_1d ?? 0),
          week: Number(sd.pnl_week ?? 0),
          '7d': Number(sd.pnl_week ?? 0),
          '30d': Number(sd.pnl_total ?? 0),
          unrealized: Number(sd.pnl_unrealized ?? 0),
          per_bot: sd.pnl_per_bot || {},
          per_bot_all: sd.pnl_per_bot || {},
          source: sd.pnl_source || 'health_fallback',
        })
      }
      setDataFreshAt(Date.now())
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
  const fundingPnl = Number(pnl?.funding ?? 0)
  // economic mixes account funding with strategy realized — show both explicitly
  const strategyRealized = Number(pnl?.strategy_realized ?? pnl?.total ?? 0)
  const economicPnl = Number(
    pnl?.economic_approx
    ?? (strategyRealized + unrealizedPnl + fundingPnl)
  )
  const pnlTz = pnl?.pnl_tz || pnl?.timezone || 'Europe/Moscow'
  // Active strategy labels (only running bots contribute to dashboard PnL)
  const activeBotNames = (() => {
    const names = []
    if (momentumStatus?.running) names.push('Momentum')
    if (impulseStatus?.running) names.push('Impulse 1D', 'Impulse')
    if (validationStatus?.running) names.push('MACD+Donchian Validation', 'Validation')
    if (aiStatus?.running) names.push('AI Discretionary 1H')
    if (smartMoneyStatus?.running) names.push('Умные деньги', 'Smart Money')
    if (vwapRevStatus?.running) names.push('VWAP Mean Reversion')
    return names
  })()
  const isActiveBotTrade = (t) => {
    if (!activeBotNames.length) return true // none running → show nothing filtered below
    const b = String(t.bot || t.bot_name || '').trim()
    if (!b) return false
    return activeBotNames.some(n => b === n || b.includes(n) || n.includes(b))
  }
  // Realized windows: prefer /api/pnl (server already filters active); fallback tradeLog
  const sumClosedSince = (msBack) => {
    const cutoff = Date.now() - msBack
    let s = 0
    for (const t of (tradeLog || [])) {
      const reason = String(t.reason || '').toLowerCase()
      if (reason === 'open' || reason === 'add') continue
      if (t.pnl == null || t.pnl === '') continue
      if (!isActiveBotTrade(t)) continue
      const ts = t.exit_time || t.time || t.timestamp || ''
      if (!ts) continue
      const ms = Date.parse(ts)
      if (!Number.isFinite(ms) || ms < cutoff) continue
      s += Number(t.pnl) || 0
    }
    return s
  }
  const pnlTotal = (() => {
    // Server total is already active-only
    if (pnl && pnl.total != null && (pnl.active_bots || []).length) return Number(pnl.total)
    if (pnl && pnl.total != null && Number(pnl.total) !== 0) return Number(pnl.total)
    const per = pnl?.per_bot || {}
    const fromPer = Object.values(per).reduce((s, v) => s + Number(v || 0), 0)
    if (fromPer !== 0) return fromPer
    if (!activeBotNames.length) return 0
    return sumClosedSince(365 * 86400000)
  })()
  const pnlDay = (() => {
    if (pnl && pnl['1d'] != null && (pnl.active_bots || []).length) return Number(pnl['1d'])
    if (pnl && Number(pnl['1d'] ?? 0) !== 0) return Number(pnl['1d'])
    if (!activeBotNames.length) return 0
    return sumClosedSince(86400000)
  })()
  const pnlWeek = (() => {
    const apiW = pnl?.week ?? pnl?.['7d']
    if (pnl && apiW != null && (pnl.active_bots || []).length) return Number(apiW)
    if (pnl && Number(apiW ?? 0) !== 0) return Number(apiW)
    if (!activeBotNames.length) return 0
    return sumClosedSince(7 * 86400000)
  })()
  const pnlMonth = (() => {
    if (pnl && pnl['30d'] != null && (pnl.active_bots || []).length) return Number(pnl['30d'])
    if (pnl && Number(pnl['30d'] ?? 0) !== 0) return Number(pnl['30d'])
    if (!activeBotNames.length) return 0
    return sumClosedSince(30 * 86400000)
  })()

  // Per-strategy realized PnL breakdown (from /api/pnl per_bot)
  const botNameMap = {
    rotation_strategy: 'Momentum',
    momentum_strategy: 'Momentum',
    Momentum: 'Momentum',
    impulse_strategy: 'Impulse 1D',
    'Impulse 1D': 'Impulse 1D',
    validation_strategy: 'MACD+Donchian Validation',
    'MACD+Donchian Validation': 'MACD+Donchian Validation',
    ai_strategy: 'AI Discretionary 1H',
    'AI Discretionary 1H': 'AI Discretionary 1H',
    orderbook_scalp: 'Order Book Scalp',
    smart_money: 'Умные деньги',
    'Умные деньги': 'Умные деньги',
  }
  const pnlByBot = useMemo(() => {
    const per = pnl?.per_bot || {}
    const merged = {}
    for (const [bid, val] of Object.entries(per)) {
      const name = botNameMap[bid] || bid
      if (name === 'Unassigned' || name === 'Прочее / без стратегии') continue
      merged[name] = (merged[name] || 0) + Number(val || 0)
    }
    return Object.entries(merged)
      .map(([name, val]) => ({ name, val }))
      .sort((a, b) => Math.abs(b.val) - Math.abs(a.val))
  }, [pnl])

  // Bot card realized PnL: prefer /api/pnl per_bot (same as Total PnL breakdown)
  const momentumCardPnl = useMemo(() => {
    const per = pnl?.per_bot || {}
    if (per.Momentum != null) return Number(per.Momentum)
    if (per.rotation_strategy != null) return Number(per.rotation_strategy)
    if (per.momentum_strategy != null) return Number(per.momentum_strategy)
    return Number(momentumStatus?.total_pnl ?? 0)
  }, [pnl, momentumStatus?.total_pnl])
  const impulseCardPnl = useMemo(() => {
    const per = pnl?.per_bot || {}
    if (per['Impulse 1D'] != null) return Number(per['Impulse 1D'])
    if (per.impulse_strategy != null) return Number(per.impulse_strategy)
    return Number(impulseStatus?.total_pnl ?? 0)
  }, [pnl, impulseStatus?.total_pnl])
  const validationCardPnl = useMemo(() => {
    const per = pnl?.per_bot || {}
    if (per['MACD+Donchian Validation'] != null) return Number(per['MACD+Donchian Validation'])
    if (per.validation_strategy != null) return Number(per.validation_strategy)
    return Number(validationStatus?.total_pnl ?? 0)
  }, [pnl, validationStatus?.total_pnl])

  const aiCardPnl = useMemo(() => {
    const per = pnl?.per_bot || {}
    if (per['AI Discretionary 1H'] != null) return Number(per['AI Discretionary 1H'])
    if (per.ai_strategy != null) return Number(per.ai_strategy)
    return Number(aiStatus?.lifetime_pnl ?? aiStatus?.total_pnl ?? 0)
  }, [pnl, aiStatus?.lifetime_pnl, aiStatus?.total_pnl])

    // Closed trades for the card: OKX-paired log only (no in-memory bot log merges).
  // Local momentumTrades previously injected phantom closes not on OKX / History.
  const allTrades = useMemo(() => {
    const combined = [...tradeLog]
    combined.sort((a, b) => {
      const ta = a.exit_time || a.entry_time || ''
      const tb = b.exit_time || b.entry_time || ''
      return tb.localeCompare(ta)
    })
    return combined
  }, [tradeLog])

  // Map instId|side → bot name for badge resolution on exchange positions
  const botMap = useMemo(() => {
    const m = {}
    const addPositions = (list, name) => {
      for (const p of (list || [])) {
        const inst = p.inst_id || p.instId || ''
        const sideKey = (p.side || p.pos_side || 'long').toLowerCase().includes('short') ? 'short' : 'long'
        m[`${inst}|${sideKey}`] = name
      }
    }
    addPositions(momentumStatus?.open_positions, 'Momentum')
    addPositions(impulseStatus?.open_positions, 'Impulse 1D')
    addPositions(validationStatus?.open_positions, 'Validation')
    addPositions(aiStatus?.open_positions, 'AI Discretionary 1H')
    addPositions(smartMoneyStatus?.open_positions, 'Умные деньги')
    addPositions(vwapRevStatus?.open_positions, 'VWAP Mean Reversion')
    return m
  }, [momentumStatus?.open_positions, impulseStatus?.open_positions, validationStatus?.open_positions, aiStatus?.open_positions, smartMoneyStatus?.open_positions, vwapRevStatus?.open_positions])

  const isOwnedBot = (bn) => {
    if (!bn) return false
    const n = String(bn).toLowerCase()
    return n.includes('momentum') || n.includes('impulse') || n.includes('validation')
      || n.includes('macd') || n.includes('ai') || n.includes('умн') || n.includes('smart')
      || n.includes('vwap') || n.includes('scalp')
  }

  // Exchange positions with no strategy owner (manual / lost bot state)
  const orphanPositions = useMemo(() => {
    const managedKeys = new Set(Object.keys(botMap || {}))
    const addStatus = (arr) => {
      for (const op of (arr || [])) {
        const inst = op.inst_id || op.instId || (op.coin ? `${op.coin}-USDT-SWAP` : '')
        const side = (op.side || op.posSide || 'long').toLowerCase() === 'short' ? 'short' : 'long'
        if (inst) managedKeys.add(`${inst}|${side}`)
      }
    }
    addStatus(momentumStatus?.open_positions)
    addStatus(impulseStatus?.open_positions)
    addStatus(validationStatus?.open_positions)
    addStatus(aiStatus?.open_positions)
    addStatus(smartMoneyStatus?.open_positions)
    return (positions || []).filter((p) => {
      const posSz = Math.abs(parseFloat(p.pos || p.size || 0))
      if (!posSz) return false
      const posSideKey = (p.posSide || 'long').toLowerCase() === 'short' ? 'short' : 'long'
      const key = `${p.instId || ''}|${posSideKey}`
      const bn = p.bot || botMap[key] || ''
      if (isOwnedBot(bn)) return false
      if (managedKeys.has(key)) return false
      return true
    })
  }, [positions, botMap, momentumStatus?.open_positions, impulseStatus?.open_positions, validationStatus?.open_positions, aiStatus?.open_positions, smartMoneyStatus?.open_positions])

  // Active trades — open from bots + OKX positions; closed from paired log only
  const activeTrades = useMemo(() => {
    const rows = []
    const openKeys = new Set() // inst|side to avoid double rows

    const pushOpen = (p, botHint) => {
      const inst = p.inst_id || p.instId || p.symbol || ''
      const coin = (p.coin || inst.replace('-USDT-SWAP', '') || '').replace('-USDT-SWAP', '')
      const sideRaw = (p.side || p.posSide || p.pos_side || 'long').toLowerCase()
      const isLong = sideRaw !== 'short' && sideRaw !== 'sell'
      const sideKey = isLong ? 'long' : 'short'
      const key = `${inst}|${sideKey}`
      if (openKeys.has(key)) return
      openKeys.add(key)
      const entry = parseFloat(p.entry_price ?? p.entry ?? p.avgPx ?? 0) || 0
      const mark = parseFloat(p.mark_px ?? p.markPx ?? p.last ?? 0) || 0
      const size = parseFloat(p.size_original ?? p.size ?? p.pos ?? 0) || 0
      const sizeRem = parseFloat(p.size_remaining ?? p.size ?? p.pos ?? size) || size
      rows.push({
        type: 'open',
        time: p.opened_at || p.time || p.cTime || '',
        symbol: coin || p.symbol,
        inst_id: inst,
        side: isLong ? 'buy' : 'sell',
        pos_side: sideKey,
        entry: entry || null,
        stop: p.stop_price ?? p.stop ?? null,
        tp1: p.tp1 ?? null,
        tp2: p.tp2 ?? null,
        be: p.be_price ?? (entry ? (isLong ? entry * 0.999 : entry * 1.001) : null),
        mark: mark || null,
        size: size,
        size_remaining: sizeRem,
        stage: p.stage || (p.breakeven ? 'trailing' : (p.partial_done ? 'partial' : 'initial')),
        pos_mode: p.pos_mode,
        breakeven: !!p.breakeven,
        partial_done: !!p.partial_done,
        unrealized_pnl: p.unrealized_pnl != null ? p.unrealized_pnl : (parseFloat(p.upl) || null),
        pnl: null,
        reason: 'open',
        bot: botHint || p.bot || '',
      })
    }

    // 1a. Bot-owned opens (have stops/stage)
    for (const p of (momentumStatus?.open_positions || [])) pushOpen(p, 'Momentum')
    for (const p of (impulseStatus?.open_positions || [])) pushOpen(p, 'Impulse 1D')
    for (const p of (validationStatus?.open_positions || [])) pushOpen(p, 'Validation')
    for (const p of (aiStatus?.open_positions || [])) pushOpen(p, 'AI Discretionary 1H')
    // Smart Money opens/trades live only on /smart-money — not on main dashboard

    // 1b. Exchange positions not yet in bot memory (prevents missing open row)
    for (const p of (positions || [])) {
      const posSz = Math.abs(parseFloat(p.pos || p.size || 0))
      if (!posSz) continue
      const posSide = (p.posSide || p.side || 'net').toLowerCase() === 'short' ? 'short' : 'long'
      const posKey = `${p.instId || p.inst_id || ''}|${posSide}`
      let hint = p.bot || botMap[posKey] || ''
      if (!hint) {
        const coin = (p.instId || '').replace('-USDT-SWAP', '')
        for (const op of (aiStatus?.open_positions || [])) {
          if ((op.coin || '').toUpperCase() === coin.toUpperCase()) {
            hint = 'AI Discretionary 1H'
            break
          }
        }
      }
      if (hint === 'Smart Money') continue
      if (!hint) continue
      pushOpen({
        ...p,
        inst_id: p.instId || p.inst_id,
        side: posSide,
        entry_price: parseFloat(p.avgPx || p.avg_px || 0),
        mark_px: parseFloat(p.markPx || p.last || 0),
        size: posSz,
        size_remaining: posSz,
        upl: p.upl,
        bot: hint,
      }, hint)
    }

    // 2. Closed — paired log only; skip opens still on exchange and partials
    for (const tr of allTrades) {
      if (tr.bot === 'Smart Money') continue
      const r = (tr.reason || '').toLowerCase()
      if (r === 'open' || r === 'add') continue
      if (r === 'tp1' || r === 'partial_tp' || r === 'partial_tp2') continue
      const inst = tr.inst_id || tr.symbol || ''
      // Hide phantom "closed" while the SAME instrument+side is still open on
      // OKX/bots (a false close of the still-open position). Side-aware so real
      // historical closes of the other direction are still shown.
      if (inst) {
        const sideKey = (tr.side || '').toLowerCase() === 'sell' ? 'short' : 'long'
        if (openKeys.has(`${inst}|${sideKey}`)) continue
      }
      rows.push({
        type: 'closed',
        time: tr.exit_time || tr.entry_time || tr.time || '',
        symbol: (inst || '').replace('-USDT-SWAP', ''),
        inst_id: inst,
        side: tr.side,
        entry: tr.entry_px ?? tr.entry,
        exit: tr.exit_px ?? tr.exit_price,
        pnl: parseFloat(tr.pnl || 0),
        reason: r,
        stage: null,
        bot: tr.bot,
      })
    }

    rows.sort((a, b) => {
      if (a.type === 'open' && b.type !== 'open') return -1
      if (a.type !== 'open' && b.type === 'open') return 1
      return (b.time || '').localeCompare(a.time || '')
    })
    return rows
  }, [momentumStatus?.open_positions, impulseStatus?.open_positions, validationStatus?.open_positions, aiStatus?.open_positions, smartMoneyStatus?.open_positions, positions, allTrades, botMap])

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

  const handleReconcile = async () => {
    setReconcileOpen(true)
    setReconcileLoading(true)
    setReconcileError('')
    setReconcileResult(null)
    try {
      const r = await api.pnlReconcile()
      setReconcileResult(r)
    } catch (e) {
      setReconcileError(e.message || String(e))
    } finally {
      setReconcileLoading(false)
    }
  }

  // UX badges (Phase 5)
  const dataAgeSec = dataFreshAt ? Math.floor((Date.now() - dataFreshAt) / 1000) : null
  const dataStale = dataAgeSec != null && dataAgeSec > 90
  const llmErr = String(aiStatus?.last_llm_error || aiStatus?.llm_error || aiStatus?.last_decision?.error || '')
  const llmRateLimited = /429|rate.?limit|TPD|tokens per day/i.test(llmErr)
  const llmSoftError = Boolean(llmErr) && !llmRateLimited
  const claimMissing = (orphanPositions || []).length > 0
  const pnlSource = pnl?.source || ''
  const fundingNote = Number(pnl?.funding || 0)

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

      {/* ═══ Status strip (Phase 5 UX) ═══ */}
      <div className="flex-shrink-0 flex flex-wrap items-center gap-2 px-1">
        <span
          className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-2xs border ${
            !connected
              ? 'border-[var(--loss)]/40 bg-[var(--loss-dim)] text-[var(--loss)]'
              : dataStale
                ? 'border-[var(--warn)]/40 bg-[var(--warn-dim)] text-[var(--warn)]'
                : 'border-[var(--border)] bg-[var(--surface)] text-[var(--txt-muted)]'
          }`}
          title={dataFreshAt ? `Обновлено ${new Date(dataFreshAt).toLocaleTimeString()}` : 'Нет данных'}
        >
          <Clock size={11} />
          {!connected ? 'Нет связи' : dataStale ? `Данные устарели · ${dataAgeSec}с` : dataAgeSec != null ? `Свежие · ${dataAgeSec}с` : 'Загрузка…'}
        </span>

        {llmRateLimited && (
          <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-2xs border border-[var(--warn)]/40 bg-[var(--warn-dim)] text-[var(--warn)]" title={llmErr.slice(0, 200)}>
            <AlertTriangle size={11} />
            LLM: лимит запросов
          </span>
        )}
        {llmSoftError && !llmRateLimited && (
          <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-2xs border border-[var(--warn)]/30 bg-[var(--warn-dim)] text-[var(--txt-secondary)]" title={llmErr.slice(0, 200)}>
            <AlertTriangle size={11} />
            LLM: сбой
          </span>
        )}

        {claimMissing && (
          <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-2xs border border-[var(--loss)]/40 bg-[var(--loss-dim)] text-[var(--loss)]" title="Позиции на бирже без claim стратегии">
            <ShieldAlert size={11} />
            Без стратегии: {orphanPositions.length}
          </span>
        )}

        {pnlSource && (
          <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-2xs border border-[var(--border)] text-[var(--txt-muted)]" title="Источник расчёта PnL">
            PnL: {String(pnlSource).slice(0, 24)}
            {pnl?.pnl_tz ? ` · ${pnl.pnl_tz}` : ''}
          </span>
        )}

        {fundingNote !== 0 && (
          <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-2xs border border-[var(--border)] text-[var(--txt-muted)]" title="Funding — уровень аккаунта, не фильтр стратегий">
            Funding (acc): {fundingNote >= 0 ? '+' : ''}{fmt(fundingNote)}
          </span>
        )}

        {!isGuest && (
          <button
            type="button"
            onClick={handleReconcile}
            className="ml-auto inline-flex items-center gap-1 px-2.5 py-1 rounded-md text-2xs font-semibold border border-[var(--border)] bg-[var(--surface)] text-[var(--txt-secondary)] hover:border-[var(--info)] hover:text-[var(--info)] active:opacity-80"
            title="Сверить strategy PnL с bills OKX"
          >
            <RefreshCw size={12} className={reconcileLoading ? 'animate-spin' : ''} />
            Сверка OKX
          </button>
        )}
      </div>

      {/* Reconcile panel */}
      {reconcileOpen && (
        <div className="flex-shrink-0 rounded-xl border border-[var(--border)] bg-[var(--surface)] p-3 text-2xs space-y-2">
          <div className="flex items-center justify-between">
            <span className="font-semibold text-[var(--txt)]">Сверка PnL с OKX</span>
            <button type="button" className="text-[var(--txt-muted)] hover:text-[var(--txt)]" onClick={() => setReconcileOpen(false)}>Закрыть</button>
          </div>
          {reconcileLoading && <div className="text-[var(--txt-muted)]">Запрос bills / positions…</div>}
          {reconcileError && <div className="text-[var(--loss)]">{reconcileError}</div>}
          {reconcileResult && !reconcileLoading && (() => {
            const d = reconcileResult.dashboard || {}
            const strategy = Number(d.realized_tagged ?? reconcileResult.dashboard_total ?? 0)
            const okxTrade = Number(reconcileResult.okx_trade_pnl ?? 0)
            const tagged = Number(reconcileResult.okx_tagged_pnl ?? 0)
            const untagged = Number(reconcileResult.okx_untagged_pnl ?? 0)
            const gap = Number(
              reconcileResult.gap_tagged
              ?? reconcileResult.gap
              ?? (tagged - strategy)
            )
            const fund = Number(d.funding ?? reconcileResult.funding ?? 0)
            const uplD = Number(d.unrealized ?? 0)
            const uplO = Number(reconcileResult.okx_upl ?? reconcileResult.unrealized_okx ?? 0)
            return (
            <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
              <div className="p-2 rounded-lg bg-[var(--bg)]">
                <div className="text-[var(--txt-muted)]">Strategy realized</div>
                <div className="mono font-semibold text-[var(--txt)]">{fmt(strategy)}</div>
              </div>
              <div className="p-2 rounded-lg bg-[var(--bg)]">
                <div className="text-[var(--txt-muted)]">OKX tagged bills</div>
                <div className="mono font-semibold text-[var(--txt)]">{fmt(tagged)}</div>
              </div>
              <div className="p-2 rounded-lg bg-[var(--bg)]">
                <div className="text-[var(--txt-muted)]">Untagged OKX</div>
                <div className="mono font-semibold text-[var(--txt)]">{fmt(untagged)}</div>
              </div>
              <div className="p-2 rounded-lg bg-[var(--bg)]">
                <div className="text-[var(--txt-muted)]">Gap (tagged − strategy)</div>
                <div className={`mono font-semibold ${gap >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>
                  {fmt(gap)}
                </div>
              </div>
              <div className="p-2 rounded-lg bg-[var(--bg)]">
                <div className="text-[var(--txt-muted)]">UPL dash / OKX</div>
                <div className="mono text-[var(--txt)]">{fmt(uplD)} / {fmt(uplO)}</div>
              </div>
              <div className="p-2 rounded-lg bg-[var(--bg)]">
                <div className="text-[var(--txt-muted)]">Funding (account)</div>
                <div className="mono text-[var(--txt)]">{fmt(fund)}</div>
              </div>
              <div className="p-2 rounded-lg bg-[var(--bg)] col-span-2">
                <div className="text-[var(--txt-muted)]">Статус</div>
                <div className={`font-semibold ${reconcileResult.ok ? 'text-[var(--profit)]' : 'text-[var(--warn)]'}`}>
                  {reconcileResult.ok ? 'Согласовано' : 'Есть расхождения — проверьте untagged / claims'}
                </div>
              </div>
            </div>
            )
          })()}
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
          tip={`${t('dash.pnl_day_tip')} (${pnlTz}, только активные боты)`}
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
              {(fundingPnl !== 0 || Math.abs(economicPnl - pnlTotal) > 0.01) && (
                <div className="text-[0.6rem] leading-tight text-[var(--txt-muted)] mono">
                  <span>funding {fundingPnl >= 0 ? '+' : ''}{fmt(fundingPnl)}</span>
                  <span className="mx-1">·</span>
                  <span title="strategy realized + upl + account funding">eco {economicPnl >= 0 ? '+' : ''}{fmt(economicPnl)}</span>
                  {fundingPnl !== 0 && (
                    <span className="text-[var(--txt-muted)]" title="Funding (account)">fund {fundingPnl >= 0 ? '+' : ''}{fmt(fundingPnl)}</span>
                  )}
                </div>
              )}
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
                  {orphanPositions.length > 0 && (
                    <div className="mb-2 px-2 py-1.5 rounded-md bg-amber-500/10 border border-amber-500/30 text-2xs text-amber-400">
                      {orphanPositions.length} {t('dash.orphan_positions')}{' '}
                      <span className="mono">
                        {orphanPositions.map(op =>
                          `${(op.instId || '').replace('-USDT-SWAP', '')} ${(op.posSide || '').toUpperCase()}`
                        ).join(' · ')}
                      </span>
                    </div>
                  )}
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
                    {positions.filter((p) => {
                      const posSideKey = (p.posSide || 'long').toLowerCase()
                      const bn = p.bot || botMap[`${p.instId || ''}|${posSideKey}`] || ''
                      if (!bn || bn === 'Smart Money') return false
                      return true
                    }).map((p, i) => {
                      const upl = parseFloat(p.upl || 0)
                      const roe = parseFloat(p.uplRatio || 0) * 100
                      const posId = `${p.instId}_${p.posSide}`
                      const posSideKey = (p.posSide || 'long').toLowerCase()
                      const botName = p.bot || botMap[`${p.instId || ''}|${posSideKey}`] || ''
                      const botBadge = botName === 'Momentum'
                        ? { label: 'MOM', cls: 'bg-blue-500/20 text-blue-400 border border-blue-500/30' }
                        : (botName === 'Impulse' || botName === 'Impulse 1D')
                        ? { label: 'IMP', cls: 'bg-violet-500/20 text-violet-400 border border-violet-500/30' }
                        : botName === 'Validation' || botName === 'MACD+Donchian Validation'
                        ? { label: 'MAC', cls: 'bg-purple-500/20 text-purple-400 border border-purple-500/30' }
                        : botName === 'AI Discretionary 1H'
                        ? { label: 'AI', cls: 'bg-orange-500/20 text-orange-400 border border-orange-500/30' }
                        : botName === 'Smart Money'
                        ? { label: 'OBI', cls: 'bg-cyan-500/20 text-cyan-400 border border-cyan-500/30' }
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
                      : (tr.bot === 'Impulse 1D' || tr.bot === 'Impulse')
                        ? { label: 'IMP', cls: 'bg-green-500/20 text-green-400 border border-green-500/30' }
                        : tr.bot === 'MACD+Donchian Validation' || tr.bot === 'Validation'
                          ? { label: 'MAC', cls: 'bg-purple-500/20 text-purple-400 border border-purple-500/30' }
                          : tr.bot === 'AI Discretionary 1H'
                            ? { label: 'AI', cls: 'bg-orange-500/20 text-orange-400 border border-orange-500/30' }
                            : tr.bot === 'Smart Money'
                              ? { label: 'OBI', cls: 'bg-cyan-500/20 text-cyan-400 border border-cyan-500/30' }
                              : tr.bot
                                ? { label: String(tr.bot).slice(0, 3).toUpperCase(), cls: 'bg-white/10 text-[var(--txt-secondary)]' }
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

          {/* ─── Momentum Bot (only when running) ─── */}
          {!!momentumStatus?.running && (
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
                      <div className={`mono text-xs font-bold mt-0.5 ${momentumCardPnl >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>
                        ${momentumCardPnl >= 0 ? '+' : ''}{momentumCardPnl.toFixed(2)}
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
          )}

          {/* ─── Impulse 1D Bot (only when running) ─── */}
          {!!impulseStatus?.running && (
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
                  <div className={`mono text-xs font-bold mt-0.5 ${impulseCardPnl >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>
                    ${impulseCardPnl >= 0 ? '+' : ''}{impulseCardPnl.toFixed(2)}
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
          )}

          {/* ─── Validation Bot (only when running) ─── */}
          {!!validationStatus?.running && (
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
                  <div className={`mono text-xs font-bold mt-0.5 ${validationCardPnl >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>
                    ${validationCardPnl >= 0 ? '+' : ''}{validationCardPnl.toFixed(2)}
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
          )}

          {/* ─── AI Discretionary (only when running) ─── */}
          {!!aiStatus?.running && (
          <div className="panel flex-shrink-0">
            <div className="panel-header">
              <Bot size={13} className="text-[var(--accent)]" />
              AI Discretionary 1H
              {aiStatus?.version && (
                <span className="ml-1 text-2xs text-[var(--txt-muted)] mono">{aiStatus.version}</span>
              )}
              {aiStatus?.running && <StatusBadge mode="live" label={t('dash.running')} />}
              {!aiStatus?.running && aiStatus && <StatusBadge mode="stopped" label={t('dash.stopped')} />}
            </div>
            {/* Health indicator */}
            {aiStatus?.health && (() => {
              const h = aiStatus.health
              const poll = (aiStatus?.config?.poll_interval_sec || 120) * 3
              const lastTs = h.last_activity ? Date.parse(h.last_activity) : 0
              const stale = lastTs ? (Date.now() - lastTs) > poll * 1000 : false
              const hasErrors = h.consecutive_fails > 0 || h.last_tick_error
              const rateLimited = h.llm_rate_limited && h.llm_rate_limit_until > Date.now() / 1000
              let dotColor = 'var(--profit)'
              let statusText = `♥ ${h.tick_count} ticks`
              if (rateLimited) { dotColor = 'var(--loss)'; statusText = `⏳ rate limit ${Math.max(0, Math.ceil(h.llm_rate_limit_until - Date.now() / 1000))}s` }
              else if (h.last_tick_error) { dotColor = 'var(--loss)'; statusText = `✕ ${h.last_tick_error.slice(0, 40)}` }
              else if (h.last_llm_error) { dotColor = 'var(--warn)'; statusText = `⚠ LLM: ${h.last_llm_error.slice(0, 40)}` }
              else if (stale) { dotColor = 'var(--warn)'; statusText = `⏰ stale ${Math.floor((Date.now() - lastTs) / 60000)}м` }
              else if (h.last_provider_used && h.last_provider_used !== aiStatus?.provider) { dotColor = 'var(--warn)'; statusText = `↻ fallback ${h.last_provider_used}` }
              const ago = lastTs ? Math.floor((Date.now() - lastTs) / 60000) : null
              return (
                <div className="px-3 pb-1 flex items-center gap-1.5 text-2xs">
                  <span className="inline-block w-1.5 h-1.5 rounded-full flex-shrink-0 animate-pulse" style={{ background: dotColor }} />
                  <span className="text-[var(--txt-muted)] truncate">{statusText}</span>
                  {ago != null && <span className="text-[var(--txt-muted)] ml-auto whitespace-nowrap">{ago < 1 ? '<1м' : `${ago}м`} ago</span>}
                </div>
              )
            })()}
            <div className="p-3 space-y-2">
              <div className="grid grid-cols-2 gap-1.5 text-2xs">
                <div className="p-1.5 rounded-md bg-[var(--bg)]">
                  <div className="text-[var(--txt-muted)]">LLM</div>
                  <div className="mono font-semibold text-[var(--txt)] mt-0.5">
                    {aiStatus?.provider || '—'}{aiStatus?.groq_key_configured || aiStatus?.llm?.groq_key_configured ? ' ✓' : ''}
                  </div>
                </div>
                <div className="p-1.5 rounded-md bg-[var(--bg)]">
                  <div className="text-[var(--txt-muted)]">Execute</div>
                  <div className={`mono font-semibold mt-0.5 ${(aiStatus?.execute || aiStatus?.llm?.execute) ? 'text-[var(--loss)]' : 'text-[var(--txt-muted)]'}`}>
                    {(aiStatus?.execute || aiStatus?.llm?.execute) ? 'ON' : 'OFF (signals)'}
                  </div>
                </div>
                <div className="p-1.5 rounded-md bg-[var(--bg)]">
                  <div className="text-[var(--txt-muted)]">Model</div>
                  <div className="mono text-[var(--txt)] mt-0.5 truncate" title={aiStatus?.model || aiStatus?.llm?.model || ''}>
                    {(aiStatus?.model || aiStatus?.llm?.model || '—').toString().slice(0, 18)}
                  </div>
                </div>
                <div className="p-1.5 rounded-md bg-[var(--bg)]">
                  <div className="text-[var(--txt-muted)]">{t('dash.positions')}</div>
                  <div className="mono font-semibold text-[var(--txt)] mt-0.5">
                    {aiStatus?.open_positions?.length || 0}/{aiStatus?.config?.max_positions || 1}
                  </div>
                </div>
                <div className="p-1.5 rounded-md bg-[var(--bg)] col-span-2">
                  <div className="text-[var(--txt-muted)]">{t('dash.total_pnl')}</div>
                  <div className={`mono font-semibold mt-0.5 ${aiCardPnl >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>
                    {aiCardPnl >= 0 ? '+' : ''}{aiCardPnl.toFixed(2)}
                    {aiStatus?.total_pnl_source === 'okx_history' && (
                      <span className="text-2xs text-[var(--txt-muted)] font-normal ml-1">hist</span>
                    )}
                  </div>
                </div>
              </div>
              {aiStatus?.last_decision && (
                <div className="p-1.5 rounded-md bg-[var(--bg)] text-2xs">
                  <div className="text-[var(--txt-muted)] mb-0.5">Статус AI{aiStatus.symbols_scanned?.length ? ` · scan ${aiStatus.symbols_scanned.join("/")}` : (aiStatus.last_decision?.symbols_scanned?.length ? ` · scan ${aiStatus.last_decision.symbols_scanned.join("/")}` : "")}</div>
                  <div className="text-[var(--txt)]">
                    <span className="font-bold mono">{aiStatus.last_decision.action}</span>
                    {aiStatus.last_decision.symbol ? ` ${aiStatus.last_decision.symbol}` : ''}
                    {aiStatus.last_decision.side ? ` ${aiStatus.last_decision.side}` : ''}
                    {aiStatus.last_decision.confidence != null ? ` · conf ${aiStatus.last_decision.confidence}` : ''}
                  </div>
                  {aiStatus.last_decision.reason && (
                    <div className="text-[var(--txt-muted)] mt-0.5 whitespace-pre-wrap break-words max-h-40 overflow-y-auto rounded-md border border-[var(--border)] bg-[var(--bg-elevated)] p-2 text-[11px] leading-relaxed">{aiStatus.last_decision.pulse || aiStatus.pulse || aiStatus.last_decision.reason}</div>
                  )}
                </div>
              )}
              {!isGuest && (
                <div className="flex flex-col gap-1.5">
                  {aiStatus?.running ? (
                    <>
                      <button
                        className="btn btn-secondary btn-sm w-full"
                        disabled={aiBusy}
                        onClick={async () => {
                          setAiBusy(true)
                          try {
                            const r = await api.aiDecide()
                            setAiStatus(prev => ({ ...(prev || {}), last_decision: r.decision }))
                            loadData()
                          } catch (e) { alert(e.message) }
                          finally { setAiBusy(false) }
                        }}
                      >
                        {aiBusy ? '…' : 'Decide now'}
                      </button>
                      <button className="btn btn-secondary btn-sm w-full" onClick={async () => {
                        try {
                          const r = await api.aiLogs(100)
                          console.log('AI logs', r)
                          alert(`Logs: memory=${r.memory_n} file=${r.file_n} (см. console + /api/ai/logs/download)`)
                        } catch (e) { alert(e.message) }
                      }}>
                        Export logs
                      </button>
                      <button className="btn btn-danger btn-sm w-full" onClick={async () => {
                        try { await api.aiStop(); loadData() } catch (e) { alert(e.message) }
                      }}>
                        <Square size={12} /> {t('dash.stop_bot')}
                      </button>
                    </>
                  ) : (
                    <button className="btn btn-primary btn-sm w-full" onClick={async () => {
                      try {
                        await api.aiStart({ capital: 10000, provider: 'groq', execute: true })
                        loadData()
                      } catch (e) { alert(e.message) }
                    }}>
                      <Play size={12} /> {t('dash.start')} (demo exec)
                    </button>
                  )}
                </div>
              )}
            </div>
          </div>
          )}

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