import React, { useState, useEffect, useCallback, useRef, useMemo, forwardRef } from 'react'
import {
  Play, Square, Edit3, TrendingUp, Zap, Clock, RotateCcw,
  ShieldCheck, BadgeCheck, CheckCircle2, Award
} from 'lucide-react'
import { api } from '../services/api'
import { SliderPanel, Tip, StatusBadge, ConfirmDialog, getStrategyDesc, Loader } from '../components/ui'
import { useTranslation } from '../hooks/useTranslation'

const SYMBOL_OPTIONS = ['BTC', 'ETH', 'SOL', 'BNB']

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

// Валидированные результаты бэктеста (walk-forward, OKX 0.05%, плечо 2x)
const BACKTEST_YEARS = [
  { year: '2022', ret: '+21%' },
  { year: '2023', ret: '+234%' },
  { year: '2024', ret: '+64%' },
  { year: '2025', ret: '+17%' },
  { year: '2026', ret: '+76%' },
]
const BACKTEST_SUMMARY = { cagr: '76.6%', dd: '33%' }

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
  pnl, trades, winRate, sparklinePnl, startedAt,
  openPositions = [], onToggle, onReset, onEdit,
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
          <StatusBadge mode={statusMode} label={statusLabel} />
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
            <PerfTile label={t('bots.cagr')} value={BACKTEST_SUMMARY.cagr} />
          </div>
        </div>

        {/* Open positions */}
        {openPositions.length > 0 && (
          <div className="space-y-1">
            <div className="text-2xs text-[var(--txt-muted)] font-medium">{t('dash.open_positions')}</div>
            {openPositions.map((p, i) => {
              const isLong = p.side !== 'short'
              const upnl = parseFloat(p.unrealized_pnl || 0)
              return (
                <div key={i} className="flex items-center justify-between text-2xs p-1.5 rounded bg-[var(--bg)]">
                  <div className="flex items-center gap-1.5">
                    <span className={`px-1 py-0.5 rounded font-bold ${isLong ? 'bg-[var(--profit-dim)] text-[var(--profit)]' : 'bg-[var(--loss-dim)] text-[var(--loss)]'}`}>{isLong ? 'L' : 'S'}</span>
                    <span className="text-[var(--txt)] font-medium">{p.coin}</span>
                  </div>
                  <span className={`mono font-semibold ${upnl >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>
                    {upnl >= 0 ? '+' : ''}{upnl.toFixed(2)}
                  </span>
                </div>
              )
            })}
          </div>
        )}

        {/* ─── Описание / особенности ─── */}
        <div className="space-y-2">
          <div className="flex items-start gap-1">
            <div className="text-xs text-[var(--txt-secondary)] leading-relaxed">{t('bots.tagline')}</div>
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

        {/* ─── Таблица доходности ─── */}
        <div className="rounded-xl bg-[var(--bg)] ring-1 ring-[var(--border)]/60 p-3">
          <div className="flex items-center justify-between mb-2">
            <span className="text-2xs font-semibold text-[var(--txt-muted)] uppercase tracking-wider">{t('bots.yearly_title')}</span>
            <span className="text-[0.6rem] text-[var(--txt-muted)]">CAGR {BACKTEST_SUMMARY.cagr} · DD {BACKTEST_SUMMARY.dd}</span>
          </div>
          <div className="grid grid-cols-5 gap-1 text-center">
            {BACKTEST_YEARS.map(y => (
              <div key={y.year} className="rounded-md bg-[var(--surface-overlay)]/50 px-1 py-1.5">
                <div className="text-[0.6rem] text-[var(--txt-muted)]">{y.year}</div>
                <div className={`mono text-xs font-bold ${y.ret.startsWith('-') ? 'text-[var(--loss)]' : 'text-[var(--profit)]'}`}>{y.ret}</div>
              </div>
            ))}
          </div>
          <div className="text-[0.6rem] text-[var(--txt-muted)] mt-2 leading-snug">{t('bots.backtest_note')}</div>
        </div>

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
  const [confirmStopAll, setConfirmStopAll] = useState(false)
  const [sliderOpen, setSliderOpen] = useState(false)
  const [editingBot, setEditingBot] = useState(null) // 'momentum'
  const [saving, setSaving] = useState(false)
  const formRef = useRef(null)

  const [momLocal, setMomLocal] = useState(() => loadSavedConfig('bot_config_momentum', {
    symbols: ['BTC', 'ETH', 'SOL', 'BNB'],
    config: DEFAULT_MOM_CONFIG,
  }))

  const refreshStatus = useCallback(async () => {
    try {
      const m = await api.momentumStatus().catch(() => null)
      if (m) {
        setMomentumStatus(m)
        if (m.config) {
          setMomLocal(prev => ({
            symbols: m.config.symbols || prev.symbols,
            config: { ...prev.config, ...pickRotationParams(m.config) },
          }))
        }
      }
    } catch { /* ignore */ }
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

  const handleSave = (botData) => {
    setSaving(true)
    const payload = { symbols: botData.symbols, config: botData.config }
    if (editingBot === 'momentum') {
      setMomLocal(payload)
      localStorage.setItem('bot_config_momentum', JSON.stringify(payload))
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
          pnl={momentumStatus?.total_pnl || 0}
          trades={momentumStatus?.total_trades || 0}
          winRate={momentumStatus?.win_rate}
          sparklinePnl={momentumStatus?.total_pnl || 0}
          startedAt={momRunning ? momStartedAt : null}
          openPositions={momentumStatus?.open_positions || []}
          onToggle={momToggle}
          onEdit={() => { setEditingBot('momentum'); setSliderOpen(true) }}
          isGuest={isGuest}
          loading={momLoading}
          t={t}
        />
      </div>

      {/* ─── Проверено на Freqtrade ─── */}
      <div className="panel relative overflow-hidden">
        <div className="absolute inset-0 bg-gradient-to-br from-[var(--info)]/12 via-transparent to-[var(--profit)]/10 pointer-events-none" />
        <div className="relative p-5 space-y-4">
          <div className="flex items-center justify-between flex-wrap gap-2">
            <div className="flex items-center gap-3">
              <div className="w-11 h-11 rounded-xl bg-[var(--info)]/15 flex items-center justify-center ring-1 ring-[var(--info)]/30">
                <ShieldCheck size={22} className="text-[var(--info)]" />
              </div>
              <div>
                <div className="flex items-center gap-2">
                  <span className="text-base font-bold text-[var(--txt)]">{t('bots.ft_title')}</span>
                  <span className="px-2 py-0.5 rounded-full text-[0.6rem] font-bold uppercase tracking-wide bg-[var(--profit)]/15 text-[var(--profit)] ring-1 ring-[var(--profit)]/30">
                    {t('bots.ft_badge')}
                  </span>
                </div>
                <div className="text-2xs text-[var(--txt-muted)]">{t('bots.ft_subtitle')}</div>
              </div>
            </div>
            <div className="flex items-center gap-1.5 text-2xs text-[var(--profit)] font-semibold">
              <BadgeCheck size={14} />
              <span>{t('bots.ft_note')}</span>
            </div>
          </div>

          <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
            <div className="rounded-xl bg-[var(--bg)]/70 ring-1 ring-[var(--border)]/60 p-3">
              <div className="text-[0.62rem] text-[var(--txt-muted)] uppercase tracking-wide">{t('bots.ft_cagr')}</div>
              <div className="mono text-2xl font-bold text-[var(--profit)] mt-1">76.6%</div>
            </div>
            <div className="rounded-xl bg-[var(--bg)]/70 ring-1 ring-[var(--border)]/60 p-3">
              <div className="text-[0.62rem] text-[var(--txt-muted)] uppercase tracking-wide">{t('bots.ft_forward')}</div>
              <div className="mono text-2xl font-bold text-[var(--profit)] mt-1">+102%</div>
              <div className="text-[0.62rem] text-[var(--txt-muted)]">{t('bots.ft_forward_val')}</div>
            </div>
            <div className="rounded-xl bg-[var(--bg)]/70 ring-1 ring-[var(--border)]/60 p-3">
              <div className="text-[0.62rem] text-[var(--txt-muted)] uppercase tracking-wide">{t('bots.ft_winrate')}</div>
              <div className="mono text-2xl font-bold text-[var(--txt)] mt-1">~57%</div>
            </div>
            <div className="rounded-xl bg-[var(--bg)]/70 ring-1 ring-[var(--border)]/60 p-3">
              <div className="text-[0.62rem] text-[var(--txt-muted)] uppercase tracking-wide">{t('bots.ft_liq')}</div>
              <div className="mono text-2xl font-bold text-[var(--profit)] mt-1">0</div>
            </div>
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
            {[
              'bots.ft_check_wf',
              'bots.ft_check_lookahead',
              'bots.ft_check_fees',
              'bots.ft_check_funding',
            ].map(k => (
              <div key={k} className="flex items-start gap-2 text-2xs text-[var(--txt-secondary)]">
                <CheckCircle2 size={14} className="text-[var(--profit)] flex-shrink-0 mt-0.5" />
                <span>{t(k)}</span>
              </div>
            ))}
          </div>
        </div>
      </div>

      <SliderPanel
        open={sliderOpen}
        onClose={() => { setSliderOpen(false); setEditingBot(null) }}
        title={`${t('bots.edit')} Momentum`}
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
          symbols={momLocal.symbols}
          config={momLocal.config}
          onSave={handleSave}
        />
      </SliderPanel>

      <ConfirmDialog
        open={confirmStopAll}
        onClose={() => setConfirmStopAll(false)}
        onConfirm={async () => {
          try { await api.momentumStop() } catch {}
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

const BotConfigForm = forwardRef(function BotConfigForm({ botType, symbols, config, onSave }, ref) {
  const { t } = useTranslation()
  const PARAM_META = useMemo(() => getParamMeta(t), [t])

  const [form, setForm] = useState({
    symbols: symbols?.length ? [...symbols] : ['BTC', 'ETH', 'SOL', 'BNB'],
    config: { ...DEFAULT_MOM_CONFIG, ...config },
  })

  useEffect(() => {
    setForm({
      symbols: symbols?.length ? [...symbols] : ['BTC', 'ETH', 'SOL', 'BNB'],
      config: { ...DEFAULT_MOM_CONFIG, ...config },
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
        <TrendingUp size={14} className="text-[var(--info)]" />
        <div>
          <div className="text-xs font-semibold text-[var(--txt)]">
            {t('dash.momentum_bot')}
          </div>
          <div className="text-2xs text-[var(--txt-muted)]">
            momentum_rotation
          </div>
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
          {ROTATION_PARAMS.map(key => {
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
