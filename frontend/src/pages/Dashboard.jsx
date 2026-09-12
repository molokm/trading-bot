import React, { useState, useEffect, useMemo, useRef } from 'react'
import {
  Wallet, TrendingUp, TrendingDown, Activity, XCircle, Loader2, Zap,
  ArrowUpRight, ArrowDownRight, BarChart3, Play, Square, ChevronDown, Filter, ScrollText,
  Clock, Bot, FlaskConical, AlertTriangle, RefreshCw, ShieldAlert
} from 'lucide-react'
import { api } from '../services/api'
import { MetricCard, EnhancedMetricCard, Tip, StatusBadge, Chip, PnlBar, EmptyState, Loader, Skeleton, SkeletonMetricCard } from '../components/ui'
import { useTranslation } from '../hooks/useTranslation'
import { fmtTs } from '../utils/time'

const PAIRS = ['Все', 'BTC', 'ETH', 'BNB', 'XRP', 'SOL', 'DOGE', 'ADA', 'TRX', 'AVAX', 'LTC']

// Coins the bot actively trades — shown as live price cards on the dashboard
const PRICE_COINS = ['BTC', 'ETH', 'BNB', 'XRP', 'SOL', 'DOGE', 'ADA', 'TRX', 'AVAX', 'LTC']
/** AI product mode — Discretionary + optional Scale-In on dashboard */
const AI_ONLY_MODE = true


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

