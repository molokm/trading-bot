import React, { useState, useEffect, useCallback, useMemo } from 'react'
import {
  RefreshCw, Zap, Wifi, WifiOff, ArrowUpRight, ArrowDownRight,
} from 'lucide-react'
import { api } from '../services/api'
import { useTranslation } from '../hooks/useTranslation'

function fmt(n, digits = 2) {
  if (n == null || Number.isNaN(Number(n))) return '—'
  return Number(n).toLocaleString('ru-RU', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })
}

function pnlClass(v) {
  const n = Number(v)
  if (!n) return 'text-[var(--txt-secondary)]'
  return n > 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'
}

function pnlSign(v) {
  const n = Number(v) || 0
  if (!n) return '$0,00'
  const abs = fmt(Math.abs(n))
  return (n > 0 ? '+$' : '−$') + abs
}

/** DEMO / LIVE dual value — same as iOS dashboard metric cards */
function dualPnl(demoVal, liveVal, liveConnected) {
  return (
    <span className="inline-flex items-baseline gap-0.5 flex-wrap mono leading-tight">
      <span className={pnlClass(demoVal)} title="Демо">{pnlSign(demoVal)}</span>
      <span className="text-[var(--txt-muted)] font-normal text-[0.85em]">/</span>
      <span
        className={liveConnected ? pnlClass(liveVal) : 'text-[var(--txt-muted)]'}
        title="Лайф"
      >
        {liveConnected ? pnlSign(liveVal) : '—'}
      </span>
    </span>
  )
}

function withTimeout(promise, ms = 15000) {
  return new Promise((resolve, reject) => {
    const id = setTimeout(() => reject(new Error('timeout')), ms)
    promise.then(
      (v) => { clearTimeout(id); resolve(v) },
      (e) => { clearTimeout(id); reject(e) },
    )
  })
}

function normSide(raw) {
  const s = String(raw || '').toLowerCase()
  if (s === 'long' || s === 'buy' || s === 'net') return 'long'
  if (s === 'short' || s === 'sell') return 'short'
  return ''
}

function parseTs(v) {
  if (v == null || v === '') return 0
  if (typeof v === 'number') return v < 1e12 ? v * 1000 : v
  const p = Date.parse(String(v))
  return Number.isNaN(p) ? 0 : p
}

class MiniAppErrorBoundary extends React.Component {
  constructor(props) {
    super(props)
    this.state = { err: null }
  }
  static getDerivedStateFromError(err) {
    return { err }
  }
  render() {
    if (this.state.err) {
      return (
        <div className="min-h-[100dvh] flex flex-col items-center justify-center gap-3 p-6 bg-[var(--bg)] text-[var(--txt)]">
          <div className="text-sm font-semibold">Ошибка</div>
          <div className="text-2xs text-[var(--txt-muted)] text-center max-w-xs break-words">
            {String(this.state.err?.message || this.state.err)}
          </div>
          <button
            type="button"
            className="px-4 py-2 rounded-lg bg-[var(--info)] text-white text-sm font-semibold"
            onClick={() => window.location.reload()}
          >
            Обновить
          </button>
        </div>
      )
    }
    return this.props.children
  }
}

