import React, { useState, useEffect, useCallback, useRef, useMemo, forwardRef } from 'react'
import {
  Plus, Play, Pause, Square, Copy, Trash2, Edit3, Bot, Settings2,
  TrendingUp, X, Loader2, Zap, ChevronRight, Clock, RotateCcw
} from 'lucide-react'
import { api } from '../services/api'
import { SliderPanel, Tip, StatusBadge, MetricCard, ConfirmDialog, getStrategyDesc, EmptyState, Loader } from '../components/ui'
import { useTranslation } from '../hooks/useTranslation'

const STRATEGIES_BASE = [
  { id: 'momentum', nameKey: null, name: 'Momentum', icon: TrendingUp, descKey: 'momentum', params: [
    'max_budget', 'max_notional_per_position_pct', 'max_total_notional_pct',
    'signal_risk_min', 'signal_risk_max', 'signal_adx_weak', 'signal_adx_strong',
    'risk_per_trade', 'max_positions', 'poll_interval_sec', 'trail_pct', 'breakeven_pct',
    'tp1_pct', 'tp1_frac', 'sl1_pct', 'sl1_frac', 'adx_threshold'
  ] },
  { id: 'grid', nameKey: 'bots.grid', name: 'Grid', icon: Settings2, descKey: 'grid', params: ['position_size', 'grid_levels', 'grid_step', 'max_positions', 'tp_pct', 'sl_pct'] },
  { id: 'dca', nameKey: null, name: 'DCA', icon: TrendingUp, descKey: 'dca', params: ['position_size', 'dca_orders', 'dca_step', 'max_positions', 'tp_pct'] },
  { id: 'scalping', nameKey: 'bots.scalping', name: 'Scalping', icon: Zap, descKey: 'scalping', params: ['position_size', 'tp_pct', 'sl_pct', 'max_positions', 'poll_interval_sec'] },
  { id: 'custom', nameKey: 'bots.custom', name: 'Custom', icon: Settings2, descKey: 'custom', params: [] },
]

function getStrategies(t) {
  const strategyDesc = getStrategyDesc(t)
  return STRATEGIES_BASE.map(s => ({
    ...s,
    name: s.nameKey ? t(s.nameKey) : s.name,
    desc: strategyDesc[s.descKey],
  }))
}

const SYMBOL_OPTIONS = ['BTC', 'ETH', 'SOL', 'BNB']

const PARAM_BASE = {
  max_budget:                     { min: 500, max: 100000, step: 500, unit: '$', div: 1 },
  max_notional_per_position_pct:   { min: 5, max: 50, step: 5, unit: '%', div: 0.01 },
  max_total_notional_pct:          { min: 30, max: 100, step: 5, unit: '%', div: 0.01 },
  signal_risk_min:                { min: 0.5, max: 3, step: 0.5, unit: '%', div: 0.01 },
  signal_risk_max:                { min: 2, max: 10, step: 0.5, unit: '%', div: 0.01 },
  signal_adx_weak:                { min: 15, max: 35, step: 1, unit: '', div: 1 },
  signal_adx_strong:              { min: 30, max: 60, step: 1, unit: '', div: 1 },
  risk_per_trade:                 { min: 0.5, max: 10, step: 0.5, unit: '%', div: 0.01 },
  max_positions:                  { min: 1, max: 10, step: 1, unit: '', div: 1 },
  poll_interval_sec:   { min: 15, max: 300, step: 15, unitKey: 'bots.param.poll_interval_sec.unit', unit: 's', div: 1 },
  trail_pct:           { min: 0.5, max: 5, step: 0.1, unit: '%', div: 0.01 },
  breakeven_pct:       { min: 0.1, max: 3, step: 0.1, unit: '%', div: 0.01 },
  tp1_pct:             { min: 0.5, max: 10, step: 0.5, unit: '%', div: 0.01 },
  tp1_frac:            { min: 20, max: 100, step: 5, unit: '%', div: 0.01 },
  sl1_pct:             { min: 0, max: 5, step: 0.5, unit: '%', div: 0.01 },
  sl1_frac:            { min: 20, max: 100, step: 5, unit: '%', div: 0.01 },
  adx_threshold:       { min: 10, max: 50, step: 1, unit: '', div: 1 },
  position_size:       { min: 1, max: 100, step: 1, unit: 'USDT', div: 1 },
  grid_levels:         { min: 2, max: 20, step: 1, unit: '', div: 1 },
  grid_step:           { min: 0.1, max: 5, step: 0.1, unit: '%', div: 1 },
  tp_pct:              { min: 0.1, max: 20, step: 0.1, unit: '%', div: 1 },
  sl_pct:              { min: 0.1, max: 20, step: 0.1, unit: '%', div: 1 },
  dca_orders:          { min: 1, max: 10, step: 1, unit: '', div: 1 },
  dca_step:            { min: 0.1, max: 10, step: 0.1, unit: '%', div: 1 },
}

