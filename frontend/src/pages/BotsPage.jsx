import React, { useState, useEffect, useCallback, useRef, useMemo, forwardRef } from 'react'
import {
  Play, Square, Edit3, TrendingUp, Zap, Clock, RotateCcw,
  ShieldCheck, BadgeCheck, CheckCircle2, Award, FlaskConical
} from 'lucide-react'
import { api } from '../services/api'
import { SliderPanel, Tip, StatusBadge, ConfirmDialog, getStrategyDesc, Loader } from '../components/ui'
import { useTranslation } from '../hooks/useTranslation'

const SYMBOL_OPTIONS = ['BTC', 'ETH', 'BNB', 'XRP', 'SOL', 'DOGE', 'ADA', 'TRX', 'AVAX', 'LTC']

/** Coins the validation bot trades (MACD+Donchian universe, 10 coins like Momentum/Impulse) */
const VALIDATION_SYMBOL_OPTIONS = ['BTC', 'ETH', 'BNB', 'XRP', 'SOL', 'DOGE', 'ADA', 'TRX', 'AVAX', 'LTC']

/** Params that map 1:1 to RotationConfig on the backend */
const ROTATION_PARAMS = [
  'capital', 'top_k', 'risk_per_trade', 'poll_interval_sec',
  'breakeven_pct', 'partial_tp_pct', 'partial_tp_ratio',
  'trail_atr_mult', 'adx_min', 'min_hold_days', 'max_leverage',
]

const PARAM_BASE = {
  capital:            { min: 500, max: 100000, step: 500, unit: '$' },
  top_k:              { min: 1, max: 4, step: 1, unit: '' },
  risk_per_trade:     { min: 0.5, max: 20, step: 0.5, unit: '%', asPercent: true },
  poll_interval_sec:  { min: 60, max: 900, step: 60, unitKey: 'bots.param.poll_interval_sec.unit', unit: 's' },
  breakeven_pct:      { min: 0.5, max: 10, step: 0.5, unit: '%', asPercent: true },
  partial_tp_pct:     { min: 1, max: 20, step: 0.5, unit: '%', asPercent: true },
  partial_tp_ratio:   { min: 20, max: 100, step: 5, unit: '%', asPercent: true },
  trail_atr_mult:     { min: 0.2, max: 2, step: 0.1, unit: '×ATR' },
  adx_min:            { min: 10, max: 40, step: 1, unit: '' },
  min_hold_days:      { min: 1, max: 14, step: 1, unit: 'd' },
  max_leverage:       { min: 1, max: 5, step: 0.5, unit: 'x' },
}

const DEFAULT_MOM_CONFIG = {
  capital: 10000, top_k: 2, risk_per_trade: 0.14, poll_interval_sec: 300,
  breakeven_pct: 0.05, partial_tp_pct: 0.08, partial_tp_ratio: 0.5,
  trail_atr_mult: 0.2, adx_min: 29, min_hold_days: 11, max_leverage: 2,
}

/** Params that map 1:1 to ImpulseConfig on the backend */
const IMPULSE_PARAMS = [
  'capital', 'top_k', 'risk_per_trade', 'poll_interval_sec',
  'entry_roc', 'max_adds', 'cooldown_bars',
  'sl_atr_mult', 'sl_atr_mult_short', 'trail_atr_mult', 'trail_atr_mult_short',
  'tp1_atr', 'tp1_frac', 'tp2_atr', 'tp2_frac', 'max_hold_bars', 'max_leverage',
]

/** Impulse uses its own ranges (shares keys with momentum but different scale) */
const IMPULSE_PARAM_BASE = {
  ...PARAM_BASE,
  entry_roc:            { min: 1, max: 8, step: 0.5, unit: '%' },
  max_adds:             { min: 0, max: 4, step: 1, unit: '' },
  cooldown_bars:        { min: 0, max: 15, step: 1, unit: 'd' },
  sl_atr_mult:          { min: 2, max: 10, step: 0.5, unit: '×ATR' },
  sl_atr_mult_short:    { min: 2, max: 10, step: 0.5, unit: '×ATR' },
  trail_atr_mult:       { min: 3, max: 15, step: 0.5, unit: '×ATR' },
  trail_atr_mult_short: { min: 3, max: 15, step: 0.5, unit: '×ATR' },
  tp1_atr:              { min: 1, max: 6, step: 0.5, unit: '×ATR' },
  tp1_frac:             { min: 10, max: 70, step: 5, unit: '%', asPercent: true },
  tp2_atr:              { min: 3, max: 12, step: 0.5, unit: '×ATR' },
  tp2_frac:             { min: 10, max: 70, step: 5, unit: '%', asPercent: true },
  max_hold_bars:        { min: 5, max: 90, step: 1, unit: 'd' },
  max_leverage:         { min: 1, max: 5, step: 0.5, unit: 'x' },
}