function MiniAppPageInner() {
  const { t } = useTranslation()
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  useEffect(() => {
    try { window.__MINI_APP__ = true } catch { /* ignore */ }
    try {
      const tg = window.Telegram && window.Telegram.WebApp
      if (tg) {
        tg.ready && tg.ready()
        tg.expand && tg.expand()
        try {
          if (tg.setHeaderColor) tg.setHeaderColor('secondary_bg_color')
          if (tg.setBackgroundColor) tg.setBackgroundColor('bg_color')
        } catch { /* ignore */ }
      }
    } catch { /* ignore */ }
  }, [])

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [dash, pnlDemo, pnlLive, positions, trades, liveSt, aiSt] = await Promise.all([
        withTimeout(api.meDashboard().catch(() => null)).catch(() => null),
        withTimeout(api.getPnlSummary?.({ mode: 'demo' }).catch(() => api.getPnl?.({ mode: 'demo' }).catch(() => null))).catch(() => null),
        withTimeout(api.getPnlSummary?.({ mode: 'live' }).catch(() => api.getPnl?.({ mode: 'live' }).catch(() => null))).catch(() => null),
        withTimeout(api.getPositions?.().catch(() => null)).catch(() => null),
        withTimeout(api.getPairedTrades?.(40).catch(() => null)).catch(() => null),
        withTimeout(api.liveStatus?.().catch(() => null)).catch(() => null),
        withTimeout(api.aiStatus?.().catch(() => null)).catch(() => null),
      ])

      const posList = positions?.positions || positions || []
      const tradeList = trades?.trades || trades || []

      const demoFromDash = dash?.demo || {}
      const liveFromDash = dash?.live || {}

      setData({
        demo: {
          pnl: pnlDemo?.total ?? demoFromDash.pnl ?? dash?.pnl?.total,
          session_pnl: pnlDemo?.['1d'] ?? demoFromDash.session_pnl,
          week: pnlDemo?.week ?? demoFromDash.week,
          unrealized: pnlDemo?.unrealized ?? demoFromDash.unrealized,
          equity: demoFromDash.equity ?? null,
          positions: demoFromDash.positions?.length ? demoFromDash.positions : posList,
          pulse: aiSt?.pulse || aiSt?.status_text_ru || aiSt?.status_text || aiSt?.description || demoFromDash.pulse || '',
          running: !!(aiSt?.running || aiSt?.active || demoFromDash.running),
        },
        live: {
          connected: !!(liveSt?.connected || liveFromDash.connected),
          total_pnl: pnlLive?.total ?? liveSt?.total_pnl ?? liveFromDash.total_pnl,
          session_pnl: pnlLive?.['1d'] ?? liveSt?.session_pnl ?? liveFromDash.session_pnl,
          week: pnlLive?.week ?? liveSt?.week ?? liveFromDash.week,
          unrealized_pnl: liveSt?.unrealized_pnl ?? liveSt?.unrealized ?? liveFromDash.unrealized,
          equity: liveSt?.equity ?? liveFromDash.equity,
          positions: liveSt?.open_positions || liveSt?.positions || liveFromDash.positions || [],
        },
        trades: Array.isArray(dash?.trades) && dash.trades.length
          ? dash.trades
          : tradeList,
      })
    } catch (e) {
      setError(String(e?.message || e || 'load failed'))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
    const id = setInterval(() => {
      if (typeof document !== 'undefined' && document.hidden) return
      load()
    }, 30000)
    return () => clearInterval(id)
  }, [load])

  const liveConnected = !!(data?.live?.connected)

  const metrics = useMemo(() => {
    const d = data?.demo || {}
    const L = data?.live || {}
    let unreal = Number(d.unrealized ?? 0)
    if (!unreal && Array.isArray(d.positions)) {
      unreal = d.positions.reduce((s, p) => s + Number(p.upl || p.unrealized_pnl || 0), 0)
    }
    let liveUnreal = Number(L.unrealized_pnl ?? L.unrealized ?? 0)
    if (!liveUnreal && Array.isArray(L.positions)) {
      liveUnreal = L.positions.reduce((s, p) => s + Number(p.upl || p.unrealized_pnl || 0), 0)
    }
    return {
      total: Number(d.pnl ?? 0),
      today: Number(d.session_pnl ?? 0),
      week: Number(d.week ?? d.pnl_week ?? 0),
      unreal,
      liveConnected,
      liveTotal: Number(L.total_pnl ?? L.strategy_realized ?? 0),
      liveToday: Number(L.session_pnl ?? L.pnl_1d ?? 0),
      liveWeek: Number(L.week ?? L.pnl_week ?? 0),
      liveUnreal,
    }
  }, [data, liveConnected])

  const positions = useMemo(() => {
    const out = []
    const push = (list, mode) => {
      if (!Array.isArray(list)) return
      list.forEach((p, i) => {
        const coin = String(p.inst_id || p.instId || p.symbol || p.coin || '')
          .replace('-USDT-SWAP', '')
          .replace('-USD-SWAP', '')
        const side = normSide(p.pos_side || p.side || p.posSide)
        const isLong = side === 'long'
        const size = Number(p.size ?? p.pos ?? p.sz ?? 0)
        const entry = Number(p.entry_price ?? p.avgPx ?? p.entry ?? 0)
        const mark = Number(p.mark_price ?? p.markPx ?? p.last ?? p.mark ?? 0)
        let notional = Number(p.notional ?? p.notionalUsd ?? p.margin ?? 0)
        if (!notional || !Number.isFinite(notional)) {
          const px = mark || entry
          const ct = Number(p.ctVal || p.ct_val || 1) || 1
          notional = Math.abs(size) * px * ct
        }
        out.push({
          key: `${mode}-${coin || i}-${side}-${size}-${i}`,
          coin: coin || '—',
          side: isLong ? 'long' : 'short',
          sideLabel: isLong ? 'LONG' : 'SHORT',
          size,
          entry,
          mark,
          notional,
          upl: Number(p.upl ?? p.unrealized_pnl ?? p.pnl ?? 0),
          mode,
        })
      })
    }
    push(data?.demo?.positions, 'demo')
    if (liveConnected) push(data?.live?.positions, 'live')
    return out
  }, [data, liveConnected])

  const viewTrades = useMemo(() => {
    const all = Array.isArray(data?.trades) ? data.trades : []
    const mapped = all
      .filter((tr) => String(tr.reason || '').toLowerCase() !== 'open')
      .map((tr, i) => {
        const mode = String(tr.account_mode || tr.mode || 'demo').toLowerCase() === 'live' ? 'live' : 'demo'
        let side = normSide(tr.pos_side)
        if (!side) side = normSide(tr.side)
        const inst = String(tr.inst_id || tr.symbol || tr.coin || '')
          .replace('-USDT-SWAP', '')
          .replace('-USD-SWAP', '')
        return {
          key: `${mode}-${tr.ord_id || i}-${tr.time || tr.exit_time || i}`,
          inst: inst || '—',
          side,
          sideLabel: side === 'long' ? 'LONG' : side === 'short' ? 'SHORT' : '—',
          pnl: Number(tr.pnl || 0),
          mode,
          ts: parseTs(tr.exit_time || tr.time || tr.ts),
        }
      })
      .sort((a, b) => (b.ts || 0) - (a.ts || 0))
    return mapped.slice(0, 3)
  }, [data?.trades])

  if (loading && !data) {
    return (
      <div className="mini-app-root items-center justify-center text-[var(--txt-muted)] text-sm">
        Загрузка…
      </div>
    )
  }

  if (error && !data) {
    return (
      <div className="mini-app-root items-center justify-center gap-3">
        <div className="text-sm font-semibold">Не удалось загрузить</div>
        <div className="text-2xs text-[var(--txt-muted)]">{error}</div>
        <button
          type="button"
          onClick={load}
          className="px-4 py-2 rounded-lg bg-[var(--info)] text-white text-sm font-semibold"
        >
          Повторить
        </button>
      </div>
    )
  }

  const pulse = data?.demo?.pulse || ''
  const demoOn = !!(data?.demo?.running)

  return (
    <div
      className="mini-app-root"
      style={{
        paddingTop: 'max(8px, env(safe-area-inset-top))',
        paddingBottom: 'max(8px, env(safe-area-inset-bottom))',
      }}
    >
      <div className="mini-header">
        <div className="flex items-center gap-1.5 min-w-0">
          <Zap size={14} className="text-[var(--info)] flex-shrink-0" />
          <span className="text-[0.85rem] font-bold tracking-tight truncate">
            {t('nav.dashboard') || 'COPIX'}
          </span>
        </div>
        <div className="flex items-center gap-1.5 flex-shrink-0">
          <span
            className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md text-[0.55rem] font-bold border ${
              liveConnected
                ? 'border-[var(--profit)]/40 bg-[var(--profit-dim)] text-[var(--profit)]'
                : 'border-[var(--border)] bg-[var(--surface)] text-[var(--txt-muted)]'
            }`}
            title={liveConnected ? 'Лайф подключён' : 'Лайф не подключён'}
          >
            {liveConnected ? <Wifi size={10} /> : <WifiOff size={10} />}
            {liveConnected ? 'ЛАЙФ ●' : 'ЛАЙФ —'}
          </span>
          <button
            type="button"
            onClick={load}
            className="p-1.5 rounded-lg border border-[var(--border)] bg-[var(--surface)] text-[var(--txt-muted)]"
            aria-label="Обновить"
          >
            <RefreshCw size={12} className={loading ? 'animate-spin' : ''} />
          </button>
        </div>
      </div>

      <div className="mini-metrics">
        <div className="mini-metric">
          <span className="label">Нереализ. PnL</span>
          <span className="value">{dualPnl(metrics.unreal, metrics.liveUnreal, liveConnected)}</span>
        </div>
        <div className="mini-metric">
          <span className="label">PnL сегодня</span>
          <span className="value">{dualPnl(metrics.today, metrics.liveToday, liveConnected)}</span>
        </div>
        <div className="mini-metric">
          <span className="label">PnL неделя</span>
          <span className="value">{dualPnl(metrics.week, metrics.liveWeek, liveConnected)}</span>
        </div>
        <div className="mini-metric">
          <span className="label">Сумма PnL</span>
          <span className="value">{dualPnl(metrics.total, metrics.liveTotal, liveConnected)}</span>
        </div>
      </div>

      <div className="mini-ai">
        <div className="flex items-center justify-between gap-2">
          <span className="text-[0.7rem] font-bold truncate">AI Discretionary 1H</span>
          <span
            className={`text-[0.55rem] font-bold px-1.5 py-0.5 rounded-md border ${
              demoOn
                ? 'border-[var(--profit)]/40 text-[var(--profit)] bg-[var(--profit-dim)]'
                : 'border-[var(--border)] text-[var(--txt-muted)]'
            }`}
          >
            {demoOn ? '● РАБОТАЕТ' : '○ СТОП'}
          </span>
        </div>
        {pulse ? (
          <div className="text-[0.65rem] leading-snug text-[var(--txt-secondary)] line-clamp-3">
            {pulse}
          </div>
        ) : (
          <div className="text-[0.65rem] text-[var(--txt-muted)]">Статус обновляется…</div>
        )}
      </div>

      <div className="mini-section mini-positions">
        <div className="mini-section-title">
          <span>Открытые позиции</span>
          <span>{positions.length || 0}</span>
        </div>
        <div className="mini-panel" style={{ maxHeight: '100%', overflow: 'auto' }}>
          {positions.length === 0 ? (
            <div className="py-2.5 text-center text-[0.7rem] text-[var(--txt-muted)]">Нет открытых позиций</div>
          ) : (
            positions.map((p) => (
              <div key={p.key} className="mini-pos-row">
                <div className="flex items-center justify-between gap-2 mb-0.5">
                  <div className="flex items-center gap-1.5 min-w-0">
                    <span className="text-[0.75rem] font-bold truncate">{p.coin}</span>
                    <span
                      className={`text-[0.55rem] font-bold ${
                        p.side === 'long' ? 'text-[var(--profit)]' : 'text-[var(--loss)]'
                      }`}
                    >
                      {p.sideLabel}
                    </span>
                    {p.mode === 'live' ? (
                      <span className="badge-live">LIVE</span>
                    ) : (
                      <span className="badge-demo">DEMO</span>
                    )}
                  </div>
                  <span className={`text-[0.75rem] font-bold mono ${pnlClass(p.upl)}`}>
                    {pnlSign(p.upl)}
                  </span>
                </div>
                <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-[0.6rem] text-[var(--txt-muted)] mono">
                  <span>
                    Объём{' '}
                    <span className="text-[var(--txt)]">
                      {p.notional > 0 ? `$${fmt(p.notional, 0)}` : '—'}
                    </span>
                  </span>
                  <span>
                    Вход{' '}
                    <span className="text-[var(--txt)]">
                      {p.entry ? `$${fmt(p.entry)}` : '—'}
                    </span>
                  </span>
                  <span>
                    Марка{' '}
                    <span className="text-[var(--txt)]">
                      {p.mark ? `$${fmt(p.mark)}` : '—'}
                    </span>
                  </span>
                </div>
              </div>
            ))
          )}
        </div>
      </div>

      <div className="mini-section mini-trades">
        <div className="mini-section-title">
          <span>Сделки</span>
          <span>последние 3</span>
        </div>
        <div className="mini-panel">
          {viewTrades.length === 0 ? (
            <div className="py-2.5 text-center text-[0.7rem] text-[var(--txt-muted)]">Нет сделок</div>
          ) : (
            viewTrades.map((tr) => (
              <div key={tr.key} className="mini-trade-row">
                <div className="flex items-center gap-1.5 min-w-0">
                  {Number(tr.pnl) >= 0 ? (
                    <ArrowUpRight size={12} className="text-[var(--profit)] flex-shrink-0" />
                  ) : (
                    <ArrowDownRight size={12} className="text-[var(--loss)] flex-shrink-0" />
                  )}
                  <div className="text-[0.7rem] font-semibold truncate">
                    {tr.inst}{' '}
                    <span
                      className={`font-bold text-[0.55rem] ${
                        tr.side === 'long'
                          ? 'text-[var(--profit)]'
                          : tr.side === 'short'
                            ? 'text-[var(--loss)]'
                            : 'text-[var(--txt-muted)]'
                      }`}
                    >
                      {tr.sideLabel}
                    </span>
                    {tr.mode === 'live' ? (
                      <span className="badge-live ml-1">LIVE</span>
                    ) : (
                      <span className="badge-demo ml-1">DEMO</span>
                    )}
                  </div>
                </div>
                <span className={`text-[0.7rem] font-bold mono flex-shrink-0 ${pnlClass(tr.pnl)}`}>
                  {pnlSign(tr.pnl)}
                </span>
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  )
}

export default function MiniAppPage(props) {
  return (
    <MiniAppErrorBoundary>
      <MiniAppPageInner {...props} />
    </MiniAppErrorBoundary>
  )
}