function getParamMeta(t) {
  const result = {}
  for (const key of Object.keys(PARAM_BASE)) {
    const base = PARAM_BASE[key]
    result[key] = {
      ...base,
      label: t(`bots.param.${key}.label`),
      tip: t(`bots.param.${key}.tip`),
      unit: base.unitKey ? t(base.unitKey) : base.unit,
    }
  }
  return result
}

function getDefaultBot(t) {
  return {
    id: 'mom-1',
    name: t('bots.default_bot_name'),
    strategy: 'momentum',
    symbols: ['BTC', 'ETH', 'SOL', 'BNB'],
    status: 'stopped',
    config: {
      risk_per_trade: 0.03, max_positions: 4, poll_interval_sec: 60,
      trail_pct: 0.015, breakeven_pct: 0.003, tp1_pct: 0.02, tp1_frac: 0.75,
      sl1_pct: 0, sl1_frac: 0.5, adx_threshold: 20,
      max_budget: 10000, max_notional_per_position_pct: 0.25, max_total_notional_pct: 0.80,
      signal_risk_min: 0.01, signal_risk_max: 0.05, signal_adx_weak: 25, signal_adx_strong: 45,
    },
    pnl: 0, trades: 0, created: new Date().toISOString(),
  }
}

function simpleHash(str) {
  let h = 0
  for (let i = 0; i < str.length; i++) { h = ((h << 5) - h + str.charCodeAt(i)) | 0 }
  return Math.abs(h)
}
function seededRandom(seed) {
  let s = seed
  return () => { s = (s * 16807) % 2147483647; return (s - 1) / 2147483646 }
}