const DEFAULT_IMP_CONFIG = {
  capital: 10000, top_k: 4, risk_per_trade: 0.10, poll_interval_sec: 300,
  entry_roc: 4.0, max_adds: 2, cooldown_bars: 5,
  sl_atr_mult: 5.0, sl_atr_mult_short: 5.0,
  trail_atr_mult: 8.0, trail_atr_mult_short: 8.0,
  tp1_atr: 2.0, tp1_frac: 0.3, tp2_atr: 6.0, tp2_frac: 0.3,
  max_hold_bars: 30, max_leverage: 3.0,
}

/** Params the validation bot accepts via /api/validation/start */
const VALIDATION_PARAMS = [
  'capital', 'top_k', 'risk_per_trade', 'poll_interval_sec',
  'donchian_n', 'tp_pct', 'tp_ratio', 'tp2_pct', 'be_pct',
  'chandelier_atr', 'max_hold_days', 'max_leverage', 'allocation_pct',
]

/** Validation ranges (MACD+Donchian) on a small budget */
const VALIDATION_PARAM_BASE = {
  ...PARAM_BASE,
  capital:          { min: 50, max: 5000, step: 50, unit: '$' },
  top_k:            { min: 1, max: 4, step: 1, unit: '' },
  risk_per_trade:   { min: 1, max: 30, step: 1, unit: '%', asPercent: true },
  donchian_n:       { min: 5, max: 30, step: 1, unit: 'd' },
  tp_pct:           { min: 2, max: 20, step: 0.5, unit: '%', asPercent: true },
  tp_ratio:         { min: 10, max: 60, step: 5, unit: '%', asPercent: true },
  tp2_pct:          { min: 3, max: 30, step: 0.5, unit: '%', asPercent: true },
  be_pct:           { min: 0.5, max: 10, step: 0.5, unit: '%', asPercent: true },
  chandelier_atr:   { min: 2, max: 8, step: 0.5, unit: '×ATR' },
  max_hold_days:    { min: 1, max: 10, step: 1, unit: 'd' },
  max_leverage:     { min: 1, max: 3, step: 0.5, unit: 'x' },
  allocation_pct:   { min: 5, max: 100, step: 5, unit: '%', asPercent: true },
}

const DEFAULT_VAL_CONFIG = {
  capital: 300, top_k: 4, risk_per_trade: 0.14, poll_interval_sec: 300,
  donchian_n: 15, tp_pct: 0.08, tp_ratio: 0.3, tp2_pct: 0.10,
  be_pct: 0.015, chandelier_atr: 4.0, max_hold_days: 3, max_leverage: 1,
  allocation_pct: 0.15,
}

// Независимый бэктест (Backtrader, нативные 1D OKX) — по каждой стратегии
const MOM_BACKTEST = {
  years: [
    { year: '2023', ret: '+12.9%' },
    { year: '2024', ret: '+62.0%' },
    { year: '2025', ret: '+16.3%' },
    { year: '2026', ret: '+134.9%' },
  ],
  summary: { cagr: '64.4%', dd: '46.4%' },
}
const IMP_BACKTEST = {
  years: [
    { year: '2023', ret: '+114.9%' },
    { year: '2024', ret: '+114.1%' },
    { year: '2025', ret: '-10.4%' },
    { year: '2026', ret: '+34.1%' },
  ],
  summary: { cagr: '68.7%', dd: '38.7%' },
}
const VAL_BACKTEST = {
  years: [
    { year: '2023', ret: '+40.8%' },
    { year: '2024', ret: '+281.9%' },
    { year: '2025', ret: '-16.1%' },
    { year: '2026', ret: '-20.1%' },
  ],
  summary: { cagr: '48.6%', dd: '40.7%' },
}

function getParamMeta(t, base = PARAM_BASE) {
  const result = {}
  for (const key of Object.keys(base)) {
    const b = base[key]
    result[key] = {
      ...b,
      label: t(`bots.param.${key}.label`),
      tip: t(`bots.param.${key}.tip`),
      unit: b.unitKey ? t(b.unitKey) : b.unit,
    }
  }
  return result
}

function toDisplay(raw, meta) {
  if (raw == null || Number.isNaN(raw)) return meta.min
  return meta.asPercent ? +(raw * 100).toFixed(2) : raw
}

function fromDisplay(display, meta) {
  return meta.asPercent ? display / 100 : display
}

