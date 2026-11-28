import React, { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { Brain, Play, Square, Edit3, TrendingUp, Zap, Clock, RotateCcw,
  ShieldCheck, BadgeCheck, CheckCircle2, Award, FlaskConical, Bot,
  Link, Unlink, AlertTriangle, Wifi, WifiOff, Target
} from 'lucide-react'
import { api } from '../services/api'
import { Tip, StatusBadge, ConfirmDialog, Loader } from '../components/ui'
import { useTranslation } from '../hooks/useTranslation'

/** Stage-3: AI Discretionary + Live mirror only (legacy bots removed). */

const AI_SYMBOLS = ['BTC', 'ETH', 'SOL', 'XRP']

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

function NextTickCountdown({ nextTickAt, pollIntervalSec }) {
  const [remaining, setRemaining] = useState(null)
  const ref = useRef(null)

  useEffect(() => {
    if (!nextTickAt) { setRemaining(null); return }
    const tick = () => {
      const diff = Math.max(0, Math.floor((Date.parse(nextTickAt) - Date.now()) / 1000))
      setRemaining(diff)
    }
    tick()
    ref.current = setInterval(tick, 1000)
    return () => clearInterval(ref.current)
  }, [nextTickAt])

  if (remaining === null) return null
  const pct = pollIntervalSec ? Math.round(((pollIntervalSec - remaining) / pollIntervalSec) * 100) : 0

  return (
    <div className="flex items-center gap-2 text-[0.6rem] text-[var(--txt-muted)]">
      <Clock size={11} className="flex-shrink-0 opacity-60" />
      <span>След. проверка: <span className="mono font-semibold text-[var(--txt)]">{remaining}с</span></span>
      <div className="flex-1 h-1 rounded-full bg-[var(--bg)] overflow-hidden max-w-[60px]">
        <div className="h-full rounded-full bg-[var(--info)] transition-all duration-1000" style={{ width: `${Math.min(100, pct)}%` }} />
      </div>
    </div>
  )
}

function CompactSignals({ signals, t }) {
  if (!signals || signals.length === 0) return null
  const sideRu = (side) => side === 'long' ? 'лонг' : side === 'short' ? 'шорт' : (side || '—')
  const regimeRu = (r) => {
    const x = String(r || '').toLowerCase()
    if (x === 'bear') return 'медвежий'
    if (x === 'bull') return 'бычий'
    if (x === 'chop') return 'боковик'
    return r || '—'
  }
  return (
    <div className="space-y-1.5">
      <div className="flex items-center gap-1.5 text-[0.62rem] font-semibold text-[var(--txt-muted)] uppercase tracking-wider">
        <Target size={10} />
        <span>{t('bots.top_signals') || 'Лидер на вход'}</span>
      </div>
      <p className="text-[0.55rem] text-[var(--txt-muted)] leading-snug m-0">
        Ближе всех к сделке: совпадение (тренд) и готовность (фильтры).
      </p>
      <div className="grid grid-cols-1 gap-1.5">
        {(signals || []).slice(0, 1).map((s, i) => {
          const isLong = s.side === 'long'
          const scorePct = Math.round((s.score || 0) * 100)
          const alignPct = Math.round((s.align_score || 0) * 100)
          const fp = s.filters_passed ?? 10
          const ft = s.filters_total ?? 10
          const blockRu = s.blocked_reason_ru || (s.blocked_reason ? String(s.blocked_reason).replace(/_/g, ' ') : null)
          return (
            <div key={s.coin + i} className="p-1.5 rounded-lg bg-[var(--bg)] ring-1 ring-[var(--border)]/60 space-y-1">
              <div className="flex items-center gap-1.5 flex-wrap">
                <span className="text-[0.55rem] font-bold text-[var(--txt-muted)]">#{i + 1}</span>
                <span className="text-[0.7rem] font-semibold text-[var(--txt)] mono">{s.coin}</span>
                <span className={`px-1 py-px rounded text-[0.55rem] font-bold ${isLong ? 'bg-[var(--profit-dim)] text-[var(--profit)]' : 'bg-[var(--loss-dim)] text-[var(--loss)]'}`}>
                  {sideRu(s.side)}
                </span>
                <span className="text-[0.55rem] text-[var(--txt-muted)]">{regimeRu(s.regime)}</span>
                <div className="flex-1" />
                <span className="text-[0.55rem] text-[var(--txt-muted)]">Совп. <b className="text-[var(--txt)] mono">{alignPct}%</b></span>
                <span className="text-[0.55rem] text-[var(--txt-muted)]">Готов. <b className="text-[var(--txt)] mono">{scorePct}%</b></span>
                <span className="text-[0.5rem] text-[var(--txt-muted)]">{fp}/{ft}</span>
              </div>
              {blockRu ? (
                <div className="text-[0.55rem] text-[var(--warn)] leading-snug">Почему не входим: {blockRu}</div>
              ) : (
                <div className="text-[0.55rem] text-[var(--profit)] leading-snug">Фильтры ок — ждём подтверждение ИИ</div>
              )}
            </div>
          )
        })}
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

function LiveMirrorCard({
  connected, liveStatus, loading,
  liveKey, setLiveKey, liveSecret, setLiveSecret, livePass, setLivePass,
  liveCapital, setLiveCapital,
  onConnect, onDisconnect, isGuest, t,
}) {
  const [showForm, setShowForm] = useState(false)
  if (isGuest) return null
  const livePnl = Number(liveStatus?.total_pnl ?? 0)
  const liveUnrealized = Number(liveStatus?.unrealized_pnl ?? 0)
  const liveTrades = liveStatus?.lifetime_trades ?? 0
  const liveWinRate = liveStatus?.win_rate
  const liveEquity = Number(liveStatus?.equity ?? 0)
  const allocated = Number(liveStatus?.capital ?? liveCapital ?? 0)
  const liveOpen = liveStatus?.open_positions || []

  if (!connected) {
    return (
      <div className="panel flex flex-col border-dashed border-[var(--warn)]/40 bg-[var(--warn)]/5">
        <div className="px-4 py-3 border-b border-[var(--border)]">
          <div className="flex items-center gap-2">
            <WifiOff size={14} className="text-[var(--warn)]" />
            <span className="text-sm font-bold text-[var(--txt)]">LIVE Mirror</span>
            <span className="text-2xs px-1.5 py-0.5 rounded bg-[var(--warn)]/15 text-[var(--warn)] font-semibold">OFF</span>
          </div>
          <div className="text-2xs text-[var(--txt-muted)] mt-1">Подключите LIVE-аккаунт для зеркальной торговли</div>
        </div>
        <div className="p-4 space-y-3">
          {!showForm ? (
            <button className="btn btn-primary btn-sm w-full" onClick={() => setShowForm(true)}>
              <Link size={12} /> Подключить LIVE
            </button>
          ) : (
            <div className="space-y-2">
              <input type="text" placeholder="API Key" value={liveKey}
                onChange={e => setLiveKey(e.target.value)}
                className="w-full px-2.5 py-1.5 rounded-lg bg-[var(--bg)] border border-[var(--border)] text-xs mono"
              />
              <input type="password" placeholder="Secret Key" value={liveSecret}
                onChange={e => setLiveSecret(e.target.value)}
                className="w-full px-2.5 py-1.5 rounded-lg bg-[var(--bg)] border border-[var(--border)] text-xs mono"
              />
              <input type="password" placeholder="Passphrase" value={livePass}
                onChange={e => setLivePass(e.target.value)}
                className="w-full px-2.5 py-1.5 rounded-lg bg-[var(--bg)] border border-[var(--border)] text-xs mono"
              />
              <input type="number" min="10" step="1" placeholder="Капитал зеркала, USDT (мин. 10)"
                value={liveCapital}
                onChange={e => setLiveCapital(e.target.value)}
                className="w-full px-2.5 py-1.5 rounded-lg bg-[var(--bg)] border border-[var(--border)] text-xs mono"
              />
              <div className="text-2xs text-[var(--txt-muted)]">Сумма, которую бот может использовать на LIVE (не весь счёт)</div>
              <div className="flex gap-1.5">
                <button className="btn btn-ghost btn-sm flex-1" onClick={() => { setShowForm(false); setLiveKey(''); setLiveSecret(''); setLivePass(''); setLiveCapital('') }}>
                  Отмена
                </button>
                <button className="btn btn-primary btn-sm flex-1"
                  onClick={onConnect} disabled={loading || !liveKey || !liveSecret || !livePass || !(Number(liveCapital) >= 10)}>
                  {loading ? <Loader /> : <><Link size={11} /> Подключить</>}
                </button>
              </div>
              <div className="flex items-start gap-1.5 mt-1">
                <AlertTriangle size={11} className="text-[var(--warn)] flex-shrink-0 mt-0.5" />
                <span className="text-2xs text-[var(--txt-muted)] leading-snug">
                  Бот будет торговать на DEMO и LIVE одновременно. Закрытие/стоп на LIVE — зеркальное с DEMO.
                </span>
              </div>
            </div>
          )}
        </div>
      </div>
    )
  }

  return (
    <div className="panel flex flex-col border-[var(--profit)]/30 bg-[var(--profit)]/5">
      <div className="px-4 py-3 border-b border-[var(--border)]">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Wifi size={14} className="text-[var(--profit)]" />
            <span className="text-sm font-bold text-[var(--txt)]">LIVE Mirror</span>
            <span className="text-2xs px-1.5 py-0.5 rounded bg-[var(--profit)]/15 text-[var(--profit)] font-semibold">ON</span>
          </div>
          {!isGuest && (
            <button className="btn btn-ghost btn-sm text-[var(--loss)]" onClick={onDisconnect} disabled={loading}>
              {loading ? <Loader /> : <><Unlink size={11} /> Отключить</>}
            </button>
          )}
        </div>
      </div>
      <div className="p-4 space-y-3">
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
          <PerfTile label="Капитал" value={`$${allocated > 0 ? allocated.toFixed(0) : '—'}`} tone="neutral" />
          <PerfTile label="Equity" value={`$${liveEquity.toFixed(0)}`} tone="neutral" />
          <PerfTile label="PnL" value={`${livePnl >= 0 ? '+' : ''}${livePnl.toFixed(2)}`} tone={livePnl >= 0 ? 'profit' : 'loss'} />
          <PerfTile label="Нереализ." value={`${liveUnrealized >= 0 ? '+' : ''}${liveUnrealized.toFixed(2)}`} tone={liveUnrealized >= 0 ? 'profit' : 'loss'} />
          <PerfTile label="Сделок" value={liveTrades} />
          <PerfTile label="WR" value={liveWinRate != null ? `${liveWinRate}%` : '—'} />
        </div>
        {liveOpen.length > 0 && (
          <div className="space-y-1.5">
            <div className="text-2xs text-[var(--txt-muted)] font-medium">{t('dash.open_positions')} (LIVE)</div>
            {liveOpen.map((p, i) => {
              const isLong = p.side !== 'short'
              const upl = Number(p.upl ?? 0)
              const uplPct = Number(p.upl_ratio ?? 0) * 100
              const mark = Number(p.mark_px || 0)
              return (
                <div key={i} className="p-2 rounded-lg bg-[var(--bg)] border border-[var(--border)]">
                  <div className="flex items-center justify-between gap-2 mb-1.5">
                    <div className="flex items-center gap-1.5 min-w-0">
                      <span className={`px-1 py-0.5 rounded font-bold ${isLong ? 'bg-[var(--profit-dim)] text-[var(--profit)]' : 'bg-[var(--loss-dim)] text-[var(--loss)]'}`}>{isLong ? 'L' : 'S'}</span>
                      <span className="text-xs font-bold text-[var(--txt)]">{p.coin}</span>
                      {p.leverage ? <span className="text-2xs text-[var(--txt-muted)]">x{p.leverage}</span> : null}
                    </div>
                    <div className="text-right">
                      <div className={`mono text-2xs font-bold ${upl >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>
                        {upl >= 0 ? '+' : ''}{upl.toFixed(2)}
                      </div>
                      <div className={`text-2xs mono ${upl >= 0 ? 'text-[var(--profit)] opacity-70' : 'text-[var(--loss)] opacity-70'}`}>
                        {uplPct >= 0 ? '+' : ''}{uplPct.toFixed(2)}%
                      </div>
                    </div>
                  </div>
                  <div className="grid grid-cols-4 gap-1 text-2xs">
                    <div>
                      <div className="text-[var(--txt-muted)]">вх</div>
                      <div className="mono text-[var(--txt)]">{Number(p.entry_price).toFixed(4)}</div>
                    </div>
                    <div>
                      <div className="text-[var(--txt-muted)]">сейчас</div>
                      <div className="mono text-[var(--txt)]">{mark > 0 ? mark.toFixed(4) : '—'}</div>
                    </div>
                    <div>
                      <div className="text-[var(--txt-muted)]">SL</div>
                      <div className="mono text-[var(--loss)]">{Number(p.stop_price).toFixed(4)}</div>
                    </div>
                    <div>
                      <div className="text-[var(--txt-muted)]">TP</div>
                      <div className="mono text-[var(--profit)]">{Number(p.take_price).toFixed(4)}</div>
                    </div>
                  </div>
                </div>
              )
            })}
          </div>
        )}
      </div>
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
  capitalValue, onCapitalChange, showCapital,
  nextTickAt, pollIntervalSec, topSignals,
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
          {statusMode === 'live' && nextTickAt && (
            <div className="mt-2">
              <NextTickCountdown nextTickAt={nextTickAt} pollIntervalSec={pollIntervalSec} />
            </div>
          )}
          {topSignals && topSignals.length > 0 && (
            <div className="mt-2">
              <CompactSignals signals={topSignals} t={t} />
            </div>
          )}
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
            
            {showCapital && statusMode !== 'live' && (
              <div className="flex items-center gap-2 mr-auto min-w-0">
                <label className="text-[0.65rem] text-[var(--txt-muted)] whitespace-nowrap">Сумма, $</label>
                <input
                  type="number"
                  min={100}
                  step={100}
                  value={capitalValue ?? ''}
                  onChange={(e) => onCapitalChange?.(Number(e.target.value))}
                  className="w-28 px-2 py-1 rounded-lg bg-[var(--bg)] border border-[var(--border)] text-xs mono"
                  title="Капитал для торговли бота в Live"
                />
              </div>
            )}
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

function KV({ label, value, tone = 'default' }) {
  const color = tone === 'profit' ? 'text-[var(--profit)]' : tone === 'muted' ? 'text-[var(--txt-muted)]' : 'text-[var(--txt)]'
  return (
    <div className="flex items-center justify-between gap-2">
      <span className="text-[var(--txt-muted)]">{label}</span>
      <span className={`mono font-semibold ${color}`}>{value}</span>
    </div>
  )
}

/** Paper daily trend book (ADMIN). Read-only module — no start/stop here. */
function TrendPortfolioCard({ status, positions = [], trades = [], onRunCycle, loading, isGuest, t }) {
  if (!status) return null
  const configRejected = !!status.reason
  const enabled = status.enabled !== false && !configRejected
  const running = !!status.running
  const eq = Number(status.equity ?? status.start_equity ?? 0)
  const start = Number(status.start_equity ?? 0)
  const retPct = status.return_pct != null ? Number(status.return_pct) : (start ? (eq / start - 1) * 100 : 0)
  const pnl = Number(status.realized_pnl ?? (eq - start))
  const tradeCount = status.trades ?? 0
  const wr = status.win_rate_pct
  const riskPct = status.risk_per_trade_pct
  const gate = status.regime_symbol ? `${status.regime_symbol} · ${status.regime_ma}d` : 'выкл'
  const venue = status.source === 'binance_vision'
    ? 'Binance Vision · UTC'
    : status.source === 'binance' ? 'Binance · UTC' : 'OKX · UTC+8'
  const fallback = status.sources_used
    ? Object.entries(status.sources_used).find(([v]) => v !== status.source)
    : null
  const lastBar = status.last_bar_ts ? new Date(Number(status.last_bar_ts)).toLocaleDateString() : '—'
  const lastErr = status.last_error

  return (
    <div className="panel !overflow-visible flex flex-col transition-colors hover:border-[var(--border-hover)]">
      <div className="relative px-4 py-3.5 border-b border-[var(--border)] bg-gradient-to-br from-[var(--profit-dim)] via-transparent to-transparent rounded-t-[var(--radius-lg)]">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-11 h-11 rounded-xl bg-[var(--bg)]/70 ring-1 ring-[var(--border)] flex items-center justify-center shadow-sm">
              <TrendingUp size={22} className="text-[var(--profit)]" />
            </div>
            <div>
              <div className="flex items-center gap-2 flex-wrap">
                <span className="text-base font-bold text-[var(--txt)]">Daily Trend Portfolio</span>
                <span className="text-[0.6rem] font-bold mono px-1.5 py-0.5 rounded-md bg-[var(--info)]/15 text-[var(--info)]">1D</span>
                <span className="text-[0.6rem] font-bold mono px-1.5 py-0.5 rounded-md bg-amber-500/15 text-amber-400">PAPER</span>
              </div>
              <div className="text-2xs text-[var(--txt-muted)] mono">trend_portfolio · long-only Donchian</div>
            </div>
          </div>
          <StatusBadge
            mode={running ? 'live' : 'stopped'}
            label={configRejected ? 'ошибка конфига' : !enabled ? 'выключен' : running ? t('bots.status_running') : t('bots.status_stopped')}
          />
        </div>
      </div>

      <div className="p-4 space-y-4 flex flex-col flex-1">
        {configRejected && (
          <div className="flex items-center gap-2 text-2xs text-[var(--loss)]">
            <AlertTriangle size={12} />
            <span>Конфиг отклонён: {status.reason}</span>
          </div>
        )}

        {!configRejected && !enabled && (
          <div className="text-2xs text-[var(--txt-muted)] leading-snug p-2 rounded-lg bg-[var(--bg)] ring-1 ring-[var(--border)]/60">
            Модуль выключен (<span className="mono">TREND_PORTFOLIO_ENABLE=0</span>). Кнопки старта здесь нет — бот запускается только переменной окружения на сервере, чтобы его нельзя было включить случайно.
          </div>
        )}

        <div>
          <div className="flex items-center justify-between mb-2">
            <span className="text-2xs font-semibold text-[var(--txt-muted)] uppercase tracking-wider">Бумажный результат</span>
            {running && (
              <div className="flex items-center gap-1.5 text-2xs text-[var(--txt-muted)]">
                <Clock size={11} /><span>бары до {lastBar}</span>
              </div>
            )}
          </div>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
            <PerfTile label="Капитал" value={`$${eq.toFixed(2)}`} tone={pnl >= 0 ? 'profit' : 'loss'} />
            <PerfTile label="Доход" value={`${retPct >= 0 ? '+' : ''}${retPct.toFixed(2)}%`} tone={retPct >= 0 ? 'profit' : 'loss'} />
            <PerfTile label="Сделок" value={tradeCount} />
            <PerfTile label="Win rate" value={wr != null ? `${wr}%` : '—'} />
          </div>
          {running && status.live_since > 0 && (
            <div className="flex items-center justify-between gap-2 mt-2 text-2xs text-[var(--txt-muted)]">
              <span>Форвард с {new Date(Number(status.live_since)).toLocaleDateString()}</span>
              <span className={`mono font-semibold ${Number(status.forward_return_pct) >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>
                {status.forward_days} дн · {Number(status.forward_return_pct) >= 0 ? '+' : ''}{status.forward_return_pct}%
              </span>
            </div>
          )}
        </div>

        <div className="grid grid-cols-2 gap-x-4 gap-y-1.5">
          <KV label="Вселенная" value={status.stale_coins ? `${(status.universe_size ?? 0) - status.stale_coins} из ${status.universe_size}` : `${status.universe_size ?? '—'} монет`} />
          <KV label="Риск/сделку" value={riskPct != null ? `${Number(riskPct).toFixed(2)}%` : '—'} />
          <KV label="Вход / выход" value={`${status.entry_lookback}/${status.exit_lookback} дн`} />
          <KV label="Трейл" value={`${status.chandelier_mult}×ATR`} />
          <KV label="Гейт режима" value={gate} tone={status.regime_symbol ? 'profit' : 'muted'} />
          <KV label="Позиций" value={positions.length} />
          <KV
            label="Данные"
            value={venue}
            tone={status.source !== 'okx' ? 'profit' : 'muted'}
          />
          <KV
            label="Фолбэк"
            value={fallback ? `${fallback[0]} · ${fallback[1]}` : '—'}
            tone={fallback ? 'loss' : 'muted'}
          />
        </div>

        {status.mirror && status.mirror.mode !== 'off' && (
          <div className="rounded-lg bg-[var(--bg)] ring-1 ring-[var(--border)]/60 p-2 space-y-1.5">
            <div className="flex items-center justify-between gap-2 text-2xs">
              <span className="font-semibold text-[var(--txt-muted)] uppercase tracking-wider">
                Зеркало · {status.mirror.mode}
              </span>
              <span className="mono text-[var(--info)]">ордера не отправляются</span>
            </div>
            <div className="grid grid-cols-2 gap-x-4 gap-y-1">
              <KV
                label="OKX-инструменты"
                value={`${status.mirror.instruments ?? 0} из ${status.mirror.universe ?? 0}`}
                tone={(status.mirror.instruments ?? 0) < (status.mirror.universe ?? 0) ? 'loss' : 'profit'}
              />
              <KV label="Планов" value={`${status.mirror.opens ?? 0} откр · ${status.mirror.closes ?? 0} закр`} />
              <KV
                label="Пропусков"
                value={status.mirror.skips ?? 0}
                tone={status.mirror.skips ? 'loss' : 'muted'}
              />
              <KV label="Зеркальный notional" value={`$${Number(status.mirror.open_notional || 0).toFixed(0)}`} />
            </div>
            {status.mirror.orders?.[0] && (
              <div className="text-[0.6rem] mono text-[var(--txt-muted)] truncate">
                последний план: {status.mirror.orders[0].kind} {status.mirror.orders[0].coin} ·{' '}
                {status.mirror.orders[0].inst_id} · {status.mirror.orders[0].side}{' '}
                {status.mirror.orders[0].sz} · ${status.mirror.orders[0].notional}
              </div>
            )}
          </div>
        )}

        {positions.length > 0 && (
          <div className="space-y-1">
            <div className="text-2xs text-[var(--txt-muted)] font-medium">Открытые позиции (бумага)</div>
            {positions.map((p, i) => {
              const upnl = Number(p.unrealized || 0)
              const isLong = p.side !== -1 && p.side !== 'short'
              return (
                <div key={`${p.coin}-${i}`} className="flex items-center justify-between gap-2 text-2xs p-1.5 rounded bg-[var(--bg)]">
                  <div className="flex items-center gap-1.5 flex-shrink-0">
                    <span className={`px-1 py-0.5 rounded font-bold ${isLong ? 'bg-[var(--profit-dim)] text-[var(--profit)]' : 'bg-[var(--loss-dim)] text-[var(--loss)]'}`}>{isLong ? 'L' : 'S'}</span>
                    <span className="text-[var(--txt)] font-medium">{p.coin}</span>
                  </div>
                  <div className="flex items-center gap-2 min-w-0">
                    {p.entry != null && <span className="mono text-[0.6rem] text-[var(--txt-muted)]">вх {Number(p.entry).toPrecision(5)}</span>}
                    {p.stop != null && <span className="mono text-[0.6rem] text-[var(--txt-muted)]">стоп {Number(p.stop).toPrecision(5)}</span>}
                    <span className={`mono font-semibold flex-shrink-0 ${upnl >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>
                      {upnl >= 0 ? '+' : ''}{upnl.toFixed(2)}
                    </span>
                  </div>
                </div>
              )
            })}
          </div>
        )}

        {trades.length > 0 && (
          <div className="space-y-1">
            <div className="flex items-center justify-between">
              <span className="text-2xs text-[var(--txt-muted)] font-medium">Последние сделки</span>
              <span className="text-[0.6rem] text-[var(--txt-muted)] mono">{trades.length} из {tradeCount}</span>
            </div>
            <div className="max-h-52 overflow-y-auto space-y-1 pr-0.5">
              {trades.map((tr, i) => {
                const p = Number(tr.pnl || 0)
                const pct = Number(tr.pnl_pct || 0)
                const isLong = tr.side !== -1 && tr.side !== 'short'
                const exitD = tr.exit_ts ? new Date(Number(tr.exit_ts)).toLocaleDateString() : '—'
                const reason = tr.reason === 'stop' ? 'стоп' : tr.reason === 'trail' ? 'трейл' : tr.reason || ''
                return (
                  <div
                    key={`${tr.coin}-${tr.exit_ts}-${i}`}
                    className="flex items-center justify-between gap-2 text-2xs p-1.5 rounded bg-[var(--bg)] border border-[var(--border)]/50"
                    title={`${tr.entry} → ${tr.exit}${tr.r_multiple != null ? ` · ${tr.r_multiple}R` : ''}`}
                  >
                    <div className="flex items-center gap-1.5 flex-shrink-0 min-w-0">
                      <span className={`px-1 py-0.5 rounded font-bold ${isLong ? 'bg-[var(--profit-dim)] text-[var(--profit)]' : 'bg-[var(--loss-dim)] text-[var(--loss)]'}`}>{isLong ? 'L' : 'S'}</span>
                      <span className="text-[var(--txt)] font-medium">{tr.coin}</span>
                      <span className="mono text-[0.6rem] text-[var(--txt-muted)] truncate">{reason}</span>
                    </div>
                    <div className="flex items-center gap-2 flex-shrink-0">
                      <span className="mono text-[0.6rem] text-[var(--txt-muted)]">{exitD}</span>
                      <span className={`mono font-semibold ${p >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>
                        {p >= 0 ? '+' : ''}{p.toFixed(2)}
                      </span>
                      <span className={`mono text-[0.6rem] w-10 text-right ${pct >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>
                        {pct >= 0 ? '+' : ''}{pct.toFixed(1)}%
                      </span>
                    </div>
                  </div>
                )
              })}
            </div>
          </div>
        )}

        {lastErr && (
          <div className="flex items-start gap-2 text-[0.6rem] text-[var(--warn)]">
            <AlertTriangle size={11} className="mt-px flex-shrink-0" />
            <span className="break-words">Последняя ошибка цикла: {lastErr}</span>
          </div>
        )}

        {status.data_note && (
          <div className="flex items-start gap-2 text-[0.6rem] text-[var(--warn)]">
            <AlertTriangle size={11} className="mt-px flex-shrink-0" />
            <span className="break-words">{status.data_note}</span>
          </div>
        )}

        <div className="text-[0.6rem] text-[var(--txt-muted)] leading-snug">
          Бумажный портфель на публичных дневных свечах, ордера не выставляются. Гейт BTC200 + риск 0.32 → бэктест ≈ +29.7% CAGR при просадке ≈ 31% (mark-to-market, 0.16%/сторона, 7.9 лет).
        </div>

        {!isGuest && enabled && (
          <div className="flex gap-1.5 pt-1 mt-auto">
            <button className="btn btn-ghost btn-sm flex-1" onClick={onRunCycle} disabled={loading}>
              {loading ? <Loader /> : <><RotateCcw size={12} /> Прогнать цикл</>}
            </button>
          </div>
        )}
      </div>
    </div>
  )
}

/** Combined paper equity sparkline for the core portfolio. */
function EquitySparkline({ points = [] }) {
  const eq = (points || []).map((p) => Number(p.equity || 0)).filter((v) => v > 0)
  if (eq.length < 2) return null
  const w = 320, h = 44, pad = 2
  const min = Math.min(...eq), max = Math.max(...eq)
  const range = max - min || 1
  const step = (w - pad * 2) / (eq.length - 1)
  const coords = eq.map((v, i) => [pad + i * step, pad + (1 - (v - min) / range) * (h - pad * 2)])
  const line = coords.map(([x, y], i) => `${i ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)}`).join(' ')
  const area = `${line} L${coords[coords.length - 1][0].toFixed(1)},${h - pad} L${pad},${h - pad} Z`
  const up = eq[eq.length - 1] >= eq[0]
  const color = up ? 'var(--profit)' : 'var(--loss)'
  return (
    <svg viewBox={`0 0 ${w} ${h}`} className="w-full h-11 block" preserveAspectRatio="none">
      <path d={area} fill={color} opacity="0.12" />
      <path d={line} fill="none" stroke={color} strokeWidth="1.5" vectorEffect="non-scaling-stroke" />
    </svg>
  )
}

/** Core portfolio: daily book + 6h book, two independent paper accounts (ADMIN). */
function CorePortfolioCard({ status, positions = [], trades = [], onRunCycle, loading, isGuest, t }) {
  if (!status) return null
  const rejected = !!status.reason
  const enabled = status.enabled !== false && !rejected
  const running = !!status.running
  const c = status.combined || {}
  const blocks = status.blocks || {}
  const d = blocks.daily || {}
  const h = blocks.h6 || {}
  const eq = Number(c.equity ?? 0)
  const start = Number(c.start_equity ?? 0)
  const ret = Number(c.return_pct ?? 0)
  const pnl = Number(c.realized_pnl ?? (eq - start))
  const curve = c.curve || {}
  const venues = [...new Set([status.daily_venue, status.h6_venue].filter(Boolean))]
  const venueLabel = venues.length ? venues.join(' + ') : '—'
  const gate = status.regime_symbol ? `${status.regime_symbol} · ${status.regime_ma_days}d` : 'выкл'
  const dailyPos = positions.filter((p) => p.block === 'daily')
  const h6Pos = positions.filter((p) => p.block === '6h')

  const blockRow = (label, b, tone) => (
    <div className="rounded-lg bg-[var(--bg)] ring-1 ring-[var(--border)]/60 p-2 space-y-1">
      <div className="flex items-center justify-between">
        <span className="text-2xs font-semibold text-[var(--txt)]">{label}</span>
        <span className={`mono text-2xs ${Number(b.return_pct ?? 0) >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>
          {Number(b.return_pct ?? 0) >= 0 ? '+' : ''}{Number(b.return_pct ?? 0).toFixed(2)}%
        </span>
      </div>
      <div className="mono text-xs font-bold text-[var(--txt)]">${Number(b.equity ?? 0).toFixed(2)}</div>
      <div className="flex items-center justify-between text-[0.6rem] text-[var(--txt-muted)]">
        <span>{b.trades ?? 0} сдел.</span>
        <span>{b.open_positions ?? 0} поз.</span>
        <span className={tone}>WR {b.win_rate_pct != null ? `${b.win_rate_pct}%` : '—'}</span>
      </div>
    </div>
  )

  return (
    <div className="panel !overflow-visible flex flex-col transition-colors hover:border-[var(--border-hover)]">
      <div className="relative px-4 py-3.5 border-b border-[var(--border)] bg-gradient-to-br from-[var(--info-dim,rgba(59,130,246,0.12))] via-transparent to-transparent rounded-t-[var(--radius-lg)]">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-11 h-11 rounded-xl bg-[var(--bg)]/70 ring-1 ring-[var(--border)] flex items-center justify-center shadow-sm">
              <Target size={22} className="text-[var(--info)]" />
            </div>
            <div>
              <div className="flex items-center gap-2 flex-wrap">
                <span className="text-base font-bold text-[var(--txt)]">Core Portfolio</span>
                <span className="text-[0.6rem] font-bold mono px-1.5 py-0.5 rounded-md bg-[var(--info)]/15 text-[var(--info)]">1D + 6H</span>
                <span className="text-[0.6rem] font-bold mono px-1.5 py-0.5 rounded-md bg-amber-500/15 text-amber-400">PAPER</span>
              </div>
              <div className="text-2xs text-[var(--txt-muted)] mono">core_portfolio · 50/50 two books</div>
            </div>
          </div>
          <StatusBadge
            mode={running ? 'live' : 'stopped'}
            label={rejected ? 'ошибка конфига' : !enabled ? 'выключен' : running ? t('bots.status_running') : t('bots.status_stopped')}
          />
        </div>
      </div>

      <div className="p-4 space-y-4 flex flex-col flex-1">
        {rejected && (
          <div className="flex items-center gap-2 text-2xs text-[var(--loss)]">
            <AlertTriangle size={12} />
            <span>Конфиг отклонён: {status.reason}</span>
          </div>
        )}

        {!rejected && !enabled && (
          <div className="text-2xs text-[var(--txt-muted)] leading-snug p-2 rounded-lg bg-[var(--bg)] ring-1 ring-[var(--border)]/60">
            Модуль выключен (<span className="mono">CORE_PORTFOLIO_ENABLE=0</span>). Запуск только переменной окружения на сервере.
          </div>
        )}

        <div>
          <div className="flex items-center justify-between mb-2">
            <span className="text-2xs font-semibold text-[var(--txt-muted)] uppercase tracking-wider">Совокупный бумажный результат</span>
            {running && (
              <div className="flex items-center gap-1.5 text-2xs text-[var(--txt-muted)]">
                <Clock size={11} /><span>циклов {status.cycles ?? 0}</span>
              </div>
            )}
          </div>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
            <PerfTile label="Капитал" value={`$${eq.toFixed(2)}`} tone={pnl >= 0 ? 'profit' : 'loss'} />
            <PerfTile label="Доход" value={`${ret >= 0 ? '+' : ''}${ret.toFixed(2)}%`} tone={ret >= 0 ? 'profit' : 'loss'} />
            <PerfTile label="Сделок" value={c.trades ?? 0} />
            <PerfTile label="Win rate" value={c.win_rate_pct != null ? `${c.win_rate_pct}%` : '—'} />
          </div>
          {curve.days > 1 && (
            <div className="mt-2">
              <EquitySparkline points={status.equity_curve || []} />
              <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-[0.6rem] text-[var(--txt-muted)] mt-1">
                <span>CAGR <span className="mono text-[var(--txt)]">{curve.cagr_pct >= 0 ? '+' : ''}{curve.cagr_pct}%</span></span>
                <span>DD <span className="mono text-[var(--txt)]">{curve.max_dd_pct}%</span></span>
                <span>Sharpe <span className="mono text-[var(--txt)]">{curve.sharpe}</span></span>
                <span>{curve.days} дн.</span>
              </div>
            </div>
          )}
        </div>

        <div className="grid grid-cols-2 gap-2">
          {blockRow('Дневная книга', d, 'text-[var(--txt-muted)]')}
          {blockRow('6h блок', h, 'text-[var(--txt-muted)]')}
        </div>

        <div className="grid grid-cols-2 gap-x-4 gap-y-1.5">
          <KV label="Вселенная" value={`${status.universe_size ?? '—'} монет`} />
          <KV label="Веса" value={`${Math.round(Number(c.daily_weight ?? 0.5) * 100)}/${Math.round(Number(c.h6_weight ?? 0.5) * 100)}`} />
          <KV label="Гейт режима" value={gate} tone={status.regime_symbol ? 'profit' : 'muted'} />
          <KV label="Позиций" value={positions.length} />
          <KV label="Данные" value={venueLabel} tone={venues.length ? 'profit' : 'muted'} />
          <KV
            label="Фолбэк"
            value={status.sources_used && Object.keys(status.sources_used).length > 1
              ? Object.entries(status.sources_used).map(([k, v]) => `${k}:${v}`).join(' ')
              : '—'}
            tone={status.sources_used && Object.keys(status.sources_used).length > 1 ? 'loss' : 'muted'}
          />
          <KV label="6H вход/выход" value={`${status.h6?.entry ?? '—'}/${status.h6?.exit ?? '—'} баров`} />
          <KV label="6H трейл" value={`${status.h6?.chandelier ?? '—'}×ATR`} />
        </div>

        {positions.length > 0 && (
          <div className="space-y-1">
            <div className="text-2xs text-[var(--txt-muted)] font-medium">
              Открытые позиции (бумага) · 1D {dailyPos.length} · 6H {h6Pos.length}
            </div>
            {positions.slice(0, 12).map((p, i) => {
              const upnl = Number(p.unrealized || 0)
              return (
                <div key={`${p.block}-${p.coin}-${i}`} className="flex items-center justify-between gap-2 text-2xs p-1.5 rounded bg-[var(--bg)]">
                  <div className="flex items-center gap-1.5 flex-shrink-0">
                    <span className="px-1 py-0.5 rounded font-bold bg-[var(--profit-dim)] text-[var(--profit)]">L</span>
                    <span className="text-[var(--txt)] font-medium">{p.coin}</span>
                    <span className="mono text-[0.55rem] text-[var(--txt-muted)] uppercase">{p.block}</span>
                  </div>
                  <div className="flex items-center gap-2 min-w-0">
                    {p.entry != null && <span className="mono text-[0.6rem] text-[var(--txt-muted)]">вх {Number(p.entry).toPrecision(5)}</span>}
                    {p.stop != null && <span className="mono text-[0.6rem] text-[var(--txt-muted)]">стоп {Number(p.stop).toPrecision(5)}</span>}
                    <span className={`mono font-semibold flex-shrink-0 ${upnl >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>
                      {upnl >= 0 ? '+' : ''}{upnl.toFixed(2)}
                    </span>
                  </div>
                </div>
              )
            })}
          </div>
        )}

        {trades.length > 0 && (
          <div className="space-y-1">
            <div className="flex items-center justify-between">
              <span className="text-2xs text-[var(--txt-muted)] font-medium">Последние сделки</span>
              <span className="text-[0.6rem] text-[var(--txt-muted)] mono">{trades.length} из {c.trades ?? 0}</span>
            </div>
            <div className="max-h-52 overflow-y-auto space-y-1 pr-0.5">
              {trades.slice(0, 12).map((tr, i) => {
                const p = Number(tr.pnl || 0)
                const pct = Number(tr.pnl_pct || 0)
                const exitD = tr.exit_ts ? new Date(Number(tr.exit_ts)).toLocaleDateString() : '—'
                const reason = tr.reason === 'stop' ? 'стоп' : tr.reason === 'signal' ? 'сигнал' : tr.reason || ''
                return (
                  <div
                    key={`${tr.block}-${tr.coin}-${tr.exit_ts}-${i}`}
                    className="flex items-center justify-between gap-2 text-2xs p-1.5 rounded bg-[var(--bg)] border border-[var(--border)]/50"
                    title={`${tr.entry} → ${tr.exit}`}
                  >
                    <div className="flex items-center gap-1.5 flex-shrink-0 min-w-0">
                      <span className="px-1 py-0.5 rounded font-bold bg-[var(--profit-dim)] text-[var(--profit)]">L</span>
                      <span className="text-[var(--txt)] font-medium">{tr.coin}</span>
                      <span className="mono text-[0.55rem] text-[var(--txt-muted)] uppercase">{tr.block}</span>
                      <span className="mono text-[0.6rem] text-[var(--txt-muted)] truncate">{reason}</span>
                    </div>
                    <div className="flex items-center gap-2 flex-shrink-0">
                      <span className="mono text-[0.6rem] text-[var(--txt-muted)]">{exitD}</span>
                      <span className={`mono font-semibold ${p >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>
                        {p >= 0 ? '+' : ''}{p.toFixed(2)}
                      </span>
                      <span className={`mono text-[0.6rem] w-10 text-right ${pct >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>
                        {pct >= 0 ? '+' : ''}{pct.toFixed(1)}%
                      </span>
                    </div>
                  </div>
                )
              })}
            </div>
          </div>
        )}

        {status.last_error && (
          <div className="flex items-start gap-2 text-[0.6rem] text-[var(--warn)]">
            <AlertTriangle size={11} className="mt-px flex-shrink-0" />
            <span className="break-words">Последняя ошибка цикла: {status.last_error}</span>
          </div>
        )}

        {status.data_note && (
          <div className="flex items-start gap-2 text-[0.6rem] text-[var(--warn)]">
            <AlertTriangle size={11} className="mt-px flex-shrink-0" />
            <span className="break-words">{status.data_note}</span>
          </div>
        )}

        <div className="text-[0.6rem] text-[var(--txt-muted)] leading-snug">
          Две независимые бумажные книги по 50% капитала: дневной Donchian 40/20 + 6h Donchian 80/40, обе long-only с гейтом BTC200.
          Бэктест 2021-06 → 2026-11 (издержки 0.16%/сторона): ≈ +21% CAGR, DD ≈ 15%, Sharpe ≈ 1.4. Ордера не выставляются.
        </div>

        {!isGuest && enabled && (
          <div className="flex gap-1.5 pt-1 mt-auto">
            <button className="btn btn-ghost btn-sm flex-1" onClick={onRunCycle} disabled={loading}>
              {loading ? <Loader /> : <><RotateCcw size={12} /> Прогнать цикл</>}
            </button>
          </div>
        )}
      </div>
    </div>
  )
}

export default function BotsPage({ connected, isGuest, isAdmin }) {
  const { t } = useTranslation()
  const [aiStatus, setAiStatus] = useState(null)
  const [demoPnl, setDemoPnl] = useState(null)
  const [aiLoading, setAiLoading] = useState(false)
  const [liveStatus, setLiveStatus] = useState(null)
  const [liveLoading, setLiveLoading] = useState(false)
  const [liveKey, setLiveKey] = useState('')
  const [liveCapital, setLiveCapital] = useState('')
  const [liveSecret, setLiveSecret] = useState('')
  const [livePass, setLivePass] = useState('')
  const [aiCapital, setAiCapital] = useState(() => {
    try {
      const v = Number(localStorage.getItem('ai_live_capital') || '10000')
      return Number.isFinite(v) && v >= 100 ? v : 10000
    } catch {
      return 10000
    }
  })
  const [apiAlive, setApiAlive] = useState(true)
  const [confirmStopAll, setConfirmStopAll] = useState(false)
  const [trendStatus, setTrendStatus] = useState(null)
  const [trendPositions, setTrendPositions] = useState([])
  const [trendTrades, setTrendTrades] = useState([])
  const [trendLoading, setTrendLoading] = useState(false)
  const [coreStatus, setCoreStatus] = useState(null)
  const [corePositions, setCorePositions] = useState([])
  const [coreTrades, setCoreTrades] = useState([])
  const [coreLoading, setCoreLoading] = useState(false)

  const refreshStatus = useCallback(async () => {
    // One public payload + status (client-cached) instead of separate heavy /api/pnl
    const [a, ls, dash, tr, trp, trt, cr, crp, crt] = await Promise.all([
      api.aiStatus().catch(() => null),
      api.liveStatus().catch(() => null),
      api.meDashboard().catch(() => null),
      isAdmin ? api.trendStatus().catch(() => null) : Promise.resolve(null),
      isAdmin ? api.trendPositions().catch(() => null) : Promise.resolve(null),
      isAdmin ? api.trendTrades(20).catch(() => null) : Promise.resolve(null),
      isAdmin ? api.coreStatus().catch(() => null) : Promise.resolve(null),
      isAdmin ? api.corePositions().catch(() => null) : Promise.resolve(null),
      isAdmin ? api.coreTrades(20).catch(() => null) : Promise.resolve(null),
    ])
    if (a) setAiStatus(a)
    if (ls) setLiveStatus(ls)
    if (tr) setTrendStatus(tr)
    if (trp) setTrendPositions(trp.positions || [])
    if (trt) setTrendTrades(trt.trades || [])
    if (cr) setCoreStatus(cr)
    if (crp) setCorePositions(crp.positions || [])
    if (crt) setCoreTrades(crt.trades || [])
    const pnl = dash?.pnl
    if (pnl && !pnl.detail && (pnl.total != null || pnl.per_bot)) {
      setDemoPnl(pnl)
    } else if (a && a.lifetime_pnl != null) {
      setDemoPnl({ total: a.lifetime_pnl, per_bot: { 'AI Discretionary 1H': a.lifetime_pnl } })
    }
    setApiAlive(!!a)
  }, [isAdmin])

  const trendCycle = async () => {
    setTrendLoading(true)
    try {
      await api.trendCycle()
      await refreshStatus()
    } catch (e) { alert(e?.message || String(e)) }
    setTrendLoading(false)
  }

  const coreCycle = async () => {
    setCoreLoading(true)
    try {
      await api.coreCycle()
      await refreshStatus()
    } catch (e) { alert(e?.message || String(e)) }
    setCoreLoading(false)
  }

  useEffect(() => {
    refreshStatus()
    const id = setInterval(refreshStatus, 30000)
    return () => clearInterval(id)
  }, [connected, refreshStatus])

  const aiToggle = async () => {
    setAiLoading(true)
    try {
      if (aiStatus?.running) {
        await api.aiStop()
      } else {
        const cap = Math.max(100, Number(aiCapital) || 10000)
        try { localStorage.setItem('ai_live_capital', String(cap)) } catch {}
        // Do NOT override strategy knobs here: provider/symbols/max_positions
        // come from env + saved DEMO config, otherwise the same bot behaves
        // differently depending on which button started it.
        await api.aiStart({ capital: cap, execute: true })
      }
      await refreshStatus()
    } catch (e) { alert(e.message) }
    setAiLoading(false)
  }

  const liveConnect = async () => {
    const cap = Number(liveCapital)
    if (!(cap >= 10)) {
      alert('Укажите капитал зеркала (мин. $10)')
      return
    }
    if (!liveKey?.trim() || !liveSecret?.trim() || !livePass?.trim()) {
      alert('Заполните API Key, Secret и Passphrase')
      return
    }
    setLiveLoading(true)
    try {
      const res = await api.liveConnect({
        key: liveKey.trim(),
        secret: liveSecret.trim(),
        passphrase: livePass.trim(),
        confirm: 'LIVE',
        capital: cap,
      })
      setLiveKey(''); setLiveSecret(''); setLivePass(''); setLiveCapital('')
      // Optimistic UI — status may lag one poll
      setLiveStatus((prev) => ({
        ...(prev || {}),
        connected: true,
        enabled: true,
        capital: res?.capital ?? cap,
        equity: res?.equity ?? (prev?.equity || 0),
      }))
      await refreshStatus()
    } catch (e) {
      alert(e?.message || String(e) || 'Не удалось подключить LIVE')
    } finally {
      setLiveLoading(false)
    }
  }

    const liveDisconnect = async () => {
    setLiveLoading(true)
    try {
      await api.liveDisconnect()
      setLiveStatus({
        connected: false,
        enabled: false,
        equity: 0,
        capital: 0,
        total_pnl: 0,
        unrealized_pnl: 0,
        open_positions: [],
        lifetime_trades: 0,
      })
      await refreshStatus()
    } catch (e) { alert(e.message) }
    setLiveLoading(false)
  }

const aiRunning = !!aiStatus?.running
  const aiStartedAt = aiStatus?.started_at ? Date.parse(aiStatus.started_at) : null
  const coins = aiStatus?.symbols || aiStatus?.config?.symbols || AI_SYMBOLS

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
          id="ai"
          name="AI Discretionary 1H"
          stratId="ai_strategy"
          version={aiStatus?.version}
          icon={Brain}
          accentDim="bg-[var(--accent-dim)]"
          accentTxt="text-[var(--accent)]"
          statusMode={aiRunning ? 'live' : 'stopped'}
          statusLabel={aiRunning ? t('bots.status_running') : t('bots.status_stopped')}
          coins={coins}
          description={
            aiStatus?.pulse
            || aiStatus?.description
            || t('bots.ai_desc')
            || 'AI Discretionary 1H v1.10 — защитный режим: BTC/ETH/SOL/XRP, высокий порог входа, ранний трейл и безубыток, риск ~1.5% на сделку.'
          }
          tags={[
            aiStatus?.model || aiStatus?.llm?.model || 'LLM',
            '1H',
            aiStatus?.execute ? 'execute' : 'signals',
          ]}
          tagline={coins.join(' · ')}
          pnl={(
            demoPnl?.per_bot?.['AI Discretionary 1H']
            ?? demoPnl?.total
            ?? aiStatus?.lifetime_pnl
            ?? aiStatus?.total_pnl
            ?? 0
          )}
          trades={demoPnl?.trades_counted ?? aiStatus?.lifetime_trades ?? aiStatus?.total_trades ?? 0}
          winRate={demoPnl?.win_rate ?? aiStatus?.win_rate}
          sparklinePnl={(
            demoPnl?.per_bot?.['AI Discretionary 1H']
            ?? demoPnl?.total
            ?? aiStatus?.lifetime_pnl
            ?? aiStatus?.total_pnl
            ?? 0
          )}
          startedAt={aiRunning ? aiStartedAt : null}
          openPositions={aiStatus?.open_positions || []}
          managed={aiStatus?.running}
          lastActivity={aiStatus?.last_activity}
          heartbeatMaxAge={(aiStatus?.config?.poll_interval_sec || 300) * 3}
          apiAlive={apiAlive}
          nextTickAt={aiStatus?.health?.next_tick_at}
          pollIntervalSec={aiStatus?.health?.poll_interval_sec || aiStatus?.config?.poll_interval_sec || 300}
          topSignals={(aiStatus?.top_signals || []).slice(0, 1)}
          onToggle={aiToggle}
          isGuest={isGuest}
          loading={aiLoading}
          t={t}
          showCapital={!aiRunning}
          capitalValue={aiCapital}
          onCapitalChange={(v) => setAiCapital(v)}
        />

        <LiveMirrorCard
          connected={!!liveStatus?.connected && liveStatus?.enabled !== false}
          liveStatus={liveStatus}
          loading={liveLoading}
          liveKey={liveKey} setLiveKey={setLiveKey}
          liveSecret={liveSecret} setLiveSecret={setLiveSecret}
          livePass={livePass} setLivePass={setLivePass}
          liveCapital={liveCapital} setLiveCapital={setLiveCapital}
          onConnect={liveConnect}
          onDisconnect={liveDisconnect}
          isGuest={isGuest}
          t={t}
        />

        {isAdmin && (
          <TrendPortfolioCard
            status={trendStatus}
            positions={trendPositions}
            trades={trendTrades}
            onRunCycle={trendCycle}
            loading={trendLoading}
            isGuest={isGuest}
            t={t}
          />
        )}

        {isAdmin && (
          <CorePortfolioCard
            status={coreStatus}
            positions={corePositions}
            trades={coreTrades}
            onRunCycle={coreCycle}
            loading={coreLoading}
            isGuest={isGuest}
            t={t}
          />
        )}
      </div>

      <ConfirmDialog
        open={confirmStopAll}
        onClose={() => setConfirmStopAll(false)}
        onConfirm={async () => {
          try { await api.aiStop() } catch {}
          try { await api.liveDisconnect() } catch {}
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