function BotSparkline({ botId, pnl }) {
  const points = useMemo(() => {
    const rng = seededRandom(simpleHash(botId || 'default') % 2147483647)
    const data = [100]
    for (let i = 1; i < 8; i++) {
      const delta = (rng() - 0.45) * 12
      data.push(Math.max(80, data[i - 1] + delta))
    }
    if (pnl < 0) data[data.length - 1] = Math.min(data[data.length - 1], 95)
    else if (pnl > 0) data[data.length - 1] = Math.max(data[data.length - 1], 105)
    return data
  }, [botId, pnl])

  const w = 80, h = 28, pad = 2
  const min = Math.min(...points) * 0.98
  const max = Math.max(...points) * 1.02
  const range = max - min || 1
  const xStep = (w - pad * 2) / (points.length - 1)
  const coords = points.map((v, i) => ({
    x: pad + i * xStep,
    y: pad + (1 - (v - min) / range) * (h - pad * 2)
  }))
  const lineD = coords.map((c, i) => `${i === 0 ? 'M' : 'L'}${c.x.toFixed(1)},${c.y.toFixed(1)}`).join(' ')
  const areaD = lineD + ` L${coords[coords.length - 1].x.toFixed(1)},${(h - pad).toFixed(1)} L${coords[0].x.toFixed(1)},${(h - pad).toFixed(1)} Z`
  const color = pnl >= 0 ? 'var(--profit)' : 'var(--loss)'

  return (
    <svg width={w} height={h} className="block flex-shrink-0">
      <defs>
        <linearGradient id={`spk-${botId}`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity="0.3" />
          <stop offset="100%" stopColor={color} stopOpacity="0.02" />
        </linearGradient>
      </defs>
      <path d={areaD} fill={`url(#spk-${botId})`} />
      <path d={lineD} fill="none" stroke={color} strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

function useRuntime(startedAt, t) {
  const [now, setNow] = useState(Date.now())
  useEffect(() => {
    if (!startedAt) return
    const id = setInterval(() => setNow(Date.now()), 30000)
    return () => clearInterval(id)
  }, [startedAt])
  if (!startedAt) return null
  const diffMs = now - startedAt
  const totalMin = Math.floor(diffMs / 60000)
  const h = Math.floor(totalMin / 60)
  const m = totalMin % 60
  if (h === 0 && m === 0) return t('bots.uptime_less_1m')
  if (h === 0) return t('bots.uptime_minutes', { m })
  return t('bots.uptime_hours', { h, m })
}

function RiskMeter({ value }) {
  const { t } = useTranslation()
  const pct = ((value - 0.5) / 9.5) * 100
  let color
  if (pct <= 33) color = 'var(--profit)'
  else if (pct <= 66) color = 'var(--warn)'
  else color = 'var(--loss)'
  let label
  if (pct <= 33) label = t('bots.risk_low')
  else if (pct <= 66) label = t('bots.risk_medium')
  else label = t('bots.risk_high')
  return (
    <div className="mt-2">
      <div className="flex items-center justify-between mb-1">
        <span className="text-2xs text-[var(--txt-muted)]">{t('bots.risk_level')}</span>
        <span className="text-2xs font-medium" style={{ color }}>{label}</span>
      </div>
      <div className="h-2 rounded-full bg-[var(--surface-overlay)] overflow-hidden">
        <div className="h-full rounded-full transition-all duration-200" style={{ width: `${Math.max(pct, 2)}%`, background: color }} />
      </div>
    </div>
  )
}

export default function BotsPage({ connected, isGuest }) {
  const { t } = useTranslation()
  // ── Alpha Bot State ──
  const [alphaStatus, setAlphaStatus] = useState(null)
  const [alphaLoading, setAlphaLoading] = useState(false)

  const [bots, setBots] = useState(() => {
    const saved = localStorage.getItem('bots_config')
    if (saved) {
      try {
        const parsed = JSON.parse(saved)
        // Migration: fix old percentage params that were stored as integers
        let migrated = false
        const fixed = parsed.map(b => {
          const cfg = b.config || {}
          const newCfg = { ...cfg }
          // If risk_per_trade >= 1, it's in old integer-percentage format (e.g. 3 instead of 0.03)
          if (cfg.risk_per_trade >= 1) { newCfg.risk_per_trade = cfg.risk_per_trade / 100; migrated = true }
          if (cfg.trail_pct >= 0.1 && cfg.trail_pct < 1) { /* old format like 0.15 = 15% */ } else if (cfg.trail_pct >= 1) { newCfg.trail_pct = cfg.trail_pct / 100; migrated = true }
          if (cfg.breakeven_pct >= 0.01) { /* already decimal */ } else if (cfg.breakeven_pct >= 1) { newCfg.breakeven_pct = cfg.breakeven_pct / 100; migrated = true }
          if (cfg.tp1_pct >= 1) { newCfg.tp1_pct = cfg.tp1_pct / 100; migrated = true }
          if (cfg.tp1_frac >= 1) { newCfg.tp1_frac = cfg.tp1_frac / 100; migrated = true }
          if (cfg.sl1_frac >= 1) { newCfg.sl1_frac = cfg.sl1_frac / 100; migrated = true }
          // Migration: rename old bot names to Momentum Rotation
          let name = b.name || ''
          if (name === 'Бот Momentum' || name === 'Momentum Bot' || name === 'Momentum') {
            name = 'Momentum Rotation'
            migrated = true
          }
          return { ...b, config: newCfg, name }
        })
        if (migrated) localStorage.setItem('bots_config', JSON.stringify(fixed))
        return fixed
      } catch { /* ignore parse error */ }
    }
    return [getDefaultBot(t)]
  })
  const [momentumStatus, setMomentumStatus] = useState(null)
  const [sliderOpen, setSliderOpen] = useState(false)
  const [editingBot, setEditingBot] = useState(null)
  const [confirmDelete, setConfirmDelete] = useState(null)
  const [confirmStopAll, setConfirmStopAll] = useState(false)
  const [saving, setSaving] = useState(false)
  const formRef = useRef(null)

  const STRATEGIES = useMemo(() => getStrategies(t), [t])

  useEffect(() => {
    api.momentumStatus().then(s => {
      if (s?.running) {
        setMomentumStatus(s)
        // Sync real bot symbols and config from backend
        setBots(prev => prev.map(b => b.id === 'mom-1' ? {
          ...b, status: 'running', startedAt: b.startedAt || Date.now(),
          symbols: s.config?.symbols || b.symbols,
        } : b))
      }
    }).catch(() => {})
    // Fetch Alpha status
    api.alphaStatus().then(s => setAlphaStatus(s)).catch(() => {})
  }, [connected])

  const alphaToggle = async () => {
    setAlphaLoading(true)
    try {
      if (alphaStatus?.running) {
        await api.alphaStop()
        setAlphaStatus(null)
      } else {
        const s = await api.alphaStart({})
        setAlphaStatus(s)
      }
    } catch (e) { alert(e.message) }
    setAlphaLoading(false)
  }

  const alphaReset = async () => {
    try { await api.alphaReset(); setAlphaStatus(null) } catch (e) { alert(e.message) }
  }

  const saveBots = useCallback((updaterOrArray) => {
    setBots(prev => {
      const next = typeof updaterOrArray === 'function' ? updaterOrArray(prev) : updaterOrArray
      localStorage.setItem('bots_config', JSON.stringify(next))
      return next
    })
  }, [])

  const handleSave = (botData) => {
    setSaving(true)
    setTimeout(() => {
      saveBots(prev => {
        if (botData.id) {
          return prev.map(b => b.id === botData.id ? { ...b, ...botData } : b)
        }
        return [...prev, { ...botData, id: `bot-${Date.now()}`, status: 'stopped', pnl: 0, trades: 0, created: new Date().toISOString() }]
      })
      setSaving(false)
      setSliderOpen(false)
      setEditingBot(null)
    }, 300)
  }

  const handleClone = (bot) => {
    const clone = { ...bot, id: `bot-${Date.now()}`, name: `${bot.name} ${t('bots.copy')}`, status: 'stopped', pnl: 0, trades: 0, startedAt: undefined }
    saveBots(prev => [...prev, clone])
  }

  const handleDelete = (id) => {
    saveBots(prev => prev.filter(b => b.id !== id))
    setConfirmDelete(null)
  }

  const handleToggle = async (bot) => {
    if (bot.status === 'running') {
      try { await api.momentumStop() } catch (e) { alert(e.message) }
      saveBots(prev => prev.map(b => b.id === bot.id ? { ...b, status: 'stopped' } : b))
    } else {
      // Pass all selected symbols to backend
      const syms = bot.symbols?.length ? bot.symbols : ['BTC', 'ETH', 'SOL', 'BNB']
      try { await api.momentumStart({ symbols: syms, ...bot.config }) } catch (e) { alert(e.message) }
      saveBots(prev => prev.map(b => b.id === bot.id ? { ...b, status: 'running', startedAt: Date.now() } : b))
    }
  }

  const statusMap = {
    running: { mode: 'live', label: t('bots.status_running') },
    paused: { mode: 'paused', label: t('bots.status_paused') },
    stopped: { mode: 'stopped', label: t('bots.status_stopped') },
    error: { mode: 'error', label: t('bots.status_error') },
  }

  return (
    <div className="h-full flex flex-col p-4 gap-4 overflow-auto">
      <div className="flex items-center justify-between flex-shrink-0">
        <div>
          <h2 className="text-lg font-bold text-[var(--txt)]">{t('bots.title')}</h2>
          <p className="text-xs text-[var(--txt-muted)]">{t('bots.subtitle')}</p>
        </div>
        <div className="flex gap-2">
          {!isGuest && (
            <>
              <button className="btn btn-ghost btn-sm" onClick={() => setConfirmStopAll(true)}>
                <Square size={12} /> {t('bots.stop_all')}
              </button>
              <button className="btn btn-primary btn-sm" onClick={() => { setEditingBot(null); setSliderOpen(true) }}>
                <Plus size={12} /> {t('bots.new_bot')}
              </button>
            </>
          )}
        </div>
      </div>

      {/* ═══ Alpha Strategy Bot ═══ */ }
      <div className="panel border-[var(--warn)]/30 hover:border-[var(--warn)]/60 transition-colors">
        <div className="p-4 space-y-3">
          <div className="flex items-start justify-between">
            <div className="flex items-center gap-2">
              <div className="w-8 h-8 rounded-lg bg-[var(--warn-dim)] flex items-center justify-center">
                <Zap size={16} className="text-[var(--warn)]" />
              </div>
              <div>
                <div className="text-sm font-semibold text-[var(--txt)]">Alpha Rotation</div>
                <div className="text-2xs text-[var(--txt-muted)] mono">alpha_strategy</div>
              </div>
            </div>
            <StatusBadge mode={alphaStatus?.running ? 'live' : 'stopped'} label={alphaStatus?.running ? t('bots.status_running') : t('bots.status_stopped')} />
          </div>

          <div className="text-2xs text-[var(--txt-secondary)] leading-relaxed">
            Aggressive rotation v2: RSI + ATR + correlation filters, dynamic leverage up to 3x, partial TP +7%,
            wider trailing (ATR×0.8), breakeven after 2%, BTC 200MA bear filter. Risk 3%/trade.
          </div>

          <div className="flex items-center gap-2 text-xs">
            <span className="text-[var(--txt-muted)]">Coins:</span>
            <div className="flex gap-1">
              {['BTC', 'ETH', 'SOL', 'BNB'].map(s => (
                <span key={s} className="px-1.5 py-0.5 rounded text-2xs font-medium bg-[var(--warn-dim)] text-[var(--warn)]">{s}</span>
              ))}
            </div>
          </div>

          {alphaStatus && (
            <div className="grid grid-cols-3 gap-1.5">
              <div className="p-1.5 rounded-md bg-[var(--bg)]">
                <div className="text-2xs text-[var(--txt-muted)]">PnL</div>
                <div className={`mono text-xs font-bold ${alphaStatus.total_pnl >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>
                  ${alphaStatus.total_pnl >= 0 ? '+' : ''}{(alphaStatus.total_pnl || 0).toFixed(2)}
                </div>
              </div>
              <div className="p-1.5 rounded-md bg-[var(--bg)]">
                <div className="text-2xs text-[var(--txt-muted)]">{t('bots.trades_count')}</div>
                <div className="mono text-xs font-bold text-[var(--txt)]">{alphaStatus.total_trades || 0}</div>
              </div>
              <div className="p-1.5 rounded-md bg-[var(--bg)]">
                <div className="text-2xs text-[var(--txt-muted)]">Win Rate</div>
                <div className="mono text-xs font-bold text-[var(--txt)]">{alphaStatus.win_rate || 0}%</div>
              </div>
            </div>
          )}

          {alphaStatus?.running && alphaStatus.open_positions?.length > 0 && (
            <div className="space-y-1">
              <div className="text-2xs text-[var(--txt-muted)] font-medium">Open Positions</div>
              {alphaStatus.open_positions.map((p, i) => {
                const isLong = p.side !== 'short'
                return (
                  <div className="flex items-center justify-between text-2xs p-1.5 rounded bg-[var(--bg)]">
                    <div className="flex items-center gap-1.5">
                      <span className={`px-1 py-0.5 rounded font-bold ${isLong ? 'bg-[var(--profit-dim)] text-[var(--profit)]' : 'bg-[var(--loss-dim)] text-[var(--loss)]'}`}>{isLong ? 'L' : 'S'}</span>
                      <span className="text-[var(--txt)] font-medium">{p.coin}</span>
                    </div>
                    <span className={`mono font-semibold ${p.unrealized_pnl >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>
                      {p.unrealized_pnl >= 0 ? '+' : ''}{p.unrealized_pnl?.toFixed(2)}
                    </span>
                  </div>
                )
              })}
            </div>
          )}

          {!isGuest && (
            <div className="flex gap-1.5 pt-1">
              <button className={`btn btn-sm flex-1 ${alphaStatus?.running ? 'btn-danger' : 'btn-primary'}`} onClick={alphaToggle} disabled={alphaLoading}>
                {alphaLoading ? <Loader /> : alphaStatus?.running ? <><Square size={11} /> {t('bots.stop')}</> : <><Play size={11} /> {t('bots.start')}</>}
              </button>
              <button className="btn btn-ghost btn-sm" onClick={alphaReset} title="Reset Alpha data">
                <RotateCcw size={12} />
              </button>
            </div>
          )}
        </div>
      </div>

      {bots.length === 0 ? (
        <EmptyState icon={Bot} text={t('bots.not_created')} sub={t('bots.not_created_hint')} />
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
          {bots.map(bot => {
            const st = statusMap[bot.status] || statusMap.stopped
            const strat = STRATEGIES.find(s => s.id === bot.strategy)
            const runtime = useRuntime(bot.startedAt, t)
            return (
              <div key={bot.id} className="panel hover:border-[var(--border-hover)] transition-colors">
                <div className="p-4 space-y-3">
                  <div className="flex items-start justify-between">
                    <div className="flex items-center gap-2">
                      {strat && <strat.icon size={16} className="text-[var(--info)]" />}
                      <div>
                        <div className="text-sm font-semibold text-[var(--txt)]">{bot.name}</div>
                        <div className="text-2xs text-[var(--txt-muted)] mono">{bot.id}</div>
                      </div>
                    </div>
                    <StatusBadge mode={st.mode} label={st.label} />
                  </div>

                  <div className="flex items-center gap-2 text-xs">
                    <span className="text-[var(--txt-muted)]">{t('bots.coins')}</span>
                    <div className="flex gap-1">
                      {(bot.status === 'running' && momentumStatus?.config?.symbols ? momentumStatus.config.symbols : bot.symbols || []).map(s => (
                        <span key={s} className="px-1.5 py-0.5 rounded text-2xs font-medium bg-[var(--info-dim)] text-[var(--info)]">{s}</span>
                      ))}
                    </div>
                  </div>

                  <div className="text-2xs text-[var(--txt-secondary)] leading-relaxed">
                    {strat?.desc?.substring(0, 100)}...
                  </div>

                  <BotSparkline botId={bot.id} pnl={bot.pnl} />

                  {bot.status === 'running' && runtime && (
                    <div className="flex items-center gap-1.5 text-2xs text-[var(--profit)]">
                      <Clock size={11} className="flex-shrink-0" />
                      <span>{runtime}</span>
                    </div>
                  )}

                  <div className="grid grid-cols-2 gap-2">
                    <div className="p-2 rounded-md bg-[var(--bg)]">
                      <div className="text-2xs text-[var(--txt-muted)]">{t('bots.total_pnl')}</div>
                      <div className={`mono text-sm font-bold ${bot.pnl >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>${bot.pnl.toFixed(2)}</div>
                    </div>
                    <div className="p-2 rounded-md bg-[var(--bg)]">
                      <div className="text-2xs text-[var(--txt-muted)]">{t('bots.trades_count')}</div>
                      <div className="mono text-sm font-bold text-[var(--txt)]">{bot.trades}</div>
                    </div>
                  </div>

                  {!isGuest && (
                    <div className="flex gap-1.5 pt-1">
                      <button className={`btn btn-sm flex-1 ${bot.status === 'running' ? 'btn-danger' : 'btn-primary'}`} onClick={() => handleToggle(bot)}>
                        {bot.status === 'running' ? <><Square size={11} /> {t('bots.stop')}</> : <><Play size={11} /> {t('bots.start')}</>}
                      </button>
                      <button className="btn btn-ghost btn-sm" onClick={() => { setEditingBot(bot); setSliderOpen(true) }}><Edit3 size={12} /></button>
                      <button className="btn btn-ghost btn-sm" onClick={() => handleClone(bot)}><Copy size={12} /></button>
                      <button className="btn btn-ghost btn-sm text-[var(--loss)]" onClick={() => setConfirmDelete(bot.id)}><Trash2 size={12} /></button>
                    </div>
                  )}
                </div>
              </div>
            )
          })}
        </div>
      )}

      <SliderPanel
        open={sliderOpen}
        onClose={() => { setSliderOpen(false); setEditingBot(null) }}
        title={editingBot ? `${t('bots.edit')} ${editingBot.name}` : t('bots.new_bot')}
        footer={
          <>
            <button className="btn btn-ghost" onClick={() => { setSliderOpen(false); setEditingBot(null) }}>{t('bots.cancel')}</button>
            <button className="btn btn-primary" onClick={() => formRef.current?.requestSubmit()} disabled={saving}>
              {saving ? <Loader /> : <><Zap size={13} /> {t('bots.save')}</>}
            </button>
          </>
        }
      >
        <BotConfigForm ref={formRef} bot={editingBot} onSave={handleSave} />
      </SliderPanel>

      <ConfirmDialog
        open={!!confirmDelete}
        onClose={() => setConfirmDelete(null)}
        onConfirm={() => handleDelete(confirmDelete)}
        title={t('bots.delete_bot')}
        text={t('bots.delete_confirm')}
        danger
      />
      <ConfirmDialog
        open={confirmStopAll}
        onClose={() => setConfirmStopAll(false)}
        onConfirm={async () => {
          try { await api.momentumStop() } catch {}
          saveBots(prev => prev.map(b => ({ ...b, status: 'stopped' })))
          setConfirmStopAll(false)
        }}
        title={t('bots.stop_all_confirm')}
        text={t('bots.stop_all_desc')}
        danger
        confirmText={t('bots.stop_all_btn')}
      />
    </div>
  )
}

const BotConfigForm = forwardRef(function BotConfigForm({ bot, onSave }, ref) {
  const { t } = useTranslation()
  const STRATEGIES = useMemo(() => getStrategies(t), [t])
  const PARAM_META = useMemo(() => getParamMeta(t), [t])

  const strat = bot?.strategy || 'momentum'
  const [form, setForm] = useState({
    name: bot?.name || '',
    strategy: bot?.strategy || 'momentum',
    symbols: bot?.symbols?.length ? [...bot.symbols] : ['BTC', 'ETH', 'SOL', 'BNB'],
    config: bot?.config || {
      risk_per_trade: 0.03, max_positions: 4, poll_interval_sec: 60,
      trail_pct: 0.015, breakeven_pct: 0.003, tp1_pct: 0.02, tp1_frac: 0.75,
      sl1_pct: 0, sl1_frac: 0.5, adx_threshold: 20,
      max_budget: 10000, max_notional_per_position_pct: 0.25, max_total_notional_pct: 0.80,
      signal_risk_min: 0.01, signal_risk_max: 0.05, signal_adx_weak: 25, signal_adx_strong: 45,
    },
  })

  const activeStrategy = STRATEGIES.find(s => s.id === form.strategy)
  const activeParams = activeStrategy?.params || []

  const handleSubmit = (e) => {
    e.preventDefault()
    onSave({ id: bot?.id, ...form })
  }

  const updateConfig = (key, val) => {
    setForm(f => ({ ...f, config: { ...f.config, [key]: val } }))
  }

  return (
    <form ref={ref} onSubmit={handleSubmit} className="space-y-5">
      <div>
        <label className="text-2xs font-medium text-[var(--txt-muted)] uppercase tracking-wider flex items-center gap-1">
          {t('bots.name')} <Tip text={t('bots.name_tip')} />
        </label>
        <input className="w-full mt-1.5" value={form.name} onChange={e => setForm(f => ({ ...f, name: e.target.value }))} placeholder={t('bots.default_name')} autoFocus />
      </div>

      <div>
        <label className="text-2xs font-medium text-[var(--txt-muted)] uppercase tracking-wider">{t('bots.strategy')}</label>
        <div className="grid grid-cols-2 gap-2 mt-2">
          {STRATEGIES.map(s => (
            <button
              key={s.id}
              type="button"
              onClick={() => setForm(f => ({ ...f, strategy: s.id, name: f.name || s.name }))}
              className={`p-3 rounded-lg text-left border transition-all ${
                form.strategy === s.id
                  ? 'border-[var(--info)] bg-[var(--info-dim)]'
                  : 'border-[var(--border)] hover:border-[var(--border-hover)]'
              }`}
            >
              <div className="flex items-center gap-2 mb-1">
                <s.icon size={14} className={form.strategy === s.id ? 'text-[var(--info)]' : 'text-[var(--txt-muted)]'} />
                <span className={`text-xs font-semibold ${form.strategy === s.id ? 'text-[var(--info)]' : 'text-[var(--txt)]'}`}>{s.name}</span>
              </div>
              <div className="text-2xs text-[var(--txt-muted)] leading-relaxed line-clamp-2">{s.desc?.substring(0, 80)}...</div>
            </button>
          ))}
        </div>
      </div>

      <div>
        <label className="text-2xs font-medium text-[var(--txt-muted)] uppercase tracking-wider flex items-center gap-1">
          {t('bots.coins_label')} <Tip text={t('bots.coins_tip')} />
        </label>
        <div className="flex flex-wrap gap-2 mt-2">
          {SYMBOL_OPTIONS.map(s => {
            const active = form.symbols.includes(s)
            return (
              <button
                key={s}
                type="button"
                onClick={() => setForm(f => ({
                  ...f,
                  symbols: active ? f.symbols.filter(x => x !== s) : [...f.symbols, s]
                }))}
                className={`px-3 py-1.5 rounded-lg text-xs font-semibold border transition-all ${
                  active
                    ? 'border-[var(--info)] bg-[var(--info-dim)] text-[var(--info)]'
                    : 'border-[var(--border)] text-[var(--txt-muted)] hover:border-[var(--border-hover)]'
                }`}
              >
                {s}/USDT
              </button>
            )
          })}
        </div>
      </div>

      {activeParams.length > 0 && (
        <div>
          <label className="text-2xs font-medium text-[var(--txt-muted)] uppercase tracking-wider mb-3 block">{t('bots.params')}</label>
          <div className="space-y-4">
            {activeParams.map(key => {
              const meta = PARAM_META[key]
              if (!meta) return null
              const rawVal = form.config[key] ?? meta.min
              const displayVal = meta.div < 1 ? (rawVal * meta.div).toFixed(meta.step < 1 ? 1 : 0) : rawVal
              return (
                <div key={key}>
                  <div className="flex items-center justify-between mb-1.5">
                    <span className="text-xs text-[var(--txt-secondary)] flex items-center gap-1">
                      {meta.label}
                      <Tip text={meta.tip} />
                    </span>
                    <span className="mono text-xs font-semibold text-[var(--txt)]">{displayVal}{meta.unit}</span>
                  </div>
                  <div className="flex items-center gap-3">
                    <input
                      type="range"
                      min={meta.min}
                      max={meta.max}
                      step={meta.step}
                      value={rawVal}
                      onChange={e => updateConfig(key, parseFloat(e.target.value))}
                      className="flex-1"
                    />
                    <input
                      type="number"
                      className="w-20 text-right mono"
                      value={typeof rawVal === 'number' ? displayVal : rawVal}
                      onChange={e => {
                        const v = parseFloat(e.target.value)
                        if (!isNaN(v)) updateConfig(key, meta.div < 1 ? v / meta.div : v)
                      }}
                      step={meta.step}
                      min={meta.min}
                      max={meta.max}
                    />
                  </div>
                  {key === 'risk_per_trade' && <RiskMeter value={rawVal} />}
                </div>
              )
            })}
          </div>
        </div>
      )}

      {form.strategy === 'grid' && form.config.grid_levels && form.config.grid_step && (
        <div>
          <label className="text-2xs font-medium text-[var(--txt-muted)] uppercase tracking-wider mb-2 block">{t('bots.grid_visual')}</label>
          <div className="relative h-40 bg-[var(--bg)] rounded-lg border border-[var(--border)] p-3">
            <div className="absolute inset-x-3 top-1/2 h-px bg-[var(--txt-muted)] opacity-30" />
            <span className="absolute left-1/2 -translate-x-1/2 -translate-y-1/2 text-2xs text-[var(--txt-muted)]">{t('bots.current_price')}</span>
            {Array.from({ length: form.config.grid_levels }).map((_, i) => {
              const offset = (i + 1) * 12
              return (
                <React.Fragment key={i}>
                  <div className="absolute left-3 right-3 border-t border-dashed border-[var(--profit)] opacity-40" style={{ top: `calc(50% - ${offset}px)` }}>
                    <span className="absolute right-0 -top-3 text-2xs mono text-[var(--profit)]">{t('bots.buy')}</span>
                  </div>
                  <div className="absolute left-3 right-3 border-t border-dashed border-[var(--loss)] opacity-40" style={{ top: `calc(50% + ${offset}px)` }}>
                    <span className="absolute right-0 -top-3 text-2xs mono text-[var(--loss)]">{t('bots.sell')}</span>
                  </div>
                </React.Fragment>
              )
            })}
          </div>
        </div>
      )}
    </form>
  )
})