function BotSparkline({ botId, pnl }) {
  const points = useMemo(() => {
    // Deterministic shape from PnL only — no fake random walk
    const base = [100, 100, 100, 100, 100, 100, 100, 100]
    const end = pnl >= 0 ? 100 + Math.min(20, Math.abs(pnl) / 50) : 100 - Math.min(20, Math.abs(pnl) / 50)
    for (let i = 1; i < 8; i++) {
      base[i] = 100 + ((end - 100) * i) / 7
    }
    return base
  }, [pnl])

  const w = 80, h = 28, pad = 2
  const min = Math.min(...points) * 0.98
  const max = Math.max(...points) * 1.02
  const range = max - min || 1
  const xStep = (w - pad * 2) / (points.length - 1)
  const coords = points.map((v, i) => ({
    x: pad + i * xStep,
    y: pad + (1 - (v - min) / range) * (h - pad * 2),
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

function BotRuntime({ startedAt, t }) {
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
  let label
  if (h === 0 && m === 0) label = t('bots.uptime_less_1m')
  else if (h === 0) label = t('bots.uptime_minutes', { m })
  else label = t('bots.uptime_hours', { h, m })
  return (
    <div className="flex items-center gap-1.5 text-2xs text-[var(--profit)]">
      <Clock size={11} className="flex-shrink-0" />
      <span>{label}</span>
    </div>
  )
}

function RiskMeter({ percentValue }) {
  const { t } = useTranslation()
  const pct = ((percentValue - 0.5) / 9.5) * 100
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

function ManagedPill({ statusMode, managed, apiAlive, lastActivity, heartbeatMaxAge, t }) {
  if (statusMode !== 'live') return null
  let color, label
  if (!apiAlive) {
    color = 'var(--txt-muted)'
    label = t('bots.managed_offline')
  } else if (managed) {
    color = 'var(--profit)'
    label = t('bots.managed_yes')
  } else {
    color = 'var(--loss)'
    label = t('bots.managed_no')
  }

  let lastStr = ''
  if (lastActivity) {
    const ts = Date.parse(lastActivity)
    if (!Number.isNaN(ts)) {
      const mins = Math.max(0, Math.floor((Date.now() - ts) / 60000))
      lastStr = mins < 1 ? '<1м' : `${mins}м`
    }
  }

  const stale = apiAlive && heartbeatMaxAge && lastActivity &&
    (Date.now() - Date.parse(lastActivity)) > heartbeatMaxAge * 1000

  return (
    <div className="flex items-center gap-1.5" style={{ color }} title={lastActivity ? `${t('bots.managed_last_activity')}: ${lastActivity}` : t('bots.managed_tip')}>
      <span className="relative flex h-2 w-2 flex-shrink-0">
        <span className="absolute inline-flex h-full w-full rounded-full opacity-50 animate-ping" style={{ background: color }} />
        <span className={`relative inline-flex rounded-full h-2 w-2 ${stale ? 'animate-pulse' : ''}`} style={{ background: color }} />
      </span>
      <span className="text-[0.6rem] font-semibold whitespace-nowrap">{label}</span>
      {lastStr && <span className="text-[0.55rem] opacity-70 whitespace-nowrap">{lastStr}</span>}
      <Tip text={t('bots.managed_tip')} />
    </div>
  )
}

function PerfTile({ label, value, tone = 'neutral' }) {
  const color = tone === 'profit' ? 'text-[var(--profit)]' : tone === 'loss' ? 'text-[var(--loss)]' : 'text-[var(--txt)]'
  return (
    <div className="rounded-lg bg-[var(--bg)] ring-1 ring-[var(--border)]/60 px-2.5 py-2">
      <div className="text-[0.62rem] text-[var(--txt-muted)] uppercase tracking-wide">{label}</div>
      <div className={`mono text-base font-bold mt-0.5 truncate ${color}`}>{value}</div>
    </div>
  )
}

function BotCard({
  id, name, stratId, version, icon: Icon, accentDim, accentTxt,
  statusMode, statusLabel, coins, description, tags = [],
  tagline, backtest,
  pnl, trades, winRate, sparklinePnl, startedAt,
  openPositions = [], onToggle, onReset, onEdit,
  managed, lastActivity, heartbeatMaxAge, apiAlive,
  isGuest, loading, t,
}) {
  const pnlStr = `$${pnl >= 0 ? '+' : ''}${Number(pnl || 0).toFixed(2)}`
  return (
    <div className="panel !overflow-visible flex flex-col transition-colors hover:border-[var(--border-hover)]">
      {/* ─── Banner ─── */}
      <div className={`relative px-4 py-3.5 border-b border-[var(--border)] bg-gradient-to-br ${accentDim} via-transparent to-transparent rounded-t-[var(--radius-lg)]`}>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-11 h-11 rounded-xl bg-[var(--bg)]/70 ring-1 ring-[var(--border)] flex items-center justify-center shadow-sm">
              <Icon size={22} className={accentTxt} />
            </div>
            <div>
              <div className="flex items-center gap-2">
                <span className="text-base font-bold text-[var(--txt)]">{name}</span>
                {version && (
                  <span className="text-[0.6rem] font-bold mono px-1.5 py-0.5 rounded-md bg-[var(--info)]/15 text-[var(--info)]">{version}</span>
                )}
              </div>
              <div className="text-2xs text-[var(--txt-muted)] mono">{stratId}</div>
            </div>
          </div>
          <div className="flex flex-col items-end gap-1.5">
            <StatusBadge mode={statusMode} label={statusLabel} />
            <ManagedPill
              statusMode={statusMode}
              managed={managed}
              apiAlive={apiAlive}
              lastActivity={lastActivity}
              heartbeatMaxAge={heartbeatMaxAge}
              t={t}
            />
          </div>
        </div>
      </div>

      {/* ─── Body ─── */}
      <div className="p-4 space-y-4 flex flex-col flex-1">
        {/* Assets */}
        <div className="flex items-center gap-1.5 flex-wrap">
          <span className="text-2xs text-[var(--txt-muted)]">{t('bots.assets')}:</span>
          {coins.map(s => (
            <span key={s} className={`px-2 py-0.5 rounded-md text-2xs font-semibold ${accentDim} ${accentTxt}`}>{s}/USDT</span>
          ))}
        </div>

        {/* ─── Работоспособность ─── */}
        <div>
          <div className="flex items-center justify-between mb-2">
            <span className="text-2xs font-semibold text-[var(--txt-muted)] uppercase tracking-wider">{t('bots.perf_title')}</span>
            {statusMode === 'live' && <BotRuntime startedAt={startedAt} t={t} />}
          </div>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
            <PerfTile label={t('bots.total_pnl')} value={pnlStr} tone={pnl >= 0 ? 'profit' : 'loss'} />
            <PerfTile label={t('bots.trades_count')} value={trades} />
            <PerfTile label={t('bots.win_rate')} value={winRate != null ? `${winRate}%` : '—'} />
            <PerfTile label={t('bots.open_count')} value={openPositions.length} />
          </div>
        </div>

        {/* Open positions */}
        {openPositions.length > 0 && (
          <div className="space-y-1">
            <div className="text-2xs text-[var(--txt-muted)] font-medium">{t('dash.open_positions')}</div>
            {openPositions.map((p, i) => {
              const isLong = p.side !== 'short'
              const upnl = parseFloat(p.unrealized_pnl || 0)
              const stop = p.stop ?? p.stop_price
              const entry = p.entry ?? p.entry_price
              return (
                <div key={i} className="flex items-center justify-between gap-2 text-2xs p-1.5 rounded bg-[var(--bg)]">
                  <div className="flex items-center gap-1.5 flex-shrink-0">
                    <span className={`px-1 py-0.5 rounded font-bold ${isLong ? 'bg-[var(--profit-dim)] text-[var(--profit)]' : 'bg-[var(--loss-dim)] text-[var(--loss)]'}`}>{isLong ? 'L' : 'S'}</span>
                    <span className="text-[var(--txt)] font-medium">{p.coin}</span>
                  </div>
                  <div className="flex items-center gap-2 min-w-0">
                    {entry != null && <span className="mono text-[0.6rem] text-[var(--txt-muted)]">вх {Number(entry).toFixed(4)}</span>}
                    {stop != null && <span className="mono text-[0.6rem] text-[var(--txt-muted)]">{t('bots.pos_sl')} {Number(stop).toFixed(4)}</span>}
                    {p.tp1 != null && <span className="mono text-[0.6rem] text-[var(--txt-muted)]">{t('bots.pos_tp')} {Number(p.tp1).toFixed(4)}</span>}
                    <span className={`mono font-semibold flex-shrink-0 ${upnl >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>
                      {upnl >= 0 ? '+' : ''}{upnl.toFixed(2)}
                    </span>
                  </div>
                </div>
              )
            })}
          </div>
        )}

        {/* ─── Описание / особенности ─── */}
        <div className="space-y-2">
          <div className="flex items-start gap-1">
            <div className="text-xs text-[var(--txt-secondary)] leading-relaxed">{tagline}</div>
            <Tip text={description} />
          </div>
          {tags.length > 0 && (
            <div className="flex flex-wrap gap-1.5">
              {tags.map(tag => (
                <span key={tag} className="px-2 py-0.5 rounded-full text-[0.62rem] font-medium bg-[var(--surface-overlay)] text-[var(--txt-secondary)] ring-1 ring-[var(--border)]/50">
                  {tag}
                </span>
              ))}
            </div>
          )}
        </div>

        {/* ─── Таблица доходности (Backtrader) ─── */}
        {backtest && (
        <div className="rounded-xl bg-[var(--bg)] ring-1 ring-[var(--border)]/60 p-3">
          <div className="flex items-center gap-1.5 mb-2">
            <BadgeCheck size={13} className="text-[var(--info)] flex-shrink-0" />
            <span className="text-[0.62rem] font-semibold text-[var(--txt-secondary)]">{t('bots.ft_verified')}</span>
          </div>
          <div className="flex items-center justify-between mb-2">
            <span className="text-2xs font-semibold text-[var(--txt-muted)] uppercase tracking-wider">{t('bots.yearly_title')}</span>
            <span className="text-[0.6rem] text-[var(--txt-muted)]">CAGR {backtest.summary.cagr} · DD {backtest.summary.dd}</span>
          </div>
          <div className="grid grid-cols-5 gap-1 text-center">
            {backtest.years.map(y => (
              <div key={y.year} className="rounded-md bg-[var(--surface-overlay)]/50 px-1 py-1.5">
                <div className="text-[0.6rem] text-[var(--txt-muted)]">{y.year}</div>
                <div className={`mono text-xs font-bold ${y.ret.startsWith('-') ? 'text-[var(--loss)]' : 'text-[var(--profit)]'}`}>{y.ret}</div>
              </div>
            ))}
          </div>
          <div className="text-[0.6rem] text-[var(--txt-muted)] mt-2 leading-snug">{t('bots.yearly_note')}</div>
        </div>
        )}

        {/* ─── Actions ─── */}
        {!isGuest && (
          <div className="flex gap-1.5 pt-1 mt-auto">
            <button
              className={`btn btn-sm flex-1 ${statusMode === 'live' ? 'btn-danger' : 'btn-primary'}`}
              onClick={onToggle}
              disabled={loading}
            >
              {loading ? <Loader /> : statusMode === 'live' ? <><Square size={11} /> {t('bots.stop')}</> : <><Play size={11} /> {t('bots.start')}</>}
            </button>
            {onReset && <button className="btn btn-ghost btn-sm" onClick={onReset} title="Reset"><RotateCcw size={12} /></button>}
            {onEdit && <button className="btn btn-ghost btn-sm" onClick={onEdit}><Edit3 size={12} /></button>}
          </div>
        )}
      </div>
    </div>
  )
}

function loadSavedConfig(key, fallback) {
  try {
    const raw = localStorage.getItem(key)
    if (!raw) return fallback
    return { ...fallback, ...JSON.parse(raw) }
  } catch {
    return fallback
  }
}

export default function BotsPage({ connected, isGuest }) {
  const { t } = useTranslation()
  const strategyDesc = getStrategyDesc(t)

  const [momentumStatus, setMomentumStatus] = useState(null)
  const [momLoading, setMomLoading] = useState(false)
  const [impulseStatus, setImpulseStatus] = useState(null)
  const [impLoading, setImpLoading] = useState(false)
  const [valStatus, setValStatus] = useState(null)
  const [valLoading, setValLoading] = useState(false)
  const [apiAlive, setApiAlive] = useState(true)
  const [confirmStopAll, setConfirmStopAll] = useState(false)
  const [sliderOpen, setSliderOpen] = useState(false)
  const [editingBot, setEditingBot] = useState(null) // 'momentum' | 'impulse' | 'validation'
  const [saving, setSaving] = useState(false)
  const formRef = useRef(null)

  const [momLocal, setMomLocal] = useState(() => loadSavedConfig('bot_config_momentum', {
    symbols: ['BTC', 'ETH', 'BNB', 'XRP', 'SOL', 'DOGE', 'ADA', 'TRX', 'AVAX', 'LTC'],
    config: DEFAULT_MOM_CONFIG,
  }))

  const [impLocal, setImpLocal] = useState(() => loadSavedConfig('bot_config_impulse', {
    symbols: ['BTC', 'ETH', 'BNB', 'XRP', 'SOL', 'DOGE', 'ADA', 'TRX', 'AVAX', 'LTC'],
    config: DEFAULT_IMP_CONFIG,
  }))

  const [valLocal, setValLocal] = useState(() => loadSavedConfig('bot_config_validation', {
    symbols: [...VALIDATION_SYMBOL_OPTIONS],
    config: DEFAULT_VAL_CONFIG,
  }))

  const refreshStatus = useCallback(async () => {
    let anyOk = false
    try {
      const m = await api.momentumStatus().catch(() => null)
      if (m) {
        anyOk = true
        setMomentumStatus(m)
        if (m.config) {
          setMomLocal(prev => ({
            symbols: m.config.symbols || prev.symbols,
            config: { ...prev.config, ...pickRotationParams(m.config) },
          }))
        }
      }
    } catch { /* ignore */ }
    try {
      const i = await api.impulseStatus().catch(() => null)
      if (i) {
        anyOk = true
        setImpulseStatus(i)
        if (i.config) {
          setImpLocal(prev => ({
            symbols: i.config.symbols || prev.symbols,
            config: { ...prev.config, ...pickParams(i.config, IMPULSE_PARAMS) },
          }))
        }
      }
    } catch { /* ignore */ }
    try {
      const v = await api.validationStatus().catch(() => null)
      if (v) {
        anyOk = true
        setValStatus(v)
        if (v.config) {
          setValLocal(prev => ({
            symbols: v.config.symbols || prev.symbols,
            config: { ...prev.config, ...pickParams(v.config, VALIDATION_PARAMS) },
          }))
        }
      }
    } catch { /* ignore */ }
    setApiAlive(anyOk)
  }, [])

  useEffect(() => {
    refreshStatus()
    const id = setInterval(refreshStatus, 10000)
    return () => clearInterval(id)
  }, [connected, refreshStatus])

  const momToggle = async () => {
    setMomLoading(true)
    try {
      if (momentumStatus?.running) {
        await api.momentumStop()
      } else {
        await api.momentumStart({
          symbols: momLocal.symbols,
          ...momLocal.config,
          leverage: momLocal.config.max_leverage,
        })
      }
      await refreshStatus()
    } catch (e) { alert(e.message) }
    setMomLoading(false)
  }

  const impToggle = async () => {
    setImpLoading(true)
    try {
      if (impulseStatus?.running) {
        await api.impulseStop()
      } else {
        await api.impulseStart({
          symbols: impLocal.symbols,
          ...impLocal.config,
        })
      }
      await refreshStatus()
    } catch (e) { alert(e.message) }
    setImpLoading(false)
  }

  const valToggle = async () => {
    setValLoading(true)
    try {
      if (valStatus?.running) {
        await api.validationStop()
      } else {
        await api.validationStart({
          symbols: valLocal.symbols,
          ...valLocal.config,
        })
      }
      await refreshStatus()
    } catch (e) { alert(e.message) }
    setValLoading(false)
  }

  const handleSave = (botData) => {
    setSaving(true)
    const payload = { symbols: botData.symbols, config: botData.config }
    if (editingBot === 'momentum') {
      setMomLocal(payload)
      localStorage.setItem('bot_config_momentum', JSON.stringify(payload))
    } else if (editingBot === 'impulse') {
      setImpLocal(payload)
      localStorage.setItem('bot_config_impulse', JSON.stringify(payload))
    } else if (editingBot === 'validation') {
      setValLocal(payload)
      localStorage.setItem('bot_config_validation', JSON.stringify(payload))
    }
    setTimeout(() => {
      setSaving(false)
      setSliderOpen(false)
      setEditingBot(null)
    }, 200)
  }

  const momRunning = !!momentumStatus?.running
  const momStartedAt = momentumStatus?.started_at ? Date.parse(momentumStatus.started_at) : null

  const momCfg = momentumStatus?.config
  const momTags = [
    ...(momCfg?.symbols?.length ? [`${momCfg.symbols.length} монет`] : []),
    t('bots.tag_timeframe'),
    t('bots.tag_positions', { n: momCfg?.top_k || 2 }),
    ...(momCfg?.max_leverage ? [t('bots.tag_leverage', { x: momCfg.max_leverage })] : []),
    t('bots.tag_regime'),
    t('bots.tag_trailing'),
    t('bots.tag_roi'),
  ]

  const impRunning = !!impulseStatus?.running
  const impStartedAt = impulseStatus?.started_at ? Date.parse(impulseStatus.started_at) : null

  const valRunning = !!valStatus?.running
  const valStartedAt = valStatus?.started_at ? Date.parse(valStatus.started_at) : null

  const valCfg = valStatus?.config
  const valTags = [
    ...(valCfg?.symbols?.length ? [`${valCfg.symbols.length} монет`] : []),
    t('bots.tag_timeframe'),
    t('bots.tag_positions', { n: valCfg?.top_k || 4 }),
    ...(valCfg?.max_leverage ? [t('bots.tag_leverage', { x: valCfg.max_leverage })] : []),
    t('bots.tag_breakout'),
    t('bots.tag_partial_tp'),
  ]

  const impCfg = impulseStatus?.config
  const impTags = [
    ...(impCfg?.symbols?.length ? [`${impCfg.symbols.length} монет`] : []),
    t('bots.tag_timeframe'),
    t('bots.tag_positions', { n: impCfg?.top_k || 4 }),
    ...(impCfg?.max_leverage ? [t('bots.tag_leverage', { x: impCfg.max_leverage })] : []),
    t('bots.tag_pyramid'),
    t('bots.tag_cascade_tp'),
    t('bots.tag_trailing'),
  ]

  return (
    <div className="h-full flex flex-col p-4 gap-4 overflow-auto">
      <div className="flex items-center justify-between flex-shrink-0">
        <div>
          <h2 className="text-lg font-bold text-[var(--txt)]">{t('bots.title')}</h2>
          <p className="text-xs text-[var(--txt-muted)]">{t('bots.subtitle')}</p>
        </div>
        <div className="flex gap-2">
          {!isGuest && (
            <button className="btn btn-ghost btn-sm" onClick={() => setConfirmStopAll(true)}>
              <Square size={12} /> {t('bots.stop_all')}
            </button>
          )}
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
        <BotCard
          id="momentum"
          name={t('dash.momentum_bot')}
          stratId="momentum_rotation"
          version={momentumStatus?.version}
          icon={TrendingUp}
          accentDim="bg-[var(--info-dim)]"
          accentTxt="text-[var(--info)]"
          statusMode={momRunning ? 'live' : 'stopped'}
          statusLabel={momRunning ? t('bots.status_running') : t('bots.status_stopped')}
          coins={momentumStatus?.config?.symbols || momLocal.symbols}
          description={momentumStatus?.description || strategyDesc.momentum}
          tags={momTags}
          tagline={t('bots.tagline')}
          backtest={MOM_BACKTEST}
          pnl={momentumStatus?.total_pnl || 0}
          trades={momentumStatus?.total_trades || 0}
          winRate={momentumStatus?.win_rate}
          sparklinePnl={momentumStatus?.total_pnl || 0}
          startedAt={momRunning ? momStartedAt : null}
          openPositions={momentumStatus?.open_positions || []}
          managed={momentumStatus?.managed}
          lastActivity={momentumStatus?.last_activity}
          heartbeatMaxAge={momentumStatus?.heartbeat_max_age_sec}
          apiAlive={apiAlive}
          onToggle={momToggle}
          onEdit={() => { setEditingBot('momentum'); setSliderOpen(true) }}
          isGuest={isGuest}
          loading={momLoading}
          t={t}
        />

        <BotCard
          id="impulse"
          name={t('docs.strat_impulse_title')}
          stratId={impulseStatus?.strategy || 'impulse_1d'}
          version={impulseStatus?.version}
          icon={Zap}
          accentDim="bg-[var(--profit-dim)]"
          accentTxt="text-[var(--profit)]"
          statusMode={impRunning ? 'live' : 'stopped'}
          statusLabel={impRunning ? t('bots.status_running') : t('bots.status_stopped')}
          coins={impulseStatus?.config?.symbols || impLocal.symbols}
          description={impulseStatus?.description || strategyDesc.impulse}
          tags={impTags}
          tagline={t('bots.tagline_impulse')}
          backtest={IMP_BACKTEST}
          pnl={impulseStatus?.total_pnl || 0}
          trades={impulseStatus?.total_trades || 0}
          winRate={impulseStatus?.win_rate}
          sparklinePnl={impulseStatus?.total_pnl || 0}
          startedAt={impRunning ? impStartedAt : null}
          openPositions={impulseStatus?.open_positions || []}
          managed={impulseStatus?.managed}
          lastActivity={impulseStatus?.last_activity}
          heartbeatMaxAge={impulseStatus?.heartbeat_max_age_sec}
          apiAlive={apiAlive}
          onToggle={impToggle}
          onReset={() => {
            if (window.confirm('Сбросить историю сделок Impulse 1D?')) {
              api.impulseReset().then(refreshStatus).catch(e => alert(e.message))
            }
          }}
          onEdit={() => { setEditingBot('impulse'); setSliderOpen(true) }}
          isGuest={isGuest}
          loading={impLoading}
          t={t}
        />

        {!isGuest && (
          <BotCard
            id="validation"
            name={t('dash.validation_bot')}
            stratId={valStatus?.strategy || 'macd_donchian_validation'}
            version={valStatus?.version}
            icon={FlaskConical}
            accentDim="bg-[var(--warn-dim)]"
            accentTxt="text-[var(--warn)]"
            statusMode={valRunning ? 'live' : 'stopped'}
            statusLabel={valRunning ? t('bots.status_running') : t('bots.status_stopped')}
            coins={valStatus?.config?.symbols || valLocal.symbols}
            description={valStatus?.description || t('bots.validation_desc')}
            tags={valTags}
            tagline={t('bots.tagline_validation')}
            backtest={VAL_BACKTEST}
            pnl={valStatus?.total_pnl || 0}
            trades={valStatus?.total_trades || 0}
            winRate={valStatus?.win_rate}
            sparklinePnl={valStatus?.total_pnl || 0}
            startedAt={valRunning ? valStartedAt : null}
            openPositions={valStatus?.open_positions || []}
            managed={valStatus?.managed}
            lastActivity={valStatus?.last_activity}
            heartbeatMaxAge={valStatus?.heartbeat_max_age_sec}
            apiAlive={apiAlive}
            onToggle={valToggle}
            onReset={() => {
              if (window.confirm(t('bots.validation_reset_confirm'))) {
                api.validationReset().then(refreshStatus).catch(e => alert(e.message))
              }
            }}
            onEdit={() => { setEditingBot('validation'); setSliderOpen(true) }}
            isGuest={isGuest}
            loading={valLoading}
            t={t}
          />
        )}
      </div>

      <SliderPanel
        open={sliderOpen}
        onClose={() => { setSliderOpen(false); setEditingBot(null) }}
        title={`${t('bots.edit')} ${
          editingBot === 'impulse' ? t('docs.strat_impulse_title')
          : editingBot === 'validation' ? t('dash.validation_bot')
          : 'Momentum'
        }`}
        footer={
          <>
            <button className="btn btn-ghost" onClick={() => { setSliderOpen(false); setEditingBot(null) }}>{t('bots.cancel')}</button>
            <button className="btn btn-primary" onClick={() => formRef.current?.requestSubmit()} disabled={saving}>
              {saving ? <Loader /> : <><Zap size={13} /> {t('bots.save')}</>}
            </button>
          </>
        }
      >
        <BotConfigForm
          ref={formRef}
          botType={editingBot}
          symbols={editingBot === 'impulse' ? impLocal.symbols : editingBot === 'validation' ? valLocal.symbols : momLocal.symbols}
          config={editingBot === 'impulse' ? impLocal.config : editingBot === 'validation' ? valLocal.config : momLocal.config}
          onSave={handleSave}
        />
      </SliderPanel>

      <ConfirmDialog
        open={confirmStopAll}
        onClose={() => setConfirmStopAll(false)}
        onConfirm={async () => {
          try { await api.momentumStop() } catch {}
          try { await api.impulseStop() } catch {}
          try { await api.validationStop() } catch {}
          await refreshStatus()
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

function pickRotationParams(cfg) {
  const out = {}
  for (const key of ROTATION_PARAMS) {
    if (cfg[key] != null) out[key] = cfg[key]
  }
  if (cfg.leverage != null && out.max_leverage == null) out.max_leverage = cfg.leverage
  return out
}

function pickParams(cfg, keys) {
  const out = {}
  for (const key of keys) {
    if (cfg[key] != null) out[key] = cfg[key]
  }
  return out
}

const BotConfigForm = forwardRef(function BotConfigForm({ botType, symbols, config, onSave }, ref) {
  const { t } = useTranslation()
  const isImpulse = botType === 'impulse'
  const isValidation = botType === 'validation'
  const defaultConfig = isImpulse ? DEFAULT_IMP_CONFIG : isValidation ? DEFAULT_VAL_CONFIG : DEFAULT_MOM_CONFIG
  const PARAM_LIST = isImpulse ? IMPULSE_PARAMS : isValidation ? VALIDATION_PARAMS : ROTATION_PARAMS
  const PARAM_META = useMemo(() => getParamMeta(t, isImpulse ? IMPULSE_PARAM_BASE : isValidation ? VALIDATION_PARAM_BASE : PARAM_BASE), [t, isImpulse, isValidation])

  const [form, setForm] = useState({
    symbols: symbols?.length ? [...symbols] : (isValidation ? [...VALIDATION_SYMBOL_OPTIONS] : ['BTC', 'ETH', 'BNB', 'XRP', 'SOL', 'DOGE', 'ADA', 'TRX', 'AVAX', 'LTC']),
    config: { ...defaultConfig, ...config },
  })

  useEffect(() => {
    setForm({
      symbols: symbols?.length ? [...symbols] : (isValidation ? [...VALIDATION_SYMBOL_OPTIONS] : ['BTC', 'ETH', 'BNB', 'XRP', 'SOL', 'DOGE', 'ADA', 'TRX', 'AVAX', 'LTC']),
      config: { ...defaultConfig, ...config },
    })
  }, [botType, symbols, config])

  const handleSubmit = (e) => {
    e.preventDefault()
    onSave(form)
  }

  const updateConfig = (key, val) => {
    setForm(f => ({ ...f, config: { ...f.config, [key]: val } }))
  }

  return (
    <form ref={ref} onSubmit={handleSubmit} className="space-y-5">
      <div className="flex items-center gap-2 p-2.5 rounded-lg bg-[var(--bg)] border border-[var(--border)]">
        {isImpulse
          ? <Zap size={14} className="text-[var(--profit)]" />
          : isValidation
            ? <FlaskConical size={14} className="text-[var(--warn)]" />
            : <TrendingUp size={14} className="text-[var(--info)]" />}
        <div>
          <div className="text-xs font-semibold text-[var(--txt)]">
            {isImpulse ? t('docs.strat_impulse_title') : isValidation ? t('dash.validation_bot') : t('dash.momentum_bot')}
          </div>
          <div className="text-2xs text-[var(--txt-muted)]">
            {isImpulse ? 'impulse_1d' : isValidation ? 'macd_donchian_validation' : 'momentum_rotation'}
          </div>
        </div>
      </div>

      <div>
        <label className="text-2xs font-medium text-[var(--txt-muted)] uppercase tracking-wider flex items-center gap-1">
          {t('bots.coins_label')} <Tip text={t('bots.coins_tip')} />
        </label>
        <div className="flex flex-wrap gap-2 mt-2">
          {(isValidation ? VALIDATION_SYMBOL_OPTIONS : SYMBOL_OPTIONS).map(s => {
            const active = form.symbols.includes(s)
            return (
              <button
                key={s}
                type="button"
                onClick={() => setForm(f => ({
                  ...f,
                  symbols: active
                    ? (f.symbols.length > 1 ? f.symbols.filter(x => x !== s) : f.symbols)
                    : [...f.symbols, s],
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

      <div>
        <label className="text-2xs font-medium text-[var(--txt-muted)] uppercase tracking-wider mb-3 block">{t('bots.params')}</label>
        <div className="space-y-4">
          {PARAM_LIST.map(key => {
            const meta = PARAM_META[key]
            if (!meta) return null
            const rawVal = form.config[key] ?? (meta.asPercent ? meta.min / 100 : meta.min)
            const displayVal = toDisplay(rawVal, meta)
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
                    value={displayVal}
                    onChange={e => updateConfig(key, fromDisplay(parseFloat(e.target.value), meta))}
                    className="flex-1"
                  />
                  <input
                    type="number"
                    className="w-20 text-right mono"
                    value={displayVal}
                    onChange={e => {
                      const v = parseFloat(e.target.value)
                      if (!isNaN(v)) updateConfig(key, fromDisplay(v, meta))
                    }}
                    step={meta.step}
                    min={meta.min}
                    max={meta.max}
                  />
                </div>
                {key === 'risk_per_trade' && <RiskMeter percentValue={displayVal} />}
              </div>
            )
          })}
        </div>
      </div>

      <p className="text-2xs text-[var(--txt-muted)]">
        {t('bots.config_apply_hint')}
      </p>
    </form>
  )
})