/** Unified bot panel on Dashboard (same layout for Discretionary & Scale-In). */
function DashBotPanel({
  title, version, running, loading, accent = 'text-[var(--accent)]',
  pnl, trades, winRate, openCount, model, capital, pulse, tagline,
  isGuest, onStart, onStop, startLabel, t,
}) {
  const pnlN = Number(pnl ?? 0)
  const pnlCls = pnlN >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'
  return (
    <div className="panel flex-shrink-0">
      <div className="panel-header">
        <Bot size={13} className={accent} />
        <span className="flex-1 truncate">{title}</span>
        {loading && !running && (
          <span className="text-2xs text-[var(--warn)] mr-1">…</span>
        )}
        {version && (
          <span className="text-2xs text-[var(--txt-muted)] mono mr-1">{version}</span>
        )}
        {running
          ? <StatusBadge mode="live" label={t('dash.running')} />
          : <StatusBadge mode="stopped" label={t('dash.stopped')} />}
      </div>
      <div className="p-3 space-y-2.5 text-2xs">
        <div className="grid grid-cols-4 gap-1.5">
          <div className="rounded-md bg-[var(--bg)] border border-[var(--border)] p-1.5">
            <div className="text-[var(--txt-muted)]">PnL</div>
            <div className={`mono font-semibold ${pnlCls}`}>
              {pnlN >= 0 ? '+' : ''}{pnlN.toFixed(2)}
            </div>
          </div>
          <div className="rounded-md bg-[var(--bg)] border border-[var(--border)] p-1.5">
            <div className="text-[var(--txt-muted)]">Сделок</div>
            <div className="mono font-semibold">{trades ?? 0}</div>
          </div>
          <div className="rounded-md bg-[var(--bg)] border border-[var(--border)] p-1.5">
            <div className="text-[var(--txt-muted)]">WR</div>
            <div className="mono font-semibold">{winRate != null ? `${winRate}%` : '—'}</div>
          </div>
          <div className="rounded-md bg-[var(--bg)] border border-[var(--border)] p-1.5">
            <div className="text-[var(--txt-muted)]">Поз.</div>
            <div className="mono font-semibold">{openCount ?? 0}</div>
          </div>
        </div>
        <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-[var(--txt-muted)]">
          {model && <span>Модель: <span className="text-[var(--txt)] mono">{model}</span></span>}
          {capital != null && <span>Капитал: <span className="text-[var(--txt)] mono">${Number(capital).toLocaleString()}</span></span>}
          {tagline && <span className="w-full text-[11px]">{tagline}</span>}
        </div>
        {(pulse) && (
          <div className="p-2 rounded-md bg-[var(--bg)] border border-[var(--border)] max-h-28 overflow-y-auto text-[11px] leading-relaxed text-[var(--txt)] whitespace-pre-wrap break-words">
            {pulse}
          </div>
        )}
        {!isGuest && (
          <div className="flex flex-col gap-1.5 pt-0.5">
            {running ? (
              <button type="button" className="btn btn-danger btn-sm w-full" onClick={onStop}>
                <Square size={12} /> {t('dash.stop_bot')}
              </button>
            ) : (
              <button type="button" className="btn btn-primary btn-sm w-full" onClick={onStart}>
                <Play size={12} /> {startLabel || t('dash.start')}
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

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
  const [aiScaleStatus, setAiScaleStatus] = useState(null)
  const [smartMoneyStatus, setSmartMoneyStatus] = useState(null)
  const [vwapRevStatus, setVwapRevStatus] = useState(null)
  const [aiBusy, setAiBusy] = useState(false)
  const [tradeLog, setTradeLog] = useState([])
  const [pnl, setPnl] = useState(null)
  // Drop PnL when switching Demo ↔ Live so cards never keep the other mode's total
  useEffect(() => {
    setPnl(null)
  }, [demoMode])
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

  // Reload on connect AND on Demo↔Live switch (mode change invalidates server cache)
  useEffect(() => {
    loadData({ fastOnly: false })
    const interval = setInterval(() => {
      if (typeof document !== 'undefined' && document.hidden) return
      loadData({ fastOnly: false })
    }, 30000) // was 20s — less load on Render free tier
    return () => clearInterval(interval)
  }, [connected, demoMode])

  useEffect(() => {
    const onVis = () => { if (!document.hidden) loadData({ fastOnly: true }) }
    const onMode = () => { loadData({ fastOnly: false }) }
    document.addEventListener('visibilitychange', onVis)
    window.addEventListener('trading-mode-changed', onMode)
    return () => {
      document.removeEventListener('visibilitychange', onVis)
      window.removeEventListener('trading-mode-changed', onMode)
    }
  }, [connected, demoMode])

  async function loadData(opts = {}) {
    if (!connected) { setLoading(false); return }
    if (document.hidden && !opts.force) return
    const fastOnly = !!opts.fastOnly
    const isLive = !demoMode

    // ── FAST: portfolio + positions + AI status (paint UI ASAP) ──
    try {
      const [pf, pos, aiSt, priceTickers] = await Promise.all([
        api.getPortfolio().catch(() => null),
        api.getPositions('SWAP').catch(() => null),
        api.aiStatus().catch(() => null),
        api.getTickers(PRICE_COINS.map(c => `${c}-USDT-SWAP`)).catch(() => null),
      ])
      if (pf) setPortfolio(pf)
      if (pos) setPositions(pos.positions || [])
      if (aiSt && !aiSt.detail) setAiStatus(aiSt)
      if (priceTickers?.tickers) {
        const byCoin = {}
        priceTickers.tickers.forEach(tp => {
          const id = (tp.instId || '').replace('-USDT-SWAP', '')
          if (id) byCoin[id] = tp
        })
        setTickers(byCoin)
      }
      setDataFreshAt(Date.now())
      setLoading(false)
    } catch {
      setLoading(false)
    }
    if (fastOnly) return

    // ── SLOW: trades + PnL (server-cached; never block first paint) ──
    try {
      const [trades, pnlData] = await Promise.all([
        api.getPairedTrades(50).catch(() => null),  // was 200 — heavy
        api.getPnlSummary().catch(() => api.getPnl()).catch(() => null),
      ])
      if (trades) setTradeLog(trades.trades || [])
      if (pnlData && !pnlData.detail && (pnlData.total != null || pnlData['1d'] != null || pnlData.per_bot)) {
        const src = String(pnlData.source || pnlData.engine || '')
        if (src.includes('exchange_close') || src.includes('pnl_engine') || pnlData.engine) {
          setPnl(pnlData)
        }
      }
    } catch { /* ignore slow-tier errors */ }
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
        <EnhancedMetricCard
          label={t('dash.balance')}
          value={totalEquity ? `$${totalEquity.toLocaleString()}` : '---'}
          icon={Wallet}
          mono
          tip={t('dash.balance_tip')}
          sparkData={sparkData[0]}
          subtitle={demoMode ? 'Demo Account' : 'Live Account'}
        />
        <EnhancedMetricCard
          label={t('dash.unrealized')}
          value={unrealizedPnl >= 0 ? `+$${fmt(unrealizedPnl)}` : `-$${fmt(Math.abs(unrealizedPnl))}`}
          changeType={unrealizedPnl >= 0 ? 'positive' : 'negative'}
          icon={Activity}
          mono
          tip={t('dash.unrealized_tip')}
          sparkData={sparkData[1]}
        />
        <EnhancedMetricCard
          label={t('dash.pnl_day')}
          value={pnlDay >= 0 ? `+$${fmt(pnlDay)}` : `-$${fmt(Math.abs(pnlDay))}`}
          changeType={pnlDay >= 0 ? 'positive' : 'negative'}
          icon={pnlDay >= 0 ? TrendingUp : TrendingDown}
          mono
          tip={`${t('dash.pnl_day_tip')} (${pnlTz}, только активные боты)`}
          sparkData={sparkData[2]}
        />
        <EnhancedMetricCard
          label={t('dash.pnl_week')}
          value={pnlWeek >= 0 ? `+$${fmt(pnlWeek)}` : `-$${fmt(Math.abs(pnlWeek))}`}
          changeType={pnlWeek >= 0 ? 'positive' : 'negative'}
          icon={BarChart3}
          mono
          tip={t('dash.pnl_week_tip')}
          sparkData={sparkData[3]}
        />
        <EnhancedMetricCard
          label={t('dash.total_pnl')}
          value={
            <div className="flex flex-col gap-0.5">
              <span className={pnlTotal >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}>
                {pnlTotal >= 0 ? `+$${fmt(pnlTotal)}` : `-$${fmt(Math.abs(pnlTotal))}`}
              </span>
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
          icon={pnlTotal >= 0 ? ArrowUpRight : ArrowDownRight}
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
                      const bn = resolveBotName(p)
                      if (!bn || bn === 'Smart Money') return false
                      return true
                    }).map((p, i) => {
                      const upl = parseFloat(p.upl || 0)
                      const roe = parseFloat(p.uplRatio || 0) * 100
                      const posId = `${p.instId}_${p.posSide}`
                      const posSideKey = (p.posSide || 'long').toLowerCase()
                      const botName = resolveBotName(p)
                      const botBadge = botName === 'Momentum'
                        ? { label: 'MOM', cls: 'bg-blue-500/20 text-blue-400 border border-blue-500/30' }
                        : (botName === 'Impulse' || botName === 'Impulse 1D')
                        ? { label: 'IMP', cls: 'bg-violet-500/20 text-violet-400 border border-violet-500/30' }
                        : botName === 'AI Scale-In 1H' || botName === 'AI Scale-In'
                        ? { label: 'SCL', cls: 'bg-fuchsia-500/20 text-fuchsia-400 border border-fuchsia-500/30' }
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
                      : (tr.bot === 'AI Scale-In 1H' || tr.bot === 'AI Scale-In')
                      ? { label: 'SCL', cls: 'bg-fuchsia-500/20 text-fuchsia-400 border border-fuchsia-500/30' }
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


          <DashBotPanel
            title="AI Discretionary 1H"
            version={aiStatus?.version}
            running={!!aiStatus?.running}
            loading={!aiStatus}
            accent="text-[var(--accent)]"
            pnl={discPnlResolved}
            trades={discTradesResolved}
            winRate={aiStatus?.win_rate}
            openCount={(aiStatus?.open_positions || []).length}
            model={aiStatus?.model || aiStatus?.llm?.model}
            capital={aiStatus?.capital ?? aiStatus?.config?.capital}
            pulse={aiStatus?.pulse || aiStatus?.description || aiStatus?.last_decision?.reason}
            tagline={aiStatus?.config?.symbols ? (aiStatus.config.symbols || []).join(' · ') : 'BTC · ETH · SOL · XRP'}
            isGuest={isGuest}
            t={t}
            startLabel={`${t('dash.start')} AI`}
            onStart={async () => {
              try {
                await api.aiStart({ capital: 10000, provider: 'groq', execute: true })
                loadData()
              } catch (e) { alert(e.message) }
            }}
            onStop={async () => {
              try { await api.aiStop(); loadData() } catch (e) { alert(e.message) }
            }}
          />


        </div>
      </div>
    </div>
  )
}