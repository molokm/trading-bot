import React, { useState, useEffect, useCallback, useMemo } from 'react'
import {
  RefreshCw, Zap, Wifi, WifiOff, Bot, ArrowUpRight, ArrowDownRight,
} from 'lucide-react'
import { api } from '../services/api'
import { useTranslation } from '../hooks/useTranslation'

function fmt(n, digits = 2) {
  if (n == null || Number.isNaN(Number(n))) return '—'
  return Number(n).toLocaleString('en-US', {
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
  if (!n) return '$0.00'
  return (n > 0 ? '+$' : '-$') + fmt(Math.abs(n))
}

/** DEMO / LIVE fraction — same as iOS dashboard cards */
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
      const dash = await withTimeout(api.meDashboard().catch(() => api.getDashboard?.() || null))
      // Prefer dedicated me/dashboard payload shaped for mini
      if (dash && (dash.demo || dash.live || dash.trades)) {
        setData(dash)
      } else {
        // Fallback: assemble from public endpoints
        const [pnl, positions, trades, liveSt, aiSt] = await Promise.all([
          api.getPnlSummary?.().catch(() => api.getPnl?.().catch(() => null)),
          api.getPositions?.().catch(() => null),
          api.getPairedTrades?.(30).catch(() => null),
          api.liveStatus?.().catch(() => null),
          api.aiStatus?.().catch(() => null),
        ])
        setData({
          demo: {
            pnl: pnl?.total,
            session_pnl: pnl?.['1d'],
            week: pnl?.week,
            unrealized: pnl?.unrealized,
            equity: null,
            positions: positions?.positions || positions || [],
            pulse: aiSt?.pulse || aiSt?.status_text || aiSt?.description || '',
            running: !!(aiSt?.running || aiSt?.active),
          },
          live: {
            connected: !!(liveSt?.connected || liveSt?.enabled),
            total_pnl: liveSt?.total_pnl,
            session_pnl: liveSt?.session_pnl,
            week: liveSt?.week,
            unrealized_pnl: liveSt?.unrealized_pnl ?? liveSt?.unrealized,
            equity: liveSt?.equity,
            positions: liveSt?.open_positions || liveSt?.positions || [],
          },
          trades: trades?.trades || trades || [],
        })
      }
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
    // Sum closed DEMO trades when server total is missing
    let totalFromTrades = null
    const all = Array.isArray(data?.trades) ? data.trades : []
    if (all.length) {
      let s = 0
      let n = 0
      const EPOCH = Date.parse('2026-09-01T00:00:00Z')
      for (const tr of all) {
        const mode = String(tr.account_mode || tr.mode || 'demo').toLowerCase()
        if (mode === 'live') continue
        if (String(tr.reason || '').toLowerCase() === 'open') continue
        let ts = Number(tr.time || tr.exit_time || tr.ts || 0) || 0
        if (ts > 0 && ts < 1e12) ts *= 1000
        if (typeof tr.time === 'string') {
          const p = Date.parse(tr.time)
          if (!Number.isNaN(p)) ts = p
        }
        if (ts > 0 && ts < EPOCH) continue
        const pnl = Number(tr.pnl)
        if (!Number.isFinite(pnl) || Math.abs(pnl) < 1e-9) continue
        s += pnl
        n += 1
      }
      if (n > 0) totalFromTrades = Math.round(s * 100) / 100
    }
    return {
      total: totalFromTrades != null ? totalFromTrades : Number(d.pnl ?? 0),
      today: Number(d.session_pnl ?? 0),
      week: Number(d.week ?? d.pnl_week ?? 0),
      unreal,
      equity: d.equity ?? null,
      liveConnected,
      liveTotal: Number(L.total_pnl ?? L.strategy_realized ?? 0),
      liveToday: Number(L.session_pnl ?? L.pnl_1d ?? 0),
      liveWeek: Number(L.week ?? L.pnl_week ?? 0),
      liveUnreal: Number(L.unrealized_pnl ?? L.unrealized ?? 0),
      liveEquity: L.equity != null ? Number(L.equity) : null,
    }
  }, [data, liveConnected])

  const positions = useMemo(() => {
    const out = []
    const push = (list, mode) => {
      if (!Array.isArray(list)) return
      list.forEach((p, i) => {
        const coin = (p.inst_id || p.instId || p.symbol || p.coin || '').replace('-USDT-SWAP', '')
        const side = String(p.pos_side || p.side || p.posSide || '').toLowerCase()
        const isLong = side === 'long' || side === 'net' || side === 'buy'
        out.push({
          key: `${mode}-${coin || i}-${side}-${p.size || p.pos || i}`,
          coin: coin || '—',
          side: isLong ? 'long' : 'short',
          sideLabel: isLong ? 'LONG' : 'SHORT',
          size: Number(p.size ?? p.pos ?? p.sz ?? 0),
          entry: Number(p.entry_price ?? p.avgPx ?? p.entry ?? 0),
          mark: Number(p.mark_price ?? p.markPx ?? p.last ?? 0),
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
        const sideRaw = String(tr.pos_side || tr.side || '').toLowerCase()
        const isLong = sideRaw === 'long' || sideRaw === 'buy'
        const isShort = sideRaw === 'short' || sideRaw === 'sell'
        const inst = (tr.inst_id || tr.symbol || tr.coin || '').replace('-USDT-SWAP', '')
        return {
          key: `${mode}-${tr.ord_id || i}-${tr.time || tr.exit_time || i}`,
          inst: inst || '—',
          side: isLong ? 'long' : isShort ? 'short' : '',
          sideLabel: isLong ? 'LONG' : isShort ? 'SHORT' : '—',
          pnl: Number(tr.pnl || 0),
          mode,
          time: tr.exit_time || tr.time || '',
        }
      })
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

  const pulse = data?.demo?.pulse || data?.demo?.description || ''
  const demoOn = !!(data?.demo?.running)

  return (
    <div
      className="mini-app-root"
      style={{
        paddingTop: 'max(8px, env(safe-area-inset-top))',
        paddingBottom: 'max(8px, env(safe-area-inset-bottom))',
      }}
    >
      {/* Header — like iOS strip */}
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

      {/* 2×2 metrics — same as iOS dashboard */}
      <div className="mini-metrics">
        <div className="mini-metric">
          <div className="label">Нереализ. · демо/лайф</div>
          <div className="value">{dualPnl(metrics.unreal, metrics.liveUnreal, liveConnected)}</div>
        </div>
        <div className="mini-metric">
          <div className="label">Сегодня · демо/лайф</div>
          <div className="value">{dualPnl(metrics.today, metrics.liveToday, liveConnected)}</div>
        </div>
        <div className="mini-metric">
          <div className="label">Сумма · демо/лайф</div>
          <div className="value">{dualPnl(metrics.total, metrics.liveTotal, liveConnected)}</div>
        </div>
        <div className="mini-metric">
          <div className="label">Equity · демо/лайф</div>
          <div className="value mono text-[var(--txt)]">
            <span title="Демо">{metrics.equity != null ? `$${fmt(metrics.equity, 0)}` : '—'}</span>
            <span className="text-[var(--txt-muted)] mx-0.5 font-normal">/</span>
            <span title="Лайф" className={liveConnected ? '' : 'text-[var(--txt-muted)]'}>
              {liveConnected && metrics.liveEquity != null ? `$${fmt(metrics.liveEquity, 0)}` : '—'}
            </span>
          </div>
        </div>
      </div>

      {/* AI status + Russian pulse */}
      <div className="mini-ai">
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-1.5 min-w-0">
            <span
              className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${
                demoOn ? 'bg-[var(--profit)]' : 'bg-[var(--txt-muted)]'
              }`}
            />
            <Bot size={12} className="text-[var(--txt-muted)] flex-shrink-0" />
            <span className="text-[0.7rem] font-semibold truncate">AI Discretionary</span>
            <span
              className={`text-[0.55rem] font-bold px-1.5 py-0.5 rounded ${
                demoOn
                  ? 'bg-[var(--profit-dim)] text-[var(--profit)]'
                  : 'bg-[var(--surface-overlay)] text-[var(--txt-muted)]'
              }`}
            >
              {demoOn ? 'ВКЛ' : 'ВЫКЛ'}
            </span>
          </div>
        </div>
        {pulse ? (
          <div className="text-[0.65rem] leading-snug text-[var(--txt-secondary)] line-clamp-2">
            {pulse}
          </div>
        ) : null}
      </div>

      {/* Open positions */}
      <div className="mini-section mini-positions">
        <div className="mini-section-title">
          <span>Открытые позиции</span>
          <span>{positions.length}</span>
        </div>
        <div className="mini-panel flex-1 min-h-0 overflow-auto">
          {positions.length === 0 ? (
            <div className="py-3 text-center text-[0.7rem] text-[var(--txt-muted)]">Нет позиций</div>
          ) : (
            positions.map((p) => (
              <div key={p.key} className="mini-pos-row">
                <div className="flex items-center justify-between gap-2">
                  <div className="flex items-center gap-1.5 min-w-0">
                    <span className="text-[0.75rem] font-bold">{p.coin}</span>
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
                <div className="mt-0.5 flex flex-wrap gap-x-3 text-[0.58rem] text-[var(--txt-muted)] mono">
                  <span>
                    Размер <span className="text-[var(--txt)]">{p.size ? p.size.toFixed(3) : '—'}</span>
                  </span>
                  <span>
                    Вход{' '}
                    <span className="text-[var(--txt)]">
                      {p.entry ? `$${p.entry.toLocaleString(undefined, { maximumFractionDigits: 2 })}` : '—'}
                    </span>
                  </span>
                  <span>
                    Марка{' '}
                    <span className="text-[var(--txt)]">
                      {p.mark ? `$${p.mark.toLocaleString(undefined, { maximumFractionDigits: 2 })}` : '—'}
                    </span>
                  </span>
                </div>
              </div>
            ))
          )}
        </div>
      </div>

      {/* Last 3 trades */}
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
